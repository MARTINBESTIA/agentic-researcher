"""Testy rozšírení: projekty, prebratý text, zlúčenie hypotéz, embeddingy, triáž RSS,
zoznamové stránky, kódovanie, miera istoty a konfigurácia. Bez modelu a bez internetu."""

import sqlite3
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest

from radar.cluster import run_cluster
from radar.collect import gather_candidates
from radar.config import Config, EmbeddingsConfig, Feed, FetchConfig, LLMConfig, Project, load_config
from radar.db import DB
from radar.embed import Embedder, rank_hypotheses, similar_pairs
from radar.fetch import Fetcher
from radar.llm import LLM, LLMError
from radar.market import confidence
from radar.report import evidence_strength
from radar.util import Deadline, canonical_url

QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty z eurofondov meškajú."


class NoLLM:
    """Model, ktorý sa nesmie zavolať."""

    def chat_json(self, *args, **kwargs):
        raise AssertionError("model sa nemal volať")


def add_claim(db: DB, url: str, quote: str = QUOTE, topic: str | None = None) -> int:
    doc_id, _ = db.upsert_document(url, "Titulok", "search", topic, None, url, "text " * 100)
    claim_id = db.insert_claim(doc_id, 0, topic, "Malé obce nestíhajú obstarávanie.", "malé obce", "pain", quote)
    db.set_claim_verification(claim_id, "verified", 100, "ok")
    return claim_id


def test_canonical_url_strips_tracking():
    assert canonical_url("https://www.nku.gov.sk/sk/-/clanok?utm_source=rss&utm_medium=feed") == "https://www.nku.gov.sk/sk/-/clanok"
    assert canonical_url("https://X.sk/a?id=5&utm_campaign=x#top") == "https://x.sk/a?id=5"
    assert canonical_url("nie-je-url") == "nie-je-url"


def test_load_config_resolves_paths_relative_to_config(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "proj").mkdir()
    path = tmp_path / "cfg" / "config.yaml"
    path.write_text(
        "db_path: data/x.sqlite\nprojects:\n  - name: p\n    path: ../proj\n    title: T\n", encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.db_path == str(tmp_path / "cfg" / "data" / "x.sqlite")
    assert cfg.projects[0].path == str((tmp_path / "proj").resolve())
    assert [t.name for t in cfg.all_topics()] == []  # projekt bez dopytov nepridá tému


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER, topic TEXT,"
                 " problem TEXT, affected_party TEXT, claim_type TEXT, quote TEXT, numbers_json TEXT, origin TEXT,"
                 " status TEXT, quote_score REAL, verify_note TEXT, created_at TEXT, verified_at TEXT)")
    conn.execute("CREATE TABLE hypotheses (id INTEGER PRIMARY KEY, title TEXT, description TEXT, segment TEXT,"
                 " status TEXT, created_at TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    db = DB(path)
    assert "duplicate_of" in {r["name"] for r in db.conn.execute("PRAGMA table_info(claims)")}
    assert "project" in {r["name"] for r in db.conn.execute("PRAGMA table_info(hypotheses)")}


def test_syndicated_quote_counts_once(tmp_path):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), embeddings=EmbeddingsConfig(enabled=False))
    db = DB(cfg.db_path)
    original = add_claim(db, "https://portal-a.sk/clanok")
    hid = db.create_hypothesis("Kapacita obcí", "Popis", "malé obce")
    db.assign_claim(hid, original)
    # Tá istá tlačová správa na inom portáli, s typografickými rozdielmi.
    copy = add_claim(db, "https://portal-b.sk/spravy", QUOTE.replace(",", " ,").replace("Malé", "malé"))

    stats = run_cluster(cfg, db, NoLLM(), Deadline(1))
    assert stats["duplicates"] == 1
    assert db.get_claim(copy)["duplicate_of"] == original
    assert [e["id"] for e in db.evidence_for(hid)] == [original]
    assert len(db.evidence_for(hid, include_duplicates=True)) == 2
    assert evidence_strength(db.evidence_for(hid))[3] == 1  # jedna nezávislá doména, nie dve


def test_cluster_offers_project_hypothesis(tmp_path):
    project = Project(name="eurofondy-radar", title="Kraje nevidia, kde sa projekty zasekávajú", segment="VÚC",
                      queries=["čerpanie eurofondov"])
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), projects=[project], embeddings=EmbeddingsConfig(enabled=False))
    db = DB(cfg.db_path)
    claim = add_claim(db, "https://example.sk/a", topic=project.topic)
    seen = {}

    class FakeLLM:
        def chat_json(self, messages, model_cls, **kwargs):
            seen["listing"] = messages[-1]["content"]
            hid = db.project_hypothesis("eurofondy-radar")["id"]
            return model_cls(decision="existing", hypothesis_id=hid)

    stats = run_cluster(cfg, db, FakeLLM(), Deadline(1))
    hyp = db.project_hypothesis("eurofondy-radar")
    assert stats == {"project_created": 1, "assigned_existing": 1}
    assert hyp["status"] == "watch"
    assert "[existing project]" in seen["listing"]
    assert db.hypothesis_of_claim(claim) == hyp["id"]

    # Druhý beh: text projektu sa zosúladí s config.yaml, ručne nastavený stav zostane.
    db.set_hypothesis_status(hyp["id"], "interviewing")
    project.title = "Nový názov"
    assert run_cluster(cfg, db, NoLLM(), Deadline(1)) == {"project_synced": 1}
    hyp = db.project_hypothesis("eurofondy-radar")
    assert (hyp["title"], hyp["status"]) == ("Nový názov", "interviewing")


def test_merge_hypotheses(tmp_path):
    db = DB(tmp_path / "r.sqlite")
    a, b = db.create_hypothesis("A", "", ""), db.create_hypothesis("B", "", "")
    claim = add_claim(db, "https://example.sk/a")
    db.assign_claim(a, claim)
    db.add_note(a, "rozhovor")
    assert db.merge_hypotheses(a, b) == 1
    assert db.get_hypothesis(a)["status"] == "rejected"
    assert db.hypothesis_of_claim(claim) == b
    assert [n["text"] for n in db.notes_for(b)][0] == "rozhovor"
    with pytest.raises(ValueError):
        db.merge_hypotheses(b, b)


class FakeEmbeddingsAPI:
    """Vektor podľa kľúčového slova: obce / účtovníctvo / ostatné."""

    def __init__(self):
        self.calls = 0

    def create(self, model, input):
        self.calls += 1

        def vec(text):
            text = text.lower()
            return [1.0, 0.1, 0.0] if "obc" in text else [0.0, 1.0, 0.1] if "účtov" in text else [0.0, 0.1, 1.0]

        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=vec(t)) for i, t in enumerate(input)])


def test_embeddings_rank_cache_and_similar_pairs(tmp_path):
    db = DB(tmp_path / "r.sqlite")
    embedder = Embedder(EmbeddingsConfig(), LLMConfig(), db)
    api = FakeEmbeddingsAPI()
    embedder.client = SimpleNamespace(embeddings=api)
    ids = [
        db.create_hypothesis("Účtovné kancelárie prepisujú doklady", "", "účtovníci"),
        db.create_hypothesis("Malé obce nestíhajú obstarávanie", "", "obce"),
        db.create_hypothesis("Obce nemajú odborníkov na obstarávanie", "", "obce"),
    ]
    hyps = db.active_hypotheses()
    claim = db.get_claim(add_claim(db, "https://example.sk/a"))
    ranked = rank_hypotheses(embedder, claim, hyps, k=2)
    assert {h["id"] for h in ranked} == {ids[1], ids[2]}
    calls = api.calls
    rank_hypotheses(embedder, claim, hyps, k=2)
    assert api.calls == calls  # vektory sú v databáze, model sa znova nevolá
    assert [(a, b) for a, b, _ in similar_pairs(embedder, hyps, 0.9)] == [(ids[1], ids[2])]


def test_consolidate_merges_confirmed_similar_hypotheses(tmp_path):
    project = Project(name="obce-radar", title="Malé obce nestíhajú obstarávanie", segment="obce")
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), projects=[project])
    db = DB(cfg.db_path)
    embedder = Embedder(cfg.embeddings, cfg.llm, db)
    embedder.client = SimpleNamespace(embeddings=FakeEmbeddingsAPI())
    run_cluster(cfg, db, NoLLM(), Deadline(1))  # založí hypotézu projektu
    own = db.project_hypothesis("obce-radar")["id"]
    other = db.create_hypothesis("Obce nemajú odborníkov na obstarávanie", "", "obce")
    accounting = db.create_hypothesis("Účtovné kancelárie prepisujú doklady", "", "účtovníci")
    db.assign_claim(other, add_claim(db, "https://example.sk/a"))
    asked = []

    class MergeLLM:
        def chat_json(self, messages, model_cls, **kwargs):
            asked.append(messages[-1]["content"])
            return model_cls(same=True, title="Nový názov", segment="obce")

    stats = run_cluster(cfg, db, MergeLLM(), Deadline(1), embedder)
    assert stats["merged"] == 1 and len(asked) == 1  # účtovníci nie sú podobní, model sa na nich nepýta
    assert db.get_hypothesis(other)["status"] == "rejected"
    assert db.project_hypothesis("obce-radar")["title"] == project.title  # projekt si nechá názov z config.yaml
    assert len(db.evidence_for(own)) == 1
    assert db.get_hypothesis(accounting)["status"] == "new"
    assert run_cluster(cfg, db, NoLLM(), Deadline(1), embedder) == {"project_synced": 1}  # už posúdené dvojice sa nepýta


def test_rss_triage_keeps_selected_entries(tmp_path, monkeypatch):
    entries = [
        {"link": "https://news.sk/sport?utm_source=rss", "title": "Futbal", "summary": "Výsledky zápasu"},
        {"link": "https://news.sk/obce?utm_source=rss", "title": "Obciam chýbajú úradníci", "summary": "<p>Prieskum</p>"},
    ]
    monkeypatch.setattr("radar.collect.feedparser.parse", lambda url, agent=None: SimpleNamespace(entries=entries))
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), feeds=[Feed(name="Správy", url="https://news.sk/rss")])
    db = DB(cfg.db_path)

    class TriageLLM:
        def chat_json(self, messages, model_cls, **kwargs):
            assert "Obciam chýbajú úradníci – Prieskum" in messages[-1]["content"]  # HTML z perexu je preč
            return model_cls(selected=[1])

    stats = Counter()
    cands = gather_candidates(cfg, db, None, None, TriageLLM(), stats)
    assert cands == [("https://news.sk/obce", "rss", "Správy", None)]
    assert stats["triage_dropped"] == 1

    class BrokenLLM:
        def chat_json(self, *args, **kwargs):
            raise LLMError("nefunguje")

    cands = gather_candidates(cfg, db, None, None, BrokenLLM(), Counter())
    assert len(cands) == 2  # pri chybe triáže sa nič nestratí


def mock_fetcher(routes: dict[str, httpx.Response]) -> Fetcher:
    fetcher = Fetcher(FetchConfig(per_domain_delay_s=0))

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    fetcher.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return fetcher


def test_links_from_listing_page():
    html = """<html><body>
      <a href="/sk/financie/hodnota-za-peniaze/blogy-uhp/2026/lieky/">Blog</a>
      <a href='https://www.mfsr.sk/files/sk/revizia.pdf?utm_source=x'>PDF</a>
      <a href="/sk/financie/hodnota-za-peniaze/blogy-uhp/2026/lieky/#komentare">duplicitný odkaz</a>
      <a href="/kontakty/">Kontakty</a>
      <a href="/files/sk/druha.pdf">PDF 2</a>
    </body></html>"""
    url = "https://www.mfsr.sk/sk/financie/hodnota-za-peniaze/"
    fetcher = mock_fetcher({url: httpx.Response(200, text=html, headers={"content-type": "text/html"})})
    links = fetcher.links(url, r"(blogy-uhp/20\d\d/|\.pdf$)", limit=2)
    assert links == [
        "https://www.mfsr.sk/sk/financie/hodnota-za-peniaze/blogy-uhp/2026/lieky/",
        "https://www.mfsr.sk/files/sk/revizia.pdf",
    ]


def test_html_in_windows_1250_is_decoded():
    paragraph = "Žilinský kraj rieši nedostatok úradníkov v malých obciach, ktoré nestíhajú agendu. " * 6
    html = (
        '<html><head><meta charset="windows-1250"><title>Obce</title></head><body><article>'
        f"<h1>Obce</h1><p>{paragraph}</p><p>{paragraph}</p></article></body></html>"
    ).encode("cp1250")
    url = "https://obce.sk/clanok"
    fetcher = mock_fetcher({url: httpx.Response(200, content=html, headers={"content-type": "text/html"})})
    page = fetcher.fetch(url)
    assert page is not None and "Žilinský kraj" in page.text and "nestíhajú" in page.text


def test_market_unit_must_appear_in_quote():
    from radar.market import unit_in_quote

    assert not unit_in_quote("€/obyvateľ/rok", "Krásno nad Kysucou okres Čadca | 5,5")  # skóre, nie eurá
    assert unit_in_quote("€/rok", "výdavky dosiahli 1,4 mil. eur ročne")
    assert unit_in_quote("%", "podiel tvorí 15,9 %")
    assert unit_in_quote("obcí", "Na Slovensku je 2 890 obcí.")  # pri počtoch sa jednotka nevyžaduje


def test_market_reads_chunks_about_the_quantity():
    from radar.market import relevant_chunks

    chunks = ["Úvod k rozpočtu štátu. " * 20, "Dlh a deficit. " * 20, "Na Slovensku je 2 890 obcí; v obciach žije...", "Záver."]
    # Časť 2 má "Slovensku", "obcí" aj "obciach"; ostatné nič, pri zhode vyhráva skoršia časť.
    assert relevant_chunks(chunks, "Počet obcí na Slovensku") == [0, 2]
    assert relevant_chunks(chunks[:2], "počet obcí") == [0, 1]


def test_market_confidence_labels():
    data = {"status": "data"}
    assumption = {"status": "assumption"}
    assert confidence([data, data], 100, 200)["label"] == "vyššia"
    assert confidence([data, assumption], 100, 500)["label"] == "stredná"
    assert confidence([data, assumption, assumption], 100, 200)["label"] == "nízka"
    assert confidence([data, assumption], 100, 5000)["label"] == "nízka"
    assert confidence([data, {"status": "missing"}], None, None)["label"] == "žiadna"


def test_agent_stance_and_repeated_search(tmp_path):
    from radar.agent import ResearchAgent
    from radar.fetch import FetchedPage
    from radar.search import SearchResult

    cfg = Config(db_path=str(tmp_path / "r.sqlite"))
    db = DB(cfg.db_path)
    searches = []
    searx = SimpleNamespace(search=lambda q, limit=None: searches.append(q) or [SearchResult("https://a.sk/x", "A", "...")])
    text = "Faktúra sa u nás spracuje za 40 sekúnd vďaka automatizácii. " * 10
    fetcher = SimpleNamespace(fetch=lambda url: FetchedPage(url, url, "A", text, "h"))
    agent = ResearchAgent(cfg, db, None, searx, fetcher, "Účtovné kancelárie")
    agent.web_search("prepis faktúr")
    assert "already made this search" in agent.web_search("Prepis  faktúr")
    assert len(searches) == 1
    agent.read_page("https://a.sk/x")
    agent.dispatch("record_evidence", '{"url": "https://a.sk/x", "problem": "Riešenie", "quote": "spracuje za 40 sekúnd", '
                   '"stance": "existing_solution"}')
    claim = db.get_claim(agent.claim_ids[0])
    assert claim["claim_type"] == "existing_solution"
    hid = db.create_hypothesis("H", "", "")
    db.set_claim_verification(claim["id"], "verified", 100, "ok")
    db.assign_claim(hid, claim["id"])
    assert evidence_strength(db.evidence_for(hid))[1] == 0  # riešenie dodávateľa nedokladá problém


def test_model_is_unloaded_periodically():
    llm = LLM(LLMConfig(unload_every=2))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))])
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    unloads = []
    llm.unload = lambda: unloads.append(1) or True
    for _ in range(5):
        llm._create([{"role": "user", "content": "x"}])
    assert len(unloads) == 2  # po 2. a 4. volaní


def test_reasoning_only_answer_gives_clear_error():
    llm = LLM(LLMConfig())
    message = SimpleNamespace(content="", reasoning="Thinking Process: ...")
    llm._create = lambda *args, **kwargs: SimpleNamespace(choices=[SimpleNamespace(message=message)])
    with pytest.raises(LLMError, match="reasoning_effort"):
        llm.chat_json([{"role": "user", "content": "x"}], Project)
