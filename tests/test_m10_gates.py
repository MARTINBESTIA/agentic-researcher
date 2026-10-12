"""M10–M13: kontakty k osobe, výber modelov z GUI, Brána 1 (polia, zdroje, DuckDB, reprodukcia, infozákon,
pripravenosť) a Brána 2 (predregistrácia, spätný test, demo). Offline: falošný model, web, vyhľadávanie a lokálne CSV."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from radar import gate1, gate2
from radar.config import Config, EmbeddingsConfig, Gate1Config, ModelsConfig, PeopleConfig, PrivacyConfig
from radar.datasets import DatasetError, check_sql, run_query
from radar.db import DB
from radar.models import MODEL_SETTINGS_KEY, ModelRouter
from radar.person_contacts import contacts_of_name, run_person_contacts
from radar.privacy import forget
from radar.search import SearchResult
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router, mock_fetcher


def call(name, args, call_id="1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def step(*calls):
    return SimpleNamespace(content=None, tool_calls=list(calls))


def setup(tmp_path, **gate):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=False),
                 gate1=Gate1Config(**gate), config_dir=str(tmp_path))
    cfg.gate2.demos_dir = str(tmp_path / "demos")
    db = DB(cfg.db_path)
    rec = Recorder()
    return cfg, db, rec, make_router(cfg, db, rec)


# ======================================================================= M10 kontakty k osobe
STAFF = """<html><body><table>
<tr><td>Ing. Jana NOVÁKOVÁ</td><td>prednostka úradu</td><td>jana.novakova@fiktivne-mesto.sk</td><td>041 513 5101</td></tr>
<tr><td>Peter Vzorový</td><td>referent</td><td>peter.vzorovy@fiktivne-mesto.sk</td><td>041 513 5102</td></tr>
</table><p>Podateľňa: podatelna@fiktivne-mesto.sk</p></body></html>"""


def test_contacts_of_name_takes_only_the_persons_own_contacts():
    text = ("Ing. Jana NOVÁKOVÁ prednostka jana.novakova@fiktivne-mesto.sk 041 513 5101 "
            "Peter Vzorový referent peter.vzorovy@fiktivne-mesto.sk info@fiktivne-mesto.sk")
    emails = ["jana.novakova@fiktivne-mesto.sk", "peter.vzorovy@fiktivne-mesto.sk", "info@fiktivne-mesto.sk"]
    got = contacts_of_name(text, "Peter Vzorový", emails, ["+421415135101"])
    assert [(k, v) for k, v, _ in got] == [("email", "peter.vzorovy@fiktivne-mesto.sk")]  # nie Janin ani info@
    got = contacts_of_name(text, "Jana Nováková", emails, ["+421415135101"])
    assert {(k, v) for k, v, _ in got} == {("email", "jana.novakova@fiktivne-mesto.sk"), ("phone", "+421415135101")}
    assert all(v in s for k, v, s in got if k == "email")  # hodnota doslova v úryvku


def test_person_contacts_from_org_site_only_and_respect_suppression(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "obce")
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Mesto Fiktívne", "mesto fiktivne", "search", website="https://www.fiktivne-mesto.sk/",
                                    website_verified=1)
    pid = db.insert_person(org, "Jana Nováková", "jana novakova", "https://api.statistics.sk/rpo", "RPO", hid,
                           role_title="prednostka", source_kind="rpo")
    db.set_link_priority(db.upsert_link(pid, hid, "org_affected"), 0.6, {})
    searx = FakeSearx({"site:fiktivne-mesto.sk": [SearchResult(url="https://www.fiktivne-mesto.sk/urad/kontakty",
                                                              title="Kontakty", snippet="")],
                       "kontakt": [SearchResult(url="https://katalog-firiem.sk/jana", title="Jana", snippet="")]})
    fetcher = FakeFetcher(htmls={"https://www.fiktivne-mesto.sk/urad/kontakty": STAFF, "https://katalog-firiem.sk/jana": STAFF})
    stats = run_person_contacts(cfg, db, searx, fetcher, Deadline(1))
    kinds = {(c["kind"], c["value"], c["extraction"]) for c in db.contacts_for_person(pid)}
    assert kinds == {("email", "jana.novakova@fiktivne-mesto.sk", "person_page"), ("phone", "+421415135101", "person_page")}
    assert "https://katalog-firiem.sk/jana" not in fetcher.fetched  # iba web organizácie a úradné domény
    assert stats["people_found"] == 1
    assert run_person_contacts(cfg, db, searx, fetcher, Deadline(1))["already_has_contact"] == 1
    forget(cfg, db, email="jana.novakova@fiktivne-mesto.sk")
    assert db.get_person(pid) is None


# ======================================================================= výber modelov z GUI
def test_gui_model_selection_overrides_config_for_new_routers(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    assert router.route("scout").model == "radar-qwen3.5-9b" and router.route("scout").role == "agent"
    db.set_setting(MODEL_SETTINGS_KEY, {"roles": {"agent": "qwen3.6:35b-a3b"}, "profile": "balanced"})
    fresh = ModelRouter(cfg, db, client_factory=lambda *a: None)
    assert fresh.route("scout").model == "qwen3.6:35b-a3b" and fresh.route("extract").model == "radar-qwen3.5-9b"
    assert fresh.role_config("agent").timeout_s == cfg.models.roles["agent"].timeout_s  # limity ostávajú z roly
    db.set_setting(MODEL_SETTINGS_KEY, {"roles": {}, "profile": "accurate"})
    assert ModelRouter(cfg, db, client_factory=lambda *a: None).route("extract").role == "accurate"
    db.delete_setting(MODEL_SETTINGS_KEY)
    assert ModelRouter(cfg, db, client_factory=lambda *a: None).route("scout").model == "radar-qwen3.5-9b"


# ======================================================================= Brána 1
CATALOG_PAGE = """<html><body><h1>Rozpočty obcí – dataset</h1>
<p>Dataset obsahuje výdavky obcí na verejné obstarávanie podľa rokov. Licencia: CC BY 4.0. Aktualizácia: ročne.</p>
<a href="/files/vydavky.csv">Stiahnuť CSV</a> <a href="/o-nas">O nás</a></body></html>"""
CSV = "ico;rok;suma\n00313271;2020;400000\n00313271;2021;500000\n00603481;2021;600000\n"
PERSONAL_CSV = "meno;priezvisko;suma\nJana;Nováková;100\nPeter;Malý;200\n"


def gate1_hyp(db, status="new"):
    hid = db.create_hypothesis("Obce nevidia výdavky na obstarávanie", "Popis", "obce", status=status)
    db.set_people_selection(hid, True)
    return hid


def test_fields_are_defined_and_human_edits_kept(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    rec.responses["DataFields"] = {"fields": [
        {"name": "Výdavky obce na obstarávanie", "description": "€ za rok a obec", "is_key": True, "rationale": "jadro",
         "source_hints": ["rozpočty obcí"]},
        {"name": "Počet zákaziek", "description": "počet za rok", "is_key": False}]}
    assert gate1.run_fields(cfg, db, router, Deadline(1), None) == {"fields": 2, "hypotheses": 1}
    fields = db.fields_for(hid)
    assert [f["is_key"] for f in fields] == [1, 0] and "rozpočty obcí" in fields[0]["rationale"]
    h = db.get_hypothesis(hid)
    assert h["gate1_status"] == "running" and h["gate1_started_at"]
    assert rec.models_for("DataFields") == ["radar-qwen3.6-27b"]  # presný model
    db.update_field(fields[1]["id"], is_key=1, edited_by_human=1)
    db.upsert_field(hid, "Počet zákaziek", "iný popis", False, "")
    assert db.get_field(fields[1]["id"])["is_key"] == 1  # ručná úprava sa neprepíše


def test_source_agent_keeps_license_only_if_on_page_and_check_finds_files(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    fid = db.upsert_field(hid, "Výdavky obce na obstarávanie", "", True, "")
    page = "https://data.obce.sk/dataset/vydavky"
    rec.responses["tools"] = [
        step(call("read_page", {"url": page})),
        step(call("record_source", {"field": "Výdavky obce na obstarávanie", "name": "Rozpočty obcí", "url": page,
                                    "page_url": page, "access": "open", "license": "CC BY 4.0", "update_frequency": "ročne",
                                    "formats": "CSV", "evidence_quote": "Licencia: CC BY 4.0. Aktualizácia: ročne."}),
             call("record_source", {"field": "Neexistujúce pole", "name": "x", "url": page, "page_url": page,
                                    "access": "open", "evidence_quote": "x"}, "2")),
        step(call("finish", {"summary": "ok"}))]
    fetcher = FakeFetcher(texts={page: CATALOG_PAGE.replace("<", " <") * 2}, htmls={page: CATALOG_PAGE})
    stats = gate1.run_sources(cfg, db, router, FakeSearx(), fetcher, Deadline(1), [hid])
    src = db.sources_for(hid)[0]
    assert stats["sources"] == 1 and src["license"] == "CC BY 4.0" and src["update_frequency"] == "ročne"
    assert json.loads(src["checks_json"])["citát_na_stránke"] is True
    assert gate1.run_check(cfg, db, fetcher, Deadline(1), [hid])["checked"] == 1
    src = db.get_source(src["id"])
    checks, quality = json.loads(src["checks_json"]), json.loads(src["quality_json"])
    assert checks["súbory"] == ["https://data.obce.sk/files/vydavky.csv"] and src["status"] == "verified"
    assert quality["licencia"] == 1.0 and quality["strojovo_čitateľné"] == 1.0
    assert db.get_field(fid)["status"] == "found"

    # licencia, ktorá na stránke nie je, sa neuloží (nič sa nehádá)
    fid2 = db.upsert_field(hid, "Počet zákaziek", "", False, "")
    rec.responses["tools"] = [
        step(call("read_page", {"url": page})),
        step(call("record_source", {"field": "Počet zákaziek", "name": "Rozpočty obcí", "url": page, "page_url": page,
                                    "access": "open", "license": "CC0", "evidence_quote": "Dataset obsahuje výdavky obcí"})),
        step(call("finish", {"summary": "ok"}))]
    gate1.run_sources(cfg, db, router, FakeSearx(), fetcher, Deadline(1), [hid])
    src2 = [s for s in db.sources_for(hid) if s["field_id"] == fid2][0]
    assert src2["license"] is None and json.loads(src2["checks_json"])["licencia_neoverená"] == "CC0"


def _dataset_env(tmp_path, csv_text=CSV, **gate):
    cfg, db, rec, router = setup(tmp_path, **gate)
    hid = gate1_hyp(db)
    fid = db.upsert_field(hid, "Výdavky obce na obstarávanie", "", True, "")
    url = "https://data.obce.sk/files/vydavky.csv"
    sid = db.add_source(fid, hid, "Rozpočty obcí", url, "agent", access="open", formats="CSV")
    db.update_source(sid, status="verified", checks_json={"http_ok": True, "súbory": [url]})
    org, _ = db.upsert_organization("Mesto Fiktívne", "mesto fiktivne", "search", ico="00313271")
    fetcher = mock_fetcher({url: httpx.Response(200, content=csv_text.encode("cp1250"), headers={"content-type": "text/csv"})})
    return cfg, db, rec, router, hid, fid, org, fetcher


def test_datasets_go_to_duckdb_with_profile_coverage_and_limits(tmp_path):
    cfg, db, rec, router, hid, fid, org, fetcher = _dataset_env(tmp_path)
    stats = gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    assert stats == {"datasets": 1, "coverage": 1}
    ds = db.datasets_for(hid)[0]
    prof = json.loads(ds["profile_json"])
    assert ds["status"] == "ok" and ds["rows"] == 3 and ds["sha256"] and prof["ico_columns"] == ["ico"]
    assert [c["org_name"] for c in db.coverage_for(hid)] == ["Mesto Fiktívne"]
    assert run_query(cfg, hid, f"SELECT SUM(suma) FROM {ds['table_name']}")[0][0] == 1_500_000
    assert not list((tmp_path / "data" / "datasets" / f"H{hid}").glob("*.csv"))  # pôvodný súbor zmazaný po importe


def test_personal_data_dataset_is_not_kept_and_size_limit(tmp_path):
    cfg, db, rec, router, hid, fid, org, fetcher = _dataset_env(tmp_path, PERSONAL_CSV)
    assert gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid]) == {"personal_blocked": 1}
    ds = db.datasets_for(hid)[0]
    assert ds["status"] == "personal_data_blocked" and "GDPR" in ds["note"]
    with pytest.raises(DatasetError):
        run_query(cfg, hid, f"SELECT * FROM {ds['table_name']}")  # tabuľka sa neuložila

    cfg2, db2, *_, hid2, _, _, fetcher2 = _dataset_env(tmp_path / "b", CSV * 1000, max_dataset_mb=0)
    gate1.run_datasets(cfg2, db2, fetcher2, Deadline(1), [hid2])
    assert db2.datasets_for(hid2)[0]["status"] == "too_large"


def test_sql_from_model_is_read_only_single_select():
    for bad in ("SELECT * FROM read_csv('C:/Users/x.csv')", "COPY ds1 TO 'x.csv'", "SELECT 1; DROP TABLE ds1",
                "ATTACH 'x.db'", "SELECT * FROM 'https://example.com/a.csv'", "DELETE FROM ds1"):
        with pytest.raises(DatasetError):
            check_sql(bad)
    assert check_sql("WITH a AS (SELECT 1 AS x) SELECT x FROM a;") == "WITH a AS (SELECT 1 AS x) SELECT x FROM a"


def test_reproduction_of_known_number_scale_and_readiness(tmp_path):
    cfg, db, rec, router, hid, fid, org, fetcher = _dataset_env(tmp_path)
    gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    doc, _ = db.upsert_document("https://nku.gov.sk/sprava", "NKÚ", "search", "t", "q", "h", "Obce minuli 1,5 mil. € na …")
    cid = db.insert_claim(doc, 0, "t", "Obce minuli 1,5 mil. €", "obce", "cost", "Obce minuli 1,5 mil. €",
                          [{"value": 1.5, "unit": "mil. €", "meaning": "výdavky"}])
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    table = db.datasets_for(hid)[0]["table_name"]
    rec.responses["ReproSpec"] = {"sql": f"SELECT SUM(suma) FROM {table}", "explanation": "súčet", "unit": "€"}
    assert gate1.run_repro(cfg, db, router, Deadline(1), [hid]) == {"match": 1}
    rep = db.reproductions_for(hid)[0]
    assert rep["verdict"] == "match" and rep["scale"] == 1e6 and "mierke" in rep["note"]
    assert rec.models_for("ReproSpec") == ["radar-qwen3.6-27b"]

    db.update_field(fid, status="found")
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    summary = json.loads(h["gate1_json"])
    assert summary["zložky"]["pokrytie_kľúčových"] == 1.0 and summary["reprodukcia"] == "match"
    assert h["gate1_status"] == "ready" and summary["organizácie_v_dátach"] == 1  # návrh, nie automaticky „prešla“


def test_missing_key_field_gets_info_request_draft_and_six_week_flag(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    fid = db.upsert_field(hid, "Termíny ukončenia projektov", "dátum podľa projektu", True, "")
    db.start_gate1(hid)
    db.update_field(fid, status="missing")
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    req = db.info_requests_for(hid)[0]
    assert "211/2000" in req["body"] and "Termíny ukončenia projektov" in req["body"] and req["status"] == "draft"
    assert "{POVINNÁ_OSOBA}" in req["body"]  # povinnú osobu doplní človek; nič sa neodosiela
    summary = gate1.readiness(cfg, db, hid, now=datetime.now(timezone.utc) + timedelta(weeks=7))
    assert "dáta nenájdené (žiadne kľúčové pole)" in summary["príznaky"]
    assert "kľúčové pole nezískané do 6 týždňov" in summary["príznaky"]
    assert db.get_hypothesis(hid)["status"] == "new"  # nič sa nevyraďuje
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert len(db.info_requests_for(hid)) == 1  # žiadosť sa neduplikuje


# ======================================================================= Brána 2
SERIES_CSV = "ico;rok;suma\n" + "".join(f"00313271;{2010 + i};{100 + 10 * i}\n" for i in range(10))


def gate2_env(tmp_path):
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/files/vydavky.csv"
    fetcher = mock_fetcher({url: httpx.Response(200, content=SERIES_CSV.encode(), headers={"content-type": "text/csv"})})
    gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    gate1.decide(db, hid, 1, "passed", "dáta sú")
    return cfg, db, rec, router, hid, org, db.datasets_for(hid)[0]["table_name"]


def test_backtest_series_math():
    linear = [2 * t + 1 for t in range(10)]
    res = gate2.backtest_series(linear, "last_value", "linear_trend", folds=4, min_history=5)
    assert res["model_error"] == 0 and res["baseline_error"] == 2 and res["improvement"] == 1.0
    assert res["diff_ci95"][0] > 0
    flat_noise = [10, 12, 10, 12, 10, 12, 10, 12, 10, 12]
    res = gate2.backtest_series(flat_noise, "mean", "linear_trend", folds=4, min_history=5)
    assert res["improvement"] <= 0.1
    with pytest.raises(ValueError):
        gate2.backtest_series([1, 2, 3], "mean", "ar1", folds=4, min_history=5)


def test_gate2_prereg_lock_backtest_and_demo(tmp_path):
    cfg, db, rec, router, hid, org, table = gate2_env(tmp_path)
    assert [h["id"] for h in db.gate2_hypotheses()] == [hid]
    assert gate2.targets(cfg, db, hid)[0]["org_id"] == org
    rec.responses["Prereg"] = {"statement": "Trend predpovie výdavky lepšie ako minulý rok.", "table": table,
                               "time_column": "rok", "value_column": "suma", "entity_column": "ico", "aggregation": "sum",
                               "metric": "MAE", "baseline": "last_value", "model": "linear_trend",
                               "success_rule": "MAE modelu < MAE naivného odhadu"}
    assert gate2.run_prereg(cfg, db, router, Deadline(1), [hid]) == {"drafts": 1}
    exp = db.experiments_for(hid)[0]
    assert gate2.run_backtest(cfg, db, Deadline(1), [hid]) == {}  # bez uzamknutia sa netestuje
    digest = gate2.lock(cfg, db, exp["id"])
    assert len(digest) == 64 and db.get_experiment(exp["id"])["status"] == "locked"
    with pytest.raises(ValueError):
        db.update_experiment(exp["id"], metric="MAPE")  # predregistrácia sa už nemení
    assert gate2.run_backtest(cfg, db, Deadline(1), [hid]) == {"done": 1}
    exp = db.get_experiment(exp["id"])
    res, flags = json.loads(exp["result_json"]), json.loads(exp["flags_json"])
    assert res["improvement"] == 1.0 and res["n_periods"] == 10 and flags["model_neprekonal_naivny_odhad"] is False

    rec.responses["DemoText"] = {"text": "Trend znížil chybu o 100 % a ušetrí 5 miliónov."}  # 5 nie je vo faktoch
    assert gate2.run_demo(cfg, db, router, Deadline(1), [hid]) == {"demos": 1}
    html_text = open(db.get_experiment(exp["id"])["demo_path"], encoding="utf-8").read()
    assert "<svg" in html_text and "Mesto Fiktívne" in html_text and "ušetrí" not in html_text
    assert "nepoužil sa" in html_text and digest[:16] in html_text


def test_invalid_prereg_columns_cannot_be_locked(tmp_path):
    cfg, db, rec, router, hid, org, table = gate2_env(tmp_path)
    rec.responses["Prereg"] = {"statement": "x", "table": table, "time_column": "neexistuje", "value_column": "suma"}
    gate2.run_prereg(cfg, db, router, Deadline(1), [hid])
    exp = db.experiments_for(hid)[0]
    assert json.loads(exp["flags_json"])["chyby_návrhu"]
    with pytest.raises(ValueError):
        gate2.lock(cfg, db, exp["id"])


def test_sitemap_finds_person_page_without_search_engine():
    from radar.person_contacts import sitemap_person_links

    xml = ("<urlset><url><loc>https://www.fiktivne-mesto.sk/urad/jana-novakova</loc></url>"
           "<url><loc>https://www.fiktivne-mesto.sk/o-nas</loc></url><url><loc>https://ine.sk/novakova</loc></url></urlset>")
    assert sitemap_person_links(xml, "fiktivne-mesto.sk", "Ing. Jana NOVÁKOVÁ") == [
        "https://www.fiktivne-mesto.sk/urad/jana-novakova"]  # iba web organizácie


def test_sources_from_read_pages_when_agent_only_reads(tmp_path):
    """Reálny beh: agent prečítal 5 stránok ITMS, ale record_source nezavolal ani raz."""
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    fid = db.upsert_field(hid, "Výdavky obce na obstarávanie", "", True, "")
    db.update_field(fid, status="missing")
    db.add_info_request(hid, fid, "{POVINNÁ_OSOBA}", "Žiadosť", "text")
    page = "https://data.obce.sk/dataset/vydavky"
    rec.responses["tools"] = [step(call("read_page", {"url": page})), step(call("finish", {"summary": "našiel som portál"}))]
    rec.responses["ExtractedSources"] = {"sources": [
        {"field": "Výdavky obce na obstarávanie", "name": "Rozpočty obcí", "url": page, "access": "open",
         "license": "CC BY 4.0", "formats": "CSV", "evidence_quote": "Dataset obsahuje výdavky obcí na verejné obstarávanie"}]}
    fetcher = FakeFetcher(texts={page: CATALOG_PAGE.replace("<", " <") * 2}, htmls={page: CATALOG_PAGE})
    stats = gate1.run_sources(cfg, db, router, FakeSearx(), fetcher, Deadline(1), [hid])
    assert stats["sources_from_pages"] == 1 and db.sources_for(hid)[0]["license"] == "CC BY 4.0"
    assert rec.models_for("ExtractedSources") == ["radar-qwen3.6-27b"]
    gate1.run_check(cfg, db, fetcher, Deadline(1), [hid])
    assert db.get_field(fid)["status"] == "found" and db.info_requests_for(hid) == []  # návrh žiadosti už netreba


def test_field_name_with_description_and_shortened_quote():
    """Reálny beh: model vrátil pole ako „názov: popis“ a citát skrátený „…“."""
    from radar.gate1 import SourceAgent, quote_on_page
    from radar.verify import normalize

    agent = SourceAgent.__new__(SourceAgent)
    agent.fields = {"Aktuálna fáza projektu": {}, "Identifikátor prijímateľa": {}}
    assert agent.match_field("Aktuálna fáza projektu: Kód aktuálneho stavu v procese (napr. zámer)") == "Aktuálna fáza projektu"
    assert agent.match_field('"Identifikátor prijímateľa"') == "Identifikátor prijímateľa"
    assert agent.match_field("Úplne iné pole") is None
    body = normalize("OpenData API verejnosti sprístupňuje informácie o subjektoch, ktoré sú zapojené do implementácie "
                     "eurofondov, a o schválených projektoch a o pokroku realizácie jednotlivých projektov.")
    assert quote_on_page("OpenData API verejnosti sprístupňuje informácie ... o pokroku realizácie jednotlivých projektov", body)
    assert not quote_on_page("OpenData API verejnosti sprístupňuje ... vymyslené tvrdenie o licencii CC0 pre všetky dáta", body)


def test_openapi_endpoints_become_json_datasets_with_nested_fields(tmp_path):
    """Zdroj typu API (napr. OpenData ITMS2014+): GET endpointy z popisu Swagger, vnorený JSON → stĺpce (ico)."""
    spec = {"swagger": "2.0", "host": "opendata.example.sk", "basePath": "/v2", "schemes": ["https"], "paths": {
        "/projekty/vrealizacii": {"get": {"summary": "Projekty v realizácii", "parameters": [
            {"name": "limit", "in": "query"}, {"name": "minId", "in": "query"}]}},
        "/projekty/vrealizacii/{id}": {"get": {}},
        "/ciselniky": {"get": {"summary": "Číselníky", "parameters": [{"name": "kod", "in": "query", "required": True}]}}}}
    urls = gate1.api_endpoints(spec, "https://opendata.example.sk/v2/swagger.json", "Aktuálna fáza projektu")
    assert urls == ["https://opendata.example.sk/v2/projekty/vrealizacii?limit=1000"]

    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    body = json.dumps([{"id": 1, "stav": "Zmluva uzavretá", "sumaZazmluvnena": 364319.36,
                        "prijimatel": {"subjekt": {"ico": "00313271", "dic": "2021339298"}}}])
    fetcher = mock_fetcher({urls[0]: httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})})
    src = db.sources_for(hid)[0]
    db.update_source(src["id"], checks_json={"http_ok": True, "súbory": urls})
    gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    ds = db.datasets_for(hid)[0]
    assert ds["status"] == "ok" and "ico" in json.loads(ds["columns_json"]) and ds["format"] == "json"
    assert [c["org_name"] for c in db.coverage_for(hid)] == ["Mesto Fiktívne"]  # IČO z vnoreného objektu


def test_backtest_groups_date_column_by_period(tmp_path):
    """Reálne dáta (ITMS): dátum platnosti zmluvy → štvrťroky; inak by každá zmluva bola samostatné obdobie."""
    rows = "".join(f"00313271;{2015 + i // 4}-{(i % 4) * 3 + 1:02d}-15;{100 + 10 * i}\n" for i in range(12))
    csv = "ico;datum;suma\n" + rows + rows.replace(";1", ";1", 1)
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/files/vydavky.csv"
    gate1.run_datasets(cfg, db, mock_fetcher({url: httpx.Response(200, content=csv.encode(), headers={"content-type": "text/csv"})}),
                       Deadline(1), [hid])
    table = db.datasets_for(hid)[0]["table_name"]
    spec = {"table": table, "time_column": "datum", "value_column": "suma", "entity_column": "ico", "entity_value": "00313271",
            "aggregation": "sum", "period": "quarter"}
    data = gate2.series(cfg, hid, spec)
    assert len(data) == 12 and str(data[0][0]) == "2015-01-01" and data[0][1] == 200  # dva riadky v štvrťroku sa sčítajú
