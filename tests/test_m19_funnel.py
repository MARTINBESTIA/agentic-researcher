"""M19 – lievik: jedna hypotéza po druhej od testu po Bránu 2, donekonečna (bez siete a modelu)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PeopleConfig, PrivacyConfig, WorkflowsConfig
from radar.db import DB
from radar.funnel import next_step, pick, signature
from radar.workflows import run_workflow

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def setup(tmp_path, lia=True, **workflows):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=lia), people=PeopleConfig(search_profiles=False),
                 workflows=WorkflowsConfig(**workflows))
    return cfg, DB(cfg.db_path)


def mark_tested(db, hid, verdict="podložená"):
    db.set_validation_searched(hid, {"agent": {"ok": True}})
    db.set_validation_result(hid, {"verdikt": verdict})


def contacts_done(db, hid):
    target = db.add_contact_target(hid, "customer", "vedúci nákupu", ["nákupca"], ["q"], "rozhovor", "r")
    db.update_contact_target(target, queries_json=[{"q": "q", "done_at": "2026-10-15T10:00:00+00:00"}])
    db.set_contacts_searched(hid, {"kolá": 1})


def test_next_step_follows_hypothesis_from_test_to_gate2(tmp_path):
    cfg, db = setup(tmp_path)
    hid = db.create_hypothesis("Meškanie dodávok", "", "výrobcovia")
    h = lambda: db.get_hypothesis(hid)  # noqa: E731
    assert next_step(cfg, db, h(), NOW) == ("test", "neotestovaná")
    db.set_validation_searched(hid, {})
    assert next_step(cfg, db, h(), NOW)[0] == "test"  # čaká na posúdenie tvrdení agenta
    mark_tested(db, hid)
    db.set_people_selection(hid, True, manual=False)
    assert next_step(cfg, db, h(), NOW) == ("contacts", "bez plánu kontaktov")
    contacts_done(db, hid)
    db.set_contacts_searched(hid, {"kolá": 2})
    cfg.people.contact_search_rounds = 1
    assert next_step(cfg, db, h(), NOW) == ("gate1", "Brána 1 nezačatá")
    db.set_gate_status(hid, 1, "needs_input")
    step, why = next_step(cfg, db, h(), NOW)
    assert step is None and "čaká na tvoje dáta" in why
    db.set_gate_status(hid, 1, "passed")
    exp = db.add_experiment(hid, None, None, "s", "m", "b", "r", {}, task="ranking")
    step, why = next_step(cfg, db, h(), NOW)
    assert step is None and "čakajú na tvoje uzamknutie" in why  # človek uzamyká predregistráciu
    db.update_experiment(exp, status="locked")
    assert next_step(cfg, db, h(), NOW) == ("gate2", "spätný test uzamknutých experimentov")
    db.update_experiment(exp, status="done")
    assert next_step(cfg, db, h(), NOW) == ("gate2", "záver experimentu")
    db.update_experiment(exp, conclusion_json={"text": "záver"})
    assert next_step(cfg, db, h(), NOW) == (None, "hotovo")


def test_failed_test_stops_and_contacts_skip_without_lia_or_outside_hours(tmp_path):
    cfg, db = setup(tmp_path, lia=False)
    weak = db.create_hypothesis("Slabá", "", "x")
    mark_tested(db, weak, "slabá")
    assert next_step(cfg, db, db.get_hypothesis(weak), NOW) == (None, "test: slabá")
    ok = db.create_hypothesis("Podložená", "", "x")
    mark_tested(db, ok)
    assert next_step(cfg, db, db.get_hypothesis(ok), NOW)[0] == "gate1"  # bez LIA sa kontakty preskočia
    cfg2, db2 = setup(tmp_path / "b", autopilot_hours={"people": [6, 7], "gate1": [6, 7]})
    hid = db2.create_hypothesis("H", "", "x")
    mark_tested(db2, hid)
    db2.set_people_selection(hid, True, manual=False)
    step, why = next_step(cfg2, db2, db2.get_hypothesis(hid), NOW)  # 12:00 UTC je mimo okna 6–7 h
    assert step is None and "mimo času" in why


def test_pick_prefers_focus_then_started_then_untested_then_find(tmp_path):
    cfg, db = setup(tmp_path, lia=False, funnel_find_every=0)
    new = db.create_hypothesis("Neotestovaná", "", "x")
    started = db.create_hypothesis("Prešla testom", "", "x")
    mark_tested(db, started)
    item = pick(cfg, db, NOW)
    assert (item["hypothesis_id"], item["step"]) == (started, "gate1")  # rozpracovaná skôr než neotestovaná
    db.set_gate_status(started, 1, "needs_input")
    item = pick(cfg, db, NOW)
    assert (item["hypothesis_id"], item["step"]) == (new, "test")
    row = db.start_funnel_step(new, "test", "x", None)
    db.finish_funnel_step(row, "verdikt: podložená", "gate1", "Brána 1 nezačatá", False)
    mark_tested(db, new)
    other = db.create_hypothesis("Ďalšia", "", "x")
    item = pick(cfg, db, NOW)
    assert (item["hypothesis_id"], item["step"]) == (new, "gate1") and item["reason"].startswith("pokračuje")
    db.set_gate_status(new, 1, "needs_input")
    mark_tested(db, other, "nepodložená")
    assert pick(cfg, db, NOW)["step"] == "find"  # niet čo testovať: hľadá nové


def test_find_every_n_tests_and_stuck_step_is_skipped(tmp_path):
    cfg, db = setup(tmp_path, lia=False, funnel_find_every=2, funnel_max_attempts=2)
    a = db.create_hypothesis("A", "", "x")
    b = db.create_hypothesis("B", "", "x")
    for _ in range(2):  # krok „test“ pre A dvakrát bez zmeny stavu
        sig = signature(db, db.get_hypothesis(a))
        row = db.start_funnel_step(a, "test", sig, None)
        db.finish_funnel_step(row, "test čaká", "test", "x", False)
    assert pick(cfg, db, NOW)["step"] == "find"  # po 2 testoch aj nové hypotézy
    row = db.start_funnel_step(None, "find", None, None)
    db.finish_funnel_step(row, "nové hypotézy 0", None, "nič nové", False)
    item = pick(cfg, db, NOW)
    assert (item["hypothesis_id"], item["step"]) == (b, "test")  # A je zaseknutá (2× rovnaký stav)


def fake_stages(db, hid, verdict="podložená", calls=None):
    def run_stage(app, stage, dl):
        calls.append(stage)
        if stage == "validate-search":
            db.set_validation_searched(hid, {"agent": {"ok": True}})
        if stage == "validate-finish":
            db.set_validation_result(hid, {"verdikt": verdict})
        return {}

    return run_stage


def test_funnel_job_runs_one_step_logs_it_and_queues_next(tmp_path, monkeypatch):
    cfg, db = setup(tmp_path, lia=False, funnel_find_every=0)
    hid = db.create_hypothesis("Meškanie dodávok", "", "x")
    monkeypatch.setattr("radar.jobs.start_next", lambda c, d: None)
    job = db.create_job("funnel", {"minutes": 0, "config": "config.yaml"})
    calls = []
    run_workflow(SimpleNamespace(cfg=cfg, db=db, router=None), "funnel", 0, job_id=job, run_stage=fake_stages(db, hid, calls=calls))
    assert calls == ["validate-search", "source-types", "embed", "validate-verify", "validate-cluster", "validate-finish"]
    log = db.funnel_log(5)[0]
    assert (log["hypothesis_id"], log["step"], log["outcome"], log["next_step"]) == (hid, "test", "verdikt: podložená", "gate1")
    queued = [j for j in db.queued_jobs() if j["kind"] == "funnel"]
    assert len(queued) == 1 and json.loads(queued[0]["args_json"])["chain"] is True
    assert json.loads(queued[0]["args_json"])["until"] is None  # 0 minút = donekonečna
    # ďalšia úloha pokračuje tou istou hypotézou (Brána 1)
    calls.clear()
    nxt = queued[0]["id"]
    db.update_job(nxt, status="running")
    run_workflow(SimpleNamespace(cfg=cfg, db=db, router=None), "funnel", 120, job_id=nxt, run_stage=fake_stages(db, hid, calls=calls))
    assert calls[0] == "gate1-fields" and calls[-1] == "gate1-finish"
    assert db.funnel_log(1)[0]["step"] == "gate1"


def test_funnel_chain_stops_after_time_or_repeated_failures(tmp_path, monkeypatch):
    cfg, db = setup(tmp_path, lia=False, funnel_find_every=0, funnel_max_failures=2)
    db.create_hypothesis("H", "", "x")
    monkeypatch.setattr("radar.jobs.start_next", lambda c, d: None)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    job = db.create_job("funnel", {"minutes": 120, "chain": True, "until": past})
    run_workflow(SimpleNamespace(cfg=cfg, db=db, router=None), "funnel", 120, job_id=job, run_stage=lambda a, s, dl: {})
    assert db.queued_jobs() == [] and db.funnel_log(5) == []  # čas vypršal: nič nespustí

    def broken(app, stage, dl):
        raise ConnectionError("model nebeží")

    for i in range(2):
        job = db.create_job("funnel", {"minutes": 120, "chain": True, "until": None})
        run_workflow(SimpleNamespace(cfg=cfg, db=db, router=None), "funnel", 120, job_id=job, run_stage=broken)
        queued = [j for j in db.queued_jobs() if j["kind"] == "funnel"]
        assert len(queued) == (1 if i == 0 else 0)  # po 2 zlyhaniach za sebou lievik zastaví
        for q in queued:
            db.update_job(q["id"], status="cancelled")
    assert db.funnel_log(1)[0]["failed"] == 1


def test_restart_keeps_chain_arguments(tmp_path):
    from radar.jobs import restart_job

    cfg, db = setup(tmp_path)
    job = db.create_job("funnel", {"minutes": 0, "chain": True, "until": None, "config": "config.yaml"})
    db.update_job(job, status="interrupted")
    spawned = []
    new = restart_job(cfg, db, "config.yaml", job, popen=lambda cmd, **kw: spawned.append(cmd) or SimpleNamespace(pid=1),
                      alive=lambda pid: False)
    args = json.loads(db.get_job(new)["args_json"])
    assert args["chain"] is True and args["minutes"] == 0 and "--minutes" in spawned[0]


def test_actions_page_shows_funnel_log_and_waiting_hypotheses(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from tests.test_m2_gui import APP, fixture_db

    cfg_path = fixture_db(tmp_path)
    monkeypatch.setenv("RADAR_CONFIG", str(cfg_path))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    from radar.config import load_config

    db = DB(load_config(cfg_path).db_path)
    hid = db.active_hypotheses()[0]["id"]
    row = db.start_funnel_step(hid, "gate1", "s", None)
    db.finish_funnel_step(row, "Brána 1: needs_input", None, "Brána 1 čaká na tvoje dáta (Brána 1 → Tvoje dáta)", False)
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/actions.py").run()
    assert not at.exception, at.exception
    tables = [t.value for t in at.dataframe]
    assert any("krok" in t.columns and "Brána 1 – dáta" in list(t["krok"]) for t in tables)
    assert any("čaká na" in t.columns for t in tables)
    assert any("Čaká na teba 1" in w.value for w in at.warning)


def test_invalid_experiment_plans_are_not_progress(tmp_path):
    """16. 10. 2026: návrh experimentov, ktorý pridá iba neplatný plán, menil odtlačok stavu – lievik ho zopakoval
    4× (limit behov), namiesto odloženia po 2 pokusoch bez zmeny."""
    cfg, db = setup(tmp_path)
    hid = db.create_hypothesis("Meškanie dodávok", "", "výrobcovia")
    db.set_gate_status(hid, 1, "passed")
    before = signature(db, db.get_hypothesis(hid))
    bad = db.add_experiment(hid, None, None, "s", "m", "b", "r", {}, task="ranking")
    db.update_experiment(bad, status="invalid")
    assert signature(db, db.get_hypothesis(hid)) == before
    db.add_experiment(hid, None, None, "s", "m", "b", "r", {}, task="ranking")  # platný návrh na uzamknutie je zmena
    assert signature(db, db.get_hypothesis(hid)) != before
