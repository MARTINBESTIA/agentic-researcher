"""M33 – od doménového prehľadu ku konkrétnej ponuke (zadanie 17. 10. 2026): 14 povinných scenárov.

Relevantnosť dôkazov voči segmentu a organizácii, referencia produktu ≠ aktuálny problém, úspora dodávateľa sa
neprenáša, pokrytie bez falošnej pripravenosti, návrh merania vs. fakt bez zdroja, odlišné ponuky podľa nástrojov,
segment adresáta, obstarávanie (kandidát / potvrdené), ohraničený výskum medzier, učenie z odpovedí po organizáciách,
event log bez nepodloženej úspory, benefit guard, migrácia v15 a zastaranie, celý tok. Offline (falošné modely,
vyhľadávač a web). Fixtures sú syntetické – nejde o živé zákaznícke výsledky."""

import json

import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig, OrgCardsConfig, OutreachConfig, PrivacyConfig
from radar.db import DB, SCHEMA_VERSION
from radar.search import SearchResult
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI
from tests.test_m6_outreach import REAL_SENDER

SEGMENT = "malé účtovné kancelárie"
HYP = "Ručný prepis prijatých dokladov v malých účtovných kanceláriách"


def _claim(db, hid, url, problem, affected, ctype, quote, title="Dokument"):
    doc, _ = db.upsert_document(url, title, "search", "t", "q", f"h-{abs(hash(url + quote))}", f"Úvod. {quote} Záver.")
    cid = db.insert_claim(doc, 0, "t", problem, affected, ctype, quote)
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    return cid


def _world(tmp_path, **oc):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(),
                 embeddings=EmbeddingsConfig(enabled=False), privacy=PrivacyConfig(lia_completed=True),
                 outreach=OutreachConfig(sender=REAL_SENDER), org_cards=OrgCardsConfig(**oc))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis(HYP, "Účtovníci ručne prepisujú faktúry a bločky do účtovného softvéru.", SEGMENT)
    db.set_people_selection(hid, True)
    ids = {
        "eu": _claim(db, hid, "https://www.mirri.gov.sk/sprava", "Platobná jednotka ručne zosúlaďuje účtovné doklady "
                     "k žiadostiam o platbu z eurofondov", "sprostredkovateľský orgán", "pain",
                     "Sprostredkovateľský orgán ručne zosúlaďuje účtovné doklady k žiadostiam o platbu z eurofondov."),
        "proc": _claim(db, hid, "https://www.zvaz-uctovnikov.sk/prieskum", "Účtovné kancelárie ručne prepisujú doklady",
                       "účtovné kancelárie", "pain",
                       "Malé účtovné kancelárie ručne prepisujú prijaté faktúry do účtovného programu."),
        "ref": _claim(db, hid, "https://www.kros.sk/referencie/sigma", "Sigma používa KROS na spracovanie dokladov",
                      "účtovná kancelária Sigma", "existing_solution",
                      "Účtovná kancelária Sigma s.r.o. po zavedení OCR modulu spracúva doklady rýchlejšie."),
        "feat": _claim(db, hid, "https://www.kros.sk/ocr", "Modul vyťaží faktúry", "účtovné kancelárie", "pain",
                       "Modul automaticky vyťaží údaje z faktúr a ušetrí 80 % času pri zadávaní."),
    }
    rec = Recorder()
    rec.responses = {"Entailment": {"verdict": "supported", "reason": "ok"},
                     "CardCheckOut": {"support": {"verdict": "ok", "problems": []},
                                      "usability": {"verdict": "ok", "problems": []}},
                     "DraftCheck": {"verdict": "ok", "problems": []}}
    router = make_router(cfg, db, rec)
    return cfg, db, hid, ids, rec, router


def _org(db, hid, name, ico, relation="segment_match", claim=None, sector="private", web=None, nace=None):
    org, _ = db.upsert_organization(name, name.lower(), "evidence", ico=ico, sector_kind=sector, sk_nace=nace,
                                    website=web, website_verified=1 if web else 0, legal_form="spoločnosť s ručením obmedzeným")
    db.link_org_hypothesis(org, hid, relation, claim, None)
    db.add_contact_point(None, org, "email", f"info@{ico}.sk", f"https://{ico}.sk/kontakt", f"info@{ico}.sk", "regex", True)
    return org


def card_payload(tool="", deliverable="porovnanie ručného prepisu a vyťaženia na 20 dokladoch", claims=None,
                 obstacle_kind="hypothesis", benefit="mohlo by to znížiť ručnú prácu; zmerali by sme to pri skúške"):
    return {
        "process_key": "prepis-dokladov",
        "why_org": [{"text": "Je to malá účtovná kancelária v segmente.", "kind": "hypothesis",
                     "rationale": "register a segment", "falsify": "nespracúva doklady klientov"}],
        "work_case": {"trigger": {"text": "klient doručí doklady", "kind": "hypothesis", "rationale": "bežná prax"},
                      "unit": {"text": "prijatá faktúra", "kind": "hypothesis", "rationale": "segment"},
                      "steps": [{"text": "účtovník prepíše údaje z faktúry", "kind": "source_fact",
                                 "claims": claims or []}],
                      "tools": {"text": tool or "nevieme, aký program používajú", "kind": "unknown" if not tool else
                                "contact_reported"}},
        "obstacle": {"observed": [{"text": "ručný prepis zaberá čas", "kind": obstacle_kind, "claims": claims or [],
                                   "rationale": "prieskum segmentu", "falsify": "doklady už vyťažuje program"}]},
        "authority": {"who_can_act": {"text": "majiteľ kancelárie", "kind": "hypothesis", "rationale": "malá firma"},
                      "what_changes": {"text": "ako sa zadávajú doklady do programu", "kind": "proposal"}},
        "current_solution": ([{"tool": {"text": tool, "kind": "contact_reported"},
                               "gap": {"text": "nevieme, či používajú import", "kind": "unknown"}}] if tool else []),
        "options": [{"type": "tool_configuration", "title": f"nastavenie importu v {tool or 'programe'}",
                     "mechanism": "menej prepisovania", "dependencies": "prístup k programu", "downsides": "čas nastavenia"},
                    {"type": "process_change", "title": "zber dokladov v jednom formáte", "mechanism": "menej opráv",
                     "dependencies": "spolupráca klientov", "downsides": "zmena zvyku"}],
        "trial": {"input": "20 anonymizovaných faktúr a správny zápis", "output": "porovnanie chýb a času",
                  "comparison": "dnešný ručný prepis", "metric": "počet ručných opráv na doklad", "limits": "20 dokladov",
                  "stop_condition": "ak import nezníži opravy"},
        "partner_ask": {"first_step": "20 anonymizovaných faktúr", "alternative_if_no_data": "prechod posledného prípadu",
                        "ask_kind": "technical_example"},
        "main_question": {"text": "Ako ste spracovali posledné prijaté faktúry klienta?", "decision_changed": "solution"},
        "interview_questions": [{"text": "Koľko dokladov ste ručne opravovali minulý týždeň?", "decision_changed": "trial"}],
        "offer": {"deliverable": deliverable, "possible_benefit": benefit, "conditions": "bezplatná skúška",
                  "open_questions": ["aký program používajú"], "next_action": "dohodnúť 20 dokladov"},
        "example": {"today": "účtovník prepíše 20 faktúr", "after": "skontroluje 20 vyťažených faktúr"},
        "conflicts": [],
    }


# ---------------------------------------------------------------------------------------------------- 1
def test_eu_funds_accounting_is_not_evidence_of_commercial_firm_process(tmp_path):
    from radar.relevance import allowed_for
    from radar.value import domain_buckets

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    buckets = domain_buckets(db, hid)
    by_bucket = {e["id"]: name for name, items in buckets.items() for e, _ in items}
    assert by_bucket[ids["eu"]] == "context" and by_bucket[ids["proc"]] == "process_problem"
    assert ids["eu"] not in allowed_for(buckets, ("process_problem", "organization"))
    reason = next(a for name in buckets for e, a in buckets[name] if e["id"] == ids["eu"])["relevance_reason"]
    assert "eurofondy" in reason


# ---------------------------------------------------------------------------------------------------- 2
def test_product_reference_is_existing_solution_not_confirmed_current_problem(tmp_path):
    from radar.opportunity import OrgCardOut, card_inputs, code_check_card
    from radar.recipients import need_state

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    ref_org = _org(db, hid, "Sigma s.r.o.", "11111111", "uses_solution", ids["ref"])
    assert need_state(db, hid, ref_org, "uses_solution") == "solved_per_source"
    inp = card_inputs(cfg, db, ref_org, hid)
    solutions = {e["id"] for e, _ in inp["buckets"]["organization"] + inp["buckets"]["solutions"]}
    assert ids["ref"] in solutions  # dostupná pri existujúcich riešeniach (a ako zdroj o organizácii)
    ref = next(a for name in inp["buckets"] for e, a in inp["buckets"][name] if e["id"] == ids["ref"])
    assert ref["meaning"] == "post_deployment" and ref["need_state"] == "solved_per_source"
    out = OrgCardOut.model_validate(card_payload(claims=[ids["ref"]], obstacle_kind="source_fact"))
    card, checks = code_check_card(cfg, out, inp)
    observed = card["obstacle"]["observed"][0]
    assert observed["kind"] == "hypothesis" and "referencia používania" in observed["rationale"]
    assert any("referencia používania produktu" in z for z in checks["zoslabené"])


# ---------------------------------------------------------------------------------------------------- 3
def test_vendor_feature_is_for_comparison_but_its_savings_do_not_transfer(tmp_path):
    from radar.opportunity import OrgCardOut, card_inputs, code_check_card

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Alfa Účty s.r.o.", "22222222")
    inp = card_inputs(cfg, db, org, hid)
    feat = next(a for name in inp["buckets"] for e, a in inp["buckets"][name] if e["id"] == ids["feat"])
    assert feat["publisher_type"] == "vendor" and feat["meaning"] == "product_feature"
    # uložený odhad modelu („združenie“) neprebije pravidlo podľa domény (testovací beh 17. 10. 2026)
    from radar.relevance import Target, assess_claim

    db.conn.execute("UPDATE documents SET source_type = 'association' WHERE domain = 'kros.sk'")
    row = next(e for e in db.evidence_for(hid) if e["id"] == ids["feat"])
    assert row["source_type"] == "association"
    assert assess_claim(row, Target(key="t", segment=SEGMENT))["publisher_type"] == "vendor"
    assert ids["feat"] in {e["id"] for e, _ in inp["buckets"]["solutions"]}
    payload = card_payload(tool="KROS")
    payload["why_org"].append({"text": "Ušetrí 80 % času ako u dodávateľa", "kind": "source_fact", "claims": [ids["feat"]]})
    payload["current_solution"][0]["functions"] = {"text": "modul vyťaží údaje z faktúr", "kind": "source_fact",
                                                   "claims": [ids["feat"]]}
    card, checks = code_check_card(cfg, OrgCardOut.model_validate(payload), inp)
    assert any("úspora dodávateľa sa neprenáša" in p for p in checks["problémy"])
    assert card["why_org"][-1]["claims"] == [] and card["why_org"][-1]["kind"] == "hypothesis"
    assert card["current_solution"][0]["functions"]["claims"] == [ids["feat"]]  # porovnanie riešení ostáva


# ---------------------------------------------------------------------------------------------------- 4
def test_empty_process_or_unreferenced_data_needed_gives_no_false_readiness(tmp_path):
    from radar.domain import coverage, persist_brief
    from radar.opportunity import OrgCardOut, card_inputs, card_readiness, code_check_card, evaluate_card

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    key = f"hypothesis:{hid}"
    brief = {"process_steps": [], "terms": [], "roles": {}, "decision": {"text": ""}, "current_practice": {"text": ""},
             "existing_solutions": [], "alternative_explanations": [],
             "data_needed": [{"field": "dátum doručenia dokladu", "meaning_pitfall": "", "claims": [], "evidence": []}],
             "falsifying_questions": [], "open_questions": [{"question": "Ako prebieha zadávanie?", "about": "process",
                                                             "target_role": "customer", "blocks_action": "none"}],
             "unknowns": []}
    bid = db.add_brief(key, HYP, brief, [], "x", {}, "ok", None, "test")
    persist_brief(db, key, bid, brief, [])
    data_claims = [c for c in db.domain_claims(key) if c["part"] == "data"]
    assert data_claims and all(c["status"] == "proposal" for c in data_claims)  # návrh vstupu, nie source_fact
    cov, status = coverage(db, key)
    assert status != "ready" and cov["process"] in ("len otázka", "chýba") and cov["data"] != "podložené"
    org = _org(db, hid, "Beta Účty s.r.o.", "33333333")
    empty = OrgCardOut.model_validate({"process_key": "x", "main_question": {"text": "Ako?", "decision_changed": "trial"}})
    card, checks = code_check_card(cfg, empty, card_inputs(cfg, db, org, hid))
    grades = {e["kľúč"]: e["stav"] for e in evaluate_card(card)}
    assert grades["úkon"] == grades["prekážka"] == grades["zásah"] == grades["metrika"] == "chýba"
    assert any("skúška neúplná" in p for p in checks["problémy"]) and any("kandidátny zásah" in p for p in checks["problémy"])
    cid = db.add_org_card(org, hid, "x", "customer", card, inputs_hash="h", status="needs_review")
    ready = card_readiness(cfg, db, db.get_org_card(cid))
    assert not ready["skúška"]["pripravené"] and not ready["prínos"]["pripravené"]


# ---------------------------------------------------------------------------------------------------- 5
def test_reasoned_measurement_proposal_passes_but_same_content_as_unsourced_fact_does_not(tmp_path):
    from radar.opportunity import OrgCardOut, card_inputs, code_check_card
    from radar.prompts import ORG_CARD_CHECK_SYSTEM

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Gama Účty s.r.o.", "44444444")
    payload = card_payload()
    payload["obstacle"]["observed"] = [
        {"text": "Skúsime zmerať, ktoré typy dokladov potrebujú najviac ručných opráv.", "kind": "hypothesis",
         "rationale": "rozdelenie opráv zatiaľ nepoznáme", "falsify": "opravy sú rovnomerné"},
        {"text": "Vaše zahraničné faktúry spôsobujú väčšinu chýb.", "kind": "source_fact", "claims": []}]
    card, checks = code_check_card(cfg, OrgCardOut.model_validate(payload), card_inputs(cfg, db, org, hid))
    measure, fact = card["obstacle"]["observed"]
    assert measure["kind"] == "hypothesis" and not any("Skúsime zmerať" in p for p in checks["problémy"])
    assert fact["kind"] == "hypothesis" and any("zahraničné faktúry" in z for z in checks["zoslabené"])
    assert any("hypotéza bez odôvodnenia (obstacle.observed: Vaše zahraničné" in p for p in checks["problémy"])
    assert "MEASURE" in ORG_CARD_CHECK_SYSTEM and "do not reject it for missing proof" in ORG_CARD_CHECK_SYSTEM


# ---------------------------------------------------------------------------------------------------- 6
def test_two_orgs_with_same_domain_and_different_tools_get_different_offers(tmp_path):
    from radar.opportunity import ensure_card, offer_for
    from radar.outcomes import record_outcome

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    a = _org(db, hid, "Delta Účty s.r.o.", "55555555")
    b = _org(db, hid, "Epsilon Účty s.r.o.", "66666666")
    record_outcome(cfg, db, hid=hid, org_id=a, reply="relevant", structured={"tool_used": "Pohoda"})
    record_outcome(cfg, db, hid=hid, org_id=b, reply="relevant", structured={"tool_used": "Money S3"})
    prompts = []

    def card_from_prompt(messages):
        user = messages[-1]["content"]
        prompts.append(user)
        tool = "Pohoda" if "Pohoda" in user else "Money S3" if "Money S3" in user else ""
        return card_payload(tool=tool, deliverable=f"nastavenie importu dokladov v programe {tool}")

    rec.responses["OrgCardOut"] = card_from_prompt
    ca, cb = ensure_card(cfg, db, router, a, hid), ensure_card(cfg, db, router, b, hid)
    assert ca and cb and ca != cb
    assert "Pohoda" in prompts[0] and "Money S3" not in prompts[0]  # odpoveď inej organizácie do karty nevstúpi
    oa, ob = offer_for(db, a, hid, "customer"), offer_for(db, b, hid, "customer")
    assert oa["výstup"] != ob["výstup"] and "Pohoda" in oa["výstup"] and "Money S3" in ob["výstup"]
    tools_a = json.loads(db.get_org_card(ca)["card_json"])["current_solution"][0]["tool"]["text"]
    assert tools_a == "Pohoda"


# ---------------------------------------------------------------------------------------------------- 7
def test_wrong_segment_is_not_customer_but_data_or_expert_role_can_stay(tmp_path):
    from radar.shortlist import contact_shortlist

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    nbs = _org(db, hid, "Národná banka Slovenska", "30844789", "segment_match", sector="public")
    stat = _org(db, hid, "Štatistický úrad SR", "00166197", "data_holder", sector="public")
    firm = _org(db, hid, "Zeta Účty s.r.o.", "77777777")
    rows = {r["org_id"]: r for r in contact_shortlist(cfg, db, hid)}
    assert rows[nbs]["odporúčanie"] == "preveriť rolu" and not rows[nbs]["prvé_oslovenie"]
    assert "nesúlad so segmentom" in rows[nbs]["dôvody"] and rows[nbs]["zhoda_segmentu"].startswith("nie")
    assert rows[stat]["skupina"] == "data_holder" and rows[stat]["odporúčanie"] != "preveriť rolu"
    assert rows[firm]["skupina"] == "customer" and rows[firm]["odporúčanie"] != "preveriť rolu"
    # SK NACE mimo filtra segmentu (filter navrhol model) nie je dôvod na vyradenie – iba „neznáme, over“ (test 17. 10.)
    db.conn.execute("UPDATE hypotheses SET segment_filter_json = ? WHERE id = ?", ('{"sk_nace_prefixes": ["6920"]}', hid))
    other = _org(db, hid, "Tau Poisťovňa a.s.", "90909090", nace="8430")
    row = {r["org_id"]: r for r in contact_shortlist(cfg, db, hid)}[other]
    assert row["zhoda_segmentu"].startswith("neznáme") and row["odporúčanie"] != "preveriť rolu"
    assert {r["org_id"]: r for r in contact_shortlist(cfg, db, hid)}[firm]["zhoda_segmentu"].startswith("áno")


# ---------------------------------------------------------------------------------------------------- 8
def test_generic_procurement_hub_is_only_candidate_and_specific_active_record_keeps_rules(tmp_path):
    from datetime import date

    from radar.outreach import OutreachError, approve, run_drafts, send_state
    from radar.recipients import assess_procurement, set_procurement

    hub = assess_procurement("https://www.uvo.gov.sk/pripravne-trhove-konzultacie", "Prípravné trhové konzultácie",
                             "Zoznam konzultácií", HYP, date(2026, 10, 17))
    assert hub["status"] == "candidate" and "rozcestník" in hub["reason"]
    specific = assess_procurement("https://www.uvo.gov.sk/vestnik/zakazka/12345", "Spracovanie účtovných dokladov "
                                  "– vyťaženie faktúr", "Lehota na predkladanie ponúk do 30. 11. 2026, účtovných dokladov",
                                  HYP, date(2026, 10, 17))
    assert specific["status"] == "confirmed" and specific["date"] == "2026-11-30"
    old = assess_procurement("https://www.uvo.gov.sk/vestnik/zakazka/999", "Spracovanie účtovných dokladov",
                             "Lehota na predkladanie ponúk do 1. 3. 2025", HYP, date(2026, 10, 17))
    assert old["status"] == "closed"

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Eta Účty s.r.o.", "88888888")
    db.set_org_hypothesis(org, hid, procurement_status="candidate", procurement_url=hub["url"],
                          procurement_note=hub["reason"])
    rec.responses["DraftOut"] = {"subject": "Otázka k dokladom", "facts_used": [],
                                 "body": "zaujíma ma, ako ste spracovali posledné prijaté faktúry klienta?"}
    run_drafts(cfg, db, router, Deadline(1), [hid])
    row = db.outreach_for_org(org, hid)[0]
    assert row["status"] != "blocked"  # návrh smie vzniknúť
    state, reasons = send_state(cfg, db, row)
    assert state == "procurement_unverified" and "Koho osloviť" in reasons[0]
    if row["status"] == "draft":
        with pytest.raises(OutreachError):
            approve(cfg, db, row["id"])
    set_procurement(db, org, hid, "none", "overené")
    assert send_state(cfg, db, db.get_outreach(row["id"]))[0] != "procurement_unverified"
    set_procurement(db, org, hid, "confirmed", "overené")  # potvrdené: nové návrhy sa blokujú (oficiálna cesta)
    db.conn.execute("DELETE FROM outreach WHERE org_id = ?", (org,))
    run_drafts(cfg, db, router, Deadline(1), [hid])
    assert [r["status"] for r in db.outreach_for_org(org, hid)] in ([], ["blocked"])


# ---------------------------------------------------------------------------------------------------- 9
def test_targeted_research_respects_budget_cache_stop_and_keeps_question_on_outage(tmp_path):
    from radar.opportunity import run_research

    cfg, db, hid, ids, rec, router = _world(tmp_path, research_max_tasks_per_card=2, research_max_queries_per_task=1,
                                            research_max_documents_per_task=1)
    org = _org(db, hid, "Theta Účty s.r.o.", "99999999", web="https://theta.sk/")
    card = card_payload()
    cid = db.add_org_card(org, hid, "prepis-dokladov", "customer", card, inputs_hash="h", status="checked")
    from radar.opportunity import plan_research

    many_unknowns = {**card, "why_org": card["why_org"] + [{"text": f"Nevieme {i}. vec o kancelárii", "kind": "unknown"}
                                                          for i in range(12)]}
    from radar.opportunity import MAX_GAPS, card_gaps

    gaps = card_gaps(many_unknowns, "Theta", "prepis")
    assert len(gaps) <= MAX_GAPS and sum(g["question"].startswith("Zistiť") for g in gaps) <= 2  # zlúčené, nie 12 úloh
    assert plan_research(cfg, db, cid) >= 3
    tasks = db.research_tasks_for(cid)
    internal = [t for t in tasks if t["best_source"] in ("contact", "sample", "event_log")]
    public = [t for t in tasks if t["best_source"] in ("public_doc", "tool_docs")]
    for t in public:  # krátke dopyty s názvom bez právnej formy, nie slová otázky (testovací beh 17. 10. 2026)
        for q in json.loads(t["queries_json"]):
            assert "s.r.o." not in q and "Ktor" not in q and len(q.split()) <= 6, q
    from radar.opportunity import base_name, research_queries

    assert base_name("X & Y Účty, s.r.o.") == "X & Y Účty" and base_name("Beta a.s.") == "Beta"
    from radar.opportunity import _known_tools

    assert _known_tools({"current_solution": [{"tool": {"text": "Docspoint (podľa zdroja) alebo iný účtovný systém.",
                                                         "kind": "source_fact"}}]}) == ["Docspoint"]
    assert research_queries("current_solution", "tool_docs", "X s.r.o.", "", "prepis dokladov", ["Pohoda"])[0] == \
        "Pohoda prepis dokladov funkcie"
    assert internal and all(not json.loads(t["queries_json"]) for t in internal)  # interné otázky sa nehľadajú
    page = ("Theta Účty s.r.o. spracúva účtovníctvo pre živnostníkov. Doklady klientov zadávame do programu Pohoda "
            "a kontrolujeme ich podľa interných postupov kancelárie pri každom mesačnom uzávierke.")
    searx = FakeSearx(lambda q: [SearchResult("https://theta.sk/sluzby", "Služby", ""),
                                 SearchResult("https://theta.sk/o-nas", "O nás", "")])
    fetcher = FakeFetcher(texts={"https://theta.sk/sluzby": page, "https://theta.sk/o-nas": page + " Tím."})
    rec.responses["GapAnswerOut"] = lambda m: {"status": "found", "answer": "používajú Pohodu",
                                               "excerpts": [int(x) for x in __import__("re").findall(r"\[(\d+)\]",
                                                                                                    m[-1]["content"])[:1]]}
    stats = run_research(cfg, db, router, cid, searx, fetcher, Deadline(1))
    assert stats["úlohy"] <= 2 and stats["dopyty"] <= 2 and stats["dokumenty"] <= 2
    for _ in range(5):  # zvyšné verejné úlohy v ďalších behoch (limit na beh), potom už nič
        if not run_research(cfg, db, router, cid, searx, fetcher, Deadline(1))["úlohy"]:
            break
    n_queries = len(searx.queries)
    again = run_research(cfg, db, router, cid, searx, fetcher, Deadline(1))
    assert again["úlohy"] == 0 and len(searx.queries) == n_queries  # pokus sa neopakuje (limit pokusov)
    assert all(t["attempts"] <= 1 for t in db.research_tasks_for(cid))
    cached = [json.loads(t["cost_json"] or "{}").get("z_cache", 0) for t in db.research_tasks_for(cid) if t["attempts"]]
    assert any(cached)  # neskoršia úloha použila odseky webu organizácie stiahnuté skoršou (bez nového sťahovania)
    assert len(fetcher.fetched) == len(set(fetcher.fetched))  # už stiahnutá URL organizácie sa nesťahuje znova
    found = [t for t in db.research_tasks_for(cid) if t["status"] == "found"]
    assert found and all(json.loads(t["sources_json"]) for t in found)
    assert {e["scope"] for e in db.domain_evidence(f"org:{org}")} == {"organization"}  # vlastný web
    # rovnaký názov na cudzej stránke bez IČO nie je podklad „o organizácii“ a nález z neho je iba čiastočný
    namesake = _org(db, hid, "Lambda Kancelária", None)
    cidn = db.add_org_card(namesake, hid, "menovec", "customer", card, inputs_hash="hn", status="checked")
    plan_research(cfg, db, cidn)
    fetcher.texts["https://lambda-app.example/"] = ("Lambda Kancelária je mobilná aplikácia na plánovanie kancelárie "
                                                     "a zdieľanie dokladov medzi kolegami, ktorú vyvíja iný tím.")
    run_research(cfg, db, router, cidn, FakeSearx(lambda q: [SearchResult("https://lambda-app.example/", "App", "")]),
                 fetcher, Deadline(1))
    assert {e["scope"] for e in db.domain_evidence(f"org:{namesake}")} <= {"sector"}
    assert not [t for t in db.research_tasks_for(cidn) if t["status"] == "found"]
    # výpadok vyhľadávača pri organizácii bez uložených podkladov: otázka ostáva bez vymyslenej odpovede
    # (organizácia s uloženými odsekmi z vlastného webu ich smie použiť aj počas výpadku – to nie je vymyslená odpoveď)
    org2 = _org(db, hid, "Kapa Účty s.r.o.", "12121212", web="https://kapa.sk/")
    cid2 = db.add_org_card(org2, hid, "iny-pripad", "customer", card, inputs_hash="h2", status="checked")
    plan_research(cfg, db, cid2)

    class Down:
        queries: list = []

        def search(self, q, limit=None):
            raise ConnectionError("SearXNG nebeží")

    calls_before = len(rec.models_for("GapAnswerOut"))
    run_research(cfg, db, router, cid2, Down(), fetcher, Deadline(1))
    public = [t for t in db.research_tasks_for(cid2) if t["attempts"]]
    assert public and all(t["status"] == "unavailable" and not json.loads(t["result_json"])["answer"] for t in public)
    assert len(rec.models_for("GapAnswerOut")) == calls_before
    # odpoveď modelu bez platného úryvku sa neuloží ako nález; časový limit zastaví výskum
    cid3 = db.add_org_card(org, hid, "treti-pripad", "customer", card, inputs_hash="h3", status="checked")
    plan_research(cfg, db, cid3)
    rec.responses["GapAnswerOut"] = {"status": "found", "answer": "vymyslené", "excerpts": [99999]}
    fetcher.texts["https://theta.sk/nove"] = page + " Nová stránka."
    searx.results = lambda q: [SearchResult("https://theta.sk/nove", "Nové", "")]
    run_research(cfg, db, router, cid3, searx, fetcher, Deadline(1))
    assert not [t for t in db.research_tasks_for(cid3) if t["status"] == "found"]
    cfg.org_cards.research_max_seconds_per_card = 0
    cid4 = db.add_org_card(org, hid, "stvrty-pripad", "customer", card, inputs_hash="h4", status="checked")
    plan_research(cfg, db, cid4)
    assert "časový limit" in run_research(cfg, db, router, cid4, searx, fetcher, Deadline(1)).get("ukončenie", "")


# ---------------------------------------------------------------------------------------------------- 10
def test_reply_of_one_org_does_not_change_another_no_reply_is_not_refutation_conflict_after_approval_visible(tmp_path):
    from radar.opportunity import approve_card
    from radar.outcomes import record_outcome
    from radar.outreach import readiness_of
    from radar.recipients import need_state

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    a = _org(db, hid, "Iota Účty s.r.o.", "10101010")
    b = _org(db, hid, "Kappa Účty s.r.o.", "20202020")
    ca = db.add_org_card(a, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="a", status="checked")
    cb = db.add_org_card(b, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="b", status="checked")
    approve_card(db, ca)
    record_outcome(cfg, db, hid=hid, org_id=b, reply="none")
    assert need_state(db, hid, b, None) == "hypothesis"  # bez odpovede ≠ vyvrátenie
    out = record_outcome(cfg, db, hid=hid, org_id=a, reply="not_relevant", problem_relevance="refuted",
                         decline_reason="already_solved", card_id=ca, structured={"tool_used": "Pohoda s importom"})
    assert out["karty"]["karty_na_obnovu"] == 1
    assert need_state(db, hid, a, None) == "refuted" and need_state(db, hid, b, None) == "hypothesis"
    row_a, row_b = db.get_org_card(ca), db.get_org_card(cb)
    assert row_a["stale_reason"] and row_a["approved_at"] and json.loads(row_a["conflict_json"])["outcome_id"] == out["id"]
    assert row_b["stale_reason"] is None and row_b["conflict_json"] is None
    o = next(x for x in db.outcomes_for(hid) if x["id"] == out["id"])
    assert o["card_version"] == 1 and o["decline_reason"] == "already_solved" and o["tool_used"] == "Pohoda s importom"
    with pytest.raises(Exception):
        record_outcome(cfg, db, hid=hid, org_id=b, reply="relevant", card_id=ca)  # karta inej organizácie
    pkg = {"prečo_tento_kontakt": {"zdroj": "x"}, "karta": {"rozpor": json.loads(row_a["conflict_json"])}}
    state, reasons = readiness_of(pkg, {"problems": []}, "Predmet", "Text")
    assert state == "needs_input" and any("rozpor" in r for r in reasons)


# ---------------------------------------------------------------------------------------------------- 11
def test_timestamps_only_event_log_gives_no_working_time_saving_and_missing_log_allows_other_trial(tmp_path):
    from radar.event_logs import EventLogError, build_request, card_log_need
    from radar.opportunity import OrgCardOut, card_inputs, card_readiness, code_check_card

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Lambda Účty s.r.o.", "30303030")
    db.add_eventlog_import(org, "abc", hypothesis_id=hid, status="checked", retained_rows=40,
                           availability_json={"odstupy": {"dostupné": True}, "čas_aktivity": {"dostupné": False}})
    payload = card_payload(benefit="ušetrí pracovný čas účtovníkov")
    payload["obstacle"]["observed"].append({"text": "účtovníčka denne pracuje 3 hodiny na prepise", "kind": "data_observed"})
    card, checks = code_check_card(cfg, OrgCardOut.model_validate(payload), card_inputs(cfg, db, org, hid))
    assert any("prínos je tvrdený ako istý" in p for p in checks["problémy"])
    assert any("čísla bez podkladu" in p and "3" in p for p in checks["problémy"])
    cid = db.add_org_card(org, hid, "prepis-dokladov", "customer", card, inputs_hash="x", status="needs_review")
    ready = card_readiness(cfg, db, db.get_org_card(cid))
    assert not ready["prínos"]["pripravené"] and ready["skúška"]["pripravené"]  # skúška bez event logu je možná
    need, why, alts = card_log_need(card)
    assert not need and "documents" in alts  # vyťažovanie dokladov: dokumenty a správny výsledok
    handover = {**card, "main_question": {"text": "Ako klient odovzdá doklady a ako prebieha ich prepis?"}}
    assert not card_log_need(handover)[0]  # „odovzdá“, „prebieha“ pri dokladoch nestačia (testovací beh 17. 10.)
    with pytest.raises(EventLogError, match="Alternatívy"):
        build_request(cfg, db, hid, org, card_id=cid)
    flow = card_payload()
    flow["trial"]["metric"] = "odstup medzi doručením a zaúčtovaním dokladu (koľko dní)"
    cid2 = db.add_org_card(org, hid, "priebeh-uctovania", "customer", flow, inputs_hash="y", status="checked")
    assert card_log_need(flow)[0]
    rid = build_request(cfg, db, hid, org, card_id=cid2)
    assert db.get_eventlog_request(rid)["card_id"] == cid2


# ---------------------------------------------------------------------------------------------------- 12
def test_result_worse_than_benchmark_or_wrong_entity_semantics_cannot_support_benefit_claim(tmp_path):
    from radar.opportunity import card_readiness, link_experiment

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Mi Účty s.r.o.", "40404040")
    cid = db.add_org_card(org, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="x", status="checked")

    def exp(prekonal, platne, flags=None, spec=None):
        eid = db.add_experiment(hid, org, None, "Vyťaženie zníži opravy", "opravy", "ručne", "lepšie o 20 %",
                                spec or {"dataset": "faktury_org"})
        db.update_experiment(eid, status="done", result_json={"osi": {
            "predregistrácia": {"splnené": True}, "silné_porovnanie": {"prekonal": prekonal, "naivný": "ručne"},
            "metodika": {"platné": platne, "problémy": [] if platne else ["entita štítka nie je doklad"]}}},
            flags_json={"výsledok_používateľa": {"stav": "potvrdené"}, **(flags or {})})
        link_experiment(db, eid, cid)
        return eid

    worse = exp(False, True)
    wrong = exp(True, False)
    synthetic = exp(True, True, {"synthetic": True})
    ready = card_readiness(cfg, db, db.get_org_card(cid))["prínos"]
    assert not ready["pripravené"]
    text = " ".join(ready["chýba"])
    assert f"E{worse}" in text and "neprekonal" in text and f"E{wrong}" in text and "metodika" in text
    assert f"E{synthetic}" in text and "syntetické" in text
    good = exp(True, True)
    ready = card_readiness(cfg, db, db.get_org_card(cid))["prínos"]
    assert ready["pripravené"] and f"E{good}" in " ".join(ready["dôvody"])
    unlinked = db.add_org_card(org, hid, "iny-ukon", "customer", card_payload(), inputs_hash="z", status="checked")
    assert not card_readiness(cfg, db, db.get_org_card(unlinked))["prínos"]["pripravené"]  # výsledok k inému úkonu


# ---------------------------------------------------------------------------------------------------- 13
def test_migration_v15_keeps_data_and_version_change_marks_only_dependent_outputs(tmp_path):
    from radar.opportunity import mark_stale

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Ny Účty s.r.o.", "50505050")
    ref_org = _org(db, hid, "Sigma s.r.o.", "60606060", "mentioned_with_problem", ids["ref"])
    db.set_org_hypothesis(org, hid, procurement_active=1,
                          procurement_note="Možné prebiehajúce obstarávanie: https://www.uvo.gov.sk/ptk")
    pid = db.add_value_proposal(hid, "customer", {"deliverable": "x", "schválený_záväzok": "20 riadkov do 10 dní"},
                                status="approved", delivery_commitment_approved=1)
    db.add_domain_claim(f"hypothesis:{hid}", "data", "dátum doručenia", "source_fact", {"claims": [], "evidence": []})
    oid = db.add_outcome(hid, org_id=org, reply="relevant", problem_relevance="confirmed", note="urgujú denne")
    counts = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("claims", "organizations", "value_proposals", "outreach_outcomes", "domain_claims")}
    db.conn.execute("PRAGMA user_version = 14")
    db.conn.commit()
    db.close()
    db = DB(cfg.db_path)
    assert db.migration["from"] == 14 and db.migration["to"] == SCHEMA_VERSION and db.migration["backup"]
    assert counts == {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}
    oh = db.org_hypothesis(org, hid)
    assert oh["procurement_status"] == "candidate" and oh["procurement_active"] == 0
    assert oh["procurement_url"] == "https://www.uvo.gov.sk/ptk" and "v15: neoverené" in oh["procurement_note"]
    assert db.org_hypothesis(ref_org, hid)["relation"] == "uses_solution"
    assert db.get_value_proposal(pid)["delivery_commitment_approved"] == 1
    assert next(o for o in db.outcomes_for(hid) if o["id"] == oid)["note"] == "urgujú denne"
    assert all(c["status"] == "proposal" for c in db.domain_claims(f"hypothesis:{hid}") if c["part"] == "data")
    # zmena verzie promptu: iba karta vytvorená starou verziou sa označí; nič sa neregeneruje
    from radar.prompts import prompt_version
    from radar.value import policy_version

    new = db.add_org_card(org, hid, "a", "customer", card_payload(), inputs_hash="a", status="checked",
                          schema_version="card-1", prompt_version=prompt_version("org_card"),
                          policy_version=policy_version(cfg))
    old = db.add_org_card(ref_org, hid, "a", "customer", card_payload(), inputs_hash="b", status="checked",
                          schema_version="card-1", prompt_version="starý", policy_version=policy_version(cfg))
    calls = len(rec.calls)
    assert mark_stale(cfg, db) == 1
    assert db.get_org_card(old)["stale_reason"].startswith("zmena verzie") and db.get_org_card(new)["stale_reason"] is None
    assert len(rec.calls) == calls


# ---------------------------------------------------------------------------------------------------- 14
def test_full_flow_evidence_card_gaps_trial_draft_reply_new_version_export(tmp_path):
    from radar.opportunity import card_markdown, current_card, ensure_card, run_research, select_trial
    from radar.outcomes import record_outcome
    from radar.outreach import run_drafts

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Omikron Účty s.r.o.", "70707070", web="https://omikron.sk/")
    pid = db.insert_person(org, "Oľga Testová", "olga testova", "https://omikron.sk/tim", "Oľga Testová, konateľka",
                           hid, role_title="konateľka")
    db.add_contact_point(pid, org, "email", "olga@omikron.sk", "https://omikron.sk/tim",
                         "Oľga Testová, konateľka – olga@omikron.sk", "regex")
    db.set_link_priority(db.upsert_link(pid, hid, "org_affected", ids["proc"], "https://www.zvaz-uctovnikov.sk/prieskum"),
                         0.9, {"role_fit": 1.0})
    prompts = []
    rec.responses["OrgCardOut"] = lambda m: prompts.append(m[-1]["content"]) or card_payload(claims=[ids["proc"]])
    rec.responses["GapAnswerOut"] = {"status": "not_found", "answer": "", "excerpts": []}
    rec.responses["DraftOut"] = {"subject": "Spracovanie faktúr", "facts_used": [],
                                 "body": "zaujíma ma, ako ste spracovali posledné prijaté faktúry klienta?"}
    cid = ensure_card(cfg, db, router, org, hid)
    row = db.get_org_card(cid)
    assert row["version"] == 1 and row["status"] == "checked" and db.research_tasks_for(cid)
    assert "Oľga" not in prompts[0] and "olga@" not in prompts[0]  # karta bez mien a kontaktov ľudí
    run_research(cfg, db, router, cid, FakeSearx(), FakeFetcher(), Deadline(1))
    select_trial(cfg, db, cid, 1)
    assert json.loads(db.get_org_card(cid)["readiness_json"])["skúška"]["pripravené"]
    run_drafts(cfg, db, router, Deadline(1), [hid])
    draft = db.outreach_for_person(pid, hid)[0]
    pkg = json.loads(draft["package_json"])
    assert draft["card_id"] == cid and pkg["karta"]["card_version"] == 1 and pkg["výstup"] == card_payload()["offer"]["deliverable"]
    assert pkg["hlavná_otázka"]["text"] == card_payload()["main_question"]["text"]
    record_outcome(cfg, db, outreach_id=draft["id"], reply="relevant", problem_relevance="confirmed",
                   structured={"last_case": "minulý týždeň 40 faktúr ručne", "tool_used": "Pohoda"}, interview_agreed=True,
                   willingness={"stage": "not_asked"})
    assert db.get_org_card(cid)["stale_reason"]
    cid2 = ensure_card(cfg, db, router, org, hid)
    assert cid2 != cid and db.get_org_card(cid2)["version"] == 2 and db.get_org_card(cid)["status"] == "superseded"
    assert "minulý týždeň 40 faktúr ručne" in prompts[-1] and "Oľga" not in prompts[-1]
    assert current_card(db, org, hid)["id"] == cid2
    md = card_markdown(db, db.get_org_card(cid2))
    for part in ("Päť odpovedí", "Na rozhovor", "Na návrh skúšky", "Na tvrdenie o prínose", "Ponuka", "Medzery a cielený "
                 "výskum", "ILUSTRÁCIA", "verzia 2", "Hodnotenie karty"):
        assert part in md, part
    assert "Oľga" not in md and "olga@" not in md


# ---------------------------------------------------------------------------------------------------- GUI a väzba
def test_gate0_value_tab_shows_card_answers_readiness_and_no_model_call_on_render(gui):
    """Brána 0 → Doména a prínos: výber organizácie, päť odpovedí, tri pripravenosti, ďalší krok; vykreslenie
    nevolá model (karta vzniká iba úlohou na pozadí)."""
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.opportunity import card_readiness
    from radar.shortlist import contact_shortlist
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    org = next(r["org_id"] for r in contact_shortlist(cfg, d, hid) if r["org_id"])
    cid = d.add_org_card(org, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="g", status="checked")
    d.update_org_card(cid, readiness_json=card_readiness(cfg, d, d.get_org_card(cid)))
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate0.py").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Komu pomáham" in text and "Na rozhovor" in text and "Na tvrdenie o prínose" in text
    assert any("Ďalší krok" in i.value for i in at.info)
    d = DB(cfg.db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()


def test_people_functions_stay_local_when_card_runs_in_cloud_without_personal_data_permission(tmp_path):
    """Vyhodnotenie 17. 10. 2026: funkcia „Konateľ“ malej s.r.o. (bez mena) išla do Azure – z registra je to
    dohľadateľná osoba. V cloude bez privacy.cloud_personal_data sa funkcie ľudí do karty nedávajú."""
    from radar.opportunity import card_inputs

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Pi Účty s.r.o.", "80808080")
    db.insert_person(org, "Peter Konateľ", "peter konatel", "https://pi.sk/o-nas", "Peter Konateľ, konateľ", hid,
                     role_title="konateľ")
    assert card_inputs(cfg, db, org, hid, router=router)["profile"]["funkcie_kontaktov"] == ["konateľ"]  # lokálne áno
    router.is_cloud = lambda role: role == "accurate"  # presná rola v cloude (ako sandbox variantu B)
    assert card_inputs(cfg, db, org, hid, router=router)["profile"]["funkcie_kontaktov"] == []
    cfg.privacy.cloud_personal_data = True
    assert card_inputs(cfg, db, org, hid, router=router)["profile"]["funkcie_kontaktov"] == ["konateľ"]


def test_old_draft_without_card_is_flagged_and_can_be_replaced_by_card_draft(tmp_path):
    """Testovací beh 21. 10. 2026: kontakt so starým návrhom (pred v15) nedostal ponuku z karty – návrh sa nevytvára,
    ak už existuje. Teraz upozornenie a nahradenie neodoslaného, neupraveného návrhu."""
    from radar.outreach import OutreachError, discard_for_card, run_drafts, send_state

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    org = _org(db, hid, "Rho Účty s.r.o.", "31313131")
    old = db.insert_outreach(None, hid, "email", "B", "Otázka", "Dobrý deň, starý text.", [], {"ok": True}, "draft",
                             org_id=org)
    assert send_state(cfg, db, db.get_outreach(old))[1] == []  # bez karty nič
    db.add_org_card(org, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="r", status="checked")
    reasons = send_state(cfg, db, db.get_outreach(old))[1]
    assert any("nevychádza z aktuálnej karty" in r for r in reasons)
    edited = db.insert_outreach(None, hid, "email", "A", "Otázka", "Upravené", [], {"ok": True, "edited": True}, "draft",
                                org_id=org)
    with pytest.raises(OutreachError):
        discard_for_card(db, edited)  # ručná úprava sa nemaže
    db.conn.execute("DELETE FROM outreach WHERE id = ?", (edited,))
    discard_for_card(db, old)
    rec.responses["DraftOut"] = {"subject": "Spracovanie faktúr", "facts_used": [],
                                 "body": "zaujíma ma, ako ste spracovali posledné prijaté faktúry klienta?"}
    run_drafts(cfg, db, router, Deadline(1), [hid])
    new = db.outreach_for_org(org, hid)
    assert new and all(n["card_id"] for n in new) and not any("nevychádza" in r for n in new
                                                                for r in send_state(cfg, db, n)[1])


def test_org_from_existing_solution_quote_is_linked_as_uses_solution(tmp_path):
    from radar.orgs import OrgFinder

    cfg, db, hid, ids, rec, router = _world(tmp_path)
    rec.responses["OrgMentions"] = lambda m: {"organizations": [{"name": "Sigma s.r.o.", "relation": "affected"}]
                                              if "Sigma" in m[-1]["content"].split("<untrusted_document>")[0] else []}
    finder = OrgFinder(cfg, db, router, FakeSearx(), FakeFetcher(), None, None, Deadline(1))
    finder.from_evidence(db.get_hypothesis(hid))
    linked = [o for o in db.orgs_for_hypothesis(hid) if "Sigma" in o["name"]]
    assert linked and linked[0]["relation"] == "uses_solution"
    assert "nie potvrdená aktuálna potreba" in linked[0]["fit_json"]


def test_gendered_aby_som_form_is_flagged():
    """Testovací beh 21. 10. 2026: „aby som sa mohol obrátiť“ v návrhu bez nastaveného rodu odosielateľa prešlo kontrolou."""
    from radar.outreach import GENDERED_FIRST_PERSON_RE

    assert GENDERED_FIRST_PERSON_RE.search("Prosím o kontakt, aby som sa mohol obrátiť na správnu osobu.")
    assert GENDERED_FIRST_PERSON_RE.search("aby som si mohla dohodnúť termín")
    assert not GENDERED_FIRST_PERSON_RE.search("Chcem sa opýtať, kto je zodpovedný, aby sa návrh dostal k správnej osobe.")
