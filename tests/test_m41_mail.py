"""M41 – pošta (27. 10. 2026): odoslané oslovenia a odpovede z Gmailu bez ručného zapisovania. Iba správy od a pre
kontakty z oslovení, iba hlavičky pri kontrole; rozbor odpovede lokálnym modelom je návrh s citáciami overenými kódom,
zapisuje až človek. Offline (falošná schránka, falošný model)."""

import pytest

from radar.config import Config, EmbeddingsConfig, MailConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.mail import (
    GmailImap, Header, MailError, apply_proposal, clean_body, dismiss, extract, parse_header, pending_proposals, sync,
)
from radar.util import Deadline
from tests.fakes import Recorder, make_router
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI

ME = "odosielatel@example.com"
PERSON = "konatel@reality-test.sk"
GENERIC = "info@ucty-test.sk"
REPLY_TEXT = ("Dobrý deň,\n\náno, dopyty nám naozaj unikajú, minulý týždeň sme dvom záujemcom odpísali až na druhý deň. "
              "Dnes platíme 49 € mesačne za CRM a aj tak to nestíhame. Môžeme sa porozprávať v utorok o 14:00.\n\n"
              "S pozdravom\nKonateľ\n0905 123 456\n\nDňa 20. 10. 2026 o 9:00 odosielatel@example.com napísal:\n"
              "> Dobrý deň, chcem sa opýtať na spracovanie dopytov…")


class FakeMailbox:
    """Schránka: hlavičky podľa UID a telá podľa Message-ID; zaznamenáva, čo sa čítalo."""

    def __init__(self, headers, bodies=None):
        self.headers_list, self.bodies, self.searched, self.bodies_read = headers, bodies or {}, [], []

    def search(self, addrs, days):
        self.searched.append(list(addrs))
        return [h.uid for h in self.headers_list]

    def headers(self, uids):
        return [h for h in self.headers_list if h.uid in uids]

    def body(self, message_id):
        self.bodies_read.append(message_id)
        if message_id not in self.bodies:
            raise MailError("správa sa v schránke nenašla")
        return self.bodies[message_id]


def _world(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(),
                 embeddings=EmbeddingsConfig(enabled=False), privacy=PrivacyConfig(lia_completed=True),
                 mail=MailConfig(enabled=True, user=ME))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Malé realitné kancelárie nestíhajú spracovať dopyty", "", "realitné kancelárie")
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Reality Test s.r.o.", "reality test", "evidence", ico="11111111", sector_kind="private")
    pid = db.insert_person(org, "Ing. Peter Testovací", "peter testovaci", "https://reality-test.sk/kontakt",
                           "Peter Testovací, konateľ", hid, role_title="konateľ")
    cp = db.add_contact_point(pid, org, "email", PERSON, "https://reality-test.sk/kontakt", f"konateľ {PERSON}", "mailto")
    draft = db.insert_outreach(pid, hid, "email", "A", "Otázka k dopytom záujemcov", "text návrhu", [], {"ok": True},
                               "draft", cp)
    org2, _ = db.upsert_organization("Účty Test s.r.o.", "ucty test", "evidence", ico="22222222", sector_kind="private")
    cp2 = db.add_contact_point(None, org2, "email", GENERIC, "https://ucty-test.sk", f"kontakt {GENERIC}", "mailto", True)
    generic = db.insert_outreach(None, hid, "email", "B", "Otázka k dokladom", "text", [], {"ok": True}, "approved",
                                 cp2, org_id=org2)
    return cfg, db, hid, pid, draft, generic


def H(uid, mid, frm, to, subject, when, thread=None, auto=False, failed=()):
    return Header(uid=uid, message_id=mid, thread_id=thread, sent_at=when, from_addr=frm, to=list(to), subject=subject,
                  auto=auto, failed=list(failed))


MAIL = [
    H(1, "<s1@x>", ME, [PERSON], "Otázka k dopytom záujemcov", "2026-10-20T09:00:00+02:00", thread="T1"),
    H(2, "<s2@x>", ME, [GENERIC], "Otázka k dokladom", "2026-10-20T09:05:00+02:00", thread="T2"),
    H(3, "<news@x>", "newsletter@obchod.sk", [ME], "Akcia týždňa", "2026-10-21T08:00:00+02:00", thread="T9"),
    H(4, "<ooo@x>", PERSON, [ME], "Automatická odpoveď: Otázka k dopytom", "2026-10-20T09:01:00+02:00", thread="T1",
      auto=True),
    H(5, "<r1@x>", PERSON, [ME], "Re: Otázka k dopytom záujemcov", "2026-10-22T10:00:00+02:00", thread="T1"),
    H(6, "<b1@x>", "mailer-daemon@googlemail.com", [ME], "Delivery Status Notification (Failure)",
      "2026-10-20T09:06:00+02:00", failed=[GENERIC]),
]


def test_sync_records_sent_replies_bounces_and_ignores_unrelated_mail(tmp_path):
    cfg, db, hid, pid, draft, generic = _world(tmp_path)
    box = FakeMailbox(MAIL)
    out = sync(cfg, db, box)
    assert out == {"odoslané": 2, "automatické_odpovede": 1, "nedoručené": 1, "odpovede": 1}
    assert sorted(box.searched[0]) == sorted([PERSON, GENERIC])  # hľadá sa iba podľa adries z oslovení
    row = db.get_outreach(draft)
    assert row["status"] == "replied" and row["sent_at"] == "2026-10-20" and row["follow_up_due"]  # aj neschválený návrh
    assert db.get_outreach(generic)["status"] == "bounced"
    kinds = {m["message_id"]: (m["kind"], m["status"]) for m in db.mail_messages()}
    assert "<news@x>" not in kinds  # nesúvisiaca pošta sa nezapíše
    assert kinds["<r1@x>"] == ("reply", "new") and kinds["<ooo@x>"] == ("auto_reply", "recorded")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(mail_messages)")}
    assert not cols & {"body", "text", "from_addr", "to_addr"}  # telá ani adresy sa neukladajú
    assert box.bodies_read == []  # kontrola číta iba hlavičky
    assert sync(cfg, db, box) == {}  # druhý beh nič nové (Message-ID)


def _router(cfg, db, answer):
    rec = Recorder()
    rec.responses["MailReply"] = answer
    return rec, make_router(cfg, db, rec)


def test_reply_becomes_a_proposal_with_verified_quotes_and_human_applies_it(tmp_path):
    cfg, db, hid, pid, draft, generic = _world(tmp_path)
    box = FakeMailbox(MAIL, {"<r1@x>": REPLY_TEXT})
    sync(cfg, db, box)
    seen = []

    def answer(messages):
        seen.append(messages[-1]["content"])
        return {"reply": "relevant", "problem_relevance": "confirmed",
                "problem_quote": "dopyty nám naozaj unikajú, minulý týždeň sme dvom záujemcom odpísali až na druhý deň",
                "wtp_stage": "budget_verified", "wtp_quote": "Dnes platíme 49 € mesačne za CRM a aj tak to nestíhame.",
                "meeting": "agreed", "meeting_text": "utorok o 14:00", "tool_used": "CRM",
                "next_action": "rozhovor v utorok"}

    rec, router = _router(cfg, db, answer)
    out = extract(cfg, db, box, router, Deadline(5))
    assert out == {"návrhy": 1} and box.bodies_read == ["<r1@x>"]
    # do modelu ide iba nová časť odpovede v <untrusted_document>, bez citovanej pôvodnej správy a telefónu
    assert "<untrusted_document>" in seen[0] and "chcem sa opýtať" not in seen[0] and "905 123" not in seen[0]
    assert rec.models_for("MailReply") == [cfg.models.roles["fast"].model]  # lokálny rýchly model
    [pr] = pending_proposals(db)
    p = pr["návrh"]
    assert pr["kontakt"] == "Ing. Peter Testovací" and pr["organizácia"] == "Reality Test s.r.o."
    assert p["problem_relevance"] == "confirmed" and p["wtp_stage"] == "budget_verified" and p["kontroly"] == []
    assert db.commercial_evidence_for(hid) == [] and db.outcomes_for(hid) == []  # model sám nič nezapísal
    res = apply_proposal(cfg, db, pr["id"])
    outcome = next(o for o in db.outcomes_for(hid) if o["id"] == res["id"])
    assert outcome["problem_relevance"] == "confirmed" and outcome["wtp_stage"] == "budget_verified"
    assert outcome["interview_agreed"] == 1 and outcome["tool_used"] == "CRM"
    [ce] = db.commercial_evidence_for(hid)
    assert ce["stage"] == "budget_verified" and "49 € mesačne za CRM" in ce["evidence"] and ce["evidence"].startswith("e-mail")
    assert db.get_outreach(draft)["status"] == "scheduled" and pending_proposals(db) == []


def test_invented_quotes_are_downgraded_and_proposal_can_be_dismissed(tmp_path):
    cfg, db, hid, pid, draft, generic = _world(tmp_path)
    box = FakeMailbox(MAIL, {"<r1@x>": REPLY_TEXT})
    sync(cfg, db, box)
    _, router = _router(cfg, db, {
        "reply": "relevant", "problem_relevance": "confirmed", "problem_quote": "Problém máme každý deň.",
        "wtp_stage": "purchase_intent", "wtp_quote": "Dnes platíme 49 € mesačne za CRM a aj tak to nestíhame.",
        "wtp_conditions": ""})
    extract(cfg, db, box, router, Deadline(5))
    [pr] = pending_proposals(db)
    p = pr["návrh"]
    assert p["problem_relevance"] == "unclear" and p["wtp_stage"] == "unclear" and len(p["kontroly"]) == 2
    dismiss(db, pr["id"])
    assert pending_proposals(db) == [] and db.outcomes_for(hid) == []


def test_model_failure_keeps_the_reply_for_the_next_run_and_missing_message_is_marked(tmp_path):
    from radar.llm import LLMError

    cfg, db, hid, pid, draft, generic = _world(tmp_path)
    box = FakeMailbox(MAIL, {})
    sync(cfg, db, box)
    _, router = _router(cfg, db, LLMError("model nebeží"))
    assert extract(cfg, db, box, router, Deadline(5)) == {"nenájdené": 1}  # telo už v schránke nie je
    box.bodies["<r1@x>"] = REPLY_TEXT
    db.conn.execute("UPDATE mail_messages SET status = 'new' WHERE message_id = '<r1@x>'")
    db.conn.commit()
    assert extract(cfg, db, box, router, Deadline(5)) == {"neurčené": 1}
    assert db.mail_messages(status="new", kind="reply")  # skúsi sa znova


def test_headers_bodies_and_login_without_password():
    raw = (b"From: Peter <konatel@reality-test.sk>\r\nTo: odosielatel@example.com\r\nSubject: Automatick\xc3\xa1 "
           b"odpove\xc4\x8f\r\nDate: Tue, 20 Oct 2026 09:01:00 +0200\r\nMessage-ID: <ooo@x>\r\nAuto-Submitted: "
           b"auto-replied\r\n\r\n")
    h = parse_header(4, "T1", raw)
    assert (h.from_addr, h.to, h.auto, h.thread_id, h.message_id) == (PERSON, [ME], True, "T1", "<ooo@x>")
    bounce = parse_header(6, None, b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nX-Failed-Recipients: "
                                   b"info@ucty-test.sk\r\nSubject: Delivery Status Notification (Failure)\r\n\r\n")
    assert bounce.failed == [GENERIC]
    text = clean_body(REPLY_TEXT, 3000)
    assert "unikajú" in text and "napísal" not in text and "chcem sa opýtať" not in text and "905 123" not in text
    with pytest.raises(MailError, match="RADAR_GMAIL_APP_PASSWORD"):
        GmailImap(MailConfig(enabled=True, user=ME), None).__enter__()
    with pytest.raises(MailError, match="mail.user"):
        GmailImap(MailConfig(enabled=True), "x").__enter__()


def test_outreach_page_shows_mail_proposals_without_model_calls(gui):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    db = DB(cfg.db_path)
    oid = db.outreach_list()[0]["id"]
    db.add_mail_message("<r9@x>", outreach_id=oid, direction="in", kind="reply", sent_at="2026-10-22T10:00:00+02:00",
                        subject="Re: Otázka", status="extracted",
                        proposal_json={"reply": "relevant", "problem_relevance": "confirmed",
                                       "problem_quote": "obstarávanie nám zaberá veľa času", "wtp_stage": "not_asked",
                                       "meeting": "proposed", "meeting_text": "budúci týždeň", "kontroly": []})
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/outreach.py").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "obstarávanie nám zaberá veľa času" in text and "nepýtali sme sa" in text
    assert any(b.label == "Potvrdiť a zapísať" for b in at.button)
    d = DB(cfg.db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()
