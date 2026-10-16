"""M27 – výsledky oslovení a učenie z odpovedí (audit 15. 10. 2026, balík E): zápis výsledku človekom, stav
oslovenia, stav potreby návrhov prínosu, aktualizácia prehľadu domény s históriou a pseudonymizovanou poznámkou,
metriky po unikátnych organizáciách, výmaz pri forget a námietke. Offline (falošný model)."""

import json

import pytest

from radar.outcomes import OutcomeError, need_summary, outcome_metrics, record_outcome
from radar.outreach import need_confirmed, run_drafts, set_status
from radar.privacy import forget
from radar.util import Deadline
from radar.value import domain_of, ensure_brief
from tests.test_m24_value import brief_response, claim
from tests.test_m25_outreach_modes import _world


def _sent(tmp_path):
    w = _world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    run_drafts(cfg, db, router, Deadline(1), [hid])
    for o in db.outreach_list(hid):
        if o["status"] == "draft":
            db.update_outreach(o["id"], status="sent", sent_at="2026-10-14T09:00:00")
    return w


def _of(db, hid, pid=None, org=None):
    return next(o for o in db.outreach_list(hid) if (pid and o["person_id"] == pid) or (org and o["org_id"] == org
                                                                                          and not o["person_id"]))


def test_outcome_updates_outreach_status_need_and_triggers_brief_update_with_history(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _sent(tmp_path)
    c1 = claim(db, hid, "Dodávky meškajú.", "Dodávky dielcov meškajú o týždne.")
    c2 = claim(db, hid, "Plánovanie trpí.", "Plánovači ručne urgujú dodávateľov každý deň.")
    prompts = []

    def brief(messages):
        prompts.append(messages[-1]["content"])
        return brief_response(c1, c2)

    rec.responses["DomainBriefOut"] = brief
    rec.responses["ReviewOut"] = {"verdict": "ok", "problems": []}
    first = ensure_brief(cfg, db, router, hid)
    person = db.get_person(pid)
    cust = _of(db, hid, pid=pid)
    res = record_outcome(cfg, db, outreach_id=cust["id"], reply="relevant", problem_relevance="confirmed",
                         interview_agreed=True, note=f"{person['full_name']} potvrdil urgencie, píšte na x@y.sk",
                         next_action="rozhovor v utorok", prep_minutes=25)
    assert res["stav_oslovenia"] == "scheduled" and res["potreba"] == {"stav": "confirmed", "potvrdili": 1, "vyvrátili": 0}
    assert db.get_outreach(cust["id"])["status"] == "scheduled"
    assert all(p["need_status"] == "confirmed" for p in db.value_proposals_for(hid, "customer"))
    assert need_confirmed(db, hid, pid, org_id) and not need_confirmed(db, hid, None, firma)  # iná organizácia nie
    assert "x@y.sk" not in (db.outcomes_for(hid)[0]["note"] or "")  # maskované už pri uložení
    assert res["prehľad_domény"]

    second = ensure_brief(cfg, db, router, hid)
    history = db.brief_history(domain_of(db, hid)[0])
    assert second != first and [b["status"] for b in history] == ["ok", "superseded"]
    assert history[0]["reason"].startswith("odpoveď kontaktu")
    assert "confirmed" in prompts[-1] and person["full_name"] not in prompts[-1]  # do promptu bez mena


def test_metrics_count_unique_organizations_not_messages(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _sent(tmp_path)
    second = db.insert_person(org_id, "Jozef Druhý", "jozef druhy", "https://alfa.sk/t", "Jozef Druhý, nákupca", hid,
                              role_title="nákupca", source_kind="website")
    out2 = db.insert_outreach(second, hid, "email", "A", "x", "text", [], {"ok": True}, "sent")
    db.update_outreach(out2, sent_at="2026-10-14T09:00:00")
    record_outcome(cfg, db, outreach_id=_of(db, hid, pid=pid)["id"], reply="relevant", problem_relevance="confirmed",
                   prep_minutes=20)
    record_outcome(cfg, db, outreach_id=out2, reply="relevant", sample_obtained=True, prep_minutes=40, delivery_days=3)
    record_outcome(cfg, db, outreach_id=_of(db, hid, org=firma)["id"], reply="declined", rejection_reason="nemáme čas")
    m = outcome_metrics(db, [hid])
    rows = {r["metrika"]: r["organizácie"] for r in m["riadky"]}
    assert rows["relevantná odpoveď"] == 1 and rows["potvrdený problém"] == 1 and rows["vzorka dát"] == 1
    assert m["oslovené"] == 4  # zákazník (2 osoby jednej organizácie = 1), držiteľ dát, expert, firma cez funkčný kontakt
    assert m["príprava_min_medián"] == 30 and m["dodanie_dni_medián"] == 3
    assert {r["režim"] for r in m["režimy"]} >= {"discovery", "data_request", "expert_review"}
    assert db.get_outreach(_of(db, hid, org=firma)["id"])["status"] == "declined"


def test_refuted_and_mixed_need_status_and_validation(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _sent(tmp_path)
    record_outcome(cfg, db, outreach_id=_of(db, hid, org=firma)["id"], reply="not_relevant", problem_relevance="refuted")
    assert need_summary(db, hid, "customer")["stav"] == "refuted"
    record_outcome(cfg, db, outreach_id=_of(db, hid, pid=pid)["id"], reply="relevant", problem_relevance="confirmed")
    assert need_summary(db, hid, "customer") == {"stav": "mixed", "potvrdili": 1, "vyvrátili": 1}
    assert need_summary(db, hid, "data_holder")["stav"] == "hypothesis"  # iný typ adresáta sa nemieša
    with pytest.raises(OutcomeError):
        record_outcome(cfg, db, outreach_id=_of(db, hid, pid=pid)["id"], reply="možno")
    with pytest.raises(OutcomeError):
        record_outcome(cfg, db, outreach_id=_of(db, hid, pid=pid)["id"], prep_minutes=-5)
    with pytest.raises(OutcomeError):
        record_outcome(cfg, db, hid=hid)  # bez osoby aj organizácie


def test_forget_and_objection_remove_outcomes(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _sent(tmp_path)
    record_outcome(cfg, db, outreach_id=_of(db, hid, pid=pid)["id"], reply="relevant", note="poznámka")
    mailbox = _of(db, hid, org=firma)["id"]
    record_outcome(cfg, db, outreach_id=mailbox, reply="relevant", note="info schránka odpovedala")
    forget(cfg, db, person_id=pid)
    assert all(o["person_id"] != pid for o in db.outcomes_for(hid))
    set_status(cfg, db, mailbox, "objected")
    assert db.outcomes_for(hid) == []


def test_outreach_page_records_outcome_and_shows_org_metrics(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.db import DB
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    oid = db.outreach_list(1)[0]["id"]
    db.update_outreach(oid, status="sent", sent_at="2026-10-14T09:00:00",
                       package_json={"typ": "customer", "režim": "discovery"})
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/outreach.py").run()
    assert not at.exception, at.exception
    assert any("Učenie z odpovedí" in e.label for e in at.expander)
    next(b for b in at.button if b.label == "Uložiť výsledok oslovenia").click().run()
    assert not at.exception, at.exception
    db = DB(cfg.db_path)
    rows = db.outcomes_for(1)
    assert len(rows) == 1 and rows[0]["reply"] == "none" and rows[0]["outreach_id"] == oid
    assert json.loads(db.get_outreach(oid)["package_json"])["typ"] == "customer"
