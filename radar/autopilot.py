"""Autopilot („Nechať bežať všetko“): kolá akcií v poradí podľa závislostí, kým nevyprší zvolený čas.

Úloha `autopilot` iba naplánuje kolo: zistí, kde je práca, a zaradí do poradia Správcu akcií samostatné úlohy
(každá so svojím časovým úsekom, v GUI viditeľná zvlášť); poslednou úlohou kola je ďalší `autopilot`, ktorý naplánuje
ďalšie kolo. Akcie bez práce sa preskočia. Zastavenie: „Zastaviť všetko vrátane poradia“.

Osobné údaje: zber ľudí, kontaktov a návrhov dostane presne hypotézy, ktoré si zapol na Bránu 0 (automatický výber
top N sa nespustí); Brána 1 iba pre ne, postupne od najdlhšie nespracovaných. Tvoje vstupy k dátam (Brána 1 → Tvoje
dáta) posúdi samostatná krátka úloha pred prieskumom dát; čo prejde Bránou 1, ide v ďalšom kole do Brány 2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB
from .strength import evidence_strength
from .util import utcnow

log = logging.getLogger(__name__)

# (akcia, minúty) v poradí závislostí: nové hypotézy → test → posúdenie → témy → ľudia → kontakty → návrhy → brány → kritika
ROUND: list[tuple[str, int]] = [
    ("scout", 60), ("topics", 30), ("validate", 60), ("evidence", 45), ("themes", 10), ("cards", 30), ("people", 60),
    ("contacts", 20), ("drafts", 30), ("gate1-input", 10), ("gate1", 60), ("gate2", 30), ("critique", 30),
]
MIN_SLICE = 10  # kratší úsek nemá zmysel (načítanie modelu, prvé kroky)
GATE1_PER_JOB = 3


def _since_last(db: DB, kinds: tuple[str, ...]) -> str:
    """Čas konca poslednej dokončenej úlohy daného druhu (alebo prázdny reťazec)."""
    marks = ",".join("?" * len(kinds))
    row = db._one(f"SELECT MAX(finished_at) AS t FROM jobs WHERE kind IN ({marks}) AND status = 'done'", kinds)
    return (row["t"] if row and row["t"] else "") or ""


def selected_ids(db: DB) -> list[int]:
    return [h["id"] for h in db.people_enabled_hypotheses()]


def gate1_ids(db: DB, k: int = GATE1_PER_JOB, retry_days: float = 3.0, now: datetime | None = None) -> list[int]:
    """Vybrané hypotézy s nedokončenou Bránou 1: najprv nikdy nespracované, potom najdlhšie nespracované; tie, ktoré
    čakajú na tvoje dáta, sa znova automaticky prehľadajú najskôr po `retry_days` (zdroje a katalóg pribúdajú)."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retry_days)).isoformat(timespec="seconds")
    rows = []
    for h in db.people_enabled_hypotheses():
        status = h["gate1_status"] or "not_started"
        when = (json.loads(h["gate1_json"] or "{}") or {}).get("vypočítané") or ""
        if status in ("not_started", "running"):
            rows.append((0, when, h["id"]))
        elif status == "needs_input" and when < cutoff:
            rows.append((1, when, h["id"]))
    return [hid for *_, hid in sorted(rows)[:k]]


def pending_work(cfg: Config, db: DB) -> dict[str, str]:
    """Akcia → dôvod, prečo má prácu (akcie bez práce chýbajú)."""
    work: dict[str, str] = {"scout": "nové zdroje a matica dopytov", "topics": "RSS a dopyty k témam"}
    v = db.validation_counts()
    if v.get("none") or v.get("searched"):
        work["validate"] = f"neotestované {v.get('none', 0)}, čaká na verdikt {v.get('searched', 0)}"
    pending, unassigned = db.pending_entailment_count(), db.unassigned_count()
    if pending or unassigned:
        work["evidence"] = f"na posúdenie {pending}, na zaradenie {unassigned}"
    last_themes = db._one("SELECT MAX(updated_at) AS t FROM themes")["t"] or ""
    new_h = db._one("SELECT COUNT(*) AS n FROM hypotheses WHERE status != 'rejected' AND created_at > ?", (last_themes,))["n"]
    if new_h:
        work["themes"] = f"{new_h} hypotéz od posledného zoskupenia"
    ids = selected_ids(db)
    if ids:
        marks = ",".join("?" * len(ids))
        orgs = db._one(f"SELECT COUNT(*) AS n FROM org_hypotheses WHERE hypothesis_id IN ({marks}) "
                       "AND status IN ('candidate', 'website_checked')", tuple(ids))["n"]
        unranked = sum(len(db.unranked_people(h)) for h in ids)
        no_map = sum(1 for h in ids if db.get_role_map(h) is None)
        if orgs or unranked or no_map:
            work["people"] = f"organizácie na spracovanie {orgs}, nezoradení ľudia {unranked}, bez mapy rolí {no_map}"
        since = _since_last(db, ("contacts", "people"))
        fresh = db._one(f"SELECT COUNT(*) AS n FROM people WHERE collected_for_hypothesis_id IN ({marks}) "
                        "AND created_at > ?", (*ids, since))["n"]
        if fresh:
            work["contacts"] = f"{fresh} ľudí od posledného hľadania kontaktov"
        drafts = db._one(
            f"""SELECT COUNT(DISTINCT l.person_id || '-' || l.hypothesis_id) AS n FROM person_problem_links l
                JOIN people p ON p.id = l.person_id
                WHERE l.hypothesis_id IN ({marks}) AND l.priority IS NOT NULL AND p.is_expert = 0
                  AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.person_id = l.person_id AND o.hypothesis_id = l.hypothesis_id)
                  AND EXISTS (SELECT 1 FROM contact_points c WHERE c.person_id = l.person_id
                              OR (c.org_id = p.org_id AND c.is_generic = 1))""", tuple(ids))["n"]
        if drafts:
            work["drafts"] = f"{drafts} zoradených ľudí s kontaktom bez návrhu"
        g1 = gate1_ids(db, retry_days=cfg.gate1.research_retry_days)
        if g1:
            work["gate1"] = "hypotézy " + ", ".join(f"H{i}" for i in g1)
    inputs = sorted({r["hypothesis_id"] for r in db.pending_gate1_inputs()})
    if inputs:
        work["gate1-input"] = "tvoje dáta na posúdenie: " + ", ".join(f"H{i}" for i in inputs)
    from .cards import card_targets
    from .exp_plan import plan_targets

    cards = card_targets(cfg, db, None)
    if cards:
        work["cards"] = "bez karty: " + ", ".join(f"H{h['id']}" for h in cards[:8]) + (" …" if len(cards) > 8 else "")
    passed = [h["id"] for h in db.gate2_hypotheses()]
    todo = {h["id"] for h, _ in plan_targets(cfg, db, None)}
    for hid in passed:
        exps = db.experiments_for(hid)
        if any(e["status"] == "locked" or (e["status"] == "done" and not (e["demo_path"] or e["conclusion_json"]))
               for e in exps) or hid in todo:
            work["gate2"] = "Brána 1 prešla: " + ", ".join(f"H{i}" for i in passed)
            break
    for h in db.active_hypotheses():
        evidence = db.evidence_for(h["id"])
        if len(evidence) < cfg.pipeline.critique_min_claims or evidence_strength(evidence)[0] == "slabá":
            continue
        last = db.latest_critique(h["id"])
        if not last or last["claim_count"] < len(evidence):
            work["critique"] = "hypotézy s novými dôkazmi bez kritiky"
            break
    return work


def plan_round(cfg: Config, db: DB, config_path: str | Path, until: datetime, round_no: int,
               now: datetime | None = None) -> dict[str, Any]:
    """Zaradí úlohy kola (s prácou, kým sa zmestia do zvyšného času) a na koniec ďalší autopilot."""
    now = now or datetime.now(timezone.utc)
    left = (until - now).total_seconds() / 60
    work = pending_work(cfg, db)
    planned, skipped, budget = [], {}, left
    config = str(config_path)
    for kind, minutes in ROUND:
        if kind not in work:
            skipped[kind] = "bez práce"
            continue
        slice_min = min(minutes, budget)
        if slice_min < MIN_SLICE:
            skipped[kind] = "nezmestí sa do času"
            continue
        ids = (gate1_ids(db, retry_days=cfg.gate1.research_retry_days) if kind == "gate1"
               else selected_ids(db) if kind in ("people", "contacts", "drafts") else [])
        job = db.create_job(kind, {"minutes": round(slice_min), "hypotheses": ids, "config": config,
                                   "autopilot": round_no}, status="queued")
        planned.append({"úloha": job, "akcia": kind, "minúty": round(slice_min), "dôvod": work[kind]})
        budget -= slice_min
    next_job = None
    if planned:  # ďalší autopilot prepočíta zvyšný čas (úlohy často skončia skôr); keď sa nič nezmestí, reťaz skončí
        next_job = db.create_job("autopilot", {"minutes": 5, "hypotheses": [], "config": config,
                                               "until": until.isoformat(timespec="seconds"), "round": round_no + 1},
                                 status="queued")
    log.info("Autopilot kolo %d: %d úloh, ďalšie kolo %s (do %s)", round_no, len(planned),
             f"#{next_job}" if next_job else "nie", until.isoformat(timespec="minutes"))
    return {"kolo": round_no, "do": until.isoformat(timespec="minutes"), "naplánované": planned,
            "preskočené": skipped, "ďalšie_kolo": next_job, "naplánované_o": utcnow()}


def until_for(job, minutes: float) -> datetime:
    """Koniec autopilota: z argumentov úlohy (ďalšie kolá), inak začiatok prvej úlohy + zvolený čas."""
    args = json.loads(job["args_json"] or "{}") if job is not None else {}
    if args.get("until"):
        return datetime.fromisoformat(args["until"])
    start = datetime.fromisoformat(job["started_at"]) if job is not None and job["started_at"] else datetime.now(timezone.utc)
    return start + timedelta(minutes=minutes)
