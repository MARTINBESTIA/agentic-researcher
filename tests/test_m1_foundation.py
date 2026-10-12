"""M1 – migrácie, roly modelov, provenance, orezaný výstup, maskovanie logov (bez modelu a siete)."""

import logging
import sqlite3

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PipelineConfig
from radar.db import DB, SCHEMA_VERSION
from radar.db.schema import SCHEMA_V1
from radar.extract import run_extract
from radar.llm import LLMTruncated
from radar.models import ModelRouter
from radar.privacy import MaskingFilter, mask_text
from radar.prompts import prompt_version
from radar.util import Deadline
from radar.verify import run_verify
from tests.fakes import Recorder, make_router

DOC = "Úvod. " * 50 + "\nMalé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty meškajú.\n" + "Záver. " * 50
QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty meškajú."


def v01_database(path) -> None:
    """Databáza vytvorená schémou v0.1 (bez user_version) s dátami."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT INTO documents (url, domain, title, source_kind, fetched_at, last_checked_at, content_hash, text) "
        "VALUES ('https://a.sk/x', 'a.sk', 'T', 'search', '2026-10-01', '2026-10-01', 'h', 'text')"
    )
    conn.execute(
        "INSERT INTO claims (document_id, chunk_index, problem, quote, status, created_at) "
        "VALUES (1, 0, 'Problém', 'citát', 'verified', '2026-10-01')"
    )
    conn.execute(
        "INSERT INTO hypotheses (title, status, project, created_at, updated_at) VALUES ('H', 'watch', 'p', '2026-10-01', '2026-10-01')"
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_v01_database_is_migrated_with_backup(tmp_path):
    path = tmp_path / "data" / "radar.sqlite"
    path.parent.mkdir()
    v01_database(path)
    db = DB(path)
    assert db.schema_version() == SCHEMA_VERSION == 5
    assert db.migration["backup"] is not None
    backups = list((tmp_path / "data" / "backup").glob("radar-v1-*.sqlite"))
    assert len(backups) == 1
    # dáta ostali a dostali nové stĺpce
    assert db.get_claim(1)["problem"] == "Problém" and db.get_claim(1)["det_status"] == "ok"
    h = db.get_hypothesis(1)
    assert h["origin"] == "manual" and h["people_enabled"] == 0 and h["gate0_status"] == "not_started"
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"organizations", "people", "contact_points", "outreach", "model_judgments", "review_queue", "labels", "jobs", "settings", "data_fields",
            "data_sources", "datasets", "reproductions", "info_requests", "gate_decisions", "experiments", "domain_types"} <= tables
    assert h["validation_state"] is None and h["validation_runs"] == 0  # v3: testovanie hypotéz
    db.close()
    # druhé spustenie nič nezmení
    db = DB(path)
    assert db.migration == {"from": 5, "to": 5, "backup": None}
    assert len(list((tmp_path / "data" / "backup").glob("*.sqlite"))) == 1
    db.close()


def test_new_database_has_latest_schema_without_backup(tmp_path):
    db = DB(tmp_path / "new.sqlite")
    assert db.schema_version() == 5
    assert db.migration["backup"] is None
    assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_without_models_section_everything_goes_to_llm(tmp_path):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"))
    router = ModelRouter(cfg, DB(cfg.db_path), legacy_client=object())
    for task in ("extract", "entail", "role_map", "draft"):
        route = router.route(task)
        assert (route.role, route.model, route.k) == ("llm", cfg.llm.model, 1)


def extraction_response(messages):
    return {"relevant": True, "claims": [
        {"problem": "Malé obce nemajú kapacitu na obstarávanie.", "affected_party": "malé obce", "claim_type": "pain", "quote": QUOTE},
    ]}


def balanced_setup(tmp_path):
    cfg = Config(
        db_path=str(tmp_path / "r.sqlite"),
        models=ModelsConfig(),
        embeddings=EmbeddingsConfig(enabled=False),
        pipeline=PipelineConfig(),
    )
    db = DB(cfg.db_path)
    db.upsert_document("https://obce.sk/analyza", "Analýza", "search", "Samosprávy", "q", "h1", DOC)
    rec = Recorder()
    router = make_router(cfg, db, rec)
    return cfg, db, rec, router


def test_balanced_profile_routes_extract_to_fast_and_entail_to_accurate(tmp_path):
    cfg, db, rec, router = balanced_setup(tmp_path)
    rec.responses = {
        "ExtractionResult": extraction_response,
        "Entailment": {"verdict": "supported", "reason": "Priamo uvedené."},
    }
    run_extract(cfg, db, router, Deadline(1))
    stats = run_verify(cfg, db, router, Deadline(1))
    assert stats == {"verified": 1}
    assert rec.models_for("ExtractionResult") == ["radar-qwen3.5-9b"]
    assert rec.models_for("Entailment") == ["radar-qwen3.6-27b"]  # generátor nie je overovateľ


def test_truncated_output_goes_to_review_and_run_continues(tmp_path):
    cfg, db, rec, router = balanced_setup(tmp_path)
    doc2, _ = db.upsert_document("https://b.sk/x", "B", "search", None, None, "h2", DOC)
    rec.responses = {"ExtractionResult": extraction_response}
    run_extract(cfg, db, router, Deadline(1))
    calls = {"n": 0}

    def entail(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMTruncated("orezané")
        return {"verdict": "supported", "reason": "ok"}

    rec.responses["Entailment"] = entail
    stats = run_verify(cfg, db, router, Deadline(1))
    assert stats == {"llm_failures": 1, "verified": 1}  # druhé tvrdenie sa spracovalo
    pending = db.conn.execute("SELECT id FROM claims WHERE status = 'pending'").fetchall()
    assert len(pending) == 1
    review = db.open_reviews("entail")
    assert [(r["object_id"], r["reason"]) for r in review] == [(pending[0]["id"], "truncated")]
    truncated = db.conn.execute("SELECT COUNT(*) FROM model_judgments WHERE truncated = 1").fetchone()[0]
    assert truncated == 1
    # tvrdenie čakajúce na človeka sa pri ďalšom behu znova nepýta
    assert run_verify(cfg, db, router, Deadline(1)) == {}


def test_every_model_call_is_recorded_with_prompt_version(tmp_path):
    cfg, db, rec, router = balanced_setup(tmp_path)
    rec.responses = {
        "ExtractionResult": extraction_response,
        "Entailment": {"verdict": "partial", "reason": "Zovšeobecňuje."},
    }
    run_extract(cfg, db, router, Deadline(1))
    run_verify(cfg, db, router, Deadline(1))
    rows = db.conn.execute("SELECT task, role, model, profile, prompt_version, verdict FROM model_judgments ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("extract", "fast", "radar-qwen3.5-9b", "balanced", prompt_version("extract"), "True"),
        ("entail", "accurate", "radar-qwen3.6-27b", "balanced", prompt_version("entail"), "partial"),
    ]
    assert len(rec.calls) == len(rows)
    assert db.conn.execute("SELECT status FROM claims").fetchone()[0] == "partial"


def test_profiles_override_roles(tmp_path):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), models=ModelsConfig())
    db = DB(cfg.db_path)
    rec = Recorder()
    assert make_router(cfg, db, rec, profile="fast").route("entail").role == "fast"
    assert make_router(cfg, db, rec, profile="accurate").route("extract").role == "accurate"
    route = make_router(cfg, db, rec).route("entail_gate")
    assert (route.role, route.k) == ("accurate", 3)


def test_config_merges_partial_task_overrides():
    m = ModelsConfig.model_validate({"tasks": {"draft": {"thinking": True}}, "roles": {"accurate": {"model": "gemma"}}})
    assert m.tasks["draft"].role == "accurate" and m.tasks["draft"].thinking is True
    assert m.tasks["extract"].role == "fast"  # ostatné úlohy ostali predvolené
    assert m.roles["accurate"].model == "gemma" and m.roles["fast"].model == "radar-qwen3.5-9b"


def test_log_does_not_contain_email_or_phone(tmp_path):
    log_file = tmp_path / "radar.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.addFilter(MaskingFilter())
    logger = logging.getLogger("radar.test.masking")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("Kontakt %s, tel. %s", "jana.novakova@obec-x.sk", "+421 905 123 456")
        logger.info("Volaj 02/5937 1111 alebo 0905123456; IČO 00397563")
        try:
            raise ValueError("chyba pre peter@firma.sk")
        except ValueError:
            logger.exception("Výnimka")
    finally:
        handler.close()
        logger.removeHandler(handler)
    text = log_file.read_text(encoding="utf-8")
    for secret in ("jana.novakova", "obec-x.sk", "905 123 456", "5937 1111", "0905123456", "peter@firma.sk"):
        assert secret not in text
    assert "IČO 00397563" in text  # IČO nie je osobný údaj, nemaskuje sa
    assert mask_text("bez kontaktov 2 890 obcí") == "bez kontaktov 2 890 obcí"
