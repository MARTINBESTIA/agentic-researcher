"""Príkazy v0.2 (Brána 0, objavovanie, eval, súkromie, GUI) a kroky pre run-all."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .util import Deadline

log = logging.getLogger("radar")


# ======================================================================= kroky (run_stage)
def stage_discover(app, deadline: Deadline) -> dict:
    from .discover import run_discover

    # discover_budget nastaví workflow „scout“ (tlačidlo v GUI); inak podiel z rozpočtu behu.
    return run_discover(app.cfg, app.db, app.searx, app.fetcher, deadline, budget=getattr(app, "discover_budget", None))


def stage_scout(app, deadline: Deadline) -> dict:
    """Agent hľadá nové problémy (bunky matice vzor bolesti × odvetvie)."""
    from .scout import run_scout

    return run_scout(app.cfg, app.db, app.router, app.searx, app.fetcher, deadline,
                     sessions=getattr(app, "scout_sessions", None))


def stage_validate_search(app, deadline: Deadline) -> dict:
    """Agent testuje ďalšie hypotézy z fronty (všetky uložené hypotézy postupne)."""
    from .validate import run_validate_search

    return run_validate_search(app.cfg, app.db, app.router, app.searx, app.fetcher, deadline,
                               limit=getattr(app, "validate_limit", None), ids=getattr(app, "validate_ids", None))


def stage_validate_verify(app, deadline: Deadline) -> dict:
    """Posúdenie iba tvrdení z testu hypotéz (nie celého zásobníka), aby verdikty prišli po minútach."""
    from .verify import run_verify

    ids = app.db.validation_claim_ids()
    return run_verify(app.cfg, app.db, app.verifier(), deadline, claim_ids=ids) if ids else {}


def stage_validate_cluster(app, deadline: Deadline) -> dict:
    from .cluster import run_cluster

    ids = set(app.db.validation_claim_ids())
    return run_cluster(app.cfg, app.db, app.router, deadline, app.embedder, claim_ids=ids) if ids else {}


def stage_validate_finish(app, deadline: Deadline) -> dict:
    from .validate import run_validate_finish

    return run_validate_finish(app.cfg, app.db, deadline)


def stage_source_types(app, deadline: Deadline) -> dict:
    """Typ vydavateľa zdrojov (pravidlo podľa domény, inak rýchly model raz na doménu)."""
    from .publishers import run_source_types

    return run_source_types(app.cfg, app.db, app.router, deadline)


def stage_themes(app, deadline: Deadline) -> dict:
    """Témy: zhluky podobných hypotéz z embeddingov (kód), názov tém rýchly model."""
    from .themes import run_themes

    return run_themes(app.cfg, app.db, app.router, app.embedder, deadline)


def stage_contacts(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    """E-mail a telefón ku konkrétnej osobe z webu jej organizácie alebo úradných domén (bez modelu)."""
    from .person_contacts import run_person_contacts

    return run_person_contacts(app.cfg, app.db, app.searx, app.fetcher, deadline, _ids(app, hypothesis_ids))


def _gate_ids(app) -> list[int] | None:
    return getattr(app, "gate_ids", None)


def stage_gate1(step: str) -> Callable[[Any, Deadline], dict]:
    """Kroky Brány 1: fields (presný), sources (agent), check a datasets (kód), repro (presný), finish (kód)."""

    def run(app, deadline: Deadline) -> dict:
        from . import gate1

        ids = _gate_ids(app)
        if step == "fields":
            return gate1.run_fields(app.cfg, app.db, app.router, deadline, ids)
        if step == "sources":
            return gate1.run_sources(app.cfg, app.db, app.router, app.searx, app.fetcher, deadline, ids)
        if step == "check":
            return gate1.run_check(app.cfg, app.db, app.fetcher, deadline, ids)
        if step == "datasets":
            return gate1.run_datasets(app.cfg, app.db, app.fetcher, deadline, ids)
        if step == "repro":
            return gate1.run_repro(app.cfg, app.db, app.router, deadline, ids)
        return gate1.run_finish(app.cfg, app.db, deadline, ids)

    return run


def stage_gate2(step: str) -> Callable[[Any, Deadline], dict]:
    """Kroky Brány 2: prereg (presný model), backtest (iba Python a DuckDB), demo (presný model, iba fakty)."""

    def run(app, deadline: Deadline) -> dict:
        from . import gate2

        ids = _gate_ids(app)
        if step == "prereg":
            return gate2.run_prereg(app.cfg, app.db, app.router, deadline, ids)
        if step == "backtest":
            return gate2.run_backtest(app.cfg, app.db, deadline, ids)
        return gate2.run_demo(app.cfg, app.db, app.router, deadline, ids)

    return run


def stage_bench(app, deadline: Deadline) -> dict:
    """Rýchlosť a schopnosti každej roly (JSON, nástroje) – na porovnanie modelov pred výberom v GUI."""
    from .agent import TOOLS
    from .bench import bench_role, save_bench

    out: dict[str, Any] = {}
    if app.router.legacy:
        return {"skipped": "bez sekcie models"}
    for role in app.router.models.roles:
        if deadline.expired():
            break
        r = bench_role(app.router, role, TOOLS if role in ("fast", "agent") else None)
        save_bench(app.db, r)
        out[role] = {"model": r.model, "gen_tps": r.gen_tps, "prompt_tps": r.prompt_tps, "json": r.json_ok,
                     "nástroje": r.tools_ok, "poznámka": (r.note or "")[:120]}
    return out


def stage_profiles(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    """URL profilov (LinkedIn) z výsledkov vyhľadávania pre vybraných ľudí; stránky sa nesťahujú."""
    from .people import run_profiles

    return run_profiles(app.cfg, app.db, app.searx, deadline, hypothesis_ids or getattr(app, "people_ids", None))


def _ids(app, hypothesis_ids: list[int] | None) -> list[int] | None:
    """Hypotézy pre kroky s ľuďmi: z príkazu, z workflow „people“ (GUI), inak všetky vybrané na Bránu 0."""
    return hypothesis_ids or getattr(app, "people_ids", None)


def stage_orgs(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    from .orgs import run_orgs

    return run_orgs(app.cfg, app.db, app.router, app.searx, app.fetcher, deadline, _ids(app, hypothesis_ids))


def stage_people(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    from .people import run_people_collect

    return run_people_collect(app.cfg, app.db, app.router, app.searx, app.fetcher, deadline, _ids(app, hypothesis_ids))


def stage_people_rank(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    from .people import run_people_rank

    return run_people_rank(app.cfg, app.db, app.router, app.searx, deadline, _ids(app, hypothesis_ids))


def stage_drafts(app, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    from .outreach import run_drafts

    return run_drafts(app.cfg, app.db, app.router, deadline, _ids(app, hypothesis_ids))


def stage_prepare_next(app, deadline: Deadline) -> dict:
    """Presný model pripraví filter segmentu a mapu rolí pre vybrané hypotézy, aby ďalší beh
    nemusel kvôli nim meniť model uprostred fázy rýchleho modelu."""
    from .orgs import OrgFinder
    from .people import role_map_for

    stats: Counter = Counter()
    finder = OrgFinder(app.cfg, app.db, app.router, app.searx, app.fetcher, None, None, deadline)
    for h in app.db.people_enabled_hypotheses():
        if deadline.expired():
            break
        if not h["segment_filter_json"] and finder.segment_filter(h) is not None:
            stats["segment_filters"] += 1
        if app.cfg.privacy.lia_completed and app.db.get_role_map(h["id"]) is None and role_map_for(app.cfg, app.db, app.router, h):
            stats["role_maps"] += 1
    return dict(stats)


def stage_purge(app, deadline: Deadline) -> dict:
    from .privacy import purge

    return purge(app.cfg, app.db)


def stage_embed(app, deadline: Deadline) -> dict:
    """Embeddingy (malý model na CPU) vopred pre nezaradené tvrdenia a hypotézy."""
    emb = app.embedder
    if emb is None:
        return {"skipped": "embeddingy vypnuté"}
    from .embed import claim_text, hypothesis_text

    claims = app.db.unassigned_evidence(limit=2000)
    hyps = app.db.assignable_hypotheses()
    try:
        emb.vectors("claim", [(c["id"], claim_text(c)) for c in claims])
        emb.vectors("hypothesis", [(h["id"], hypothesis_text(h)) for h in hyps])
    except Exception as exc:  # noqa: BLE001 – zoskupovanie potom použije textovú podobnosť
        log.warning("Embeddingy sa nepodarilo vypočítať: %s", exc)
        return {"error": str(exc)[:200]}
    return {"claims": len(claims), "hypotheses": len(hyps)}


EXTRA_STAGES: dict[str, Callable[[Any, Deadline], dict]] = {
    "discover": stage_discover,
    "scout": stage_scout,
    "validate-search": stage_validate_search,
    "validate-verify": stage_validate_verify,
    "validate-cluster": stage_validate_cluster,
    "validate-finish": stage_validate_finish,
    "profiles": stage_profiles,
    "contacts": stage_contacts,
    "source-types": stage_source_types,
    "themes": stage_themes,
    "gate1-fields": stage_gate1("fields"),
    "gate1-sources": stage_gate1("sources"),
    "gate1-check": stage_gate1("check"),
    "gate1-datasets": stage_gate1("datasets"),
    "gate1-repro": stage_gate1("repro"),
    "gate1-finish": stage_gate1("finish"),
    "gate2-prereg": stage_gate2("prereg"),
    "gate2-backtest": stage_gate2("backtest"),
    "gate2-demo": stage_gate2("demo"),
    "bench": stage_bench,
    "orgs": stage_orgs,
    "people": stage_people,
    "people-rank": stage_people_rank,
    "drafts": stage_drafts,
    "prepare-next": stage_prepare_next,
    "purge": stage_purge,
    "embed": stage_embed,
}
STAGE_HELP = {
    "scout": "Agent hľadá nové problémy na webe (rýchly model); tvrdenia overí kód a presný model.",
    "validate-search": "Agent testuje ďalšie uložené hypotézy z fronty: dôkazy za, proti a existujúce riešenia.",
    "validate-finish": "Verdikt testu hypotéz z overených tvrdení (kód, bez modelu).",
    "validate-verify": "Posúdenie iba tvrdení z testu hypotéz (presný model).",
    "validate-cluster": "Zaradenie iba tvrdení z testu hypotéz (presný model, bez zlučovania).",
    "profiles": "URL profilov LinkedIn z výsledkov vyhľadávania pre vybraných ľudí (nesťahuje sa).",
    "contacts": "E-mail a telefón ku konkrétnej osobe z webu jej organizácie alebo úradných domén (bez modelu).",
    "gate1-fields": "Brána 1: polia potrebné na riešenie problému (presný model).",
    "gate1-sources": "Brána 1: agent hľadá zdroje dát k poliam (licencia, frekvencia z prečítanej stránky).",
    "gate1-check": "Brána 1: dostupnosť zdrojov, odkazy na súbory a kvalita (kód).",
    "gate1-datasets": "Brána 1: stiahnutie, import do DuckDB, profil a pokrytie organizácií (kód).",
    "gate1-repro": "Brána 1: zreprodukovať známe číslo (model navrhne SQL, počíta DuckDB).",
    "gate1-finish": "Brána 1: žiadosti podľa 211/2000 (návrhy), pripravenosť dát a príznaky (kód).",
    "gate2-prereg": "Brána 2: návrh predregistrácie experimentu pre hypotézu × organizáciu (presný model).",
    "gate2-backtest": "Brána 2: spätný test uzamknutých experimentov (iba Python a DuckDB).",
    "gate2-demo": "Brána 2: demo pre organizáciu (HTML; text iba z vypočítaných faktov).",
    "bench": "Rýchlosť a schopnosti modelov každej roly (do Eval a výberu modelov).",
    "source-types": "Typ vydavateľa zdrojov: úrad, médiá, výskum, združenie, dodávateľ (dodávatelia sa nepočítajú do sily).",
    "embed": "Embeddingy pre nezaradené tvrdenia a hypotézy (CPU).",
    "themes": "Témy: zhluky podobných hypotéz podľa embeddingov (kód), názvy tém (rýchly model); hypotézy sa nezlučujú.",
    "people-rank": "Mapa rolí, väzby osoba–problém, priorita a výber ľudí (presný model).",
    "prepare-next": "Filter segmentu a mapa rolí pre vybrané hypotézy (presný model).",
    "purge": "Retencia: zmaže osobné údaje po lehotách z config.yaml a staré exporty.",
    "screen": "Objavovanie: posúdi začiatok nových dokumentov (rýchly model).",
    "verify-code": "Kontrola citátov a čísel kódom (bez modelu).",
}


def _run(app, stage: str, minutes: float | None, fn: Callable[[], dict]) -> int:
    run_id = app.db.start_run(stage)
    stats = fn()
    app.db.finish_run(run_id, stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _deadline(app, args) -> Deadline:
    return Deadline(getattr(args, "minutes", None) or app.cfg.pipeline.max_run_minutes)


def _hyp_ids(app, args) -> list[int] | None:
    if getattr(args, "hypothesis", None) is None:
        return None
    if app.db.get_hypothesis(args.hypothesis) is None:
        raise SystemExit(f"Hypotéza {args.hypothesis} neexistuje.")
    return [args.hypothesis]


# ======================================================================= príkazy
def cmd_discover(app, args) -> int:
    return _run(app, "discover", args.minutes, lambda: stage_discover(app, _deadline(app, args)))


def cmd_sources(app, args) -> int:
    from .discover import check_sources, load_sources

    if not args.check:
        for s in load_sources(app.cfg):
            print(f"[{'✓' if s.verified else ' '}] {s.category:<16} {s.name} – {s.url}")
        return 0
    for row in check_sources(app.cfg):
        print(f"[{'✓' if row['verified'] else ' '}] {row['category']:<16} HTTP {row['http']} · robots: {row['robots']:<22} {row['name']}")
    print("\nZdroj sa použije až po potvrdení: v sources.yaml nastav verified: true (najprv si web pozri).")
    return 0


def cmd_select(app, args) -> int:
    from .orgs import select_people_hypotheses

    if args.auto:
        ids = select_people_hypotheses(app.cfg, app.db)
        print("Hľadanie ľudí zapnuté pre: " + (", ".join(f"H{i}" for i in ids) or "žiadnu hypotézu"))
        return 0
    if args.hypothesis is None or args.people is None:
        print("Zadaj --hypothesis ID --people on|off, alebo --auto.")
        return 2
    hyp = app.db.get_hypothesis(args.hypothesis)
    if hyp is None:
        print(f"Hypotéza {args.hypothesis} neexistuje.")
        return 1
    app.db.set_people_selection(args.hypothesis, args.people == "on")
    print(f"H{args.hypothesis}: hľadanie ľudí {'zapnuté' if args.people == 'on' else 'vypnuté'} (ručná voľba).")
    return 0


def cmd_orgs(app, args) -> int:
    if args.import_csv:
        from .orgs import import_public_list
        from .registers import RPOClient

        if args.hypothesis is None:
            print("Import zoznamu potrebuje --hypothesis ID.")
            return 2
        rpo = RPOClient(app.db, app.cfg.people.registry_delay_s, app.cfg.people.registry_cache_days, app.cfg.fetch.user_agent)
        print(json.dumps(import_public_list(app.cfg, app.db, args.hypothesis, args.import_csv, rpo), ensure_ascii=False))
        return 0
    return _run(app, "orgs", args.minutes, lambda: stage_orgs(app, _deadline(app, args), _hyp_ids(app, args)))


def cmd_people(app, args) -> int:
    from .privacy import PrivacyGateError

    ids = _hyp_ids(app, args)
    try:
        code = _run(app, "orgs", args.minutes, lambda: stage_orgs(app, _deadline(app, args), ids))
        code |= _run(app, "people", args.minutes, lambda: stage_people(app, _deadline(app, args), ids))
        code |= _run(app, "people-rank", args.minutes, lambda: stage_people_rank(app, _deadline(app, args), ids))
    except PrivacyGateError as exc:
        print(exc)
        return 1
    return code


def cmd_drafts(app, args) -> int:
    from .privacy import PrivacyGateError

    try:
        return _run(app, "drafts", args.minutes, lambda: stage_drafts(app, _deadline(app, args), _hyp_ids(app, args)))
    except PrivacyGateError as exc:
        print(exc)
        return 1


def gui_command(cfg) -> list[str]:
    """Streamlit iba na 127.0.0.1 a bez telemetrie (R-GUI-1)."""
    app_path = Path(__file__).resolve().parent / "gui" / "app.py"
    return [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.address", "127.0.0.1",
        "--server.port", str(cfg.gui.port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]


def cmd_gui(app, args) -> int:
    cmd = gui_command(app.cfg)
    env = dict(os.environ, RADAR_CONFIG=str(Path(app.cfg.config_dir) / "config.yaml"))
    print(f"GUI: http://127.0.0.1:{app.cfg.gui.port} (ukončíš Ctrl+C)")
    app.db.close()
    return subprocess.call(cmd, env=env, cwd=app.cfg.config_dir)


def cmd_eval(app, args) -> int:
    from .evals import EVAL_TASKS, make_synthetic_cases, run_eval, sample_for_review, write_eval_report

    if args.task not in EVAL_TASKS:
        print(f"Úloha je jedna z: {', '.join(EVAL_TASKS)}")
        return 2
    if args.synthetic:
        print(f"Syntetické prípady: {make_synthetic_cases(app.db, args.task, args.synthetic)}")
    if args.sample:
        print(f"Do kontroly pridaných na označenie: {sample_for_review(app.db, args.task, args.sample)} (GUI → Kontrola)")
    if args.profile is None:
        return 0
    profiles = list(app.cfg.models.eval_profiles) if args.profile == "all" else [args.profile]
    results = run_eval(app.cfg, app.db, app.router, args.task, profiles, args.limit)
    path = write_eval_report(app.cfg, results)
    print(f"Eval: {path}\nOdporúčanie: {results['recommendation']}")
    return 0


def cmd_forget(app, args) -> int:
    from .privacy import forget

    if not (args.person or args.email or args.phone):
        print("Zadaj --person ID, --email X alebo --phone X.")
        return 2
    result = forget(app.cfg, app.db, person_id=args.person, email=args.email, phone=args.phone, reason=args.reason)
    print(f"Vymazané osoby: {result['people_removed']}, kontakty: {result['contacts_removed']}; identifikátory sú v zozname potlačenia.")
    return 0


def cmd_privacy(app, args) -> int:
    from .privacy import reapply, sender_placeholders

    if args.action == "reapply":
        print(json.dumps(reapply(app.cfg, app.db), ensure_ascii=False))
        return 0
    cfg = app.cfg
    print(f"LIA potvrdená: {'áno' if cfg.privacy.lia_completed else 'NIE (hľadanie ľudí a návrhy sú zablokované)'}")
    missing = sender_placeholders(cfg)
    print("Profil odosielateľa: " + ("doplnený" if not missing else "placeholdery " + ", ".join(missing) + " (schvaľovanie zablokované)"))
    print(f"Potlačenie: {app.db.suppression_counts() or 'prázdne'}")
    print(f"Údaje: {app.db.gate0_counts()}")
    return 0


def cmd_export(app, args) -> int:
    from .crm import export, export_person

    if args.person is not None:
        print(f"Údaje o osobe: {export_person(app.cfg, app.db, args.person)}")
        return 0
    if not args.what:
        print("Zadaj --what hypotheses|people|outreach|interviews alebo --person ID.")
        return 2
    if args.include_personal:
        print("POZOR: export obsahuje osobné údaje. Neposielaj ho ďalej; po 30 dňoch ho zmaže purge.")
    print(f"Export: {export(app.cfg, app.db, args.what, args.format, args.include_personal)}")
    return 0


def _id_list(value: str) -> list[int]:
    try:
        return [int(x.strip().lstrip("Hh")) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Zadaj ID hypotéz oddelené čiarkou, napr. 4,11,134.") from exc


TERMINAL_JOB = ("done", "failed", "stopped", "interrupted", "cancelled")


def cmd_job(app, args, sleep=None) -> int:
    """Akcia ako úloha Správcu akcií (rovnaké poradie ako tlačidlá v GUI): Plánovač úloh tak nezrazí nočný beh
    s akciou spustenou z GUI a v GUI je vidieť priebeh. --wait čaká na koniec (aj v poradí)."""
    import time

    from .jobs import JobBusy, refresh_jobs, start_job

    sleep = sleep or time.sleep
    config = Path(app.cfg.config_dir) / "config.yaml"
    try:
        job_id = start_job(app.cfg, app.db, config, args.kind, args.minutes, args.hypotheses, mode=args.mode)
    except JobBusy as exc:
        print(str(exc))
        return 3
    status = app.db.get_job(job_id)["status"]
    print(f"Úloha #{job_id} ({args.kind}): {'v poradí' if status == 'queued' else 'spustená'}")
    if not args.wait:
        return 0
    while True:
        refresh_jobs(app.db)
        job = app.db.get_job(job_id)
        if job["status"] in TERMINAL_JOB:
            print(f"Úloha #{job_id} skončila: {job['status']}")
            return 0 if job["status"] == "done" else 1
        sleep(30)


def cmd_workflow(app, args) -> int:
    from .workflows import run_workflow

    stats = run_workflow(app, args.kind, args.minutes, hypothesis_ids=args.hypotheses, job_id=args.job)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats.get("failed") else 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("workflow", help="Akcia zo Správcu akcií: scout (nové hypotézy), validate (test hypotéz), "
                                        "people (firmy, ľudia, kontakty), drafts, evidence, topics, critique, "
                                        "profiles, run-all.")
    from .workflows import WORKFLOWS

    p.add_argument("kind", choices=list(WORKFLOWS))
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--hypotheses", type=_id_list, default=None, help="ID hypotéz, napr. 4,11 (validate, people).")
    p.add_argument("--job", type=int, default=None, help=argparse.SUPPRESS)  # úloha z GUI (stav v tabuľke jobs)

    p = sub.add_parser("job", help="Spustí akciu ako úlohu Správcu akcií (viditeľná v GUI, s poradím); pre Plánovač úloh.")
    p.add_argument("kind", choices=list(WORKFLOWS))
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--hypotheses", type=_id_list, default=None, help="ID hypotéz, napr. 4,11.")
    p.add_argument("--mode", choices=["start", "queue", "replace"], default="queue",
                   help="queue (predvolené): ak niečo beží, zaradí sa do poradia; start: iba ak nič nebeží; "
                        "replace: bežiacu zastaví.")
    p.add_argument("--wait", action="store_true", help="Čaká na koniec úlohy (aj čas v poradí).")

    p = sub.add_parser("discover", help="Objavovanie nových problémov (katalóg zdrojov a matica dopytov).")
    p.add_argument("--minutes", type=float, default=None)

    p = sub.add_parser("sources", help="Katalóg zdrojov na objavovanie (--check overí dostupnosť a robots.txt).")
    p.add_argument("--check", action="store_true")

    p = sub.add_parser("select", help="Zapne alebo vypne hľadanie ľudí pre hypotézu (alebo --auto top 3).")
    p.add_argument("--hypothesis", type=int)
    p.add_argument("--people", choices=["on", "off"])
    p.add_argument("--auto", action="store_true", help="Automatický výber top N podľa sily dôkazov.")

    p = sub.add_parser("orgs", help="Organizácie k hypotéze (dôkazy, obstarávanie, segment; registre RPO a RÚZ).")
    p.add_argument("--hypothesis", type=int)
    p.add_argument("--import", dest="import_csv", help="CSV verejného zoznamu (name;ico;website).")
    p.add_argument("--minutes", type=float, default=None)

    p = sub.add_parser("people", help="Organizácie, ľudia, kontakty, väzby a priorita (vyžaduje LIA).")
    p.add_argument("--hypothesis", type=int)
    p.add_argument("--minutes", type=float, default=None)

    p = sub.add_parser("drafts", help="Návrhy oslovení (nič sa neodosiela).")
    p.add_argument("--hypothesis", type=int)
    p.add_argument("--minutes", type=float, default=None)

    sub.add_parser("gui", help="GUI v prehliadači na 127.0.0.1:8501.")

    p = sub.add_parser("eval", help="Porovnanie profilov modelov na položkách s gold labelom.")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", help="A, B, C, D alebo all.")
    p.add_argument("--limit", type=int)
    p.add_argument("--sample", type=int, help="Pridaj N náhodných posúdení do kontroly na ručné označenie.")
    p.add_argument("--synthetic", type=int, help="Vytvor N syntetických prípadov s nastrčenou chybou.")

    p = sub.add_parser("forget", help="Výmaz osoby a zápis do zoznamu potlačenia (námietka, žiadosť o výmaz).")
    p.add_argument("--person", type=int)
    p.add_argument("--email")
    p.add_argument("--phone")
    p.add_argument("--reason", default="erasure", choices=["erasure", "objection", "bounced", "manual"])

    p = sub.add_parser("privacy", help="Stav súkromia, alebo 'reapply' (potlačenie po obnove zo zálohy).")
    p.add_argument("action", nargs="?", default="status", choices=["status", "reapply"])

    p = sub.add_parser("export", help="Export do exports/ (predvolene pseudonymizovaný).")
    p.add_argument("--what", choices=["hypotheses", "people", "outreach", "interviews"])
    p.add_argument("--format", default="csv", choices=["csv", "xlsx"])
    p.add_argument("--include-personal", action="store_true")
    p.add_argument("--person", type=int, help="Všetky údaje o osobe (žiadosť o prístup).")


HANDLERS = {
    "workflow": cmd_workflow,
    "job": cmd_job,
    "discover": cmd_discover,
    "sources": cmd_sources,
    "select": cmd_select,
    "orgs": cmd_orgs,
    "people": cmd_people,
    "drafts": cmd_drafts,
    "gui": cmd_gui,
    "eval": cmd_eval,
    "forget": cmd_forget,
    "privacy": cmd_privacy,
    "export": cmd_export,
}
