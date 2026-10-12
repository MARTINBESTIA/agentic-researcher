"""M6 – návrhy oslovení: dĺžky, kontroly, oslovenie, schválenie, denný limit, follow-up, námietka,
obstarávanie a mailto (falošný model, bez siete)."""

import json
from datetime import date, datetime, timezone
from urllib.parse import unquote

import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig, OutreachConfig, PrivacyConfig, SenderConfig
from radar.db import DB
from radar.outreach import (
    FOOTER_MARK, OutreachError, add_business_days, approve, check_draft, compose, greeting, mailto_link, mark_sent,
    run_drafts, set_status,
)
from radar.util import Deadline
from tests.fakes import Recorder, make_router

DOC = "Úvod. " * 50 + "\nVýrobcovia hlásia meškanie dodávok komponentov o 6 týždňov.\n" + "Záver. " * 50
QUOTE = "Výrobcovia hlásia meškanie dodávok komponentov o 6 týždňov."
REAL_SENDER = SenderConfig(name="Test Odosielateľ", email="test@example.org", affiliation="študent inžinierskeho štúdia",
                           privacy_notice_url="https://example.org/info")


def setup(tmp_path, sender=None, cap=10):
    cfg = Config(
        db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
        privacy=PrivacyConfig(lia_completed=True), outreach=OutreachConfig(sender=sender or SenderConfig(), daily_cap=cap),
    )
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Meškanie dodávok komponentov v zákazkovej výrobe", "Popis", "zákazkoví výrobcovia")
    db.set_people_selection(hid, True)
    doc_id, _ = db.upsert_document("https://zvaz.sk/prieskum", "Prieskum", "search", "t", "q", "h", DOC)
    cid = db.insert_claim(doc_id, 0, "t", "Výrobcovia hlásia meškanie dodávok komponentov o 6 týždňov.", "výrobcovia", "pain", QUOTE)
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    org_id, _ = db.upsert_organization("Fiktívna Strojáreň s.r.o.", "fiktivna strojaren", "evidence",
                                       website="https://fiktivna-strojaren.sk/", website_verified=1, sector_kind="private")
    db.link_org_hypothesis(org_id, hid, "mentioned_with_problem", cid, "https://zvaz.sk/prieskum")
    pid = db.insert_person(org_id, "Anna Prvá", "anna prva", "https://fiktivna-strojaren.sk/tim", "Anna Prvá, vedúca nákupu", hid,
                           role_title="vedúca nákupu")
    db.add_contact_point(pid, org_id, "email", "anna.prva@fiktivna-strojaren.sk", "https://fiktivna-strojaren.sk/tim",
                         "Anna Prvá, vedúca nákupu – anna.prva@fiktivna-strojaren.sk", "regex")
    link_id = db.upsert_link(pid, hid, "org_affected", cid, "https://zvaz.sk/prieskum")
    db.set_link_priority(link_id, 0.8, {"role_fit": 1.0})
    rec = Recorder()
    rec.responses = {
        "Entailment": {"verdict": "supported", "reason": "ok"},
        "DraftOut": {"subject": "Vaša skúsenosť s meškaním dodávok – 20 minút?",
                     "body": "Podľa prieskumu výrobcovia hlásia meškanie dodávok komponentov o 6 týždňov. Zaujíma ma, kedy sa vám to "
                             "stalo naposledy a ako ste to riešili. Mali by ste 20 minút na telefonát?",
                     "facts_used": ["F2"]},
        "DraftCheck": {"verdict": "ok", "problems": []},
    }
    router = make_router(cfg, db, rec)
    return cfg, db, hid, pid, org_id, rec, router


def person_row(gender=None, role="vedúca nákupu", name="Ing. Anna Prvá"):
    return {"full_name": name, "gender_hint": gender, "role_title": role}


def test_lengths_email_and_linkedin(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    stats = run_drafts(cfg, db, router, Deadline(1), [hid])
    assert stats["draft"] == 2  # variant A (dôkaz o organizácii) aj B
    rows = db.outreach_list(hid)
    for row in rows:
        main = row["body"].split(FOOTER_MARK)[0]
        assert len(main.split()) <= 150 and len(row["subject"]) <= 70
        assert "oprávnený záujem" in row["body"] and "https://example.org/info" in row["body"]
    subject, note = compose(cfg, "linkedin", person_row(), "private",
                            "skúmam, ako zákazkoví výrobcovia riešia meškanie dodávok. Mali by ste 20 min na rozhovor?",
                            "", "https://x.sk")
    assert len(note) <= 200 and note.endswith("https://example.org/info")
    checks = check_draft(cfg, "linkedin", None, note + " " * 0, "", [], "https://x.sk")
    assert checks["ok"], checks["problems"]
    long_note = compose(cfg, "linkedin", person_row(), None, "x " * 120, "", "https://x.sk")[1]
    assert not check_draft(cfg, "linkedin", None, long_note, "", [], "https://x.sk")["ok"]


def test_forbidden_phrase_foreign_number_or_missing_footer_needs_edit(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    facts = [{"id": "F1", "text": "Výrobcovia hlásia meškanie o 6 týždňov.", "url": "https://zvaz.sk/p", "kind": "evidence"}]
    core = "Ponúkame vám naše riešenie, ktoré ušetrí 40 % času."
    subject, body = compose(cfg, "email", person_row(), "private", core, "Otázka", "https://x.sk")
    checks = check_draft(cfg, "email", subject, body, core, facts, "https://x.sk")
    assert not checks["ok"]
    joined = " ".join(checks["problems"])
    assert "ponúkame" in joined and "40" in joined
    no_footer = body.split(FOOTER_MARK)[0]
    assert "Chýba pätička" in " ".join(check_draft(cfg, "email", subject, no_footer, "ok", facts, "https://x.sk")["problems"])
    ok_core = "Výrobcovia hlásia meškanie o 6 týždňov. Mali by ste 20 minút na rozhovor?"
    assert check_draft(cfg, "email", "Otázka", compose(cfg, "email", person_row(), None, ok_core, "Otázka", "https://x.sk")[1],
                       ok_core, facts, "https://x.sk")["ok"]
    # model nájde vetu mimo faktov -> needs_edit
    rec.responses["DraftCheck"] = {"verdict": "problems", "problems": [{"sentence": "x", "issue": "mimo faktov"}]}
    run_drafts(cfg, db, router, Deadline(1), [hid])
    assert {r["status"] for r in db.outreach_list(hid)} == {"needs_edit"}


def test_greeting_only_from_verified_gender():
    assert greeting(person_row(None), "private") == "Dobrý deň,"
    assert greeting(person_row("female"), "private") == "Dobrý deň, pani Prvá,"
    assert greeting(person_row("male", name="Ján Novák"), "private") == "Dobrý deň, pán Novák,"
    assert greeting(person_row("female", role="starostka"), "public") == "Vážená pani starostka,"
    assert greeting(person_row(None, role="starosta obce", name="Ján Novák"), "public") == "Vážený pán starosta,"


def test_approval_blocked_with_placeholder_sender(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path)  # placeholdery {MENO_PRIEZVISKO} …
    run_drafts(cfg, db, router, Deadline(1), [hid])
    row = db.outreach_list(hid)[0]
    assert row["status"] == "draft"
    with pytest.raises(OutreachError, match="profil odosielateľa"):
        approve(cfg, db, row["id"])
    cfg.outreach.sender = REAL_SENDER  # po doplnení profilu sa pevné časti zložia s aktuálnymi údajmi
    approve(cfg, db, row["id"])
    approved = db.get_outreach(row["id"])
    assert approved["status"] == "approved" and "{" not in approved["body"] and "Test Odosielateľ" in approved["body"]


def test_eleventh_approval_same_day_blocked(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER, cap=10)
    now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    ids = [db.insert_outreach(pid, hid, "email", "B", "Otázka", "Dobrý deň, text", [], {"ok": True}, "draft") for _ in range(11)]
    for oid in ids[:10]:
        approve(cfg, db, oid, now=now)
    with pytest.raises(OutreachError, match="Denný limit"):
        approve(cfg, db, ids[10], now=now)
    approve(cfg, db, ids[10], now=datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc))  # na druhý deň áno


def test_follow_up_due_on_fifth_business_day_over_weekend(tmp_path):
    assert add_business_days(date(2026, 10, 8), 5) == date(2026, 10, 15)  # štvrtok -> štvrtok (víkend sa nepočíta)
    assert add_business_days(date(2026, 10, 8), 5, ["2026-10-12"]) == date(2026, 10, 16)  # sviatok
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    oid = db.insert_outreach(pid, hid, "email", "B", "Otázka", "Dobrý deň", [], {"ok": True}, "draft")
    approve(cfg, db, oid)
    due = mark_sent(cfg, db, oid, when=date(2026, 10, 8))
    row = db.get_outreach(oid)
    assert due == date(2026, 10, 15) and row["follow_up_due"] == "2026-10-15" and row["status"] == "sent"
    assert db.get_person(pid)["status"] == "contacted"


def test_objected_runs_forget(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    oid = db.insert_outreach(pid, hid, "email", "B", "Otázka", "Dobrý deň", [], {"ok": True}, "sent")
    result = set_status(cfg, db, oid, "objected")
    assert result["people_removed"] == 1
    assert db.get_person(pid) is None and db.get_outreach(oid) is None
    assert db.conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] >= 2


def test_declined_sets_purge_after(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    oid = db.insert_outreach(pid, hid, "email", "B", "Otázka", "Dobrý deň", [], {"ok": True}, "sent")
    set_status(cfg, db, oid, "declined")
    person = db.get_person(pid)
    assert person["status"] == "declined" and person["purge_after"] is not None


def test_active_procurement_gives_warning_not_draft(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    db.set_org_hypothesis(org_id, hid, procurement_active=1, procurement_note="https://www.uvo.gov.sk/ptk/123")
    stats = run_drafts(cfg, db, router, Deadline(1), [hid])
    rows = db.outreach_list(hid)
    assert stats == {"blocked_procurement": 1}
    assert [r["status"] for r in rows] == ["blocked"] and "oficiálnou cestou" in rows[0]["body"]
    assert "DraftOut" not in [c[0] for c in rec.calls]
    with pytest.raises(OutreachError):
        approve(cfg, db, rows[0]["id"])


def test_mailto_is_url_encoded():
    body = "Dobrý deň,\nďakujem – „nie“ stačí.\n\n--\nViac: https://example.org/info?a=1&b=2"
    link = mailto_link("anna.prva@fiktivna-strojaren.sk", "Otázka č. 1 – meškanie", body)
    assert link.startswith("mailto:anna.prva@fiktivna-strojaren.sk?subject=")
    assert " " not in link and "\n" not in link and "%0D%0A" in link and "%C4%8F" in link  # ď
    query = link.split("?", 1)[1]
    parts = dict(p.split("=", 1) for p in query.split("&"))
    assert unquote(parts["subject"]) == "Otázka č. 1 – meškanie"
    assert unquote(parts["body"]).replace("\r\n", "\n") == body
    assert mailto_link("a@b.sk", "x", "y" * 3000) is None  # dlhý odkaz -> iba kopírovanie


def test_facts_pass_entail_gate_with_three_votes(tmp_path):
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    rec.responses["Entailment"] = [{"verdict": "supported", "reason": "a"}, {"verdict": "partial", "reason": "b"},
                                   {"verdict": "supported", "reason": "c"}]
    run_drafts(cfg, db, router, Deadline(1), [hid])
    gate_calls = [c for c in rec.calls if c[0] == "Entailment"]
    assert len(gate_calls) == 3 and all(c[1] == "radar-qwen3.6-27b" and c[3] == 0.7 for c in gate_calls)
    claim = db.conn.execute("SELECT gate_verdict, low_agreement FROM claims").fetchone()
    assert tuple(claim) == ("supported", 1)  # 2:1 = prijaté s príznakom nízkej zhody
    facts = json.loads(db.outreach_list(hid)[0]["facts_json"])
    assert any(f["kind"] == "evidence" for f in facts)


def test_refresh_draft_rechecks_without_regenerating(tmp_path):
    from radar.outreach import refresh_draft

    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    core = "Výrobcovia hlásia meškanie dodávok o 6 týždňov. Máte čas na 20-30 minútový rozhovor?"
    oid = db.insert_outreach(pid, hid, "email", "B", "", "starý text", [{"id": "F1", "text": QUOTE, "url": "https://zvaz.sk/prieskum"}],
                             {"ok": False, "problems": ["Chýba predmet."], "core": core, "subject_core": ""}, "needs_edit")
    assert refresh_draft(cfg, db, router, oid) == "draft"
    row = db.get_outreach(oid)
    assert row["subject"] and "Test Odosielateľ" in row["body"] and "zvaz.sk" not in row["subject"]
    assert [c[0] for c in rec.calls] == ["DraftCheck"]  # iba kontrola faktov, žiadne nové generovanie


def test_gendered_first_person_needs_edit_unless_sender_gender_set(tmp_path):
    """Reálny návrh #45: „Rád by som sa dozvedel…“, hoci rod odosielateľa nie je určený (nehádame ho z mena)."""
    cfg, db, hid, pid, org_id, rec, router = setup(tmp_path, REAL_SENDER)
    core = "Zaujíma ma, ako riešite finančné opravy. Rád by som sa dozvedel, kedy sa to stalo naposledy."
    subject, body = compose(cfg, "email", person_row(), None, core, "Otázka", "https://x.sk")
    checks = check_draft(cfg, "email", subject, body, core, [], "https://x.sk")
    assert not checks["ok"] and "rád by som" in " ".join(checks["problems"])
    neutral = "Zaujíma ma, ako riešite finančné opravy a kedy sa to stalo naposledy. Obecná rada to schválila?"
    subject, body = compose(cfg, "email", person_row(), None, neutral, "Otázka", "https://x.sk")
    assert check_draft(cfg, "email", subject, body, neutral, [], "https://x.sk")["ok"]
    cfg.outreach.sender.gender = "m"  # človek zvolil rod v config.local.yaml
    subject, body = compose(cfg, "email", person_row(), None, core, "Otázka", "https://x.sk")
    assert check_draft(cfg, "email", subject, body, core, [], "https://x.sk")["ok"]
