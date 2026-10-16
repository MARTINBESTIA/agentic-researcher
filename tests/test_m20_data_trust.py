"""M20 – dôvera v dáta Brány 1 (audit 14. 10. 2026): súbor overený požiadavkou, import podľa obsahu s kontrolou
kvality, väzba pole → dataset → stĺpec overená kódom a potvrdená človekom, pravidlo prechodu a preverenie prešlých.
Offline: falošný model a web, lokálne súbory."""

import json

import httpx
import pytest

from radar import gate1
from radar.bindings import concept_problems, confirm_binding, key_conditions, manual_binding, reject_binding, run_bindings
from radar.cards import split_hypothesis
from radar.datasets import raw_cache_dir, run_query
from radar.util import Deadline
from tests.fakes import FakeFetcher, mock_fetcher
from tests.test_m10_gates import CSV, gate1_hyp, setup

URL = "https://data.obce.sk/files/vydavky.csv"


def _env(tmp_path, files: dict[str, bytes], fields=(("Výdavky obce na obstarávanie", "€ za rok a obec", True),), **gate):
    """Hypotéza s poľami a overenými zdrojmi (jeden na súbor) – súbory sa stiahnu cez falošný HTTP."""
    cfg, db, rec, router = setup(tmp_path, **gate)
    hid = gate1_hyp(db)
    fids = [db.upsert_field(hid, name, desc, key, "") for name, desc, key in fields]
    for url in files:
        sid = db.add_source(fids[0], hid, "Zdroj", url, "agent", access="open", formats="CSV")
        db.update_source(sid, status="verified", checks_json={"http_ok": True, "súbory": [url], "http_stav": "ok",
                                                              "overenie_súborov": {url: "dáta dostupné"}})
    fetcher = mock_fetcher({u: httpx.Response(200, content=c, headers={"content-type": "application/octet-stream"})
                            for u, c in files.items()})
    stats = gate1.run_datasets(cfg, db, fetcher, Deadline(1), [hid])
    return cfg, db, rec, router, hid, fids, stats


# ======================================================================= P0 1: dostupnosť overená požiadavkou
def test_broken_link_or_login_page_is_not_verified_and_field_stays_partial(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    fid = db.upsert_field(hid, "Výdavky obce", "", True, "")
    broken = db.add_source(fid, hid, "Starý odkaz", "https://data.obce.sk/files/stare.csv", "agent", access="open")
    login = db.add_source(fid, hid, "Prihlásenie", "https://data.obce.sk/files/login.csv", "agent", access="open")
    fetcher = FakeFetcher(files={"https://data.obce.sk/files/login.csv": "<!DOCTYPE html><html><body>Prihlásenie</body>".encode()})
    assert gate1.run_check(cfg, db, fetcher, Deadline(1), [hid])["checked"] == 2
    for sid in (broken, login):
        src = db.get_source(sid)
        checks = json.loads(src["checks_json"])
        assert checks["http_ok"] is False and checks["súbory"] == [] and src["status"] == "candidate"
    assert "HTTP 404" in json.loads(db.get_source(broken)["checks_json"])["http_stav"]
    assert "HTML" in json.loads(db.get_source(login)["checks_json"])["http_stav"]
    assert db.get_field(fid)["status"] == "partial"  # iba odkaz na zdroj, nie „nájdené“


def test_fetcher_probe_follows_redirects_blocks_social_and_detects_html():
    fetcher = mock_fetcher({
        "https://data.sk/a.csv": httpx.Response(302, headers={"location": "https://data.sk/b.csv"}),
        "https://data.sk/b.csv": httpx.Response(200, content=b"a;b\n1;2\n", headers={"content-type": "text/csv"}),
        "https://data.sk/c.csv": httpx.Response(200, content=b"<html><head><title>Chyba</title>",
                                                headers={"content-type": "text/html"}),
        "https://data.sk/d.csv": httpx.Response(302, headers={"location": "https://www.linkedin.com/x.csv"}),
    })
    p = fetcher.probe("https://data.sk/a.csv")
    assert (p.status, p.final_url, p.head) == ("ok", "https://data.sk/b.csv", b"a;b\n1;2\n")
    assert fetcher.probe("https://data.sk/c.csv").status == "html"
    assert (fetcher.probe("https://data.sk/zmazane.csv").status, fetcher.probe("https://data.sk/zmazane.csv").http_status) == (
        "error", 404)
    assert fetcher.probe("https://data.sk/d.csv").status == "blocked_domain"  # ani po presmerovaní


# ======================================================================= P0 2: import podľa obsahu
SSML = """<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Styles><Style ss:ID="s1"/></Styles>
 <Worksheet ss:Name="Výdavky"><Table>
  <Row><Cell ss:MergeAcross="2" ss:StyleID="s1"><Data ss:Type="String">Výdavky obcí 2021 (v EUR)</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">obec</Data></Cell><Cell><Data ss:Type="String">rok</Data></Cell>
       <Cell><Data ss:Type="String">výdavky EUR</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">Fiktívne</Data></Cell><Cell><Data ss:Type="Number">2021</Data></Cell>
       <Cell ss:Formula="=1+1"><Data ss:Type="Number">400000</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">Vzorové</Data></Cell><Cell><Data ss:Type="Number">2021</Data></Cell>
       <Cell><Data ss:Type="Number">500000</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">Ukážkové</Data></Cell><Cell ss:Index="2"><Data ss:Type="Number">2021</Data></Cell>
       <Cell><Data ss:Type="Number">600000</Data></Cell></Row>
  <Row><Cell><Data ss:Type="String">Zdroj: obce</Data></Cell></Row>
 </Table></Worksheet></Workbook>"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:qb="http://purl.org/linked-data/cube#"
  xmlns:skos="http://www.w3.org/2004/02/skos/core#" xmlns:ex="https://data.example.sk/def#">
 <skos:Concept rdf:about="https://data.example.sk/kraj/BA"><skos:prefLabel>Bratislavský kraj</skos:prefLabel></skos:Concept>
 <skos:Concept rdf:about="https://data.example.sk/kraj/KE"><skos:prefLabel>Košický kraj</skos:prefLabel></skos:Concept>
 <qb:Observation rdf:about="https://data.example.sk/obs/1"><ex:kraj rdf:resource="https://data.example.sk/kraj/BA"/>
   <ex:rok>2021</ex:rok><ex:hodnota>1200.5</ex:hodnota><ex:unitMeasure rdf:resource="https://data.example.sk/unit/EUR"/>
 </qb:Observation>
 <qb:Observation rdf:about="https://data.example.sk/obs/2"><ex:kraj rdf:resource="https://data.example.sk/kraj/KE"/>
   <ex:rok>2021</ex:rok><ex:hodnota>800</ex:hodnota><ex:unitMeasure rdf:resource="https://data.example.sk/unit/EUR"/>
 </qb:Observation>
 <rdf:Description rdf:about="https://data.example.sk/obs/3">
   <rdf:type rdf:resource="http://purl.org/linked-data/cube#Observation"/><ex:kraj rdf:nodeID="k3"/>
   <ex:rok>2022</ex:rok><ex:hodnota>900</ex:hodnota><ex:unitMeasure rdf:resource="https://data.example.sk/unit/EUR"/>
 </rdf:Description>
 <rdf:Description rdf:nodeID="k3"><skos:prefLabel>Žilinský kraj</skos:prefLabel></rdf:Description>
</rdf:RDF>"""


def test_html_saved_as_csv_is_rejected_not_imported_as_table(tmp_path):
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {URL: "<!DOCTYPE html>\n<html><head><title>Prihlásenie</title>".encode()})
    ds = db.datasets_for(hid)[0]
    assert stats == {"dataset_errors": 1} and ds["status"] == "error" and "HTML" in ds["note"]
    assert db.get_field(fids[0])["status"] != "loaded"


def test_spreadsheetml_rows_become_records_without_formatting_columns(tmp_path):
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {"https://data.obce.sk/files/vydavky.xml": SSML.encode()})
    ds = db.datasets_for(hid)[0]
    rep = json.loads(ds["profile_json"])["import"]
    assert ds["status"] == "ok" and ds["rows"] == 3 and json.loads(ds["columns_json"]) == ["obec", "rok", "výdavky EUR"]
    assert rep["adaptér"] == "spreadsheetml" and rep["jednotky"] == {"výdavky EUR": "EUR"} and rep["verzia_parsera"]
    assert rep["tabuľka"]["vynechané_riadky_nad_tabuľkou"] == 1  # nadpis nad tabuľkou
    assert run_query(cfg, hid, f'SELECT SUM("výdavky EUR") FROM {ds["table_name"]}')[0][0] == 1_500_000
    assert db.get_field(fids[0])["status"] == "loaded"  # dáta načítané, stĺpec ešte neoverený


def test_rdf_cube_observations_keep_dimensions_and_unit(tmp_path):
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {"https://data.example.sk/kocka.xml": RDF.encode()})
    ds = db.datasets_for(hid)[0]
    rep = json.loads(ds["profile_json"])["import"]
    assert ds["status"] == "ok" and ds["rows"] == 3
    assert rep["kocka"]["dimenzie"] == ["kraj", "rok"] and rep["kocka"]["hodnoty"] == ["hodnota"]
    assert rep["jednotky"] == {"unitMeasure": "EUR"}  # „hodnota“ nie je jednotka „hod“
    rows = run_query(cfg, hid, f"SELECT kraj, rok, hodnota FROM {ds['table_name']} ORDER BY kraj")
    assert [r[0] for r in rows] == ["Bratislavský kraj", "Košický kraj", "Žilinský kraj"]  # aj cez rdf:nodeID


def test_rdf_with_disconnected_dimensions_or_without_observations_is_not_usable(tmp_path):
    loose = RDF.replace('rdf:resource="https://data.example.sk/kraj/BA"', 'rdf:nodeID="x1"').replace(
        'rdf:resource="https://data.example.sk/kraj/KE"', 'rdf:nodeID="x2"').replace("<ex:rok>2021</ex:rok>", "")
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {"https://data.example.sk/kocka.xml": loose.encode()})
    ds = db.datasets_for(hid)[0]
    assert ds["status"] == "error" and "odpojené dimenzie" in ds["note"]
    concepts = RDF.split("<qb:Observation")[0] + "</rdf:RDF>"
    cfg, db, *_, hid, fids, stats = _env(tmp_path / "b", {"https://data.example.sk/ciselnik.xml": concepts.encode()})
    assert "bez pozorovaní" in db.datasets_for(hid)[0]["note"]


def test_unknown_xml_and_rejected_csv_rows_go_to_review_and_do_not_count(tmp_path):
    styles = b'<?xml version="1.0"?><root><row><StyleID>s1</StyleID><Index>1</Index></row>' \
             b'<row><StyleID>s2</StyleID><Index>2</Index></row></root>'
    broken = (CSV + "00313271;2022;1;2;3\n" * 4).encode()
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {"https://data.sk/styly.xml": styles, URL: broken})
    by_url = {d["url"]: d for d in db.datasets_for(hid)}
    xml_rep = json.loads(by_url["https://data.sk/styly.xml"]["profile_json"])["import"]
    csv_rep = json.loads(by_url[URL]["profile_json"])["import"]
    assert by_url["https://data.sk/styly.xml"]["status"] == "needs_review" and any(
        "formátovania" in p for p in xml_rep["problémy"])
    assert by_url[URL]["status"] == "needs_review" and csv_rep["odmietnuté_riadky"] == 4 and csv_rep["riadky"] == 3
    assert stats["needs_review"] == 2 and gate1.readiness(cfg, db, hid)["datasety"] == 0  # nerátajú sa
    assert db.get_field(fids[0])["status"] != "loaded"
    summary = gate1.readiness(cfg, db, hid)
    assert any("na kontrole importu" in f for f in summary["príznaky"])
    # tvoja kontrola: schválený import sa ráta, zamietnutý sa zmaže z DuckDB; obe sú referenčné hodnotenia
    gate1.review_import(cfg, db, by_url[URL]["id"], True, "4 riadky sú súčty – v poriadku")
    gate1.review_import(cfg, db, by_url["https://data.sk/styly.xml"]["id"], False, "formátovanie")
    assert db.get_dataset(by_url[URL]["id"])["status"] == "ok" and db.get_field(fids[0])["status"] == "loaded"
    assert db.get_dataset(by_url["https://data.sk/styly.xml"]["id"])["status"] == "rejected"
    labels = {r["label"] for r in db.conn.execute("SELECT label FROM labels WHERE task = 'dataset_import'")}
    assert labels == {"approved", "rejected"}
    with pytest.raises(ValueError):
        gate1.review_import(cfg, db, by_url[URL]["id"], True)  # už nie je na kontrole


def test_xlsx_title_rows_and_two_row_header_are_detected(tmp_path):
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Výkaz výdavkov obcí"])
    ws.append(["Stav k 31. 12. 2021"])
    ws.append(["obec", "výdavky", None])
    ws.append([None, "2020", "2021"])
    ws.append(["Fiktívne", 100, 120])
    ws.append(["Vzorové", 200, 210])
    buf = BytesIO()
    wb.save(buf)
    cfg, db, *_, hid, fids, stats = _env(tmp_path, {"https://data.obce.sk/files/vykaz.xlsx": buf.getvalue()})
    ds = db.datasets_for(hid)[0]
    rep = json.loads(ds["profile_json"])["import"]
    assert json.loads(ds["columns_json"]) == ["obec", "výdavky 2020", "výdavky 2021"] and ds["rows"] == 2
    assert rep["tabuľka"]["viacriadková_hlavička"] is True and rep["tabuľka"]["vynechané_riadky_nad_tabuľkou"] == 2


def test_duplicate_distribution_counts_once_and_raw_file_is_kept_by_hash(tmp_path):
    files = {URL: CSV.encode(), "https://data.obce.sk/files/vydavky_en.csv": CSV.replace(";", ",").encode()}
    cfg, db, *_, hid, fids, stats = _env(tmp_path, files)
    assert stats["datasets"] == 2 and stats["duplicate_distributions"] == 1
    assert len(gate1.readiness(cfg, db, hid)["príznaky"]) >= 1 and gate1.readiness(cfg, db, hid)["datasety"] == 1
    raw = list(raw_cache_dir(cfg).iterdir())
    assert len(raw) == 2  # pôvodné súbory podľa SHA-256 (opakovanie parsera po oprave bez sťahovania)
    rep = json.loads(db.datasets_for(hid)[0]["profile_json"])["import"]
    assert rep["pôvodný_súbor"] and rep["verzia_parsera"]


# ======================================================================= P0 1: väzba pole → stĺpec
DRUGS = ("kod_sukl;nazov_lieku;atc;exspiracia\n1001;Liek A 10 mg;C09AA;31.12.2027\n1002;Liek B 5 mg;C10AA;30.06.2026\n"
         "1003;Liek C 20 mg;A10BA;31.01.2028\n")


def test_drug_register_does_not_cover_patent_or_price(tmp_path):
    """H18 z auditu: zoznam liekov s dátumom exspirácie nemá stĺpec patentu ani ceny – pole nesmie byť „nájdené“."""
    fields = (("Dátum expirácie patentu", "kedy končí patentová ochrana lieku", True),
              ("Schválená cena lieku", "maximálna cena v EUR", True))
    cfg, db, rec, router, hid, fids, _ = _env(tmp_path, {"https://www.sukl.sk/lieky.csv": DRUGS.encode()}, fields,
                                               auto_gate2_readiness=0.0)
    table = db.datasets_for(hid)[0]["table_name"]
    proposals = iter([
        {"in_data": True, "table": table, "expression": '"exspiracia"', "unit": "dátum", "meaning": "koniec patentu",
         "evidence": "exspiracia"},
        {"in_data": True, "table": table, "expression": '"nazov_lieku"', "unit": "EUR", "meaning": "cena"},
    ])
    rec.responses["FieldBindingOut"] = lambda m: next(proposals)
    stats = run_bindings(cfg, db, router, Deadline(1), [hid])
    assert stats == {"code_failed": 2} and rec.models_for("FieldBindingOut") == ["radar-qwen3.6-27b"] * 2
    patent, price = (db.bindings_for_field(f)[0] for f in fids)
    assert any("patent" in p for p in json.loads(patent["checks_json"])["problémy"])  # exspirácia ≠ patent
    price_problems = json.loads(price["checks_json"])["problémy"]
    assert any("cena" in p for p in price_problems) and any("nie sú číslo" in p for p in price_problems)
    assert [db.get_field(f)["status"] for f in fids] == ["loaded", "open"]  # nie „nájdené“ (cena nemá ani zdroj)
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    summary = json.loads(h["gate1_json"])
    assert h["gate1_status"] != "passed" and len(summary["podmienky_prechodu"]) == 2  # skóre nenahradí kľúčové pole
    with pytest.raises(ValueError):
        confirm_binding(db, patent["id"])  # potvrdiť sa dá iba väzba, ktorú overil kód
    with pytest.raises(ValueError):
        manual_binding(cfg, db, hid, fids[0], table, '"exspiracia"', "dátum", "patent")  # ani ručne


def test_concept_guard_needs_concept_in_column_not_in_model_text():
    assert concept_problems("Dátum expirácie patentu", '"exspiracia"', "koniec patentu")
    assert not concept_problems("Dátum expirácie patentu", '"datum_expiracie_patentu"')
    assert concept_problems("Čas odpovede úradu", '"datum_podania"')
    assert not concept_problems("Výdavky obce", '"suma"')


def test_binding_confirmed_by_human_passes_gate_and_rejection_revokes_it(tmp_path):
    cfg, db, rec, router, hid, fids, _ = _env(tmp_path, {URL: CSV.encode()})
    table = db.datasets_for(hid)[0]["table_name"]
    rec.responses["FieldBindingOut"] = {"in_data": True, "table": table, "expression": '"suma"', "unit": "EUR",
                                        "entity_kind": "obec", "grain": "obec × rok", "meaning": "výdavky obce za rok",
                                        "evidence": "stĺpec suma"}
    assert run_bindings(cfg, db, router, Deadline(1), [hid]) == {"code_ok": 1}
    assert run_bindings(cfg, db, router, Deadline(1), [hid]) == {}  # pre rovnaké dáta sa návrh neopakuje
    b = db.bindings_for_field(fids[0])[0]
    checks = json.loads(b["checks_json"])
    assert checks["pokrytie"] == 1.0 and checks["typ"]["zhoda"] == 1.0 and checks["vzorka"]
    assert db.get_field(fids[0])["status"] == "found"
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    assert h["gate1_status"] == "needs_input" and "potvrď stĺpec" in json.loads(h["gate1_json"])["podmienky_prechodu"][0]
    confirm_binding(db, b["id"])
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    last = db.gate_decisions_for(hid, 1)[0]
    assert h["gate1_status"] == "passed" and last["decided_by"] == "rule" and last["data_fingerprint"]
    # väzbu zamietneš → prechod pravidlom už neplatí: „treba znova overiť“ (nie tichý zostatok „prešla“)
    reject_binding(db, b["id"], "suma je s DPH, treba bez DPH")
    stats = gate1.run_finish(cfg, db, Deadline(1), [hid])
    h = db.get_hypothesis(hid)
    assert stats["revalidation_needed"] == 1 and h["gate1_status"] == "needs_revalidation"
    assert db.gate_decisions_for(hid, 1)[0]["decision"] == "needs_revalidation"
    assert not [x for x in db.gate2_hypotheses() if x["id"] == hid]
    # nová potvrdená väzba (ručne) → pravidlo znova prejde
    manual_binding(cfg, db, hid, fids[0], table, '"suma"', "EUR", "výdavky bez DPH")
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert db.get_hypothesis(hid)["gate1_status"] == "passed" and key_conditions(db, hid) == []


def test_manual_pass_is_kept_with_author_but_flagged(tmp_path):
    cfg, db, rec, router, hid, fids, _ = _env(tmp_path, {URL: CSV.encode()})
    gate1.decide(db, hid, 1, "passed", "Dáta mám od partnera", by="human")
    stats = gate1.revalidate_passed(cfg, db, [hid])
    h = db.get_hypothesis(hid)
    assert stats == {"manual_pass_flagged": 1} and h["gate1_status"] == "passed"
    assert db.gate_decisions_for(hid, 1)[0]["decided_by"] == "human"
    assert any("ručné rozhodnutie" in f for f in json.loads(h["gate1_json"])["príznaky"])
    # staré rozhodnutie pravidlom (pred 15. 10. 2026, bez autora) sa rozpozná podľa dôvodu
    other = gate1_hyp(db)
    db.upsert_field(other, "Výdavky", "", True, "")
    db.add_gate_decision(1, other, "passed", "Automaticky: pripravenosť dát 0,62 ≥ 0,50", {}, decided_by=None)
    db.set_gate_status(other, 1, "passed")
    assert gate1.revalidate_passed(cfg, db, [other]) == {"revalidation_needed": 1}


def test_split_child_inherits_data_and_bindings_not_the_pass(tmp_path):
    cfg, db, rec, router, hid, fids, _ = _env(tmp_path, {URL: CSV.encode()})
    table = db.datasets_for(hid)[0]["table_name"]
    manual_binding(cfg, db, hid, fids[0], table, '"suma"', "EUR", "výdavky obce")
    gate1.run_finish(cfg, db, Deadline(1), [hid])
    assert db.get_hypothesis(hid)["gate1_status"] == "passed"
    card = {"subproblems": [{"title": "Malé obce", "problem": "p", "segment": "obce do 1000", "claims": []}]}
    (child,) = split_hypothesis(cfg, db, db.get_hypothesis(hid), card)
    c = db.get_hypothesis(child)
    assert c["gate1_status"] == "needs_revalidation" and db.gate_decisions_for(child, 1)[0]["decided_by"] == "rule"
    cb = db.bindings_for(child)
    assert len(cb) == 1 and cb[0]["validation_status"] == "code_ok" and "zdedené" in cb[0]["note"]
    assert key_conditions(db, child) and "potvrď stĺpec" in key_conditions(db, child)[0]
    from radar.funnel import step_state

    state, why = step_state(cfg, db, c, "gate1")  # lievik čaká na tvoje potvrdenie, neopakuje prieskum
    assert state == "stop" and "potvrdenie stĺpcov" in why


def test_internal_field_goes_to_partner_not_web_search(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = gate1_hyp(db)
    rec.responses["DataFields"] = {"fields": [
        {"name": "Čas odpovede na požiadavku zákazníka", "description": "interné tikety", "is_key": True,
         "availability": "internal"},
        {"name": "Výdavky obce", "description": "€", "is_key": False}]}
    gate1.run_fields(cfg, db, router, Deadline(1), None)
    internal, public = db.fields_for(hid)
    assert internal["status"] == "partner" and internal["availability"] == "internal" and public["status"] == "open"
    assert "partnera" in key_conditions(db, hid)[0]
    assert not gate1.field_needs_search(internal) and gate1.field_needs_search(public)  # web sa pre interné nehľadá


def test_gate1_page_confirms_column_and_approves_import(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.datasets import hyp_dir, import_table, profile_table
    from radar.db import DB
    from tests.test_m2_gui import APP, fixture_db

    gui = tmp_path
    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(gui / "config.yaml")
    db = DB(cfg.db_path)
    fid = db.upsert_field(1, "Výdavky obce", "€ za rok", True, "")
    hyp_dir(cfg, 1).mkdir(parents=True, exist_ok=True)
    ids = {}
    for i, status in ((1, "ok"), (2, "needs_review")):
        path = gui / f"d{i}.csv"
        path.write_text(CSV, encoding="utf-8")
        import_table(cfg, 1, f"ds{i}", path, "csv")
        prof = profile_table(cfg, 1, f"ds{i}")
        prof["import"] = {"problémy": ["parser odmietol 4 riadkov (57%)"], "riadky": 3, "odmietnuté_riadky": 4} if i == 2 else {}
        ids[i] = db.add_dataset(1, None, f"https://data.obce.sk/{i}.csv", f"ds{i}", rows=3, profile_json=prof,
                                columns_json=["ico", "rok", "suma"], status=status, sha256=f"sha{i}")
    bid = db.add_binding(fid, 1, "code_ok", dataset_id=ids[1], dataset_sha="sha1", table_name="ds1", expression='"suma"',
                         unit="EUR", meaning="výdavky obce za rok", checks_json={"pokrytie": 1.0, "vzorka": ["400000"]},
                         inputs_fingerprint="x", proposed_by="model")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate1.py").run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Potvrdiť význam stĺpca").click().run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Schváliť import").click().run()
    assert not at.exception, at.exception
    db = DB(cfg.db_path)
    assert db.get_binding(bid)["validation_status"] == "confirmed" and db.get_binding(bid)["confirmed_by"] == "človek"
    assert db.get_dataset(ids[2])["status"] == "ok" and db.get_field(fid)["status"] == "found"
    db.close()


def test_gate1_action_maps_fields_for_evaluated_hypothesis_with_new_data(tmp_path, monkeypatch):
    """16. 10. 2026 (test autopilota): hypotéza „čaká na tvoje dáta“ s novými dátami na mapovanie – autopilot pre ňu
    zaradil Bránu 1, akcia ju ako vyhodnotenú hneď preskočila a autopilot to skúšal v každom kole."""
    from types import SimpleNamespace

    from radar import workflows
    from radar.config import Config
    from radar.db import DB
    from radar.util import Deadline

    cfg = Config(db_path=str(tmp_path / "r.sqlite"))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Meškanie dodávok", "", "výrobcovia")
    db.set_people_selection(hid, True)
    db.set_gate_status(hid, 1, "needs_input")
    monkeypatch.setattr("radar.gate1.binding_work", lambda d, h: h == hid)
    calls = []
    app = SimpleNamespace(cfg=cfg, db=db)
    workflows._gate1(app, Deadline(30), lambda stage, dl: calls.append((stage, list(app.gate_ids))), None,
                     lambda **k: None)
    assert ("gate1-bindings", [hid]) in calls
    assert not any(stage in ("gate1-fields", "gate1-sources", "gate1-check") for stage, _ in calls)  # bez nového hľadania
