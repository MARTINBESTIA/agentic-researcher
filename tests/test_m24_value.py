"""M24 – prehľad domény a návrh prínosu (audit 15. 10. 2026, balík B): citované tvrdenia, neznáme namiesto
vymyslených medzier, tri vyvracajúce otázky, zdieľanie v doméne, nová verzia iba pri zmene podkladov, návrh prínosu
ako hypotéza bez sľubov, hodnotiteľ v samostatnej role. Offline (falošný model)."""

import json

import pytest

from radar.db import DB
from radar.value import (
    approve_commitment, best_proposal, brief_markdown, domain_of, ensure_brief, ensure_value_proposals, run_value,
)
from radar.util import Deadline
from tests.test_m18_contacts import setup


def claim(db, hid, text, quote, n=[0]):
    n[0] += 1
    doc, _ = db.upsert_document(f"https://zdroj{n[0]}.sk/a", "T", "search", "t", "q", f"h{n[0]}", quote * 3)
    cid = db.insert_claim(doc, 0, "t", text, "výrobcovia", "pain", quote, [])
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    return cid


def brief_response(c1, c2):
    return {
        "process_steps": [{"text": "Zákazník objedná dielec s termínom.", "claims": [c1]},
                          {"text": "Dodávateľ posiela polotovar neskoro.", "claims": [c2]},
                          {"text": "Plánovač ručne prepláva výrobu.", "claims": []}],  # bez citátu → neznáme
        "terms": [{"term": t, "explanation": f"vysvetlenie {t}", "claims": [c1]} for t in
                  ("termín dodania", "polotovar", "kapacita", "urgencia", "pôvodný plán")],
        "roles": {"user": {"text": "plánovač výroby", "claims": [c2]}, "data_steward": {"text": "", "claims": []},
                  "decision_maker": {"text": "konateľ", "claims": [999]}},  # cudzí citát → neznáme
        "decision": {"text": "Ktoré zákazky urgovať tento týždeň.", "claims": [c2]},
        "current_practice": {"text": "Excel a telefonáty.", "claims": [c1]},
        "data_needed": [{"field": "pôvodný termín", "meaning_pitfall": "zmenený termín nie je pôvodný prísľub",
                         "claims": [c2]}],
        "falsifying_questions": [{"aspect": "problem_exists", "question": "Aké rozhodnutie naposledy skomplikovalo meškanie?"},
                                 {"aspect": "already_solved", "question": "Čo dnešný systém nezvládol?"},
                                 {"aspect": "data_enables", "question": "Máte pôvodný prísľub aj históriu zmien?"}],
        "unknowns": ["Či majú históriu zmien termínov."]}


def _env(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    cfg.models.roles["reviewer"] = cfg.models.roles["reviewer"].model_copy(update={"model": "hodnotitel-model"})
    from tests.fakes import make_router

    router = make_router(cfg, db, rec)
    c1 = claim(db, hid, "Dodávky meškajú.", "Dodávky dielcov meškajú o týždne.")
    c2 = claim(db, hid, "Plánovanie trpí.", "Plánovači ručne urgujú dodávateľov každý deň.")
    rec.responses["DomainBriefOut"] = brief_response(c1, c2)
    rec.responses["ReviewOut"] = {"verdict": "ok", "problems": []}
    return cfg, db, hid, rec, router, c1, c2


def test_brief_cites_evidence_moves_gaps_to_unknowns_and_has_three_questions(tmp_path):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    bid = ensure_brief(cfg, db, router, hid)
    row = db.get_brief(bid)
    brief = json.loads(row["brief_json"])
    assert row["status"] == "ok" and row["version"] == 1
    assert len(brief["process_steps"]) == 2 and any("Plánovač ručne" in u for u in brief["unknowns"])
    assert brief["roles"]["decision_maker"]["text"] == "" and any("konateľ" in u for u in brief["unknowns"])
    assert [q["aspect"] for q in brief["falsifying_questions"]] == ["problem_exists", "already_solved", "data_enables"]
    assert {s["claim_id"] for s in json.loads(row["sources_json"])} == {c1, c2}
    assert rec.models_for("ReviewOut") == ["hodnotitel-model"]  # kontrolu robí rola reviewer
    md = brief_markdown(row)
    for part in ("## Proces", "## Pojmy", "## Otázky, ktoré môžu nápad vyvrátiť", "## Neznáme", "## Zdroje"):
        assert part in md


def test_brief_is_shared_in_domain_and_new_version_only_when_inputs_change(tmp_path):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    other = db.create_hypothesis("Urgovanie dodávateľov", "", "výrobcovia")
    db.save_themes([{"members": {hid, other}, "name": "Meškanie dodávok", "summary": "", "name_method": "model",
                     "medoid_id": hid}])
    assert domain_of(db, hid)[0] == domain_of(db, other)[0]
    first = ensure_brief(cfg, db, router, hid)
    assert ensure_brief(cfg, db, router, other) == first  # rovnaké podklady: žiadne nové generovanie
    assert len(rec.models_for("DomainBriefOut")) == 1
    claim(db, other, "Urgencie zaberajú čas.", "Urgencie zaberajú plánovačom hodiny týždenne.")
    second = ensure_brief(cfg, db, router, other)
    key = domain_of(db, hid)[0]
    assert second != first and [b["status"] for b in db.brief_history(key)] == ["ok", "superseded"]


def test_reviewer_failure_leaves_brief_pending_not_approved(tmp_path):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    from radar.llm import LLMError

    rec.responses["ReviewOut"] = LLMError("cloud nedostupný")
    row = db.get_brief(ensure_brief(cfg, db, router, hid))
    assert row["status"] == "pending_review"


ITEMS = {"items": [
    {"need_statement": "Plánovač možno ručne urguje meškajúce dodávky.", "evidence_refs": [], "deliverable":
     "Prehľad odchýlok dodania a chýbajúcich informácií pre urgencie", "decision_supported": "ktoré zákazky urgovať",
     "minimum_input": "vysvetlenie polí pôvodného a skutočného termínu", "limitations": "nevieme, či majú históriu zmien"},
    {"need_statement": "Ušetríte 40 % času.", "deliverable": "Garantujeme úsporu", "decision_supported": "x",
     "minimum_input": "celú databázu objednávok", "savings_estimate": "5 hodín mesačne"},
    {"need_statement": "", "deliverable": "Rozbor", "decision_supported": "y", "minimum_input": "20 riadkov"}]}


def test_value_proposals_are_hypotheses_without_promises_and_commitment_needs_human(tmp_path):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    bid = ensure_brief(cfg, db, router, hid)
    ITEMS["items"][0]["evidence_refs"] = [c2, 12345]
    rec.responses["ValueOut"] = ITEMS
    stats = ensure_value_proposals(cfg, db, router, hid, "customer", bid)
    rows = db.value_proposals_for(hid, "customer")
    assert stats == {"checked": 1, "needs_edit": 2} and all(r["need_status"] == "hypothesis" for r in rows)
    good, bad, empty = rows
    assert json.loads(good["evidence_refs_json"]) == [c2]  # cudzí citát vyradený
    problems = json.loads(bad["checks_json"])["kód"]
    assert any("sľub" in p for p in problems) and any("priveľká" in p for p in problems)
    assert any("odhad úspory bez vstupov" in p for p in problems) and json.loads(bad["proposal_json"])["savings_estimate"] == ""
    assert any("hypotéza potreby" in p for p in json.loads(empty["checks_json"])["kód"])
    assert best_proposal(db, hid, "customer")["id"] == good["id"]
    assert ensure_value_proposals(cfg, db, router, hid, "customer", bid) == {}  # rovnaké podklady
    with pytest.raises(ValueError):
        approve_commitment(db, good["id"], "áno")
    approve_commitment(db, good["id"], "rozbor 20 riadkov do 10 pracovných dní")
    assert db.get_value_proposal(good["id"])["delivery_commitment_approved"] == 1


def test_data_holder_is_not_treated_as_buyer(tmp_path):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    rec.responses["ValueOut"] = {"items": [{"need_statement": "Správca možno rieši nejasné definície.",
                                            "deliverable": "Zoznam nejasných definícií a licencia na produkt",
                                            "decision_supported": "úprava dátového slovníka",
                                            "minimum_input": "dátový slovník"}]}
    ensure_value_proposals(cfg, db, router, hid, "data_holder", None)
    row = db.value_proposals_for(hid, "data_holder")[0]
    assert row["status"] == "needs_edit" and any("nákupca" in p for p in json.loads(row["checks_json"])["kód"])


def test_run_value_stage_and_gui_tab(tmp_path, monkeypatch):
    cfg, db, hid, rec, router, c1, c2 = _env(tmp_path)
    db.set_people_selection(hid, True)
    rec.responses["ValueOut"] = {"items": [ITEMS["items"][0]]}
    stats = run_value(cfg, db, router, Deadline(1), None)
    assert stats["briefs"] == 1 and stats["value_checked"] == 1
    assert run_value(cfg, db, router, Deadline(1), None) == {}  # nič nové


def test_gate0_page_shows_domain_brief_and_value_proposals(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    key = domain_of(db, 1)[0]
    db.add_brief(key, "Doména", {"process_steps": [{"text": "Krok jedna", "claims": [1]}], "terms": [], "roles": {},
                                 "falsifying_questions": [{"aspect": "problem_exists", "question": "Kedy naposledy?"}],
                                 "unknowns": ["Neznáme X"]}, [], "h", {}, "ok", "m", "prvá verzia")
    db.add_value_proposal(1, "customer", {"need_statement": "Možno ručne opravujú doklady.", "deliverable": "Rozbor príčin",
                                          "decision_supported": "čo automatizovať", "minimum_input": "typy chýb"},
                          need_status="hypothesis", status="checked")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate0.py").run()
    assert not at.exception, at.exception
    assert any("Krok jedna" in m.value and "Neznáme X" in m.value for m in at.markdown)
    assert any("deliverable" in df.value.columns and "Rozbor príčin" in list(df.value["deliverable"]) for df in at.dataframe)


def test_ranges_in_own_ask_are_not_unsupported_numbers_and_review_has_room():
    """16. 10. 2026: „2-3 projekty“ sa čítalo ako číslo -3 bez podkladu a hodnotiteľ s limitom 900 tokenov orezal posudok
    troch návrhov – všetky ostali „čaká na hodnotiteľa“."""
    from radar.value import REVIEW_MAX_TOKENS, ValueItem, code_check_value

    item = ValueItem(need_statement="Predpokladáme, že prijímateľ nevidí zdržania včas.", deliverable="prehľad zdržaní",
                     decision_supported="ktoré projekty riešiť skôr",
                     minimum_input="potvrdenie relevantnosti pre 2-3 konkrétne projekty alebo vzorka 20 riadkov",
                     success_metric="prijímateľ určí príčinu zdržania pri 2–3 projektoch")
    _, problems = code_check_value(item, "customer", set(), "zdržania projektov 2 3", "")
    assert not any("čísla" in p for p in problems), problems
    item.need_statement = "Prijímateľ stráca 40 % prostriedkov."
    _, problems = code_check_value(item, "customer", set(), "zdržania projektov 2 3", "")
    assert any("40" in p for p in problems)  # číslo v tvrdení o adresátovi bez podkladu sa stále hlási
    assert REVIEW_MAX_TOKENS >= 2000
