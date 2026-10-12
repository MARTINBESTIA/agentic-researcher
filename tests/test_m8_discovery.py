"""M8 – objavovanie: rozpočet, bandit, novosť a kandidáti, screen, rotácia matice (bez siete)."""

import random

import numpy as np

from radar.cluster import run_cluster
from radar.config import Config, DiscoveryConfig, EmbeddingsConfig, ModelsConfig, PipelineConfig
from radar.db import DB
from radar.discover import MATRIX_ARM, allocate, discovery_budget, matrix_cells, next_matrix_queries, run_discover
from radar.extract import run_extract, run_screen
from radar.search import SearchResult
from radar.util import Deadline
from tests.fakes import FakeFetcher, FakeSearx, Recorder, make_router

TEXT = "Úvod k téme. " * 30 + "\nNemocnice evidujú lieky v Exceli a pri inventúre chýbajú stovky položiek.\n" + "Koniec. " * 30


def cfg_for(tmp_path, **disc):
    return Config(db_path=str(tmp_path / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                  pipeline=PipelineConfig(max_new_docs_per_run=10),
                  discovery=DiscoveryConfig(sources_file=str(tmp_path / "none.yaml"), seed=1, **disc))


def test_budget_gives_discovery_at_least_30_percent(tmp_path):
    cfg = cfg_for(tmp_path)
    db = DB(cfg.db_path)
    assert discovery_budget(cfg, 10) == 3
    urls = [f"https://web{i}.sk/clanok" for i in range(20)]
    searx = FakeSearx(lambda q: [SearchResult(u, "Titulok", "") for u in urls])
    fetcher = FakeFetcher(texts={u: TEXT + u for u in urls})
    stats = run_discover(cfg, db, searx, fetcher, Deadline(1))
    assert stats["new_documents"] >= 3
    kinds = db.conn.execute("SELECT source_kind, source_category, topic FROM documents").fetchall()
    assert all(tuple(k) == ("discovery", MATRIX_ARM, None) for k in kinds)


def test_bandit_is_repeatable_and_respects_minimum_share():
    arms = ["audit", "legislation", "procurement", "association", MATRIX_ARM]
    stats = {"audit": {"tries": 20, "successes": 15}, "legislation": {"tries": 20, "successes": 1}}
    a1 = allocate(arms, 100, stats, 0.05, random.Random(42))
    a2 = allocate(arms, 100, stats, 0.05, random.Random(42))
    assert a1 == a2 and sum(a1.values()) == 100
    assert min(a1.values()) >= 5  # každá kategória aspoň 5 %
    assert a1["audit"] > a1["legislation"]  # úspešnejšie rameno dostane viac


def test_matrix_rotation_continues_where_last_run_ended(tmp_path):
    cfg = cfg_for(tmp_path)
    db = DB(cfg.db_path)
    cells = matrix_cells(cfg)
    first = next_matrix_queries(cfg, db, 4)
    second = next_matrix_queries(cfg, db, 4)
    assert first == [f"{p} {s}" for p, s in cells[:4]] and second == [f"{p} {s}" for p, s in cells[4:8]]
    db.close()
    db = DB(cfg.db_path)  # nový beh (stav je v DB)
    assert next_matrix_queries(cfg, db, 1) == [" ".join(cells[8])]


def test_screen_negative_document_is_not_extracted(tmp_path):
    cfg = cfg_for(tmp_path)
    db = DB(cfg.db_path)
    d1, _ = db.upsert_document("https://a.sk/1", "A", "discovery", None, "q", "h1", TEXT, source_category=MATRIX_ARM)
    d2, _ = db.upsert_document("https://b.sk/2", "B", "discovery", None, "q", "h2", "Šport a počasie. " * 40, source_category=MATRIX_ARM)
    rec = Recorder()
    rec.responses = {
        "ScreenResult": lambda m: {"relevant": "Nemocnice" in m[-1]["content"], "sector_hint": "nemocnice"},
        "ExtractionResult": {"relevant": True, "claims": [{
            "problem": "Nemocnice evidujú lieky v Exceli a chýbajú položky.", "affected_party": "nemocnice",
            "claim_type": "pain", "quote": "Nemocnice evidujú lieky v Exceli a pri inventúre chýbajú stovky položiek.",
            "sector_hint": "nemocnice"}]},
    }
    router = make_router(cfg, db, rec)
    assert run_screen(cfg, db, router, Deadline(1)) == {"relevant": 1, "not_relevant": 1}
    stats = run_extract(cfg, db, router, Deadline(1))
    assert stats["claims"] == 1 and stats["documents"] == 1
    assert rec.models_for("ExtractionResult") == ["radar-qwen3.5-9b"]
    assert db.get_document(d2)["screened"] == "no" and db.conn.execute(
        "SELECT COUNT(*) FROM claims WHERE document_id = ?", (d2,)).fetchone()[0] == 0
    claim = db.conn.execute("SELECT * FROM claims").fetchone()
    assert claim["topic"] is None and claim["sector_hint"] == "nemocnice"


class FakeEmbedder:
    """Vektory podľa kľúčových slov: 'excel' a 'meškanie' sú ortogonálne smery."""

    def _vec(self, text):
        t = text.lower()
        v = np.array([1.0 if "excel" in t or "eviden" in t else 0.0, 1.0 if "mešk" in t else 0.0, 0.05], dtype=np.float32)
        return v / np.linalg.norm(v)

    def hypothesis_matrix(self, hyps):
        return np.vstack([self._vec(f"{h['title']} {h['description']}") for h in hyps])

    def claim_vector(self, claim):
        return self._vec(claim["problem"])


def add_verified(db, url, problem, kind="discovery", topic=None):
    doc_id, _ = db.upsert_document(url, "T", kind, topic, "q", url, problem * 5)
    cid = db.insert_claim(doc_id, 0, topic, problem, "nemocnice", "pain", problem, sector_hint="nemocnice")
    db.set_claim_verification(cid, "verified", 100, "ok")
    return cid


def test_novelty_candidate_and_promotion_after_two_domains(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, novelty_threshold=0.8)
    db = DB(cfg.db_path)
    existing = db.create_hypothesis("Meškanie dodávok komponentov", "meškanie", "výrobcovia")
    rec = Recorder()
    rec.responses = {"Assignment": {"decision": "existing", "hypothesis_id": existing}}
    router = make_router(cfg, db, rec)
    emb = FakeEmbedder()
    monkeypatch.setattr("radar.cluster.rank_hypotheses", lambda e, c, h, k=8: list(h))
    monkeypatch.setattr("radar.cluster.similar_pairs", lambda e, h, t: [])
    # podobné tvrdenie -> existujúca hypotéza (rozhodne model)
    similar = add_verified(db, "https://a.sk/1", "Výrobcom mešká dodávka ložísk.")
    # odlišné -> nový kandidát bez volania modelu
    novel = add_verified(db, "https://b.sk/2", "Nemocnice vedú evidenciu liekov v Exceli.")
    stats = run_cluster(cfg, db, router, Deadline(1), emb)
    assert db.hypothesis_of_claim(similar) == existing
    cand = db.get_hypothesis(db.hypothesis_of_claim(novel))
    assert (cand["status"], cand["origin"], cand["sector"]) == ("candidate", "discovery", "nemocnice")
    assert stats["new_candidates"] == 1 and len(rec.models_for("Assignment")) == 1
    # druhé tvrdenie z tej istej domény ešte nepovýši
    rec.responses = {"Assignment": {"decision": "existing", "hypothesis_id": cand["id"]}}
    add_verified(db, "https://b.sk/3", "Lekárnici v nemocnici prepisujú výdaj liekov do Excelu každý večer.")
    run_cluster(cfg, db, router, Deadline(1), emb)
    assert db.get_hypothesis(cand["id"])["status"] == "candidate"
    # tretie z inej domény -> 2 domény -> "new"
    add_verified(db, "https://c.sk/4", "V nemocniciach sa zásoby liekov sledujú v excelových tabuľkách a chýba prehľad.")
    stats = run_cluster(cfg, db, router, Deadline(1), emb)
    assert db.get_hypothesis(cand["id"])["status"] == "new" and stats["promoted"] == 1


def test_demand_signal_needs_procurement_or_call_wording():
    from radar.extract import checked_claim_type

    assert checked_claim_type("demand_signal", "Duplicity v zákazníckych dátach majú zvyčajne procesnú príčinu.") == "pain"
    assert checked_claim_type("demand_signal", "Mesto vyhlasuje prípravné trhové konzultácie na dodávku systému.") == "demand_signal"
    assert checked_claim_type("demand_signal", "Nemocnica opakovane vyhlásila súťaž na správu liekov.") == "demand_signal"
    assert checked_claim_type("regulation", "Od roku 2027 musia firmy…") == "regulation"
