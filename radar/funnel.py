"""Lievik („hypotéza po hypotéze“, akcia `funnel`): donekonečna jeden krok jednej hypotézy po druhom.

Na rozdiel od autopilota (kolá akcií s časovými úsekmi pre všetky hypotézy naraz) lievik vezme jednu hypotézu a vedie
ju čo najďalej, až potom berie ďalšiu. Kroky v poradí podľa toho, čo dnes treba od hypotézy po Bránu 2:

    nájsť ──► otestovať ──► kontakty (Brána 0) ──► Brána 1 (dáta) ──► Brána 2 (experimenty)
    scout     validate      people                 gate1               gate2

- nájsť (scout): objavovanie a agent prieskumu; nová hypotéza je kandidát, kým nemá 2 overené tvrdenia z 2 domén,
- otestovať: agent hľadá zdroje za aj proti → kód a presný model posúdia citáty → verdikt kódu; ďalej ide iba verdikt
  z people.auto_select_verdicts („podložená“), inak lievik pre hypotézu končí (opakovaný test po retest_days),
- kontakty: organizácie → plán kontaktov → ľudia a kontakty → poradie (iba s LIA a pre hypotézu na Bráne 0),
- Brána 1: polia → zdroje → kontrola a sťahovanie → reprodukcia → pripravenosť; ≥ prah → Brána 2,
  inak „čaká na tvoje dáta“ (lievik pre hypotézu stojí, kým nevložíš dáta alebo „Prehľadať znova“),
- Brána 2: problémová karta (aj podhypotézy) → návrh experimentov → spätný test uzamknutých → záver.

Každá úloha `funnel` urobí jeden krok (časový limit z workflows.funnel_minutes) a zaradí ďalšiu úlohu `funnel`, kým ho
nezastavíš („Zastaviť všetko vrátane poradia“), nevyprší zvolený čas alebo nezlyhá funnel_max_failures krokov za sebou.
Ktorú hypotézu ďalej:
1. tú istú, ak má ďalší krok (fokus),
2. každých funnel_find_every testov hľadanie nových hypotéz,
3. hypotézu, ktorá už prešla testom a má ďalší krok (najďalej v lieviku najskôr),
4. neotestovanú (poradie fronty testu: vybrané na Bránu 0, s viac overenými tvrdeniami skôr),
5. inak hľadanie nových hypotéz („pokiaľ nenájde, ide znova“).
Krok, ktorý pri rovnakom stave hypotézy beží funnel_max_attempts-krát (nič nezmenil), sa pre hypotézu odloží; vráti sa
k nej, keď sa jej stav zmení (napr. vložíš dáta). Stav je v DB (funnel_log), beh sa dá kedykoľvek prerušiť.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .db import DB
from .util import Deadline, utc_days_ago

log = logging.getLogger(__name__)

STEPS = ("test", "contacts", "gate1", "gate2")
STEP_LABEL = {"find": "nájsť nové hypotézy", "test": "otestovať", "contacts": "kontakty (Brána 0)",
              "gate1": "Brána 1 – dáta", "gate2": "Brána 2 – experimenty"}
STEP_ACTION = {"find": "scout", "test": "validate", "contacts": "people", "gate1": "gate1", "gate2": "gate2"}
STAGE_RANK = {"gate2": 3, "gate1": 2, "contacts": 1, "test": 0}


def verdict_of(h) -> str | None:
    try:
        return (json.loads(h["validation_json"] or "{}") or {}).get("verdikt")
    except ValueError:
        return None


def family(db: DB, hid: int) -> list[int]:
    """Hypotéza a jej podhypotézy (Brána 2 delí širokú hypotézu; experimenty majú podhypotézy)."""
    return [hid] + [c["id"] for c in db.children_of(hid) if c["status"] != "rejected"]


def in_window(cfg: Config, step: str, now: datetime | None = None) -> bool:
    from .autopilot import in_hours

    return in_hours(cfg.workflows.autopilot_hours.get(STEP_ACTION.get(step, step)), now or datetime.now(timezone.utc))


# ================================================================ stav hypotézy
def step_state(cfg: Config, db: DB, h, step: str) -> tuple[str, str]:
    """(todo | done | stop | skip, dôvod) jedného kroku pre hypotézu."""
    hid = h["id"]
    if step == "test":
        state = h["validation_state"]
        if state is None:
            return "todo", "neotestovaná"
        if state == "searched":
            return "todo", "test čaká na posúdenie nájdených tvrdení"
        verdict = verdict_of(h)
        if verdict in cfg.people.auto_select_verdicts:
            if h["status"] == "candidate":
                return "stop", "kandidát – menej ako 2 overené tvrdenia z 2 domén"
            return "done", f"test: {verdict}"
        if (h["validated_at"] or "") < utc_days_ago(cfg.workflows.retest_days):
            return "todo", f"opakovaný test (naposledy {verdict or '?'})"
        return "stop", f"test: {verdict or '?'}"
    if step == "contacts":
        if not cfg.privacy.lia_completed:
            return "skip", "kontakty čakajú na LIA"
        if not h["people_enabled"]:
            return "skip", "kontakty ručne vypnuté" if h["people_manual"] == 0 else "kontakty: nie je na Bráne 0"
        if cfg.people.contact_search:
            from .contact_scout import search_due

            due = search_due(cfg, db, hid)
            if h["contacts_searched_at"] is None or due:
                return "todo", due or "bez hľadania kontaktov"
            return "done", "kontakty hľadané"
        return ("done", "ľudia zoradení") if db.funnel_runs(hid, "contacts") else ("todo", "bez hľadania ľudí")
    if step == "gate1":
        g1 = h["gate1_status"] or "not_started"
        if g1 in ("not_started", "running", "ready"):
            return "todo", {"not_started": "Brána 1 nezačatá", "running": "Brána 1 rozpracovaná"}.get(g1, "Brána 1")
        if g1 in ("needs_input", "needs_revalidation"):
            from .gate1 import binding_work

            if db.pending_gate1_inputs([hid]):
                return "todo", "posúdiť tvoje dáta"
            if binding_work(db, hid):
                return "todo", "mapovanie polí na stĺpce (nové dáta)"
            if any(b["validation_status"] == "code_ok" and b["is_key"] for b in db.bindings_for(hid)):
                return "stop", "Brána 1 čaká na tvoje potvrdenie stĺpcov kľúčových polí (Brána 1 → Mapovanie polí)"
            if g1 == "needs_revalidation":
                return "stop", "Brána 1: treba znova overiť – podmienky prechodu už neplatia (Brána 1 → Mapovanie polí)"
            return "stop", "Brána 1 čaká na tvoje dáta (Brána 1 → Tvoje dáta, alebo Prehľadať znova)"
        if g1 == "passed":
            return "done", "Brána 1 prešla"
        return "stop", f"Brána 1: {g1} (tvoje rozhodnutie)"
    if step == "gate2":
        return gate2_state(cfg, db, h)
    raise ValueError(f"Neznámy krok lievika {step!r}")


def gate2_state(cfg: Config, db: DB, h) -> tuple[str, str]:
    from .cards import card_targets
    from .exp_plan import counts, plan_targets

    if h["gate1_status"] != "passed":
        return "stop", "Brána 1 neprešla"
    if (h["gate2_status"] or "") in ("paused", "stopped"):
        return "stop", f"Brána 2: {h['gate2_status']} (tvoje rozhodnutie)"
    fam = family(db, h["id"])
    if card_targets(cfg, db, fam):
        return "todo", "problémová karta"
    if plan_targets(cfg, db, fam):
        return "todo", "návrh experimentov"
    exps = [e for i in fam for e in db.experiments_for(i) if e["task"]]
    if any(e["status"] == "locked" for e in exps):
        return "todo", "spätný test uzamknutých experimentov"
    if any(e["status"] == "done" and not e["conclusion_json"] for e in exps):
        return "todo", "záver experimentu"
    drafts = [e for e in exps if e["status"] == "draft" and counts(e)]
    if drafts:
        return "stop", f"Brána 2: {len(drafts)} návrh(y) čakajú na tvoje uzamknutie"
    if any(e["status"] == "done" for e in exps):
        return "done", "experimenty so záverom"
    for i in fam:
        card = db.latest_card(i)
        if card is not None and (json.loads(card["card_json"] or "{}") or {}).get("chýbajúce_dáta_pre_experiment"):
            return "stop", "Brána 2: v dátach chýba, čo experiment potrebuje (Brána 1 → datasety)"
    if not any(db.datasets_for(i, only_ok=True) for i in fam):
        return "stop", "Brána 2: bez datasetu"
    return "stop", "Brána 2: bez experimentu"


def next_step(cfg: Config, db: DB, h, now: datetime | None = None) -> tuple[str | None, str]:
    """Ďalší krok hypotézy v lieviku a prečo; (None, dôvod) = pre ňu lievik skončil alebo čaká na teba."""
    if h is None:
        return None, "hypotéza neexistuje"
    if h["status"] == "rejected":
        return None, "zamietnutá"
    notes = []
    for step in (s for s in STEPS if s in cfg.workflows.funnel_steps):
        state, why = step_state(cfg, db, h, step)
        if state in ("done", "skip"):
            if state == "skip":
                notes.append(why)
            continue
        if state == "stop":
            return None, why
        if not in_window(cfg, step, now):
            if step == "contacts":  # kontakty nič neblokujú: doplnia sa, keď príde ich čas
                notes.append("kontakty mimo času (autopilot_hours)")
                continue
            return None, f"{STEP_LABEL[step]}: mimo času (workflows.autopilot_hours)"
        return step, why
    return None, "hotovo" + (f" ({'; '.join(notes)})" if notes else "")


def signature(db: DB, h) -> str:
    """Odtlačok stavu hypotézy pred krokom: rovnaký odtlačok po kroku = krok nič nezmenil."""
    from .contact_scout import target_queries

    hid = h["id"]
    fam = family(db, hid)
    targets = db.contact_targets_for(hid)
    fields = db.fields_for(hid)
    exps = [e for i in fam for e in db.experiments_for(i)]
    parts = {
        "h": [h["status"], h["validation_state"], verdict_of(h), len(db.evidence_for(hid)), h["people_enabled"],
              h["gate1_status"], h["gate2_status"]],
        "kontakty": [len(targets), sum(1 for t in targets for q in target_queries(t) if q.get("done_at")),
                     sorted(db.org_hypotheses_by_relation(hid).items()), len(db.links_for_hypothesis(hid)),
                     db.orgs_waiting_for_people(hid)],
        "brána1": [len(fields), sorted(Counter(f["status"] for f in fields).items()),
                   sum(1 for s in db.sources_for(hid) if s["status"] != "rejected"),
                   len(db.datasets_for(hid, only_ok=True)), len(db.pending_gate1_inputs([hid]))],
        "brána2": [len(fam), sum(1 for i in fam if db.latest_card(i) is not None),
                   sorted(Counter(f"{e['status']}:{bool(e['conclusion_json'])}" for e in exps).items())],
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ================================================================ výber
def _usable(cfg: Config, db: DB, h, now: datetime | None) -> tuple[str, str, str] | None:
    step, why = next_step(cfg, db, h, now)
    if step is None:
        return None
    sig = signature(db, h)
    w = cfg.workflows
    if db.funnel_attempts(h["id"], step, sig) >= w.funnel_max_attempts or db.funnel_runs(h["id"], step) >= w.funnel_max_runs_per_step:
        return None  # zaseknutá: krok nič nemení alebo minul limit behov; vráti sa, keď sa stav zmení
    return step, why, sig


def pick(cfg: Config, db: DB, now: datetime | None = None) -> dict[str, Any]:
    """Ďalšia práca lievika: {hypothesis_id, step, reason, signature} alebo {step: "find"} alebo {step: None}."""
    w = cfg.workflows
    find_ok = in_window(cfg, "find", now)
    last = db.funnel_last()
    if last is not None and last["hypothesis_id"] and last["next_step"]:
        h = db.get_hypothesis(last["hypothesis_id"])
        u = _usable(cfg, db, h, now) if h is not None else None
        if u:
            return {"hypothesis_id": h["id"], "step": u[0], "reason": f"pokračuje: {u[1]}", "signature": u[2]}
    if w.funnel_find_every and find_ok and db.funnel_steps_since_find("test") >= w.funnel_find_every:
        return {"hypothesis_id": None, "step": "find", "reason": f"po {w.funnel_find_every} testoch aj nové hypotézy"}
    seen: set[int] = set()
    started = []
    pools = (db.validated_hypotheses(tuple(cfg.people.auto_select_verdicts)), db.gate2_hypotheses(),
             db.gate1_hypotheses(), db.people_enabled_hypotheses())
    for pool in pools:
        for h in pool:
            if h["id"] in seen or h["status"] == "rejected":
                continue
            seen.add(h["id"])
            u = _usable(cfg, db, h, now)
            if u and u[0] != "test":
                started.append((-STAGE_RANK[u[0]], -int(h["people_enabled"] or 0), h["id"], u))
    if started:
        _, _, hid, u = min(started)
        return {"hypothesis_id": hid, "step": u[0], "reason": f"rozpracovaná: {u[1]}", "signature": u[2]}
    for h in db.validation_queue(60, utc_days_ago(w.retest_days)):
        if h["status"] == "rejected":
            continue
        u = _usable(cfg, db, h, now)
        if u and u[0] == "test":
            return {"hypothesis_id": h["id"], "step": "test", "reason": f"z fronty testu: {u[1]}", "signature": u[2]}
    if find_ok:
        return {"hypothesis_id": None, "step": "find", "reason": "niet čo testovať – hľadám nové hypotézy"}
    return {"hypothesis_id": None, "step": None, "reason": "niet čo robiť (hľadanie nových je mimo času)"}


# ================================================================ kroky
def run_test(app, hid: int, dl: Deadline, run: Callable[[str, Deadline], bool]) -> None:
    """Agent (zdroje za aj proti) → typ vydavateľa → embeddingy → posúdenie a zaradenie tvrdení → verdikt kódu."""
    h = app.db.get_hypothesis(hid)
    app.validate_ids, app.validate_limit = [hid], 1
    if h["validation_state"] != "searched":  # agent už hľadal: zostáva posúdiť jeho tvrdenia
        run("validate-search", dl.sub(dl.remaining_minutes() * 0.5))
    run("source-types", dl.sub(min(3.0, dl.remaining_minutes())))
    run("embed", dl.sub(min(3.0, dl.remaining_minutes())))
    run("validate-verify", dl.sub(max(0.0, dl.remaining_minutes() - 2) * 0.6))
    run("validate-cluster", dl.sub(max(0.0, dl.remaining_minutes() - 1)))
    run("validate-finish", Deadline(2))


def run_contacts(app, hid: int, dl: Deadline, run: Callable[[str, Deadline], bool]) -> None:
    from .workflows import people_stages

    app.people_ids = [hid]
    people_stages(app, dl, run)


def run_gate1(app, hid: int, dl: Deadline, run: Callable[[str, Deadline], bool]) -> None:
    from .workflows import _gate1

    _gate1(app, dl, run, [hid], lambda **kw: None)


def run_gate2(app, hid: int, dl: Deadline, run: Callable[[str, Deadline], bool]) -> None:
    """Karta (aj rozdelenie na podhypotézy) → návrh experimentov → spätný test uzamknutých → záver → demo."""
    app.gate_ids = family(app.db, hid)
    run("gate2-cards", dl.sub(dl.remaining_minutes() * 0.25))
    app.gate_ids = family(app.db, hid)  # rozdelenie mohlo pridať podhypotézy
    run("gate2-plan", dl.sub(dl.remaining_minutes() * 0.45))
    run("gate2-backtest", dl.sub(dl.remaining_minutes() * 0.6))
    run("gate2-conclusion", dl.sub(max(1.0, dl.remaining_minutes() - 2)))
    run("gate2-demo", Deadline(3))


def run_find(app, dl: Deadline, run: Callable[[str, Deadline], bool]) -> None:
    from .workflows import _scout

    _scout(app, dl, run, None, lambda **kw: None)


RUNNERS: dict[str, Callable[..., None]] = {"test": run_test, "contacts": run_contacts, "gate1": run_gate1,
                                           "gate2": run_gate2}


def describe(cfg: Config, db: DB, h, step: str) -> str:
    """Výsledok kroku jednou vetou (z DB, nie z modelu)."""
    hid = h["id"]
    if step == "test":
        return f"verdikt: {verdict_of(h)}" if h["validation_state"] == "tested" else "test čaká na posúdenie tvrdení"
    if step == "contacts":
        from .contact_scout import contactable

        people = len({l["person_id"] for l in db.links_for_hypothesis(hid) if l["priority"] is not None})
        return f"ľudia s väzbou {people}, kontaktovateľní (e-mail) {contactable(db, hid)}"
    if step == "gate1":
        score = (json.loads(h["gate1_json"] or "{}") or {}).get("skóre")
        return f"Brána 1: {h['gate1_status']}" + (f", pripravenosť {score:.2f}".replace(".", ",") if score is not None else "")
    exps = [e for i in family(db, hid) for e in db.experiments_for(i) if e["task"]]
    stats = Counter(e["status"] for e in exps)
    return ("experimenty: " + (", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "žiadne")
            + f", so záverom {sum(1 for e in exps if e['conclusion_json'])}")


# ================================================================ úloha
def chain_until(job, args: dict[str, Any]) -> datetime | None:
    """Koniec lievika: z reťaze (ďalšie úlohy), inak začiatok prvej úlohy + minúty (0 = donekonečna)."""
    if args.get("chain"):
        return datetime.fromisoformat(args["until"]) if args.get("until") else None
    minutes = float(args.get("minutes") or 0)
    if minutes <= 0:
        return None
    start = datetime.fromisoformat(job["started_at"]) if job is not None and job["started_at"] else datetime.now(timezone.utc)
    return start + timedelta(minutes=minutes)


def funnel_job(app, run: Callable[[str, Deadline], bool], report_progress: Callable[..., None],
               now: datetime | None = None) -> dict[str, Any]:
    """Jeden krok lievika a zaradenie ďalšej úlohy `funnel` (reťaz končí zastavením, časom alebo chybami)."""
    cfg, db = app.cfg, app.db
    w = cfg.workflows
    job_id = getattr(app, "job_id", None)
    job = db.get_job(job_id) if job_id else None
    args = json.loads(job["args_json"] or "{}") if job is not None else {}
    until = chain_until(job, args)
    now = now or datetime.now(timezone.utc)
    if until is not None and now >= until:
        report_progress(lievik={"stop": "vypršal zvolený čas"})
        return {"stop": "vypršal zvolený čas"}
    item = pick(cfg, db, now)
    step, hid = item["step"], item.get("hypothesis_id")
    report_progress(lievik={"krok": STEP_LABEL.get(step or "", "nič"), "hypotéza": f"H{hid}" if hid else None,
                            "dôvod": item["reason"]})
    out: dict[str, Any] = {"krok": step, "hypotéza": hid, "dôvod": item["reason"]}
    failures: list[str] = []
    if step is not None:
        row = db.start_funnel_step(hid, step, item.get("signature"), job_id)

        def tracked(stage: str, dl: Deadline) -> bool:
            if dl.expired():
                return run(stage, dl)  # preskočené pre čas nie je chyba
            ok = run(stage, dl)
            if not ok:
                failures.append(stage)
            return ok

        minutes = float(w.funnel_minutes.get(step, 60))
        if until is not None:
            minutes = max(1.0, min(minutes, (until - now).total_seconds() / 60))
        from .util import utcnow

        t0 = utcnow()
        try:
            if step == "find":
                run_find(app, Deadline(minutes), tracked)
            else:
                RUNNERS[step](app, hid, Deadline(minutes), tracked)
        except Exception as exc:  # noqa: BLE001 – lievik pokračuje ďalším krokom (invariant 5)
            log.exception("Lievik: krok %s pre H%s zlyhal", step, hid)
            failures.append(f"{step}: {str(exc)[:160]}")
        if step == "find":
            new = list(db.hypotheses_created_since(t0))
            outcome = f"nové hypotézy {len(new)} (kandidáti {sum(1 for h in new if h['status'] == 'candidate')})"
            nxt, why = None, "ďalej: test" if new else "nič nové – lievik hľadá znova alebo testuje frontu"
        else:
            h = db.get_hypothesis(hid)
            outcome = describe(cfg, db, h, step) if h is not None else "hypotéza zmazaná"
            nxt, why = next_step(cfg, db, h, now) if h is not None else (None, "hypotéza zmazaná")
        failed = bool(failures)
        db.finish_funnel_step(row, outcome, nxt, why, failed, {"chyby": failures, "dôvod_výberu": item["reason"]})
        out.update(výsledok=outcome, ďalej=STEP_LABEL.get(nxt or "", why), chyby=failures)
        log.info("Lievik: %s%s → %s; ďalej: %s", STEP_LABEL[step], f" H{hid}" if hid else "", outcome, nxt or why)
    stop = None
    end = datetime.now(timezone.utc) if now is None else now
    if until is not None and end >= until:
        stop = "vypršal zvolený čas"
    elif db.funnel_recent_failures() >= w.funnel_max_failures:
        stop = f"{w.funnel_max_failures} zlyhané kroky za sebou – skontroluj modely a SearXNG (python -m radar doctor)"
    elif step is None:
        stop = item["reason"]
    elif job_id is None:
        stop = "jeden krok (python -m radar workflow funnel); donekonečna beží ako úloha: python -m radar job funnel"
    if stop is None:
        config = args.get("config") or str(Path(cfg.config_dir) / "config.yaml")
        out["ďalšia_úloha"] = db.create_job("funnel", {
            "minutes": max(w.funnel_minutes.values() or [60]), "hypotheses": [], "config": config, "chain": True,
            "until": until.isoformat(timespec="seconds") if until else None}, status="queued")
    else:
        out["stop"] = stop
        log.warning("Lievik zastavil: %s", stop)
    report_progress(lievik=out)
    return out


def waiting_for_you(db: DB, limit: int = 300) -> list[dict[str, Any]]:
    """Hypotézy, pri ktorých lievik naposledy skončil s „čaká na tvoje …“ (dáta, uzamknutie plánu, rozhodnutie)."""
    seen: set[int] = set()
    out = []
    for r in db.funnel_log(limit):
        hid = r["hypothesis_id"]
        if not hid or hid in seen or r["finished_at"] is None:
            continue
        seen.add(hid)
        if r["next_step"] is None and r["reason"] and ("tvoje" in r["reason"] or "tvojím" in r["reason"]):
            out.append({"hypotéza": f"H{hid}", "názov": (r["title"] or "")[:90], "čaká na": r["reason"],
                        "od": r["finished_at"]})
    return out
