"""M34 – Prehľad príležitosti (21. 10. 2026): jedna strana na problém pre nového človeka – odkazy na overené zdroje
podľa pôvodu, čo problém znamená, navrhnutý produkt ako hypotéza, čo je podložené dátami, koho a s čím osloviť, čo chýba.
Bez volania modelu, osoby pseudonymizované. Offline."""

import json

from radar.problem_brief import completeness, problem_brief, problems_index, write_briefs
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI
from tests.test_m33_org_cards import _org, _world, card_payload


def _full(db, hid, ids):
    db.add_card(hid, {"problem": {"text": "Kancelárie ručne prepisujú doklady.", "claims": [ids["proc"]]},
                      "who": {"text": "malé účtovné kancelárie"}, "current_practice": {"text": "ručné zadávanie"},
                      "cost_of_error": {"text": "čas a opravy"}, "decision": {"text": "ktoré doklady spracovať skôr"},
                      "helpful_output": "ranking", "helpful_output_text": "poradie typov dokladov podľa ručnej práce",
                      "business_metric": "minúty na doklad", "falsification": "prepis netrvá dlho"}, "ok")
    brief = {"process_steps": [{"text": "Klient doručí doklady.", "claims": [ids["proc"]]}],
             "terms": [{"term": "prepis", "explanation": "ručné zadanie údajov"}], "roles": {"user": {"text": "účtovník"}},
             "decision": {"text": "čo automatizovať"}, "current_practice": {"text": "ručne"},
             "existing_solutions": [{"text": "OCR moduly účtovných programov"}], "alternative_explanations": [],
             "data_needed": [{"field": "čas na doklad", "meaning_pitfall": "líši sa podľa typu"}],
             "falsifying_questions": [{"aspect": "problem_exists", "question": "Kedy ste naposledy prepisovali faktúru?"}],
             "open_questions": [], "unknowns": ["koľko času to trvá"]}
    db.add_brief(f"hypothesis:{hid}", "H", brief, [], "x", {}, "ok", None, "test")
    db.add_value_proposal(hid, "customer", {"deliverable": "rozbor 20 dokladov", "decision_supported": "čo automatizovať",
                                            "minimum_input": "20 dokladov", "limitations": "malá vzorka"},
                          status="checked")


def test_brief_has_all_parts_with_sources_by_origin_product_contacts_and_no_names(tmp_path):
    cfg, db, hid, ids, rec, router = _world(tmp_path)
    _full(db, hid, ids)
    org = _org(db, hid, "Alfa Účty s.r.o.", "22222222", web="https://alfa.sk/")
    pid = db.insert_person(org, "Ing. Oľga Testová", "olga testova", "https://alfa.sk/tim", "Oľga Testová, konateľka", hid,
                           role_title="konateľka")
    db.add_contact_point(pid, org, "email", "olga@alfa.sk", "https://alfa.sk/tim", "Oľga Testová olga@alfa.sk", "regex")
    db.set_link_priority(db.upsert_link(pid, hid, "role_owner"), 0.8, {"role_fit": 1.0})
    md = problem_brief(cfg, db, hid)
    for part in ("## 1. Problém jednou vetou", "## 2. Odkazy na zdroje", "## 3. Čo ten problém znamená",
                 "## 4. Navrhnutý produkt", "## 5. Čo je podložené dátami", "## 6. Koho osloviť a s čím",
                 "## 7. Čo chýba"):
        assert part in md, part
    assert "https://www.zvaz-uctovnikov.sk/prieskum" in md and "dôkaz problému 1" in md  # nezávislý zdroj s odkazom
    assert "https://www.mirri.gov.sk/sprava" not in md and "iba kontext 1" in md  # eurofondy nie sú dôkaz procesu
    assert "kros.sk" in md and "dodávatelia a riešenia 2" in md  # dodávatelia zvlášť
    assert "poradie typov dokladov" in md and "rozbor 20 dokladov" in md and "OCR moduly" in md
    assert "Kedy ste naposledy prepisovali faktúru?" in md and "klient doručí doklady" in md.lower()
    assert "konateľka" in md and "Osoba P" in md and "Testov" not in md and "olga@" not in md  # pseudonymizované
    assert "rozhovory 0" in md  # zákazník nepotvrdený
    c = completeness(cfg, db, hid)
    assert c["má"]["doména"] and c["má"]["produkt"] and c["má"]["kontakty"] and "oslovenia" in c["chýba"]


def test_vendor_only_evidence_is_flagged_and_missing_parts_point_to_actions(tmp_path):
    cfg, db, hid, ids, rec, router = _world(tmp_path)
    db.conn.execute("DELETE FROM hypothesis_claims WHERE hypothesis_id = ? AND claim_id = ?", (hid, ids["proc"]))
    md = problem_brief(cfg, db, hid)
    assert "Nezávislý dôkaz problému chýba" in md and "dôkaz problému 0" in md
    assert "Prehľad domény ešte nevznikol" in md and "Zatiaľ bez návrhu produktu" in md
    assert "Zatiaľ nikto s kontaktom" in md
    c = completeness(cfg, db, hid)
    assert set(c["chýba"]) >= {"zdroje", "doména", "produkt", "kontakty"}


def test_index_and_files_are_written_ordered_by_completeness(tmp_path):
    cfg, db, hid, ids, rec, router = _world(tmp_path)
    other = db.create_hypothesis("Iný problém bez podkladov", "", "x")
    db.set_people_selection(other, True)
    _full(db, hid, ids)
    paths = write_briefs(cfg, db, tmp_path / "out")
    assert {p.name for p in paths} == {"INDEX.md", f"H{hid}.md", f"H{other}.md"}
    idx = problems_index(cfg, db)
    assert idx.index(f"[H{hid}]") < idx.index(f"[H{other}]")


def test_problems_page_shows_opportunity_brief_tab_without_model_calls(gui):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.db import DB
    from tests.test_m2_gui import APP

    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/problems.py").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Prehľad príležitosti" in text and "Koho osloviť a s čím" in text
    d = DB(load_config(gui / "config.yaml").db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()
