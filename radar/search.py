"""Vyhľadávanie cez vlastnú inštanciu SearXNG (beží v Dockeri, bez platených API)."""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from .config import SearchConfig
from .util import canonical_url

log = logging.getLogger(__name__)


class SearchUnavailable(RuntimeError):
    """SearXNG nepočúva (Docker alebo kontajner nebeží). Krok sa zastaví hneď – agent by inak minul všetky kroky
    na opakované hľadanie a test hypotézy by sa zarátal ako neúspešný (úloha #108, 15. 10. 2026)."""


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


class SearxClient:
    def __init__(self, cfg: SearchConfig, user_agent: str):
        self.cfg = cfg
        self.http = httpx.Client(timeout=40, headers={"User-Agent": user_agent})
        self._last = 0.0
        self.penalty = 0.0  # pauza navyše, kým vyhľadávače hlásia blokovanie (rastie a klesá)
        self._cache: dict[tuple[str, int], tuple[float, list[SearchResult], list[str]]] = {}
        # Vyhľadávač, ktorý hlási CAPTCHA, limit alebo výpadok, sa na chvíľu vynechá: dopyt ide iba funkčným
        # (úloha #75: DuckDuckGo CAPTCHA, Google CSE limit, Bing výpadky – 5 hľadaní agenta s nulou výsledkov).
        self.suspended: dict[str, float] = {}
        self._engines: list[str] | None = None
        self.last_blocked: list[str] = []
        self.stats: Counter = Counter()  # hľadania, prázdne, prázdne pri blokovaní, s vynechanými vyhľadávačmi

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        """Výsledky z pamäte (rovnaký dopyt), inak dopyt; pri prázdnom výsledku a blokovaných vyhľadávačoch
        jedno zopakovanie po pauze a pri frázach v úvodzovkách pokus bez nich (reálne behy 12. 10. 2026:
        DuckDuckGo CAPTCHA, Google CSE „too many requests“, z 26 hľadaní kontaktu uspelo jedno)."""
        n = limit or self.cfg.results_per_query
        key = (" ".join(query.split()).lower(), n)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cfg.cache_minutes * 60:
            self.last_blocked = list(hit[2])
            return list(hit[1])
        self.stats["hľadania"] += 1
        if self.healthy_engines():  # za hľadanie, nie za dopyt (opakovanie pri prázdnom výsledku sa neráta znova)
            self.stats["s_vynechanými_vyhľadávačmi"] += 1
        results, blocked = self._query(query, n)
        if not results and blocked and self.cfg.retry_on_empty:
            self.penalty = min(self.cfg.max_backoff_s, max(4.0, self.penalty * 2))
            log.info("SearXNG: vyhľadávače blokujú (%s), opakujem po %.0f s", ", ".join(blocked), self.penalty)
            time.sleep(self.penalty)
            results, blocked = self._query(query, n)
        if not results and self.cfg.retry_on_empty and '"' in query:
            results, blocked = self._query(query.replace('"', ""), n)
        if results:
            self.penalty /= 2
        else:
            self.stats["prázdne"] += 1
        self.last_blocked = blocked
        if not results and blocked:
            self.stats["prázdne_pri_blokovaní"] += 1
            log.warning("SearXNG '%s': žiadne výsledky, nedostupné vyhľadávače: %s", query, blocked)
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), results, blocked)
        return list(results)

    def enabled_engines(self) -> list[str]:
        """Zapnuté vyhľadávače SearXNG (GET /config, raz za behu); pri chybe [] = bez obmedzenia."""
        if self._engines is None:
            try:
                data = self.http.get(self.cfg.searxng_url.rstrip("/") + "/config").json()
                self._engines = [e["name"] for e in data.get("engines", [])
                                 if e.get("enabled") and "general" in (e.get("categories") or [])]
            except Exception:  # noqa: BLE001 – starší SearXNG alebo výpadok: hľadá sa bez výberu vyhľadávačov
                self._engines = []
        return self._engines

    def healthy_engines(self) -> list[str] | None:
        """Vyhľadávače bez nedávneho blokovania; None = nič nie je vynechané (SearXNG použije svoje nastavenie)."""
        now = time.monotonic()
        self.suspended = {e: t for e, t in self.suspended.items() if t > now}
        if not self.suspended:
            return None
        healthy = [e for e in self.enabled_engines() if e not in self.suspended]
        return healthy or None

    def _query(self, query: str, n: int) -> tuple[list[SearchResult], list[str]]:
        # Pauza medzi dopytmi: vyhľadávače za SearXNG inak dočasne blokujú (CAPTCHA, "too many requests").
        wait = self.cfg.delay_s + self.penalty - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        params = {"q": query, "format": "json", "language": self.cfg.language, "safesearch": 0}
        engines = self.healthy_engines()
        if engines:
            params["engines"] = ",".join(engines)
        try:
            resp = self.http.get(self.cfg.searxng_url.rstrip("/") + "/search", params=params)
        except httpx.ConnectError as exc:
            raise SearchUnavailable(f"SearXNG nebeží na {self.cfg.searxng_url} – spusti Docker Desktop a kontajner "
                                    "radar-searxng (po reštarte najprv scripts\\fix_docker_sockets.ps1)") from exc
        if resp.status_code == 403:
            raise RuntimeError("SearXNG nemá povolený formát JSON – pozri searxng/settings.yml (search.formats).")
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in data.get("results", []):
            url = canonical_url(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(url=url, title=item.get("title") or "", snippet=item.get("content") or ""))
            if len(results) >= n:
                break
        blocked = [f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) > 1 else str(e)
                   for e in data.get("unresponsive_engines") or []]
        until = time.monotonic() + self.cfg.engine_suspend_minutes * 60
        for e in data.get("unresponsive_engines") or []:
            self.suspended[e[0] if isinstance(e, (list, tuple)) else str(e)] = until
        log.debug("SearXNG '%s': %d výsledkov", query, len(results))
        return results, blocked
