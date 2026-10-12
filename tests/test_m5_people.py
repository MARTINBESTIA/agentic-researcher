"""M5 – ľudia, kontakty, súkromie (HTML fixtures, falošný model, bez siete)."""

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PeopleConfig, PrivacyConfig
from radar.contacts import extract_contacts, value_in_snippet
from radar.db import DB
from radar.people import PeopleCollector, consensus_roles, rank_hypothesis, run_people_collect, RoleCategory
from radar.privacy import PrivacyGateError, forget, purge
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router, mock_fetcher

FIX = Path(__file__).parent / "fixtures"
TABLE = (FIX / "contacts_table.html").read_text(encoding="utf-8")
PATTERN = (FIX / "pattern_page.html").read_text(encoding="utf-8")


def setup(tmp_path, lia=True, **people):
    cfg = Config(
        db_path=str(tmp_path / "data" / "r.sqlite"), exports_dir=str(tmp_path / "exports"), models=ModelsConfig(),
        embeddings=EmbeddingsConfig(enabled=False), privacy=PrivacyConfig(lia_completed=lia),
        people=PeopleConfig(search_profiles=False, **people),
    )
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Meškanie dodávok", "Popis", "zákazkoví výrobcovia")
    db.set_people_selection(hid, True)
    rec = Recorder()
    router = make_router(cfg, db, rec)
    return cfg, db, hid, rec, router


def add_org(db, hid, name="Fiktívna Strojáreň s.r.o.", website="https://fiktivna-strojaren.sk/", relation="segment_match"):
    org_id, _ = db.upsert_organization(name, name.lower(), "search", website=website, website_verified=1)
    db.link_org_hypothesis(org_id, hid, relation)
    return org_id


def pattern_proposals(messages):
    """Model navrhne aj e-mail štvrtej osobe podľa vzoru – kód ho musí odmietnuť."""
    text = messages[-1]["content"]
    blocks = {int(m.group(1)): m.group(2) for m in re.finditer(r"\[BLOCK (\d+)\]\n([^\n]+)", text)}
    persons = []
    for idx, block in blocks.items():
        for name, email in (("Anna Prvá", "anna.prva@fiktivna-strojaren.sk"), ("Boris Druhý", "boris.druhy@fiktivna-strojaren.sk"),
                            ("Cyril Tretí", "cyril.treti@fiktivna-strojaren.sk"), ("Dana Štvrtá", "dana.stvrta@fiktivna-strojaren.sk")):
            if name in block:
                role = block.split(",")[1].split("–")[0].strip() if "," in block else ""
                persons.append({"block": idx, "full_name": name, "role": role, "email": email})
    return {"persons": persons}


def test_email_from_mailto_and_text_with_literal_snippet():
    page = extract_contacts(TABLE, "https://www.fiktivne-mesto.sk/kontakty/")
    assert "jana.testovacia@fiktivne-mesto.sk" in page.emails  # z mailto: (viditeľný text je „napíšte“)
    assert "peter.vzorovy@fiktivne-mesto.sk" in page.emails  # z textu
    for email, snippet in page.emails.items():
        assert value_in_snippet("email", email, snippet) and len(snippet) <= 300
    assert "nie@toto.sk" not in page.emails  # skript sa ignoruje


def test_pattern_does_not_create_email_for_fourth_person(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    rec.responses = {"ContactAssoc": pattern_proposals}
    collector = PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1))
    collector.process_page(db.get_hypothesis(hid), db.get_organization(org_id),
                           extract_contacts(PATTERN, "https://fiktivna-strojaren.sk/tim"))
    people = {p["full_name"]: p for p in db.people_of_org(org_id)}
    assert set(people) == {"Anna Prvá", "Boris Druhý", "Cyril Tretí", "Dana Štvrtá"}
    assert [c["value"] for c in db.contacts_for_person(people["Anna Prvá"]["id"])] == ["anna.prva@fiktivna-strojaren.sk"]
    assert db.contacts_for_person(people["Dana Štvrtá"]["id"]) == []  # e-mail podľa vzoru sa nevytvoril
    all_values = [r[0] for r in db.conn.execute("SELECT value FROM contact_points")]
    assert "dana.stvrta@fiktivna-strojaren.sk" not in all_values
    generic = {c["value"]: c for c in db.org_contacts(org_id)}
    assert generic["info@fiktivna-strojaren.sk"]["is_generic"] == 1 and "+421415551234" in generic
    assert people["Anna Prvá"]["role_title"] == "vedúca nákupu" and people["Anna Prvá"]["gender_hint"] == "female"
    assert people["Boris Druhý"]["gender_hint"] is None  # mužský tvar funkcie nie je výslovný údaj


def test_obfuscated_and_cloudflare_become_hidden_contact_never_decoded(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid, "Mestský úrad Fiktívne Mesto", "https://www.fiktivne-mesto.sk/", "mentioned_with_problem")

    def proposals(messages):
        out = []
        for m in re.finditer(r"\[BLOCK (\d+)\]\n([^\n]+)", messages[-1]["content"]):
            for name in ("Zuzana Kartová", "Ondrej Chránený"):
                if name in m.group(2):
                    out.append({"block": int(m.group(1)), "full_name": name, "role": "riaditeľka" if "Zuzana" in name else "konateľ",
                                "email": "zuzana.kartova@fiktivne-mesto.sk" if "Zuzana" in name else ""})
        return {"persons": out}

    rec.responses = {"ContactAssoc": proposals}
    collector = PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1))
    collector.process_page(db.get_hypothesis(hid), db.get_organization(org_id),
                           extract_contacts(TABLE, "https://www.fiktivne-mesto.sk/kontakty/"))
    values = " ".join(r[0] for r in db.conn.execute("SELECT value || ' ' || source_snippet FROM contact_points"))
    assert "zuzana.kartova@" not in values and "(at)" not in values  # nedekódované, ani v úryvku
    zuzana = db.find_person(org_id, "zuzana kartova")
    kinds = [(c["kind"], c["value"]) for c in db.contacts_for_person(zuzana["id"])]
    assert kinds == [("hidden_contact", "https://www.fiktivne-mesto.sk/kontakty/")]
    assert zuzana["gender_hint"] == "female"  # „Pani“ pred menom
    ondrej = db.find_person(org_id, "ondrej chraneny")
    assert [c["kind"] for c in db.contacts_for_person(ondrej["id"])] == ["hidden_contact"]


def test_phone_formats_are_stored_as_e164():
    html = """<html><body><ul>
      <li>Ján Telefónny, vedúci – tel. 0905 123 456</li><li>Iná Osoba, referent – +421 2 5937 1111</li>
      <li>Tretí Človek, technik – <a href="tel:00421415135000">zavolajte</a></li><li>Štvrtý Pán, nákupca – 041/513 5101</li>
    </ul></body></html>"""
    page = extract_contacts(html, "https://x.sk/")
    assert set(page.phones) == {"+421905123456", "+421259371111", "+421415135000", "+421415135101"}


def test_two_people_in_table_get_contacts_by_row_and_outside_contact_is_generic(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid, "Mestský úrad Fiktívne Mesto", "https://www.fiktivne-mesto.sk/")

    def proposals(messages):
        out = []
        for m in re.finditer(r"\[BLOCK (\d+)\]\n([^\n]+)", messages[-1]["content"]):
            block = m.group(2)
            if "Jana Testovacia" in block:
                out.append({"block": int(m.group(1)), "full_name": "Jana Testovacia", "titles": "Ing. PhD.",
                            "role": "vedúca oddelenia investícií", "email": "jana.testovacia@fiktivne-mesto.sk", "phone": "+421 41 513 5101"})
            if "Peter Vzorový" in block:  # model by mu priradil aj podateľňu – tá nie je v jeho riadku
                out.append({"block": int(m.group(1)), "full_name": "Peter Vzorový", "role": "referent verejného obstarávania",
                            "email": "podatelna@fiktivne-mesto.sk", "phone": "041 513 5102"})
        return {"persons": out}

    rec.responses = {"ContactAssoc": proposals}
    collector = PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1))
    collector.process_page(db.get_hypothesis(hid), db.get_organization(org_id),
                           extract_contacts(TABLE, "https://www.fiktivne-mesto.sk/kontakty/"))
    jana = db.find_person(org_id, "jana testovacia")
    peter = db.find_person(org_id, "peter vzorovy")
    assert {(c["kind"], c["value"]) for c in db.contacts_for_person(jana["id"])} == {
        ("email", "jana.testovacia@fiktivne-mesto.sk"), ("phone", "+421415135101")}
    assert {(c["kind"], c["value"]) for c in db.contacts_for_person(peter["id"])} == {("phone", "+421415135102")}
    assert jana["titles"] == "Ing. PhD."
    generic = {c["value"] for c in db.org_contacts(org_id)}
    assert {"podatelna@fiktivne-mesto.sk", "+421415135000"} <= generic  # podateľňa a ústredňa
    # nepriradený osobný e-mail sa k organizácii neukladá (minimalizácia; zmena oproti spec, docs/DECISIONS.md)
    assert "peter.vzorovy@fiktivne-mesto.sk" not in generic and "eva.ukazkova@fiktivne-mesto.sk" not in generic


def test_list_of_people_in_one_block_assigns_contact_to_nearest_name(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    html = """<html><body><div class="kontakty">
      Ing. Anna Prvá, vedúca nákupu, anna.prva@fiktivna-strojaren.sk, 0905 111 222.
      Boris Druhý, plánovač výroby, boris.druhy@fiktivna-strojaren.sk, 0905 333 444.
      Cyril Tretí, projektový manažér.
    </div><p>Ústredňa: 041/555 1234, info@fiktivna-strojaren.sk</p></body></html>"""
    page = extract_contacts(html, "https://fiktivna-strojaren.sk/kontakt")
    big = next(b.index for b in page.blocks if "Anna" in b.text)
    rec.responses = {"ContactAssoc": {"persons": [
        {"block": big, "full_name": "Anna Prvá", "role": "vedúca nákupu", "email": "anna.prva@fiktivna-strojaren.sk", "phone": "0905 111 222"},
        {"block": big, "full_name": "Boris Druhý", "role": "plánovač výroby", "email": "anna.prva@fiktivna-strojaren.sk"},  # model sa pomýlil
        {"block": big, "full_name": "Cyril Tretí", "role": "projektový manažér", "phone": "0905 333 444"},  # patrí Borisovi
    ]}}
    PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1)).process_page(
        db.get_hypothesis(hid), db.get_organization(org_id), page)
    contacts = {p["full_name"]: {c["value"] for c in db.contacts_for_person(p["id"])} for p in db.people_of_org(org_id)}
    assert contacts["Anna Prvá"] == {"+421905111222"}  # e-mail navrhnutý dvom osobám sa nepriradí nikomu
    assert contacts["Boris Druhý"] == set() and contacts["Cyril Tretí"] == set()  # telefón je bližšie Borisovi
    assert {c["value"] for c in db.org_contacts(org_id)} == {"+421415551234", "info@fiktivna-strojaren.sk"}
    assert "Ústredňa" not in db.find_person(org_id, "anna prva")["source_snippet"]  # úryvok iba okolo osoby


def test_fetcher_refuses_linkedin_and_redirect_to_it():
    routes = {
        "https://www.linkedin.com/in/niekto": httpx.Response(200, text="<html>profil</html>"),
        "https://kratky.sk/x": httpx.Response(302, headers={"location": "https://sk.linkedin.com/in/niekto"}),
        "https://ok.sk/": httpx.Response(200, text="<html><body>ok</body></html>", headers={"content-type": "text/html"}),
    }
    fetcher = mock_fetcher(routes)
    assert fetcher.fetch_html("https://www.linkedin.com/in/niekto").status == "blocked_domain"
    assert fetcher.fetch_html("https://kratky.sk/x").status == "blocked_domain"
    assert fetcher.fetch("https://kratky.sk/x") is None
    assert fetcher.fetch_html("https://ok.sk/").status == "ok"


def test_people_without_lia_fails_with_clear_error(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path, lia=False)
    with pytest.raises(PrivacyGateError, match="lia_completed"):
        run_people_collect(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), [hid])


def test_forget_email_deletes_and_suppresses_on_next_run(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    rec.responses = {"ContactAssoc": pattern_proposals}
    page = extract_contacts(PATTERN, "https://fiktivna-strojaren.sk/tim")
    collector = PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1))
    collector.process_page(db.get_hypothesis(hid), db.get_organization(org_id), page)
    anna = db.find_person(org_id, "anna prva")
    assert anna is not None
    result = forget(cfg, db, email="Anna.Prva@fiktivna-strojaren.sk")
    assert result["people_removed"] == 1
    assert db.find_person(org_id, "anna prva") is None
    assert db.conn.execute("SELECT COUNT(*) FROM contact_points WHERE value = 'anna.prva@fiktivna-strojaren.sk'").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] >= 2
    # nový beh nad rovnakou stránkou osobu znova neuloží
    PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1)).process_page(
        db.get_hypothesis(hid), db.get_organization(org_id), page)
    assert db.find_person(org_id, "anna prva") is None
    assert db.find_person(org_id, "boris druhy") is not None
    assert "anna.prva@fiktivna-strojaren.sk" not in {c["value"] for c in db.org_contacts(org_id)}


def test_purge_with_shifted_time(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    old = db.insert_person(org_id, "Starý Kandidát", "stary kandidat", "https://x.sk", "Starý Kandidát", hid)
    declined = db.insert_person(org_id, "Odmietol Ma", "odmietol ma", "https://x.sk", "Odmietol Ma", hid,
                                status="declined", purge_after="2026-11-01T00:00:00+00:00")
    fresh_declined = db.insert_person(org_id, "Nedávno Odmietol", "nedavno odmietol", "https://x.sk", "x", hid,
                                      status="declined", purge_after="2027-06-01T00:00:00+00:00")
    exports = Path(cfg.exports_dir)
    exports.mkdir(parents=True)
    old_file, new_file = exports / "stary.csv", exports / "novy.csv"
    old_file.write_text("x")
    new_file.write_text("x")
    past = time.time() - 40 * 86400
    os.utime(old_file, (past, past))
    stats = purge(cfg, db)  # exporty: starší ako 30 dní sa zmaže, nový ostane
    assert not old_file.exists() and new_file.exists() and stats["exports"] == 1
    now = datetime.now(timezone.utc) + timedelta(days=91)
    # nový kandidát pred 90 dňami ešte nezmizne
    stats = purge(cfg, db, now=datetime.now(timezone.utc) + timedelta(days=30))
    assert db.get_person(old) is not None and stats["uncontacted"] == 0
    stats = purge(cfg, db, now=now)
    assert db.get_person(old) is None and stats["uncontacted"] >= 1
    assert db.get_person(declined) is None and db.get_person(fresh_declined) is not None


def test_limits_15_per_hypothesis_2_per_org_and_priority_components(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["nákupca", "vedúci nákupu"], "rationale": "r"}])
    for i in range(10):
        org_id = add_org(db, hid, f"Firma {i} s.r.o.", f"https://firma{i}.sk/")
        for j in range(3):
            pid = db.insert_person(org_id, f"Osoba{i} Meno{j}", f"osoba{i} meno{j}", f"https://firma{i}.sk/kontakt", "x", hid,
                                   role_title="vedúci nákupu" if j == 0 else "nákupca")
            db.add_contact_point(pid, org_id, "email", f"o{i}{j}@firma{i}.sk", f"https://firma{i}.sk/kontakt",
                                 f"o{i}{j}@firma{i}.sk", "regex")
    from collections import Counter

    stats = rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    links = db.links_for_hypothesis(hid)
    people = {l["person_id"] for l in links}
    assert len(people) == 15 and stats["selected"] == 15
    per_org = Counter(db.get_person(p)["org_id"] for p in people)
    assert max(per_org.values()) <= 2 and len(per_org) == 10  # prednosť majú rôzne organizácie
    top = links[0]
    import json

    comp = json.loads(top["priority_json"])
    assert {"role_fit", "väzba", "kanál", "zhoda_org", "váhy"} <= set(comp)
    assert db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 15  # nevybraní sa vymazali


def test_consensus_roles_majority():
    a = [RoleCategory(category="vedúci nákupu", examples=["nákupca"]), RoleCategory(category="plánovač výroby")]
    b = [RoleCategory(category="Vedúci nákupu"), RoleCategory(category="plánovač výroby"), RoleCategory(category="konateľ")]
    c = [RoleCategory(category="vedúci nákupu"), RoleCategory(category="IT manažér")]
    roles, low = consensus_roles([a, b, c])
    assert [r["category"] for r in roles] == ["vedúci nákupu", "plánovač výroby"] and low is False
    roles, low = consensus_roles([a, [RoleCategory(category="úplne iné")], [RoleCategory(category="ešte iné")]])
    assert low is True


def test_no_smtp_or_social_clients_in_code():
    root = Path(__file__).parent.parent / "radar"
    code = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    for forbidden in ("smtplib", "import smtp", "linkedin_api", "tweepy", "facebook_sdk", "selenium", "playwright"):
        assert forbidden not in code


def test_rpo_address_never_stored(tmp_path):
    """AK12: adresa z RPO statutoryBodies sa nikde neuloží (cache ani osoby)."""
    import json as _json

    from radar.registers import RPOClient

    entity = _json.loads((FIX / "rpo_entity.json").read_text(encoding="utf-8"))

    def handler(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"id": 555001}]})
        return httpx.Response(200, json=entity)

    cfg, db, hid, rec, router = setup(tmp_path)
    rpo = RPOClient(db, delay_s=0, http=httpx.Client(transport=httpx.MockTransport(handler)))
    org_id, _ = db.upsert_organization("Fiktívna Strojáreň s.r.o.", "fiktivna strojaren", "rpo", ico="12345678")
    db.link_org_hypothesis(org_id, hid, "segment_match")
    collector = PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), rpo=rpo)
    collector.statutory_people(db.get_hypothesis(hid), db.get_organization(org_id))
    person = db.find_person(org_id, "jan testovaci")
    assert person["role_title"] == "konateľ" and "RPO" in person["source_snippet"]
    dump = "\n".join(str(tuple(r)) for t in ("people", "contact_points", "register_cache", "organizations")
                     for r in db.conn.execute(f"SELECT * FROM {t}"))
    assert "Súkromná ulica" not in dump and "01004" not in dump and "Tajná" not in dump


def test_small_org_management_counts_as_related_role(tmp_path):
    from collections import Counter

    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["nákupca"], "rationale": "r"}])
    small = add_org(db, hid, "Malá Firma s.r.o.", "https://mala.sk/")
    big = add_org(db, hid, "Veľká Firma a.s.", "https://velka.sk/")
    db.update_organization(small, size_category="micro")
    db.update_organization(big, size_category="large")
    for org_id, name in ((small, "Peter Malý"), (big, "Pavol Veľký")):
        db.insert_person(org_id, name, name.lower(), "https://x.sk", f"{name}, konateľ", hid, role_title="konateľ", source_kind="rpo")
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    names = {l["full_name"]: l for l in db.links_for_hypothesis(hid)}
    assert set(names) == {"Peter Malý"}  # konateľ veľkej firmy bez väzby na proces nie je vybraný
    import json

    assert json.loads(names["Peter Malý"]["priority_json"])["kategória"] == "vedenie malej organizácie"


def test_rerank_removes_stale_link_types(tmp_path):
    from collections import Counter

    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_role_map(hid, [{"category": "hlavný účtovník", "examples": ["hlavná účtovníčka"], "rationale": "r"}])
    org_id = add_org(db, hid, "Účtovná Kancelária s.r.o.", "https://uk.sk/")
    pid = db.insert_person(org_id, "Ingrid Testová", "ingrid testova", "https://uk.sk/tim", "x", hid,
                           role_title="Hlavný účtovník, Daňový poradca")
    db.upsert_link(pid, hid, "expert")  # z predošlého poradia (poradca bol vtedy expert)
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    assert [l["link_type"] for l in db.links_for_person(pid)] == ["role_owner"]
