"""M22 – plánovanie, účel kontaktov, osi stavu, postup k zákazníkovi, referenčná vzorka (audit 14. 10. 2026, P1 6 a
dokument o kontaktoch, bod 7). Offline, bez modelu a siete."""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from radar import gate1
from radar.autopilot import AGING_KEY, plan_round, priority_ids
from radar.datasets import hyp_dir, import_table, profile_table
from radar.evals import HOLDOUT_PERCENT, eval_items, is_holdout, reference_status
from radar.people import contact_purposes, rank_hypothesis
from radar.progress import axes, business_state, customer_progress, set_business_step
from tests.test_m5_people import add_org, setup


# ======================================================================= P1 6: krátke behy autopilota
def test_short_run_plans_user_inputs_and_branches_before_discovery(tmp_path):
    """Sonda z auditu: 90 min a všetky úlohy pripravené → predtým iba scout a topics."""
    cfg, db, hid, rec, router = setup(tmp_path)
    db.add_gate1_input(hid, "schéma: obec; rok; výdavky v €")
    db.create_hypothesis("Neotestovaná", "", "x")
    now = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
    plan = plan_round(cfg, db, "config.yaml", now + timedelta(minutes=90), 1, now=now)
    kinds = [p["akcia"] for p in plan["naplánované"]]
    assert kinds[0] == "gate1-input" and "gate1" in kinds  # tvoje dáta a vetva vybranej hypotézy prvé
    assert "scout" in kinds and kinds.index("scout") > kinds.index("gate1")  # zber dostal vyhradený podiel
    assert plan["rezerva_na_zber_min"] == round(90 * cfg.workflows.autopilot_discovery_share)
    skipped = [k for k, why in plan["preskočené"].items() if why == "nezmestí sa do času"]
    assert all(db.get_setting(AGING_KEY)[k] == 1 for k in skipped)  # čakajúca akcia časom dostane prednosť


def test_priority_hypotheses_are_limited_and_furthest_first(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    ids = [db.create_hypothesis(f"H{i}", "", "x") for i in range(4)]
    for i in ids:
        db.set_people_selection(i, True)
    db.set_gate_status(ids[3], 1, "passed")
    db.set_gate_status(ids[2], 1, "running")
    got = priority_ids(cfg, db)
    assert len(got) == 3 and got[:2] == [ids[3], ids[2]]  # najďalej v lieviku prvé, najviac 3


# ======================================================================= kontakty: účel s dôkazom
def _person(db, org_id, hid, name, title, kind="website", url="https://firma.sk/tim"):
    return db.insert_person(org_id, name, name.lower(), url, f"{name}, {title}", hid, role_title=title, source_kind=kind)


def test_contact_purposes_need_evidence_and_politicians_are_not_owners(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org = add_org(db, hid, "Firma s.r.o.", "https://firma.sk/")
    org_row = db.get_organization(org)
    cases = {
        "vedúca nákupu": ("website", 1.0, {"vlastník procesu"}),
        "referent nákupu": ("website", 0.5, {"používateľ riešenia"}),
        "finančný riaditeľ": ("website", 0.3, {"schvaľovateľ rozpočtu"}),
        "dátový analytik": ("website", 0.3, {"správca dát"}),
        "člen predstavenstva": ("rpo", 1.0, {"politik alebo člen orgánu"}),
        "poslanec NR SR": ("evidence", 1.0, {"politik alebo člen orgánu"}),
        "vedúci výroby": ("evidence", 1.0, {"citovaná osoba"}),  # spomenutý v článku – kompetencia nedoložená
    }
    for i, (title, (kind, fit, want)) in enumerate(cases.items()):
        pid = _person(db, org, hid, f"Osoba {i}", title, kind)
        purposes = contact_purposes(db.get_person(pid), fit, org_row)
        assert {p["rola"] for p in purposes} == want, title
        assert all(p["dôkaz"] for p in purposes)


def test_rank_gives_role_owner_only_with_responsibility_evidence(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["vedúci nákupu", "nákupca"], "rationale": "r"}])
    org = add_org(db, hid, "Firma s.r.o.", "https://firma.sk/")
    owner = _person(db, org, hid, "Anna Prvá", "vedúci nákupu")
    user = _person(db, org, hid, "Boris Druhý", "nákupca")
    board = _person(db, org, hid, "Cyril Tretí", "člen predstavenstva, vedúci nákupu", kind="rpo",
                    url="https://www.registeruz.sk/x")
    stats = rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    links = {l["person_id"]: l for l in db.links_for_hypothesis(hid)}
    assert links[owner]["link_type"] == "role_owner" and links[user]["link_type"] == "process_user"
    assert board not in links and stats["role_without_evidence"] == 1  # člen orgánu nie je vlastník procesu
    comp = json.loads(links[owner]["priority_json"])
    assert comp["účely"][0]["rola"] == "vlastník procesu" and "firma.sk" in comp["účely"][0]["dôkaz"]


# ======================================================================= osi stavu a obchodný stav
def test_axes_are_separate_and_business_steps_need_proof(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    ax = axes(cfg, db, hid)
    assert set(ax) == {"literatúra", "zákazník", "dáta", "ochota_platiť"}
    assert ax["zákazník"]["stav"] == "chýba" and ax["ochota_platiť"]["stav"] == "chýba" and ax["dáta"]["stav"] == "nezačaté"
    with pytest.raises(ValueError):
        set_business_step(db, hid, "vzorka", True, "")  # hotový krok bez dôkazu
    set_business_step(db, hid, "platiteľ", True, "riaditeľ účtovnej firmy, rozhovor 12. 10.: zaplatí 50 €/mes")
    # M35 (26. 10. 2026): známy platiteľ nie je ochota platiť – „potvrdená“ je až záujem o kúpu alebo platba
    assert business_state(db, hid)["platiteľ"]["hotové"] and axes(cfg, db, hid)["ochota_platiť"]["stav"] == "platiteľ známy"
    assert axes(cfg, db, hid)["zákazník"]["stav"] == "chýba"  # platiteľ nepotvrdzuje problém u zákazníka


def test_customer_progress_counts_steps_toward_customer(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    set_business_step(db, hid, "vzorka", True, "schéma + 20 riadkov od vedúcej účtovníctva")
    set_business_step(db, hid, "pilot", True, "pilot 4 týždne, 2 účtovníčky")
    prog = customer_progress(cfg, db)
    assert prog["spolu"]["vzorky"] == 1 and prog["spolu"]["piloty"] == 1 and prog["spolu"]["rozhovory"] == 0
    assert prog["prevádzkové"]["hypotézy"] >= 1  # počet hypotéz je iba prevádzková metrika


# ======================================================================= referenčná vzorka
def test_reference_sample_has_stable_holdout_and_system_agreement(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    for i in range(1, 41):
        db.add_label("field_binding", "binding", i, "confirmed" if i % 4 else "rejected")
    rows = {r["úloha"]: r for r in reference_status(db)}
    fb = rows["field_binding"]
    assert fb["označené"] == 40 and fb["chýba"] == 0 and fb["zhoda_systému"] == 0.75 and fb["interval_95"]
    hold = [i for i in range(1, 41) if is_holdout("field_binding", "binding", i)]
    assert fb["kontrolná_časť"] == len(hold) and 0 < len(hold) < 40
    assert is_holdout("field_binding", "binding", hold[0])  # stabilné medzi behmi (hash, nie náhoda)
    assert rows["contact_role"]["cieľ"] == 20 and rows["entail"]["cieľ"] == 30 and HOLDOUT_PERCENT == 20
    db.add_label("entail", "claim", 1, "supported")
    db.add_label("entail", "claim", 2, "unsupported")
    split = {i.object_id for i in eval_items(db, "entail", split="holdout")} | {
        i.object_id for i in eval_items(db, "entail", split="tuning")}
    assert split == {1, 2}


# ======================================================================= objavovanie, IČO, snapshot
def test_vendor_documents_do_not_reward_discovery(tmp_path):
    from radar.discover import update_arm_stats

    cfg, db, hid, rec, router = setup(tmp_path)
    for i, kind in enumerate(("vendor", "government")):
        doc, _ = db.upsert_document(f"https://zdroj{i}.sk/a", "t", "discovery", "t", "q", f"h{i}", "text")
        db._exec("UPDATE documents SET source_category = 'priemysel', source_type = ?, extracted_at = '2026-10-01' "
                 "WHERE id = ?", (kind, doc))
        cid = db.insert_claim(doc, 0, "t", "Problém", "firmy", "pain", "Problém", [])
        db.set_claim_verification(cid, "verified", 100, "ok")
    assert update_arm_stats(db) == 2
    arm = {r["arm"]: r for r in db._all("SELECT * FROM query_stats")}["priemysel"]
    assert arm["tries"] == 2 and arm["successes"] == 1  # dokument dodávateľa nie je úspech


def test_identifier_quality_keeps_original_and_reprofile_from_snapshot(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    cfg.gate1.datasets_dir = str(tmp_path / "datasets")
    hyp_dir(cfg, hid).mkdir(parents=True, exist_ok=True)
    path = tmp_path / "d.csv"
    path.write_text("ico_objednavatel;suma\n00313271;1\nIČO: 313 271;2\n35763469;3\nDE123456789X;4\n", encoding="utf-8")
    import_table(cfg, hid, "ds1", path, "csv")
    prof = profile_table(cfg, hid, "ds1")
    q = prof["identifikátory"]["ico_objednavatel"]
    assert q == {"vyplnené": 4, "čisté_8_číslic": 2, "normalizované": 1, "nerozpoznané": 1, "kontrolná_číslica_ok": 3}
    from radar.datasets import run_query

    assert run_query(cfg, hid, "SELECT ico_objednavatel FROM ds1 WHERE suma = 2")[0][0] == "IČO: 313 271"  # pôvodná hodnota
    ds = db.add_dataset(hid, None, "https://data.sk/d.csv", "ds1", status="ok", rows=3, profile_json={
        "import": {"adaptér": "csv"}})
    assert gate1.reprofile(cfg, db, hid) == 1  # 3 v metadátach, 4 v tabuľke
    row = db.get_dataset(ds)
    snap = json.loads(row["profile_json"])
    assert row["rows"] == 4 and snap["snapshot"]["predtým"] == 3 and snap["import"] == {"adaptér": "csv"}
    assert gate1.reprofile(cfg, db, hid) == 0  # už sedí


def test_problems_page_shows_axes_and_eval_page_reference_sample(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/problems.py").run()
    assert not at.exception, at.exception
    assert {"Literatúra", "Zákazník", "Dáta", "Ochota platiť"} <= {m.label for m in at.metric}
    at.switch_page("app_pages/eval.py").run()
    assert not at.exception, at.exception
    assert any("úloha" in df.value.columns and "field_binding" in list(df.value["úloha"]) for df in at.dataframe)
