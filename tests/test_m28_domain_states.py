"""M28 – stavy doménových podkladov a pripravenosť oslovenia (dodatok 16. 10. 2026, body 1, 4.1–4.3, 5): priority
domén, jeden pokus na verziu vstupu, partial / needs_research s dôvodom, tvrdenia so stavom a rozsahom, otázky
s blokovanou akciou a históriou odpovedí, jedna hlavná otázka v prvom oslovení, pripravenosť oslovenia nezávislá od
karty (needs_contact_evidence, review_pending, suppressed). Offline (falošný model)."""

import json

import pytest

from radar.domain import coverage, interview_questions, main_question, request_research, set_priority, sync_priorities
from radar.llm import LLMError
from radar.outcomes import record_outcome
from radar.outreach import OutreachError, approve, confirm_manual_review, run_drafts, send_state
from radar.util import Deadline
from radar.value import domain_of, ensure_brief
from tests.test_m24_value import brief_response, claim
from tests.test_m25_outreach_modes import _world


def _brief_world(tmp_path):
    w = _world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    c1 = claim(db, hid, "Dodávky meškajú.", "Dodávky dielcov meškajú o týždne.")
    c2 = claim(db, hid, "Plánovanie trpí.", "Plánovači ručne urgujú dodávateľov každý deň.")
    resp = brief_response(c1, c2)
    resp["open_questions"] = [
        {"question": "Uchovávate pôvodný prísľub termínu aj históriu jeho zmien?", "about": "data",
         "target_role": "data_holder", "blocks_action": "duration_analysis"},
        {"question": "Kto rozhoduje o nákupe nástroja na plánovanie?", "about": "roles", "target_role": "customer",
         "blocks_action": "contact_choice"}]
    rec.responses["DomainBriefOut"] = resp
    rec.responses["ReviewOut"] = {"verdict": "ok", "problems": []}
    return w, c1, c2


def test_priority_batch_and_manual_extra_domain(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, *_ = w
    others = []
    for i in range(3):
        h = db.create_hypothesis(f"Iný problém {i}", "", "segment")
        db.set_people_selection(h, True)
        for j in range(i + 1):
            claim(db, h, f"Tvrdenie {i}-{j}", f"Citát o probléme {i} číslo {j} zo zdroja.")
        others.append(h)
    rows = {r["domain_key"]: r for r in sync_priorities(cfg, db)}
    prio = [k for k, r in rows.items() if r["priority"]]
    assert len(prio) == 3 and all(rows[k]["status"] == "queued" for k in prio)
    low = next(k for k, r in rows.items() if not r["priority"])
    assert rows[low]["status"] == "not_requested"
    set_priority(db, low, "ručne", True)
    rows = {r["domain_key"]: r for r in sync_priorities(cfg, db)}
    assert sum(r["priority"] for r in rows.values()) == 4  # ručne zaradená doména je navyše
    assert rows[low]["priority_source"] == "manual" and rows[low]["status"] == "queued"


def test_failure_saves_needs_research_once_per_input_version_and_explicit_request_retries(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, *_ = w
    rec.responses["DomainBriefOut"] = LLMError("výpadok")
    assert ensure_brief(cfg, db, router, hid) is None
    key = domain_of(db, hid)[0]
    state = db.research_get(key)
    assert state["status"] == "needs_research" and "zlyhala" in state["stop_reason"] and state["next_step"]
    calls = len(rec.models_for("DomainBriefOut"))
    ensure_brief(cfg, db, router, hid)  # rovnaká verzia vstupu: žiadny nekonečný retry
    assert len(rec.models_for("DomainBriefOut")) == calls
    request_research(db, key, "Doména")
    rec.responses["DomainBriefOut"] = brief_response(c1, c2)
    assert ensure_brief(cfg, db, router, hid) is not None
    assert len(rec.models_for("DomainBriefOut")) == calls + 1
    assert db.research_get(key)["status"] in ("partial", "ready")


def test_claims_have_status_scope_and_questions_block_actions(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, *_ = w
    key = domain_of(db, hid)[0]
    import hashlib

    ev = db.add_domain_evidence(key, "esco", "Nákupca plánuje a sleduje dodávky a komunikuje s dodávateľmi.",
                                hashlib.sha256(b"esco1").hexdigest(), "occupation", local_reference="esco:occ/1",
                                title="nákupca", source_version="ESCO v1.2.0")
    resp = dict(w[5].responses["DomainBriefOut"])
    resp["roles"] = {"user": {"text": "nákupca sleduje dodávky", "claims": [], "evidence": [ev]},
                     "data_steward": {"text": "", "claims": []}, "decision_maker": {"text": "", "claims": []}}
    w[5].responses["DomainBriefOut"] = resp
    ensure_brief(cfg, db, w[6], hid)
    claims = db.domain_claims(key)
    role = next(x for x in claims if x["part"] == "roles" and x["status"] == "source_fact")
    assert role["scope"] == "occupation" and "nie fakt o konkrétnej organizácii" in role["applicability"]
    assert any(x["status"] == "hypothesis" and "Plánovač ručne" in x["text"] for x in claims)  # bez citátu = hypotéza
    qs = {q["question"]: q for q in db.domain_questions(key)}
    assert qs["Uchovávate pôvodný prísľub termínu aj históriu jeho zmien?"]["blocks_action"] == "duration_analysis"
    assert qs["Kto rozhoduje o nákupe nástroja na plánovanie?"]["blocks_action"] == "contact_choice"
    cov, status = coverage(db, key)
    assert cov["roles"] == "podložené" and status in ("partial", "ready")
    assert main_question(db, key, "customer")["aspect"] == "problem_exists"
    assert main_question(db, key, "data_holder")["blocks_action"] == "duration_analysis"
    assert len(interview_questions(db, key, "customer")) >= 2


def test_partial_brief_still_gives_cautious_draft_with_one_main_question_and_shared_version(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    brief_id = ensure_brief(cfg, db, router, hid)
    prompts = []
    rec.responses["DraftOut"] = lambda m: prompts.append(m[-1]["content"]) or {
        "subject": "Otázka k meškaniu dodávok", "body": "predpokladám, že sa vo vašom tíme môže objavovať ručné "
        "urgovanie dodávok. Chcem si overiť, či je to aj váš prípad – kedy sa to stalo naposledy?", "facts_used": ["F1"]}
    run_drafts(cfg, db, router, Deadline(1), [hid])
    rows = [o for o in db.outreach_list(hid) if o["package_json"]]
    pkgs = [json.loads(o["package_json"]) for o in rows]
    assert {p["prehľad_domény"]["id"] for p in pkgs} == {brief_id}  # všetky kontakty domény: jedna verzia
    cust = next(p for p in pkgs if p["typ"] == "customer")
    assert cust["hlavná_otázka"]["text"] and cust["príprava_rozhovoru"]
    assert cust["stav_podkladov_domény"] in ("partial", "ready")
    assert any("MAIN QUESTION:" in p for p in prompts)
    states = {send_state(cfg, db, o)[0] for o in rows}
    assert states <= {"draft_ready", "review_pending"}


def test_answer_updates_question_and_claim_with_history_and_new_brief_keeps_it(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    ensure_brief(cfg, db, router, hid)
    key = domain_of(db, hid)[0]
    run_drafts(cfg, db, router, Deadline(1), [hid])
    out = next(o for o in db.outreach_list(hid) if o["person_id"] == pid)
    db.update_outreach(out["id"], status="sent", sent_at="2026-10-15T09:00:00")
    q = next(q for q in db.domain_questions(key, "open") if q["blocks_action"] == "contact_choice")
    res = record_outcome(cfg, db, outreach_id=out["id"], reply="relevant",
                         answers={q["id"]: {"text": "rozhoduje riaditeľ výroby"}})
    row = db.get_domain_question(q["id"])
    assert row["status"] == "answered" and row["answer"] == "rozhoduje riaditeľ výroby"
    assert [h.get("stav") for h in json.loads(row["history_json"])][-1] == "answered"
    reported = [x for x in db.domain_claims(key) if x["status"] == "contact_reported"]
    assert len(reported) == 1 and reported[0]["organization_id"] == org_id and res["odpovede"]
    assert res.get("prehľad_domény")  # odpoveď mení podklady → nová verzia pri ďalšom kroku value
    second = ensure_brief(cfg, db, router, hid)
    assert db.get_brief(second)["reason"].startswith("odpoveď kontaktu")
    assert db.get_domain_question(q["id"])["status"] == "answered"  # zodpovedaná otázka ostáva
    assert any(x["status"] == "contact_reported" for x in db.domain_claims(key))  # výpoveď kontaktu ostáva aktívna


def test_missing_reason_review_pending_and_suppressed_block_approval(tmp_path):
    w, c1, c2 = _brief_world(tmp_path)
    cfg, db, hid, pid, org_id, rec, router, dh, ex, firma = w
    rec.responses["DraftCheck"] = LLMError("hodnotiteľ nedostupný")
    run_drafts(cfg, db, router, Deadline(1), [hid])
    out = next(o for o in db.outreach_list(hid) if o["person_id"] == pid and o["status"] == "draft")
    assert send_state(cfg, db, out)[0] == "review_pending"
    with pytest.raises(OutreachError, match="čaká na hodnotiteľa"):
        approve(cfg, db, out["id"])
    confirm_manual_review(db, out["id"])
    assert send_state(cfg, db, db.get_outreach(out["id"]))[0] == "draft_ready"
    approve(cfg, db, out["id"])
    assert send_state(cfg, db, db.get_outreach(out["id"]))[0] == "ready_for_manual_send"

    other = next(o for o in db.outreach_list(hid) if o["person_id"] == dh)
    pkg = json.loads(other["package_json"])
    pkg["prečo_tento_kontakt"]["zdroj"] = ""
    db.update_outreach(other["id"], package_json=pkg)
    state, reasons = send_state(cfg, db, db.get_outreach(other["id"]))
    assert state == "needs_contact_evidence" and "zdroj dôvodu výberu" in reasons[0]
    with pytest.raises(OutreachError, match="chýba podklad"):
        approve(cfg, db, other["id"])

    expert = next(o for o in db.outreach_list(hid) if o["person_id"] == ex)
    db.update_person(ex, status="declined")
    assert send_state(cfg, db, db.get_outreach(expert["id"]))[0] == "suppressed"


def test_gui_shows_domain_state_and_send_readiness(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.db import DB
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    key = domain_of(db, 1)[0]
    db.add_domain_question(key, "Kto rozhoduje o nákupe?", "kto rozhoduje o nakupe", "open", target_role="customer",
                           blocks_action="contact_choice")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate0.py").run()
    assert not at.exception, at.exception
    assert any("Doménové podklady" in m.value for m in at.markdown)
    assert any("blokuje" in df.value.columns for df in at.dataframe)
    at.switch_page("app_pages/outreach.py").run()
    assert not at.exception, at.exception
    assert any("Pripravenosť" in a.value for a in at.info) or any("Pripravenosť" in a.value for a in at.warning)
