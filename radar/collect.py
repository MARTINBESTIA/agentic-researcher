"""Krok 1 – zber: pevné URL, zoznamové stránky (napr. ÚHP), RSS a vyhľadávanie podľa tém a projektov.

Pri RSS model najprv z titulkov vyberie články, ktoré pravdepodobne opisujú konkrétny problém
(triáž). Jeden dopyt na 25 titulkov je lacný; extrakcia nerelevantného článku je drahá.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from itertools import zip_longest
from typing import Any, Optional

from openai import APIError
from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .fetch import Fetcher
from .models import as_router
from .prompts import TRIAGE_SYSTEM
from .search import SearxClient
from .discover import parse_feed
from .util import Deadline, canonical_url

log = logging.getLogger(__name__)

Candidate = tuple[str, str, Optional[str], Optional[str]]  # url, druh zdroja, téma, dopyt

_TAG_RE = re.compile(r"<[^>]+>")


class Triage(BaseModel):
    selected: list[int] = Field(default_factory=list)


def triage(cfg: Config, llm: Any, items: list[tuple[str, str]], stats: Counter, db: DB | None = None) -> set[int]:
    """Indexy položiek (titulok, perex), ktoré model považuje za relevantné. Pri chybe nechá všetky."""
    if llm is None or not items:
        return set(range(len(items)))
    router = as_router(llm, cfg, db)
    topics = "; ".join(t.name for t in cfg.all_topics()) or "-"
    keep: set[int] = set()
    for start in range(0, len(items), 25):
        batch = items[start : start + 25]
        listing = "\n".join(
            f"{start + i}: {title} – {' '.join(_TAG_RE.sub(' ', summary).split())[:200]}"
            for i, (title, summary) in enumerate(batch)
        )
        try:
            res = router.run_task(
                "triage",
                [
                    {"role": "system", "content": TRIAGE_SYSTEM.format(topics=topics)},
                    {"role": "user", "content": f"<untrusted_items>\n{listing}\n</untrusted_items>"},
                ],
                Triage,
                max_tokens=400,
            )
        except APIError as exc:
            res = None
            log.warning("Triáž zlyhala: %s", exc)
        if res is not None and res.ok:
            keep |= {i for i in res.value.selected if start <= i < start + len(batch)}
        else:
            log.warning("Triáž zlyhala, beriem všetky položky.")
            stats["triage_failures"] += 1
            keep |= set(range(start, start + len(batch)))
    stats["triage_dropped"] += len(items) - len(keep)
    return keep


def _fresh(cfg: Config, db: DB, urls: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Len URL, ktoré sme v poslednom čase nesťahovali (aby sa triáž neopakovala pri každom behu)."""
    out, seen = [], set()
    for url, title, summary in urls:
        url = canonical_url(url or "")
        if url.startswith(("http://", "https://")) and url not in seen and not db.url_recently_checked(url, cfg.fetch.refetch_days):
            seen.add(url)
            out.append((url, title, summary))
    return out


def gather_candidates(
    cfg: Config, db: DB, searx: SearxClient, fetcher: Fetcher, llm: Any, stats: Counter
) -> list[Candidate]:
    """Zoznam URL na stiahnutie. Zdroje sa striedajú, aby jeden nevyčerpal celý rozpočet behu."""
    groups: list[list[Candidate]] = [[(canonical_url(url), "seed", None, None) for url in cfg.seed_urls]]

    for source in cfg.link_sources:
        try:
            links = _fresh(cfg, db, [(u, "", "") for u in fetcher.links(source.url, source.pattern, source.max_items)])
        except re.error as exc:
            log.warning("Neplatný regulárny výraz pri zdroji %s: %s", source.name, exc)
            stats["link_errors"] += 1
            continue
        keep = triage(cfg, llm, [(u, "") for u, _, _ in links], stats, db) if source.triage else set(range(len(links)))
        groups.append([(u, "links", source.name, None) for i, (u, _, _) in enumerate(links) if i in keep])

    for feed in cfg.feeds:
        try:
            parsed = parse_feed(cfg, feed.url)
        except Exception as exc:
            log.warning("RSS %s zlyhalo: %s", feed.url, exc)
            stats["feed_errors"] += 1
            continue
        if not parsed.entries:
            log.warning("RSS %s nevrátilo žiadne položky", feed.url)
            stats["feed_errors"] += 1
            continue
        feed_url = canonical_url(feed.url)
        entries = _fresh(
            cfg, db, [(e.get("link"), e.get("title", ""), e.get("summary", "")) for e in parsed.entries[: feed.max_items]]
        )
        entries = [e for e in entries if e[0] != feed_url]  # položka s relatívnym odkazom ukazuje na samotný feed
        keep = triage(cfg, llm, [(t, s) for _, t, s in entries], stats, db) if feed.triage else set(range(len(entries)))
        groups.append([(u, "rss", feed.name, None) for i, (u, _, _) in enumerate(entries) if i in keep])

    for topic in cfg.all_topics():
        for query in topic.queries:
            try:
                groups.append([(r.url, "search", topic.name, query) for r in searx.search(query)])
            except Exception as exc:
                log.warning("Vyhľadávanie '%s' zlyhalo: %s", query, exc)
                stats["search_errors"] += 1

    seen: set[str] = set()
    merged: list[Candidate] = []
    for row in zip_longest(*groups):
        for cand in row:
            if cand is None:
                continue
            url = cand[0]
            if url and url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                merged.append(cand)
    return merged


def run_collect(
    cfg: Config, db: DB, searx: SearxClient, fetcher: Fetcher, deadline: Deadline, llm: Any = None,
    budget: int | None = None,
) -> dict:
    """Zber k témam a projektom. Bez zadaného rozpočtu berie celý max_new_docs_per_run;
    run-all mu dá iba časť, ktorá ostane po objavovaní (R-DISC-5)."""
    stats: Counter = Counter()
    limit = cfg.pipeline.max_new_docs_per_run if budget is None else budget
    candidates = gather_candidates(cfg, db, searx, fetcher, llm, stats)
    stats["candidates"] = len(candidates)
    for url, kind, topic, query in candidates:
        if stats["new_documents"] >= limit or deadline.expired():
            break
        if db.url_recently_checked(url, cfg.fetch.refetch_days):
            stats["skipped_recent"] += 1
            continue
        page = fetcher.fetch(url)
        if page is None:
            stats["fetch_failed"] += 1
            continue
        _, is_new = db.upsert_document(page.url, page.title, kind, topic, query, page.content_hash, page.text)
        stats["new_documents" if is_new else "unchanged"] += 1
    return dict(stats)
