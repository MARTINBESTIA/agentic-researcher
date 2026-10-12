"""M3 – kaskáda, self-consistency, eval (falošný model so zoznamom odpovedí, bez siete)."""

import math

import pytest

from radar.config import Config, EmbeddingsConfig, ModelsConfig
from radar.db import DB
from radar.evals import compute_metrics, make_synthetic_cases, recommend, run_eval, wilson, write_eval_report
from radar.llm import LLMTruncated
from radar.review import apply_label
from radar.util import Deadline
from radar.verify import Entailment, run_verify
from tests.fakes import Recorder, make_router

DOC = "Úvod. " * 40 + "\nMalé obce nemajú kapacitu na verejné obstarávanie, preto 30 projektov mešká.\n" + "Záver. " * 40
QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie, preto 30 projektov mešká."


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "r.sqlite"), reports_dir=str(tmp_path / "reports"), models=ModelsConfig(),
                 embeddings=EmbeddingsConfig(enabled=False))
    db = DB(cfg.db_path)
    rec = Recorder()
    return cfg, db, rec, make_router(cfg, db, rec)


def add_claim(db, n=1):
    ids = []
    for i in range(n):
        doc_id, _ = db.upsert_document(f"https://obce{i}.sk/x", "T", "search", "t", "q", f"h{i}", DOC)
        cid = db.insert_claim(doc_id, 0, "t", "Malé obce nemajú kapacitu na obstarávanie, 30 projektov mešká.", "malé obce", "pain", QUOTE)
        ids.append(cid)
    return ids


def ent(verdict):
    return {"verdict": verdict, "reason": "r"}


def vote(router, rec, answers, object_id):
    rec.responses = {"Entailment": [ent(a) if isinstance(a, str) else a for a in answers]}
    return router.run_task("entail_gate", [{"role": "user", "content": "x"}], Entailment, object_type="claim",
                           object_id=object_id, vote_key=lambda e: e.verdict, review=True)


def test_self_consistency_votes(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    r = vote(router, rec, ["supported", "supported", "supported"], 1)
    assert (r.verdict, r.low_agreement, r.undecided) == ("supported", False, False)
    r = vote(router, rec, ["supported", "partial", "supported"], 2)
    assert (r.verdict, r.low_agreement) == ("supported", True)
    assert [x["reason"] for x in db.open_reviews("entail_gate")] == ["low_agreement"]
    r = vote(router, rec, ["supported", "partial", "unsupported"], 3)
    assert r.undecided and r.reason == "disagreement"
    r = vote(router, rec, [LLMTruncated("x"), LLMTruncated("x"), "supported"], 4)
    assert r.undecided
    reviews = {x["object_id"]: x["reason"] for x in db.open_reviews("entail_gate")}
    assert reviews[3] == "disagreement" and reviews[4] == "truncated"
    temps = {c[3] for c in rec.calls}
    assert temps == {0.7}  # hlasovanie so zvýšenou teplotou
    assert db.conn.execute("SELECT COUNT(*) FROM model_judgments WHERE task = 'entail_gate'").fetchone()[0] == 12


def test_wilson_matches_manual_formula():
    lo, hi = wilson(17, 20)
    p, n, z = 17 / 20, 20, 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    assert lo == pytest.approx(centre - half) and hi == pytest.approx(centre + half)
    assert wilson(0, 0) == (0.0, 1.0)


def test_eval_with_20_labels_computes_metrics_like_manual(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    claims = add_claim(db, 20)
    gold = ["unsupported"] * 8 + ["supported"] * 12
    for cid, g in zip(claims, gold):
        db.add_label("entail", "claim", cid, g)
    # plánované odpovede modelu: 6 zo 8 nepodložených zachytí, 2 z 12 podložených označí omylom
    preds = ["unsupported"] * 6 + ["supported"] * 2 + ["unsupported"] * 2 + ["supported"] * 10
    rec.responses = {"Entailment": [ent(p) for p in preds]}
    results = run_eval(cfg, db, router, "entail", ["C"])
    m = results["profiles"]["C"]
    b = m["binary"]
    tp, fp, fn = 6, 2, 2
    assert (b["tp"], b["fp"], b["fn"]) == (tp, fp, fn)
    assert b["precision"] == pytest.approx(tp / (tp + fp)) and b["recall"] == pytest.approx(tp / (tp + fn))
    assert b["f1"] == pytest.approx(2 * 0.75 * 0.75 / 1.5)
    assert m["accuracy"] == pytest.approx(16 / 20) and m["accuracy_ci"] == pytest.approx(wilson(16, 20))
    assert b["precision_ci"] == pytest.approx(wilson(6, 8))
    assert all(c[1] == "radar-qwen3.6-27b" and c[2] is False for c in rec.calls)  # profil C = accurate bez uvažovania
    assert results["recommendation"].startswith("nedostatok dát")
    report = write_eval_report(cfg, results).read_text(encoding="utf-8")
    assert "Nedostatok dát" in report and "80 %" in report
    assert db.conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0] == 1


def test_recommendation_needs_50_labels_then_picks_cheapest_overlapping():
    assert recommend({"A": {"accuracy": 0.9, "accuracy_ci": (0.8, 0.95), "avg_s": 1}}, 49).startswith("nedostatok dát")
    profiles = {
        "A": {"accuracy": 0.80, "accuracy_ci": (0.70, 0.88), "avg_s": 5},
        "C": {"accuracy": 0.88, "accuracy_ci": (0.79, 0.94), "avg_s": 40},
        "D": {"accuracy": 0.90, "accuracy_ci": (0.81, 0.95), "avg_s": 300},
    }
    assert recommend(profiles, 60).startswith("profil A")  # interval A (do 0,88) sa prekrýva s najlepším D (od 0,81)
    profiles["A"]["accuracy_ci"] = (0.60, 0.79)  # už sa neprekrýva -> najlacnejší prekrývajúci je C
    assert recommend(profiles, 60).startswith("profil C")


def test_metrics_multiclass_accuracy_only():
    m = compute_metrics([("exact", "exact"), ("none", "related")], None)
    assert m["accuracy"] == 0.5 and "binary" not in m


def test_review_label_applies_to_claim(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    cid = add_claim(db)[0]
    rec.responses = {"Entailment": LLMTruncated("orezané")}
    run_verify(cfg, db, router, Deadline(1))
    review = db.open_reviews("entail")[0]
    result = apply_label(db, review["id"], "supported", "pozrel som zdroj")
    assert db.get_claim(cid)["status"] == "verified" and "tvrdenie verified" in result["effect"]
    assert db.labels_for("entail")[0]["label"] == "supported" and db.open_reviews() == []


def test_synthetic_entail_cases(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    for cid in add_claim(db, 3):
        db.set_claim_verification(cid, "verified", 100, "ok")
    n = make_synthetic_cases(db, "entail", n=3)
    assert n >= 1
    labels = db.labels_for("entail")
    assert {l["label"] for l in labels} == {"unsupported"} and all(l["object_type"] == "eval_case" for l in labels)
