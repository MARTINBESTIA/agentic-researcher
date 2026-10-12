"""M11 – témy nad hypotézami: zhluky z embeddingov, názov z modelu, stabilita medzi behmi, ručný názov."""

import numpy as np

from radar.config import Config, EmbeddingsConfig, ModelsConfig
from radar.db import DB
from radar.themes import average_linkage, run_themes
from radar.util import Deadline
from tests.fakes import Recorder, make_router

KEYS = ("faktúr", "liek", "obc")


class KeywordEmbedder:
    """Smer podľa kľúčového slova v názve (e-faktúry, lieky, obce); ostatné ortogonálne."""

    def __init__(self):
        self.calls = 0

    def hypothesis_matrix(self, hyps):
        self.calls += 1
        rows = []
        for h in hyps:
            t = h["title"].lower()
            v = np.array([1.0 if k in t else 0.0 for k in KEYS] + [0.3], dtype=np.float32)
            rows.append(v / np.linalg.norm(v))
        return np.vstack(rows)


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 config_dir=str(tmp_path))
    db = DB(cfg.db_path)
    rec = Recorder()
    rec.responses["ThemeName"] = lambda messages: (
        {"name": "E-faktúry: zavedenie", "summary": "Problémy firiem s e-faktúrami."}
        if "faktúr" in messages[-1]["content"] else {"name": "Úhrady liekov", "summary": "Výdavky na lieky."})
    return cfg, db, rec, make_router(cfg, db, rec)


def test_average_linkage_threshold_and_max_size():
    sim = np.full((6, 6), 0.1)
    for block, value in (((0, 1, 2), 0.9), ((3, 4), 0.8)):
        for i in block:
            for j in block:
                sim[i, j] = value
    np.fill_diagonal(sim, 1.0)
    assert sorted(average_linkage(sim, 0.6, 30)) == [[0, 1, 2], [3, 4], [5]]
    assert sorted(average_linkage(sim, 0.85, 30)) == [[0, 1, 2], [3], [4], [5]]
    assert max(len(c) for c in average_linkage(sim, 0.6, 2)) == 2  # téma nad max_size nevznikne


def test_themes_named_by_model_stable_ids_and_manual_name_kept(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    ids = [db.create_hypothesis(t, "", "firmy") for t in
           ("Chyby XML pri e-faktúrach", "Náklady na softvér pre e-faktúry", "Peppol a doručovanie e-faktúr",
            "Rastúce ceny liekov", "Meškajúce úhrady liekov", "Kapacita obcí na obstarávanie")]
    rejected = db.create_hypothesis("Zamietnutá hypotéza o e-faktúrach", "", "x", status="rejected")
    emb = KeywordEmbedder()
    stats = run_themes(cfg, db, router, emb, Deadline(1))
    assert stats["themes"] == 2 and stats["named"] == 2 and stats["hypotheses_in_themes"] == 5
    themes = {t["name"]: t for t in db.themes()}
    assert set(themes) == {"E-faktúry: zavedenie", "Úhrady liekov"} and themes["E-faktúry: zavedenie"]["size"] == 3
    assert db.get_hypothesis(ids[5])["theme_id"] is None  # samostatná hypotéza
    assert db.get_hypothesis(rejected)["theme_id"] is None  # zamietnuté sa do tém nezaraďujú
    first = {t["name"]: t["id"] for t in db.themes()}

    db.rename_theme(first["Úhrady liekov"], "Lieky – ceny a úhrady")
    new = db.create_hypothesis("Archivácia e-faktúr", "", "firmy")
    rec.calls.clear()
    stats = run_themes(cfg, db, router, emb, Deadline(1))
    assert stats["kept"] == 2 and stats.get("named", 0) == 0 and rec.models_for("ThemeName") == []
    after = {t["id"]: t for t in db.themes()}
    assert set(after) == set(first.values())  # malá zmena členov: téma si ponechá ID aj názov
    assert after[first["E-faktúry: zavedenie"]]["size"] == 4 and db.get_hypothesis(new)["theme_id"] == first["E-faktúry: zavedenie"]
    assert after[first["Úhrady liekov"]]["name"] == "Lieky – ceny a úhrady" and after[first["Úhrady liekov"]]["name_method"] == "manual"


def test_theme_name_falls_back_to_most_typical_hypothesis(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    for t in ("Rastúce ceny liekov", "Meškajúce úhrady liekov"):
        db.create_hypothesis(t, "", "nemocnice")
    rec.responses["ThemeName"] = RuntimeError("model nebeží")
    stats = run_themes(cfg, db, router, KeywordEmbedder(), Deadline(1))
    theme = db.themes()[0]
    assert stats["medoid_names"] == 1 and theme["name_method"] == "medoid"
    assert theme["name"] in ("Rastúce ceny liekov", "Meškajúce úhrady liekov")
    assert run_themes(cfg, db, router, None, Deadline(1)) == {"skipped": "embeddingy vypnuté"}
