"""M23 – fronta kontaktov (audit 15. 10. 2026, balík A): stav komunikácie naprieč hypotézami, ďalšia akcia, fronta
prvého oslovenia bez oslovených a odmietnutých, deduplikácia spoločných e-mailov a organizácií. Offline."""

import sqlite3
from collections import Counter
from datetime import date, timedelta

from radar.contact_scout import contactable
from radar.db import DB
from radar.outreach import set_status
from radar.people import rank_hypothesis
from radar.shortlist import contact_shortlist, shortlist_summary
from tests.test_m18_contacts import setup


def _world(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_people_selection(hid, True)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["vedúci nákupu"], "rationale": "r"}])
    people = {}
    for i, org_name in enumerate(("Alfa", "Beta", "Gama")):
        org, _ = db.upsert_organization(f"{org_name} s.r.o.", org_name.lower(), "search", website=f"https://{org_name.lower()}.sk/",
                                        website_verified=1)
        db.link_org_hypothesis(org, hid, "mentioned_with_problem")
        pid = db.insert_person(org, f"Osoba {org_name}", f"osoba {org_name.lower()}", f"https://{org_name.lower()}.sk/k", "x",
                               hid, role_title="vedúci nákupu", source_kind="website")
        db.add_contact_point(pid, org, "email", f"osoba@{org_name.lower()}.sk", f"https://{org_name.lower()}.sk/k",
                             f"Osoba {org_name} osoba@{org_name.lower()}.sk", "regex")
        people[org_name] = (pid, org)
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    return cfg, db, hid, rec, router, people


def _outreach(db, pid, hid, status, sent_days_ago=None, contact=None):
    oid = db.insert_outreach(pid, hid, "email", "B", "Predmet", "Text", [], {"ok": True}, "draft", contact)
    fields = {"status": status}
    if sent_days_ago is not None:
        fields["sent_at"] = (date.today() - timedelta(days=sent_days_ago)).isoformat()
    db.update_outreach(oid, **fields)
    return oid


def test_states_from_audit_probe_and_first_contact_queue(tmp_path):
    """Sonda z auditu: kontakt v stave new/contacted/replied/interviewed bol stále „oslov najprv“."""
    cfg, db, hid, rec, router, people = _world(tmp_path)
    by = lambda: {r["osoba"]: r for r in contact_shortlist(cfg, db, hid)}  # noqa: E731
    row = by()["Osoba Alfa"]
    assert (row["odporúčanie"], row["stav_komunikácie"], row["prvé_oslovenie"]) == ("oslov najprv", "nové", True)
    pid, _ = people["Alfa"]
    oid = _outreach(db, pid, hid, "sent", sent_days_ago=2)
    row = by()["Osoba Alfa"]
    assert row["stav_komunikácie"] == "čaká na odpoveď" and not row["prvé_oslovenie"]
    for status, stage in (("replied", "nadviazanie"), ("interviewed", "nadviazanie"), ("declined", "uzavreté")):
        set_status(cfg, db, oid, status)
        row = by()["Osoba Alfa"]
        assert row["stav_komunikácie"] == stage and not row["prvé_oslovenie"], status
    summary = shortlist_summary(contact_shortlist(cfg, db, hid))
    assert summary["fronta_prvého_oslovenia"] == 2 and summary["stav"]["uzavreté"] == 1  # odmietnutý mimo frontu
    assert contactable(db, hid) == 2  # odmietnutý sa nepočíta do cieľa


def test_contact_from_other_hypothesis_and_org_cooldown(tmp_path):
    cfg, db, hid, rec, router, people = _world(tmp_path)
    other = db.create_hypothesis("Iná hypotéza", "", "x")
    pid, org = people["Beta"]
    _outreach(db, pid, other, "sent", sent_days_ago=3)  # tá istá osoba oslovená v inej hypotéze
    row = {r["osoba"]: r for r in contact_shortlist(cfg, db, hid)}["Osoba Beta"]
    assert row["stav_komunikácie"] == "čaká na odpoveď" and f"H{other}" in row["ďalšia_akcia"]
    # iná osoba tej istej organizácie: organizácia oslovená pred 3 dňami → čaká
    p2 = db.insert_person(org, "Druhá Beta", "druha beta", "https://beta.sk/k", "x", hid, role_title="vedúci nákupu",
                          source_kind="website")
    db.add_contact_point(p2, org, "email", "druha@beta.sk", "https://beta.sk/k", "Druhá Beta druha@beta.sk", "regex")
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    row = {r["osoba"]: r for r in contact_shortlist(cfg, db, hid)}["Druhá Beta"]
    assert not row["prvé_oslovenie"] and "organizácia oslovená" in row["ďalšia_akcia"]


def test_shared_mailbox_and_one_contact_per_organization_in_queue(tmp_path):
    cfg, db, hid, rec, router, people = _world(tmp_path)
    _, org = people["Gama"]
    for name in ("Prvá Gama", "Druhá Gama"):
        p = db.insert_person(org, name, name.lower(), "https://gama.sk/k", "x", hid, role_title="vedúci nákupu",
                             source_kind="website")
        db.add_contact_point(p, org, "email", "nakup@gama.sk", "https://gama.sk/k", f"{name} nakup@gama.sk", "regex")
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    rows = [r for r in contact_shortlist(cfg, db, hid) if r["organizácia"] == "Gama s.r.o."]
    assert sum(r["prvé_oslovenie"] for r in rows) == 1  # jedna osoba z organizácie naraz
    assert any("jedno prvé oslovenie naraz" in r["ďalšia_akcia"] or "spoločný e-mail" in r["ďalšia_akcia"] for r in rows)
    summary = shortlist_summary(contact_shortlist(cfg, db, hid))
    assert summary["fronta_organizácie"] == summary["fronta_prvého_oslovenia"] == 3


def test_migration_v13_keeps_outreach_and_allows_organization_contact(tmp_path):
    cfg, db, hid, rec, router, people = _world(tmp_path)
    pid, org = people["Alfa"]
    oid = _outreach(db, pid, hid, "sent", sent_days_ago=1)
    row = db.get_outreach(oid)
    assert row["org_id"] == org and row["mode"] == "discovery" and row["message_version"] == 1
    db.conn.execute("INSERT INTO outreach (person_id, org_id, hypothesis_id, channel, variant, body, facts_json, status, "
                    "created_at, updated_at) VALUES (NULL, ?, ?, 'email', 'B', 'x', '[]', 'draft', 'now', 'now')", (org, hid))
    db.close()
    raw = sqlite3.connect(cfg.db_path)
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.close()
    assert DB(cfg.db_path).migration["to"] == 13
