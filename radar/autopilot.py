"""Autopilot („Nechať bežať všetko“): kolá akcií v poradí podľa závislostí, kým nevyprší zvolený čas.

Úloha `autopilot` iba naplánuje kolo: zistí, kde je práca, a zaradí do poradia Správcu akcií samostatné úlohy
(každá so svojím časovým úsekom, v GUI viditeľná zvlášť); poslednou úlohou kola je ďalší `autopilot`, ktorý naplánuje
ďalšie kolo. Akcie bez práce sa preskočia. Zastavenie: „Zastaviť všetko vrátane poradia“.

Osobné údaje: zber ľudí, kontaktov a návrhov dostane presne hypotézy zapnuté na Bránu 0 – ručne alebo automaticky
po teste „podložená“ (people.auto_select_validated; automatický výber top N podľa sily sa nespustí); Brána 1 iba pre ne, postupne od najdlhšie nespracovaných. Tvoje vstupy k dátam (Brána 1 → Tvoje
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
from .util import utc_days_ago, utcnow

log = logging.getLogger(__name__)

# (akcia, minúty). Audit 14. 10. 2026 (P1 6): pri krátkych behoch dostal čas iba nový zber (scout, topics boli prví
# a vždy majú prácu). Teraz najprv tvoje vstupy a vetvy vybraných hypotéz (Brána 1 → karty → Brána 2 → ľudia),
# potom spracovanie existujúcich hypotéz; nový zber (aj index katalógu) má vyhradený podiel kola
# (workflows.autopilot_discovery_share).
PRIORITY: list[tuple[str, int]] = [
    ("gate1-input", 10), ("gate1", 60), ("cards", 30), ("gate2", 60), ("people", 60), ("contacts", 20), ("drafts", 30),
    ("evidence", 45), ("validate", 60), ("themes", 10), ("critique", 30),
]
DISCOVERY: list[tuple[str, int]] = [("scout", 60), ("topics", 30), ("catalog", 30)]
# Sústredený režim prípravy oslovení (zadanie 17. 10. 2026, časť E): karty zvolených organizácií a návrhy pred
# všetkým ostatným; nový všeobecný zber iba zo zvyšného času (bez vyhradeného podielu). Mimo režimu sa karty
# v autopilote nevytvárajú (iba akciou na žiadosť) – žiadna hromadná regenerácia.
FOCUS: list[tuple[str, int]] = [("org_cards", 30), ("drafts", 30)]
ROUND: list[tuple[str, int]] = PRIORITY + DISCOVERY + FOCUS[:1]
AGING_KEY = "autopilot_aging"  # akcia → počet kôl, v ktorých mala prácu, ale nezmestila sa
MIN_SLICE = 10  # kratší úsek nemá zmysel (načítanie modelu, prvé kroky)
GATE1_PER_JOB = 3


def _since_last(db: DB, kinds: tuple[str, ...]) -> str:
    """Čas konca poslednej dokončenej úlohy daného druhu (alebo prázdny reťazec)."""
    marks = ",".join("?" * len(kinds))
    row = db._one(f"SELECT MAX(finished_at) AS t FROM jobs WHERE kind IN ({marks}) AND status = 'done'", kinds)
    return (row["t"] if row and row["t"] else "") or ""


def selected_ids(db: DB) -> list[int]:
    return [h["id"] for h in db.people_enabled_hypotheses()]


STAGE_RANK = {"passed": 0, "needs_revalidation": 1, "running": 1, "needs_input": 2, "ready": 1, "not_started": 3}


def priority_ids(cfg: Config, db: DB, k: int | None = None) -> list[int]:
    """Prioritné hypotézy (najviac `autopilot_max_priorities`): vybrané na Bránu 0, najďalej v lieviku prvé
    (Brána 1 prešla → rozpracovaná → čaká na dáta → nezačatá), potom najdlhšie vybrané. Ostatné počkajú, kým
    sa niektorá vetva dokončí (audit: systém má dokončovať vybrané vetvy, nie rozbiehať nové)."""
    k = k if k is not None else cfg.workflows.autopilot_max_priorities
    rows = [(STAGE_RANK.get(h["gate1_status"] or "not_started", 3), h["id"]) for h in db.people_enabled_hypotheses()]
    return [hid for _, hid in sorted(rows)[:k]]


def gate1_ids(db: DB, k: int = GATE1_PER_JOB) -> list[int]:
    """Vybrané hypotézy s nedokončenou Bránou 1 (nezačaté a rozpracované): najprv rozpracované, potom najdlhšie
    nespracované. Vyhodnotené („čaká na tvoje dáta“) sa automaticky znova neprehľadávajú (pokyn 14. 10. 2026);
    pri nových dátach sa iba namapujú polia na stĺpce."""
    from .gate1 import binding_work

    rows = []
    for h in db.people_enabled_hypotheses():
        status = h["gate1_status"] or "not_started"
        when = (json.loads(h["gate1_json"] or "{}") or {}).get("vypočítané") or ""
        if status in ("not_started", "running") or (status in ("needs_input", "needs_revalidation")
                                                    and binding_work(db, h["id"])):
            rows.append((0 if status != "not_started" else 1, when, h["id"]))  # nové dáta: mapovanie polí
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
    cat = db.catalog_stats()  # index katalógu raz za týždeň (nové datasety pribúdajú)
    if cfg.gate1.nkod_enabled and (not cat["datasety"] or (cat["aktualizované"] or "") < utc_days_ago(7)):
        work["catalog"] = f"index katalógu: {cat['datasety']} datasetov, aktualizovaný {(cat['aktualizované'] or 'nikdy')[:10]}"
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
        from .contact_scout import search_due

        searches = sum(1 for h in ids if search_due(cfg, db, h)) if cfg.privacy.lia_completed else 0
        if orgs or unranked or no_map or searches:
            work["people"] = (f"organizácie na spracovanie {orgs}, nezoradení ľudia {unranked}, bez mapy rolí {no_map}, "
                              f"hľadanie kontaktov podľa hypotézy {searches}")
        since = _since_last(db, ("contacts", "people"))
        fresh = db._one(f"SELECT COUNT(*) AS n FROM people WHERE collected_for_hypothesis_id IN ({marks}) "
                        "AND created_at > ?", (*ids, since))["n"]
        if fresh:
            work["contacts"] = f"{fresh} ľudí od posledného hľadania kontaktov"
        drafts = db._one(
            f"""SELECT COUNT(DISTINCT l.person_id || '-' || l.hypothesis_id) AS n FROM person_problem_links l
                JOIN people p ON p.id = l.person_id
                WHERE l.hypothesis_id IN ({marks}) AND l.priority IS NOT NULL AND p.is_expert = 0
                  AND l.link_type != 'data_holder'
                  AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.person_id = l.person_id AND o.hypothesis_id = l.hypothesis_id)
                  AND EXISTS (SELECT 1 FROM contact_points c WHERE c.person_id = l.person_id
                              OR (c.org_id = p.org_id AND c.is_generic = 1))""", tuple(ids))["n"]
        if drafts:
            work["drafts"] = f"{drafts} zoradených ľudí s kontaktom bez návrhu"
        g1 = gate1_ids(db)
        if g1:
            work["gate1"] = "hypotézy " + ", ".join(f"H{i}" for i in g1)
    focus = focus_work(cfg, db)
    if focus:
        work["org_cards"] = focus
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


def focus_work(cfg: Config, db: DB) -> str:
    """Práca sústredeného režimu: organizácia bez karty, so zastaranou kartou alebo s otvorenou verejnou medzerou."""
    from .opportunity import PUBLIC_SOURCES, current_card, focus_settings

    focus = focus_settings(db)
    if not focus.get("enabled") or not cfg.org_cards.enabled:
        return ""
    todo = []
    for it in focus.get("items") or []:
        card = current_card(db, it["org_id"], it["hypothesis_id"], it.get("role") or "customer")
        if card is None:
            todo.append(f"org {it['org_id']}: bez karty")
        elif card["stale_reason"]:
            todo.append(f"org {it['org_id']}: zastaraná karta")
        elif any(t["status"] == "open" and t["best_source"] in PUBLIC_SOURCES
                 and t["attempts"] < cfg.org_cards.research_max_attempts for t in db.research_tasks_for(card["id"])):
            todo.append(f"org {it['org_id']}: otvorené medzery")
    return ("sústredený režim – " + ", ".join(todo)) if todo else ""


def in_hours(window: list[int] | None, when: datetime) -> bool:
    """Je čas `when` (miestny) v okne [od, do)? Okno cez polnoc: od > do. Bez okna vždy áno."""
    if not window or len(window) != 2:
        return True
    start, end = int(window[0]) % 24, int(window[1]) % 24
    h = when.astimezone().hour
    return start <= h < end if start < end else (h >= start or h < end) if start > end else True


def plan_round(cfg: Config, db: DB, config_path: str | Path, until: datetime, round_no: int,
               now: datetime | None = None) -> dict[str, Any]:
    """Zaradí úlohy kola (s prácou, kým sa zmestia do zvyšného času) a na koniec ďalší autopilot."""
    now = now or datetime.now(timezone.utc)
    left = (until - now).total_seconds() / 60
    if cfg.privacy.lia_completed:  # hypotézy, ktorých test prešiel, idú na Bránu 0 (people.auto_select_validated)
        from .orgs import auto_select_validated

        auto_select_validated(cfg, db)
    work = pending_work(cfg, db)
    planned, skipped = [], {}
    config = str(config_path)
    aging = db.get_setting(AGING_KEY, {}) or {}
    wf = cfg.workflows
    # starnutie: akcia s prácou, ktorá sa `autopilot_aging_rounds` kôl nezmestila, ide na začiatok kola
    aged = [(kd, m) for kd, m in PRIORITY if kd in work and aging.get(kd, 0) >= wf.autopilot_aging_rounds]
    priority = aged + [(kd, m) for kd, m in PRIORITY if (kd, m) not in aged]
    focus_on = "org_cards" in work
    if focus_on:  # karty a návrhy zvolených organizácií prvé (drafts sa nepresúva dvakrát)
        priority = FOCUS + [(kd, m) for kd, m in priority if kd not in dict(FOCUS)]
    has_priority = any(kd in work for kd, _ in PRIORITY)
    has_discovery = any(kd in work for kd, _ in DISCOVERY)
    reserve = 0.0
    if has_priority and has_discovery and not focus_on:  # pevný podiel na nový zber; bez inej práce má zber celý čas
        reserve = left * wf.autopilot_discovery_share
        reserve = reserve if reserve >= MIN_SLICE else 0.0
    used = 0.0
    ids_for = {"gate1": gate1_ids(db, wf.autopilot_max_priorities), "people": priority_ids(cfg, db),
               "contacts": priority_ids(cfg, db), "drafts": priority_ids(cfg, db)}
    if focus_on:
        from .opportunity import focus_settings

        focus_ids = sorted({it["hypothesis_id"] for it in focus_settings(db)["items"]})
        ids_for.update(org_cards=focus_ids, drafts=focus_ids)

    def plan(kind: str, minutes: int, budget: float) -> float:
        if kind not in work:
            skipped[kind] = "bez práce"
            return 0.0
        slice_min = min(minutes, budget)
        if slice_min < MIN_SLICE:
            skipped[kind] = "nezmestí sa do času"
            return 0.0
        window = cfg.workflows.autopilot_hours.get(kind)
        start = now + timedelta(minutes=used)  # odhad: úlohy pred ňou minú celý svoj úsek
        if not in_hours(window, start):
            skipped[kind] = f"mimo času ({window[0]}–{window[1]} h)"
            return 0.0
        job = db.create_job(kind, {"minutes": round(slice_min), "hypotheses": ids_for.get(kind, []), "config": config,
                                   "autopilot": round_no}, status="queued")
        planned.append({"úloha": job, "akcia": kind, "minúty": round(slice_min), "dôvod": work[kind]
                        + (" · prednosť po čakaní" if aging.get(kind, 0) >= wf.autopilot_aging_rounds else "")})
        return slice_min

    for kind, minutes in priority:
        used += plan(kind, minutes, left - reserve - used)
    for kind, minutes in DISCOVERY:  # vyhradený čas + čo priority nevyužili
        used += plan(kind, minutes, left - used)
    for kind, _ in ROUND:  # starnutie: prácu mala, nezmestila sa → +1; naplánovaná alebo bez práce → 0
        aging[kind] = aging.get(kind, 0) + 1 if skipped.get(kind) == "nezmestí sa do času" else 0
    db.set_setting(AGING_KEY, {k_: v for k_, v in aging.items() if v})
    next_job = None
    if planned:  # ďalší autopilot prepočíta zvyšný čas (úlohy často skončia skôr); keď sa nič nezmestí, reťaz skončí
        next_job = db.create_job("autopilot", {"minutes": 5, "hypotheses": [], "config": config,
                                               "until": until.isoformat(timespec="seconds"), "round": round_no + 1},
                                 status="queued")
    log.info("Autopilot kolo %d: %d úloh, ďalšie kolo %s (do %s)", round_no, len(planned),
             f"#{next_job}" if next_job else "nie", until.isoformat(timespec="minutes"))
    return {"kolo": round_no, "do": until.isoformat(timespec="minutes"), "naplánované": planned,
            "preskočené": skipped, "ďalšie_kolo": next_job, "naplánované_o": utcnow(),
            "rezerva_na_zber_min": round(reserve), "prioritné_hypotézy": ids_for["people"],
            "sústredený_režim": focus_on}


def until_for(job, minutes: float) -> datetime:
    """Koniec autopilota: z argumentov úlohy (ďalšie kolá), inak začiatok prvej úlohy + zvolený čas."""
    args = json.loads(job["args_json"] or "{}") if job is not None else {}
    if args.get("until"):
        return datetime.fromisoformat(args["until"])
    start = datetime.fromisoformat(job["started_at"]) if job is not None and job["started_at"] else datetime.now(timezone.utc)
    return start + timedelta(minutes=minutes)
