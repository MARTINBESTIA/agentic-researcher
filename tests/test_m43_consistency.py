"""M43 – kontrola konzistentnosti M36–M42 so staršími funkciami (28. 10. 2026): jedno pravidlo vynechania v automatike,
osoby s oslovením ostávajú vo výbere, výmaz osoby čistí texty dôkazov, ručné oslovenie dodržiava pravidlá schválenia
a v GUI sa neupravuje, dosiér obsahuje nové dáta. Fiktívne dáta, bez modelu a siete."""

import io
import json
import zipfile
from collections import Counter
from datetime import date

import pytest

from radar import dossier, funnel
from radar.alternatives import GAP_STATUS, pending
from radar.autopilot import gate1_ids, priority_ids, selected_ids
from radar.benchmark import BenchmarkError, add_manual_problem, record_manual_outreach
from radar.cards import card_targets
from radar.commercial import record as record_commercial
from radar.config import Config, EmbeddingsConfig, Gate1Config, ModelsConfig, OutreachConfig, PeopleConfig, PrivacyConfig
from radar.db import DB
from radar.domain import gate0_domains
from radar.economics import record as record_factor
from radar.opportunity import card_markdown, card_readiness
from radar.outreach import OutreachError, follow_up_text, recheck
from radar.people import rank_hypothesis
from radar.problem_brief import problem_brief
from radar.procurement import scope_hypotheses
from tests.fakes import Recorder, make_router
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI


def setup(tmp_path, **people):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=False, **people),
                 gate1=Gate1Config(nkod_enabled=False), config_dir=str(tmp_path), outreach=OutreachConfig(daily_cap=2))
    cfg.exports_dir = str(tmp_path / "exports")
    return cfg, DB(cfg.db_path)


def three_hypotheses(db):
    """B – potvrdené bez medzery, C – ručná kohorta benchmarku, A – bežná (vznikla posledná, aby filter nebol náhoda)."""
    b = db.create_hypothesis("Problém bez medzery", "Popis", "segment")
    db.set_people_selection(b, True)
    db.set_gap_status(b, GAP_STATUS, "človek potvrdil")
    c = add_manual_problem(db, "Ručný problém z oznámenia o obstarávaní", "segment", "", "https://ted.europa.eu/x")
    a = db.create_hypothesis("Bežný problém na Bráne 0", "Popis", "segment")
    db.set_people_selection(a, True)
    return a, b, c


def test_one_exclusion_rule_for_funnel_autopilot_and_default_selection(tmp_path, monkeypatch):
    cfg, db = setup(tmp_path)
    cfg.cards.min_claims = 0  # karty aj bez citátov – testuje sa iba výber hypotéz
    a, b, c = three_hypotheses(db)
    assert db.automation_excluded_ids() == {b, c}
    assert [h["id"] for h in db.automation_hypotheses()] == [a]
    assert selected_ids(db) == [a] and priority_ids(cfg, db) == [a] and set(gate1_ids(db)) <= {a}
    monkeypatch.setattr(funnel, "_usable", lambda cfg, db, h, now: ("contacts", "kontakty", "sig"))
    assert funnel.pick(cfg, db)["hypothesis_id"] == a
    queue = [h["id"] for h in db.validation_queue(50)]
    assert a in queue and b not in queue and c not in queue
    assert [h["id"] for h in db.validation_queue(50, ids=[c])] == [c]  # spustené človekom pre konkrétnu hypotézu
    assert [h["id"] for h in card_targets(cfg, db, None)] == [a]
    assert [h["id"] for h in scope_hypotheses(cfg, db)] == [a] and [h["id"] for h in scope_hypotheses(cfg, db, [c])] == [c]
    assert pending(cfg, db) == [a] and pending(cfg, db, [c]) == [c]
    assert {i for d in gate0_domains(cfg, db).values() for i in d["hypotheses"]} == {a}


def test_rank_keeps_people_with_outreach_and_restores_their_link(tmp_path):
    cfg, db = setup(tmp_path, max_people_per_org=1)
    hid = db.create_hypothesis("Kancelárie nestíhajú dopyty", "Popis", "kancelárie")
    db.set_people_selection(hid, True)
    db.set_role_map(hid, [{"category": "realitný maklér", "examples": ["realitná maklérka"], "rationale": "r"}])
    org, _ = db.upsert_organization("Fiktívne Reality s.r.o.", "fiktivne reality", "search", website="https://fr.example/",
                                    website_verified=1)
    db.link_org_hypothesis(org, hid, "segment_match")
    ids = []
    for i in range(3):
        pid = db.insert_person(org, f"Maklérka{i} Testová", f"maklerka{i} testova", "https://fr.example/tim", "x", hid,
                               role_title="realitná maklérka")
        cp = db.add_contact_point(pid, org, "email", f"m{i}@fr.example", "https://fr.example/tim", f"m{i}@fr.example", "regex")
        ids.append((pid, cp))
    data_pid = db.insert_person(org, "Dáta Správcová", "data spravcova", "https://fr.example/tim", "x", hid,
                                role_title="asistentka")
    data_cp = db.add_contact_point(data_pid, org, "email", "d@fr.example", "https://fr.example/tim", "d@fr.example", "regex")
    db.insert_outreach(ids[2][0], hid, "email", "A", "Otázka", "Telo", [], {}, "draft", ids[2][1], org_id=org)
    db.insert_outreach(data_pid, hid, "email", "A", "Vzorka dát", "Telo", [], {}, "needs_edit", data_cp, org_id=org,
                       mode="data_request")
    rec = Recorder()
    rec.responses = {"RoleFit": {"fit": "none", "category": "", "rationale": "nesúvisí s mapou rolí"}}
    router = make_router(cfg, db, rec)
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    links = {l["person_id"]: l["link_type"] for l in db.links_for_hypothesis(hid)}
    assert ids[2][0] in links and links[data_pid] == "data_holder"  # rozpracované oslovenia ostali s väzbou
    assert ids[0][0] in links  # limit 1 na organizáciu pre ostatných
    assert db.get_person(ids[1][0]) is None  # nevybraná bez oslovenia sa vymazala
    db.unlink_person(data_pid, hid)  # staršia nekonzistencia: väzba zmazaná, návrh ostal
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    assert {l["person_id"] for l in db.links_for_hypothesis(hid)} >= {ids[2][0], data_pid}


def test_deleting_a_person_keeps_org_facts_but_removes_who_said_it(tmp_path):
    cfg, db = setup(tmp_path)
    hid = db.create_hypothesis("Problém", "Popis", "segment")
    org, _ = db.upsert_organization("Fiktívna Firma s.r.o.", "fiktivna firma", "search")
    pid = db.insert_person(org, "Hana Fiktívna", "hana fiktivna", "https://f.example/", "x", hid, role_title="konateľka")
    record_commercial(db, hid, {"stage": "buyer_identified", "evidence": "Hana Fiktívna 25. 10.: schvaľujem to ja."},
                      org_id=org, person_id=pid)
    record_factor(db, hid, org, "minutes_per_case", "customer_reported", 30, person_id=pid,
                  evidence="Hana Fiktívna pri rozhovore 25. 10.")
    db.delete_person(pid)  # rovnaká cesta pre retenciu (purge) aj výber ľudí
    ce = db.commercial_evidence_for(hid)[0]
    bf = db.buyer_factors(hid, org)[0]
    assert ce["stage"] == "buyer_identified" and ce["person_id"] is None and "Hana" not in ce["evidence"]
    assert bf["low"] == 30 and bf["person_id"] is None and "Hana" not in (bf["evidence"] or "")


def test_manual_outreach_follows_approval_rules_and_is_not_edited_in_system(tmp_path):
    cfg, db = setup(tmp_path)
    hid = add_manual_problem(db, "Ručný problém z oznámenia o obstarávaní", "segment", "", "https://ted.europa.eu/x")
    org, _ = db.upsert_organization("Kupujúci s.r.o.", "kupujuci", "search", website="https://k.example/")
    db.link_org_hypothesis(org, hid, "manual")
    cps = []
    for i in range(3):
        pid = db.insert_person(org, f"Osoba{i} Testová", f"osoba{i} testova", "https://k.example/", "x", hid, role_title="riaditeľ")
        cps.append(db.add_contact_point(pid, org, "email", f"o{i}@k.example", "https://k.example/", f"o{i}@k.example", "regex"))
    db.set_org_hypothesis(org, hid, procurement_status="candidate", procurement_note="možná súvisiaca súťaž")
    with pytest.raises(BenchmarkError, match="obstarávanie"):
        record_manual_outreach(cfg, db, hid, cps[0], "Otázka")
    db.set_org_hypothesis(org, hid, procurement_status="none")
    oid = record_manual_outreach(cfg, db, hid, cps[0], "Otázka")
    assert db.get_outreach(oid)["approved_at"]
    record_manual_outreach(cfg, db, hid, cps[1], "Otázka 2")
    with pytest.raises(BenchmarkError, match="Denný limit"):
        record_manual_outreach(cfg, db, hid, cps[2], "Otázka 3")
    with pytest.raises(OutreachError, match="Ručne"):
        recheck(cfg, db, oid, "nový text", "Predmet")
    db.update_outreach(oid, status="sent", sent_at=date.today().isoformat())
    subject, body = follow_up_text(db.get_outreach(oid))
    assert subject == "Re: Otázka" and "mimo systému" not in body and "pripomínam" in body


def test_offer_readiness_in_card_markdown_and_brief_explains_exclusion(tmp_path):
    cfg, db = setup(tmp_path)
    a, b, c = three_hypotheses(db)
    org, _ = db.upsert_organization("Fiktívna Firma s.r.o.", "fiktivna firma", "search", website="https://f.example/",
                                    website_verified=1)
    db.link_org_hypothesis(org, a, "segment_match")
    card_id = db.add_org_card(org, a, "dopyty", "customer", {"why_org": []}, inputs_hash="x", status="checked")
    db.update_org_card(card_id, readiness_json=card_readiness(cfg, db, db.get_org_card(card_id)))
    assert "**Na ponuku:** nie" in card_markdown(db, db.get_org_card(card_id))
    assert "Automatika túto hypotézu vynecháva" in problem_brief(cfg, db, c)
    assert "bez obhájiteľnej medzery" in problem_brief(cfg, db, b)
    assert "Automatika túto hypotézu vynecháva" not in problem_brief(cfg, db, a)


def test_dossier_contains_outcomes_commercial_economics_alternatives_benchmark_and_ted(tmp_path):
    cfg, db = setup(tmp_path)
    hid = add_manual_problem(db, "Ručný problém z oznámenia o obstarávaní", "segment", "", "https://ted.europa.eu/x")
    org, _ = db.upsert_organization("Kupujúci s.r.o.", "kupujuci", "search", website="https://k.example/")
    db.link_org_hypothesis(org, hid, "manual")
    pid = db.insert_person(org, "Hana Fiktívna", "hana fiktivna", "https://k.example/", "x", hid, role_title="konateľka")
    record_commercial(db, hid, {"stage": "budget_verified", "evidence": "Hana Fiktívna 25. 10.: rozpočet máme."}, org_id=org,
                      person_id=pid)
    record_factor(db, hid, org, "cases_per_year", "customer_reported", 1200, person_id=pid,
                  evidence="Hana Fiktívna pri rozhovore 25. 10.")
    db.add_alternative_assessment(hid, "x", [], [], "insufficient", "bez zdrojov")
    path = dossier.write(cfg, db, hid)
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    for name in ("brana0/obchodne_dokazy.csv", "brana0/ekonomika_zakaznika.csv", "hypoteza/alternativy.csv",
                 "hypoteza/benchmark.csv"):  # prázdne tabuľky (výsledky oslovení, zákazky TED) sa nezapisujú
        assert name in files, name
    text = "".join(v.decode("utf-8-sig", "ignore") for k, v in files.items() if k.endswith((".csv", ".md", ".json")))
    assert "Fiktívn" not in text and "Fiktivn" not in text  # pseudonymizované: meno z textu dôkazu sa nedostane von
    assert "1200" in files["brana0/ekonomika_zakaznika.csv"].decode("utf-8-sig")
    assert "manual" in files["hypoteza/benchmark.csv"].decode("utf-8-sig")


def test_outreach_page_shows_manual_record_without_edit_or_mailto(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    link = d.links_for_hypothesis(hid)[0]
    cp = next(c for c in d.contacts_for_person(link["person_id"]) if c["kind"] == "email")
    d.insert_outreach(link["person_id"], hid, "email", "manual", "Ručná otázka", "(text napísaný ručne mimo systému)", [],
                      {"napísané_ručne": True}, "approved", cp["id"])
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/outreach.py").run()
    assert not at.exception, at.exception
    assert any("Ručne napísané oslovenie" in i.value for i in at.info)
    assert all("(text napísaný ručne mimo systému)" not in t.value for t in at.text_area)


def test_drafts_to_an_address_of_another_org_are_held_and_hidden_from_who_to_contact(tmp_path):
    """Živé dáta 28. 10. 2026: staršie pravidlo uložilo k obci adresu dodávateľa jej webu a k úradu vzorovú adresu
    z formulára – návrh na takú adresu sa nedá schváliť a „Koho osloviť“ ju ako adresu organizácie neukáže."""
    from radar.outreach import send_state
    from radar.shortlist import contact_shortlist

    cfg, db = setup(tmp_path)
    hid = db.create_hypothesis("Obce nestíhajú", "Popis", "obce")
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Obec Fiktívna", "obec fiktivna", "search", website="https://www.obecfiktivna.sk/",
                                    website_verified=1)
    db.update_organization(org, sector_kind="public")
    db.link_org_hypothesis(org, hid, "segment_match")
    bad = db.add_contact_point(None, org, "email", "info@webyobci.sk", "https://www.obecfiktivna.sk/",
                               "web: info@webyobci.sk", "mailto", is_generic=True)
    oid = db.insert_outreach(None, hid, "email", "A", "Otázka", "Telo", [], {}, "draft", bad, org_id=org)
    state, reasons = send_state(cfg, db, db.get_outreach(oid))
    assert state == "needs_contact_evidence" and "nepatrí tejto organizácii" in reasons[0]
    assert all(r["e-mail"] != "info@webyobci.sk" for r in contact_shortlist(cfg, db, hid))
    good = db.add_contact_point(None, org, "email", "podatelna@obecfiktivna.sk", "https://www.obecfiktivna.sk/kontakt",
                                "Podateľňa: podatelna@obecfiktivna.sk", "mailto", is_generic=True)
    oid2 = db.insert_outreach(None, hid, "email", "A", "Otázka", "Telo", [], {}, "draft", good, org_id=org)
    assert send_state(cfg, db, db.get_outreach(oid2))[0] != "needs_contact_evidence"
    assert any(r["e-mail"] == "podatelna@obecfiktivna.sk" for r in contact_shortlist(cfg, db, hid))


def test_message_to_org_address_shows_on_the_contact_row_and_in_the_funnel(tmp_path):
    """Živé dáta 30. 10. 2026: správy na info@ boli zapísané pri organizácii, riadok konateľa ostal „nové“ a lievik zlial
    všetky takéto správy do jednej."""
    from radar.crm import gate0_metrics
    from radar.outreach import mark_sent
    from radar.shortlist import contact_shortlist

    cfg, db = setup(tmp_path)
    hid = db.create_hypothesis("Kancelárie nestíhajú dopyty", "Popis", "kancelárie")
    db.set_people_selection(hid, True)
    for i, used in enumerate(("info", "kancelaria")):
        org, _ = db.upsert_organization(f"Fiktívna Firma {i} s.r.o.", f"fiktivna firma {i}", "search",
                                        website=f"https://firma{i}.example/", website_verified=1)
        db.link_org_hypothesis(org, hid, "segment_match")
        pid = db.insert_person(org, f"Konateľ{i} Testový", f"konatel{i} testovy", f"https://firma{i}.example/", "x", hid,
                               role_title="konateľ", source_kind="rpo")
        db.set_link_priority(db.upsert_link(pid, hid, "role_owner"), 0.5, {"skupina": "customer"})
        db.add_contact_point(None, org, "email", f"sekretariat@firma{i}.example", f"https://firma{i}.example/k",
                             f"sekretariat@firma{i}.example", "mailto", is_generic=True)
        cp = db.add_contact_point(None, org, "email", f"{used}@firma{i}.example", f"https://firma{i}.example/k",
                                  f"{used}@firma{i}.example", "mailto", is_generic=True)
        oid = record_manual_outreach(cfg, db, hid, cp, "Otázka")
        mark_sent(cfg, db, oid, date.today(), detected=True)
    rows = [r for r in contact_shortlist(cfg, db, hid) if r["person_id"]]
    assert len(rows) == 2 and all(r["stav_komunikácie"] == "čaká na odpoveď" for r in rows)
    assert {r["e-mail"] for r in rows} == {"info@firma0.example", "kancelaria@firma1.example"}  # adresa, kam správa odišla
    assert gate0_metrics(db, hid)["funnel"]["odoslaní"] == 2


def test_outreach_page_shows_bounced_message_by_default(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    link = d.links_for_hypothesis(hid)[0]
    cp = next(c for c in d.contacts_for_person(link["person_id"]) if c["kind"] == "email")
    d.insert_outreach(link["person_id"], hid, "email", "manual", "Vrátená správa", "(text)", [], {}, "bounced", cp["id"])
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/outreach.py").run()
    assert not at.exception, at.exception
    assert any("bounced" in e.label for e in at.expander)
