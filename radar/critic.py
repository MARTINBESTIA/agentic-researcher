"""Krok 5 – kritik: zhrnutie, protiargumenty, chýbajúce dôkazy, otázky na rozhovory a hľadanie konkurencie.

Model vidí iba zozbierané dôkazy. Odkazy na možnú konkurenciu sú len výsledky
vyhľadávania – posúdenie, či ide naozaj o konkurenciu, zostáva na tebe.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .models import as_router
from .prompts import CRITIC_SYSTEM
from .search import SearxClient
from .util import Deadline

log = logging.getLogger(__name__)


class Critique(BaseModel):
    summary: str
    counterarguments: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    competitor_queries: list[str] = Field(default_factory=list)
    interview_questions: list[str] = Field(default_factory=list)


KIND = {"counter": "AGAINST the problem", "existing_solution": "existing solution"}


def evidence_block(rows: list, max_items: int = 25) -> str:
    lines = []
    for r in rows[:max_items]:
        lines.append(
            f"[C{r['id']}] ({r['status']}, {KIND.get(r['claim_type'], 'supports the problem')}) {r['problem']} | "
            f"who: {r['affected_party']} | "
            f"quote: \"{r['quote'][:300]}\" | source: {r['domain']}, {r['fetched_at'][:10]}"
        )
    return "\n".join(lines)


def run_critique(cfg: Config, db: DB, llm: Any, searx: SearxClient, deadline: Deadline) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    excluded = db.automation_excluded_ids()  # M39/M40: automatika ich vynecháva
    for hyp in db.active_hypotheses():
        if deadline.expired():
            break
        if hyp["id"] in excluded:
            continue
        evidence = db.evidence_for(hyp["id"])
        if len(evidence) < cfg.pipeline.critique_min_claims:
            continue
        last = db.latest_critique(hyp["id"])
        if last and last["claim_count"] >= len(evidence):
            continue  # od poslednej kritiky nepribudli dôkazy
        res = router.run_task(
                "critique",
                [
                    {"role": "system", "content": CRITIC_SYSTEM.format(lang=cfg.pipeline.output_language, today=date.today().isoformat())},
                    {
                        "role": "user",
                        "content": f"HYPOTHESIS: {hyp['title']}\nSEGMENT: {hyp['segment']}\n"
                        f"DESCRIPTION: {hyp['description']}\n\nEVIDENCE:\n{evidence_block(evidence)}",
                    },
                ],
                Critique,
                object_type="hypothesis",
                object_id=hyp["id"],
            )
        if not res.ok:
            log.warning("Kritika hypotézy %s zlyhala: %s", hyp["id"], res.reason)
            stats["llm_failures"] += 1
            continue
        crit = res.value
        links = []
        for query in crit.competitor_queries[:3]:
            try:
                links += [{"query": query, "title": r.title, "url": r.url} for r in searx.search(query, limit=5)]
            except Exception as exc:
                log.warning("Hľadanie konkurencie '%s' zlyhalo: %s", query, exc)
        content = crit.model_dump()
        content["competitor_links"] = links
        db.add_critique(hyp["id"], len(evidence), content)
        stats["critiques"] += 1
    return dict(stats)
