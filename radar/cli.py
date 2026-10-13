"""Príkazový riadok: python -m radar <príkaz> (zoznam príkazov: python -m radar --help)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .agent import TOOLS, run_deep_dive
from .bench import bench_role, save_bench
from .cluster import run_cluster, sync_projects
from .collect import run_collect
from .config import Config, EmbeddingsConfig, load_config
from .critic import run_critique
from .db import DB, HYPOTHESIS_STATUSES
from .embed import Embedder
from .extract import run_extract, run_screen
from .fetch import Fetcher
from .llm import LLM
from .market import run_market
from .models import PROFILES, ModelRouter
from .privacy import install_log_masking
from .prompts import EXTRACT_SYSTEM
from .report import build_report, evidence_strength
from .search import SearxClient
from .util import LONG_COMMANDS, Deadline, preload_modules
from .verify import run_verify
from . import commands

log = logging.getLogger("radar")

PIPELINE = ["collect", "extract", "verify", "cluster", "critique", "market", "report"]


class App:
    """Klienty sa vytvárajú až pri použití, takže napr. `list` nepotrebuje bežiaci model."""

    def __init__(self, cfg: Config, profile: str | None = None):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.profile = profile
        self._llm: LLM | None = None
        self._router: ModelRouter | None = None
        self._searx: SearxClient | None = None
        self._fetcher: Fetcher | None = None
        self._embedder: Embedder | None = None
        self.collect_budget: int | None = None  # run-all: časť rozpočtu dokumentov po objavovaní

    @property
    def llm(self):
        """Model v0.1 (bez sekcie models); pri rolách klient roly fast (kontrola kontextu v doctor)."""
        if self._llm is None:
            self._llm = LLM(self.cfg.llm) if self.cfg.models is None else self.router.client("fast")
        return self._llm

    @property
    def router(self) -> ModelRouter:
        if self._router is None:
            if self.cfg.models is None:
                self._router = ModelRouter(self.cfg, self.db, legacy_client=self.llm)
            else:
                self._router = ModelRouter(self.cfg, self.db, profile=self.profile)
        return self._router

    @property
    def searx(self) -> SearxClient:
        if self._searx is None:
            self._searx = SearxClient(self.cfg.search, self.cfg.fetch.user_agent)
        return self._searx

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher(self.cfg.fetch, blocked_domains=self.cfg.blocked_domains())
        return self._fetcher

    def embeddings_config(self) -> EmbeddingsConfig:
        """Embeddingy: pri rolách model a server roly embed."""
        emb = self.cfg.embeddings
        m = self.cfg.models
        if m is None or "embed" not in m.roles:
            return emb
        role = m.roles["embed"]
        return emb.model_copy(update={"model": role.model, "base_url": emb.base_url or role.base_url or m.base_url})

    @property
    def embedder(self) -> Embedder | None:
        if self._embedder is None and self.cfg.embeddings.enabled:
            self._embedder = Embedder(self.embeddings_config(), self.cfg.llm, self.db)
        return self._embedder

    def verifier(self):
        return self.router if self.cfg.pipeline.verify_with_llm else None

    def verifier_llm(self):  # názov z v0.1
        return self.verifier()


def run_stage(app: App, stage: str, deadline: Deadline) -> dict:
    cfg, db = app.cfg, app.db
    run_id = db.start_run(stage)
    log.info("=== %s ===", stage)
    if stage == "collect":
        stats = run_collect(cfg, db, app.searx, app.fetcher, deadline, app.router, budget=app.collect_budget)
    elif stage in commands.EXTRA_STAGES:
        stats = commands.EXTRA_STAGES[stage](app, deadline)
    elif stage == "screen":
        stats = run_screen(cfg, db, app.router, deadline)
    elif stage == "extract":
        stats = run_extract(cfg, db, app.router, deadline)
    elif stage == "verify":
        stats = run_verify(cfg, db, app.verifier(), deadline)
    elif stage == "verify-code":
        stats = run_verify(cfg, db, app.verifier(), deadline, deterministic_only=True)
    elif stage == "cluster":
        stats = run_cluster(cfg, db, app.router, deadline, app.embedder)
    elif stage == "critique":
        stats = run_critique(cfg, db, app.router, app.searx, deadline)
    elif stage == "market":
        stats = run_market(cfg, db, app.router, app.searx, app.fetcher, deadline)
    elif stage == "report":
        stats = {"report": str(build_report(cfg, db, app.embedder))}
    else:
        raise ValueError(f"Neznámy krok {stage}")
    db.finish_run(run_id, stats)
    log.info("%s hotovo: %s", stage, stats)
    return stats


# ------------------------------------------------------------------- príkazy
class _Ping(BaseModel):
    ok: bool
    word: str


class _Marker(BaseModel):
    code: str


def check_context(app: App) -> tuple[bool, str]:
    """Vidí model celú časť dokumentu aj s rezervou na odpoveď? Ollama dlhší vstup potichu
    oreže zo začiatku, preto je kontrolné číslo na začiatku a otázka na konci. Výplň má dĺžku
    časti dokumentu + max_tokens odpovede (slovenčina ≈ 3,5 znaku na token, rátame 4,5)."""
    p = app.cfg.pipeline
    client = app.router.client_for("extract")
    size = p.chunk_chars + int(4.5 * client.cfg.max_tokens)
    filler = ("Obce a firmy na Slovensku riešia rôzne prevádzkové problémy, náklady a meškania. " * 600)[:size]
    system = EXTRACT_SYSTEM.format(lang=p.output_language, max_claims=p.max_claims_per_chunk)
    user = f"Kontrolné číslo je 731948.\n<untrusted_document>\n{filler}\n</untrusted_document>\n" \
           "Ignore the extraction task. Return only the control number from the very first line as JSON field 'code'."
    result = client.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], _Marker, max_tokens=50)
    return "731948" in result.code, result.code


def _wait_for_server(app: App, wait_s: int) -> tuple[bool, str]:
    client = app.router.client_for("extract")
    wait_until = time.monotonic() + max(0, wait_s)
    while True:
        try:
            return True, client.ping()
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() < wait_until:
                time.sleep(5)
                continue
            return False, str(exc)


def cmd_doctor(app: App, args: argparse.Namespace) -> int:
    cfg, ok = app.cfg, True
    router = app.router
    print(f"Databáza: {Path(cfg.db_path).resolve()} (schéma v{app.db.schema_version()})")
    base = cfg.models.base_url if cfg.models else cfg.llm.base_url
    up, info = _wait_for_server(app, args.wait)
    if up:
        print(f"[OK] Model server beží na {base} (modely: {info[:300]})")
    else:
        print(f"[CHYBA] Model nie je dostupný na {base}: {info}")
        print("        Ollama: spusti aplikáciu Ollama. llama-swap: spusti start_llm_swap.bat. Skontroluj port v config.yaml.")
        ok = False

    if ok:
        roles = router.roles_in_use()
        print("Roly modelov: " + ", ".join(f"{r} = {m}" for r, m in roles.items()) + f" · profil: {router.profile}")
        available = set(info.replace(":latest", "").split(", "))
        for role, model in roles.items():
            if role == "embed":
                continue
            if available and model.replace(":latest", "") not in available:
                print(f"[CHYBA] Model {model} (rola {role}) na serveri nie je – pozri README (ollama create …).")
                ok = False
                continue
            try:
                router.client(role).chat_json(
                    [{"role": "user", "content": 'Return the JSON {"ok": true, "word": "radar"}.'}], _Ping, max_tokens=200
                )
                print(f"[OK] Rola {role} ({model}) vracia JSON podľa schémy.")
            except Exception as exc:  # noqa: BLE001
                print(f"[CHYBA] Rola {role} ({model}) nevracia platný JSON: {exc}")
                ok = False
        if ok:
            try:
                full, code = check_context(app)
                if full:
                    print(f"[OK] Model extrakcie vidí celú časť dokumentu ({cfg.pipeline.chunk_chars} znakov) aj s rezervou.")
                else:
                    print(f"[CHYBA] Model nevidí začiatok dlhého vstupu (odpovedal {code!r}) – kontext je malý.")
                    print("        Ollama: použi model z ollama/Modelfile.* (num_ctx 16384), alebo zmenši pipeline.chunk_chars.")
                    ok = False
            except Exception as exc:  # noqa: BLE001
                print(f"[POZOR] Test kontextu zlyhal: {exc}")
            try:
                msg = router.client_for("deep_dive").chat_tools(
                    [{"role": "user", "content": "Call the finish tool with the summary 'test'."}], [TOOLS[-1]]
                )
                if getattr(msg, "tool_calls", None):
                    print("[OK] Volanie nástrojov funguje (potrebné pre deep-dive).")
                else:
                    print("[POZOR] Model nevolá nástroje (potrebné pre deep-dive). llama-server spusti s prepínačom --jinja.")
            except Exception as exc:  # noqa: BLE001
                print(f"[POZOR] Test volania nástrojov zlyhal: {exc}")

    if cfg.embeddings.enabled:
        try:
            dim = app.embedder.ping()
            print(f"[OK] Embeddingy {app.embeddings_config().model} fungujú (rozmer {dim}).")
        except Exception as exc:  # noqa: BLE001
            print(f"[POZOR] Embeddingy nefungujú ({exc}); zoskupovanie použije textovú podobnosť.")
            print("        Ollama: ollama pull qwen3-embedding:0.6b a potom")
            print("        ollama create radar-qwen3-embedding -f ollama/Modelfile.qwen3-embedding")

    if ok and args.bench:
        print("\nBenchmark rolí (model sa pred meraním uvoľní, takže sa meria aj načítanie):")
        roles = ["fast", "accurate", "embed"] if cfg.models else ["llm"]
        for role in roles:
            if cfg.models and role not in cfg.models.roles:
                continue
            result = bench_role(router, role, tools=[TOOLS[-1]] if role != "embed" else None)
            save_bench(app.db, result)
            if role == "embed":
                print(f"  {role:<9} {result.model:<26} {result.note}")
                continue
            fmt = lambda x, d=1: "–" if x is None else f"{x:.{d}f}"  # noqa: E731
            print(
                f"  {role:<9} {result.model:<26} načítanie {fmt(result.load_s)} s · prompt {fmt(result.prompt_tps, 0)} tok/s · "
                f"generovanie {fmt(result.gen_tps)} tok/s · JSON {'áno' if result.json_ok else 'nie'} · "
                f"nástroje {'áno' if result.tools_ok else 'nie'} {result.note}"
            )
    elif ok and cfg.models is None:  # v0.1: rýchla kontrola rýchlosti jedného modelu
        try:
            speed = app.llm.generation_speed()
            label = "[OK]" if speed >= 15 else "[POZOR]"
            print(f"{label} Rýchlosť generovania {speed:.1f} tokenov/s.")
        except Exception as exc:  # noqa: BLE001
            print(f"[POZOR] Meranie rýchlosti zlyhalo: {exc}")

    try:
        results = app.searx.search("obce Slovensko", limit=3)
        if results:
            print(f"[OK] SearXNG odpovedá ({len(results)} výsledky).")
        else:
            print("[POZOR] SearXNG beží, ale nevrátil výsledky – vyhľadávače mohli dočasne obmedziť požiadavky.")
    except Exception as exc:  # noqa: BLE001
        print(f"[CHYBA] SearXNG nie je dostupný na {cfg.search.searxng_url}: {exc}")
        print("        V priečinku projektu spusti: docker compose up -d")
        ok = False

    for project in cfg.projects:
        if project.path and not Path(project.path).exists():
            print(f"[POZOR] Projekt {project.name}: priečinok {project.path} neexistuje.")
    return 0 if ok else 1


def cmd_run_all(app: App, args: argparse.Namespace) -> int:
    from .pipeline import run_all

    from .util import keep_awake

    with keep_awake():  # nočný beh z Plánovača: počítač sa počas behu neuspí pre nečinnosť
        failed = run_all(app, args.minutes or app.cfg.pipeline.max_run_minutes, run_stage)
    print(f"Správa: {Path(app.cfg.reports_dir) / 'latest.md'}")
    return 1 if failed else 0


def cmd_stage(app: App, args: argparse.Namespace) -> int:
    if getattr(args, "hypotheses", None):
        app.gate_ids = args.hypotheses
    stats = run_stage(app, args.command, Deadline(args.minutes or app.cfg.pipeline.max_run_minutes))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_migrate(app: App, args: argparse.Namespace) -> int:
    m = app.db.migration or {}
    if m.get("backup"):
        print(f"Databáza zmigrovaná z verzie {m['from']} na {m['to']}; záloha: {m['backup']}")
    else:
        print(f"Databáza je aktuálna (verzia {app.db.schema_version()}).")
    return 0


def cmd_deep_dive(app: App, args: argparse.Namespace) -> int:
    summary, claim_ids = run_deep_dive(
        app.cfg, app.db, app.router, app.searx, app.fetcher,
        hypothesis_id=args.hypothesis, question=args.question, max_steps=args.steps,
    )
    stats = run_verify(app.cfg, app.db, app.verifier(), Deadline(60), claim_ids=claim_ids) if claim_ids else {}
    if args.hypothesis is not None:
        for claim_id in claim_ids:
            if app.db.get_claim(claim_id)["status"] in ("verified", "partial"):
                app.db.assign_claim(args.hypothesis, claim_id)
    print(summary)
    print(f"\nNové dôkazy: {len(claim_ids)} · výsledok overenia: {stats or 'žiadne'}")
    return 0


def cmd_note(app: App, args: argparse.Namespace) -> int:
    if app.db.get_hypothesis(args.hypothesis) is None:
        print(f"Hypotéza {args.hypothesis} neexistuje.")
        return 1
    app.db.add_note(args.hypothesis, args.text, source="human")
    print("Poznámka uložená.")
    return 0


def cmd_status(app: App, args: argparse.Namespace) -> int:
    if app.db.get_hypothesis(args.hypothesis) is None:
        print(f"Hypotéza {args.hypothesis} neexistuje.")
        return 1
    app.db.set_hypothesis_status(args.hypothesis, args.new_status)
    print(f"H{args.hypothesis} -> {args.new_status}")
    return 0


def cmd_merge(app: App, args: argparse.Namespace) -> int:
    try:
        moved = app.db.merge_hypotheses(args.source, args.target)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"H{args.source} zlúčená do H{args.target} ({moved} dôkazov); H{args.source} má stav rejected.")
    return 0


def cmd_list(app: App, args: argparse.Namespace) -> int:
    sync_projects(app.cfg, app.db)
    rows = app.db.all_hypotheses()
    if not rows:
        print("Zatiaľ žiadne hypotézy.")
        return 0
    for h in rows:
        label, n_ver, _, n_dom = evidence_strength(app.db.evidence_for(h["id"]))
        project = f"[{h['project']}] " if h["project"] else ""
        people = " [ľudia]" if h["people_enabled"] else ""
        print(f"H{h['id']:<4} [{h['status']:<12}] {label:<9} overené: {n_ver:>2} · domény: {n_dom:>2}  {project}{h['title']}{people}")
    return 0


def _local(ts: str | None) -> str:
    """Čas z databázy (UTC) v miestnom čase, napr. 21:24."""
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M") if ts else ""


def cmd_progress(app: App, args: argparse.Namespace) -> int:
    """Stav behu: posledné kroky, počty a najsilnejšie hypotézy. Dá sa spustiť aj počas behu."""
    db = app.db
    print("Posledné kroky:")
    runs = db.recent_runs(args.runs)
    for i, r in enumerate(runs):
        if not r["finished_at"] and i < len(runs) - 1:  # po ňom už začal ďalší krok
            print(f"  {r['stage']:<9} {_local(r['started_at'])}–       prerušené")
        elif r["finished_at"]:
            stats = json.loads(r["stats_json"] or "{}")
            stats.pop("report", None)
            summary = ", ".join(f"{k} {v}" for k, v in stats.items()) or "–"
            print(f"  {r['stage']:<9} {_local(r['started_at'])}–{_local(r['finished_at'])}  hotovo   {summary}")
        else:
            minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(r["started_at"])).total_seconds() / 60
            print(f"  {r['stage']:<9} {_local(r['started_at'])}–       BEŽÍ ({minutes:.0f} min)")
    c = db.counts()
    print(
        f"\nDokumenty: {c['documents']} · tvrdenia overené {c['verified']}, čiastočne {c['partial']}, "
        f"zamietnuté {c['rejected']}, neoverené {c['pending']}, prebratý text {c['duplicates']}"
    )
    print(f"Aktívne hypotézy: {c['active_hypotheses']} · kandidáti: {c['candidate_hypotheses']} · "
          f"tvrdenia čakajúce na zaradenie: {db.unassigned_count()}")
    reviews = db.review_counts()
    if reviews:
        print("Čakajúce kontroly: " + ", ".join(f"{t} {n}" for t, n in reviews.items()))
    ranked = []
    for h in db.active_hypotheses():
        label, n_ver, _, n_dom = evidence_strength(db.evidence_for(h["id"]))
        ranked.append((n_ver, n_dom, label, h))
    ranked.sort(key=lambda x: (x[0] >= 4 and x[1] >= 3, x[0] >= 2 and x[1] >= 2, x[1], x[0]), reverse=True)
    print(f"\nNajsilnejšie hypotézy (top {args.top}):")
    for n_ver, n_dom, label, h in ranked[: args.top]:
        project = f"[{h['project']}] " if h["project"] else ""
        print(f"  H{h['id']:<4} {label:<9} overené {n_ver:>2} · domény {n_dom}  {project}{h['title'][:90]}")
    return 0


# ------------------------------------------------------------------- parser
EXTRA_CLI_STAGES = ["screen", "verify-code", "embed", "people-rank", "prepare-next", "purge", "scout",
                    "validate-search", "validate-verify", "validate-cluster", "validate-finish", "profiles", "contacts",
                    "gate1-fields", "gate1-sources", "gate1-check", "gate1-datasets", "gate1-repro", "gate1-user",
                    "gate1-finish",
                    "gate2-prereg", "gate2-backtest", "gate2-demo", "bench", "source-types", "themes"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m radar", description="Problem Radar – lokálny výskum podnikateľských problémov.")
    parser.add_argument("--config", default=None, help="Cesta ku konfigurácii (predvolene config.yaml vedľa balíka radar).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Podrobný log.")
    parser.add_argument("--profile", choices=PROFILES, default=None, help="Profil modelov (predvolene z config.yaml).")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Skontroluje modely, kontext, embeddingy a SearXNG.")
    doctor.add_argument("--wait", type=int, default=0, help="Koľko sekúnd čakať, kým sa model načíta.")
    doctor.add_argument("--bench", action="store_true", help="Zmeria načítanie a rýchlosť každej roly (uloží do DB).")

    sub.add_parser("migrate", help="Migrácia schémy databázy (spúšťa sa aj automaticky, vždy so zálohou).")

    run_all = sub.add_parser("run-all", help="Celý beh a správa.")
    run_all.add_argument("--minutes", type=float, default=None, help="Časový limit behu.")

    for stage in PIPELINE + EXTRA_CLI_STAGES:
        stage_parser = sub.add_parser(stage, help=commands.STAGE_HELP.get(stage, f"Iba krok '{stage}'."))
        stage_parser.add_argument("--minutes", type=float, default=None)
        if stage.startswith(("gate1-", "gate2-")):  # bez zadania najviac gate1.max_hypotheses_per_run hypotéz
            stage_parser.add_argument("--hypotheses", type=commands._id_list, default=None, help="ID hypotéz, napr. 11,4.")
    commands.add_parsers(sub)

    deep = sub.add_parser("deep-dive", help="Výskumný agent pre jednu hypotézu alebo otázku.")
    target = deep.add_mutually_exclusive_group(required=True)
    target.add_argument("--hypothesis", type=int, help="ID hypotézy (napr. 3 pre H3).")
    target.add_argument("--question", help="Voľná výskumná otázka.")
    deep.add_argument("--steps", type=int, default=None, help="Max. počet krokov agenta.")

    note = sub.add_parser("note", help="Tvoja poznámka k hypotéze (napr. výsledok rozhovoru).")
    note.add_argument("--hypothesis", type=int, required=True)
    note.add_argument("text")

    status = sub.add_parser("status", help="Zmení stav hypotézy.")
    status.add_argument("--hypothesis", type=int, required=True)
    status.add_argument("--set", dest="new_status", choices=HYPOTHESIS_STATUSES, required=True)

    merge = sub.add_parser("merge", help="Zlúči dve hypotézy, ktoré opisujú ten istý problém.")
    merge.add_argument("--from", dest="source", type=int, required=True, help="Hypotéza, ktorá zanikne.")
    merge.add_argument("--into", dest="target", type=int, required=True, help="Hypotéza, ktorá zostane.")

    sub.add_parser("list", help="Prehľad hypotéz a sily dôkazov.")

    progress = sub.add_parser("progress", help="Stav behu: kroky, počty a najsilnejšie hypotézy (aj počas behu).")
    progress.add_argument("--top", type=int, default=10, help="Koľko hypotéz ukázať.")
    progress.add_argument("--runs", type=int, default=7, help="Koľko posledných krokov ukázať.")
    return parser


def setup_logging(logs_dir: str, verbose: bool) -> None:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(Path(logs_dir) / "radar.log", encoding="utf-8")],
    )
    install_log_masking()  # e-maily a telefóny sa do logu nedostanú (R-PRIV-7)
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "pypdf", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)  # hlási aj bežné neparsovateľné stránky
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # varovania o fontoch v PDF nie sú podstatné


def default_config_path() -> Path:
    local = Path("config.yaml")
    return local if local.exists() else Path(__file__).resolve().parent.parent / "config.yaml"


HANDLERS = {
    "doctor": cmd_doctor,
    "migrate": cmd_migrate,
    "run-all": cmd_run_all,
    "deep-dive": cmd_deep_dive,
    "note": cmd_note,
    "status": cmd_status,
    "merge": cmd_merge,
    "list": cmd_list,
    "progress": cmd_progress,
    **{stage: cmd_stage for stage in PIPELINE + EXTRA_CLI_STAGES},
    **commands.HANDLERS,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # konzola vo Windows: UTF-8, nepoznané znaky nahradiť
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if args.command in LONG_COMMANDS:
        preload_modules()  # celý kód z chvíle štartu (úpravy počas behu sa do neho nedostanú)
    config_path = Path(args.config) if args.config else default_config_path()
    if not config_path.exists():
        print(f"Konfigurácia {config_path} neexistuje.")
        return 2
    cfg = load_config(config_path)
    setup_logging(cfg.logs_dir, args.verbose)
    app = App(cfg, profile=args.profile)
    try:
        return HANDLERS[args.command](app, args)
    finally:
        app.db.close()
