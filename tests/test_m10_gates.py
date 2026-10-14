"""M10–M13: kontakty k osobe, výber modelov z GUI, Brána 1 (polia, zdroje, DuckDB, reprodukcia, infozákon,
pripravenosť) a Brána 2 (predregistrácia, spätný test, demo). Offline: falošný model, web, vyhľadávanie a lokálne CSV."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import numpy as np
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
                 gate1=Gate1Config(**({"nkod_enabled": False} | gate)), config_dir=str(tmp_path))  # bez siete
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
    stats = gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    summary = json.loads(h["gate1_json"])
    assert summary["zložky"]["pokrytie_kľúčových"] == 1.0 and summary["reprodukcia"] == "match"
    assert summary["organizácie_v_dátach"] == 1
    # Pravidlo prechodu (používateľ 13. 10. 2026): pripravenosť ≥ 0,5 → Brána 1 prešla automaticky, s dôvodom a metrikami.
    assert summary["skóre"] >= 0.5 and stats["auto_passed"] == 1 and h["gate1_status"] == "passed"
    decision = db.gate_decisions_for(hid, 1)[0]
    assert "pripravenosť dát" in decision["reason"] and json.loads(decision["metrics_json"])["skóre"] == summary["skóre"]
    assert [x["id"] for x in db.gate2_hypotheses()] == [hid]
    assert "auto_passed" not in gate1.run_finish(cfg, db, Deadline(1), [hid])  # už prešla – nič sa nezdvojí
    other = db.create_hypothesis("Iná", "", "x")
    db.set_gate_status(other, 1, "running")
    assert "auto_passed" not in gate1.run_finish(cfg, db, Deadline(1), [other])  # bez polí nič



# ======================================================================= prechod do Brány 2 a vstup človeka
USER_CSV = "ico;rok;mesiac;vydavky_eur\n00313271;2024;1;1200\n00313271;2024;2;1300\n"


def _waiting_hyp(cfg, db, fields=(("Výdavky obce", True), ("Termíny projektov", True), ("Počet zamestnancov", False))):
    hid = gate1_hyp(db)
    for name, key in fields:  # agent ich hľadal a zdroj nenašiel – automatický prieskum je hotový
        db.update_field(db.upsert_field(hid, name, "", key, ""), status="missing")
    db.start_gate1(hid)
    return hid


def _upload(cfg, db, hid, text, name, content):
    iid = db.add_gate1_input(hid, text, name)
    path = gate1.upload_path(cfg, hid, iid, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return iid, path


def test_low_readiness_waits_for_user_and_manual_decision_wins(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = _waiting_hyp(cfg, db)
    assert gate1.run_finish(cfg, db, Deadline(1), [hid])["needs_input"] == 1
    assert db.get_hypothesis(hid)["gate1_status"] == "needs_input" and not db.gate_decisions_for(hid, 1)
    assert "needs_input" not in gate1.run_finish(cfg, db, Deadline(1), [hid])  # stav sa len drží
    gate1.decide(db, hid, 1, "paused", "čakám na partnera")
    for f in db.fields_for(hid):
        db.update_field(f["id"], status="found")  # aj keby pripravenosť stúpla, ručné rozhodnutie sa neprepíše
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert db.get_hypothesis(hid)["gate1_status"] == "paused"


def test_user_data_is_imported_judged_per_field_and_scored_by_code(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = _waiting_hyp(cfg, db)
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    org, _ = db.upsert_organization("Mesto Fiktívne", "mesto fiktivne", "search", ico="00313271")
    iid, path = _upload(cfg, db, hid, "Mesačné výdavky obcí podľa IČO za rok 2024.", "vydavky.csv", USER_CSV)
    seen = []

    def judge(messages):
        seen.append(messages[-1]["content"])
        return {"fields": [{"field": "výdavky obce", "coverage": "yes", "reason": "stĺpec vydavky_eur"},
                           {"field": "Termíny projektov", "coverage": "no", "reason": "nie sú"},
                           {"field": "Počet zamestnancov", "coverage": "YES", "reason": "v texte"},
                           {"field": "Úplne iné pole", "coverage": "yes", "reason": "x"}],
                "summary": "Dáta pokrývajú výdavky."}

    rec.responses["UserDataEval"] = judge
    assert gate1.run_user_inputs(cfg, db, router, Deadline(1), None) == {"files": 1, "evaluated": 1, "auto_passed": 1}
    assert rec.models_for("UserDataEval") == ["radar-qwen3.6-27b"]  # sudca, nie generátor
    assert "<untrusted_document>" in seen[0] and "vydavky_eur" in seen[0]  # model videl aj stĺpce nahranej tabuľky
    inp = db.gate1_inputs_for(hid)[0]
    ev = json.loads(inp["evaluation_json"])
    # kľúčové 0,35 + 0,35, ostatné 0,3: áno + nie + áno = 0,65 (počíta kód, neznáme pole modelu sa ignoruje)
    assert inp["status"] == "evaluated" and inp["score"] == pytest.approx(0.65)
    assert {r["pole"]: r["pokrytie"] for r in ev["polia"]} == {"Výdavky obce": "yes", "Termíny projektov": "no",
                                                               "Počet zamestnancov": "yes"}
    ds = db.get_dataset(inp["dataset_id"])
    assert ds["status"] == "ok" and ds["url"] == f"user://vstup-{iid}/vydavky.csv" and ds["sha256"]
    assert run_query(cfg, hid, f"SELECT SUM(vydavky_eur) FROM {ds['table_name']}")[0][0] == 2500
    assert [c["org_name"] for c in db.coverage_for(hid)] == ["Mesto Fiktívne"]
    assert not path.exists()  # pôvodný súbor sa po importe zmaže
    assert db.get_hypothesis(hid)["gate1_status"] == "passed"
    assert "vhodnosť tvojich dát 0,65" in db.gate_decisions_for(hid, 1)[0]["reason"]
    assert gate1.run_user_inputs(cfg, db, router, Deadline(1), None) == {}  # nič nečaká


def test_user_input_with_personal_data_low_score_and_model_failure(tmp_path):
    from radar.llm import LLMError

    cfg, db, rec, router = setup(tmp_path)
    hid = _waiting_hyp(cfg, db, (("Výdavky obce", True),))
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    iid, path = _upload(cfg, db, hid, "Zoznam ľudí so sumami.", "ludia.csv", PERSONAL_CSV)
    rec.responses["UserDataEval"] = LLMError("timeout")
    assert gate1.run_user_inputs(cfg, db, router, Deadline(1), [hid]) == {"files": 1, "llm_failures": 1}
    inp = db.gate1_inputs_for(hid)[0]
    assert inp["status"] == "pending" and db.get_dataset(inp["dataset_id"])["status"] == "personal_data_blocked"
    seen = []
    rec.responses["UserDataEval"] = lambda m: seen.append(m[-1]["content"]) or {
        "fields": [{"field": "Výdavky obce", "coverage": "partial", "reason": "iba sumy"}], "summary": ""}
    assert gate1.run_user_inputs(cfg, db, router, Deadline(1), [hid]) == {"evaluated": 1, "auto_passed": 1}  # bez 2. importu
    inp = db.gate1_inputs_for(hid)[0]
    ev = json.loads(inp["evaluation_json"])
    assert inp["score"] == 0.5 and "Nováková" not in seen[0]  # osobné údaje sa k modelu nedostali
    assert any("osobné údaje" in n for n in ev["poznámky"])
    assert db.get_hypothesis(hid)["gate1_status"] == "passed"  # 0,5 ≥ 0,5 (iba z textu)
    low = _waiting_hyp(cfg, db, (("Termíny", True),))
    gate1.run_finish(cfg, db, Deadline(1), [low])
    db.add_gate1_input(low, "Nemám nič, iba tušenie.")
    rec.responses["UserDataEval"] = {"fields": [{"field": "Termíny", "coverage": "no", "reason": "chýba"}]}
    assert gate1.run_user_inputs(cfg, db, router, Deadline(1), [low]) == {"evaluated": 1}
    assert db.get_hypothesis(low)["gate1_status"] == "needs_input"  # čaká na ďalší vstup


def test_autopilot_evaluates_user_inputs_and_researches_waiting_hypotheses_only_after_reopen(tmp_path):
    from radar.autopilot import ROUND, gate1_ids, pending_work
    from radar.workflows import ACTIONS, _gate1_input

    cfg, db, rec, router = setup(tmp_path)
    hid = _waiting_hyp(cfg, db)
    assert gate1_ids(db) == [hid]  # nikdy nedokončená
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert gate1_ids(db) == []  # čaká na tvoje dáta – automaticky sa znova neprehľadáva
    assert gate1.reopen(db, hid) == 3 and gate1_ids(db) == [hid]  # „Prehľadať znova“
    assert {f["status"] for f in db.fields_for(hid)} == {"open"}
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert db.get_hypothesis(hid)["gate1_status"] == "running"  # polia čakajú na agenta – nie „čaká na tvoje dáta“
    assert "gate1-input" not in pending_work(cfg, db)
    db.add_gate1_input(hid, "schéma: obec; rok; výdavky v €")
    assert "H" + str(hid) in pending_work(cfg, db)["gate1-input"]
    kinds = [k for k, _ in ROUND]
    assert kinds.index("gate1-input") < kinds.index("gate1") < kinds.index("gate2") and "gate1-input" in ACTIONS
    ran = []
    app = SimpleNamespace(cfg=cfg, db=db)
    _gate1_input(app, Deadline(15), lambda stage, dl: ran.append(stage), None, lambda **kw: None)
    assert ran == ["gate1-user", "gate1-finish"] and app.gate_ids == [hid]


def test_repro_target_skips_years_and_dates_and_readiness_ignores_missing_target(tmp_path):
    """H11: ciele „10 mil. €“ (metodika), „40 %“ a „2025 decembra“ – reprodukcia „nevyšla“ a znížila pripravenosť."""
    assert gate1.repro_target([{"value": 2025, "unit": "decembra"}, {"value": 2024, "unit": ""}]) is None
    assert gate1.repro_target([{"value": 40, "unit": "%", "meaning": "vlastnícky podiel"},
                               {"value": 1.5, "unit": "mil. €", "meaning": "výdavky"}])["value"] == 1.5
    assert gate1.repro_target([{"value": 120, "unit": "", "meaning": "počet zmlúv"}])["value"] == 120
    cfg, db, rec, router, hid, fid, org, fetcher = _dataset_env(tmp_path)
    gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    db.update_field(fid, status="found")
    doc, _ = db.upsert_document("https://nku.gov.sk/x", "NKÚ", "search", "t", "q", "h", "V roku 2025 …")
    cid = db.insert_claim(doc, 0, "t", "Kontrola v decembri 2025", "obce", "cost", "V decembri 2025",
                          [{"value": 2025, "unit": "decembra", "meaning": "dátum"}])
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    assert gate1.run_repro(cfg, db, router, Deadline(1), [hid]) == {"no_target": 1}  # model sa ani nevolal
    summary = gate1.readiness(cfg, db, hid)
    assert "reprodukcia" not in summary["zložky"] and summary["reprodukcia"] == "no_target"
    assert abs(sum(summary["váhy"].values()) - 1) < 0.01 and summary["skóre"] == 0.75  # s nulovou reprodukciou 0,6

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
    assert gate2.run_backtest(cfg, db, Deadline(1), [hid]) == {"not_locked": 1}  # bez uzamknutia sa netestuje
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



def test_experiment_trace_explains_selection_prereg_data_folds_and_rule(tmp_path):
    """Postup experimentu: prečo organizácia, kto navrhol, SQL a rad, každé kolo testu, výber auto, pravidlo s prahom."""
    from radar.gate2_trace import as_markdown, explain

    cfg, db, rec, router, hid, org, table = gate2_env(tmp_path)
    rec.responses["Prereg"] = {"statement": "Model predpovie výdavky.", "table": table, "time_column": "rok",
                               "value_column": "suma", "entity_column": "ico", "aggregation": "sum", "model": "auto",
                               "metric": "MAE", "baseline": "last_value",
                               "success_rule": "MAE modelu je aspoň o 10 % nižšia ako MAE naivného odhadu."}
    gate2.run_prereg(cfg, db, router, Deadline(1), [hid])
    exp = db.experiments_for(hid)[0]
    assert [s["krok"][:2] for s in explain(cfg, db, exp)] == ["1.", "2.", "3."]  # bez testu iba návrh
    gate2.lock(cfg, db, exp["id"])
    gate2.run_backtest(cfg, db, Deadline(1), [hid])
    exp = db.get_experiment(exp["id"])
    assert json.loads(exp["flags_json"])["pravidlo_nesplnené"] is False  # lineárny rad: zlepšenie 100 % ≥ 10 %
    steps = {s["krok"]: s for s in explain(cfg, db, exp)}
    assert "Mesto Fiktívne" in steps["1. Prečo táto organizácia"]["text"]
    assert "radar-qwen3.6-27b" in steps["2. Návrh predregistrácie"]["text"]
    assert "00313271" in steps["4. Dáta pre test"]["kód"] and len(steps["4. Dáta pre test"]["tabuľka"]) == 10
    folds = steps["5. Spätný test po kolách (rolling origin)"]["tabuľka"]
    assert len(folds) == 4 and folds[0]["história"].endswith("(6 období)")
    assert all(r["vybraný"] for r in steps["5a. Prečo „auto“ vybral daný model"]["tabuľka"])
    assert "**splnené**" in steps["6. Výpočet výsledku"]["text"]
    assert "## 5. Spätný test" in as_markdown(exp, list(steps.values()))
    assert gate2.rule_threshold("MAPE je aspoň o 12,5 % nižšia") == 12.5 and gate2.rule_threshold("MAE < naivný") is None

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
    fid = db.upsert_field(hid, "Výdavky obce na obstarávanie", "", True, "")  # otvorené – agent ho ešte nehľadal
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
    csv = "ico;datum;suma\n" + rows + rows.replace(";1", ";1", 1) + "00313271;3202-12-18;999\n"  # preklep v roku (CRZ)
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/files/vydavky.csv"
    gate1.run_datasets(cfg, db, mock_fetcher({url: httpx.Response(200, content=csv.encode(), headers={"content-type": "text/csv"})}),
                       Deadline(1), [hid])
    table = db.datasets_for(hid)[0]["table_name"]
    spec = {"table": table, "time_column": "datum", "value_column": "suma", "entity_column": "ico", "entity_value": "00313271",
            "aggregation": "sum", "period": "quarter"}
    data = gate2.series(cfg, hid, spec)
    assert len(data) == 12 and str(data[0][0]) == "2015-01-01" and data[0][1] == 200  # dva riadky v štvrťroku sa sčítajú


class PagedApi:
    """Falošné API: 2 500 záznamov, stránkovanie kurzorom minId (ako ITMS2014+) alebo offsetom."""

    def __init__(self, mode="cursor", n=2500, ignore_paging=False):
        self.mode, self.n, self.ignore, self.calls = mode, n, ignore_paging, []
        self.blocked = []

    def fetch_json(self, url):
        from urllib.parse import parse_qs, urlparse

        self.calls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        limit = int(q.get("limit", 100))
        start = 0 if self.ignore else (int(q.get("minId", 0)) if self.mode == "cursor" else int(q.get("offset", 0)))
        recs = [{"id": i, "suma": i * 10, "prijimatel": {"subjekt": {"ico": "00313271"}}}
                for i in range(start + 1, min(self.n, start + limit) + 1)]
        return recs if self.mode == "cursor" else {"data": recs, "total": self.n}


def test_api_pagination_cursor_offset_and_ignored_paging(tmp_path):
    from radar.datasets import download_paginated

    cfg, db, rec, router = setup(tmp_path)
    assert gate1.paging_of({"limit", "minId", "modifiedSince"}) == {"limit": "limit", "cursor": "minId", "offset": None,
                                                                     "page": None}
    assert gate1.paging_of({"vyzvaId"}) is None
    api = PagedApi("cursor")
    dl = download_paginated(cfg, api, "https://api.example.sk/v2/projekty?limit=1000", tmp_path / "d", [],
                            {"limit": "limit", "cursor": "minId"})
    lines = dl.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2500 and json.loads(lines[-1])["id"] == 2500 and len(api.calls) == 3
    assert "minId=2000" in api.calls[-1] and dl.note == "API: 3 strán, 2500 záznamov"
    api = PagedApi("offset")
    dl = download_paginated(cfg, api, "https://api.example.sk/items", tmp_path / "d", [], {"limit": "limit", "offset": "offset"})
    assert len(dl.path.read_text(encoding="utf-8").splitlines()) == 2500 and "offset=2000" in api.calls[-1]
    api = PagedApi("cursor", ignore_paging=True)  # server ignoruje minId → po opakovanej strane koniec
    dl = download_paginated(cfg, api, "https://api.example.sk/v2/projekty", tmp_path / "d", [], {"limit": "limit", "cursor": "minId"})
    assert len(api.calls) == 2 and len(dl.path.read_text(encoding="utf-8").splitlines()) == 1000


def test_paginated_api_replaces_single_page_dataset(tmp_path):
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://api.example.sk/v2/projekty?limit=1000"
    src = db.sources_for(hid)[0]
    db.add_dataset(hid, src["id"], url, "ds99", status="ok", rows=1000, note=None)  # stará prvá strana
    db.update_source(src["id"], checks_json={"http_ok": True, "súbory": [url],
                                             "stránkovanie": {url: {"limit": "limit", "cursor": "minId"}}})
    stats = gate1.run_datasets(cfg, db, PagedApi("cursor"), Deadline(1), [hid])
    ds = [d for d in db.datasets_for(hid) if d["url"] == url][0]
    assert stats["datasets"] == 1 and ds["rows"] == 2500 and ds["note"].startswith("API: 3 strán")
    assert [c["org_name"] for c in db.coverage_for(hid)] == ["Mesto Fiktívne"]
    assert gate1.run_datasets(cfg, db, PagedApi("cursor"), Deadline(1), [hid]) == {}  # už stránkované → nesťahuje znova


def _zip_bytes(name, data):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def test_zip_dataset_is_unpacked_safely(tmp_path):
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/files/vydavky.zip"
    src = db.sources_for(hid)[0]
    db.update_source(src["id"], checks_json={"http_ok": True, "súbory": [url]})
    body = _zip_bytes("../../zle/vydavky.csv", CSV.encode("cp1250"))  # meno v archíve sa ako cesta nepoužije
    fetcher = mock_fetcher({url: httpx.Response(200, content=body, headers={"content-type": "application/zip"})})
    assert gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])["datasets"] == 1
    ds = [d for d in db.datasets_for(hid) if d["url"] == url][0]
    assert ds["rows"] == 3 and "ico" in json.loads(ds["columns_json"])
    assert not (tmp_path / "zle").exists() and not list((tmp_path / "data" / "datasets" / f"H{hid}").glob("*.unzipped*"))

    cfg2, db2, *_, hid2, _, _, _ = _dataset_env(tmp_path / "b", max_dataset_mb=0)  # zip bomba / limit
    src2 = db2.sources_for(hid2)[0]
    db2.update_source(src2["id"], checks_json={"http_ok": True, "súbory": [url]})
    gate1.run_datasets(cfg2, db2, mock_fetcher({url: httpx.Response(200, content=body)}), Deadline(1), [hid2])
    assert db2.datasets_for(hid2)[0]["status"] == "too_large"


def test_xml_records_become_columns_without_external_entities(tmp_path):
    secret = tmp_path / "tajne.txt"
    secret.write_text("TAJNY-OBSAH", encoding="utf-8")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE zakazky [<!ENTITY xxe SYSTEM "file:///{secret.as_posix()}">]>
<zakazky xmlns="urn:vestnik">
  <zakazka id="1"><ico>00313271</ico><suma>400000</suma><obstaravatel><nazov>Mesto Fiktívne</nazov></obstaravatel><pozn>&xxe;</pozn></zakazka>
  <zakazka id="2"><ico>00313271</ico><suma>500000</suma><obstaravatel><nazov>Mesto Fiktívne</nazov></obstaravatel><pozn>x</pozn></zakazka>
</zakazky>"""
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/vestnik.xml"
    src = db.sources_for(hid)[0]
    db.update_source(src["id"], checks_json={"http_ok": True, "súbory": [url]})
    fetcher = mock_fetcher({url: httpx.Response(200, content=xml.encode(), headers={"content-type": "application/xml"})})
    assert gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid]) == {"datasets": 1, "coverage": 1}
    ds = [d for d in db.datasets_for(hid) if d["url"] == url][0]
    cols = json.loads(ds["columns_json"])
    assert ds["rows"] == 2 and {"id", "ico", "suma", "obstaravatel_nazov"} <= set(cols)
    rows = run_query(cfg, hid, f"SELECT pozn FROM {ds['table_name']}")
    assert all("TAJNY" not in str(r[0]) for r in rows)  # externá entita sa nenačítala (XXE)


def test_search_retries_blocked_engines_relaxes_quotes_and_caches(monkeypatch):
    """Reálne behy: DuckDuckGo CAPTCHA a Google CSE limit – zopakovanie po pauze, potom bez úvodzoviek, pamäť dopytov."""
    from radar.config import SearchConfig
    from radar.search import SearxClient

    sleeps, calls = [], []
    monkeypatch.setattr("radar.search.time.sleep", lambda s: sleeps.append(s))
    blocked = {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["google cse", "too many requests"]]}
    found = {"results": [{"url": "https://www.fiktivne-mesto.sk/urad/kontakty", "title": "Kontakty", "content": "Jana"}]}

    def handler(request):
        q = request.url.params["q"]
        calls.append(q)
        if '"' in q:
            return httpx.Response(200, json=blocked)  # presná fráza nič nenájde ani po zopakovaní
        return httpx.Response(200, json=found)

    client = SearxClient(SearchConfig(delay_s=0), "test")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    res = client.search('"Jana Nováková" site:fiktivne-mesto.sk')
    assert [r.url for r in res] == ["https://www.fiktivne-mesto.sk/urad/kontakty"]
    assert calls == ['"Jana Nováková" site:fiktivne-mesto.sk'] * 2 + ["Jana Nováková site:fiktivne-mesto.sk"]
    assert 4.0 in sleeps and client.penalty == 2.0  # pauza po blokovaní, po úspechu sa zmenší
    assert client.search('"Jana  Nováková" site:fiktivne-mesto.sk') == res and len(calls) == 3  # z pamäte


def test_publisher_types_rules_model_once_per_domain_and_vendor_not_counted(tmp_path):
    from radar.publishers import rule_type, run_source_types
    from radar.strength import evidence_strength

    assert rule_type("www.nku.gov.sk") == "government" and rule_type("ec.europa.eu") == "government"
    assert rule_type("domov.sme.sk") == "media" and rule_type("ineko.sk") == "research" and rule_type("firma.sk") is None
    QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie."
    DOC = "Úvod. " * 30 + QUOTE + " Záver. " * 30
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "obce")
    docs = []
    for i, url in enumerate(["https://www.nku.gov.sk/a", "https://softver-firma.sk/blog/a", "https://softver-firma.sk/blog/b",
                             "https://ina-firma.sk/riesenie"]):
        doc, _ = db.upsert_document(url, "T", "search", "t", "q", f"h{i}", DOC)
        cid = db.insert_claim(doc, 0, "t", "Obce nemajú kapacitu", "obce", "pain", QUOTE + f" {i}")
        db.set_claim_verification(cid, "verified", 100, "ok")
        db.assign_claim(hid, cid)
        docs.append(doc)
    assert evidence_strength(db.evidence_for(hid))[0] == "silnejšia"  # bez typov: 4 overené z 3 domén
    rec.responses["SourceTypeVerdict"] = {"source_type": "vendor", "reason": "predáva softvér"}
    stats = run_source_types(cfg, db, router, Deadline(1))
    assert stats["rule:government"] == 1 and stats["model:vendor"] == 2
    assert rec.models_for("SourceTypeVerdict") == ["radar-qwen3.5-9b"] * 2  # raz na doménu, nie na dokument
    label, n_ver, _, n_dom = evidence_strength(db.evidence_for(hid))
    assert (label, n_ver, n_dom) == ("slabá", 1, 1)  # citáty dodávateľov sa nepočítajú
    doc, _ = db.upsert_document("https://softver-firma.sk/blog/c", "T", "search", "t", "q", "h9", DOC)
    assert run_source_types(cfg, db, router, Deadline(1))["from_known_domains"] == 1  # nový dokument známej domény
    assert db.get_document(doc)["source_type"] == "vendor"


def test_person_contact_from_pdf_phone_list(tmp_path):
    """Telefónny zoznam úradu v PDF: kontakt sa vezme iba z okna pri mene osoby (nie suseda)."""
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "obce")
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Mesto Fiktívne", "mesto fiktivne", "search", website="https://www.fiktivne-mesto.sk/",
                                    website_verified=1)
    pid = db.insert_person(org, "Peter Vzorový", "peter vzorovy", "https://api.statistics.sk/rpo", "RPO", hid,
                           role_title="vedúci odboru", source_kind="rpo")
    db.set_link_priority(db.upsert_link(pid, hid, "org_affected"), 0.6, {})
    pdf = "https://www.fiktivne-mesto.sk/files/telefonny-zoznam.pdf"
    text = ("Telefónny zoznam mestského úradu. " * 10 + "Ing. Jana NOVÁKOVÁ prednostka 041 513 5101 "
            "jana.novakova@fiktivne-mesto.sk Peter Vzorový vedúci odboru výstavby 041 513 5102 "
            "peter.vzorovy@fiktivne-mesto.sk Eva Malá referentka 041 513 5103")
    searx = FakeSearx({"filetype:pdf": [SearchResult(url=pdf, title="Telefónny zoznam", snippet="")]})
    stats = run_person_contacts(cfg, db, searx, FakeFetcher(texts={pdf: text}), Deadline(1))
    got = {(c["kind"], c["value"], c["extraction"]) for c in db.contacts_for_person(pid)}
    assert got == {("phone", "+421415135102", "person_pdf"), ("email", "peter.vzorovy@fiktivne-mesto.sk", "person_pdf")}
    assert stats["people_found"] == 1


def test_wide_table_with_periods_as_columns_gets_long_form(tmp_path):
    """ŠÚ SR DATAcube: roky ako stĺpce, desatinná čiarka, „:“ za chýbajúcu hodnotu → dsN_long pre časový rad."""
    from radar.datasets import drop_table, schema_text, table_columns

    wide = ("Územie;Ukazovateľ;2016;2017;2018;2019;2020;2021\n"
            "Žilinský kraj;Výdavky [mil. EUR];1,5;1,7;2,0;2,1;2,6;:\n"
            "Nitriansky kraj;Výdavky [mil. EUR];1 200,5;1 250;1 300;1 310;1 400;1 450,25\n")
    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path)
    url = "https://data.obce.sk/files/vydavky.csv"
    fetcher = mock_fetcher({url: httpx.Response(200, content=wide.encode("cp1250"), headers={"content-type": "text/csv"})})
    stats = gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    assert stats["datasets"] == 1 and stats["long_tables"] == 1
    ds = db.datasets_for(hid)[0]
    table, prof = ds["table_name"], json.loads(ds["profile_json"])
    assert prof["period_columns"] == {"granularita": "year", "počet": 6}
    assert prof["time_span"]["obdobia v stĺpcoch"] == ["2016-01-01", "2021-01-01"]
    assert prof["dlhý_tvar"]["tabuľka"] == f"{table}_long" and prof["dlhý_tvar"]["riadky"] == 12
    assert prof["dlhý_tvar"]["hodnoty"] == 11  # „:“ je chýbajúca hodnota, nie nula
    assert run_query(cfg, hid, f"SELECT hodnota FROM {table}_long WHERE Územie = 'Žilinský kraj' "
                               "AND obdobie = DATE '2016-01-01'")[0][0] == 1.5
    schema = schema_text(cfg, hid, [table])
    assert "wide" in schema and f"TABLE {table}_long" in schema and "2016…2021" in schema
    spec = {"table": f"{table}_long", "time_column": "obdobie", "value_column": "hodnota", "aggregation": "sum", "period": "none"}
    data = gate2.series(cfg, hid, spec)
    assert len(data) == 6 and data[0][1] == 1202.0 and data[-1][1] == 1450.25
    drop_table(cfg, hid, table)
    assert table_columns(cfg, hid, f"{table}_long") == []


def test_backtest_season_length_and_new_models():
    assert gate2.season_length("month", []) == 12 and gate2.season_length("quarter", []) == 4
    assert gate2.season_length("none", ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01"]) == 4
    assert gate2.season_length("none", ["2020-01-01", "2020-02-01", "2020-03-01"]) == 12
    assert gate2.season_length("none", [2015, 2016, 2017]) == 1
    # Štvrťročná sezónnosť s rastom: sezónny model ju zachytí, trend nie; sezónny naivný odhad berie hodnotu spred 4 období.
    pattern = [10, 20, 15, 30]
    y = [p + i for i, p in enumerate(pattern * 4)]
    seasonal = gate2.backtest_series(y, "seasonal_naive", "seasonal_trend", folds=4, min_history=8, season=4)
    trend = gate2.backtest_series(y, "seasonal_naive", "linear_trend", folds=4, min_history=8, season=4)
    assert seasonal["model_error"] < 1e-6 and trend["model_error"] > 3 and seasonal["baseline_error"] == 4
    assert gate2.backtest_series(y, "seasonal_naive", "linear_trend", 4, 8)["baseline_error"] != 4  # bez sezóny = posledná hodnota
    # auto vyberá iba z histórie pred predpoveďou; na čistom trende nájde presný model
    auto = gate2.backtest_series([3 * t + 2 for t in range(12)], "last_value", "auto", folds=4, min_history=6)
    assert auto["model_error"] < 1e-6 and all(f["vybraný_model"] for f in auto["folds"])
    level = [50.0] * 6 + [80.0] * 6  # skok úrovne: exponenciálne vyrovnávanie sa prispôsobí
    assert gate2.forecast(np.asarray(level), "ses") > 75 and 50 < gate2.forecast(np.asarray(level), "holt_damped") < 110
    with pytest.raises(ValueError):
        gate2.forecast(np.asarray(level), "prophet")


def test_prereg_filters_select_one_indicator_of_long_table(tmp_path):
    from radar.datasets import hyp_dir, import_table, schema_text, wide_to_long

    cfg, *_ = setup(tmp_path)
    hid = 7  # DuckDB databáza hypotézy (riadok hypotézy v SQLite netreba)
    hyp_dir(cfg, hid).mkdir(parents=True)
    path = tmp_path / "wide.csv"
    rows = "".join(f"{kraj};{ind};" + ";".join(str(base + 10 * i) for i in range(8)) + "\n"
                   for kraj, ind, base in [("ZA", "výdavky", 100), ("ZA", "príjmy", 900), ("NR", "výdavky", 200)])
    path.write_text("kraj;ukazovatel;" + ";".join(str(2016 + i) for i in range(8)) + "\n" + rows, encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    assert wide_to_long(cfg, hid, "ds1")["riadky"] == 24
    assert "values: kraj = NR | ZA; ukazovatel = príjmy | výdavky" in schema_text(cfg, hid, ["ds1"])  # presné hodnoty filtrov
    spec = {"table": "ds1_long", "time_column": "obdobie", "value_column": "hodnota", "aggregation": "sum",
            "filters": {"kraj": "ZA", "ukazovatel": "výdavky"}, "model": "auto"}
    assert gate2.validate_spec(cfg, hid, spec) == []
    data = gate2.series(cfg, hid, spec)
    assert [v for _, v in data] == [100.0 + 10 * i for i in range(8)]
    assert gate2.validate_spec(cfg, hid, spec | {"filters": {"okres": "x"}, "model": "prophet"}) == [
        "stĺpec filtra 'okres' nie je v tabuľke", "neznámy model 'prophet' (možnosti: linear_trend, ar1, ses, holt_damped, "
        "seasonal_trend, auto)"]


# ======================================================================= národný katalóg (SPARQL)
NKOD_DS = "https://data.gov.sk/set/1b2c3d4e-0000-4000-8000-00000000f001"
NKOD_FILE = "https://data.slovensko.sk/download?id=fin112-obce"
FIN_TITLE = "Vybrané údaje z výkazu FIN 1-12 za obce a ich rozpočtové organizácie"


def nkod_transport(queries):
    def b(**kw):
        return {k: {"type": "literal", "value": v} for k, v in kw.items()}

    def handler(request):
        q = request.url.params["query"]
        queries.append(q)
        if "GROUP BY ?d ORDER BY" in q:  # hľadanie datasetov
            rows = [b(d=NKOD_DS, title=FIN_TITLE, desc="Mesačné údaje o príjmoch a výdavkoch obcí.",
                      publisher="Ministerstvo financií SR", modified="2026-09-30T10:00:00Z",
                      periodicity="http://publications.europa.eu/resource/authority/frequency/MONTHLY")] \
                if 'CONTAINS(?hay, "rozpoc")' in q and '"vydavk"' not in q else []  # s tretím slovom nič
        elif "dcat:distribution ?dist" in q:
            rows = [b(dist=NKOD_DS + "/r1", title="FIN 1-12 obce 2026", format="http://publications.europa.eu/resource/authority/file-type/CSV",
                      download=NKOD_FILE, license="http://publications.europa.eu/resource/authority/licence/CC_BY_4_0"),
                    b(dist=NKOD_DS + "/r2", title="Mapa", format="http://publications.europa.eu/resource/authority/file-type/GEOJSON",
                      download="https://data.slovensko.sk/download?id=mapa")]
        else:  # dataset podľa URI
            rows = [b(title=FIN_TITLE, publisher="Ministerstvo financií SR")]
        return httpx.Response(200, json={"head": {}, "results": {"bindings": rows}})

    return httpx.MockTransport(handler)


def test_nkod_search_terms_metadata_text_and_failure():
    from radar.catalogs import NKODClient, search_terms

    assert search_terms("Rozpočet obce") == ["rozpoc", "obce"] and search_terms("Výdavky na lieky") == ["vydavk", "liek"]
    queries = []
    client = NKODClient(http=httpx.Client(transport=nkod_transport(queries)))
    [ds] = client.search("Rozpočet obce – výdavky", limit=3)  # tri slová nič nenájdu, dve áno
    assert len([q for q in queries if "GROUP BY ?d ORDER BY" in q]) == 2
    assert ds.formats == ["CSV", "GEOJSON"] and ds.periodicity == "mesačne (MONTHLY)"
    text = ds.text()
    assert "Licencia (podmienky užitia, autorské dielo): CC BY 4.0 (CC_BY_4_0)" in text and NKOD_FILE in text
    assert client.dataset("https://example.com/x") is None  # iba identifikátory katalógu

    def down(request):
        raise httpx.ConnectError("nedostupné", request=request)

    broken = NKODClient(http=httpx.Client(transport=httpx.MockTransport(down)))
    assert broken.search("Rozpočet obce") == [] and broken.failed


def test_catalog_candidates_become_sources_with_files_and_typed_download(tmp_path, monkeypatch):
    """Agent katalóg nepoužije; kandidát predčítaný pre pole prejde záložnou extrakciou, kontrola vezme súbor
    z distribúcií (bez GeoJSON) a import pozná formát z katalógu (odkaz bez prípony)."""
    from radar.catalogs import NKODClient

    queries = []
    monkeypatch.setattr(gate1, "NKODClient", lambda *a, **k: NKODClient(http=httpx.Client(transport=nkod_transport(queries))))
    cfg, db, rec, router = setup(tmp_path, nkod_enabled=True)
    hid = gate1_hyp(db)
    db.upsert_field(hid, "Rozpočet obce", "výdavky obce podľa položiek", True, "")
    rec.responses["tools"] = [step(call("finish", {"summary": "nič"}))]
    rec.responses["ExtractedSources"] = lambda messages: {"sources": [
        {"field": "Rozpočet obce", "name": FIN_TITLE, "url": NKOD_FILE, "access": "open", "license": "CC BY 4.0",
         "update_frequency": "mesačne", "formats": "CSV", "evidence_quote": FIN_TITLE}]} \
        if NKOD_DS in messages[-1]["content"] else {"sources": []}
    stats = gate1.run_sources(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), [hid])
    assert stats["catalog_candidates"] == 1 and stats["sources_from_pages"] == 1
    [src] = db.sources_for(hid)
    assert src["license"] == "CC BY 4.0" and src["update_frequency"] == "mesačne" and src["evidence_url"] == NKOD_DS
    kind = db.conn.execute("SELECT source_kind FROM documents WHERE url = ?", (NKOD_DS,)).fetchone()[0]
    assert kind == "gate1"  # metadáta katalógu nejdú do extrakcie dôkazov

    gate1.run_check(cfg, db, FakeFetcher(), Deadline(1), [hid])
    checks = json.loads(db.sources_for(hid)[0]["checks_json"])
    assert checks["súbory"] == [NKOD_FILE] and checks["formát_podľa_katalógu"] == {NKOD_FILE: "csv"}
    csv = "ico;rok;vydavky\n00313271;2025;100\n00313271;2026;120\n"
    fetcher = mock_fetcher({NKOD_FILE: httpx.Response(200, content=csv.encode(), headers={"content-type": "application/octet-stream"})})
    assert gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])["datasets"] == 1
    assert db.datasets_for(hid)[0]["format"] == "csv" and db.datasets_for(hid)[0]["rows"] == 2


def test_noncommercial_license_is_not_counted_as_open():
    src = {"formats": "CSV", "license": "CC BY-NC 4.0 (CC_BYNC_4_0)", "update_frequency": "ročne", "access": "open"}
    q = gate1.source_quality(src, {"http_ok": True, "súbory": ["x.csv"]})
    assert q["licencia"] == 0.3 and "NC/ND" in q["obmedzenie_licencie"]
    assert gate1.source_quality(src | {"license": "CC BY 4.0"}, {"http_ok": True})["licencia"] == 1.0


def test_crz_daily_exports_deduplicated_without_supplier_names(tmp_path):
    """Katalóg uvádza iba adresár crz.gov.sk/export/ (HTTP 403); dávky sú denné ZIP s dátumom. Zmluva zmenená
    v dvoch dňoch ostane raz (posledná zmena); meno dodávateľa (fyzická osoba) ani adresa sa neuložia."""
    from datetime import date, timedelta

    def zmluva(zid, suma, dodavatel, ico=""):
        return (f"<zmluva><nazov>Z-{zid}</nazov><ID>{zid}</ID><zs1>Mesto Fiktívne</zs1><zs2>{dodavatel}</zs2>"
                f"<predmet>Oprava cesty</predmet><datum>2026-09-01</datum><datum_platnost_do>0000-00-00</datum_platnost_do>"
                f"<suma_zmluva>{suma}</suma_zmluva><suma_spolu>{suma}</suma_spolu><ico>{ico}</ico><ico1>00313271</ico1>"
                f"<sidlo>Ulica 1, Mesto</sidlo><datum_zverejnene>2026-09-02 10:00:00</datum_zverejnene>"
                f"<prilohy><priloha><ID>9{zid}</ID><nazov>príloha</nazov></priloha></prilohy></zmluva>")

    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path, crz_days=3, crz_delay_s=0)
    export = "http://www.crz.gov.sk/export/"
    src = db.sources_for(hid)[0]
    db.update_source(src["id"], checks_json={"http_ok": True, "súbory": [export]})
    db.add_dataset(hid, src["id"], export, "pending", status="error", note="HTTP 403")  # predchádzajúci pokus
    end = date.today() - timedelta(days=1)
    days = [(end - timedelta(days=i)).isoformat() for i in (2, 1, 0)]
    batches = {days[0]: zmluva(1, "100", "Ing. Fiktívny Živnostník") + zmluva(2, "200", "Firma s.r.o.", "12345678"),
               days[2]: zmluva(1, "150,5", "Ing. Fiktívny Živnostník")}  # days[1] chýba (404)
    routes = {f"https://www.crz.gov.sk/export/{d}.zip": httpx.Response(
        200, content=_zip_bytes(f"{d}.xml", f"<?xml version='1.0' encoding='UTF-8'?><zmluvy>{x}</zmluvy>".encode()),
        headers={"content-type": "application/zip"}) for d, x in batches.items()}
    stats = gate1.run_datasets(cfg, db, mock_fetcher(routes), Deadline(1), [hid])
    assert stats["datasets"] == 1
    ds = [d for d in db.datasets_for(hid) if d["url"] == export][0]
    assert ds["status"] == "ok" and "2 denných dávok (1 chýba)" in ds["note"]
    table = ds["table_name"]
    cols = json.loads(ds["columns_json"])
    assert "zs2" not in cols and "sidlo" not in cols and "_den" not in cols and "ico_objednavatel" in cols
    assert run_query(cfg, hid, f'SELECT COUNT(*), SUM(suma_zmluva) FROM {table}')[0] == (2, 350.5)  # ID 1 iba raz, posledná zmena
    assert [c["org_name"] for c in db.coverage_for(hid)] == ["Mesto Fiktívne"]  # IČO objednávateľa (ico1)
    # okno dávok pre časový rad Brány 2: od prvej po poslednú stiahnutú dávku
    assert json.loads(ds["profile_json"])["časové_okno"] == {"od": days[0], "do": days[2], "podľa": "deň zverejnenia (dávky)"}



def test_coverage_prefers_own_side_of_contract_and_dirty_ico(tmp_path):
    """Úloha #44: MF SR bolo v CRZ párované ako dodávateľ (výpomoci obciam), nie ako objednávateľ vlastných zmlúv;
    IČO objednávateľa býva „51 049 775“ alebo „IČO: 00313271“."""
    from radar.datasets import coverage, hyp_dir, import_table

    cfg, *_ = setup(tmp_path)
    hid = 3
    hyp_dir(cfg, hid).mkdir(parents=True)
    path = tmp_path / "crz.csv"
    rows = (["IČO: 00313271;99999999"] * 2 + ["00313271;99999999"] + ["12345678;00313271"] * 5
            + ["51 049 775;12345678", "  ;00151742"])
    path.write_text("ico_objednavatel;ico_dodavatel\n" + "\n".join(rows) + "\n", encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    got = coverage(cfg, hid, "ds1", ["ico_objednavatel", "ico_dodavatel"],
                   [(1, "00313271"), (2, "51049775"), (3, "151742")])
    assert sorted(got) == [(1, "ico_objednavatel", 3), (2, "ico_objednavatel", 1), (3, "ico_dodavatel", 1)]


def test_backtest_uses_only_complete_periods_inside_data_window():
    """CRZ (okno dávok 15. 1. – 11. 10.): január je neúplný, október beží, november je preklep; mesiac bez zmlúv = 0."""
    data = [(gate2.date(2023, 12, 1), 5.0), (gate2.date(2026, 1, 1), 4.0), (gate2.date(2026, 2, 1), 10.0),
            (gate2.date(2026, 4, 1), 30.0), (gate2.date(2026, 9, 1), 20.0), (gate2.date(2026, 10, 1), 1.0),
            (gate2.date(2026, 11, 1), 2.0)]
    limits = {"od": "2026-01-15", "do": "2026-10-11"}
    kept, notes = gate2.complete_periods(data, "month", limits, "sum", today=gate2.date(2026, 10, 13))
    assert [str(t) for t, _ in kept] == [f"2026-{m:02d}-01" for m in range(2, 10)]
    assert dict((str(t), v) for t, v in kept)["2026-03-01"] == 0.0 and len(notes["doplnené_nuly"]) == 5
    assert notes["vyradené_neúplné"] == ["2023-12-01", "2026-01-01", "2026-10-01", "2026-11-01"]
    avg, notes = gate2.complete_periods(data, "month", limits, "avg", today=gate2.date(2026, 10, 13))
    assert len(avg) == 3 and "doplnené_nuly" not in notes  # priemer prázdneho mesiaca nie je 0
    # okno začína prvým dňom štvrťroka → ten štvrťrok ostáva; bez okna platí deň pred stiahnutím
    q = [(gate2.date(2026, 1, 1), 1.0), (gate2.date(2026, 7, 1), 2.0)]
    assert len(gate2.complete_periods(q, "quarter", {"od": "2026-01-01", "do": "2026-09-30"})[0]) == 3
    assert gate2.complete_periods(q, "none", limits) == (q, {})



def test_prereg_period_fits_data_window_and_lock_needs_enough_periods(tmp_path):
    """Úloha #45: 27B zvolil roky pri 9-mesačnom okne CRZ (0 úplných rokov). Kód zoskupenie opraví a zapíše prečo;
    návrh s príliš krátkym radom sa nedá uzamknúť; model dostane okno dát v zadaní."""
    from radar.datasets import hyp_dir, import_table

    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    org, _ = db.upsert_organization("Mesto Fiktívne", "mesto fiktivne", "search", ico="00313271")
    hyp_dir(cfg, hid).mkdir(parents=True)
    path = tmp_path / "crz.csv"
    days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 11) for d in (3, 17) if (m, d) != (1, 3)]
    path.write_text("ico_objednavatel;datum;suma\n" + "".join(f"00313271;{x};{100 + i}\n" for i, x in enumerate(days)),
                    encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    ds = db.add_dataset(hid, None, "http://www.crz.gov.sk/export/", "ds1", status="ok",
                        profile_json={"časové_okno": {"od": "2026-01-15", "do": "2026-10-11"}, "ico_columns": ["ico_objednavatel"]})
    db.set_coverage(ds, org, "ico_objednavatel", len(days))
    gate1.decide(db, hid, 1, "passed", "test")
    seen = []
    rec.responses["Prereg"] = lambda m: seen.append(m) or {
        "statement": "Trend predpovie ročné výdavky.", "table": "ds1", "time_column": "datum", "value_column": "suma",
        "entity_column": "ico_objednavatel", "aggregation": "sum", "period": "year", "model": "auto"}
    assert gate2.run_prereg(cfg, db, router, Deadline(1), [hid]) == {"drafts": 1, "period_fixed": 1}
    assert "8 months, 2 quarters, 0 years" in seen[0][1]["content"] and "at least 9" in seen[0][0]["content"]
    exp = db.experiments_for(hid)[0]
    assert json.loads(exp["spec_json"])["period"] == "month"  # 8 úplných mesiacov (február–september)
    assert "year → month" in json.loads(exp["flags_json"])["obdobie_upravené"]
    gate2.lock(cfg, db, exp["id"])  # 8 ≥ 6 → dá sa uzamknúť
    short = db.add_experiment(hid, None, None, "x", "MAE", "last_value", "x",
                              {"table": "ds1", "time_column": "datum", "value_column": "suma", "aggregation": "sum",
                               "period": "quarter"})
    with pytest.raises(ValueError, match="2 úplných období"):
        gate2.lock(cfg, db, short)

def test_finish_refreshes_coverage_for_organizations_added_later(tmp_path):
    cfg, db, rec, router, hid, fid, org, fetcher = _dataset_env(tmp_path)
    gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    later, _ = db.upsert_organization("Obec Neskoršia", "obec neskorsia", "search", ico="00603481")  # zber ľudí po stiahnutí
    assert {c["org_name"] for c in db.coverage_for(hid)} == {"Mesto Fiktívne"}
    assert gate1.run_finish(cfg, db, Deadline(1), [hid])["coverage"] == 2
    assert {c["org_name"] for c in db.coverage_for(hid)} == {"Mesto Fiktívne", "Obec Neskoršia"}


def test_crz_window_extends_backwards_in_chunks_and_records_missing_days(tmp_path):
    """36 mesiacov CRZ: staršie dávky po blokoch; okno sa posúva po každom bloku (prerušenie nič nestratí)."""
    from radar.crz import extend_window
    from radar.datasets import hyp_dir, import_table

    cfg, db, rec, router = setup(tmp_path, crz_delay_s=0)
    hid = gate1_hyp(db)
    hyp_dir(cfg, hid).mkdir(parents=True)
    path = tmp_path / "crz.csv"
    path.write_text("ID;suma_spolu\n1;100\n", encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    ds = db.add_dataset(hid, None, "http://www.crz.gov.sk/export/", "ds1", status="ok",
                        profile_json={"časové_okno": {"od": "2026-03-10", "do": "2026-03-20"}})

    def batch(zid):
        xml = f"<?xml version='1.0' encoding='UTF-8'?><zmluvy><zmluva><ID>{zid}</ID><suma_spolu>{zid}</suma_spolu></zmluva></zmluvy>"
        return httpx.Response(200, content=_zip_bytes("d.xml", xml.encode()), headers={"content-type": "application/zip"})

    routes = {f"https://www.crz.gov.sk/export/2026-03-0{d}.zip": batch(10 + d) for d in range(2, 10) if d != 5}
    stats = extend_window(cfg, db, mock_fetcher(routes), hid, days=19, chunk=4, sleep=lambda s: None)
    assert stats["od_po"] == "2026-03-02" and stats["pridané_zmluvy"] == 7 and stats["dni_chýba"] == 1
    prof = json.loads(db.get_dataset(ds)["profile_json"])
    assert prof["časové_okno"]["od"] == "2026-03-02" and prof["časové_okno"]["chýbajúce_dni"] == ["2026-03-05"]
    assert run_query(cfg, hid, "SELECT COUNT(*) FROM ds1")[0][0] == 8
    assert extend_window(cfg, db, mock_fetcher({}), hid, days=19)["pridané_zmluvy"] == 0  # už pokryté
    child = db.create_hypothesis("Podhypotéza", "", "x")
    db.add_dataset(child, None, "http://www.crz.gov.sk/export/", "ds1", status="ok", rows=1)
    assert db.sync_dataset_copies(ds) == 1 and db.datasets_for(child)[0]["rows"] == 8  # kópia v podhypotéze


def test_crz_window_does_not_move_over_network_outage_and_retries_missing_days(tmp_path):
    """13. 10. 2026: výpadok DNS počas doplňovania – okno sa nesmie posunúť cez dni bez dát; neskôr --retry."""
    from radar.crz import extend_window
    from radar.datasets import hyp_dir, import_table

    cfg, db, rec, router = setup(tmp_path, crz_delay_s=0)
    hid = gate1_hyp(db)
    hyp_dir(cfg, hid).mkdir(parents=True)
    path = tmp_path / "crz.csv"
    path.write_text("ID;suma_spolu\n1;100\n", encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    ds = db.add_dataset(hid, None, "http://www.crz.gov.sk/export/", "ds1", status="ok",
                        profile_json={"časové_okno": {"od": "2026-03-10", "do": "2026-03-20", "chýbajúce_dni": ["2026-03-15"]}})

    def batch(zid):
        xml = f"<?xml version='1.0' encoding='UTF-8'?><zmluvy><zmluva><ID>{zid}</ID><suma_spolu>{zid}</suma_spolu></zmluva></zmluvy>"
        return httpx.Response(200, content=_zip_bytes("d.xml", xml.encode()), headers={"content-type": "application/zip"})

    down = {f"https://www.crz.gov.sk/export/2026-03-0{d}.zip": httpx.Response(503) for d in range(2, 10)}
    stats = extend_window(cfg, db, mock_fetcher(down), hid, days=19, chunk=8, sleep=lambda s: None)
    assert "zastavené" in stats and json.loads(db.get_dataset(ds)["profile_json"])["časové_okno"]["od"] == "2026-03-10"
    ok = {f"https://www.crz.gov.sk/export/2026-03-{d:02d}.zip": batch(100 + d) for d in range(2, 16)}
    stats = extend_window(cfg, db, mock_fetcher(ok), hid, days=19, chunk=8, sleep=lambda s: None, retry=True)
    win = json.loads(db.get_dataset(ds)["profile_json"])["časové_okno"]
    assert stats["opakované_ok"] == 1 and win["chýbajúce_dni"] == [] and win["od"] == "2026-03-02"

def test_gate_stage_cli_accepts_hypotheses():
    """Bez zadania spracuje krok najviac gate1.max_hypotheses_per_run hypotéz (CRZ pre H11 sa preto nespustilo)."""
    from radar.cli import build_parser

    args = build_parser().parse_args(["gate1-datasets", "--hypotheses", "11,4", "--minutes", "90"])
    assert args.hypotheses == [11, 4] and args.minutes == 90
    assert build_parser().parse_args(["gate2-backtest", "--hypotheses", "1"]).hypotheses == [1]


def test_crz_batch_with_control_character_is_not_dropped():
    """Reálna dávka CRZ 2026-01-20: znak \x02 v texte (XML ho nepovoľuje) zahodil celý deň."""
    from radar.crz import contracts

    xml = ("<?xml version='1.0'?><zmluvy><zmluva><ID>7</ID><predmet>Oprava" + chr(2) + " cesty</predmet>"
           "<suma_zmluva>10</suma_zmluva></zmluva></zmluvy>").encode()
    [row] = contracts(xml)
    assert row["predmet"] == "Oprava cesty" and row["suma_zmluva"] == 10.0


def test_crz_append_missing_days_adds_only_new_contracts(tmp_path):
    from radar.crz import append_days
    from radar.datasets import import_table

    cfg, db, rec, router, hid, fid, org, _ = _dataset_env(tmp_path, crz_delay_s=0)
    seed = tmp_path / "seed.jsonl"
    seed.write_text('{"ID": "1", "objednavatel": "Mesto Fiktívne", "ico_objednavatel": "00313271", "suma_zmluva": 100.0}\n',
                    encoding="utf-8")
    (tmp_path / "data" / "datasets" / f"H{hid}").mkdir(parents=True, exist_ok=True)
    import_table(cfg, hid, "ds9", seed, "json")
    xml = ("<?xml version='1.0'?><zmluvy><zmluva><ID>1</ID><suma_zmluva>999</suma_zmluva></zmluva>"
           "<zmluva><ID>2</ID><zs1>Mesto Fiktívne</zs1><ico1>00313271</ico1><suma_zmluva>50</suma_zmluva></zmluva></zmluvy>")
    routes = {"https://www.crz.gov.sk/export/2026-01-20.zip": httpx.Response(200, content=_zip_bytes("d.xml", xml.encode()))}
    res = append_days(cfg, mock_fetcher(routes), hid, "ds9", ["2026-01-20", "2026-02-27"], [])
    assert res == {"days_ok": ["2026-01-20"], "days_failed": ["2026-02-27"], "days_error": [], "added": 1}  # 404 nie je chyba siete
    assert run_query(cfg, hid, "SELECT COUNT(*), SUM(suma_zmluva) FROM ds9")[0] == (2, 150.0)  # ID 1 ostalo pôvodné


def test_xlsx_without_extension_imports_and_bad_file_is_dataset_error(tmp_path):
    """13. 10. 2026: XLSX z katalógu uložené s príponou .bin – openpyxl ho odmietol a spadol celý krok datasetov."""
    import io

    from openpyxl import Workbook

    from radar.datasets import hyp_dir, import_table

    cfg, *_ = setup(tmp_path)
    hyp_dir(cfg, 5).mkdir(parents=True)
    wb = Workbook()
    wb.active.append(["projekt", "naklady"])
    wb.active.append(["Cesta", 10])
    buf = io.BytesIO()
    wb.save(buf)
    path = tmp_path / "download-abc.bin"
    path.write_bytes(buf.getvalue())
    import_table(cfg, 5, "ds1", path, "xlsx")
    assert run_query(cfg, 5, "SELECT naklady FROM ds1")[0][0] == 10
    bad = tmp_path / "zle.bin"
    bad.write_bytes(b"PK\x03\x04 nie je excel")
    with pytest.raises(DatasetError):
        import_table(cfg, 5, "ds2", bad, "xlsx")


def test_catalog_candidates_ranked_by_meaning_of_field():
    """„Rozpočet projektu“ → kandidát o rozpočtoch pred „Zoznamom projektov rozvoja IT“ (embeddingy, bez siete)."""
    from radar.catalogs import CatalogDataset

    class Emb:
        def embed_texts(self, texts):
            return np.array([[1.0, 0.0] if "rozpo" in t.lower() else [0.0, 1.0] for t in texts])

    cfg = Config()
    found = [CatalogDataset(uri=f"u{i}", title=t, description="", publisher="", modified="", periodicity="",
                            landing_page="", distributions=[]) for i, t in
             enumerate(["Zoznam projektov rozvoja IT", "Číselník krajov", "Rozpočty projektov obcí"])]
    me = SimpleNamespace(embedder=Emb(), cfg=cfg, hyp={"title": "Obce nevidia rozpočty"})
    ranked = gate1.SourceAgent.rank_candidates(me, {"name": "Rozpočet projektu", "description": ""}, found)
    assert [d.title for d in ranked][0] == "Rozpočty projektov obcí" and len(ranked) == cfg.gate1.nkod_hits_per_field
    assert gate1.SourceAgent.rank_candidates(SimpleNamespace(embedder=None, cfg=cfg, hyp={}), {}, found) == found[:2]


# ======================================================================= Brána 1: rozdelenie času (úloha #75)
def test_gate1_plan_only_unstarted_and_in_progress_and_fits_the_limit(tmp_path):
    """58 hypotéz za 120 min sa nedá – beh vezme s modelmi toľko, koľko stihne; vyhodnotené hypotézy sa preskočia
    (pokyn 14. 10. 2026: automaticky iba nezačaté a rozpracované)."""
    cfg, db, rec, router = setup(tmp_path)
    waiting = db.create_hypothesis("Čaká na tvoje dáta", "", "x")
    searched = db.create_hypothesis("Rozpracovaná, polia prehľadané", "", "x")
    new_a = db.create_hypothesis("Nová A", "", "x")
    new_b = db.create_hypothesis("Nová B", "", "x")
    passed = db.create_hypothesis("Prešla", "", "x")
    started = db.create_hypothesis("Rozpracovaná (polia bez hľadania)", "", "x")
    for hid, status in ((waiting, "open"), (searched, "missing"), (passed, "open"), (started, "open")):
        db.update_field(db.upsert_field(hid, "Suma", "", True, ""), status=status)
        db.start_gate1(hid)
    db.set_gate_status(waiting, 1, "needs_input")
    db.set_gate_status(passed, 1, "passed")
    found = db.upsert_field(searched, "Počet", "", False, "")
    db.add_source(found, searched, "Portál", "https://data.sk/portal", "agent")  # nájdený, ešte neskontrolovaný
    ids = [waiting, searched, new_a, new_b, passed, started]
    plan = gate1.plan_run(cfg, db, ids, 120)
    # rozpočet 120 − 15 (kontrola a sťahovanie) − 6 = 99 min: nové po 19 min, rozpracované po 15 min
    assert plan["s_modelom"] == [started, new_a, new_b] and plan["odhad_min"] == 53
    assert plan["vyhodnotené"] == [waiting, passed] and plan["všetky"] == [searched, new_a, new_b, started]
    assert plan["bez_práce_s_modelom"] == [searched] and plan["na_kontrolu"] == [searched]
    plan = gate1.plan_run(cfg, db, ids, 45)
    assert plan["s_modelom"] == [started] and plan["nabudúce"] == [new_a, new_b]
    assert gate1.plan_run(cfg, db, ids, 5)["s_modelom"] == [started]  # aspoň jedna


def test_gate1_check_and_downloads_keep_their_time_when_agent_uses_all(tmp_path, monkeypatch):
    from radar.workflows import run_workflow

    clock = [1000.0]
    monkeypatch.setattr("radar.util.awake_clock", lambda: clock[0])
    monkeypatch.setattr("radar.workflows.awake_clock", lambda: clock[0])
    cfg, db, rec, router = setup(tmp_path)
    hids = [db.create_hypothesis(f"H{i}", "", "x") for i in range(10)]
    seen = {}

    def run_stage(app, stage, dl):
        seen[stage] = (dl.remaining_minutes(), list(app.gate_ids))
        if stage == "gate1-sources":
            clock[0] = dl.end + 120  # agent minie celý úsek a ešte 2 min navyše
        return {}

    result = run_workflow(SimpleNamespace(cfg=cfg, db=db, router=router), "gate1", 120, hypothesis_ids=hids,
                          run_stage=run_stage)
    assert not [s for s in result["stages"] if s.get("preskočené")]
    assert seen["gate1-check"][0] >= 3 and seen["gate1-datasets"][0] >= 10
    batch = seen["gate1-sources"][1]
    assert 0 < len(batch) < len(hids) and seen["gate1-fields"][1] == batch  # s modelmi iba toľko, koľko sa zmestí
    assert seen["gate1-check"][1][: len(batch)] == batch and sorted(seen["gate1-check"][1]) == sorted(hids)


def test_search_skips_blocking_engines_for_a_while_and_counts_empty_searches(monkeypatch):
    """Úloha #75: vyhľadávač s CAPTCHA alebo limitom sa vynechá, dopyt ide iba funkčným; prázdne hľadania sa počítajú."""
    from radar.agent import ResearchAgent
    from radar.config import SearchConfig
    from radar.search import SearxClient

    monkeypatch.setattr("radar.search.time.sleep", lambda s: None)
    sent = []
    config = {"engines": [{"name": n, "enabled": True, "categories": ["general", "web"]}
                          for n in ("bing", "duckduckgo", "google cse", "yahoo")]}

    def handler(request):
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json=config)
        sent.append(request.url.params.get("engines"))
        if request.url.params["q"] == "prvý":
            return httpx.Response(200, json={"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]})
        return httpx.Response(200, json={"results": [{"url": "https://x.sk/a", "title": "A", "content": ""}]})

    client = SearxClient(SearchConfig(delay_s=0, retry_on_empty=False), "test")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.search("prvý") == [] and client.last_blocked == ["duckduckgo: CAPTCHA"]
    assert len(client.search("druhý")) == 1
    assert sent == [None, "bing,google cse,yahoo"]  # DuckDuckGo vynechaný
    assert client.stats == {"hľadania": 2, "prázdne": 1, "prázdne_pri_blokovaní": 1, "s_vynechanými_vyhľadávačmi": 1}
    agent = ResearchAgent.__new__(ResearchAgent)
    agent.searx, agent.searches, agent.repeats, agent.pages, agent.failed = client, {}, 0, {}, set()
    client.suspended.clear()
    assert "temporarily blocking" in agent.web_search("prvý")


def test_local_catalog_index_syncs_incrementally_and_finds_by_meaning(tmp_path):
    """Hľadanie podľa kmeňov slov v SPARQL je zašumené – lokálny index nájde dataset podľa významu poľa."""
    from radar.catalogs import LocalCatalog, NKODClient, sync_catalog

    cfg, db, rec, router = setup(tmp_path)
    titles = ["Rozpočty obcí 2025", "Adresy v obci Fiktívna", "Zmluvy ministerstiev"]
    pages = []

    def handler(request):
        q = request.url.params["query"]
        pages.append(q)
        rows = [{"d": {"value": f"https://data.slovensko.sk/datasety/{i}"}, "title": {"value": t},
                 "publisher": {"value": "Úrad"}} for i, t in enumerate(titles)] if "OFFSET 0" in q else []
        return httpx.Response(200, json={"head": {}, "results": {"bindings": rows}})

    class Emb:
        model = "test-embed"
        calls = 0

        def embed_texts(self, texts):
            Emb.calls += len(texts)
            return np.array([[1.0, 0.0] if "rozpo" in t.lower() else [0.0, 1.0] for t in texts], dtype=np.float32)

        def vectors(self, kind, items):
            from radar.embed import Embedder
            return Embedder.vectors(self, kind, items)

    emb = Emb()
    emb.db = db
    client = NKODClient(http=httpx.Client(transport=httpx.MockTransport(handler)))
    stats = sync_catalog(db, client, emb, Deadline(1))
    assert stats == {"stiahnuté": 3, "nové_vektory": 3} and db.catalog_stats()["datasety"] == 3
    assert sync_catalog(db, client, emb, Deadline(1))["nové_vektory"] == 0  # iba nové a zmenené texty
    local = LocalCatalog.load(db, emb)
    hits = local.search("Rozpočet projektu – financie obcí", 1)
    assert [h.title for h in hits] == ["Rozpočty obcí 2025"] and hits[0].uri.endswith("/0")

