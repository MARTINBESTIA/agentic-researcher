"""M39 – konkurencia ako dôvod zamietnutia: alternatívy aj nesoftvérové, citáty overuje kód, stav „bez obhájiteľnej
medzery“ navrhuje kód a potvrdzuje človek; lievik a autopilot potom hypotézu vynechajú. Falošný model, bez siete."""

import json

import pytest

from radar import funnel
from radar.alternatives import GAP_STATUS, decide, gather_sources, pending, propose, reopen, run_alternatives, state
from radar.autopilot import pending_work, selected_ids
from radar.config import Config, EmbeddingsConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.util import Deadline
from tests.fakes import FakeFetcher, Recorder, make_router
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI

QUOTE_MANUAL = "Väčšina kancelárií odpovedá na dopyty ručne z mailovej schránky."
QUOTE_CRM = "Realitný softvér automaticky odpovedá na dopyty z portálov."
QUOTE_GAP = "Odpoveď mimo pracovného času však žiadny nástroj nerieši."


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Malé realitné kancelárie nestíhajú dopyty", "Popis", "malé realitné kancelárie")
    doc, _ = db.upsert_document("https://zvaz.example/prieskum", "Prieskum zväzu", "search", "t", "q", "h1",
                                f"Úvod. {QUOTE_MANUAL} {QUOTE_GAP} Záver.")
    for quote, kind in ((QUOTE_MANUAL, "existing_solution"), (QUOTE_GAP, "pain")):
        cid = db.insert_claim(doc, 0, "t", quote, "kancelárie", kind, quote)
        db.set_claim_verification(cid, "verified", 100, "ok")
        db.assign_claim(hid, cid)
    db.set_people_selection(hid, True)
    db.add_critique(hid, 2, {"summary": "s", "competitor_links": [
        {"query": "realitný softvér dopyty", "title": "Realitný softvér", "url": "https://softver.example/"}]})
    rec = Recorder()
    return cfg, db, hid, rec, make_router(cfg, db, rec)


def alt(name, kind, covers, quote="", source=-1, unresolved="", uq="", us=-1):
    return {"name": name, "kind": kind, "covers": covers, "covers_quote": quote, "covers_source": source,
            "how_bought": "", "barriers": [], "unresolved": unresolved, "unresolved_quote": uq, "unresolved_source": us}


def fetcher():
    return FakeFetcher(texts={"https://softver.example/": f"Realitný softvér. {QUOTE_CRM} Cenník od 30 € mesačne."})


def test_sources_include_evidence_and_competitor_pages(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    src = gather_sources(cfg, db, hid, fetcher())
    assert src[0]["text"] == QUOTE_MANUAL  # existujúce riešenie prvé
    web = next(s for s in src if s["druh"].startswith("web"))
    assert QUOTE_CRM in web["text"] and web["url"] == "https://softver.example/"


def test_quotes_are_verified_by_code_and_unverified_claims_are_weakened(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    seen = []
    rec.responses = {"AlternativesOut": lambda msgs: seen.append(msgs) or {"alternatives": [
        alt("Ručne z mailovej schránky", "person", "yes", QUOTE_MANUAL, 0),
        alt("Softvér X", "software", "yes", "Softvér X rieši úplne všetko.", 2),  # citát nie je v zdroji
    ]}}
    run_alternatives(cfg, db, router, fetcher(), Deadline(1))
    s = state(db, hid)
    by = {a["názov"]: a for a in s["alternatívy"]}
    assert by["Ručne z mailovej schránky"]["pokrýva"] == "yes" and by["Ručne z mailovej schránky"]["pokrýva_citát"]
    assert by["Softvér X"]["pokrýva"] == "unknown" and by["Softvér X"]["pokrýva_model"] == "yes"
    assert s["návrh"] == "open"  # nie každá alternatíva pokrýva proces doloženým citátom
    user = seen[-1][-1]["content"]
    assert "<untrusted_document>" in user and QUOTE_CRM in user  # stránka konkurencie ako nedôveryhodný zdroj


def test_code_proposes_no_gap_only_with_quoted_coverage_and_no_documented_gap(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    rec.responses = {"AlternativesOut": {"alternatives": [
        alt("Ručne z mailovej schránky", "person", "yes", QUOTE_MANUAL, 0),
        alt("Realitný softvér", "software", "yes", QUOTE_CRM, 2),
    ]}}
    src = gather_sources(cfg, db, hid, fetcher())
    assert src[2]["url"] == "https://softver.example/"
    run_alternatives(cfg, db, router, fetcher(), Deadline(1))
    assert state(db, hid)["návrh"] == GAP_STATUS
    items = state(db, hid)["alternatívy"]
    gap = dict(items[0], nevyriešené="mimo pracovného času", nevyriešené_doložené=True)
    assert propose([gap, items[1]])[0] == "gap_evidence"
    assert propose([dict(items[0], pokrýva="partial"), items[1]])[0] == "open"
    assert propose([])[0] == "insufficient"


def test_human_confirms_then_funnel_and_autopilot_skip_until_reopened(tmp_path, monkeypatch):
    cfg, db, hid, rec, router = setup(tmp_path)
    other = db.create_hypothesis("Iný problém s medzerou", "Popis", "segment")
    db.set_people_selection(other, True)
    rec.responses = {"AlternativesOut": {"alternatives": [alt("Ručne z mailovej schránky", "person", "yes", QUOTE_MANUAL, 0)]}}
    run_alternatives(cfg, db, router, fetcher(), Deadline(1), [hid])
    aid = state(db, hid)["id"]
    with pytest.raises(ValueError, match="Neznáme"):
        decide(db, aid, "maybe")
    monkeypatch.setattr(funnel, "_usable", lambda cfg, db, h, now: ("contacts", "kontakty", "sig"))
    assert funnel.pick(cfg, db)["hypothesis_id"] == hid  # pred potvrdením ide prvá
    decide(db, aid, "confirmed", "rozhovor: všetci majú portálový softvér")
    assert db.get_hypothesis(hid)["gap_status"] == GAP_STATUS
    assert funnel.pick(cfg, db)["hypothesis_id"] == other
    assert hid not in selected_ids(db) and other in selected_ids(db)
    assert hid not in pending(cfg, db)
    reopen(db, hid)
    assert db.get_hypothesis(hid)["gap_status"] is None and hid in selected_ids(db)


def test_confirm_only_a_code_proposal_and_unchanged_inputs_are_not_reassessed(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    rec.responses = {"AlternativesOut": {"alternatives": []}}
    assert "alternatives" in pending_work(cfg, db)
    run_alternatives(cfg, db, router, fetcher(), Deadline(1))
    s = state(db, hid)
    assert s["návrh"] == "insufficient"
    with pytest.raises(ValueError, match="iba návrh"):
        decide(db, s["id"], "confirmed")
    decide(db, s["id"], "rejected", "medzera je v rozhovoroch")
    assert pending(cfg, db) == [] and "alternatives" not in pending_work(cfg, db)
    stats = run_alternatives(cfg, db, router, fetcher(), Deadline(1))
    assert stats.get("assessed", 0) == 0
    rows = db.conn.execute("SELECT COUNT(*) FROM alternative_assessments").fetchone()[0]
    assert rows == 1 and json.loads(db.latest_alternative_assessment(hid)["sources_json"])


def test_problems_page_shows_alternatives_and_confirm_without_model_calls(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    d.add_alternative_assessment(hid, "x", [], [{
        "názov": "Ručne", "druh": "person", "pokrýva": "yes", "pokrýva_model": "yes", "pokrýva_citát": "citát",
        "pokrýva_zdroj": "https://obce.sk/a", "ako_sa_kupuje": "", "bariéry": [], "nevyriešené": "",
        "nevyriešené_doložené": False, "nevyriešené_citát": "", "nevyriešené_zdroj": ""}], GAP_STATUS, "dôvod")
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/problems.py").run()
    assert not at.exception, at.exception
    assert any(b.label == "Potvrdiť: bez obhájiteľnej medzery" for b in at.button)
    d = DB(cfg.db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()
