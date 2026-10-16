"""M25 – návrhy oslovení podľa účelu (audit 15. 10. 2026, balík C): zákazník, držiteľ dát, expert aj organizácia cez
funkčný kontakt; reťazec prečo → potreba → výstup → vstup; politika bezplatného dema a skúšky; potreba ako hypotéza;
bez čísel úspory bez merania; pilot po potvrdení problému; pripravenosť. Offline (falošný model)."""

import json

from radar.crm import export_rows
from radar.outreach import POLICY_TEXT, check_draft, compose, run_drafts
from radar.util import Deadline
from tests.test_m6_outreach import REAL_SENDER, person_row, setup

CORE = ("predpokladám, že sa vo vašom tíme môže objavovať ručné urgovanie dodávok. Chcem si overiť, či je to aj váš "
        "prípad – kedy sa to stalo naposledy?")


def _world(tmp_path, sender=REAL_SENDER):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, sender)
    rec.responses["DraftOut"] = {"subject": "Otázka k meškaniu dodávok", "body": CORE, "facts_used": ["F1"]}
    db.add_value_proposal(hid, "customer", {"need_statement": "Plánovač možno ručne urguje dodávky.",
                                            "deliverable": "prehľad odchýlok dodania", "decision_supported":
                                            "ktoré zákazky urgovať", "minimum_input": "vysvetlenie polí termínov",
                                            "limitations": "nevieme, či majú pôvodný plán"},
                          need_status="hypothesis", status="checked")
    urad, _ = db.upsert_organization("Štatistický úrad", "statisticky urad", "gate1_source", sector_kind="public",
                                     website="https://urad.sk/")
    dh = db.insert_person(urad, "Eva Dátová", "eva datova", "https://urad.sk/k", "Eva Dátová, analytička", hid,
                          role_title="analytička dát", source_kind="website")
    db.add_contact_point(dh, urad, "email", "eva@urad.sk", "https://urad.sk/k", "Eva Dátová eva@urad.sk", "regex")
    db.set_link_priority(db.upsert_link(dh, hid, "data_holder", None, "https://urad.sk/data"), 0.7, {"skupina": "data_holder"})
    zvaz, _ = db.upsert_organization("Zväz strojárov", "zvaz strojarov", "contact_search", website="https://zvaz.sk/",
                                     sector_kind="association")
    ex = db.insert_person(zvaz, "Peter Expert", "peter expert", "https://zvaz.sk/t", "Peter Expert, analytik", hid,
                          role_title="analytik", source_kind="website")
    db.add_contact_point(ex, zvaz, "email", "peter@zvaz.sk", "https://zvaz.sk/t", "Peter Expert peter@zvaz.sk", "regex")
    db.set_link_priority(db.upsert_link(ex, hid, "expert"), 0.6, {"skupina": "expert"})
    db.update_person(ex, is_expert=1)
    firma, _ = db.upsert_organization("Beta Strojáreň", "beta strojaren", "segment", website="https://beta.sk/",
                                      sector_kind="private")
    db.link_org_hypothesis(firma, hid, "segment_match")
    db.add_contact_point(None, firma, "email", "info@beta.sk", "https://beta.sk/kontakt", "info@beta.sk", "regex", True)
    return cfg, db, hid, pid, org_id, rec, router, dh, ex, firma


def test_customer_data_holder_expert_and_mailbox_get_role_specific_drafts(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _world(tmp_path)
    stats = run_drafts(cfg, db, router, Deadline(1), [hid])
    assert stats["mode_discovery"] >= 2 and stats["mode_data_request"] == 1 and stats["mode_expert_review"] == 1
    cust = db.outreach_for_person(pid, hid)[0]
    data = db.outreach_for_person(dh, hid)[0]
    expert = db.outreach_for_person(ex, hid)[0]
    org_row = db.outreach_for_org(firma, hid)[0]
    assert POLICY_TEXT in cust["body"] and "Nejde o ponuku" not in cust["body"]  # komerčný adresát s ponukou dema
    assert POLICY_TEXT not in data["body"] and "Overujem, či sa dá z dostupných dát" in data["body"]  # verejný správca
    assert POLICY_TEXT not in expert["body"] and "Overujem predpoklady" in expert["body"]
    assert org_row["person_id"] is None and org_row["body"].startswith("Dobrý deň,\n")  # osoba sa nevymýšľa
    assert "funkčný kontakt organizácie" in json.loads(org_row["facts_json"])[0]["text"]
    pkg = json.loads(cust["package_json"])
    assert pkg["prečo_tento_kontakt"]["zdroj"] and pkg["potreba"]["stav"] == "hypothesis"
    assert pkg["výstup"] == "prehľad odchýlok dodania" and pkg["minimálny_vstup"] == "vysvetlenie polí termínov"
    assert cust["readiness"] == "ready"
    data_pkg = json.loads(data["package_json"])
    assert data_pkg["typ"] == "data_holder" and data_pkg["stav_návrhu_prínosu"] == "chýba"


def test_need_as_fact_promises_and_not_offer_claims_are_flagged(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, *_ = _world(tmp_path)
    facts = [{"id": "F1", "text": "Výrobcovia hlásia meškanie o 6 týždňov.", "url": "https://zvaz.sk/p", "kind": "evidence"}]
    fact_core = "váš tím ručne urguje dodávky. Pošlite nám dáta."
    subject, body = compose(cfg, "email", person_row(), "private", fact_core, "Otázka", "https://x.sk", None, "discovery",
                            POLICY_TEXT)
    checks = check_draft(cfg, "email", subject, body, fact_core, facts, "https://x.sk", "discovery", POLICY_TEXT, "",
                         "hypothesis")
    assert any("predpoklad alebo otázku" in p for p in checks["problems"])  # potreba nie je fakt
    savings = "predpokladám, že vám to ušetrí 40 % času. Je to aj váš prípad?"
    subject, body = compose(cfg, "email", person_row(), "private", savings, "Otázka", "https://x.sk", None, "discovery",
                            POLICY_TEXT)
    assert any("40" in p for p in check_draft(cfg, "email", subject, body, savings, facts, "https://x.sk", "discovery",
                                              POLICY_TEXT, "", "hypothesis")["problems"])
    approved = "pri skúške by sme rozobrali 20 riadkov do 10 pracovných dní. Je to pre vás zaujímavé?"
    subject, body = compose(cfg, "email", person_row(), "private", approved, "Otázka", "https://x.sk", None, "discovery",
                            POLICY_TEXT)
    assert check_draft(cfg, "email", subject, body, approved, facts, "https://x.sk", "discovery", POLICY_TEXT,
                       "rozbor 20 riadkov do 10 pracovných dní", "hypothesis")["ok"]  # schválený záväzok
    not_offer = "nejde o ponuku, iba osobný výskum. Kedy sa vám to stalo naposledy?"
    subject, body = compose(cfg, "email", person_row(), "private", not_offer, "Otázka", "https://x.sk", None, "discovery",
                            POLICY_TEXT)
    assert any("tvrdí, že nejde o ponuku" in p for p in check_draft(
        cfg, "email", subject, body, not_offer, facts, "https://x.sk", "discovery", POLICY_TEXT, "", "hypothesis")["problems"])
    # „demo“ je pri ponuke povolené, inak zakázané
    demo = "môžem pripraviť demo prehľadu. Bolo by to užitočné?"
    subject, body = compose(cfg, "email", person_row(), "private", demo, "Otázka", "https://x.sk", None, "discovery",
                            POLICY_TEXT)
    assert check_draft(cfg, "email", subject, body, demo, facts, "https://x.sk", "discovery", POLICY_TEXT, "",
                       "hypothesis")["ok"]
    subject, body = compose(cfg, "email", person_row(), "public", demo, "Otázka", "https://x.sk", None, "expert_review", None)
    assert not check_draft(cfg, "email", subject, body, demo, facts, "https://x.sk", "expert_review", None, "",
                           "hypothesis")["ok"]


def test_pilot_only_after_confirmed_problem_and_says_business_intent(tmp_path):
    cfg, db, hid, pid, org_id, rec, router, *_ = _world(tmp_path)
    run_drafts(cfg, db, router, Deadline(1), [hid])
    first = db.outreach_for_person(pid, hid)[0]
    db.update_outreach(first["id"], status="replied")
    db.update_person(pid, status="replied")
    assert not [o for o in db.outreach_for_person(pid, hid) if o["mode"] == "pilot"]
    run_drafts(cfg, db, router, Deadline(1), [hid])
    assert not [o for o in db.outreach_for_person(pid, hid) if o["mode"] == "pilot"]  # bez potvrdenia problému nie
    db.add_outcome(hid, outreach_id=first["id"], person_id=pid, org_id=org_id, reply="relevant",
                   problem_relevance="confirmed", note="urgujú denne")
    run_drafts(cfg, db, router, Deadline(1), [hid])
    pilot = [o for o in db.outreach_for_person(pid, hid) if o["mode"] == "pilot"][0]
    assert "neskôr chcel ponúkať aj ako platenú službu" in pilot["body"] and POLICY_TEXT in pilot["body"]
    assert json.loads(pilot["package_json"])["potreba"]["stav"] == "confirmed"


def test_placeholders_mark_draft_unfinished_and_export_has_reason_chain(tmp_path):
    from radar.config import SenderConfig

    cfg, db, hid, pid, org_id, rec, router, *_ = _world(tmp_path, SenderConfig())  # {MENO_PRIEZVISKO} …
    run_drafts(cfg, db, router, Deadline(1), [hid])
    row = db.outreach_for_person(pid, hid)[0]
    assert row["readiness"] == "needs_input" and any("zástupné polia" in r for r in json.loads(row["package_json"])["pripravenosť"])
    cols, rows = export_rows(db, "outreach", include_personal=False)
    for c in ("why_recipient", "why_source", "need", "need_state", "deliverable", "minimum_input"):
        assert c in cols
    exported = next(r for r in rows if r[0] == row["id"])
    assert exported[cols.index("deliverable")] == "prehľad odchýlok dodania" and "Anna" not in exported[cols.index("person")]


def test_missing_source_of_reason_marks_draft_for_completion():
    from radar.outreach import readiness_of

    pkg = {"prečo_tento_kontakt": {"zdroj": ""}, "stav_návrhu_prínosu": "pending_review"}
    state, reasons = readiness_of(pkg, {"problems": []}, "Predmet", "Text bez zástupných polí")
    assert state == "needs_input" and any("zdroj dôvodu" in r for r in reasons) and any("hodnotiteľa" in r for r in reasons)


def test_public_recipient_gets_no_offer_terms_and_fact_ids_never_reach_the_text(tmp_path):
    """16. 10. 2026: ministerstvá dostali vetu o bezplatnom deme a platenom používaní (podľa DECISIONS iba komerčný
    adresát) a v texte ostali interné značky faktov „(F2, F3)“."""
    from radar.outreach import is_public_org, policy_text, strip_fact_refs

    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = _world(tmp_path)
    assert policy_text(cfg, "discovery", True, commercial=False) is None
    assert policy_text(cfg, "discovery", True, commercial=True) == POLICY_TEXT
    assert is_public_org({"sector_kind": None, "website": "https://mirri.gov.sk/", "name": "MIRRI SR"})
    assert is_public_org({"sector_kind": None, "website": None, "name": "Ministerstvo obrany SR"})
    assert not is_public_org({"sector_kind": None, "website": "https://gratex.sk/", "name": "Gratex"})
    assert strip_fact_refs("zdržania pri príprave projektov (F2, F3). Chcem sa opýtať [F1] na prípad.") == \
        "zdržania pri príprave projektov. Chcem sa opýtať na prípad."
    ministry, _ = db.upsert_organization("Ministerstvo investícií", "ministerstvo investicii", "evidence",
                                         website="https://mirri.gov.sk/")
    db.link_org_hypothesis(ministry, hid, "segment_match")
    db.add_contact_point(None, ministry, "email", "kontakt@mirri.gov.sk", "https://mirri.gov.sk/kontakt",
                         "kontakt@mirri.gov.sk", "regex", True)
    rec.responses["DraftOut"] = {"subject": "Otázka (F1)", "body": CORE + " Podľa správy (F1, F2) to trvá.",
                                 "facts_used": ["F1"]}
    run_drafts(cfg, db, router, Deadline(1), [hid])
    row = db.outreach_for_org(ministry, hid)[0]
    assert POLICY_TEXT not in row["body"] and "(F1" not in row["body"] and "F1" not in (row["subject"] or "")
    assert "Podľa správy to trvá." in row["body"]
    checks = check_draft(cfg, "email", "Predmet", row["body"].replace("trvá.", "trvá (F3)."), CORE, [], "https://mirri.gov.sk/kontakt")
    assert any("značky faktov" in p for p in checks["problems"])
