"""M44: odoslané e-maily a konverzia po hypotézach – stav iba z pošty a zápisu človeka, otvorenie e-mailu sa nesleduje."""

from datetime import date

from radar.benchmark import record_manual_outreach
from radar.config import Config, EmbeddingsConfig, Gate1Config, ModelsConfig, OutreachConfig, PeopleConfig, PrivacyConfig
from radar.db import DB
from radar.outcomes import sent_overview
from radar.outreach import add_business_days, mark_follow_up_sent, mark_sent, set_status
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI

SENT = date(2026, 10, 1)


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=False),
                 gate1=Gate1Config(nkod_enabled=False), config_dir=str(tmp_path), outreach=OutreachConfig(daily_cap=50))
    cfg.exports_dir = str(tmp_path / "exports")
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Kancelárie nestíhajú dopyty", "Popis", "kancelárie")
    db.set_people_selection(hid, True)
    return cfg, db, hid


def add_contact(db, hid, i):
    org, _ = db.upsert_organization(f"Fiktívna Firma {i} s.r.o.", f"fiktivna firma {i}", "search",
                                    website=f"https://firma{i}.example/", website_verified=1)
    db.link_org_hypothesis(org, hid, "segment_match")
    return db.add_contact_point(None, org, "email", f"info@firma{i}.example", f"https://firma{i}.example/k",
                                f"info@firma{i}.example", "mailto", is_generic=True)


def sent_message(cfg, db, hid, i, when=SENT):
    oid = record_manual_outreach(cfg, db, hid, add_contact(db, hid, i), f"Otázka {i}")
    mark_sent(cfg, db, oid, when, detected=True)
    return oid


def by_id(overview):
    return {m["id"]: m for m in overview["správy"]}


def test_state_comes_only_from_mail_and_human_record_and_silence_needs_a_follow_up(tmp_path):
    cfg, db, hid = setup(tmp_path)
    fresh = sent_message(cfg, db, hid, 1)
    reminded = sent_message(cfg, db, hid, 2)
    mark_follow_up_sent(cfg, db, reminded)
    follow_day = add_business_days(SENT, cfg.outreach.follow_up_business_days[0], cfg.outreach.holidays)
    db.add_mail_message("<f2>", outreach_id=reminded, direction="out", kind="follow_up", sent_at=follow_day.isoformat(), status="recorded")
    bounced = sent_message(cfg, db, hid, 3)
    set_status(cfg, db, bounced, "bounced")
    replied = sent_message(cfg, db, hid, 4)
    set_status(cfg, db, replied, "replied")
    db.add_mail_message("<r4>", outreach_id=replied, direction="in", kind="reply", sent_at="2026-10-04T09:00:00+00:00", status="new")
    away = sent_message(cfg, db, hid, 5)
    db.add_mail_message("<a5>", outreach_id=away, direction="in", kind="auto_reply", sent_at="2026-10-01T10:00:00+00:00", status="recorded")
    cp = add_contact(db, hid, 6)
    draft = db.insert_outreach(None, hid, "email", "manual", "Neodoslané", "(text)", [], {}, "draft", cp, org_id=db.get_contact(cp)["org_id"])

    silent_from = add_business_days(follow_day, cfg.outreach.follow_up_business_days[-1], cfg.outreach.holidays)
    day_before = add_business_days(follow_day, cfg.outreach.follow_up_business_days[-1] - 1, cfg.outreach.holidays)
    m = by_id(sent_overview(cfg, db, today=SENT))
    assert draft not in m and len(m) == 5  # návrh nie je odoslaný e-mail
    assert m[fresh]["stav"] == "čaká" and m[fresh]["ďalší krok"] == f"pripomienka {follow_day.isoformat()}"
    assert m[bounced]["stav"] == "nedoručené" and m[bounced]["pripomienka"] == "–"  # nedoručenej sa nepripomína
    assert m[replied]["stav"] == "odpovedal" and m[replied]["dní do odpovede"] == 3
    assert m[away]["automatická odpoveď"] == "áno" and m[away]["stav"] == "čaká"  # automatická odpoveď nie je odpoveď

    assert by_id(sent_overview(cfg, db, today=follow_day))[fresh]["stav"] == "poslať pripomienku"
    assert by_id(sent_overview(cfg, db, today=day_before))[reminded]["stav"] == "čaká"  # po pripomienke ešte lehota
    assert by_id(sent_overview(cfg, db, today=silent_from))[reminded]["stav"] == "bez reakcie"


def test_summary_counts_per_hypothesis_and_reply_rate_ignores_bounces(tmp_path):
    cfg, db, hid = setup(tmp_path)
    other = db.create_hypothesis("Účtovníci prepisujú doklady", "Popis", "kancelárie")
    db.set_people_selection(other, True)
    set_status(cfg, db, sent_message(cfg, db, hid, 1), "bounced")
    set_status(cfg, db, sent_message(cfg, db, hid, 2), "declined")
    sent_message(cfg, db, hid, 3)
    set_status(cfg, db, sent_message(cfg, db, other, 4), "interviewed")
    rows = {r["hypotéza"].split(":")[0]: r for r in sent_overview(cfg, db, today=SENT)["súhrn"]}
    first = rows[f"H{hid}"]
    assert (first["odoslané"], first["nedoručené"], first["odpovede"], first["z toho odmietli"], first["čaká"]) == (3, 1, 1, 1, 1)
    assert first["miera odpovedí"] == "1/2 (50%)"  # nedoručená správa sa do miery nepočíta
    assert rows[f"H{other}"]["rozhovory"] == 1 and rows[f"H{other}"]["dohodnuté"] == 1
    assert rows["Spolu"]["odoslané"] == 4 and rows["Spolu"]["odpovede"] == 2


def test_overview_page_shows_sent_mail_and_explains_no_open_tracking(gui):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    d = DB(cfg.db_path)
    hid = d.people_enabled_hypotheses()[0]["id"]
    link = d.links_for_hypothesis(hid)[0]
    cp = next(c for c in d.contacts_for_person(link["person_id"]) if c["kind"] == "email")
    oid = d.insert_outreach(link["person_id"], hid, "email", "manual", "Otázka", "(text)", [], {}, "approved", cp["id"])
    mark_sent(cfg, d, oid, SENT, detected=True)
    d.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/overview.py").run()
    assert not at.exception, at.exception
    assert any(s.value == "Odoslané e-maily a konverzia" for s in at.subheader)
    assert any("Otvorenie e-mailu sa zámerne nesleduje" in c.value for c in at.caption)
    assert any("Všetky odoslané e-maily (1)" in e.label for e in at.expander)
