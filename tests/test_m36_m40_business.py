"""M36 – zdroj → obchodný výsledok (správa) a M40 – benchmark v reálnom svete (kohorty systém vs. ručne z TED).
Fiktívne dáta, bez modelu a siete."""

import json

import pytest

from radar.benchmark import (BenchmarkError, COHORT_SIZE, add_manual_problem, add_member, cohort_report,
                             record_manual_outreach, set_prep_minutes)
from radar.commercial import record as record_commercial
from radar.commercial_yield import MIN_SENT, commercial_yield, source_of, yield_markdown
from radar.config import Config, EmbeddingsConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.outcomes import record_outcome
from radar.privacy import Suppression
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True))
    return cfg, DB(cfg.db_path)


def hypothesis_from(db, kind, category, title, passed=False, url=None):
    doc, _ = db.upsert_document(url or f"https://{title[:6].lower()}.example/{kind}", title, kind, "t", "q",
                                f"h-{title}", "Text dokumentu. " * 20)
    if category:
        db.conn.execute("UPDATE documents SET source_category = ? WHERE id = ?", (category, doc))
    cid = db.insert_claim(doc, 0, "t", f"Tvrdenie {title}", "segment", "pain", "Text dokumentu.")
    db.set_claim_verification(cid, "verified", 100, "ok")
    hid = db.create_hypothesis(title, "Popis", "segment", origin="discovery" if kind == "discovery" else "topic")
    db.assign_claim(hid, cid)
    if passed:
        db.conn.execute("UPDATE hypotheses SET validation_state = 'tested', validation_json = ? WHERE id = ?",
                        (json.dumps({"verdikt": "podložená"}), hid))
    db.conn.commit()
    return hid


def contacted(cfg, db, hid, n=1, org_name="Fiktívna Firma s.r.o.", confirmed=False, stage=None, sent=True):
    org, _ = db.upsert_organization(org_name, org_name.lower(), "search", website="https://firma.example/")
    db.link_org_hypothesis(org, hid, "segment_match")
    db.set_people_selection(hid, True)
    pid = db.insert_person(org, f"Osoba {org_name[:5]}", f"osoba {org_name[:5].lower()}", "https://firma.example/k", "x", hid,
                           role_title="konateľ")
    cp = db.add_contact_point(pid, org, "email", f"o{hid}{n}@firma.example", "https://firma.example/k",
                              f"o{hid}{n}@firma.example", "regex")
    oid = None
    for _ in range(n):
        oid = db.insert_outreach(pid, hid, "email", "A", "Predmet", "Telo", [], {}, "sent" if sent else "draft", cp, org_id=org)
        if sent:
            db.update_outreach(oid, sent_at="2026-10-20")
    if confirmed:
        record_outcome(cfg, db, outreach_id=oid, reply="relevant", problem_relevance="confirmed",
                       willingness={"stage": "not_asked"})
    if stage:
        record_commercial(db, hid, {"stage": stage, "evidence": "Konateľka 20. 10.: rozpočet na nástroj máme.",
                                    "conditions": "pilot na 3 mesiace za 50 EUR mesačne", "amount": 50, "currency": "EUR",
                                    "billing": "monthly"}, org_id=org)
    return org, pid, cp


# ---------------------------------------------------------------------------- M36
def test_two_funnels_by_strategy_origin_is_the_oldest_claim(tmp_path):
    cfg, db = setup(tmp_path)
    h_audit = hypothesis_from(db, "discovery", "audit", "Audit obcí", passed=True)
    h_matrix = hypothesis_from(db, "discovery", "query_matrix", "Matica dopytov")
    hypothesis_from(db, "search", None, "Téma zberu")
    later, _ = db.upsert_document("https://neskor.example/a", "Neskôr", "search", "t", "q", "h-later", "Text dokumentu. " * 20)
    cid = db.insert_claim(later, 0, "t", "Neskoršie tvrdenie", "s", "pain", "Text dokumentu.")
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(h_audit, cid)  # neskoršie tvrdenie z inej stratégie pôvod nemení
    db.upsert_document("https://data.example/x.csv", "Dáta", "gate1", "t", "q", "h-g1", "a;b\n1;2\n")  # nepočíta sa
    contacted(cfg, db, h_audit, confirmed=True, stage="purchase_intent")
    rep = commercial_yield(cfg, db)
    rows = {r["zdroj"]: r for r in rep["stratégie"]}
    cat = rows["katalóg zdrojov"]
    assert (cat["hypotézy"], cat["test prešiel"], cat["Brána 0"], cat["oslovené"], cat["odoslané správy"]) == (1, 1, 1, 1, 1)
    assert (cat["odpovede"], cat["potvrdený problém"]) == (1, 1)
    assert (cat["platiteľ"], cat["rozpočet"], cat["záujem o kúpu"], cat["zaplatené"]) == (1, 1, 1, 0)
    assert (cat["dokumenty"], cat["s overeným tvrdením"], cat["overené tvrdenia"]) == (1, 1, 1)
    assert rows["matica dopytov"]["hypotézy"] == 1 and rows["matica dopytov"]["oslovené"] == 0
    assert rows["témy (zber)"]["hypotézy"] == 1 and rows["témy (zber)"]["dokumenty"] == 2  # bez podkladu Brány 1
    assert rep["spolu"]["hypotézy"] == 3 and rep["spolu"]["dokumenty"] == 4
    assert not rep["bandit"]["pripravené"] and f"1 z {MIN_SENT}" in rep["bandit"]["text"]
    assert h_matrix and "| spolu |" in yield_markdown(rep)


def test_bandit_rule_needs_30_sent_in_3_categories_and_ted_counts_extra(tmp_path, monkeypatch):
    cfg, db = setup(tmp_path)
    hids = [hypothesis_from(db, "discovery", cat, f"Problém {cat}") for cat in ("audit", "fiscal", "ombudsman")]
    for i, hid in enumerate(hids[:2]):
        contacted(cfg, db, hid, n=15, org_name=f"Firma {i} s.r.o.")
    rep = commercial_yield(cfg, db)
    assert rep["bandit"]["odoslané"] == 30 and not rep["bandit"]["pripravené"]  # iba 2 kategórie
    contacted(cfg, db, hids[2], n=1, org_name="Firma 2 s.r.o.")
    monkeypatch.setattr(db, "ted_hypotheses", lambda: {hids[0]})
    rep = commercial_yield(cfg, db)
    assert rep["bandit"]["pripravené"] and rep["bandit"]["kategórie_s_oslovením"] == 3
    ted = next(r for r in rep["stratégie"] if r["zdroj"].startswith("TED"))
    assert ted["hypotézy"] == 1 and ted["odoslané správy"] == 15
    assert rep["spolu"]["hypotézy"] == 3  # TED sa ráta navyše, nie do súčtu


def test_source_mapping():
    assert source_of("discovery", "scout") == ("scout", "scout")
    assert source_of("discovery", "query_matrix") == ("matrix", "matica dopytov")
    assert source_of("rss", "audit") == ("catalog", "audit")
    assert source_of("search", None, "manual") == ("manual", "ručne")
    assert source_of(None, None) == ("none", "bez tvrdení")


# ---------------------------------------------------------------------------- M40
def test_cohorts_limits_and_manual_problem_from_ted(tmp_path):
    cfg, db = setup(tmp_path)
    sys_hid = hypothesis_from(db, "discovery", "audit", "Systémový problém")
    add_member(db, sys_hid, "system")
    with pytest.raises(BenchmarkError, match="už je v benchmarku"):
        add_member(db, sys_hid, "system")
    with pytest.raises(BenchmarkError, match="ručnej kohorty"):
        add_member(db, hypothesis_from(db, "search", None, "Iný systémový"), "manual")
    with pytest.raises(BenchmarkError, match="odkaz"):
        add_manual_problem(db, "Obce nestíhajú vyhodnocovať ponuky", "malé obce", "", "nie-je-url")
    man = add_manual_problem(db, "Obce nestíhajú vyhodnocovať ponuky", "malé obce", "Z oznámenia.",
                             "https://ted.europa.eu/sk/notice/-/detail/123456-2026")
    hyp = db.get_hypothesis(man)
    assert hyp["origin"] == "manual" and hyp["people_enabled"] == 1
    assert db.benchmark_member(man)["source_url"].startswith("https://ted.europa.eu")
    outside = db.create_hypothesis("Ručný problém mimo benchmarku", "Popis", "segment", origin="manual")
    with pytest.raises(BenchmarkError, match="kohorty systému"):
        add_member(db, outside, "system")
    for i in range(COHORT_SIZE - 1):
        add_member(db, hypothesis_from(db, "search", None, f"Ďalší {i}"), "system")
    with pytest.raises(BenchmarkError, match="už má"):
        add_member(db, hypothesis_from(db, "search", None, "Jedenásty"), "system")


def test_manual_outreach_is_recorded_for_mail_matching_and_respects_suppression(tmp_path):
    cfg, db = setup(tmp_path)
    man = add_manual_problem(db, "Obce nestíhajú vyhodnocovať ponuky", "malé obce", "", "https://ted.europa.eu/x")
    org, pid, cp = contacted(cfg, db, man, n=0)
    oid = record_manual_outreach(cfg, db, man, cp, "Otázka k vyhodnocovaniu ponúk")
    row = db.get_outreach(oid)
    assert (row["status"], row["variant"], row["contact_point_id"]) == ("approved", "manual", cp)
    assert json.loads(row["checks_json"]) == {"napísané_ručne": True}
    with pytest.raises(BenchmarkError, match="predmet"):
        record_manual_outreach(cfg, db, man, cp, " ")
    Suppression(cfg, db).add("objection", email=db.get_contact(cp)["value"])
    with pytest.raises(BenchmarkError, match="potlačenia"):
        record_manual_outreach(cfg, db, man, cp, "Znova")


def test_report_compares_commercial_states_only_after_targets(tmp_path):
    cfg, db = setup(tmp_path)
    sys_hid = hypothesis_from(db, "discovery", "audit", "Systémový problém")
    add_member(db, sys_hid, "system")
    man = add_manual_problem(db, "Ručný problém z oznámenia", "segment", "", "https://ted.europa.eu/y")
    contacted(cfg, db, sys_hid, n=12, org_name="Systémová Firma s.r.o.", confirmed=True, stage="budget_verified")
    contacted(cfg, db, man, n=9, org_name="Ručná Firma s.r.o.", confirmed=True, stage="buyer_identified")
    set_prep_minutes(db, man, 240)
    rep = cohort_report(db)
    by = {r["kohorta"].split()[0]: r for r in rep["kohorty"]}
    assert by["systém"]["rozpočet"] == 1 and by["ručne"]["rozpočet"] == 0 and by["ručne"]["platiteľ"] == 1
    assert by["ručne"]["príprava (min, medián)"] == 240 and rep["odoslané"] == 21
    assert rep["verdikt"].startswith("málo dát")  # 0 z 5 rozhovorov
    for i in range(5):
        db.conn.execute("INSERT INTO interviews (pseudonym, hypothesis_id, interviewee_kind, held_at, created_at) "
                        "VALUES (?, ?, 'customer', '2026-10-25', '2026-10-25')", (f"R{i}", sys_hid if i < 3 else man))
    db.conn.commit()
    rep = cohort_report(db)
    assert rep["verdikt"].startswith("systém vedie")
    member = next(m for m in rep["členovia"] if m["H"] == sys_hid)
    assert member["dni do 1. oslovenia"] is not None and member["rozpočet"] == 1


def test_gate0_benchmark_tab_and_sources_yield_render(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from tests.test_m2_gui import APP

    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate0.py").run()
    assert not at.exception, at.exception
    assert any(b.label == "Pridať do kohorty systému" for b in at.button)
    at.switch_page("app_pages/sources.py").run()
    assert not at.exception, at.exception
    assert any("Bandit ostáva" in i.value for i in at.info)
