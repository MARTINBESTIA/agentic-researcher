"""M7 – Brána 0: citáty z rozhovorov, metriky a verdikt, experti, pseudonymizácia správ a exportov."""

import csv

from radar.config import Config, EmbeddingsConfig, PrivacyConfig
from radar.crm import export, export_person, gate0_metrics, record_decision, record_interview
from radar.db import DB
from radar.report import build_report

NOTES = ("Naposledy sa to stalo minulý týždeň: dodávateľ ložísk meškal 6 týždňov a museli sme presúvať montáž. "
         "Riešime to tabuľkou v Exceli a telefonovaním.")


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), reports_dir=str(tmp_path / "reports"),
                 exports_dir=str(tmp_path / "exports"), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Meškanie dodávok", "Popis", "zákazkoví výrobcovia")
    db.set_people_selection(hid, True)
    org_id, _ = db.upsert_organization("Fiktívna Strojáreň s.r.o.", "fiktivna strojaren", "evidence")
    db.link_org_hypothesis(org_id, hid, "mentioned_with_problem")
    pid = db.insert_person(org_id, "Anna Prvá", "anna prva", "https://f.sk/tim", "Anna Prvá, vedúca nákupu", hid,
                           role_title="vedúca nákupu")
    db.add_contact_point(pid, org_id, "email", "anna.prva@fiktivna-strojaren.sk", "https://f.sk/tim", "anna.prva@fiktivna-strojaren.sk", "regex")
    db.upsert_link(pid, hid, "role_owner")
    return cfg, db, hid, pid


def interviews(cfg, db, hid, n, unprompted, kind="customer"):
    for i in range(n):
        record_interview(cfg, db, hid, kind, f"2026-10-{10 + i:02d}", NOTES,
                         mentioned_unprompted=1 if i < unprompted else 0, commitment="intro" if i == 0 else "none",
                         role_label="vedúci nákupu", org_type="výrobná firma")


def test_interview_quote_not_in_notes_is_rejected(tmp_path):
    cfg, db, hid, pid = setup(tmp_path)
    result = record_interview(cfg, db, hid, "customer", "2026-10-12", NOTES, person_id=pid,
                              quotes=["dodávateľ ložísk meškal 6 týždňov", "Strácame milión eur ročne."])
    assert (result["verified"], result["rejected"]) == (1, 1)
    claims = {c["quote"]: c for c in db.conn.execute("SELECT * FROM claims")}
    assert claims["Strácame milión eur ročne."]["status"] == "rejected"
    ok = claims["dodávateľ ložísk meškal 6 týždňov"]
    assert ok["status"] == "verified" and ok["origin"] == "interview"
    doc = db.get_document(ok["document_id"])
    assert doc["source_kind"] == "interview" and doc["url"].startswith("interview://")
    assert [e["id"] for e in db.evidence_for(hid)] == [ok["id"]]
    assert db.get_person(pid)["status"] == "interviewed"


def test_gate0_verdicts(tmp_path):
    cfg, db, hid, pid = setup(tmp_path)
    interviews(cfg, db, hid, 7, 3)
    assert gate0_metrics(db, hid)["verdict_proposal"] == "nedostatok dát"
    interviews(cfg, db, hid, 3, 0)
    m = gate0_metrics(db, hid)
    assert (m["customer_interviews"], m["unprompted"], m["verdict_proposal"]) == (10, 3, "chýba platiteľ")
    db.set_hypothesis_fields(hid, payer_confirmed="vedúci nákupu z rozpočtu výroby")
    assert gate0_metrics(db, hid)["verdict_proposal"] == "pass"
    record_decision(db, hid, "continue", "Problém opísali sami, platiteľ jasný.")
    assert db.get_hypothesis(hid)["gate0_status"] == "passed"
    assert db.decisions_for(hid)[0]["metrics_json"]


def test_fail_when_too_few_unprompted_and_experts_not_counted(tmp_path):
    cfg, db, hid, pid = setup(tmp_path)
    interviews(cfg, db, hid, 10, 2)
    db.set_hypothesis_fields(hid, payer_confirmed="riaditeľ")
    assert gate0_metrics(db, hid)["verdict_proposal"] == "fail"
    interviews(cfg, db, hid, 5, 5, kind="expert")  # experti nič nezmenia
    m = gate0_metrics(db, hid)
    assert (m["customer_interviews"], m["unprompted"], m["expert_interviews"], m["verdict_proposal"]) == (10, 2, 5, "fail")


def test_report_and_export_without_personal_data(tmp_path):
    cfg, db, hid, pid = setup(tmp_path)
    record_interview(cfg, db, hid, "customer", "2026-10-12", NOTES + " Anna Prvá súhlasila s ďalším stretnutím.", person_id=pid)
    oid = db.insert_outreach(pid, hid, "email", "B", "Otázka pre Annu Prvú", "Dobrý deň, pani Prvá,\nAnna Prvá …",
                             [], {"ok": True}, "draft")
    path = build_report(cfg, db)
    report = path.read_text(encoding="utf-8")
    assert "## Brána 0" in report
    assert "Anna Prvá" not in report and "anna.prva@" not in report
    for what in ("people", "outreach", "interviews", "hypotheses"):
        for fmt in ("csv", "xlsx"):
            out = export(cfg, db, what, fmt)
            if fmt == "csv":
                text = out.read_text(encoding="utf-8-sig")
                assert "Anna Prvá" not in text and "anna.prva@" not in text, what
    personal = export(cfg, db, "people", "csv", include_personal=True).read_text(encoding="utf-8-sig")
    assert "Anna Prvá" in personal and "anna.prva@fiktivna-strojaren.sk" in personal
    rows = list(csv.reader(export(cfg, db, "people", "csv").read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
    assert rows[1][0].startswith("Osoba P")
    access = export_person(cfg, db, pid).read_text(encoding="utf-8")
    assert "anna.prva@fiktivna-strojaren.sk" in access and str(oid) in access
