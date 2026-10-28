"""M42 – zber ľudí po skúsenosti z H3 (28. 10. 2026): firemné kontakty, konateľ malej firmy, opätovný zber,
dopyty podľa segmentu po mestách. Fiktívne organizácie, bez siete."""

import json
from collections import Counter

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PeopleConfig, PrivacyConfig
from radar.contacts import extract_contacts, is_org_email
from radar.db import DB
from radar.orgs import OrgFinder, SegmentFilter, segment_query_plan
from radar.people import PEOPLE_COLLECT_VERSION, PeopleCollector, rank_hypothesis, small_org
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router

SITE = "https://www.fiktivnereality.sk/"
PAGE = """<html><head><title>Kontakt – Fiktívne Reality</title></head><body>
<div class="tim"><p>Mgr. Jana Maklérová, realitná maklérka, jana.maklerova@fiktivnereality.sk</p></div>
<p>Hypotéky vybaví náš partner: info@partnerbanka.sk</p>
<form action="/odoslat"><input name="meno"><textarea name="sprava"></textarea></form>
<footer>Fiktívne Reality s.r.o., Námestie 1, Žilina, E-mail: fiktivnereality@fiktivnereality.sk, Tel.: 0905 123 456</footer>
</body></html>"""


def setup(tmp_path, segment=None, **people):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=False, **people))
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Malé realitné kancelárie nestíhajú dopyty", "Popis", "malé realitné kancelárie")
    db.set_people_selection(hid, True)
    if segment is not None:
        db.set_hypothesis_fields(hid, segment_filter_json=segment.model_dump_json())
    rec = Recorder()
    return cfg, db, hid, rec, make_router(cfg, db, rec)


def add_org(db, hid, name="Fiktívne Reality s.r.o.", website=SITE, relation="segment_match", size=None):
    org_id, _ = db.upsert_organization(name, name.lower(), "search", website=website, website_verified=1)
    db.link_org_hypothesis(org_id, hid, relation)
    if size:
        db.update_organization(org_id, size_category=size)
    return org_id


def test_org_email_rules():
    assert is_org_email("fiktivnereality@fiktivnereality.sk", "Fiktívne Reality s.r.o.", SITE)  # názov domény
    assert is_org_email("realityxyz@gmail.com", "Reality XYZ s.r.o.", "https://xyz.sk/")  # názov firmy, iná doména
    assert is_org_email("info@fiktivnereality.sk", "Fiktívne Reality s.r.o.", SITE)
    assert is_org_email("podatelna@msu.fiktivne-mesto.sk", "Mesto Fiktívne", "https://www.fiktivne-mesto.sk/")
    assert not is_org_email("info@partnerbanka.sk", "Fiktívne Reality s.r.o.", SITE)  # generická adresa partnera
    assert not is_org_email("jana.maklerova@fiktivnereality.sk", "Fiktívne Reality s.r.o.", SITE)  # osobná adresa
    # spresnené po kontrole na živých dátach (28. 10. 2026): staré pravidlo ich uložilo správne, prísne by ich zahodilo
    assert is_org_email("info@fiktivnyurad.sk", "Fiktívny úrad", "https://tv.fiktivnyurad.sk/")  # web na subdoméne
    assert is_org_email("info@fiktivna-firma.sk", "Fiktívna Firma s.r.o.", "https://fiktivnafirma.biz/")  # značka, iná koncovka
    assert is_org_email("info@abtechnika.sk", "A & B technika s.r.o.", "https://produkt-xyz.cz/")  # doména s názvom firmy
    assert is_org_email("podatelna@fiktivne.gov.sk", "Ministerstvo fikcie", "https://www.minfikcie.sk/", ["gov.sk"])
    assert not is_org_email("podatelna@fiktivne.gov.sk", "Ministerstvo fikcie", "https://www.minfikcie.sk/")
    assert not is_org_email("email@vzor.example", "Úrad Fiktívny", "https://urad.example/")  # vzorová adresa z formulára
    assert not is_org_email("info@webyobci.sk", "Obec Fiktívna", "https://www.obecfiktivna.sk/")  # dodávateľ webu


def test_firm_email_and_form_are_org_contacts_partner_and_personal_are_not(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    rec.responses = {"ContactAssoc": {"persons": []}}  # model nenavrhne nikoho
    page = extract_contacts(PAGE, SITE + "kontakt")
    assert page.has_form
    PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1)).process_page(
        db.get_hypothesis(hid), db.get_organization(org_id), page)
    contacts = {(c["kind"], c["value"]) for c in db.org_contacts(org_id)}
    assert ("email", "fiktivnereality@fiktivnereality.sk") in contacts
    assert ("contact_form", SITE + "kontakt") in contacts
    assert not {v for k, v in contacts if v in ("info@partnerbanka.sk", "jana.maklerova@fiktivnereality.sk")}
    form = next(c for c in db.org_contacts(org_id) if c["kind"] == "contact_form")
    assert json.loads(form["checks_json"])["over_ručne"] is True


def test_small_org_lead_is_kept_outside_per_org_limit(tmp_path):
    """Reálny prípad H3: dve maklérky s presnou zhodou funkcie predbehli konateľku a výber ju vymazal."""
    cfg, db, hid, rec, router = setup(tmp_path, max_people_per_org=2)
    db.set_role_map(hid, [{"category": "realitný maklér", "examples": ["realitná maklérka"], "rationale": "r"}])
    org_id = add_org(db, hid, size="micro")
    lead = db.insert_person(org_id, "Hana Fiktívna", "hana fiktivna", "https://rpo.example/1", "konateľ", hid,
                            role_title="Konateľ", source_kind="rpo")
    for i in range(3):
        pid = db.insert_person(org_id, f"Maklérka{i} Testová", f"maklerka{i} testova", SITE + "tim", "x", hid,
                               role_title="realitná maklérka", source_kind="website")
        db.add_contact_point(pid, org_id, "email", f"m{i}@fiktivnereality.sk", SITE + "tim", f"m{i}@fiktivnereality.sk", "regex")
    stats = rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    selected = {l["person_id"]: l for l in db.links_for_hypothesis(hid)}
    assert lead in selected and stats["small_org_leads"] == 1
    assert len(selected) == 3  # konateľka + 2 maklérky (limit na organizáciu platí pre ostatných)
    assert json.loads(selected[lead]["priority_json"])["vedenie_malej_organizácie"].startswith("vybraté vždy")
    assert db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3  # tretia maklérka sa vymazala


def test_unknown_size_counts_as_small_only_in_small_segment(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path, segment=SegmentFilter(sizes=["micro", "small"]))
    db.set_role_map(hid, [{"category": "realitný maklér", "examples": ["realitná maklérka"], "rationale": "r"}])
    org_id = add_org(db, hid)  # RÚZ bez veľkosti
    pid = db.insert_person(org_id, "Peter Konateľ", "peter konatel", "https://rpo.example/2", "konateľ", hid,
                           role_title="konateľ", source_kind="rpo")
    rank_hypothesis(cfg, db, router, db.get_hypothesis(hid), Counter())
    link = next(l for l in db.links_for_hypothesis(hid) if l["person_id"] == pid)
    assert json.loads(link["priority_json"])["kategória"] == "vedenie malej organizácie"
    hyp = db.get_hypothesis(hid)
    org = next(o for o in db.orgs_for_hypothesis(hid) if o["id"] == org_id)
    assert small_org(org, hyp)
    other = add_org(db, hid, "Iná Firma s.r.o.", "https://ina.sk/", relation="manual")
    assert not small_org(next(o for o in db.orgs_for_hypothesis(hid) if o["id"] == other), hyp)
    cfg2, db2, hid2, *_ = setup(tmp_path / "b", segment=SegmentFilter(sizes=["micro", "medium"]))
    org2 = add_org(db2, hid2)
    assert not small_org(db2.orgs_for_hypothesis(hid2)[0], db2.get_hypothesis(hid2)) and org2


def test_orgs_from_older_rules_are_collected_again_once(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    org_id = add_org(db, hid)
    db.set_org_hypothesis(org_id, hid, status="people_done")  # zber podľa starších pravidiel (verzia 0)
    rec.responses = {"ContactAssoc": {"persons": []}}
    fetcher = FakeFetcher(htmls={SITE: PAGE})
    collector = PeopleCollector(cfg, db, router, FakeSearx(), fetcher, Deadline(1))
    collector.run_hypothesis(db.get_hypothesis(hid))
    assert SITE in fetcher.fetched and collector.stats["orgs_recollected"] == 1
    assert db.org_hypothesis(org_id, hid)["people_version"] == PEOPLE_COLLECT_VERSION
    assert ("email", "fiktivnereality@fiktivnereality.sk") in {(c["kind"], c["value"]) for c in db.org_contacts(org_id)}
    fetcher.fetched.clear()
    collector.run_hypothesis(db.get_hypothesis(hid))
    assert fetcher.fetched == []  # aktuálna verzia sa znova nezbiera


def test_segment_queries_continue_by_towns_of_the_region_and_remember_tried(tmp_path):
    flt = SegmentFilter(search_queries=["realitná kancelária Žilina kontakt"], region="Žilinský kraj", sizes=["micro"])
    plan = segment_query_plan(flt)
    assert plan[:3] == ["realitná kancelária Žilina kontakt", "realitná kancelária Martin",
                        "realitná kancelária Liptovský Mikuláš"]
    assert "realitná kancelária Žilina" not in plan  # mesto z pôvodného dopytu sa nezopakuje
    assert plan.index("realitná kancelária Bratislava") > plan.index("realitná kancelária Liptovský Hrádok")
    cfg, db, hid, rec, router = setup(tmp_path, segment=flt, segment_queries_per_run=3)
    searx = FakeSearx()
    finder = OrgFinder(cfg, db, router, searx, FakeFetcher(), None, None, Deadline(1))
    finder.from_segment(db.get_hypothesis(hid))
    finder.from_segment(db.get_hypothesis(hid))
    assert searx.queries == plan[:6]  # druhý beh pokračuje tam, kde prvý skončil
    assert set(db.get_setting(f"segment_queries:{hid}")) == set(plan[:6])
    assert segment_query_plan(flt, expand=False) == ["realitná kancelária Žilina kontakt"]
