"""M38 – ekonomika u konkrétneho zákazníka: faktory s pôvodom, rozpätia počíta Python, neznáme = otázky, ponuka až
s nákladovým údajom zákazníka. Fiktívne dáta, bez modelu a siete."""

import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.economics import FACTORS, USE_QUESTION, EconomicsError, economics, economics_summary, factor_rows, record
from radar.opportunity import card_readiness
from radar.privacy import forget
from radar.problem_brief import completeness, problem_brief
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Malé realitné kancelárie nestíhajú dopyty", "Popis", "malé realitné kancelárie")
    org, _ = db.upsert_organization("Fiktívne Reality s.r.o.", "fiktivne reality", "search", website="https://fr.example/",
                                    website_verified=1)
    db.link_org_hypothesis(org, hid, "segment_match")
    return cfg, db, hid, org


def test_ranges_are_computed_by_python_with_interval_arithmetic(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    who = "Konateľka pri rozhovore 25. 10. 2026"
    record(db, hid, org, "cases_per_year", "customer_reported", 100, 150, evidence=who)
    record(db, hid, org, "minutes_per_case", "customer_reported", 30, evidence=who)
    record(db, hid, org, "hourly_cost", "inferred", 15, 20, evidence="priemerná mzda makléra s odvodmi, odhad")
    record(db, hid, org, "saving_share", "inferred", 0.4, 0.6, evidence="odhad z opisu postupu, overiť skúškou")
    record(db, hid, org, "control_cost", "inferred", 100, evidence="hodina kontroly mesačne, odhad")
    record(db, hid, org, "implementation_cost", "inferred", 1200, evidence="nastavenie a zaškolenie, odhad")
    e = economics(db, hid, org)
    assert e["hodiny_ročne"] == (50.0, 75.0) and e["náklad_dnes"] == (750.0, 1500.0)
    assert e["ročná_hodnota"] == (300.0, 900.0) and e["čistý_prínos"] == (200.0, 800.0)
    assert e["návratnosť_mesiace"] == (18.0, 72.0)
    assert e["text"]["ročná_hodnota"] == "300 – 900 €"
    record(db, hid, org, "control_cost", "inferred", 400, evidence="dve hodiny kontroly mesačne, odhad")
    e = economics(db, hid, org)  # novší odhad toho istého pôvodu platí; pri dolnom odhade sa zavedenie nevráti
    assert e["čistý_prínos"] == (-100.0, 500.0) and e["návratnosť_mesiace"] is None and "nevráti" in e["návratnosť_poznámka"]


def test_offer_needs_customer_cost_data_not_estimates_or_market(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    record(db, hid, org, "cases_per_year", "inferred", 1000, evidence="z veľkosti trhu a počtu kancelárií")
    e = economics(db, hid, org)
    assert not e["pripravené_na_ponuku"] and "veľký trh" in e["dôvod"]
    assert FACTORS["cases_per_year"]["question"] in e["otázky"]  # odhad ostáva otázkou do rozhovoru
    record(db, hid, org, "minutes_per_case", "customer_reported", 20, 40, evidence="maklérka v e-maile 24. 10. 2026")
    e = economics(db, hid, org)
    assert e["pripravené_na_ponuku"] and e["nákladový_údaj"] == ["minutes_per_case"]
    assert economics_summary(db, hid)["s_nákladovým_údajom"] == 1
    assert completeness(cfg, db, hid)["má"]["ekonomika"]


def test_observed_needs_verbatim_quote_with_number_and_vendor_is_rejected(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    text = "Výročná správa. Kancelária minulý rok vybavila 1 450 dopytov záujemcov. Ďalšie údaje."
    doc, _ = db.upsert_document("https://fr.example/vs", "Výročná správa", "search", "t", "q", "h1", text)
    record(db, hid, org, "cases_per_year", "observed", 1450, quote="vybavila 1 450 dopytov záujemcov", document_id=doc)
    assert economics(db, hid, org)["faktory"]["cases_per_year"]["source_kind"] == "document"
    with pytest.raises(EconomicsError, match="doslova"):
        record(db, hid, org, "cases_per_year", "observed", 1500, quote="vybavila 1 500 dopytov", document_id=doc)
    with pytest.raises(EconomicsError, match="číslo"):
        record(db, hid, org, "cases_per_year", "observed", 1450, quote="Kancelária minulý rok vybavila", document_id=doc)
    vendor, _ = db.upsert_document("https://softver.example/", "Dodávateľ", "search", "t", "q", "h2",
                                   "Náš nástroj ušetrí 70 % času makléra.")
    db.conn.execute("UPDATE documents SET source_type = 'vendor' WHERE id = ?", (vendor,))
    db.conn.commit()
    with pytest.raises(EconomicsError, match="dodávateľa"):
        record(db, hid, org, "saving_share", "observed", 0.7, quote="ušetrí 70 % času makléra", document_id=vendor)
    with pytest.raises(EconomicsError, match="dokument"):
        record(db, hid, org, "minutes_per_case", "observed", 20, quote="20 minút")


def test_unknown_factors_are_questions_and_time_only_saving_is_flagged(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    e = economics(db, hid, org)
    assert len(e["otázky"]) == len(FACTORS) + 1 and USE_QUESTION in e["otázky"]
    assert e["ročná_hodnota"] is None and e["text"]["ročná_hodnota"] == "chýba"
    record(db, hid, org, "saving_use", "customer_reported", evidence="konateľka 25. 10.: ušetrený čas nevyfakturujeme",
           use="time_only")
    e = economics(db, hid, org)
    assert "teoretická" in e["varovanie"] and USE_QUESTION not in e["otázky"]
    with pytest.raises(EconomicsError, match="najviac"):
        record(db, hid, org, "saving_share", "inferred", 0.5, 1.5, evidence="neplatné rozpätie podielu")
    with pytest.raises(EconomicsError, match="kto a kedy"):
        record(db, hid, org, "hourly_cost", "customer_reported", 20)
    assert any(r["faktor"] == "využitie úspory" and r["pôvod"] == "uviedol zákazník" for r in factor_rows(db, hid, org))


def test_forget_scrubs_who_said_it_but_keeps_the_org_fact(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    pid = db.insert_person(org, "Hana Fiktívna", "hana fiktivna", "https://fr.example/k", "x", hid, role_title="konateľka")
    fid = record(db, hid, org, "minutes_per_case", "customer_reported", 25, person_id=pid,
                 evidence="Hana Fiktívna pri rozhovore 25. 10. 2026")
    forget(cfg, db, person_id=pid)
    row = next(r for r in db.buyer_factors(hid, org) if r["id"] == fid)
    assert row["person_id"] is None and "Hana" not in (row["evidence"] or "") and row["low"] == 25


def test_brief_and_card_show_economics_and_offer_readiness(tmp_path):
    cfg, db, hid, org = setup(tmp_path)
    md = problem_brief(cfg, db, hid)
    assert "| ekonomika u zákazníka | 0 z 0 organizácií" in md and FACTORS["minutes_per_case"]["question"] in md
    assert "ekonomika" in completeness(cfg, db, hid)["chýba"]
    card_id = db.add_org_card(org, hid, "dopyty", "customer", {"why_org": []}, inputs_hash="x", status="checked")
    ready = card_readiness(cfg, db, db.get_org_card(card_id))
    assert not ready["ponuka"]["pripravené"]
    assert any("tvrdenie o prínose" in m for m in ready["ponuka"]["chýba"])
    assert any("nákladový údaj" in m for m in ready["ponuka"]["chýba"])


def test_card_panel_shows_offer_readiness_and_economics_without_model_calls(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.shortlist import contact_shortlist
    from tests.test_m2_gui import APP
    from tests.test_m33_org_cards import card_payload

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    org = next(r["org_id"] for r in contact_shortlist(cfg, d, hid) if r["org_id"])
    record(d, hid, org, "minutes_per_case", "customer_reported", 30, evidence="starostka pri rozhovore 25. 10. 2026")
    cid = d.add_org_card(org, hid, "prepis-dokladov", "customer", card_payload(), inputs_hash="g", status="checked")
    d.update_org_card(cid, readiness_json=card_readiness(cfg, d, d.get_org_card(cid)))
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate0.py").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Na ponuku" in text and "Otázky do rozhovoru" in text
    assert any("nákladový údaj od zákazníka" in s.value for s in at.success)
    d = DB(cfg.db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()
