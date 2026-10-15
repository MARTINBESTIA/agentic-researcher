"""Agentický prieskum nových problémov (scout): agent prechádza web pre bunku matice
„vzor bolesti × odvetvie“ a ukladá doslovné citáty.

Agent má iba nástroje na čítanie a uloženie dôkazu. Jeho tvrdenia (origin = scout) prejdú
rovnakým overením ako ostatné (kód → presný model) a v kroku cluster sa správajú ako
objavovanie: odlišné od známych hypotéz → nová hypotéza „candidate“, ktorá sa povýši až
s 2 overenými tvrdeniami z 2 domén (R-DISC-7).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from typing import Any

from openai import APIConnectionError
from rapidfuzz import fuzz, process

from .agent import ResearchAgent
from .search import SearchUnavailable
from .config import Config
from .db import DB
from .discover import matrix_cells
from .prompts import SCOUT_SYSTEM
from .util import Deadline

log = logging.getLogger(__name__)

SCOUT_ARM = "scout"  # kurzor rotácie buniek matice (oddelený od kroku discover)


def next_scout_cells(cfg: Config, db: DB, n: int) -> list[tuple[str, str]]:
    """Ďalších n buniek (vzor bolesti, odvetvie); rotácia pokračuje tam, kde skončil minulý beh."""
    cells = matrix_cells(cfg)
    if not cells or n <= 0:
        return []
    cursor = int(db.get_cursor(SCOUT_ARM + ":cursor") or 0)
    out = [cells[(cursor + i) % len(cells)] for i in range(n)]
    db.record_arm(SCOUT_ARM + ":cursor", cursor=str((cursor + n) % len(cells)))
    return out


def known_titles(db: DB, sector: str, pain: str, k: int) -> list[str]:
    """Najpodobnejšie známe hypotézy (text), aby agent nehľadal to, čo už máme."""
    hyps = [h for h in db.all_hypotheses() if h["status"] != "rejected"]
    if not hyps or k <= 0:
        return []
    choices = {h["id"]: f"{h['title']} {h['segment'] or ''} {h['sector'] or ''}" for h in hyps}
    ranked = process.extract(f"{sector} {pain}", choices, scorer=fuzz.token_set_ratio, limit=k)
    by_id = {h["id"]: h for h in hyps}
    return [by_id[key]["title"] for _, _, key in ranked]


def run_scout(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline, sessions: int | None = None) -> dict:
    w = cfg.workflows
    stats: Counter = Counter()
    n = w.scout_sessions_per_run if sessions is None else sessions
    for pain, sector in next_scout_cells(cfg, db, n):
        if deadline.expired():
            break
        known = known_titles(db, sector, pain, w.known_titles_in_prompt)
        system = SCOUT_SYSTEM.format(
            today=date.today().isoformat(), sector=sector, pain=pain, max_steps=w.scout_max_steps,
            lang=cfg.pipeline.output_language, known="\n".join(f"- {t}" for t in known) or "(none yet)",
            portals="\n".join(f"   - {c.name}: {c.url} ({c.topics})" for c in cfg.gate1.catalog),
        )
        # Stránky, ktoré agent prečíta, sú dokumenty objavovania: aj keď agent citát neuloží (rýchly model
        # pri volaní nástrojov často len číta), spracuje ich bežný screen → extrakcia → overenie.
        agent = ResearchAgent(cfg, db, llm, searx, fetcher, topic=f"Prieskum: {sector} – {pain}", task_name="scout",
                              claim_origin="scout", sector_hint=sector, doc_kind="discovery", doc_category=SCOUT_ARM)
        task = (f"Find new concrete problems of organizations in Slovakia. Sector: {sector}. Pattern: {pain}. "
                "Search in Slovak first, then Czech or English.")
        try:
            summary = agent.run(task, w.scout_max_steps, system=system, deadline=deadline)
        except (APIConnectionError, SearchUnavailable):
            raise  # model alebo vyhľadávač nebeží: ďalšie sedenia by zlyhali tiež
        except Exception as exc:  # noqa: BLE001 – zlyhanie modelu nezastaví beh (invariant 5)
            log.warning("Prieskum %s × %s zlyhal: %s", sector, pain, exc)
            stats["errors"] += 1
            continue
        db.add_note(None, f"Prieskum (agent) {sector} × {pain}: {summary}", source="agent")
        stats["sessions"] += 1
        stats["claims"] += len(agent.claim_ids)
        stats["pages_read"] += len(agent.pages)
        log.info("Prieskum %s × %s: %d tvrdení na overenie", sector, pain, len(agent.claim_ids))
    return dict(stats)
