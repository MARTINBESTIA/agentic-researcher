"""Vyhľadávanie cez vlastnú inštanciu SearXNG (beží v Dockeri, bez platených API)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .config import SearchConfig
from .util import canonical_url

log = logging.getLogger(__name__)


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

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        # Pauza medzi dopytmi: vyhľadávače za SearXNG inak dočasne blokujú (CAPTCHA, "too many requests").
        wait = self.cfg.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        params = {"q": query, "format": "json", "language": self.cfg.language, "safesearch": 0}
        resp = self.http.get(self.cfg.searxng_url.rstrip("/") + "/search", params=params)
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
            if len(results) >= (limit or self.cfg.results_per_query):
                break
        if not results and data.get("unresponsive_engines"):
            log.warning("SearXNG '%s': žiadne výsledky, nedostupné vyhľadávače: %s", query, data["unresponsive_engines"])
        log.debug("SearXNG '%s': %d výsledkov", query, len(results))
        return results
