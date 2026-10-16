"""M31 – import event logu a report kvality (dodatok 16. 10. 2026, časť 7.1–7.2, scenáre 7–12): idempotentný import
do oddeleného priestoru organizácie, návrh mapovania (nie čas exportu, nie mená), časové zóny a prechody času,
duplicity, zhodné časy, neplatné časy, párovanie start/complete, snapshot. Nič sa nemaže potichu. Offline."""

import csv
import json

import duckdb
import pytest

from radar.config import Config, EmbeddingsConfig, EventLogConfig
from radar.db import DB
from radar.event_logs import EventLogError, confirm_mapping, import_file, org_store, propose_mapping

MAP = {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp", "event_id": "event_id"}


def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), embeddings=EmbeddingsConfig(enabled=False),
                 event_logs=EventLogConfig(dir="eventlogs"))
    db = DB(cfg.db_path)
    a, _ = db.upsert_organization("Alfa s.r.o.", "alfa sro", "manual")
    b, _ = db.upsert_organization("Beta a.s.", "beta as", "manual")
    return cfg, db, a, b


def write(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


SYNTH = [  # case_id, activity, timestamp, event_id
    ["C1", "Objednávka prijatá", "2026-03-02T08:00:00+01:00", "e1"],
    ["C1", "Potvrdenie dodávateľa", "2026-03-03T09:30:00Z", "e2"],
    ["C1", "Dodané", "2026-03-10T10:00:00+01:00", "e3"],
    ["C1", "Dodané", "2026-03-10T10:00:00+01:00", "e3"],  # duplicitné event_id
    ["C2", "Objednávka prijatá", "2026-03-05T08:00:00+01:00", "e4"],
    ["C2", "Urgencia", "2026-03-06T08:00:00+01:00", "e5"],
    ["C2", "Potvrdenie dodávateľa", "2026-03-06T08:00:00+01:00", "e6"],  # zhodný čas, neznáme poradie
    ["C3", "Objednávka prijatá", "2026-03-07T08:00:00+01:00", "e7"],
    ["C3", "Urgencia", "zajtra ráno", "e8"],  # neplatný čas
    ["", "Urgencia", "2026-03-08T08:00:00+01:00", "e9"],  # chýba ID prípadu
    ["C4", "Objednávka prijatá", "2026-03-20T08:00:00+01:00", "e10"],  # otvorený (nedokončený) prípad
]


def test_import_is_idempotent_and_same_case_id_in_two_orgs_is_not_merged(tmp_path):
    cfg, db, a, b = env(tmp_path)
    f = write(tmp_path / "log.csv", ["case_id", "activity", "timestamp", "event_id"], SYNTH)
    first, new = import_file(cfg, db, a, f)
    again, new2 = import_file(cfg, db, a, f)
    assert new and not new2 and again == first and len(db.eventlog_imports(a)) == 1
    other, _ = import_file(cfg, db, b, f)
    assert other != first and org_store(cfg, a) != org_store(cfg, b)
    confirm_mapping(cfg, db, first, MAP, end_activities=["Dodané"])
    confirm_mapping(cfg, db, other, MAP, end_activities=["Dodané"])
    con = duckdb.connect(str(org_store(cfg, a)), read_only=True)
    assert con.execute(f"SELECT COUNT(*) FROM raw_{first}").fetchone()[0] == len(SYNTH)  # nezdvojené
    assert con.execute(f"SELECT COUNT(*) FROM events_{first} WHERE case_id = 'C1'").fetchone()[0] == 4
    con.close()
    raw = list((tmp_path / "data" / "eventlogs").glob("org_*_raw/*.csv"))
    assert len(raw) == 2  # pôvodný súbor v priestore každej organizácie


def test_mapping_proposal_skips_export_time_and_personal_columns():
    mapping, notes = propose_mapping(["Case ID", "Activity", "exported_at", "Event time", "resource"])
    assert mapping["case_id"] == "Case ID" and mapping["activity"] == "Activity" and mapping["timestamp"] == "Event time"
    assert any("exported_at" in n and "exportu" in n for n in notes)
    assert any("resource" in n and "mená" in n for n in notes)


def test_quality_report_lists_counts_shares_decisions_and_is_reproducible(tmp_path):
    cfg, db, a, b = env(tmp_path)
    iid, _ = import_file(cfg, db, a, write(tmp_path / "log.csv", ["case_id", "activity", "timestamp", "event_id"], SYNTH))
    rep = confirm_mapping(cfg, db, iid, MAP, end_activities=["Dodané"])
    checks = {c["kontrola"]: c for c in rep["kontroly"]}
    assert checks["duplicitné event_id"]["počet"] == 1 and "prvý ostáva" in checks["duplicitné event_id"]["rozhodnutie"]
    assert checks["chýba ID prípadu"]["počet"] == 1 and checks["chýba ID prípadu"]["príklady_riadkov"] == [10]
    assert checks["neplatný formát času"]["počet"] == 1 and "početnosti" in checks["neplatný formát času"]["rozhodnutie"]
    assert checks["zhodné časy bez určeného poradia"]["počet"] == 1  # C2
    assert checks["otvorený prípad (posledná udalosť nie je koncová)"]["počet"] >= 1
    assert rep["časová_zóna"]["politika"] == "offset v dátach" and rep["vylúčené_riadky"] == 2
    again = confirm_mapping(cfg, db, iid, MAP, end_activities=["Dodané"])
    assert again["kontroly"] == rep["kontroly"] and again["dostupnosť"] == rep["dostupnosť"]
    imp = db.get_eventlog_import(iid)
    assert imp["mapping_version"] == 2 and imp["mapping_confirmed"] == 1 and imp["status"] == "checked"
    con = duckdb.connect(str(org_store(cfg, a)), read_only=True)
    assert con.execute(f"SELECT COUNT(*) FROM events_{iid}").fetchone()[0] == len(SYNTH)  # nič nezmazané
    con.close()


def test_invalid_time_supports_counts_but_not_time_metrics_and_order_is_not_alphabetic(tmp_path):
    cfg, db, a, b = env(tmp_path)
    iid, _ = import_file(cfg, db, a, write(tmp_path / "log.csv", ["case_id", "activity", "timestamp", "event_id"], SYNTH))
    av = confirm_mapping(cfg, db, iid, MAP, end_activities=["Dodané"])["dostupnosť"]
    assert av["počty"]["stav"] == "áno"
    assert av["odstupy"]["stav"] == "čiastočne" and "bez platného času podporí iba početnosti" in av["odstupy"]["dôvod"]
    assert av["varianty_a_nadväznosti"]["stav"] == "čiastočne" and av["varianty_a_nadväznosti"]["pokrytie"] < 1
    assert av["pracovný_čas"]["stav"] == "nie" and av["čas_aktivity"]["stav"] == "nie"
    con = duckdb.connect(str(org_store(cfg, a)), read_only=True)
    ordered = dict(con.execute(f"SELECT case_id, ordered FROM cases_{iid}").fetchall())
    con.close()
    assert ordered["C2"] is False and ordered["C3"] is False and ordered["C1"] is True


def test_identical_rows_without_event_id_are_flagged_not_deleted(tmp_path):
    cfg, db, a, b = env(tmp_path)
    rows = [["K1", "Kontrola", "2026-03-02 08:00"], ["K1", "Kontrola", "2026-03-02 08:00"],
            ["K1", "Schválenie", "2026-03-02 09:00"]]
    iid, _ = import_file(cfg, db, a, write(tmp_path / "x.csv", ["case", "activity", "time"], rows))
    rep = confirm_mapping(cfg, db, iid, {"case_id": "case", "activity": "activity", "timestamp": "time"})
    ident = next(c for c in rep["kontroly"] if c["kontrola"] == "identické riadky bez event_id")
    assert ident["počet"] == 2 and "nezmazané" in ident["rozhodnutie"] and rep["použiteľné_riadky"] == 3
    assert any("zóna neznáma" in w for w in rep["upozornenia"]) and rep["časová_zóna"]["politika"] == "neznáma"


def test_daylight_saving_transitions_are_flagged_with_documented_zone(tmp_path):
    cfg, db, a, b = env(tmp_path)
    rows = [["Z1", "Štart", "2026-03-29 01:30:00"], ["Z1", "Krok", "2026-03-29 02:30:00"],  # neexistuje
            ["Z1", "Koniec", "2026-03-29 04:00:00"], ["Z2", "Štart", "2026-10-25 02:30:00"],  # nejednoznačný
            ["Z2", "Koniec", "2026-10-25 05:00:00"]]
    iid, _ = import_file(cfg, db, a, write(tmp_path / "dst.csv", ["case_id", "activity", "timestamp"], rows))
    rep = confirm_mapping(cfg, db, iid, {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp"},
                          timezone_policy="Europe/Bratislava")
    names = {c["kontrola"]: c["počet"] for c in rep["kontroly"]}
    assert names["neexistujúci čas (prechod na letný čas)"] == 1 and names["nejednoznačný čas (prechod na zimný čas)"] == 1
    assert rep["časová_zóna"]["politika"] == "zóna Europe/Bratislava"
    con = duckdb.connect(str(org_store(cfg, a)), read_only=True)
    first = con.execute(f"SELECT ts FROM events_{iid} WHERE _row = 1").fetchone()[0]
    con.close()
    assert first.isoformat() == "2026-03-29T00:30:00"  # 01:30 SEČ = 00:30 UTC
    with pytest.raises(EventLogError):
        confirm_mapping(cfg, db, iid, {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp"},
                        timezone_policy="Mars/Olympus")


def test_start_complete_without_unambiguous_pairing_gives_no_activity_time(tmp_path):
    cfg, db, a, b = env(tmp_path)
    rows = [["P1", "Kontrola", "2026-03-02T08:00:00Z", "start"], ["P1", "Kontrola", "2026-03-02T08:05:00Z", "start"],
            ["P1", "Kontrola", "2026-03-02T09:00:00Z", "complete"], ["P1", "Kontrola", "2026-03-02T09:30:00Z", "complete"],
            ["P2", "Balenie", "2026-03-02T10:00:00Z", "complete"]]
    iid, _ = import_file(cfg, db, a, write(tmp_path / "lc.csv", ["case_id", "activity", "timestamp", "lifecycle"], rows))
    rep = confirm_mapping(cfg, db, iid, {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp",
                                         "lifecycle": "lifecycle"})
    assert rep["start_complete"]["páry"] == 0 and rep["start_complete"]["nejednoznačné"] >= 1
    assert rep["start_complete"]["complete_bez_štartu"] == 1
    assert rep["dostupnosť"]["čas_aktivity"]["stav"] == "nie"


def test_snapshot_is_not_processed_as_event_history(tmp_path):
    cfg, db, a, b = env(tmp_path)
    rows = [[f"S{i}", "Otvorená" if i % 2 else "Uzavretá", "2026-03-31T23:59:00Z"] for i in range(12)]
    iid, _ = import_file(cfg, db, a, write(tmp_path / "snap.csv", ["case_id", "status", "updated_at"], rows))
    rep = confirm_mapping(cfg, db, iid, {"case_id": "case_id", "activity": "status", "timestamp": "updated_at"})
    assert rep["druh_dát"] == "snapshot" and any("snapshot" in w for w in rep["upozornenia"])
    assert all(rep["dostupnosť"][m]["stav"] == "nie" for m in ("varianty_a_nadväznosti", "odstupy", "trvanie_prípadu"))
    assert any("exportu" in w for w in rep["upozornenia"])  # rovnaký čas pri všetkých riadkoch, názov stĺpca
    assert db.get_eventlog_import(iid)["data_kind"] == "snapshot"


def test_parquet_import_and_unsupported_format(tmp_path):
    cfg, db, a, b = env(tmp_path)
    pq = tmp_path / "log.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('Q1', 'A', TIMESTAMPTZ '2026-03-02 08:00:00+00'), "
                "('Q1', 'B', TIMESTAMPTZ '2026-03-02 09:00:00+00')) v(case_id, activity, timestamp)")
    con.execute(f"COPY t TO '{pq}' (FORMAT PARQUET)")
    con.close()
    iid, _ = import_file(cfg, db, a, pq)
    rep = confirm_mapping(cfg, db, iid, {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp"})
    assert rep["použiteľné_riadky"] == 2 and rep["usporiadané_prípady"] == 1
    assert json.loads(db.get_eventlog_import(iid)["columns_json"])[0]["stĺpec"] == "case_id"
    bad = tmp_path / "log.xes"
    bad.write_text("<log/>", encoding="utf-8")
    with pytest.raises(EventLogError, match="CSV a Parquet"):
        import_file(cfg, db, a, bad)


def test_eventlog_page_shows_mapping_quality_and_availability(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    org = db.outreach_list(1)[0]["org_id"]
    iid, _ = import_file(cfg, db, org, write(tmp_path / "log.csv", ["case_id", "activity", "timestamp", "event_id"],
                                             SYNTH), hid=1)
    confirm_mapping(cfg, db, iid, MAP, end_activities=["Dodané"])
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/eventlog.py").run()
    assert not at.exception, at.exception
    assert any("Report kvality" in m.value for m in at.markdown)
    assert any("rozhodnutie" in df.value.columns for df in at.dataframe)
    av = next(df.value for df in at.dataframe if "dostupná" in df.value.columns)
    assert "pracovný čas (kalendár)" in list(av["metrika"]) and "nie" in list(av["dostupná"])


def test_delete_import_removes_tables_and_raw_file(tmp_path):
    cfg, db, a, b = env(tmp_path)
    from radar.event_logs import delete_import

    iid, _ = import_file(cfg, db, a, write(tmp_path / "log.csv", ["case_id", "activity", "timestamp", "event_id"], SYNTH))
    confirm_mapping(cfg, db, iid, MAP)
    delete_import(cfg, db, iid)
    assert db.get_eventlog_import(iid) is None
    assert not list((tmp_path / "data" / "eventlogs").glob("org_*_raw/*.csv"))
    con = duckdb.connect(str(org_store(cfg, a)), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM information_schema.tables").fetchone()[0] == 0
    con.close()


def test_slovak_column_names_and_process_hypotheses_are_recognized(tmp_path):
    """16. 10. 2026: „Číslo dokladu“ sa nenavrhlo ako ID prípadu a hypotéza o ručnom prepisovaní dokladov nevyšla ako
    procesná (event log sa dal žiadať iba s vlastným zdôvodnením)."""
    from radar.event_logs import event_log_fit

    mapping, notes = propose_mapping(["Číslo dokladu", "Udalosť", "Čas udalosti", "Pracovník", "Čas exportu"])
    assert mapping["case_id"] == "Číslo dokladu" and mapping["timestamp"] == "Čas udalosti"
    assert "Pracovník" not in mapping.values() and not any("chýba stĺpec" in n for n in notes)
    cfg, db, a, b = env(tmp_path)
    for title in ("Účtovné kancelárie strácajú čas ručným prepisovaním údajov z dokladov",
                  "Malé realitné kancelárie nestíhajú rýchlo a kvalitne spracovať dopyty záujemcov"):
        assert event_log_fit(db, db.create_hypothesis(title, "", "segment"))[0], title
    assert not event_log_fit(db, db.create_hypothesis("Nízka spotreba mlieka bráni rozvoju odvetvia", "", "výrobcovia"))[0]
