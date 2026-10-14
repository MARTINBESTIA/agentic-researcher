"""M18 – hľadanie kontaktov podľa hypotézy, držitelia dát, ručné pridanie a zoznam „Koho osloviť“ (bez siete)."""

import json
from collections import Counter

import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PeopleConfig, PrivacyConfig
from radar.contact_scout import ContactScout, add_contact_from_url, contactable, search_due
from radar.db import DB
from radar.orgs import auto_select_validated, select_people_hypotheses
from radar.people import PeopleCollector, rank_hypothesis
from radar.privacy import PrivacyGateError
from radar.registers import OrgInfo
from radar.search import SearchResult
from radar.shortlist import contact_shortlist
from radar.util import Deadline
from radar.validate import run_validate_finish
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router

DATASET = "https://data.gov.sk/set/0123456789abcdef0123"


class FakeRPO:
    def __init__(self, orgs: dict[str, OrgInfo] | None = None):
        self.orgs = orgs or {}

    def lookup_ico(self, ico):
        return self.orgs.get(ico)

    def search_name(self, name):
        return []

    def entity(self, entity_id):
        return None

    def entity_url(self, entity_id):
        return f"https://api.statistics.sk/rpo/v1/entity/{entity_id}"


class FakeCatalog:
    failed = False

    def __init__(self, points):
        self.points, self.calls = points, []

    def contact_points(self, uri):
        self.calls.append(uri)
        return self.points.get(uri, [])


def setup(tmp_path, lia=True, **people):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=lia), people=PeopleConfig(search_profiles=False, **people))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Meškanie dodávok v zákazkovej výrobe", "Dodávky meškajú.", "zákazkoví výrobcovia")
    rec = Recorder()
    return cfg, db, hid, rec, make_router(cfg, db, rec)


def page(body: str, title: str = "Firma") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


PLAN = {"targets": [
    {"kind": "customer", "who": "vedúci nákupu v zákazkovej výrobe", "roles": ["vedúci nákupu", "nákupca"],
     "queries": ["zákazková výroba Žilina kontakt", "info@x.sk telefón 0905 123 456"],
     "ask": "Rozhovor o poslednom meškaní dodávky.", "why": "Má problém."},
    {"kind": "expert", "who": "kontrolný úrad", "roles": ["hovorca"], "queries": ["kontrolný úrad zistenia dodávky"],
     "ask": "Posúdenie a údaje o kontrolách.", "why": "Kontroluje."},
]}


def scout_env(tmp_path, plan=None, **people):
    cfg, db, hid, rec, router = setup(tmp_path, **people)
    db.set_people_selection(hid, True)
    rec.responses = {"ContactPlan": plan or PLAN}
    searx = FakeSearx({
        "zákazková výroba": [SearchResult(url="https://vyrobca.sk/o-nas", title="Výrobca s.r.o. – o nás", snippet=""),
                             SearchResult(url="https://firmy.sk/zoznam", title="Zoznam firiem", snippet="")],
        "kontrolný úrad": [SearchResult(url="https://www.nku.gov.sk/kontakt", title="NKÚ – kontakt", snippet="")],
    })
    fetcher = FakeFetcher(htmls={
        "https://vyrobca.sk/": page("<p>Výrobca s.r.o., IČO: 11 222 333</p><a href='/kontakt'>Kontakt</a>"),
        "https://www.nku.gov.sk/": page("<p>Najvyšší kontrolný úrad Slovenskej republiky, IČO 00 000 001</p>"),
    })
    rpo = FakeRPO({"11222333": OrgInfo(name="Výrobca s.r.o.", ico="11222333", sk_nace="28290"),
                   "00000001": OrgInfo(name="Najvyšší kontrolný úrad Slovenskej republiky", ico="00000001")})
    scout = ContactScout(cfg, db, router, searx, fetcher, Deadline(5), rpo, None, None)
    return cfg, db, hid, rec, scout, searx


def test_plan_queries_are_cleaned_and_orgs_verified_by_ico(tmp_path):
    cfg, db, hid, rec, scout, searx = scout_env(tmp_path)
    scout.run_hypothesis(db.get_hypothesis(hid))
    targets = db.contact_targets_for(hid)
    assert [t["kind"] for t in targets] == ["customer", "expert"]
    queries = [q["q"] for t in targets for q in json.loads(t["queries_json"])]
    assert "info@x.sk telefón 0905 123 456" not in queries  # dopyt s e-mailom alebo telefónom sa zahodí
    assert all(q["done_at"] for t in targets for q in json.loads(t["queries_json"]))
    orgs = {o["name"]: o for o in db.orgs_for_hypothesis(hid)}
    assert set(orgs) == {"Výrobca s.r.o.", "Najvyšší kontrolný úrad Slovenskej republiky"}  # firmy.sk je katalóg
    assert orgs["Výrobca s.r.o."]["relation"] == "contact_search" and orgs["Výrobca s.r.o."]["website_verified"] == 1
    assert orgs["Výrobca s.r.o."]["evidence_url"] == "https://vyrobca.sk/o-nas"  # stránka výsledku pre krok people
    # nku.gov.sk je v aggregator_domains, no pri cieli „expert“ je webom organizácie (people.expert_org_domains)
    nku = orgs["Najvyšší kontrolný úrad Slovenskej republiky"]
    assert nku["relation"] == "expert_org" and nku["target_id"] == targets[1]["id"]
    assert db.get_hypothesis(hid)["contacts_searched_at"] is not None
    plan_calls = db.judgments_for("contact_plan", "hypothesis", hid)
    assert len(plan_calls) == 1 and plan_calls[0]["role"] == "fast"  # provenance: úloha, rola, verzia promptu


def test_customer_with_segment_mismatch_is_not_linked(tmp_path):
    cfg, db, hid, rec, scout, searx = scout_env(tmp_path)
    db.set_hypothesis_fields(hid, segment_filter_json=json.dumps({"sk_nace_prefixes": ["62"], "sector_kind": "any"}))
    scout.run_hypothesis(db.get_hypothesis(hid))
    names = {o["name"] for o in db.orgs_for_hypothesis(hid)}
    assert "Výrobca s.r.o." not in names and scout.stats["segment_mismatch"] == 1


def test_next_round_only_after_people_step_and_with_feedback(tmp_path):
    second = {"targets": [{"kind": "customer", "who": "plánovač výroby", "roles": ["plánovač"],
                           "queries": ["strojárska výroba Trnava kontakty"], "ask": "", "why": ""}]}
    cfg, db, hid, rec, scout, searx = scout_env(tmp_path, contact_goal_per_hypothesis=5)
    inputs = []

    def plan(messages):
        inputs.append(messages[-1]["content"])
        return PLAN if len(inputs) == 1 else second

    rec.responses = {"ContactPlan": plan}
    hyp = db.get_hypothesis(hid)
    scout.run_hypothesis(hyp)
    assert search_due(cfg, db, hid) is None  # organizácie ešte čakajú na krok people
    for org in db.orgs_for_hypothesis(hid):
        db.set_org_hypothesis(org["id"], hid, status="people_done")
    assert search_due(cfg, db, hid) == "málo kontaktov – ďalšie kolo dopytov"
    scout.run_hypothesis(db.get_hypothesis(hid))
    targets = db.contact_targets_for(hid)
    assert targets[-1]["round"] == 2 and targets[-1]["ask"] == ""  # prázdne „čo žiadať“ doplní zoznam predvolene
    assert len(inputs) == 2 and "ALREADY TRIED: zákazková výroba Žilina kontakt" in inputs[1]
    assert "FOUND ORGANIZATIONS: customer 1, data_holder 0, expert 1" in inputs[1]
    assert "strojárska výroba Trnava kontakty" in searx.queries
    assert "<untrusted_document>" in inputs[0]  # dôkazy sú nedôveryhodný obsah


def test_data_holder_from_gate1_source_and_catalog_contact_point(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_people_selection(hid, True)
    field = db.upsert_field(hid, "termíny dodávok", "pôvodný a skutočný termín", True, "r")
    db.add_source(field, hid, "Register objednávok", DATASET, "catalog", publisher="Úrad pre objednávky")
    catalog = FakeCatalog({DATASET: [
        {"fn": "Ing. Jana Vzorová", "email": "jana.vzorova@urad.example.sk", "title": "Register objednávok",
         "publisher": "Úrad pre objednávky", "modified": "2023-05-02"},
        {"fn": "Oddelenie otvorených dát", "email": "opendata@urad.example.sk", "title": "Register objednávok",
         "publisher": "Úrad pre objednávky", "modified": "2023-05-02"},
    ]})
    rec.responses = {"ContactPlan": {"targets": []}}
    scout = ContactScout(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(5), FakeRPO(), None, catalog)
    scout.run_hypothesis(db.get_hypothesis(hid))
    org = db.orgs_for_hypothesis(hid)[0]
    assert org["name"] == "Úrad pre objednávky" and org["relation"] == "data_holder"
    person = db.find_person(org["id"], "jana vzorova")
    assert person["source_kind"] == "catalog" and person["titles"] == "Ing."
    email = db.contacts_for_person(person["id"])[0]
    assert email["value"] == "jana.vzorova@urad.example.sk" and email["value"] in email["source_snippet"]
    assert email["source_url"] == DATASET and json.loads(email["checks_json"])["metadáta_zmenené"] == "2023-05-02"
    generic = [c["value"] for c in db.org_contacts(org["id"])]
    assert generic == ["opendata@urad.example.sk"]  # kontakt bez mena osoby patrí organizácii
    # druhý beh použije cache katalógu (nevolá ho znova)
    scout.data_holders(db.get_hypothesis(hid))
    assert catalog.calls == [DATASET]
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": [], "rationale": "r"}])
    stats = rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    assert stats["data_holders"] == 1
    link = db.links_for_hypothesis(hid)[0]
    assert link["link_type"] == "data_holder" and json.loads(link["priority_json"])["skupina"] == "data_holder"
    rows = contact_shortlist(cfg, db, hid)
    assert rows[0]["odporúčanie"] == "oslov najprv" and rows[0]["typ"] == "držiteľ dát"
    assert rows[0]["e-mail"] == "jana.vzorova@urad.example.sk"


def test_rank_keeps_groups_separate_and_manual_person(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path, max_people_per_hypothesis=2, max_data_holders_per_hypothesis=1)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["nákupca"], "rationale": "r"}])
    customers = []
    for i in range(3):
        org_id, _ = db.upsert_organization(f"Firma {i}", f"firma {i}", "search", website=f"https://f{i}.sk/", website_verified=1)
        db.link_org_hypothesis(org_id, hid, "contact_search")
        pid = db.insert_person(org_id, f"Osoba{i} Nákupná", f"osoba{i} nakupna", f"https://f{i}.sk/k", "x", hid,
                               role_title="vedúci nákupu")
        customers.append(pid)
    holder, _ = db.upsert_organization("Úrad", "urad", "gate1_source", website="https://urad.sk/", website_verified=1)
    db.link_org_hypothesis(holder, hid, "data_holder", None, "https://urad.sk/data")
    analyst = db.insert_person(holder, "Eva Dátová", "eva datova", "https://urad.sk/k", "x", hid, role_title="analytička dát")
    clerk = db.insert_person(holder, "Peter Podateľňa", "peter podatelna", "https://urad.sk/k", "x", hid, role_title="referent")
    manual = db.insert_person(holder, "Mária Ručná", "maria rucna", "https://urad.sk/t", "x", hid, role_title="riaditeľka",
                              source_kind="manual")
    db.upsert_link(manual, hid, "expert", None, "https://urad.sk/t", rationale="Pridané ručne. Čo žiadať: posúdenie")
    stats = rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    linked = {l["person_id"]: l for l in db.links_for_hypothesis(hid)}
    assert sum(1 for p in customers if p in linked) == 2 and stats["selected"] == 2  # limit zákazníkov
    assert linked[analyst]["link_type"] == "data_holder" and clerk not in linked  # referent nepracuje s dátami
    assert manual in linked and stats["manual_kept"] == 1  # ručne pridaná ostáva mimo limitov
    assert db.get_person(clerk) is None  # nevybraný kandidát sa vymazal (minimalizácia)
    assert db.get_person(manual) is not None


def test_add_contact_from_url_verifies_name_and_contact_on_page(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    url = "https://firma.example.sk/tim"
    html = page("<p>Firma Príklad s.r.o., IČO: 44 555 666</p><ul>"
                "<li>Veronika Príkladná, manažérka účtovného tímu, veronika.prikladna@firma.example.sk</li>"
                "<li>Iný Človek, technik, iny.clovek@firma.example.sk</li></ul><p>Napíšte nám: info@firma.example.sk</p>")
    fetcher = FakeFetcher(htmls={url: html})
    rpo = FakeRPO({"44555666": OrgInfo(name="Firma Príklad s.r.o.", ico="44555666")})
    out = add_contact_from_url(cfg, db, fetcher, hid, url, "Veronika Príkladná", "customer", "manažérka účtovného tímu",
                               ask="100 anonymizovaných problematických dokladov", rpo=rpo)
    assert out["organizácia"] == "Firma Príklad s.r.o." and out["kontakty"] == ["veronika.prikladna@firma.example.sk"]
    org = db.orgs_for_hypothesis(hid)[0]
    person = db.find_person(org["id"], "veronika prikladna")
    assert person["source_kind"] == "manual" and person["role_title"] == "manažérka účtovného tímu"
    values = [r[0] for r in db.conn.execute("SELECT value FROM contact_points")]
    assert "iny.clovek@firma.example.sk" not in values  # e-mail iného človeka sa neuloží
    assert "info@firma.example.sk" in values  # generický kontakt k organizácii
    link = db.links_for_person(person["id"])[0]
    assert link["link_type"] == "role_owner" and "100 anonymizovaných" in link["rationale"]
    with pytest.raises(ValueError, match="Meno sa na stránke nenašlo"):
        add_contact_from_url(cfg, db, fetcher, hid, url, "Neexistujúca Osoba", rpo=rpo)
    with pytest.raises(ValueError, match="LinkedIn"):
        add_contact_from_url(cfg, db, fetcher, hid, "https://www.linkedin.com/in/niekto", "Veronika Príkladná", rpo=rpo)
    cfg2 = cfg.model_copy(update={"privacy": PrivacyConfig(lia_completed=False)})
    with pytest.raises(PrivacyGateError):
        add_contact_from_url(cfg2, db, fetcher, hid, url, "Veronika Príkladná", rpo=rpo)
    # po poradí ostane (ručne pridaná) a v zozname má „čo žiadať“ zo zadania
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": [], "rationale": "r"}])
    rec.responses = {"RoleFit": {"fit": "related", "category": "účtovníctvo", "rationale": "r"}}
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    rows = contact_shortlist(cfg, db, hid)
    row = next(r for r in rows if r["osoba"] == "Veronika Príkladná")
    assert row["čo_žiadať"] == "100 anonymizovaných problematických dokladov" and row["odporúčanie"] == "oslov najprv"


def test_shortlist_tiers_reasons_and_org_rows(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_role_map(hid, [{"category": "vedúci nákupu", "examples": ["nákupca"], "rationale": "r"}])
    a, _ = db.upsert_organization("Alfa s.r.o.", "alfa", "search", website="https://alfa.sk/", website_verified=1)
    db.link_org_hypothesis(a, hid, "mentioned_with_problem")
    p1 = db.insert_person(a, "Anna Prvá", "anna prva", "https://alfa.sk/k", "x", hid, role_title="vedúci nákupu")
    db.add_contact_point(p1, a, "email", "anna.prva@alfa.sk", "https://alfa.sk/k", "Anna Prvá anna.prva@alfa.sk", "regex")
    p2 = db.insert_person(a, "Boris Druhý", "boris druhy", "https://alfa.sk/k", "x", hid, role_title="nákupca")
    db.add_contact_point(p2, a, "phone", "+421905123456", "https://alfa.sk/k", "Boris +421905123456", "phonenumbers")
    b, _ = db.upsert_organization("Beta", "beta", "search", website="https://beta.sk/", website_verified=1)
    db.link_org_hypothesis(b, hid, "procured_related")
    db.set_org_hypothesis(b, hid, procurement_active=1)
    p3 = db.insert_person(b, "Cyril Tretí", "cyril treti", "https://beta.sk/k", "x", hid, role_title="vedúci nákupu")
    db.add_contact_point(p3, b, "email", "cyril@beta.sk", "https://beta.sk/k", "cyril@beta.sk", "regex")
    c, _ = db.upsert_organization("Zväz výrobcov", "zvaz vyrobcov", "contact_search", website="https://zvaz.sk/",
                                  website_verified=1)
    target = db.add_contact_target(hid, "expert", "zväz výrobcov", ["riaditeľ"], ["zväz"], "Posúdenie problému.", "r")
    db.link_org_hypothesis(c, hid, "expert_org", None, "https://zvaz.sk/", target_id=target)
    db.add_contact_point(None, c, "email", "info@zvaz.sk", "https://zvaz.sk/kontakt", "info@zvaz.sk", "regex", True)
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    rows = contact_shortlist(cfg, db, hid)
    by = {r["osoba"] or r["organizácia"]: r for r in rows}
    assert by["Anna Prvá"]["odporúčanie"] == "oslov najprv" and "e-mail osobný" in by["Anna Prvá"]["dôvody"]
    assert by["Boris Druhý"]["odporúčanie"] == "doplň kontakt" and "iba telefón" in by["Boris Druhý"]["dôvody"]
    assert by["Cyril Tretí"]["odporúčanie"] == "neoslovovať" and "obstarávanie" in by["Cyril Tretí"]["dôvody"]
    zvaz = by["Zväz výrobcov"]
    assert zvaz["typ"] == "expert" and zvaz["e-mail"] == "info@zvaz.sk" and "riaditeľ" in zvaz["čo_žiadať"]
    order = [r["odporúčanie"] for r in rows]
    assert order == sorted(order, key=["oslov najprv", "oslov", "doplň kontakt", "neoslovovať"].index)
    assert contactable(db, hid) >= 2


def test_validation_pass_selects_hypothesis_for_gate0_unless_manually_off(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    other = db.create_hypothesis("Iná hypotéza", "", "x")
    for h in (hid, other):
        for i, domain in enumerate(("a.sk", "b.sk")):
            doc, _ = db.upsert_document(f"https://{domain}/{h}", "T", "search", "t", "q", f"h{h}{i}", "Text. " * 20)
            cid = db.insert_claim(doc, 0, "t", "Dodávky meškajú.", "výrobcovia", "pain", "Text. Text.")
            db.set_claim_verification(cid, "verified", 100, "ok")
            db.assign_claim(h, cid)
        db.set_validation_searched(h, {"agent": {"ok": True}})
    db.set_people_selection(other, False)  # ručne vypnutá
    stats = run_validate_finish(cfg, db, Deadline(1))
    assert stats["verdict:podložená"] == 2 and stats["gate0_selected"] == 1
    assert db.get_hypothesis(hid)["people_enabled"] == 1 and db.get_hypothesis(hid)["people_manual"] is None
    assert db.get_hypothesis(other)["people_enabled"] == 0  # ručná voľba má prednosť
    # automatický výber top N ju nevypne
    assert hid in select_people_hypotheses(cfg.model_copy(update={"people": PeopleConfig(auto_select_top=0)}), db)
    assert auto_select_validated(cfg, db) == []  # nič nové


def test_website_people_visits_result_page_first(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id, _ = db.upsert_organization("Výrobca s.r.o.", "vyrobca", "contact_search", website="https://vyrobca.sk/",
                                       website_verified=1)
    db.link_org_hypothesis(org_id, hid, "contact_search", None, "https://vyrobca.sk/tim/nakup")
    fetcher = FakeFetcher(htmls={"https://vyrobca.sk/": page("<p>Úvod</p>"),
                                 "https://vyrobca.sk/tim/nakup": page("<ul><li>Jana Nákupná, vedúca nákupu, "
                                                                      "jana.nakupna@vyrobca.sk</li></ul>")})
    rec.responses = {"ContactAssoc": {"persons": [{"block": 0, "full_name": "Jana Nákupná", "role": "vedúca nákupu",
                                                   "email": "jana.nakupna@vyrobca.sk"}]}}
    collector = PeopleCollector(cfg, db, router, FakeSearx(), fetcher, Deadline(1))
    org = db.orgs_for_hypothesis(hid)[0]
    collector.website_people(db.get_hypothesis(hid), org)
    assert "https://vyrobca.sk/tim/nakup" in fetcher.fetched
    person = db.find_person(org_id, "jana nakupna")
    assert [c["value"] for c in db.contacts_for_person(person["id"])] == ["jana.nakupna@vyrobca.sk"]


def test_contact_search_needs_lia(tmp_path):
    from radar.contact_scout import run_contact_search

    cfg, db, hid, rec, router = setup(tmp_path, lia=False)
    with pytest.raises(PrivacyGateError):
        run_contact_search(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), [hid], FakeRPO(), None, FakeCatalog({}))


def test_data_role_fit_uses_whole_words():
    from radar.people import data_role_fit

    assert data_role_fit("analytička dát", []) == 1.0 and data_role_fit("odbor údajov a štatistiky", []) == 1.0
    assert data_role_fit("referentka podateľne", []) == 0.0  # „podateľňa“ nie je práca s dátami
    assert data_role_fit("vedúci odboru financií", ["vedúci odboru financií"]) == 1.0  # funkcia z cieľa plánu


def test_drafts_skip_data_holders(tmp_path):
    from radar.outreach import run_drafts

    cfg, db, hid, rec, router = setup(tmp_path)
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Úrad", "urad", "gate1_source")
    pid = db.insert_person(org, "Eva Dátová", "eva datova", "https://urad.sk/k", "x", hid, role_title="analytička dát")
    db.add_contact_point(pid, org, "email", "eva@urad.sk", "https://urad.sk/k", "eva@urad.sk", "regex")
    link = db.upsert_link(pid, hid, "data_holder", None, "https://urad.sk/data")
    db.set_link_priority(link, 0.8, {"skupina": "data_holder"})
    assert run_drafts(cfg, db, router, Deadline(1), [hid]) == {}  # žiadne volanie modelu, žiadny návrh rozhovoru
    assert rec.calls == []
