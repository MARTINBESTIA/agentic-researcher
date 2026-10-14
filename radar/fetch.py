"""Zdvorilé sťahovanie stránok a PDF: robots.txt, pauzy medzi požiadavkami, extrakcia textu.

Sociálne siete (LinkedIn, Facebook, …) sa nesťahujú nikdy, ani keď na ne presmeruje iná
stránka: presmerovania sa preto sledujú ručne a každý krok sa kontroluje (R-PPL-3).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from typing import Any
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from pypdf import PdfReader

from .config import DEFAULT_BLOCKED_DOMAINS, FetchConfig
from .util import canonical_url, tls_context

log = logging.getLogger(__name__)

_HREF_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"'#]+)["']""", re.I)
MAX_REDIRECTS = 5


class BlockedDomainError(RuntimeError):
    """URL alebo presmerovanie vedie na blokovanú doménu (sociálne siete)."""


@dataclass
class FetchedPage:
    url: str
    final_url: str
    title: str
    text: str
    content_hash: str


@dataclass
class HtmlPage:
    """Surové HTML na spracovanie v pamäti (kontaktné stránky sa celé neukladajú, R-PPL-2)."""

    url: str
    final_url: str
    html: str
    content_hash: str
    status: str = "ok"  # ok | robots_blocked | blocked_domain | error


def domain_blocked(url: str, blocked: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in blocked)


class Fetcher:
    def __init__(self, cfg: FetchConfig, blocked_domains: list[str] | None = None):
        self.cfg = cfg
        self.blocked = sorted(set(blocked_domains or cfg.blocked_domains or DEFAULT_BLOCKED_DOMAINS))
        self.http = httpx.Client(
            timeout=cfg.timeout_s, verify=tls_context(),
            follow_redirects=False,  # presmerovania sledujeme sami (kontrola blokovaných domén)
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "sk,cs;q=0.8,en;q=0.6"},
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            parser: RobotFileParser | None = None
            try:
                self._wait(parsed.netloc)
                resp = self.http.get(base + "/robots.txt", follow_redirects=True)
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None  # robots.txt nedostupný = bez obmedzení
            self._robots[base] = parser
        parser = self._robots[base]
        return True if parser is None else parser.can_fetch(self.cfg.user_agent, url)

    def sitemaps(self, url: str) -> list[str]:
        """Riadky „Sitemap:“ z robots.txt webu (robots.txt sa číta raz a pamätá ako pri kontrole povolenia)."""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            respect, self.cfg.respect_robots = self.cfg.respect_robots, True  # načíta robots.txt aj pri vypnutej kontrole
            try:
                self.allowed(url)
            finally:
                self.cfg.respect_robots = respect
        parser = self._robots.get(base)
        return list(parser.site_maps() or []) if parser is not None else []

    def _wait(self, domain: str) -> None:
        last = self._last_hit.get(domain)
        if last is not None:
            delta = time.monotonic() - last
            if delta < self.cfg.per_domain_delay_s:
                time.sleep(self.cfg.per_domain_delay_s - delta)
        self._last_hit[domain] = time.monotonic()

    def _request(self, url: str) -> tuple[httpx.Response | None, str]:
        """GET s ručným sledovaním presmerovaní; vráti (odpoveď, stav)."""
        for _ in range(MAX_REDIRECTS + 1):
            if not url.startswith(("http://", "https://")):
                return None, "error"
            if domain_blocked(url, self.blocked):
                log.info("Blokovaná doména, nesťahujem: %s", urlparse(url).hostname)
                return None, "blocked_domain"
            if not self.allowed(url):
                log.info("robots.txt nepovoľuje %s", url)
                return None, "robots_blocked"
            self._wait(urlparse(url).netloc)
            try:
                resp = self.http.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                log.warning("Nepodarilo sa stiahnuť %s: %s", url, exc)
                return None, "error"
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(str(resp.url), resp.headers["location"])
                continue
            if resp.status_code >= 400:
                log.warning("Nepodarilo sa stiahnuť %s: HTTP %s", url, resp.status_code)
                return None, "error"
            return resp, "ok"
        log.warning("Priveľa presmerovaní: %s", url)
        return None, "error"

    def _get(self, url: str) -> httpx.Response | None:
        resp, _ = self._request(url)
        return resp

    def fetch(self, url: str) -> FetchedPage | None:
        url = canonical_url(url)
        resp = self._get(url)
        if resp is None:
            return None

        ctype = resp.headers.get("content-type", "").lower()
        if "pdf" in ctype or urlparse(str(resp.url)).path.lower().endswith(".pdf"):
            title, text = self._pdf_text(resp.content)
        elif "html" in ctype or "xml" in ctype or not ctype:
            # Bajty, nie resp.text: trafilatura rozpozná kódovanie aj z <meta charset> (napr. windows-1250).
            title, text = self._html_text(resp.content, str(resp.url))
        elif ctype.startswith("text/"):
            title, text = "", resp.text
        else:
            log.debug("Preskakujem nepodporovaný typ %s (%s)", ctype, url)
            return None

        text = (text or "").strip()[: self.cfg.max_chars]
        if len(text) < 200:
            log.debug("Príliš krátky text, preskakujem %s", url)
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return FetchedPage(url=url, final_url=str(resp.url), title=title or url, text=text, content_hash=digest)

    def fetch_json(self, url: str) -> Any | None:
        """JSON (napr. popis API OpenAPI/Swagger); robots.txt a blokované domény platia ako pri stránkach."""
        resp, _ = self._request(url)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def fetch_html(self, url: str) -> HtmlPage:
        """Surové HTML (napr. kontaktná stránka). Nič sa neukladá; volajúci spracuje stránku v pamäti."""
        resp, status = self._request(url)
        if resp is None:
            return HtmlPage(url=url, final_url=url, html="", content_hash="", status=status)
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return HtmlPage(url=url, final_url=str(resp.url), html="", content_hash="", status="error")
        html = resp.text
        return HtmlPage(
            url=url, final_url=str(resp.url), html=html,
            content_hash=hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest(), status="ok",
        )

    def links(self, url: str, pattern: str, limit: int) -> list[str]:
        """Odkazy zo stránky so zoznamom dokumentov, ktoré zodpovedajú regulárnemu výrazu (v poradí na stránke)."""
        resp = self._get(url)
        if resp is None:
            return []
        regex = re.compile(pattern, re.I)
        found: list[str] = []
        for href in _HREF_RE.findall(resp.text):
            absolute = canonical_url(urljoin(str(resp.url), unescape(href.strip())))
            if absolute.startswith(("http://", "https://")) and regex.search(absolute) and absolute not in found:
                found.append(absolute)
                if len(found) >= limit:
                    break
        return found

    def _html_text(self, html: bytes | str, url: str) -> tuple[str, str]:
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_precision=True)
        title = ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta is not None and meta.title:
                title = meta.title
        except Exception:  # metadáta nie sú podstatné
            pass
        return title, text or ""

    def _pdf_text(self, data: bytes) -> tuple[str, str]:
        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:
            log.warning("PDF sa nedá otvoriť: %s", exc)
            return "", ""
        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= self.cfg.max_pdf_pages:
                break
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        title = ""
        try:
            if reader.metadata and reader.metadata.title:
                title = str(reader.metadata.title)
        except Exception:
            pass
        return title, "\n\n".join(pages)
