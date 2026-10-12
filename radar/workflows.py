"""Akcie (agentické workflowy) spúšťané v GUI (Správca akcií, Prehľad, Problémy) alebo príkazom
`python -m radar workflow …`. Každá akcia iteratívne zbiera alebo spracúva dáta lokálnymi modelmi:

- scout: nové hypotézy z webu (objavovanie + agent prieskumu → screen, extrakcia, overenie, zaradenie),
- validate: test uložených hypotéz po dávkach (agent → overenie → zaradenie → verdikt kódu),
- people: organizácie, ľudia, kontakty a LinkedIn pre vybrané problémy (vyžaduje LIA; nič sa neodosiela),
- drafts: návrhy oslovení pre vybraných ľudí (nič sa neodosiela),
- evidence: zásobník dôkazov – posúdenie čakajúcich tvrdení, zaradenie, zlúčenie podobných hypotéz,
- topics: zber dôkazov k témam a projektom z config.yaml,
- critique: kritika hypotéz (protiargumenty, otázky na rozhovory) a hrubý odhad trhu,
- contacts: e-mail, telefón a LinkedIn k vybraným ľuďom (bez modelu),
- gate1: Brána 1 – polia, zdroje, datasety (DuckDB), reprodukcia známeho čísla, pripravenosť dát,
- gate2: Brána 2 – predregistrácia, spätný test, demo pre hypotézu × organizáciu,
- bench: rýchlosť a schopnosti modelov každej roly,
- run-all: celý beh ako v noci.

Kroky idú v poradí podľa modelov ako run-all (najprv rýchly model, potom presný), aby sa veľký
model menil čo najmenej. Stav je v DB, beh sa dá kedykoľvek prerušiť a znova spustiť.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

import time

from .privacy import PrivacyGateError
from .util import Deadline, awake_clock, keep_awake, utc_days_ago, utcnow

log = logging.getLogger("radar")


@dataclass(frozen=True)
class Action:
    label: str
    what: str  # čo akcia robí
    data: str  # aké údaje pribudnú
    models: str  # rýchly / presný / bez GPU
    minutes: int  # predvolený časový limit
    hypotheses: str | None = None  # None | "verified" (overené hypotézy) | "selected" (vybrané na Bránu 0)
    needs_lia: bool = False
    gpu: bool = True


ACTIONS: dict[str, Action] = {
    "scout": Action(
        "Hľadať nové hypotézy",
        "Objavovanie (matica odvetvie × vzor bolesti) a agent prieskumu na webe → triedenie → extrakcia citátov → "
        "overenie kódom a presným modelom → zaradenie. Nový problém je kandidát, kým nemá 2 overené tvrdenia z 2 domén.",
        "dokumenty, overené tvrdenia, nové hypotézy a kandidáti", "rýchly + presný", 90),
    "validate": Action(
        "Otestovať všetky hypotézy",
        "Fronta všetkých uložených hypotéz po dávkach 5: agent hľadá nové zdroje, protidôkazy a existujúce riešenia; "
        "citáty overí kód a presný model; verdikt (podložená / sporná / slabá / nepodložená) počíta kód.",
        "nové citáty k hypotézam, verdikt testu", "rýchly + presný", 480),
    "people": Action(
        "Zbierať ľudí ku overeným hypotézam",
        "Pre zvolené overené hypotézy: organizácie (dôkazy, obstarávanie, segment + RPO/RÚZ) → ľudia a zverejnené "
        "pracovné kontakty z webov → mapa rolí, priorita, výber ≤ 15 na problém → e-mail a telefón ku každej osobe "
        "(stránky s jej menom) → LinkedIn. Nič sa neodosiela.",
        "organizácie, ľudia, e-maily, telefóny, LinkedIn", "rýchly + presný", 120, "verified", needs_lia=True),
    "drafts": Action(
        "Pripraviť návrhy oslovení",
        "Pre vybraných ľudí napíše presný model návrh e-mailu, poznámky na LinkedIn a telefonický úvod iba z overených "
        "faktov; kód skontroluje dĺžky, čísla a pätičku. Nič sa neodosiela – posielaš ručne.",
        "návrhy oslovení na kontrolu", "presný", 60, "selected", needs_lia=True),
    "evidence": Action(
        "Spracovať zásobník dôkazov",
        "Presný model posúdi čakajúce tvrdenia (citát už overil kód), zaradí overené do hypotéz alebo založí nové "
        "a zlúči podobné hypotézy; dokončí verdikty testov, ktoré čakali na posúdenie.",
        "overené tvrdenia, nové a zlúčené hypotézy", "presný", 90),
    "topics": Action(
        "Zbierať dôkazy k témam",
        "Vyhľadávanie a RSS k témam a projektom z config.yaml → extrakcia citátov → overenie → zaradenie.",
        "dokumenty a tvrdenia k témam", "rýchly + presný", 120),
    "critique": Action(
        "Kritika a odhad trhu",
        "Presný model ku hypotézam s dôkazmi napíše protiargumenty, čo ešte overiť a otázky na rozhovory; "
        "hrubý odhad trhu (čísla počíta Python, hodnoty iba zo zdrojov).",
        "kritiky, otázky na rozhovory, odhady trhu", "presný", 60),
    "themes": Action(
        "Zoskupiť hypotézy do tém",
        "Podobné hypotézy (podľa embeddingov) zoskupí do tém, napr. všetky o e-faktúrach; názov témy navrhne rýchly "
        "model zo zoznamu hypotéz. Hypotézy sa nezlučujú ani nemenia; ručne premenované témy ostanú.",
        "témy a ich súhrn dôkazov (Problémy → Témy)", "embeddingy + rýchly", 15),
    "contacts": Action(
        "Doplniť kontakty k ľuďom (e-mail, telefón, LinkedIn)",
        "Pre vybraných ľudí bez osobného kontaktu vyhľadá stránky s ich menom na webe ich organizácie alebo úradných "
        "doménach; e-mail a telefón uloží, iba ak sú v tom istom bloku ako meno a doslova v úryvku (nič sa nehádá). "
        "Potom URL profilu LinkedIn z výsledkov vyhľadávania (stránky LinkedIn sa nesťahujú).",
        "e-maily, telefóny, LinkedIn k ľuďom", "bez GPU (vyhľadávač a web)", 30, "selected", needs_lia=True, gpu=False),
    "gate1": Action(
        "Brána 1: prieskum dát",
        "Pre hypotézy vybrané na Bránu 0: polia potrebné na riešenie (kľúčové polia) → zdroje (licencia, frekvencia, "
        "prístup – iba ak sú na prečítanej stránke) → kontrola a kvalita → stiahnutie do DuckDB (limit 2 GB / 50 GB) → "
        "zreprodukovanie známeho čísla → návrhy žiadostí podľa 211/2000 a pripravenosť dát. Nič sa nevyraďuje ani neodosiela.",
        "polia, zdroje, datasety, reprodukcia, pripravenosť dát", "presný + agent", 120, "gate0"),
    "gate2": Action(
        "Brána 2: experimenty a demo",
        "Pre hypotézy, pri ktorých si Bránu 1 označil ako prejdenú: dvojice hypotéza × organizácia z dát → návrh "
        "predregistrácie (potvrdíš a uzamkneš v GUI) → spätný test proti naivnému odhadu (iba Python) → demo pre organizáciu.",
        "experimenty, výsledky spätného testu, demá", "presný (výpočty bez modelu)", 60, "gate1_passed"),
    "bench": Action(
        "Zmerať rýchlosť modelov",
        "Pre každú rolu (rýchly, presný, agent, embeddingy) zmeria načítanie, rýchlosť promptu a generovania a či model "
        "vráti platný JSON a volá nástroje. Pomôže pri výbere modelov.",
        "výsledky v Eval (benchmark)", "všetky roly", 20),
    "run-all": Action(
        "Celý beh (ako v noci)",
        "Všetko v poradí podľa modelov: objavovanie, prieskum, zber k témam, extrakcia, ľudia, test hypotéz, "
        "posúdenie, zaradenie, kritika, trh, správa a retencia.",
        "všetko vyššie", "rýchly + presný", 480),
}
WORKFLOWS = {kind: action.label for kind, action in ACTIONS.items()}
MIN_ROUND_MINUTES = 5.0


def _short(stats: Any) -> Any:
    if isinstance(stats, dict):
        return {k: v for k, v in stats.items() if k != "report" and not str(k).startswith("plan:")}
    return stats


def run_workflow(app, kind: str, minutes: float, hypothesis_ids: list[int] | None = None, job_id: int | None = None,
                 run_stage: Callable[[Any, str, Deadline], dict] | None = None) -> dict:
    if kind not in WORKFLOWS:
        raise ValueError(f"Neznámy workflow {kind!r}, povolené: {', '.join(WORKFLOWS)}")
    if run_stage is None:
        from .cli import run_stage
    db = app.db
    started = utcnow()
    deadline = Deadline(minutes)
    results: dict[str, Any] = {"workflow": kind, "stages": [], "failed": False}
    progress: dict[str, Any] = {"workflow": WORKFLOWS[kind]}
    if job_id is not None:
        db.update_job(job_id, pid=os.getpid(), status="running")

    def report_progress(**fields: Any) -> None:
        progress.update(fields)
        if job_id is not None:
            db.update_job(job_id, progress_json=progress)

    def run(stage: str, dl: Deadline) -> bool:
        if dl.expired():
            log.warning("Krok %s preskočený: vypršal čas", stage)
            results["stages"].append({"krok": stage, "preskočené": "vypršal čas"})
            return False
        report_progress(krok=stage)
        t0 = awake_clock()
        try:
            stats = run_stage(app, stage, dl)
        except PrivacyGateError as exc:
            log.warning("Krok %s preskočený: %s", stage, exc)
            results["stages"].append({"krok": stage, "preskočené": str(exc)})
            return False
        except Exception as exc:  # noqa: BLE001 – jeden krok nezastaví ostatné (invariant 5)
            log.exception("Krok %s zlyhal", stage)
            results["stages"].append({"krok": stage, "chyba": str(exc)[:200]})
            results["failed"] = True
            return False
        results["stages"].append({"krok": stage, "trvanie_min": round((awake_clock() - t0) / 60, 1),
                                  **(_short(stats) or {})})
        return True

    status = "failed"
    wall0, awake0 = time.time(), awake_clock()
    try:
        with keep_awake():  # počas akcie sa počítač neuspí pre nečinnosť
            if kind == "run-all":
                _run_all(app, minutes, run_stage, report_progress, results)
            else:
                FLOWS[kind](app, deadline, run, hypothesis_ids, report_progress)
        results["nové_hypotézy"] = [dict(h) for h in db.hypotheses_created_since(started)]
        results["testovanie"] = db.validation_counts()
        status = "failed" if results["failed"] else "done"
    except KeyboardInterrupt:
        status = "stopped"
        raise
    finally:
        slept = (time.time() - wall0) - (awake_clock() - awake0)
        if slept > 120:  # počítač spal (zatvorené veko, ručné uspanie) – limit sa za ten čas neráta
            results["spánok_min"] = round(slept / 60)
            log.warning("Počas akcie počítač spal %d min", round(slept / 60))
        if job_id is not None:
            db.update_job(job_id, status=status, stats_json=results, finished_at=utcnow())
            if status in ("done", "failed"):
                _start_next(app)
    return results


def _start_next(app) -> None:
    """Po skončení úlohy spusti ďalšiu z poradia (Správca akcií)."""
    try:
        from .jobs import start_next

        start_next(app.cfg, app.db)
    except Exception as exc:  # noqa: BLE001 – poradie sa dá spustiť aj ručne v GUI
        log.warning("Ďalšia úloha z poradia sa nespustila: %s", exc)


def _scout(app, deadline: Deadline, run, ids, report_progress) -> None:
    w = app.cfg.workflows
    app.discover_budget = w.scout_docs_per_job
    app.scout_sessions = w.scout_sessions_per_job
    fast = deadline.sub(deadline.remaining_minutes() * 0.55)
    for stage in ("discover", "scout", "screen", "extract", "verify-code", "source-types"):
        run(stage, fast)
    run("embed", deadline.sub(min(10.0, deadline.remaining_minutes())))
    # Presný model: posúdenie najviac 60 % zvyšného času, zvyšok zaradenie – nové hypotézy vznikajú až v cluster
    # (reálny beh: verify minul celý čas a z 65 overených tvrdení nevznikla ani jedna hypotéza).
    accurate_min = max(0.0, deadline.remaining_minutes() - 2)
    run("verify", deadline.sub(accurate_min * 0.6))
    run("cluster", deadline.sub(max(0.0, deadline.remaining_minutes() - 2)))
    run("themes", Deadline(5))  # nové hypotézy hneď v témach (kód + krátke názvy rýchlym modelom)
    run("report", Deadline(5))


def _themes(app, deadline: Deadline, run, ids, report_progress) -> None:
    run("embed", deadline.sub(min(5.0, deadline.remaining_minutes())))
    run("themes", deadline)


def _validate(app, deadline: Deadline, run, ids: list[int] | None, report_progress) -> None:
    """Dávky: agent (rýchly model) → embeddingy → posúdenie a zaradenie tvrdení dávky (presný model) → verdikt kódu."""
    db, w = app.db, app.cfg.workflows
    batch = len(ids) if ids else w.validate_batch
    app.validate_ids, app.validate_limit = ids, batch
    rounds = 0
    while not deadline.expired():
        if rounds and deadline.remaining_minutes() < MIN_ROUND_MINUTES:
            break  # na ďalšiu dávku (agent + posúdenie presným modelom) už nie je čas
        queue = db.validation_queue(batch, utc_days_ago(w.retest_days), ids)
        if not queue and not db.hypotheses_in_validation():
            break
        rounds += 1
        report_progress(dávka=rounds, hypotézy=[f"H{h['id']}" for h in queue])
        if queue and not run("validate-search", deadline.sub(deadline.remaining_minutes() * 0.6)):
            break  # model nebeží alebo krok zlyhal: zvyšok fronty počká na ďalší beh
        run("source-types", deadline.sub(min(5.0, deadline.remaining_minutes())))
        run("embed", deadline.sub(min(5.0, deadline.remaining_minutes())))
        # Iba tvrdenia z tejto dávky (nie celý zásobník z iných zdrojov): verdikty prídu po minútach, nie hodinách.
        accurate_min = max(0.0, deadline.remaining_minutes() - 1)
        run("validate-verify", deadline.sub(accurate_min * 0.6))
        run("validate-cluster", deadline.sub(max(0.0, deadline.remaining_minutes() - 1)))
        run("validate-finish", Deadline(2))
        report_progress(otestované=db.validation_counts())
        if ids or not queue:
            break


def _people(app, deadline: Deadline, run, ids: list[int] | None, report_progress) -> None:
    if ids:
        for hid in ids:  # výber na Bránu 0 (osobné údaje iba pre hypotézy, ktoré si zvolil)
            app.db.set_people_selection(hid, True)
    app.people_ids = ids
    # prepare-next (presný model): filter segmentu pre orgs a mapa rolí; people-rank na konci hľadá aj LinkedIn.
    run("prepare-next", deadline.sub(deadline.remaining_minutes() * 0.15))
    fast_min = deadline.remaining_minutes() * 0.65
    fast = deadline.sub(fast_min)
    # orgs najviac polovicu rýchlej fázy: akcia #19 minula celý čas na orgs a krok people (ľudia z webov
    # organizácií) sa preskočil – akcia potom nepridala nikoho nového.
    run("orgs", fast.sub(fast_min * 0.5))
    run("people", fast)
    run("people-rank", deadline.sub(deadline.remaining_minutes() * 0.7))
    run("contacts", deadline)  # e-mail a telefón ku každej vybranej osobe (bez modelu)


def _drafts(app, deadline: Deadline, run, ids, report_progress) -> None:
    app.people_ids = ids
    run("drafts", deadline)


def _contacts(app, deadline: Deadline, run, ids, report_progress) -> None:
    app.people_ids = ids
    run("contacts", deadline.sub(deadline.remaining_minutes() * 0.7))
    run("profiles", deadline)


def _evidence(app, deadline: Deadline, run, ids, report_progress) -> None:
    """Zásobník: posúdenie (≤ 60 % času) → embeddingy → zaradenie a zlúčenie → verdikty testov → správa."""
    accurate_min = max(0.0, deadline.remaining_minutes() - 3)
    run("verify", deadline.sub(accurate_min * 0.6))
    run("embed", deadline.sub(min(5.0, deadline.remaining_minutes())))
    run("cluster", deadline.sub(max(0.0, deadline.remaining_minutes() - 2)))
    run("validate-finish", Deadline(2))
    run("report", Deadline(5))


def _topics(app, deadline: Deadline, run, ids, report_progress) -> None:
    fast = deadline.sub(deadline.remaining_minutes() * 0.5)
    for stage in ("collect", "screen", "extract", "verify-code", "source-types"):
        run(stage, fast)
    run("embed", deadline.sub(min(10.0, deadline.remaining_minutes())))
    accurate_min = max(0.0, deadline.remaining_minutes() - 2)
    run("verify", deadline.sub(accurate_min * 0.6))
    run("cluster", deadline.sub(max(0.0, deadline.remaining_minutes() - 2)))
    run("report", Deadline(5))


def _critique(app, deadline: Deadline, run, ids, report_progress) -> None:
    run("critique", deadline.sub(deadline.remaining_minutes() * 0.7))
    run("market", deadline)


def _gate1(app, deadline: Deadline, run, ids, report_progress) -> None:
    app.gate_ids = ids
    run("gate1-fields", deadline.sub(deadline.remaining_minutes() * 0.15))
    fast = deadline.sub(deadline.remaining_minutes() * 0.6)
    for stage in ("gate1-sources", "gate1-check", "gate1-datasets"):
        run(stage, fast)
    run("gate1-repro", deadline.sub(max(0.0, deadline.remaining_minutes() - 1)))
    run("gate1-finish", Deadline(2))


def _gate2(app, deadline: Deadline, run, ids, report_progress) -> None:
    app.gate_ids = ids
    run("gate2-prereg", deadline.sub(deadline.remaining_minutes() * 0.5))
    run("gate2-backtest", deadline.sub(deadline.remaining_minutes() * 0.3))
    run("gate2-demo", deadline)


def _bench(app, deadline: Deadline, run, ids, report_progress) -> None:
    run("bench", deadline)


def _run_all(app, minutes: float, run_stage, report_progress, results: dict[str, Any]) -> None:
    from .pipeline import run_all

    def tracked(a, stage: str, dl: Deadline) -> dict:
        if dl.expired():  # krok sa nestihol – v súhrne úlohy musí byť vidieť, nie prázdne {}
            log.warning("Krok %s preskočený: vypršal čas", stage)
            results["stages"].append({"krok": stage, "preskočené": "vypršal čas"})
            return {}
        report_progress(krok=stage)
        t0 = awake_clock()
        stats = run_stage(a, stage, dl)
        results["stages"].append({"krok": stage, "trvanie_min": round((awake_clock() - t0) / 60, 1),
                                  **(_short(stats) or {})})
        return stats

    results["failed"] = run_all(app, minutes, tracked)


FLOWS: dict[str, Callable[..., None]] = {
    "scout": _scout, "validate": _validate, "people": _people, "drafts": _drafts, "contacts": _contacts,
    "evidence": _evidence, "topics": _topics, "critique": _critique, "gate1": _gate1, "gate2": _gate2, "bench": _bench,
    "themes": _themes,
}
