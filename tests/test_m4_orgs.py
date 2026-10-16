"""M4 – organizácie: RPO/RÚZ z fixtures, menované v dôkazoch, overenie webu, exclude_orgs (bez siete)."""

import json
from pathlib import Path

import httpx

from radar.config import Config, EmbeddingsConfig, ModelsConfig, OutreachConfig
from radar.db import DB
from radar.orgs import OrgFinder, icos_in, run_orgs, select_people_hypotheses
from radar.registers import RPOClient, RUZClient, org_info_from_rpo, sanitize_rpo
from radar.search import SearchResult
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router

FIX = Path(__file__).parent / "fixtures"
ENTITY = json.loads((FIX / "rpo_entity.json").read_text(encoding="utf-8"))
DOC = ("Úvod. " * 60 + "\nFiktívna Strojáreň s.r.o. uviedla, že meškanie dodávok komponentov jej predĺžilo projekty o tri mesiace.\n"
       + "Záver. " * 60)
QUOTE = "Fiktívna Strojáreň s.r.o. uviedla, že meškanie dodávok komponentov jej predĺžilo projekty o tri mesiace."


def rpo_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/search"):
            params = dict(request.url.params)
            if params.get("identifier") == "12345678" or "Strojáre" in params.get("fullName", ""):
                return httpx.Response(200, json={"results": [{"id": 555001, "identifiers": ENTITY["identifiers"],
                                                              "fullNames": ENTITY["fullNames"]}]})
            return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("/entity/555001"):
            return httpx.Response(200, json=ENTITY)
        if "uctovne-jednotky" in request.url.path:
            return httpx.Response(200, json={"id": [77]})
        if "uctovna-jednotka" in request.url.path:
            return httpx.Response(200, json={"id": 77, "nazovUJ": "Fiktívna Strojáreň s.r.o.", "skNace": "28290",
                                             "velkostOrganizacie": "11", "druhVlastnictva": "2", "kraj": "SK031",
                                             "mesto": "Žilina", "ulica": "Výrobná 3", "psc": "01001", "idVyrocnychSprav": [9]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def clients(db, calls):
    http = httpx.Client(transport=rpo_transport(calls))
    return RPOClient(db, delay_s=0, http=http), RUZClient(db, delay_s=0, http=http)


def setup(tmp_path, exclude=None):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 outreach=OutreachConfig(exclude_orgs=exclude or []))
    db = DB(cfg.db_path)
    doc_id, _ = db.upsert_document("https://noviny.sk/clanok", "Článok", "search", "Zákazková výroba", "q", "h", DOC)
    doc2, _ = db.upsert_document("https://zvaz.sk/prieskum", "Prieskum", "search", "Zákazková výroba", "q", "h2",
                                 "Prieskum zväzu. " * 30 + "\nVýrobcovia hlásia meškanie dodávok komponentov.\n")
    hid = db.create_hypothesis("Meškanie dodávok v zákazkovej výrobe", "Popis", "zákazkoví výrobcovia")
    for d, q in ((doc_id, QUOTE), (doc2, "Výrobcovia hlásia meškanie dodávok komponentov.")):
        cid = db.insert_claim(d, 0, "Zákazková výroba", "Výrobcovia majú meškanie dodávok.", "výrobcovia", "pain", q)
        db.set_claim_verification(cid, "verified", 100, "ok")
        db.assign_claim(hid, cid)
    rec = Recorder()
    router = make_router(cfg, db, rec)
    return cfg, db, hid, rec, router


def test_rpo_fixture_maps_fields_without_statutory_address(tmp_path, caplog):
    cfg, db, hid, rec, router = setup(tmp_path)
    calls: list[str] = []
    rpo, ruz = clients(db, calls)
    info = rpo.lookup_ico("12345678")
    assert (info.name, info.ico, info.legal_form, info.sk_nace) == ("Fiktívna Strojáreň s.r.o.", "12345678",
                                                                    "Spoločnosť s ručením obmedzeným", "28290")
    assert info.region == "Žilinský kraj" and info.municipality == "Žilina"
    assert [(p.full_name, p.function) for p in info.statutory] == [("Ján Testovací", "konateľ")]  # bývalý konateľ nie
    stored = " ".join(r["body"] for r in db.conn.execute("SELECT body FROM register_cache"))
    for secret in ("Súkromná ulica", "Bývalá 1", "Tajná 5", "Spoločník", "Výrobná"):
        assert secret not in stored  # adresy štatutárov ani spoločníci sa neuložia
    assert "Súkromná ulica" not in caplog.text
    # druhé volanie ide z cache
    n = len(calls)
    rpo.lookup_ico("12345678")
    assert len(calls) == n
    ruz_info = ruz.lookup_ico("12345678")
    assert ruz_info["size_category"] == "small" and ruz_info["sector_kind"] == "private"


def test_sanitize_keeps_only_name_and_function():
    clean = sanitize_rpo(ENTITY)
    body = clean["statutoryBodies"][0]
    assert set(body) == {"personName", "stakeholderType", "validFrom"}
    assert "stakeholders" not in clean
    assert "street" not in json.dumps(clean, ensure_ascii=False)
    assert org_info_from_rpo(ENTITY).statutory[0].titles == "Ing."


def test_org_from_evidence_only_when_name_in_quote_or_window(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    rec.responses = {
        "OrgMentions": lambda m: {"organizations": [
            {"name": "Fiktívna Strojáreň s.r.o.", "relation": "affected"},
            {"name": "Vymyslená Firma a.s.", "relation": "affected"},  # v zdroji nie je
            {"name": "Ministerstvo hospodárstva", "relation": "regulator"},
        ]},
        "SegmentFilter": {"sk_nace_prefixes": [], "search_queries": []},
    }
    calls: list[str] = []
    rpo, ruz = clients(db, calls)
    fetcher = FakeFetcher(htmls={"https://fiktivna-strojaren.sk/": "<html><body>Fiktívna Strojáreň s.r.o., IČO: 12 345 678</body></html>"})
    searx = FakeSearx({"12345678": [SearchResult("https://fiktivna-strojaren.sk/o-nas", "Fiktívna Strojáreň", "")]})
    stats = run_orgs(cfg, db, router, searx, fetcher, Deadline(1), [hid], rpo, ruz)
    orgs = db.orgs_for_hypothesis(hid)
    assert [o["name"] for o in orgs] == ["Fiktívna Strojáreň s.r.o."]
    org = orgs[0]
    assert org["ico"] == "12345678" and org["relation"] == "mentioned_with_problem" and org["sk_nace"] == "28290"
    assert org["size_category"] == "small"
    assert stats["mentions_not_in_source"] >= 1
    assert org["website_verified"] == 1 and "IČO 12345678" in org["website_evidence"]


def test_website_verified_only_with_ico_or_exact_name(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    finder = OrgFinder(cfg, db, router, FakeSearx({
        '"Fiktívna Strojáreň s.r.o." 12345678': [SearchResult("https://ina-firma.sk/", "Iná firma", ""),
                                                  SearchResult("https://finstat.sk/12345678", "Finstat", "")],
    }), FakeFetcher(htmls={
        "https://ina-firma.sk/": "<html><body>Iná strojáreň, IČO: 87 654 321</body></html>",
        "https://finstat.sk/": "<html><body>Fiktívna Strojáreň s.r.o. IČO 12345678</body></html>",
    }), None, None, Deadline(1))
    org_id, _ = db.upsert_organization("Fiktívna Strojáreň s.r.o.", "fiktivna strojaren", "evidence", ico="12345678")
    assert finder.verify_website(org_id) is False  # cudzí web nemá IČO; katalóg firiem (finstat) sa nepoužije
    assert db.get_organization(org_id)["website_verified"] == 0
    org2, _ = db.upsert_organization("Fiktívna Strojáreň s.r.o.", "fiktivna strojaren", "evidence", ico="12345678",
                                     website="https://strojaren-fikt.sk/")
    finder.fetcher.htmls["https://strojaren-fikt.sk/"] = "<html><body><h1>Fiktívna Strojáreň s.r.o.</h1> kontakt</body></html>"
    assert finder.verify_website(org2) is True
    assert "presný názov" in db.get_organization(org2)["website_evidence"]
    assert icos_in("IČO: 31 322 832, IČ DPH SK2020") == ["31322832"]


def test_excluded_org_never_appears(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path, exclude=["12345678"])
    rec.responses = {
        "OrgMentions": {"organizations": [{"name": "Fiktívna Strojáreň s.r.o.", "relation": "affected"}]},
        "SegmentFilter": {"sk_nace_prefixes": [], "search_queries": []},
    }
    rpo, ruz = clients(db, [])
    stats = run_orgs(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), [hid], rpo, ruz)
    assert db.orgs_for_hypothesis(hid) == []
    assert stats["excluded"] >= 1
    # aj podľa domény
    cfg.outreach.exclude_orgs = ["fiktivna-strojaren.sk"]
    from radar.orgs import is_excluded

    assert is_excluded(cfg, None, "https://www.fiktivna-strojaren.sk/kontakt")


def test_auto_selection_respects_manual_choice(tmp_path):
    cfg, db, hid, rec, router = setup(tmp_path)
    weak = db.create_hypothesis("Slabá", "", "x")
    assert select_people_hypotheses(cfg, db) == [hid]  # stredná sila (2 overené z 2 domén)
    db.set_people_selection(hid, False)  # ručne vypnuté
    db.set_people_selection(weak, True)  # ručne zapnuté napriek slabým dôkazom
    assert select_people_hypotheses(cfg, db) == [weak]


def test_registry_circuit_breaker_skips_unresponsive_register(tmp_path):
    """Reálny beh: RPO neodpovedalo a krok orgs čakal pri každej organizácii; po 3 zlyhaniach sa register vynechá."""
    db = DB(tmp_path / "r.sqlite")
    hits = []

    def handler(request):
        hits.append(str(request.url))
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    rpo = RPOClient(db, delay_s=0, http=httpx.Client(transport=httpx.MockTransport(handler)))
    for name in ("Obec Prvá", "Obec Druhá", "Obec Tretia", "Obec Štvrtá", "Obec Piata"):
        assert rpo.search_name(name) == []
    assert len(hits) == 3 and rpo.skipped == 2  # 4. a 5. dopyt počas prestávky ističa nečakajú na časový limit
    rpo._paused_until = 0.0  # prestávka uplynula: register sa skúsi znova
    rpo.search_name("Obec Šiesta")
    assert len(hits) == 4


def test_run_without_model_filters_and_prepare_next_does_filters_first(tmp_path):
    """Akcia #19 (53 hypotéz): orgs v behu nesmie volať presný model (striedanie 9B/27B pri každej hypotéze);
    prepare-next pripraví filtre segmentu pre všetky hypotézy skôr než pomalé mapy rolí."""
    from types import SimpleNamespace

    from radar.commands import stage_prepare_next

    cfg, db, hid, rec, router = setup(tmp_path)
    cfg.privacy.lia_completed = True
    rec.responses = {"OrgMentions": {"organizations": []},
                     "SegmentFilter": {"sk_nace_prefixes": ["25"], "search_queries": []},
                     "RoleMap": {"roles": [{"category": "vedúci výroby", "examples": ["riaditeľ výroby"]}]}}
    rpo, ruz = clients(db, [])
    stats = run_orgs(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), [hid], rpo, ruz, model_filters=False)
    assert stats["segment_filter_pending"] == 1 and rec.models_for("SegmentFilter") == []

    h2 = db.create_hypothesis("Druhá hypotéza", "Popis", "obce")
    for h in (hid, h2):
        db.set_people_selection(h, True)
    app = SimpleNamespace(cfg=cfg, db=db, router=router, searx=FakeSearx(), fetcher=FakeFetcher())
    assert stage_prepare_next(app, Deadline(1)) == {"segment_filters": 2, "role_maps": 2}
    order = [name for name, *_ in rec.calls if name in ("SegmentFilter", "RoleMap")]
    assert order[:2] == ["SegmentFilter", "SegmentFilter"] and set(order[2:]) == {"RoleMap"}


def test_malformed_link_on_page_does_not_drop_the_page():
    """Akcia #19: odkaz „http://[…“ v HTML (ValueError Invalid IPv6 URL) zahodil celú stránku kandidáta."""
    from radar.contacts import contact_page_links, extract_contacts, sitemap_contact_links

    html = ('<html><body><a href="http://[broken">zlý</a><a href="/kontakt">Kontakt</a>'
            "<p>Fiktívna Firma s.r.o., IČO: 12 345 678</p></body></html>")
    page = extract_contacts(html, "https://fiktivna-firma.sk/")
    assert contact_page_links(page, "https://fiktivna-firma.sk/") == ["https://fiktivna-firma.sk/kontakt"]
    xml = "<urlset><url><loc>https://[zle</loc></url><url><loc>https://fiktivna-firma.sk/kontakty</loc></url></urlset>"
    assert sitemap_contact_links(xml, "https://fiktivna-firma.sk/") == ["https://fiktivna-firma.sk/kontakty"]


def test_segment_candidate_ico_found_on_about_page(tmp_path):
    """Akcia #20: 130 kandidátov bez IČO na úvodnej a jednej kontaktnej stránke; IČO býva v sekcii O nás."""
    from radar.orgs import SegmentFilter

    cfg, db, hid, rec, router = setup(tmp_path)
    rec.responses = {"OrgMentions": {"organizations": []}}
    db.set_hypothesis_fields(hid, segment_filter_json=SegmentFilter(search_queries=["zákazková strojáreň"]).model_dump_json())
    rpo, ruz = clients(db, [])
    searx = FakeSearx({"zákazková strojáreň": [SearchResult("https://fiktivna-strojaren.sk/produkty", "Fiktívna Strojáreň", "")]})
    fetcher = FakeFetcher(htmls={
        "https://fiktivna-strojaren.sk/": "<html><body><h1>Fiktívna Strojáreň s.r.o.</h1> zákazková výroba</body></html>",
        "https://fiktivna-strojaren.sk/o-nas": "<html><body>Fiktívna Strojáreň s.r.o., IČO: 12 345 678</body></html>",
    })
    stats = run_orgs(cfg, db, router, searx, fetcher, Deadline(1), [hid], rpo, ruz, model_filters=False)
    assert "segment_no_ico" not in stats and stats["orgs_created"] == 1
    org = db.orgs_for_hypothesis(hid)[0]
    assert org["ico"] == "12345678" and org["website_verified"] == 1 and org["relation"] == "segment_match"


def test_name_resolution_uses_current_rpo_name_not_last_listed():
    """Reálny beh #20: „Dôvera“ z článku o poisťovniach sa zlúčila so spoločenstvom vlastníkov bytov, lebo RPO
    vracia aj historické názvy v ľubovoľnom poradí a kód bral posledný prvok."""
    import tempfile

    cfg = Config(db_path=f"{tempfile.mkdtemp()}/r.sqlite", models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False))
    db = DB(cfg.db_path)

    names = [{"value": "Spoločenstvo vlastníkov bytov Fiktívna Dôvera", "validFrom": "2005-01-01"},
             {"value": "Fiktívna Dôvera", "validFrom": "1995-01-01", "validTo": "2004-12-31"}]

    def handler(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"id": 9001, "fullNames": names}]})
        if request.url.path.endswith("/entity/9001"):
            return httpx.Response(200, json={**ENTITY, "id": 9001, "fullNames": names})
        return httpx.Response(404)

    rpo = RPOClient(db, delay_s=0, http=httpx.Client(transport=httpx.MockTransport(handler)))
    finder = OrgFinder(cfg, db, None, FakeSearx(), FakeFetcher(), rpo, None, Deadline(1))
    assert finder.resolve_name("Fiktívna Dôvera") is None  # historický názov inej organizácie nestačí


def test_directory_naming_the_org_is_not_its_website_and_revoke_removes_its_contacts(tmp_path):
    """16. 10. 2026: katalóg kontaktov (kontakttelefon.sk) spomínal názov ÚVO a overil sa ako jeho web; z katalógu sa potom
    zobral e-mail inej firmy. Samotný názov stačí iba pri zhode domény s názvom; zoznam s viacerými IČO je katalóg."""
    from radar.orgs import domain_matches_name, revoke_website

    name = "Úrad pre verejné obstarávanie"
    assert domain_matches_name("https://www.uvo.gov.sk/", name) and not domain_matches_name("https://www.kontakttelefon.sk/", name)
    assert domain_matches_name("https://www.mfsr.sk/", "Ministerstvo financií Slovenskej republiky")
    assert domain_matches_name("https://tv.nrsr.sk/", "Kancelária NR SR") and domain_matches_name("https://www.socpoist.sk/", "Sociálna poisťovňa")
    assert not domain_matches_name("https://www.skupinazse.sk/", "Východoslovenská distribučná, a.s.")
    cfg, db, hid, rec, router = setup(tmp_path)
    directory = ("<html><body>Kontakty na úrady: Úrad pre verejné obstarávanie, IČO 31797903; Iný úrad IČO 11111111; "
                 "Energetika IČO 22222222; Telekom IČO 33333333</body></html>")
    finder = OrgFinder(cfg, db, router, FakeSearx({
        f'"{name}" 31797903': [SearchResult("https://www.kontakty-uradov.sk/urady", "Kontakty", ""),
                               SearchResult("https://www.uvo.gov.sk/kontakt", "ÚVO", "")],
    }), FakeFetcher(htmls={
        "https://www.kontakty-uradov.sk/": directory,
        "https://www.uvo.gov.sk/": "<html><body><h1>Úrad pre verejné obstarávanie</h1> kontakt</body></html>",
    }), None, None, Deadline(1))
    org_id, _ = db.upsert_organization(name, "urad pre verejne obstaravanie", "procurement", ico="31797903")
    assert finder.verify_website(org_id) is True  # uvo.gov.sk je v agregátoroch, no zodpovedá názvu
    org = db.get_organization(org_id)
    assert org["website"] == "https://www.uvo.gov.sk/" and "presný názov" in org["website_evidence"]
    # staršie chybné overenie: zrušenie odstráni kontakty z cudzej stránky aj neodoslaný návrh na ne
    bad, _ = db.upsert_organization("Iný úrad", "iny urad", "procurement", website="https://www.kontakttelefon.sk/")
    db.update_organization(bad, website_verified=1, website_evidence="presný názov na https://www.kontakttelefon.sk/")
    cid = db.add_contact_point(None, bad, "email", "kontakt@firma-x.sk", "https://www.kontakttelefon.sk/firma-x",
                               "kontakt@firma-x.sk", "regex", True)
    keep = db.add_contact_point(None, bad, "email", "podatelna@iny-urad.sk", "https://www.slovensko.sk/x",
                                "podatelna@iny-urad.sk", "regex", True)
    oid = db.insert_outreach(None, hid, "email", "B", "Predmet", "Text", [], {"ok": True}, "needs_edit", cid, org_id=bad)
    assert revoke_website(db, bad, "katalóg") == {"kontakty": 1, "návrhy": 1}
    org = db.get_organization(bad)
    assert org["website"] is None and org["website_verified"] == 0 and "kontakttelefon" in org["website_evidence"]
    assert db.get_contact(cid) is None and db.get_contact(keep) is not None and db.get_outreach(oid) is None
