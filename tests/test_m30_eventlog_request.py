"""M30 – žiadosť o event log (dodatok 16. 10. 2026, časť 6, scenáre 6 a 7): účel, definícia prípadu, minimálna
schéma, malý prvý krok, prínos a podmienky bezplatnej skúšky; zdôvodnenie, prečo práve event log; návrh správy na
ručné odoslanie; alternatívy, keď event log neexistuje. Offline (bez modelu – šablóna)."""

import json

import pytest

from radar.event_logs import (
    ALTERNATIVES, EventLogError, build_request, draft_request_message, event_log_fit, request_markdown, set_request_status,
)
from radar.outcomes import record_outcome
from radar.outreach import POLICY_TEXT, run_drafts, send_state
from radar.util import Deadline
from tests.test_m25_outreach_modes import _world


def _confirmed(tmp_path):
    w = _world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    run_drafts(cfg, db, router, Deadline(1), [hid])
    out = next(o for o in db.outreach_list(hid) if o["person_id"] == pid)
    db.update_outreach(out["id"], status="sent", sent_at="2026-10-15T09:00:00")
    record_outcome(cfg, db, outreach_id=out["id"], reply="relevant", problem_relevance="confirmed")
    return w, out


def test_request_has_purpose_case_schema_small_first_step_output_and_unknowns(tmp_path):
    (cfg, db, hid, pid, org_id, *_), out = _confirmed(tmp_path)
    fit, why = event_log_fit(db, hid)
    assert fit and "priebehu" in why
    rid = build_request(cfg, db, hid, org_id, "objednávke dielcov", outreach_id=out["id"])
    row = db.eventlog_requests(hid)[0]
    fields = {f["pole"]: f for f in json.loads(row["fields_json"])}
    assert {"case_id", "activity", "timestamp"} <= set(fields) and fields["case_id"]["požiadavka"] == "povinné"
    assert "due_at / planned_at" in fields  # hypotéza o meškaní: termín s definíciou
    assert "nie čas exportu" in fields["timestamp"]["význam"] and "nie meno" in fields["role"]["význam"]
    assert "20–50" in row["sample_scope"] and "nie minimum" in row["sample_scope"]
    assert row["intended_output"] == "prehľad odchýlok dodania"  # z návrhu prínosu
    assert any("históriu krokov" in u for u in json.loads(row["unknowns_json"]))
    md = request_markdown(db.eventlog_requests(hid)[0])
    assert "| case_id | povinné |" in md and "nič nemaže potichu" in md
    assert rid == row["id"]


def test_request_message_is_template_with_free_trial_terms_and_needs_manual_send(tmp_path):
    (cfg, db, hid, pid, org_id, *_), out = _confirmed(tmp_path)
    rid = build_request(cfg, db, hid, org_id, "objednávke dielcov", outreach_id=out["id"])
    oid = draft_request_message(cfg, db, rid)
    msg = db.get_outreach(oid)
    assert msg["mode"] == "data_request" and msg["person_id"] == pid and msg["status"] in ("draft", "needs_edit")
    assert "históriu krokov pri objednávke dielcov" in msg["body"] and POLICY_TEXT in msg["body"]
    assert "Mená pracovníkov ani obsah komunikácie" in msg["body"] and "anonymn" not in msg["body"].lower()
    assert msg["body"].count("?") == 1  # jedna hlavná otázka
    pkg = json.loads(msg["package_json"])
    assert pkg["žiadosť_event_log"] == rid and pkg["potreba"]["stav"] == "confirmed"
    assert send_state(cfg, db, msg)[0] in ("draft_ready", "review_pending")
    assert db.get_eventlog_request(rid)["outreach_id"] == oid


def test_public_holder_gets_adapted_return_without_business_terms(tmp_path):
    (cfg, db, hid, pid, org_id, rec, router, dh, ex, firma), out = _confirmed(tmp_path)
    data_out = next(o for o in db.outreach_list(hid) if o["person_id"] == dh)
    urad = data_out["org_id"]
    rid = build_request(cfg, db, hid, urad, "žiadosti", outreach_id=data_out["id"])
    msg = db.get_outreach(draft_request_message(cfg, db, rid))
    assert POLICY_TEXT not in msg["body"] and "Na oplátku pošlem stručný prehľad" in msg["body"]


def test_unfit_hypothesis_needs_explicit_reason_and_alternatives_when_log_missing(tmp_path):
    (cfg, db, hid, pid, org_id, *_), out = _confirmed(tmp_path)
    other = db.create_hypothesis("Vysoké ceny tonerov v kanceláriách", "Ceny sú vysoké.", "firmy")
    fit, why = event_log_fit(db, other)
    assert not fit and "iná vzorka" in why
    with pytest.raises(EventLogError):
        build_request(cfg, db, other, org_id)
    rid = build_request(cfg, db, other, org_id, "objednávke tonera", why="overiť, ako dlho trvá schválenie nákupu")
    assert db.get_eventlog_request(rid)["why_event_log"].startswith("overiť")
    set_request_status(db, rid, "not_available", alternative="walkthrough")
    row = db.get_eventlog_request(rid)
    assert row["status"] == "alternative" and row["alternative"] == "walkthrough" and "walkthrough" in ALTERNATIVES
    with pytest.raises(EventLogError):
        set_request_status(db, rid, "not_available", alternative="snapshot_as_history")


def test_second_step_asks_for_larger_export_only_after_agreement(tmp_path):
    (cfg, db, hid, pid, org_id, *_), out = _confirmed(tmp_path)
    rid = build_request(cfg, db, hid, org_id, "objednávke dielcov", outreach_id=out["id"], step=2)
    row = db.get_eventlog_request(rid)
    assert "po dohode väčší export" in row["sample_scope"] and "nie je reprezentatívna" in row["sample_scope"]
    msg = db.get_outreach(draft_request_message(cfg, db, rid))
    assert "Väčší export" in msg["subject"] and "Bolo by pre vás takéto rozšírenie zaujímavé?" in msg["body"]


def test_eventlog_page_lists_requests_and_fit(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.db import DB
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    org = db.outreach_list(1)[0]["org_id"]
    build_request(cfg, db, 1, org, "verejnej zákazke", why="overiť, ako dlho trvá príprava zákazky")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/eventlog.py").run()
    assert not at.exception, at.exception
    assert any("Pozor:" in w.value for w in at.warning)  # hypotéza nie je zjavne o priebehu
    assert any("krok 1" in e.label and "návrh" in e.label for e in at.expander)
    assert any("case_id" in m.value for m in at.markdown)
