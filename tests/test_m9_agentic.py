"""M9 – agentické workflowy z GUI: nové hypotézy (scout), test všetkých hypotéz (validate),
úlohy na pozadí, LinkedIn odkazy (iba URL, nikdy sa nesťahujú). Offline: falošný model, web aj vyhľadávanie."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from radar.cluster import run_cluster
from radar.config import Config, EmbeddingsConfig, ModelsConfig, PeopleConfig, PrivacyConfig, WorkflowsConfig
from radar.contacts import extract_contacts, normalize_profile_url, phones_in
from radar.db import DB
from radar.jobs import JobBusy, job_command, log_tail, refresh_jobs, start_job, stop_job
from radar.people import PeopleCollector, add_manual_profile, search_profiles
from radar.privacy import forget
from radar.scout import next_scout_cells, run_scout
from radar.search import SearchResult
from radar.util import Deadline
from radar.validate import run_validate_finish, run_validate_search, validation_result
from radar.verify import run_verify
from radar.workflows import run_workflow
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router

QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty meškajú."
COUNTER = "Centrá zdieľaných služieb už obstarávanie pre malé obce bežne zabezpečujú."
DOC = "Úvod analýzy. " * 30 + QUOTE + " " + COUNTER + " Záver analýzy. " * 30
URL = "https://analyzy.sk/obce"


def call(name, args, call_id="1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def step(*calls):
    return SimpleNamespace(content=None, tool_calls=list(calls))


def agent_script(stance_quotes):
    """Agent: hľadaj → prečítaj → ulož citáty → koniec."""
    records = [call("record_evidence", {"url": URL, "part": 0, "problem": p, "affected_party": "malé obce", "quote": q,
                                        "stance": stance}, f"r{i}") for i, (p, q, stance) in enumerate(stance_quotes)]
    return [step(call("web_search", {"query": "obce obstarávanie kapacita"})), step(call("read_page", {"url": URL}, "2")),
            step(*records), step(call("finish", {"summary": "Hotovo."}, "9"))]


def setup(tmp_path, **workflows):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=True),
                 workflows=WorkflowsConfig(**workflows), config_dir=str(tmp_path))
    db = DB(cfg.db_path)
    rec = Recorder()
    return cfg, db, rec, make_router(cfg, db, rec)


def hyp_with_claim(db, title="Kapacita obcí na obstarávanie", quote=QUOTE, url="https://a.sk/x", status="new"):
    doc, _ = db.upsert_document(url, "A", "search", "t", "q", url, DOC)
    cid = db.insert_claim(doc, 0, "t", title, "malé obce", "pain", quote)
    db.set_claim_verification(cid, "verified", 100, "ok")
    hid = db.create_hypothesis(title, "Popis", "malé obce", status=status)
    db.assign_claim(hid, cid)
    return hid


# ======================================================================= testovanie hypotéz
def test_validation_queue_covers_all_hypotheses_in_priority_order(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    rejected = db.create_hypothesis("Zamietnutá", "", "x", status="rejected")
    weak = db.create_hypothesis("Slabá nová", "", "x")
    strong = hyp_with_claim(db, "Silnejšia nová")
    gate = db.create_hypothesis("Na Bránu 0", "", "x")
    db.set_people_selection(gate, True)
    cand = db.create_hypothesis("Kandidát", "", "x", status="candidate")
    order = [h["id"] for h in db.validation_queue(10)]
    assert order == [gate, cand, strong, weak, rejected]  # všetky vrátane zamietnutej, tá posledná
    db.set_validation_result(gate, {"verdikt": "slabá"})
    assert gate not in [h["id"] for h in db.validation_queue(10)]  # otestovaná sa do retestu neopakuje
    assert db.validation_counts() == {"none": 4, "searched": 0, "tested": 1, "total": 5}


def test_validate_search_records_hinted_claims_and_cluster_decides(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "Popis", "malé obce", status="rejected")
    other = db.create_hypothesis("Iný problém", "", "nemocnice")
    rec.responses["tools"] = agent_script([("Malé obce nemajú kapacitu.", QUOTE, "supports"),
                                           ("Centrá zdieľaných služieb to riešia.", COUNTER, "contradicts")])
    stats = run_validate_search(cfg, db, router, FakeSearx(), FakeFetcher({URL: DOC}), Deadline(1), ids=[hid])
    assert stats == {"searched": 1, "claims": 2}
    h = db.get_hypothesis(hid)
    assert h["validation_state"] == "searched" and h["validation_runs"] == 1
    claims = db.hinted_claims(hid)
    assert [(c["origin"], c["claim_type"], c["hypothesis_hint"]) for c in claims] == [
        ("validation", "pain", hid), ("validation", "counter", hid)]
    assert all(m == "radar-qwen3.5-9b" for s, m, *_ in rec.calls if s == "tools")  # agent na rýchlom modeli

    # citát overí kód a presný model; zaradenie rozhodne presný model (testovaná hypotéza je medzi kandidátmi aj zamietnutá)
    rec.responses["Entailment"] = {"verdict": "supported", "reason": "ok"}
    seen = []

    def assign(messages):
        seen.append(messages[-1]["content"])
        if "Centrá" in messages[-1]["content"]:
            return {"decision": "new", "title": "Protidôkaz", "segment": "x"}  # protidôkaz nezaloží hypotézu
        return {"decision": "existing", "hypothesis_id": hid}

    rec.responses["Assignment"] = assign
    assert run_verify(cfg, db, router, Deadline(1))["verified"] == 2
    assert db.validation_blocking(hid) == 2
    stats = run_cluster(cfg, db, router, Deadline(1))
    assert stats["assigned_existing"] == 1 and stats["unrelated_not_supporting"] == 1
    assert all(f"id={hid}" in text for text in seen)
    assert {h["title"] for h in db.all_hypotheses()} == {"Kapacita obcí na obstarávanie", "Iný problém"}
    assert other and db.validation_blocking(hid) == 0
    assert run_validate_finish(cfg, db, Deadline(1)) == {"tested": 1, "verdict:slabá": 1}
    result = json.loads(db.get_hypothesis(hid)["validation_json"])
    assert result["verdikt"] == "slabá" and result["z_testu"]["priradené_sem"] == 1
    assert result["agent"]["ok"] is True and result["agent"]["tvrdenia"] == 2


def test_validate_finish_waits_for_pending_claims_and_verdicts(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = hyp_with_claim(db)
    doc, _ = db.upsert_document("https://b.sk/y", "B", "agent", "t", None, "hb", DOC)
    db.insert_claim(doc, 0, "t", "Čaká", "obce", "pain", QUOTE, origin="validation", hypothesis_hint=hid)
    db.set_validation_searched(hid, {"agent": {"ok": True}})
    assert run_validate_finish(cfg, db, Deadline(1)) == {"waiting": 1}
    db.conn.execute("UPDATE hypotheses SET validation_started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (hid,))
    assert run_validate_finish(cfg, db, Deadline(1))["tested"] == 1  # po 3 dňoch verdikt aj tak (napr. čaká na človeka)

    # verdikty zo zložiek: 2 overené z 2 domén = podložená; protidôkazov ≥ podporných = sporná
    second = db.insert_claim(db.upsert_document("https://c.sk/z", "C", "search", "t", "q", "hc", DOC)[0], 0, "t", "x", "obce",
                             "pain", QUOTE)
    db.set_claim_verification(second, "verified", 100, "ok")
    db.assign_claim(hid, second)
    assert validation_result(db, hid)["verdikt"] == "podložená"
    for i in range(2):
        doc, _ = db.upsert_document(f"https://d{i}.sk/", "D", "agent", "t", None, f"hd{i}", DOC)
        cid = db.insert_claim(doc, 0, "t", "proti", "obce", "counter", COUNTER)
        db.set_claim_verification(cid, "verified", 100, "ok")
        db.assign_claim(hid, cid)
    r = validation_result(db, hid)
    assert r["verdikt"] == "sporná" and r["protidôkazy"] == 2 and r["overené"] == 2  # protidôkaz silu nezvyšuje


def test_model_down_leaves_hypothesis_in_queue(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    rec.responses["tools"] = APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11434/v1"))
    with pytest.raises(APIConnectionError):
        run_validate_search(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1))
    assert db.get_hypothesis(hid)["validation_state"] is None  # neoznačí sa ako otestovaná bez testu


# ======================================================================= nové hypotézy (scout)
def test_scout_claims_become_discovery_candidates_and_cells_rotate(tmp_path):
    cfg, db, rec, router = setup(tmp_path, scout_max_steps=6)
    cells = [next_scout_cells(cfg, db, 2), next_scout_cells(cfg, db, 2)]
    assert cells[0] != cells[1]  # rotácia pokračuje
    rec.responses["tools"] = agent_script([("Malé obce nemajú kapacitu na obstarávanie.", QUOTE, "supports")])
    stats = run_scout(cfg, db, router, FakeSearx(), FakeFetcher({URL: DOC}), Deadline(1), sessions=1)
    assert stats == {"sessions": 1, "claims": 1, "pages_read": 1}
    # prečítaná stránka ide aj do bežného screen → extrakcia (dokument objavovania, kategória scout)
    assert [(d["source_kind"], d["source_category"]) for d in db.documents_to_screen(10)] == [("discovery", "scout")]
    claim = db.conn.execute("SELECT * FROM claims").fetchone()
    assert claim["origin"] == "scout" and claim["sector_hint"]  # odvetvie z bunky matice
    system = [m for m in rec.calls if m[0] == "tools"]
    assert system and all(m[1] == "radar-qwen3.5-9b" for m in system)
    rec.responses["Entailment"] = {"verdict": "supported", "reason": "ok"}
    rec.responses["Assignment"] = {"decision": "new", "title": "Kapacita obcí na obstarávanie", "segment": "malé obce"}
    run_verify(cfg, db, router, Deadline(1))
    db.create_hypothesis("Iný problém", "", "nemocnice")
    stats = run_cluster(cfg, db, router, Deadline(1))
    assert stats["new_candidates"] == 1
    new = [h for h in db.all_hypotheses() if h["title"].startswith("Kapacita")][0]
    assert new["status"] == "candidate" and new["origin"] == "discovery"  # povýšenie až po 2 doménach


# ======================================================================= workflow a úlohy z GUI
def test_workflow_runs_stages_by_model_and_records_job(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    app = SimpleNamespace(cfg=cfg, db=db, router=router)
    calls = []
    job = db.create_job("scout", {"minutes": 5})
    result = run_workflow(app, "scout", 5, job_id=job, run_stage=lambda a, s, dl: calls.append(s) or {"ok": 1})
    assert calls == ["discover", "scout", "screen", "extract", "verify-code", "source-types", "embed", "verify",
                     "cluster", "themes", "report"]
    assert app.discover_budget == cfg.workflows.scout_docs_per_job and app.scout_sessions == cfg.workflows.scout_sessions_per_job
    row = db.get_job(job)
    assert row["status"] == "done" and json.loads(row["stats_json"])["workflow"] == "scout"

    # validate: dávky, kým je fronta; zlyhanie agenta zastaví slučku a úloha skončí s chybou
    for i in range(3):
        db.create_hypothesis(f"H{i}", "", "x")
    calls.clear()

    def stage(a, s, dl):
        calls.append(s)
        if s == "validate-search":
            for h in db.validation_queue(a.validate_limit):
                db.set_validation_searched(h["id"])
        if s == "validate-finish":
            for h in db.hypotheses_in_validation():
                db.set_validation_result(h["id"], {"verdikt": "nepodložená"})
        return {}

    app2 = SimpleNamespace(cfg=cfg.model_copy(update={"workflows": WorkflowsConfig(validate_batch=2)}), db=db, router=router)
    run_workflow(app2, "validate", 60, run_stage=stage)  # ďalšia dávka sa začne iba pri ≥ 5 min zvyšného času
    assert calls.count("validate-search") == 2 and calls[-1] == "validate-finish"
    assert db.validation_counts()["tested"] == 3

    db.create_hypothesis("Ďalšia", "", "x")
    job = db.create_job("validate", {})

    def failing(a, s, dl):
        calls.append(s)
        if s == "validate-search":
            raise RuntimeError("model nebeží")
        return {}

    calls.clear()
    result = run_workflow(app2, "validate", 5, job_id=job, run_stage=failing)
    assert calls == ["validate-search"] and result["failed"] and db.get_job(job)["status"] == "failed"


def test_people_workflow_selects_only_given_hypotheses(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    app = SimpleNamespace(cfg=cfg, db=db, router=router)
    calls = []
    run_workflow(app, "people", 5, hypothesis_ids=[hid], run_stage=lambda a, s, dl: calls.append(s) or {})
    # návrhy sú samostatná akcia; hľadanie kontaktov podľa hypotézy (v10) je pred zberom ľudí z webov
    assert calls == ["prepare-next", "orgs", "contact-search", "people", "people-rank", "contacts"]
    assert db.get_hypothesis(hid)["people_enabled"] == 1 and app.people_ids == [hid]


def test_jobs_start_once_run_on_localhost_cli_and_stop(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    started = []

    class Proc:
        pid = 4242

    def popen(cmd, **kw):
        started.append((cmd, kw))
        return Proc()

    alive = {4242: True}
    job = start_job(cfg, db, tmp_path / "config.yaml", "validate", 30, [4, 11], popen=popen, alive=lambda pid: alive.get(pid, False))
    cmd, kw = started[0]
    assert cmd[1:] == ["-m", "radar", "--config", str(tmp_path / "config.yaml"), "workflow", "validate", "--minutes", "30",
                       "--job", str(job), "--hypotheses", "4,11"]
    assert kw["stdin"] is not None and db.get_job(job)["status"] == "running" and db.get_job(job)["pid"] == 4242
    with pytest.raises(JobBusy):  # na GPU sa zmestí iba jeden veľký model
        start_job(cfg, db, tmp_path / "config.yaml", "scout", 30, popen=popen, alive=lambda pid: alive.get(pid, False))
    alive[4242] = False
    refresh_jobs(db, alive=lambda pid: alive.get(pid, False))
    assert db.get_job(job)["status"] == "interrupted"  # proces skončil bez výsledku (napr. reštart)
    job2 = start_job(cfg, db, tmp_path / "config.yaml", "scout", 30, popen=popen, alive=lambda pid: True)
    killed = []
    assert stop_job(db, job2, run=lambda cmd, **kw: killed.append(cmd))
    assert db.get_job(job2)["status"] == "stopped"
    assert job_command("c.yaml", "scout", 90, 7)[-4:] == ["--minutes", "90", "--job", "7"]
    log = tmp_path / "job.log"
    log.write_text("krok 1\nkontakt jana.novakova@firma.sk tel. 0905 123 456\n", encoding="utf-8")
    tail = log_tail(str(log))
    assert "jana.novakova@firma.sk" not in tail and "0905 123 456" not in tail


# ======================================================================= LinkedIn
TEAM = """<html><body><div class="team">
<div class="person"><h3>Ing. Jana Nováková</h3><p>vedúca oddelenia nákupu</p>
<a href="mailto:jana.novakova@firma.sk">e-mail</a> <a href="https://sk.linkedin.com/in/jana-novakova-905123456?trk=x"><img src="li.svg"></a></div>
<div class="person"><h3>Peter Malý</h3><p>konateľ</p><a href="https://www.linkedin.com/in/peter-maly/">LinkedIn</a></div>
</div><footer><a href="https://www.linkedin.com/company/firma-sro/">Firma na LinkedIn</a></footer></body></html>"""


def test_linkedin_from_org_page_is_assigned_with_literal_snippet_and_never_a_phone(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    org, _ = db.upsert_organization("Firma s.r.o.", "firma", "search", website="https://firma.sk/", website_verified=1)
    db.link_org_hypothesis(org, hid, "segment_match")
    page = extract_contacts(TEAM, "https://firma.sk/tim")
    assert not any(p.endswith("905123456") for p in page.phones)  # číslice v URL profilu nie sú telefón
    rec.responses["ContactAssoc"] = {"persons": [
        {"block": 0, "full_name": "Jana Nováková", "titles": "Ing.", "role": "vedúca oddelenia nákupu",
         "email": "jana.novakova@firma.sk"},
        {"block": 1, "full_name": "Peter Malý", "role": "konateľ"}]}
    PeopleCollector(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1)).process_page(
        db.get_hypothesis(hid), db.get_organization(org), page)
    jana = db.find_person(org, "jana novakova")
    li = [c for c in db.contacts_for_person(jana["id"]) if c["kind"] == "profile_url"][0]
    assert li["value"] == "https://www.linkedin.com/in/jana-novakova-905123456"  # kanonická URL bez parametrov
    assert li["value"] in li["source_snippet"] and li["source_url"] == "https://firma.sk/tim" and li["extraction"] == "page_link"
    assert json.loads(li["checks_json"])["over_ručne"] is True
    peter = db.find_person(org, "peter maly")
    assert [c["value"] for c in db.contacts_for_person(peter["id"]) if c["kind"] == "profile_url"] == [
        "https://www.linkedin.com/in/peter-maly"]
    assert [c["value"] for c in db.org_contacts(org) if c["kind"] == "profile_url"] == ["https://www.linkedin.com/company/firma-sro"]


def test_search_profiles_for_all_selected_people_with_name_and_org_check(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    org, _ = db.upsert_organization("Strojáreň Fiktívna s.r.o.", "strojaren fiktivna", "search")
    jana = db.insert_person(org, "Jana Nováková", "jana novakova", "https://s.sk/k", "Jana Nováková, nákup", hid, role_title="nákup")
    db.add_contact_point(jana, org, "email", "jana.novakova@s.sk", "https://s.sk/k", "jana.novakova@s.sk", "mailto")
    eva = db.insert_person(org, "Eva Malá", "eva mala", "https://s.sk/k", "Eva Malá, kvalita", hid, role_title="kvalita")
    for pid in (jana, eva):
        db.set_link_priority(db.upsert_link(pid, hid, "role_owner"), 0.5, {})

    def results(query):
        if "Jana" in query:
            return [SearchResult(url="https://sk.linkedin.com/in/jana-novakova-1a2b?trk=s", title="Jana Nováková – nákupca",
                                 snippet="Strojáreň Fiktívna · Žilina")]
        return [SearchResult(url="https://www.linkedin.com/in/eva-mala-9", title="Eva Malá – učiteľka", snippet="ZŠ Žilina")]

    searx = FakeSearx(results)
    assert search_profiles(cfg, db, searx, hid) == 1  # aj osoba s e-mailom; Eva bez zhody organizácie nie
    li = [c for c in db.contacts_for_person(jana) if c["kind"] == "profile_url"][0]
    assert li["value"] == "https://www.linkedin.com/in/jana-novakova-1a2b" and li["extraction"] == "search_result"
    assert json.loads(li["checks_json"]) == {"over_ručne": True, "stiahnuté": False, "zhoda_mena": True, "zhoda_organizácie": True}
    assert not [c for c in db.contacts_for_person(eva) if c["kind"] == "profile_url"]
    assert len([q for q in searx.queries if "Eva" in q]) == 2  # dva dopyty, potom sa vzdá

    # ručné zadanie: iba URL profilu LinkedIn, nič sa nesťahuje; po forget sa osoba nedá znova uložiť
    assert add_manual_profile(cfg, db, eva, "https://cz.linkedin.com/in/eva-mala-9/?utm=x") == "https://www.linkedin.com/in/eva-mala-9"
    with pytest.raises(ValueError):
        add_manual_profile(cfg, db, eva, "https://www.facebook.com/eva")
    with pytest.raises(ValueError):
        add_manual_profile(cfg, db, eva, "https://www.linkedin.com/company/strojaren")
    forget(cfg, db, person_id=jana, reason="objection")
    assert db.get_person(jana) is None


def test_normalize_profile_url_and_url_digits():
    assert normalize_profile_url("http://linkedin.com/in/jan-novak/details/") == "https://www.linkedin.com/in/jan-novak"
    assert normalize_profile_url("https://www.linkedin.com/feed/") is None
    assert normalize_profile_url("javascript:alert(1)") is None
    assert normalize_profile_url("https://evil.com/linkedin.com/in/x") is None
    found = phones_in("<linkedin: https://www.linkedin.com/in/jan-0905123456> tel. 0905 123 456")
    assert [v for v, *_ in found] == ["+421905123456"] and found[0][1] > 50  # iba číslo za URL


def test_sole_trader_profile_is_not_matched_by_own_name():
    """Živnostník má v názve vlastné meno – zhoda organizácie by bola samozrejmá, profil sa neuloží."""
    from radar.people import profile_match

    person = {"full_name": "Tomáš Ukážkový", "org_name": "Mgr. Tomáš Ukážkový"}
    hit = [SearchResult(url="https://www.linkedin.com/in/tomas-ukazkovy-1", title="Tomáš Ukážkový – inžinier", snippet="Košice")]
    assert profile_match(person, hit) is None
    person = {"full_name": "Jana Nováková", "org_name": "Nováková Consulting s.r.o."}
    hit = [SearchResult(url="https://www.linkedin.com/in/jana-novakova", title="Jana Nováková – Nováková Consulting",
                        snippet="Consulting")]
    assert profile_match(person, hit)[0] == "https://www.linkedin.com/in/jana-novakova"


def test_agent_that_reads_nothing_is_not_a_test_and_repeated_search_opens_result(tmp_path):
    """Reálny beh (H1): rýchly model 7× zopakoval ten istý dopyt a nič neotvoril."""
    cfg, db, rec, router = setup(tmp_path)
    rec.responses["ExtractionResult"] = {"relevant": False, "claims": []}  # záchranná extrakcia: stránka bez tvrdení
    hid = db.create_hypothesis("H", "", "x")
    loop = [step(call("web_search", {"query": "obce"}, str(i))) for i in range(3)] + [step(call("finish", {"summary": "nič"}))]
    searx = FakeSearx({"obce": [SearchResult(url="https://nedostupne.sk/", title="X", snippet="s"),
                                SearchResult(url=URL, title="A", snippet="s")]})
    loop.insert(2, step(call("web_search", {"query": "obce"}, "x")))  # 1. otvorenie zlyhá → ďalšie opakovanie otvorí 2. výsledok
    rec.responses["tools"] = list(loop)
    stats = run_validate_search(cfg, db, router, searx, FakeFetcher({URL: DOC}), Deadline(1), ids=[hid])
    # druhé opakovanie (tretí rovnaký dopyt): kód otvorí prvý neprečítaný výsledok → agent prečítal zdroj, test platí
    assert stats["searched"] == 1 and json.loads(db.get_hypothesis(hid)["validation_json"])["agent"]["prečítané_zdroje"] == 1

    other = db.create_hypothesis("H2", "", "x")
    rec.responses["tools"] = [step(call("finish", {"summary": "nič"}))]
    stats = run_validate_search(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), ids=[other])
    assert stats == {"retry_later": 1}
    h = db.get_hypothesis(other)
    assert h["validation_state"] is None and h["validation_runs"] == 1  # vo fronte ide za ostatné
    rec.responses["tools"] = [step(call("finish", {"summary": "nič"}))]
    run_validate_search(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), ids=[other])
    info = json.loads(db.get_hypothesis(other)["validation_json"])["agent"]
    assert info["ok"] is False and "neprečítal" in info["chyba"]  # druhý neúspech: test neúplný, nie „otestovaná“


def test_agent_stopped_by_deadline_does_not_close_hypothesis(tmp_path, monkeypatch):
    """Reálny beh: nová dávka začala sekundy pred koncom limitu a H3 dostala verdikt bez hľadania."""
    import radar.validate as v

    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    rec.responses["tools"] = [step(call("web_search", {"query": "obce"}))]
    original = v.ResearchAgent.run
    monkeypatch.setattr(v.ResearchAgent, "run", lambda self, task, max_steps, system=None, deadline=None:
                        original(self, task, max_steps, system=system, deadline=Deadline(0)))
    stats = run_validate_search(cfg, db, router, FakeSearx(), FakeFetcher(), Deadline(1), ids=[hid])
    h = db.get_hypothesis(hid)
    assert stats == {"deferred": 1} and h["validation_state"] is None and h["validation_runs"] == 0


def test_validation_batch_verifies_and_clusters_only_its_own_claims(tmp_path):
    """Reálny beh: dávka 5 hypotéz najprv posudzovala celý zásobník z iných zdrojov (~1 h bez verdiktu)."""
    from radar.commands import stage_validate_cluster, stage_validate_verify

    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "malé obce")
    doc, _ = db.upsert_document(URL, "A", "agent", "t", None, "h", DOC)
    mine = db.insert_claim(doc, 0, "t", "Obce nemajú kapacitu.", "obce", "pain", QUOTE, origin="validation", hypothesis_hint=hid)
    other = db.insert_claim(doc, 0, "t", "Iné.", "obce", "pain", COUNTER)  # zásobník z iných zdrojov
    db.set_validation_searched(hid, {"agent": {"ok": True}})
    assert db.validation_claim_ids() == [mine]
    rec.responses["Entailment"] = {"verdict": "supported", "reason": "ok"}
    rec.responses["Assignment"] = {"decision": "existing", "hypothesis_id": hid}
    app = SimpleNamespace(cfg=cfg, db=db, router=router, embedder=None, verifier=lambda: router)
    assert stage_validate_verify(app, Deadline(1)) == {"verified": 1}
    assert db.get_claim(other)["status"] == "pending"  # zásobník čaká na bežný verify
    db.set_claim_verification(other, "verified", 100, "ok")
    stats = stage_validate_cluster(app, Deadline(1))
    assert stats["assigned_existing"] == 1
    assert db.validation_blocking(hid) == 0 and [e["id"] for e in db.evidence_for(hid)] == [mine]
    assert db.unassigned_count() == 1  # tvrdenie zo zásobníka nezaradené
    db.set_validation_result(hid, {"verdikt": "slabá"})
    assert stage_validate_verify(app, Deadline(1)) == {}  # bez tvrdení testu nič (nie celý zásobník)



def test_every_action_runs_its_stages_in_model_order(tmp_path, monkeypatch):
    from radar.workflows import ACTIONS

    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    expected = {
        "evidence": ["verify", "embed", "cluster", "validate-finish", "report"],
        "topics": ["collect", "screen", "extract", "verify-code", "source-types", "embed", "verify", "cluster", "report"],
        "critique": ["critique", "market"],
        "drafts": ["value", "drafts"],
        "org_cards": ["value", "org-cards"],  # v15: karty organizácií (zadanie 17. 10. 2026)
        "contacts": ["contacts", "profiles"],
        "gate1": ["gate1-fields", "gate1-sources", "gate1-check", "gate1-datasets", "gate1-bindings", "gate1-repro", "gate1-user",
                  "gate1-finish"],
        "gate1-input": ["gate1-user", "gate1-finish"],
        "gate2": ["gate2-cards", "gate2-plan", "gate2-backtest", "gate2-conclusion", "gate2-demo"],
        "cards": ["gate2-cards"],
        "bench": ["bench"],
        "themes": ["embed", "themes"],
        "catalog": ["catalog-sync"],
        "procurement": ["procurement-sync", "procurement-match"],  # M37: obstarávanie z TED
        "mail": ["mail-sync", "mail-extract"],  # M41: pošta (Gmail, iba čítanie)
        "eval": ["eval-all"],
    }
    for kind, stages in expected.items():
        calls = []
        app = SimpleNamespace(cfg=cfg, db=db, router=router)
        run_workflow(app, kind, 60, hypothesis_ids=[hid], run_stage=lambda a, s, dl: calls.append(s) or {})
        assert calls == stages, kind
    calls = []
    app = SimpleNamespace(cfg=cfg, db=db, router=router, collect_budget=None)
    result = run_workflow(app, "run-all", 60, run_stage=lambda a, s, dl: calls.append(s) or {})
    assert calls[0] == "discover" and calls[-2:] == ["report", "purge"] and len(result["stages"]) == len(calls)
    # lievik (funnel) vyberá krok podľa stavu hypotézy – vlastné testy v test_m19_funnel.py
    assert set(ACTIONS) == set(expected) | {"scout", "validate", "people", "run-all", "autopilot", "funnel"}

    # po skončení úlohy z GUI sa spustí ďalšia z poradia
    started = []
    monkeypatch.setattr("radar.jobs.start_next", lambda c, d: started.append(True))
    job = db.create_job("critique", {"minutes": 5})
    run_workflow(SimpleNamespace(cfg=cfg, db=db, router=router), "critique", 5, job_id=job,
                 run_stage=lambda a, s, dl: {})
    assert started == [True] and db.get_job(job)["status"] == "done"


def test_queue_replace_and_cancel(tmp_path):
    from radar.jobs import start_next, stop_all

    cfg, db, rec, router = setup(tmp_path)
    spawned, killed = [], []
    alive = {}

    def popen(cmd, **kw):
        spawned.append(cmd)
        pid = 1000 + len(spawned)
        alive[pid] = True
        return SimpleNamespace(pid=pid)

    is_alive = lambda pid: alive.get(pid, False)  # noqa: E731
    kill = lambda cmd, **kw: killed.append(cmd)  # noqa: E731
    first = start_job(cfg, db, "config.yaml", "validate", 480, popen=popen, alive=is_alive)
    queued = start_job(cfg, db, "config.yaml", "people", 120, [4, 11], popen=popen, alive=is_alive, mode="queue")
    assert db.get_job(queued)["status"] == "queued" and len(spawned) == 1
    assert start_next(cfg, db, popen=popen, alive=is_alive) is None  # prvá ešte beží
    alive[1001] = False  # prvá dobehla
    db.update_job(first, status="done")
    assert start_next(cfg, db, popen=popen, alive=is_alive) == queued
    assert spawned[1][-6:] == ["--minutes", "120", "--job", str(queued), "--hypotheses", "4,11"]  # argumenty z poradia
    assert db.get_job(queued)["status"] == "running"
    # zastaviť bežiacu a hneď spustiť inú
    third = start_job(cfg, db, "config.yaml", "scout", 90, popen=popen, alive=is_alive, mode="replace", run=kill)
    assert db.get_job(queued)["status"] == "stopped" and db.get_job(third)["status"] == "running" and killed
    later = start_job(cfg, db, "config.yaml", "evidence", 60, popen=popen, alive=is_alive, mode="queue")
    assert stop_all(db, run=kill) == 2  # bežiaca + poradie
    assert db.get_job(later)["status"] == "cancelled" and db.active_jobs() == []


def test_gpu_status_and_unload_with_fake_ollama(tmp_path):
    from radar.jobs import gpu_status, unload_gpu

    cfg, db, rec, router = setup(tmp_path)
    ps = {"models": [{"name": "radar-qwen3.6-27b:latest", "size": 20e9, "size_vram": 7e9, "expires_at": "2026-10-11T21:30:00"},
                     {"name": "radar-qwen3-embedding:latest", "size": 1e9, "size_vram": 0}]}
    get = lambda url, timeout: SimpleNamespace(json=lambda: ps)  # noqa: E731
    status = gpu_status(cfg, get)
    assert status[0] == {"model": "radar-qwen3.6-27b:latest", "vram_gb": 7.0, "na_gpu": "35%", "do": "21:30"}
    posted = []
    assert unload_gpu(cfg, get, lambda url, json, timeout: posted.append((url, json))) == ["radar-qwen3.6-27b:latest"]
    assert posted == [("http://localhost:11434/api/generate", {"model": "radar-qwen3.6-27b:latest", "keep_alive": 0})]

    def down(url, timeout):
        raise httpx.ConnectError("nebeží")

    assert gpu_status(cfg, down) is None


def test_validation_agent_that_only_reads_gets_claims_from_extraction(tmp_path):
    """Reálny beh (MoE 35B): 52× read_page, 0× record_evidence – citáty vytiahne extrakcia so značkou hypotézy."""
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "malé obce")
    rec.responses["tools"] = [step(call("web_search", {"query": "obce"})), step(call("read_page", {"url": URL}, "2")),
                              step(call("finish", {"summary": "prečítal som analýzu"}, "3"))]
    rec.responses["ExtractionResult"] = {"relevant": True, "claims": [
        {"problem": "Malé obce nemajú kapacitu.", "affected_party": "malé obce", "claim_type": "pain", "quote": QUOTE}]}
    stats = run_validate_search(cfg, db, router, FakeSearx({"obce": [SearchResult(url=URL, title="A", snippet="")]}),
                                FakeFetcher({URL: DOC}), Deadline(1), ids=[hid])
    assert stats["claims_from_pages"] == 1 and stats["claims"] == 1
    claim = db.hinted_claims(hid)[0]
    assert claim["origin"] == "validation" and claim["quote"] == QUOTE and claim["status"] == "pending"
    assert rec.models_for("ExtractionResult") == ["radar-qwen3.5-9b"]  # extrakcia na rýchlom modeli
    info = json.loads(db.get_hypothesis(hid)["validation_json"])["agent"]
    assert info["z_prečítaných_stránok"] == 1


def test_validation_extracts_after_all_agents_of_batch(tmp_path):
    """Agent (35B) a extrakcia (9B) sa na 8 GB VRAM nezmestia naraz: extrakcia beží až po agentoch celej dávky."""
    cfg, db, rec, router = setup(tmp_path)
    url2 = "https://www.example-urad.sk/sprava-2.html"
    h1 = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "malé obce")
    h2 = db.create_hypothesis("Obce nestíhajú eurofondy", "", "malé obce")
    rec.responses["tools"] = [step(call("read_page", {"url": URL})), step(call("finish", {"summary": "a"}, "2")),
                              step(call("read_page", {"url": url2}, "3")), step(call("finish", {"summary": "b"}, "4"))]
    rec.responses["ExtractionResult"] = {"relevant": True, "claims": [
        {"problem": "Malé obce nemajú kapacitu.", "affected_party": "malé obce", "claim_type": "pain", "quote": QUOTE}]}
    stats = run_validate_search(cfg, db, router, FakeSearx({}), FakeFetcher({URL: DOC, url2: DOC}), Deadline(1), ids=[h1, h2])
    assert stats["searched"] == 2 and stats["claims_from_pages"] == 2
    kinds = [s for s, *_ in rec.calls]
    assert kinds.index("ExtractionResult") > max(i for i, k in enumerate(kinds) if k == "tools")
    assert all(db.get_hypothesis(h)["validation_state"] == "searched" for h in (h1, h2))


def test_interrupted_job_stalls_queue_until_resumed_or_restarted(tmp_path):
    """Reštart počítača počas akcie: akcia je prerušená a poradie stojí; GUI ponúkne pokračovať a spustiť znova."""
    from radar.jobs import interrupted_jobs, queue_stalled, restart_job, start_next

    cfg, db, rec, router = setup(tmp_path)
    spawned, alive = [], {}

    def popen(cmd, **kw):
        spawned.append(cmd)
        pid = 2000 + len(spawned)
        alive[pid] = True
        return SimpleNamespace(pid=pid)

    is_alive = lambda pid: alive.get(pid, False)  # noqa: E731
    first = start_job(cfg, db, "config.yaml", "validate", 240, [7], popen=popen, alive=is_alive)
    queued = start_job(cfg, db, "config.yaml", "themes", 15, popen=popen, alive=is_alive, mode="queue")
    assert not queue_stalled(db, is_alive)
    alive[2001] = False  # počítač sa reštartoval: proces zmizol bez zápisu výsledku
    assert queue_stalled(db, is_alive) and db.get_job(first)["status"] == "interrupted"
    assert [j["id"] for j in interrupted_jobs(db)] == [first]
    assert start_next(cfg, db, popen=popen, alive=is_alive) == queued  # „Pokračovať v poradí“
    again = restart_job(cfg, db, "config.yaml", first, popen=popen, alive=is_alive)
    assert db.get_job(again)["status"] == "queued"  # niečo beží → do poradia, s rovnakými parametrami
    assert json.loads(db.get_job(again)["args_json"])["hypotheses"] == [7]
    assert interrupted_jobs(db) == []  # novšia úloha tej istej akcie už existuje


def test_job_command_queues_like_gui_and_waits_for_end(tmp_path, monkeypatch):
    """Plánovač úloh: `radar job run-all --mode queue --wait` – úloha v Správcovi akcií, čaká aj v poradí."""
    import argparse

    from radar.commands import cmd_job

    cfg, db, rec, router = setup(tmp_path)
    created = []

    def fake_start(cfg_, db_, config, kind, minutes, ids, mode="start"):
        created.append((kind, minutes, ids, mode))
        return db_.create_job(kind, {"minutes": minutes}, status="queued")

    monkeypatch.setattr("radar.jobs.start_job", fake_start)
    monkeypatch.setattr("radar.jobs.refresh_jobs", lambda db_: None)
    app = SimpleNamespace(cfg=cfg, db=db)
    args = argparse.Namespace(kind="run-all", minutes=480, hypotheses=None, mode="queue", wait=True)
    ticks = []

    def sleep(_):
        ticks.append(1)
        db.update_job(1, status="running" if len(ticks) == 1 else "done")

    assert cmd_job(app, args, sleep=sleep) == 0 and len(ticks) == 2
    assert created == [("run-all", 480, None, "queue")]


def test_long_runs_preload_whole_package_at_start():
    """Úprava kódu počas behu nesmie zmiešať verzie: workflow a run-all si načítajú všetky moduly pri štarte."""
    from radar.util import LONG_COMMANDS, preload_modules

    loaded = preload_modules()
    assert {"radar.validate", "radar.gate1", "radar.gate2", "radar.themes", "radar.catalogs", "radar.strength"} <= set(loaded)
    assert not any(m.startswith("radar.gui") for m in loaded) and "radar.__main__" not in loaded
    assert set(LONG_COMMANDS) == {"workflow", "run-all"}


def test_people_workflow_leaves_time_for_people_after_orgs(tmp_path):
    """Akcia #19: orgs minul celý čas rýchlej fázy a krok people sa preskočil; orgs má najviac polovicu."""
    from radar.workflows import _people

    cfg, db, rec, router = setup(tmp_path)
    budgets = {}
    _people(SimpleNamespace(db=db), Deadline(100), lambda stage, dl: budgets.setdefault(stage, dl.remaining_minutes()),
            None, lambda **kw: None)
    assert list(budgets) == ["prepare-next", "orgs", "contact-search", "people", "people-rank", "contacts"]
    # v10: orgs 35 % a hľadanie kontaktov podľa hypotézy 30 % rýchlej fázy; people dostane zvyšok (najmenej 35 %)
    assert 22 <= budgets["orgs"] <= 23 and 19 <= budgets["contact-search"] <= 20 and 64 <= budgets["people"] <= 65.1


def test_autopilot_plans_round_by_work_and_time_and_continues(tmp_path):
    """„Nechať bežať všetko“: kolo iba z akcií s prácou, úseky do zvyšného času; od M22 najprv vetvy vybraných hypotéz
    a nový zber v pevnom podiele kola (audit P1 6); zber ľudí iba pre hypotézy zapnuté na Bránu 0 (bez automatického
    výberu); akcia, ktorá sa 2 kolá nezmestí, ide dopredu; na konci ďalší autopilot s rovnakým koncom."""
    from datetime import datetime, timedelta, timezone

    from radar.autopilot import pending_work, plan_round

    cfg, db, rec, router = setup(tmp_path)
    h1 = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "malé obce")  # neotestovaná → validate
    h2 = db.create_hypothesis("Iná hypotéza", "", "firmy")
    db.set_people_selection(h1, True)
    org, _ = db.upsert_organization("Obec Fiktívna", "obec fiktivna", "evidence")
    db.insert_person(org, "Peter Vzorový", "peter vzorovy", "https://obec-fikt.sk/urad", "Peter Vzorový, prednosta", h1)
    work = pending_work(cfg, db)
    assert {"scout", "topics", "validate", "people", "contacts", "gate1"} <= set(work) and "gate2" not in work
    now = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
    plan = plan_round(cfg, db, "config.yaml", now + timedelta(minutes=185), 1, now=now)
    kinds = [(p["akcia"], p["minúty"]) for p in plan["naplánované"]]
    assert kinds == [("gate1", 60), ("people", 60), ("contacts", 19), ("scout", 46)]  # zber: 25 % z 185 min
    assert plan["preskočené"]["validate"] == "nezmestí sa do času" and plan["preskočené"]["gate2"] == "bez práce"
    assert plan["rezerva_na_zber_min"] == 46 and plan["prioritné_hypotézy"] == [h1]
    queued = db.queued_jobs()
    people_job = [j for j in queued if j["kind"] == "people"][0]
    assert json.loads(people_job["args_json"])["hypotheses"] == [h1]  # iba zapnuté, h2 nie
    assert db.get_hypothesis(h2)["people_enabled"] == 0
    nxt = json.loads(queued[-1]["args_json"])
    assert queued[-1]["kind"] == "autopilot" and nxt["round"] == 2 and nxt["until"] == "2026-10-12T23:05:00+00:00"
    late = plan_round(cfg, db, "config.yaml", now + timedelta(minutes=5), 2, now=now)
    assert late["naplánované"] == [] and late["ďalšie_kolo"] is None  # čas vypršal → reťaz končí
    aged = plan_round(cfg, db, "config.yaml", now + timedelta(minutes=185), 3, now=now)  # validate 2 kolá čakal
    assert aged["naplánované"][0]["akcia"] == "validate" and "prednosť po čakaní" in aged["naplánované"][0]["dôvod"]


def test_autopilot_job_reads_its_end_time_and_plans(tmp_path, monkeypatch):
    from radar.workflows import run_workflow

    cfg, db, rec, router = setup(tmp_path)
    started = []
    monkeypatch.setattr("radar.jobs.start_next", lambda cfg_, db_, *a, **k: started.append(1))  # žiadny ostrý proces
    job = db.create_job("autopilot", {"minutes": 480, "hypotheses": [], "config": "config.yaml"})
    app = SimpleNamespace(cfg=cfg, db=db, router=router)
    run_workflow(app, "autopilot", 480, job_id=job, run_stage=lambda app, stage, dl: {})
    plan = json.loads(db.get_job(job)["progress_json"])["autopilot"]
    assert plan["kolo"] == 1 and [p["akcia"] for p in plan["naplánované"]][:2] == ["scout", "topics"]
    assert [j["kind"] for j in db.queued_jobs()][-1] == "autopilot" and started == [1]


def test_real_spawn_refuses_config_of_another_database(tmp_path):
    """Test spustil ostrý proces: relatívna cesta config.yaml ukazovala na skutočnú databázu, nie na testovaciu."""
    import subprocess

    from radar.jobs import JobConfigMismatch, _spawn

    cfg, db, rec, router = setup(tmp_path)
    job = db.create_job("scout", {"minutes": 60, "hypotheses": [], "config": "config.yaml"}, status="queued")
    with pytest.raises(JobConfigMismatch):
        _spawn(cfg, db, job, subprocess.Popen)
    assert db.get_job(job)["status"] == "queued"


def test_code_only_checks_run_even_when_fast_phase_ran_out(tmp_path):
    """Noc 13. 10.: verify-code sa preskočil, lebo extrakcia minula celý čas rýchlej fázy."""
    from radar.workflows import _scout

    cfg, db, rec, router = setup(tmp_path)
    ran = []

    def run(stage, dl):
        if dl.expired():
            return False
        ran.append(stage)
        return True

    app = SimpleNamespace(cfg=cfg, db=db)
    _scout(app, Deadline(0), run, None, lambda **kw: None)
    assert "verify-code" in ran and "source-types" in ran and "extract" not in ran


def test_dashboard_eta_from_history_and_queue(tmp_path):
    """Odhad konca: limit × medián pomeru skutočné trvanie / limit z histórie; poradie kumulatívne."""
    from datetime import datetime, timedelta, timezone

    from radar.eta import expected_minutes, local, schedule, typical_minutes

    cfg, db, rec, router = setup(tmp_path)
    t0 = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    for i, mins in enumerate((15, 20, 25)):  # validate s limitom 60 zvyčajne skončí za ~20 min
        j = db.create_job("validate", {"minutes": 60})
        db.update_job(j, status="done", started_at=(t0 + timedelta(hours=i)).isoformat(),
                      finished_at=(t0 + timedelta(hours=i, minutes=mins)).isoformat())
    assert typical_minutes(db, "validate") == 20 and round(expected_minutes(db, "validate", 60)) == 20
    assert expected_minutes(db, "scout", 45) == 45  # bez histórie = limit
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    run = db.create_job("validate", {"minutes": 60})
    db.update_job(run, status="running")
    db.update_job(run, started_at=(now - timedelta(minutes=5)).isoformat())  # „running“ nastaví skutočný štart
    db.create_job("scout", {"minutes": 45}, status="queued")
    plan = schedule(db, now)
    assert plan[0]["koniec_odhad"] == now + timedelta(minutes=15) and plan[0]["koniec_najneskôr"] == now + timedelta(minutes=55)
    assert plan[1]["začiatok"] == plan[0]["koniec_odhad"] and plan[1]["koniec_odhad"] == plan[1]["začiatok"] + timedelta(minutes=45)
    assert ":" in local(now.isoformat(), now)


def test_scheduled_start_resumes_stalled_queue_and_interrupted_jobs(tmp_path):
    """Reálne 13. 10. 2026: aktualizácia aplikácie o 23:02 ukončila beh; ďalší štart má najprv pokračovať."""
    from radar.jobs import resume_after_restart

    cfg, db, rec, router = setup(tmp_path)
    spawned, alive = [], {}

    def popen(cmd, **kw):
        spawned.append(cmd)
        pid = 2000 + len(spawned)
        alive[pid] = True
        return SimpleNamespace(pid=pid)

    is_alive = lambda pid: alive.get(pid, False)  # noqa: E731
    crashed = start_job(cfg, db, "config.yaml", "validate", 60, popen=popen, alive=is_alive)
    waiting = start_job(cfg, db, "config.yaml", "people", 60, popen=popen, alive=is_alive, mode="queue")
    old_pilot = db.create_job("autopilot", {"minutes": 480}, status="interrupted")
    alive[2001] = False  # počítač sa reštartoval
    out = resume_after_restart(cfg, db, "config.yaml", skip_kind="autopilot", popen=popen, alive=is_alive)
    assert out["poradie"] == waiting and db.get_job(waiting)["status"] == "running"
    assert db.get_job(crashed)["status"] == "interrupted" and len(out["znova"]) == 1  # validate znova do poradia
    assert db.get_job(out["znova"][0])["kind"] == "validate" and db.get_job(out["znova"][0])["status"] == "queued"
    assert db.get_job(old_pilot)["status"] == "interrupted"  # nový autopilot ho nahradí


def test_run_now_resumes_queue_and_does_not_stack_autopilots(tmp_path):
    """23. 10. 2026: „Spustiť všetko hneď“ (GUI) aj nočný beh (`job autopilot --resume`) – rozbehne zastavené poradie
    a zaradí autopilot; keď už jeden čaká, iba mu predĺži koniec (dve reťaze kôl by plánovali tú istú prácu dvakrát)."""
    import argparse
    from datetime import datetime, timedelta, timezone

    from radar.commands import cmd_job
    from radar.jobs import run_now

    cfg, db, rec, router = setup(tmp_path)
    spawned, alive = [], {}

    def popen(cmd, **kw):
        spawned.append(cmd)
        pid = 3000 + len(spawned)
        alive[pid] = True
        return SimpleNamespace(pid=pid)

    is_alive = lambda pid: alive.get(pid, False)  # noqa: E731
    start_job(cfg, db, "config.yaml", "validate", 60, popen=popen, alive=is_alive)
    waiting = start_job(cfg, db, "config.yaml", "cards", 40, popen=popen, alive=is_alive, mode="queue")
    alive[3001] = False  # počítač spal / reštart: poradie stojí
    first = run_now(cfg, db, "config.yaml", 480, popen=popen, alive=is_alive)
    assert first["poradie"] == waiting and db.get_job(waiting)["status"] == "running"
    assert first["nový"] and db.get_job(first["autopilot"])["status"] == "queued"  # beží karta → autopilot do poradia
    again = run_now(cfg, db, "config.yaml", 600, popen=popen, alive=is_alive)
    assert again["autopilot"] == first["autopilot"] and not again["nový"]
    assert json.loads(db.get_job(first["autopilot"])["args_json"])["minutes"] == 600
    assert len(first["znova"]) == 1 and again["znova"] == []  # prerušená validate znova iba raz
    assert [j["kind"] for j in db.queued_jobs()] == ["validate", "autopilot"]

    stop_job(db, first["autopilot"])  # ďalšie kolo reťaze má pevný koniec – predĺži sa, neskráti
    chain = db.create_job("autopilot", {"minutes": 5, "until": "2026-01-01T00:00:00+00:00", "round": 3}, status="queued")
    assert run_now(cfg, db, "config.yaml", 60, popen=popen, alive=is_alive)["autopilot"] == chain
    until = datetime.fromisoformat(json.loads(db.get_job(chain)["args_json"])["until"])
    assert until > datetime.now(timezone.utc) + timedelta(minutes=55)
    later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    db.update_job(chain, args_json={"minutes": 5, "until": later, "round": 3})
    run_now(cfg, db, "config.yaml", 60, popen=popen, alive=is_alive)
    assert json.loads(db.get_job(chain)["args_json"])["until"] == later

    app = SimpleNamespace(cfg=cfg, db=db)  # nočný beh z Plánovača: rovnaké pravidlo, žiadna druhá reťaz
    args = argparse.Namespace(kind="autopilot", minutes=480, hypotheses=None, mode="queue", wait=False, resume=False)
    assert cmd_job(app, args) == 0 and [j["id"] for j in db.queued_jobs()] == [first["znova"][0], chain]


def test_read_page_offers_candidate_quotes_for_reading_only_models():
    """9B a MoE 35B stránky čítajú, ale record_evidence takmer nevolajú – kód ponúkne vety s opisom problému."""
    from radar.agent import TOOLS, ResearchAgent, candidate_quotes

    text = ("Úvod k obstarávaniu v obciach. Malé obce nestíhajú administratívne zvládnuť verejné obstarávanie a "
            "chýba im právnik, preto sa zákazky oneskorujú o 3 mesiace. Kontakt: starosta@obec.sk, tel. 0905 123 456 "
            "chýba. Počasie bolo v októbri pekné a teplé, bez zrážok a bez problémov na cestách v regióne.")
    cands = candidate_quotes(text, "Obstarávanie v malých obciach")
    assert cands[0].startswith("Malé obce nestíhajú") and not any("@" in c for c in cands)
    agent = ResearchAgent.__new__(ResearchAgent)
    agent.pages, agent.tools, agent.topic = {"https://x.sk": (1, [text])}, TOOLS, "Obstarávanie v malých obciach"
    out = agent.read_page("https://x.sk", 0)
    assert "Candidate quotes picked by code" in out and "1. Malé obce nestíhajú" in out
    agent.tools = [t for t in TOOLS if t["function"]["name"] != "record_evidence"]  # agent bez ukladania citátov
    assert "Candidate quotes" not in agent.read_page("https://x.sk", 0)


def test_autopilot_action_hours_window(tmp_path):
    """Nápad z improvements_logs: poradie akcií podľa času (napr. zber ľudí iba cez deň)."""
    from datetime import datetime, timedelta, timezone

    from radar.autopilot import in_hours, plan_round

    when = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    h = when.astimezone().hour
    assert in_hours(None, when) and in_hours([h, h + 1], when) and not in_hours([h + 1, h + 2], when)
    assert in_hours([h + 2, h + 1], when) and not in_hours([h + 1, h], when)  # okno cez polnoc (všetko okrem hodiny)
    cfg, db, rec, router = setup(tmp_path)
    cfg.workflows.autopilot_hours = {"scout": [(h + 2) % 24, (h + 3) % 24]}
    plan = plan_round(cfg, db, "config.yaml", when + timedelta(minutes=120), 1, now=when)
    assert plan["preskočené"]["scout"].startswith("mimo času") and plan["naplánované"][0]["akcia"] == "topics"


def test_search_down_stops_validation_without_counting_attempts(tmp_path):
    """Úloha #108: Docker nebežal, agent 6× zopakoval hľadanie a hypotéze sa zarátal neúspešný pokus."""
    import httpx

    from radar.config import SearchConfig
    from radar.search import SearchUnavailable, SearxClient
    from radar.validate import run_validate_search

    def refused(request):
        raise httpx.ConnectError("[WinError 10061] No connection could be made", request=request)

    searx = SearxClient(SearchConfig(delay_s=0), "test")
    searx.http = httpx.Client(transport=httpx.MockTransport(refused))
    with pytest.raises(SearchUnavailable, match="SearXNG nebeží"):
        searx.search("obce obstarávanie")
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "", "malé obce")
    rec.responses["tools"] = [SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
        id="1", function=SimpleNamespace(name="web_search", arguments='{"query": "obce"}'))])] * 3
    with pytest.raises(SearchUnavailable):
        run_validate_search(cfg, db, router, searx, None, Deadline(1), ids=[hid])
    h = db.get_hypothesis(hid)
    assert h["validation_runs"] == 0 and h["validation_state"] is None  # nedotknutá, ostáva vo fronte

