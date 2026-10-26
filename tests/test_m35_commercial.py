"""M35 – obchodné stavy a ochota platiť (26. 10. 2026): „kto platí“ nie je „ochota platiť“. Štyri stupne s dôkazom,
platba so sumou, pri potvrdenom probléme povinné posúdenie ochoty platiť (pokyn používateľa), migrácia starých údajov
iba na to, čo naozaj dokladajú, forget. Offline."""

import json

import pytest

from radar.commercial import CommercialError, record, wtp_state
from radar.config import Config, EmbeddingsConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.outcomes import OutcomeError, record_outcome
from radar.progress import axes
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI


def _world(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(),
                 embeddings=EmbeddingsConfig(enabled=False), privacy=PrivacyConfig(lia_completed=True))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Kancelárie ručne prepisujú doklady", "", "účtovné kancelárie")
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Alfa Účty s.r.o.", "alfa ucty", "evidence", ico="22222222", sector_kind="private")
    db.link_org_hypothesis(org, hid, "segment_match")
    pid = db.insert_person(org, "Ing. Oľga Testová", "olga testova", "https://alfa.sk/tim", "Oľga Testová, konateľka", hid,
                           role_title="konateľka")
    return cfg, db, hid, org, pid


def test_payer_is_not_willingness_and_stages_need_evidence(tmp_path):
    cfg, db, hid, org, pid = _world(tmp_path)
    db.set_hypothesis_fields(hid, payer_confirmed="majiteľ kancelárie")
    assert axes(cfg, db, hid)["ochota_platiť"]["stav"] == "platiteľ známy"  # predtým „potvrdená“
    with pytest.raises(CommercialError, match="dôkaz"):
        record(db, hid, {"stage": "buyer_identified", "evidence": "áno"}, org_id=org)
    record(db, hid, {"stage": "buyer_identified", "evidence": "konateľka schvaľuje výdavky, rozhovor 3. 11."}, org_id=org)
    record(db, hid, {"stage": "budget_verified", "evidence": "dnes platia externú výpomoc 400 € mesačne (rozhovor 3. 11.)"},
           org_id=org)
    s = wtp_state(db, hid)
    assert s["stupeň"] == "budget_verified" and not s["potvrdená"] and axes(cfg, db, hid)["ochota_platiť"]["stav"] == \
        "rozpočet overený"
    with pytest.raises(CommercialError, match="podmienky"):
        record(db, hid, {"stage": "purchase_intent", "evidence": "chcú to, povedala konateľka 3. 11."}, org_id=org)
    with pytest.raises(CommercialError, match="organizáciu"):
        record(db, hid, {"stage": "purchase_intent", "evidence": "chcú to, povedala konateľka 3. 11.",
                         "conditions": "pilot na 20 dokladov, do 300 € mesačne, od januára"})
    record(db, hid, {"stage": "purchase_intent", "evidence": "konateľka po skúške 3. 11.",
                     "conditions": "ročná licencia, ak skúška zníži opravy o polovicu", "amount": 3000, "billing": "annual"},
           org_id=org)
    s = wtp_state(db, hid)
    assert s["potvrdená"] and s["stav"] == "potvrdená – záujem o kúpu" and s["záujem_eur"] == 3000
    assert s["organizácie"] == {org: "purchase_intent"} and s["potvrdené_organizácie"] == 1


def test_payment_needs_amount_currency_and_billing(tmp_path):
    cfg, db, hid, org, pid = _world(tmp_path)
    base = {"stage": "payment_received", "evidence": "faktúra uhradená 5. 12., potvrdila konateľka"}
    with pytest.raises(CommercialError, match="sumu, menu"):
        record(db, hid, base, org_id=org)
    with pytest.raises(CommercialError, match="kladná"):
        record(db, hid, {**base, "amount": -5, "currency": "EUR", "billing": "monthly"}, org_id=org)
    cid = record(db, hid, {**base, "amount": 290, "currency": "eur", "billing": "monthly", "contract_value": 3480}, org_id=org)
    row = db.get_commercial_evidence(cid)
    assert (row["amount"], row["currency"], row["billing"], row["contract_value"]) == (290.0, "EUR", "monthly", 3480.0)
    assert wtp_state(db, hid)["zaplatené_eur"] == 290 and axes(cfg, db, hid)["ochota_platiť"]["stav"] == \
        "potvrdená – zaplatené"
    db.withdraw_commercial_evidence(cid, "storno faktúry")
    assert wtp_state(db, hid)["stupeň"] is None  # odvolaný zápis sa nepočíta, ostáva v histórii
    assert "[odvolané: storno faktúry]" in db.get_commercial_evidence(cid)["evidence"]


def test_confirmed_outcome_requires_willingness_and_paid_requires_payment(tmp_path):
    cfg, db, hid, org, pid = _world(tmp_path)
    with pytest.raises(OutcomeError, match="ochotu platiť"):
        record_outcome(cfg, db, hid=hid, org_id=org, reply="relevant", problem_relevance="confirmed")
    with pytest.raises(OutcomeError, match="platbu"):
        record_outcome(cfg, db, hid=hid, org_id=org, reply="relevant", paid=True,
                       willingness={"stage": "not_asked"})
    assert db.outcomes_for(hid) == []  # chyba nič nezapísala
    res = record_outcome(cfg, db, hid=hid, org_id=org, reply="relevant", problem_relevance="confirmed",
                         willingness={"stage": "not_asked"})
    assert db.outcomes_for(hid)[0]["wtp_stage"] == "not_asked" and "ochota_platiť" not in res
    res = record_outcome(cfg, db, hid=hid, person_id=pid, reply="relevant", problem_relevance="confirmed",
                         willingness={"stage": "payment_received", "evidence": "prvá mesačná platba 1. 12. (výpis)",
                                      "amount": 290, "currency": "EUR", "billing": "monthly"})
    out = db.outcomes_for(hid)[-1]
    assert out["paid"] == 1 and out["wtp_stage"] == "payment_received"
    ce = db.get_commercial_evidence(res["ochota_platiť"])
    assert ce["org_id"] == org and ce["outcome_id"] == out["id"]  # organizácia z osoby
    with pytest.raises(OutcomeError, match="podmienky"):
        record_outcome(cfg, db, hid=hid, org_id=org, reply="relevant", problem_relevance="confirmed",
                       willingness={"stage": "purchase_intent", "evidence": "konateľka 3. 11. – chcú to"})


def test_interview_records_willingness_and_forget_keeps_only_the_org_fact(tmp_path):
    from radar.crm import record_interview
    from radar.privacy import forget

    cfg, db, hid, org, pid = _world(tmp_path)
    res = record_interview(cfg, db, hid, "customer", "2026-11-03", "Oľga Testová: rozpočet na externú výpomoc máme.",
                           person_id=pid, willingness={"stage": "budget_verified",
                                                       "evidence": "Oľga Testová: rozpočet na výpomoc 400 € mesačne"})
    ce = db.get_commercial_evidence(res["ochota_platiť"])
    assert ce["interview_id"] == res["interview_id"] and ce["org_id"] == org
    forget(cfg, db, person_id=pid)
    ce = db.get_commercial_evidence(res["ochota_platiť"])
    assert ce["person_id"] is None and "Testov" not in ce["evidence"] and ce["stage"] == "budget_verified"


def test_migration_v17_carries_old_payer_as_buyer_and_paid_flag_without_inventing_amount(tmp_path):
    cfg, db, hid, org, pid = _world(tmp_path)
    db.set_hypothesis_fields(hid, payer_confirmed="obec z vlastného rozpočtu")
    db.set_hypothesis_fields(hid, business_json=json.dumps({"platiteľ": {"hotové": True, "dôkaz": "starosta 12. 10."}}))
    oid = db.add_outcome(hid, org_id=org, reply="relevant", paid=True)
    db.conn.execute("DROP TABLE commercial_evidence")
    db.conn.execute("ALTER TABLE outreach_outcomes DROP COLUMN wtp_stage")
    db.conn.execute("PRAGMA user_version = 16")
    db.conn.commit()
    db.close()
    db = DB(cfg.db_path)
    assert db.migration["from"] == 16 and db.migration["backup"]
    rows = db.commercial_evidence_for(hid)
    buyer = [r for r in rows if r["stage"] == "buyer_identified"]
    paid = [r for r in rows if r["stage"] == "payment_received"]
    assert len(buyer) == 1 and "obec z vlastného rozpočtu" in buyer[0]["evidence"] and "starosta 12. 10." in buyer[0]["evidence"]
    assert len(paid) == 1 and paid[0]["amount"] is None and "doplň sumu" in paid[0]["evidence"] and paid[0]["outcome_id"] == oid
    assert wtp_state(db, hid)["stav"] == "potvrdená – zaplatené"  # príznak „platené“ ostáva, sumu doplní človek


def test_brief_asks_willingness_questions_and_lists_it_as_missing(gui):
    from radar.config import load_config
    from radar.problem_brief import completeness, problem_brief

    cfg = load_config(gui / "config.yaml")
    db = DB(cfg.db_path)
    md = problem_brief(cfg, db, 1)
    assert "**Ako overiť ochotu platiť**" in md and "z akého rozpočtu sa platí dnešné riešenie" in md
    assert "ochota platiť: nezistená" in md and "| ochota platiť | chýba |" in md
    assert "ochota platiť" in completeness(cfg, db, 1)["chýba"]
    db.close()
