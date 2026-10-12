"""Objavovanie nových problémov mimo tém z config.yaml (R-DISC).

- Katalóg zdrojov sources.yaml (NKÚ, revízie výdavkov, legislatíva, obstarávanie, zväzy, výročné
  správy…); použije sa iba zdroj s verified: true (potvrdí ho používateľ po sources --check).
- Matica dopytov „vzor bolesti × odvetvie“ (deterministická, rotuje; pozícia je v query_stats).
- Podiel exploration_share (0,3) z rozpočtu dokumentov behu ide na objavovanie a delí sa medzi
  kategórie Thompson samplingom s Beta(1 + úspechy, 1 + neúspechy); úspech = dokument dal aspoň
  1 overené tvrdenie. Každá kategória má minimálny podiel min_arm_share.
- Dokumenty z objavovania najprv posúdi screen (extract.run_screen) a extrakcia beží bez témy.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
import yaml
from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .fetch import domain_blocked
from .util import Deadline, canonical_url, domain_of

log = logging.getLogger(__name__)

MATRIX_ARM = "query_matrix"


class Source(BaseModel):
    category: str
    name: str
    url: str
    method: str = "search"  # search | rss | sitemap | seed | api
    queries: list[str] = Field(default_factory=list)
    cadence_days: int = 14
    language: str = "sk"
    verified: bool = False


def load_sources(cfg: Config) -> list[Source]:
    path = cfg.path(cfg.discovery.sources_file)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Source.model_validate(item) for item in data]


# ------------------------------------------------------------------ matica dopytov
def matrix_cells(cfg: Config) -> list[tuple[str, str]]:
    """Všetky bunky „vzor bolesti × odvetvie“ v pevnom pomiešanom poradí (seed 0), aby susedné
    behy nepokrývali ten istý vzor."""
    cells = [(p, s) for p in cfg.discovery.pain_patterns for s in cfg.discovery.sectors]
    random.Random(0).shuffle(cells)
    return cells


def next_matrix_queries(cfg: Config, db: DB, n: int) -> list[str]:
    """Ďalších n buniek matice; rotácia pokračuje tam, kde skončil minulý beh (R-DISC-2)."""
    cells = matrix_cells(cfg)
    if not cells or n <= 0:
        return []
    cursor = int(db.get_cursor(MATRIX_ARM + ":cursor") or 0)
    out = [f"{cells[(cursor + i) % len(cells)][0]} {cells[(cursor + i) % len(cells)][1]}" for i in range(n)]
    db.record_arm(MATRIX_ARM + ":cursor", cursor=str((cursor + n) % len(cells)))
    return out


# ------------------------------------------------------------------ bandit
def update_arm_stats(db: DB) -> int:
    """Zapíše výsledky spracovaných dokumentov z objavovania (úspech = aspoň 1 overené tvrdenie)."""
    counted = 0
    for row in db.discovery_outcomes():
        if row["pending"]:
            continue  # ešte čaká na overenie
        arm = row["source_category"] or "other"
        db.record_arm(arm, tries=1, successes=1 if row["verified"] else 0)
        db.mark_bandit_counted(row["id"])
        counted += 1
    return counted


def allocate(arms: list[str], total: int, stats: dict[str, Any], min_share: float, rng: random.Random) -> dict[str, int]:
    """Rozdelí `total` dokumentov medzi ramená: každé dostane aspoň min_share (zaokrúhlené nahor,
    ak sa zmestí), zvyšok Thompson samplingom z Beta(1 + úspechy, 1 + neúspechy)."""
    arms = sorted(set(arms))
    alloc = {a: 0 for a in arms}
    if not arms or total <= 0:
        return alloc
    floor_n = math.ceil(min_share * total)
    if floor_n * len(arms) > total:  # priveľa ramien na malý rozpočet: iba Thompson sampling
        floor_n = 0
    for a in arms:
        alloc[a] = floor_n
    remaining = total - floor_n * len(arms)
    for _ in range(remaining):
        draws = {}
        for a in arms:
            row = stats.get(a)
            s = row["successes"] if row is not None else 0
            f = (row["tries"] - row["successes"]) if row is not None else 0
            draws[a] = rng.betavariate(1 + s, 1 + f)
        alloc[max(draws, key=draws.get)] += 1
    return alloc


# ------------------------------------------------------------------ kandidáti
@dataclass
class DiscoveryCandidate:
    url: str
    category: str
    query: str | None
    title: str = ""


@dataclass
class DiscoveryPlan:
    allocation: dict[str, int]
    candidates: dict[str, list[DiscoveryCandidate]] = field(default_factory=dict)


def _ok_url(cfg: Config, url: str) -> bool:
    if not url.startswith(("http://", "https://")) or domain_blocked(url, cfg.blocked_domains()):
        return False
    host = domain_of(url)
    return not any(host == a or host.endswith("." + a) for a in ("facebook.com", "youtube.com", "linkedin.com"))


def source_candidates(cfg: Config, db: DB, searx, source: Source, limit: int) -> list[DiscoveryCandidate]:
    out: list[DiscoveryCandidate] = []
    host = domain_of(source.url)
    if source.method == "search":
        queries = source.queries or [f"site:{host} {p}" for p in cfg.discovery.pain_patterns[:2]]
        for q in queries:
            try:
                for r in searx.search(q, limit=limit + 4):
                    if domain_of(r.url).endswith(host) and _ok_url(cfg, r.url):
                        out.append(DiscoveryCandidate(canonical_url(r.url), source.category, q, r.title))
            except Exception as exc:  # noqa: BLE001
                log.warning("Objavovanie '%s' zlyhalo: %s", q, exc)
    elif source.method == "rss":
        try:
            parsed = feedparser.parse(source.url, agent=cfg.fetch.user_agent)
            for e in parsed.entries[: limit * 2]:
                if e.get("link") and _ok_url(cfg, e["link"]):
                    out.append(DiscoveryCandidate(canonical_url(e["link"]), source.category, None, e.get("title", "")))
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS %s zlyhalo: %s", source.url, exc)
    elif source.method == "seed":
        out.append(DiscoveryCandidate(canonical_url(source.url), source.category, None))
    elif source.method == "api" and source.category == "annual_report":
        out += annual_report_candidates(cfg, db, limit)
    return out


def annual_report_candidates(cfg: Config, db: DB, limit: int) -> list[DiscoveryCandidate]:
    """Výročné správy (PDF z RÚZ) organizácií, ktoré už sú v DB – časť o rizikách je dôkaz, že
    konkrétna organizácia problém má (R-ORG-3)."""
    from .registers import RUZClient

    ruz = RUZClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    out: list[DiscoveryCandidate] = []
    for org in db.all_organizations():
        if len(out) >= limit or not org["ico"]:
            continue
        info = ruz.lookup_ico(org["ico"])
        for report_id in (info or {}).get("annual_report_ids", [])[-1:]:
            report = ruz.annual_report(int(report_id))
            for att in (report or {}).get("prilohy", [])[:1]:
                if (att.get("mimeType") or "").endswith("pdf"):
                    out.append(DiscoveryCandidate(ruz.attachment_url(int(att["id"])), "annual_report", None,
                                                  f"Výročná správa {org['name']}"))
    return out


def matrix_candidates(cfg: Config, db: DB, searx, limit: int) -> list[DiscoveryCandidate]:
    out = []
    for q in next_matrix_queries(cfg, db, cfg.discovery.queries_per_run):
        try:
            for r in searx.search(q, limit=6):
                host = domain_of(r.url)
                if _ok_url(cfg, r.url) and not any(host == a or host.endswith("." + a) for a in cfg.people.aggregator_domains[:8]):
                    out.append(DiscoveryCandidate(canonical_url(r.url), MATRIX_ARM, q, r.title))
        except Exception as exc:  # noqa: BLE001
            log.warning("Dopyt matice '%s' zlyhal: %s", q, exc)
        if len(out) >= limit * 3:
            break
    return out


def discovery_budget(cfg: Config, total_docs: int) -> int:
    """Počet dokumentov z objavovania pri celkovom rozpočte behu (R-DISC-5)."""
    if not cfg.discovery.enabled:
        return 0
    return math.ceil(cfg.discovery.exploration_share * total_docs)


def run_discover(cfg: Config, db: DB, searx, fetcher, deadline: Deadline, budget: int | None = None,
                 rng: random.Random | None = None) -> dict:
    stats: Counter = Counter()
    stats["arm_updates"] = update_arm_stats(db)
    budget = discovery_budget(cfg, cfg.pipeline.max_new_docs_per_run) if budget is None else budget
    if budget <= 0:
        return dict(stats)
    sources = [s for s in load_sources(cfg) if s.verified]
    stats["unverified_sources"] = len([s for s in load_sources(cfg) if not s.verified])
    arms = sorted({s.category for s in sources} | {MATRIX_ARM})
    rng = rng or random.Random(cfg.discovery.seed)
    allocation = allocate(arms, budget, db.arm_stats(), cfg.discovery.min_arm_share, rng)
    stats.update({f"plan:{a}": n for a, n in allocation.items() if n})
    seen: set[str] = set()
    for arm, n in allocation.items():
        if n <= 0 or deadline.expired():
            continue
        if arm == MATRIX_ARM:
            cands = matrix_candidates(cfg, db, searx, n)
        else:
            cands = [c for s in sources if s.category == arm for c in source_candidates(cfg, db, searx, s, n)]
        fetched = 0
        for cand in cands:
            if fetched >= n or deadline.expired():
                break
            if cand.url in seen or db.url_recently_checked(cand.url, cfg.fetch.refetch_days):
                continue
            seen.add(cand.url)
            page = fetcher.fetch(cand.url)
            if page is None:
                stats["fetch_failed"] += 1
                continue
            _, is_new = db.upsert_document(page.url, page.title or cand.title, "discovery", None, cand.query,
                                           page.content_hash, page.text, source_category=arm)
            if is_new:
                fetched += 1
                stats["new_documents"] += 1
                stats[f"docs:{arm}"] += 1
    return dict(stats)


def check_sources(cfg: Config, fetcher=None, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """sources --check: dostupnosť a robots.txt každého zdroja (nič nemení, potvrdzuje človek)."""
    http = http or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": cfg.fetch.user_agent})
    rows = []
    for s in load_sources(cfg):
        row: dict[str, Any] = {"name": s.name, "category": s.category, "url": s.url, "verified": s.verified}
        try:
            resp = http.get(s.url)
            row["http"] = resp.status_code
        except httpx.HTTPError as exc:
            row["http"] = f"chyba: {type(exc).__name__}"
        parsed = urlparse(s.url)
        try:
            robots = http.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            if robots.status_code == 200:
                rp = RobotFileParser()
                rp.parse(robots.text.splitlines())
                row["robots"] = "povolené" if rp.can_fetch(cfg.fetch.user_agent, s.url) else "zakázané"
            else:
                row["robots"] = f"bez robots.txt ({robots.status_code})"
        except httpx.HTTPError:
            row["robots"] = "nedostupné"
        rows.append(row)
    return rows
