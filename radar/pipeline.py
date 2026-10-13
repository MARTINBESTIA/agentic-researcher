"""Celý beh (run-all) v poradí podľa modelov, aby sa veľký model menil čo najmenej (spec kap. 4):

1. rýchly model a práca bez modelu: objavovanie (aj agent scout), zber, screen, extrakcia, kontrola
   citátov kódom, témy hypotéz (z minulého behu), organizácie a ľudia (zber kontaktov), agent testuje
   ďalšie hypotézy z fronty,
2. embeddingy (malý model na CPU),
3. presný model v poradí priority (R-ACC-6): väzby a priorita ľudí, návrhy oslovení, posúdenie
   dôkazov, zaradenie do hypotéz, verdikt testu hypotéz (kód), príprava na ďalší beh, kritika a odhad trhu,
4. správa a retencia (purge).

Každá fáza má časový rozpočet; nespracované položky ostanú v DB na ďalší beh (beh sa dá prerušiť).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .bench import capacity_plan
from .discover import discovery_budget
from .privacy import PrivacyGateError
from .util import Deadline

log = logging.getLogger("radar")

FAST_STAGES = ["discover", "scout", "collect", "screen", "extract", "verify-code", "source-types", "themes", "orgs",
               "people", "gate1-sources",
               "gate1-check", "gate1-datasets", "gate2-backtest", "validate-search"]
ACCURATE_STAGES = ["people-rank", "contacts", "drafts", "verify", "cluster", "validate-finish", "gate1-fields",
                   "gate1-repro", "gate1-finish", "gate2-prereg", "gate2-demo", "prepare-next", "critique", "market"]
FINAL_STAGES = ["report", "purge"]
PEOPLE_STAGES = {"orgs", "people", "people-rank", "drafts"}
# Lacné kroky iba s kódom: bežia vždy s vlastným krátkym limitom, aj keď presnej fáze vypršal čas
# (inak by napr. hypotézy prehľadané agentom nedostali verdikt).
ALWAYS_STAGES = {"validate-finish", "gate1-finish", "verify-code", "source-types"}


def workload(app) -> dict[str, int]:
    """Odhad počtu položiek pre presný model (na plán behu)."""
    db, cfg = app.db, app.cfg
    people_hyps = db.people_enabled_hypotheses()
    pending_entail = db.pending_entailment_count()
    return {
        "role_map": sum(1 for h in people_hyps if db.get_role_map(h["id"]) is None),
        "draft": cfg.outreach.max_drafts_per_run,
        "draft_check": cfg.outreach.max_drafts_per_run,
        "entail": min(pending_entail, cfg.pipeline.max_entail_per_run or pending_entail),
        "cluster_assign": min(db.unassigned_count(), cfg.pipeline.max_cluster_per_run or 10_000),
    }


def log_plan(app, minutes: float) -> None:
    try:
        plan = capacity_plan(app.db, app.router, workload(app), minutes)
    except Exception as exc:  # noqa: BLE001 – plán je iba informácia
        log.info("Plán behu sa nedá odhadnúť: %s", exc)
        return
    parts = []
    for p in plan:
        if p["items"]:
            est = "?" if p["total_min"] is None else f"{p['total_min']:.0f} min"
            parts.append(f"{p['task']} {p['items']}× ≈ {est}")
    if parts:
        log.info("Plán pre presný model (odhad z benchmarku a minulých behov): %s", "; ".join(parts))


def run_all(app, minutes: float, run_stage: Callable[[Any, str, Deadline], dict], fast_share: float = 0.45) -> bool:
    """Vráti True, ak niektorá fáza zlyhala (ostatné aj tak bežia a správa sa vytvorí vždy)."""
    cfg = app.cfg
    deadline = Deadline(minutes)
    failed = False
    log_plan(app, minutes)
    app.collect_budget = max(0, cfg.pipeline.max_new_docs_per_run - discovery_budget(cfg, cfg.pipeline.max_new_docs_per_run))

    def run(stage: str, dl: Deadline) -> None:
        nonlocal failed
        if stage in PEOPLE_STAGES and not cfg.privacy.lia_completed:
            log.info("Krok %s preskočený: hľadanie ľudí čaká na LIA (privacy.lia_completed).", stage)
            return
        if stage in ALWAYS_STAGES:
            dl = Deadline(2)
        try:
            run_stage(app, stage, dl)
        except PrivacyGateError as exc:
            log.warning("Krok %s preskočený: %s", stage, exc)
        except Exception:  # jeden zlyhaný krok nezastaví ostatné
            log.exception("Krok %s zlyhal", stage)
            failed = True

    fast = deadline.sub(minutes * fast_share)
    for stage in FAST_STAGES:
        run(stage, fast)
    run("embed", deadline.sub(min(10, deadline.remaining_minutes())))
    accurate = deadline.sub(max(0.0, deadline.remaining_minutes() - 3))
    for stage in ACCURATE_STAGES:
        run(stage, accurate)
    for stage in FINAL_STAGES:
        run(stage, Deadline(10))  # správa a retencia vždy
    return failed
