"""M21 – experimenty Brány 2 bez úniku a so štyrmi osami výsledku (audit 14. 10. 2026, P0 3 a P1 5): príznaky zo
štítkov po foldoch, dozretie štítka na každej hranici, dostupnosť udalostí z inej tabuľky, jednotka štítka, silné
porovnanie zmrazené na validácii, intervaly po entitách, verdikt. Offline, syntetické dáta s pevným seedom."""

import json
from datetime import date, timedelta

import numpy as np
import pytest

from radar import exp_conclude, exp_plan, gate1, gate2
from radar.exp_ops import Step, Workspace
from radar.exp_tasks import EXPERIMENT_CODE_VERSION, _by_unit, cluster_bootstrap, entity_rates, verdict
from radar.util import Deadline
from tests.test_m14_experiments import RANKING_PLAN, contracts_with_amendments, dataset, setup


# ======================================================================= P0 3: únik validačných a testovacích odpovedí
def _synthetic(n=200, seed=0):
    rng = np.random.default_rng(seed)
    t = np.array(sorted(date(2024, 1, 1) + timedelta(days=int(d)) for d in rng.integers(0, 600, n)), dtype="datetime64[D]")
    ents = np.array([f"E{i % 10}" for i in range(n)], dtype=object)
    y = (rng.random(n) < 0.3).astype(int)
    return t, ents, y


def test_entity_rate_feature_ignores_validation_and_test_answers():
    """Reprodukcia z auditu: 200 záznamov, učenie 112, validácia 28, test 60; zmena iba validačných štítkov menila
    `miera_entity` vo všetkých 112 riadkoch učenia. Teraz sa nezmení ani jeden."""
    t, ents, y = _synthetic()
    known = t + np.timedelta64(30, "D")  # štítok je známy 30 dní po dátume riadku
    val_from, test_from = 112, 140
    t_val, t_test = t[val_from], t[test_from]
    base = entity_rates(y, t, known, ents, t_val)
    y_val = y.copy()
    y_val[val_from:test_from] = 1 - y_val[val_from:test_from]
    changed = entity_rates(y_val, t, known, ents, t_val)
    train = t < t_val
    assert np.array_equal(base[train], changed[train])  # príznaky učenia sa nezmenili
    assert np.array_equal(base[~train], changed[~train])  # ani validačné (zmrazené pred hranicou)
    y_test = y.copy()
    y_test[test_from:] = 1 - y_test[test_from:]
    assert np.array_equal(entity_rates(y, t, known, ents, t_test), entity_rates(y_test, t, known, ents, t_test))
    # riadok vidí iba štítky známe pred svojím dňom: ručný prepočet pre jeden riadok
    i = 80
    seen = [j for j in range(len(y)) if known[j] < t[i]]
    prior = np.mean(y[seen])
    mine = [j for j in seen if ents[j] == ents[i]]
    assert base[i] == pytest.approx((sum(y[mine]) + 5 * prior) / (len(mine) + 5))


def _ranking_env(tmp_path, rows=None, plan=None):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, gen = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows if rows is not None else gen, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    p = plan or RANKING_PLAN
    exp = db.add_experiment(hid, None, None, "s", p["primary_metric"], p["primary_baseline"], exp_plan.success_rule(p), p,
                            task="ranking", problem_link="výber zmlúv")
    gate2.lock(cfg, db, exp)
    gate2.run_backtest(cfg, db, Deadline(5), [hid])
    e = db.get_experiment(exp)
    return cfg, db, hid, e, json.loads(e["result_json"] or "{}")


def test_test_labels_do_not_change_predictions_and_labels_mature_per_boundary(tmp_path):
    header, rows = contracts_with_amendments()
    cfg, db, hid, e, res = _ranking_env(tmp_path, rows)
    det = res["detaily"]
    assert det["dozretie_štítka_dní"] == 200 and det["nedozreté_pred_validáciou"] > 0 and det["nedozreté_pred_testom"] > 0
    test_start = date.fromisoformat(det["test"][:10])
    dates = {r[0]: date.fromisoformat(r[4]) for r in rows if r[0] < 10000}
    # iné odpovede v teste: dodatky polovice testovacích zmlúv zmiznú (príznaky ani učenie sa nemenia)
    changed = [r for r in rows if not (r[0] >= 10000 and (r[0] - 10000) % 2 == 0 and dates[r[0] - 10000] >= test_start)]
    assert len(changed) < len(rows)
    _, _, _, _, res2 = _ranking_env(tmp_path / "b", changed)
    assert res2["detaily"]["test_pozitívne"] < det["test_pozitívne"]  # odpovede v teste sa naozaj zmenili
    assert res2["výber"] == res["výber"]  # výber modelu na validácii rovnaký
    score = {(p["subject"], p["skóre"]) for p in res["položky"]}
    score2 = {(p["subject"], p["skóre"]) for p in res2["položky"]}
    assert score == score2  # predikcie pre testovacie riadky sa nezmenili


def test_strong_baseline_is_chosen_on_validation_and_result_has_axes_and_versions(tmp_path):
    cfg, db, hid, e, res = _ranking_env(tmp_path)
    strong = res["silné_porovnanie"]
    val = {k: v for k, v in res["detaily"]["naivné_na_validácii"].items() if v is not None and k != "random"}
    assert strong["naivný"] == max(val, key=val.get) and "validácia" in strong["vybraný_na"]
    assert res["detaily"]["interval"] == "bootstrap po entitách"
    osi = res["osi"]
    assert set(osi) >= {"predregistrácia", "silné_porovnanie", "metodika"} and osi["metodika"]["platné"]
    assert res["verzie"]["kód"] == EXPERIMENT_CODE_VERSION and "ds1" in res["verzie"]["dáta"]
    flags = json.loads(e["flags_json"])
    assert flags["verdikt"] == verdict(res, flags)["verdikt"] and "metodicky_neplatné" in flags


def test_verdict_does_not_claim_benefit_against_weak_baseline_e6():
    """E6 z auditu: logistická regresia 0,484 vs. „najväčšie najprv“ 0,096 (+404 %, splnené), ale historická miera
    entity 0,536 (−9,7 %). Predregistrácia ostáva, verdikt však nie je prínos."""
    res = {"hlavné": {"splnené": True, "naivný": "value_desc", "zlepšenie_pct": 404.17},
           "osi": {"predregistrácia": {"splnené": True},
                   "silné_porovnanie": {"naivný": "entity_rate", "hodnota": 0.536, "model_hodnota": 0.484,
                                        "zlepšenie_pct": -9.7, "prekonal": False},
                   "metodika": {"platné": True, "problémy": [], "nepresvedčivé": []}}}
    v = verdict(res)
    assert not v["pokračovať"] and "silné" in v["verdikt"] and v["používateľ"]["stav"] == "chýba"
    res["osi"]["silné_porovnanie"]["prekonal"] = True
    assert verdict(res)["verdikt"].startswith("prínos v dátach")
    assert verdict(res, {"výsledok_používateľa": {"stav": "potvrdené", "text": "pilot: −30 % času"}})["verdikt"] == \
        "potvrdené u používateľa"
    res["osi"]["metodika"] = {"platné": False, "problémy": ["neznámy čas dostupnosti"]}
    assert verdict(res)["verdikt"] == "neplatné – metodika"


def test_cluster_bootstrap_and_entity_unit():
    rng = np.random.default_rng(0)
    groups = np.repeat(np.arange(10), 5)
    vals = np.repeat(np.arange(10, dtype=float), 5)
    ci = cluster_bootstrap(groups, lambda ix: float(vals[ix].mean()), 200, rng)
    assert ci[0] < 4.5 < ci[1]
    y, s = _by_unit(np.array([0, 1, 0, 0]), np.array([0.1, 0.2, 0.9, 0.3]), np.array(["a", "a", "b", "c"], dtype=object))
    assert list(y) == [1, 0, 0] and list(s) == [0.2, 0.9, 0.3]  # subjekt: aspoň jeden štítok, najvyššie skóre


# ======================================================================= dostupnosť štítka, jednotka, populácia
def _lookup_env(tmp_path, nku=False):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola", "", "obce")
    rng = np.random.default_rng(1)
    rows = [(i, f"{100 + i % 40:08d}", (date(2024, 1, 1) + timedelta(days=int(rng.integers(0, 700)))).isoformat(),
             round(float(rng.normal()), 3)) for i in range(400)]
    dataset(cfg, db, hid, "cid;ico_objednavatel;datum;x", rows, "2024-01-01", "2025-12-31")
    finds = [(f"{100 + i % 40:08d}", (date(2024, 3, 1) + timedelta(days=7 * i)).isoformat(),
              (date(2024, 3, 1) + timedelta(days=7 * i + 40 + i % 20)).isoformat()) for i in range(60)]
    dataset(cfg, db, hid, "ico;koniec_kontroly;zverejnene", finds, "2024-01-01", "2025-12-31", name="ds2")
    if nku:
        db._exec("UPDATE datasets SET url = 'https://www.nku.gov.sk/datasety' WHERE table_name = 'ds2'")
    return cfg, db, hid


def lookup_plan(**step):
    look = {"op": "lookup", "name": "zistenie", "table": "ds2", "keys": ["ico_objednavatel"], "target_column": "ico",
            "match": "ico", "other_time": "koniec_kontroly", "days": 180, "agg": "count"} | step
    return {"title": "t", "statement": "s", "problem_link": "p", "task": "ranking", "table": "ds1", "time_column": "datum",
            "entity_column": "ico_objednavatel", "decision_unit": "entity", "label": "mal_zistenie", "features": ["x"],
            "capacity": 20, "primary_metric": "precision@k", "primary_baseline": "entity_rate", "success_threshold": 10,
            "steps": [look, {"op": "derive", "name": "mal_zistenie", "expr": "zistenie > 0"}]}


def test_lookup_label_needs_known_availability_and_horizon_includes_publication_delay(tmp_path):
    cfg, db, hid = _lookup_env(tmp_path)
    errors = exp_plan.validate_plan(cfg, db, hid, lookup_plan())["chyby"]
    assert any("neznámy čas dostupnosti" in e for e in errors)
    unbounded = exp_plan.validate_plan(cfg, db, hid, lookup_plan(days=0))["chyby"]
    assert any("neohraničený" in e for e in unbounded) or any('"days"' in e for e in unbounded)
    ws = Workspace(cfg, hid, "ds1", "datum", {"od": "2024-01-01", "do": "2025-12-31"})
    ws.apply(Step(op="lookup", name="z", table="ds2", keys=["ico_objednavatel"], target_column="ico", match="ico",
                  other_time="koniec_kontroly", days=180, available_column="zverejnene"))
    ws.apply(Step(op="derive", name="lab", expr="z > 0"))
    assert ws.availability["lab"]["oneskorenie_dní"] == 59  # 95. percentil oneskorenia zverejnenia (40–59 dní)
    assert ws.label_horizon("lab") == 180 + 59
    ws.apply(Step(op="lookup", name="z2", table="ds2", keys=["ico_objednavatel"], target_column="ico", match="ico",
                  other_time="koniec_kontroly", days=180, available_lag_days=90))
    assert ws.label_horizon("z2") == 270 and "predpoklad 90" in ws.availability["z2"]["spôsob"]
    ws.close()
    ok = exp_plan.validate_plan(cfg, db, hid, lookup_plan(available_column="zverejnene"))
    assert not any("dostupnosti" in e for e in ok["chyby"]) and ok["štatistiky"]["dozretie_štítka_dní"] == 239


def test_label_unit_entity_and_uninspected_nku_subjects_block_the_experiment(tmp_path):
    cfg, db, hid = _lookup_env(tmp_path, nku=True)
    row_unit = exp_plan.validate_plan(cfg, db, hid, lookup_plan(available_lag_days=90) | {"decision_unit": "row"})["chyby"]
    assert any("jednotka štítka" in e for e in row_unit)  # zistenie u subjektu nie je problém zmluvy
    wrong_ent = lookup_plan(available_lag_days=90) | {"entity_column": "cid"}
    assert any("nesprávna entita" in e for e in exp_plan.validate_plan(cfg, db, hid, wrong_ent)["chyby"])
    no_pop = exp_plan.validate_plan(cfg, db, hid, lookup_plan(available_lag_days=90))["chyby"]
    assert any("nie je overený negatívny" in e for e in no_pop)  # nekontrolovaný subjekt ≠ negatívny prípad
    supplier = lookup_plan(available_lag_days=90, keys=["ico_dodavatel"])
    assert exp_plan.source_warnings(db, hid, supplier)  # spojenie NKÚ podľa dodávateľa = nesprávna entita (chyba)
    exp = db.add_experiment(hid, None, None, "s", "precision@k", "entity_rate", "x", lookup_plan(available_lag_days=90),
                            task="ranking", problem_link="p")
    with pytest.raises(ValueError):
        gate2.lock(cfg, db, exp)  # metodicky neplatný experiment sa nedá uzamknúť


def test_old_locked_experiment_with_methodology_error_is_marked_invalid(tmp_path):
    """Experiment uzamknutý pred M21 (bez dostupnosti štítka) sa spustí, no výsledok je metodicky neplatný."""
    cfg, db, hid = _lookup_env(tmp_path)
    plan = lookup_plan()
    exp = db.add_experiment(hid, None, None, "s", "precision@k", "entity_rate", "x", plan, task="ranking", problem_link="p")
    db._exec("UPDATE experiments SET status = 'locked', locked_at = '2026-10-01T00:00:00', prereg_hash = 'h' WHERE id = ?",
             (exp,))
    gate2.run_backtest(cfg, db, Deadline(5), [hid])
    e = db.get_experiment(exp)
    flags = json.loads(e["flags_json"] or "{}")
    assert e["status"] == "done" and flags["metodicky_neplatné"] and flags["verdikt"] == "neplatné – metodika"
    problems = json.loads(e["result_json"])["osi"]["metodika"]["problémy"]
    assert any("neznámy čas dostupnosti" in p for p in problems)


# ======================================================================= záver a opakovanie návrhu
def test_conclusion_states_verdict_and_strong_baseline(tmp_path):
    cfg, db, hid, e, res = _ranking_env(tmp_path)
    fl = exp_conclude.facts(None, e, res)
    assert any(f.startswith("F13: Verdikt:") for f in fl) and any(f.startswith("F11: Silné jednoduché porovnanie") for f in fl)
    concl = exp_conclude.code_conclusion(None, e, res, fl)
    assert concl["result_for_decision"].startswith("Verdikt:")


def test_failed_plan_is_retried_only_when_inputs_change(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    db._exec("UPDATE datasets SET sha256 = 'v1' WHERE hypothesis_id = ?", (hid,))
    gate1.decide(db, hid, 1, "passed", "test")
    db.add_card(hid, {"decision": {"text": "Výber zmlúv", "claims": []}})
    card = db.latest_card(hid)
    exp_plan.mark_no_valid_plan(db, card, 3, "m", exp_plan.inputs_fingerprint_for(cfg, db, hid, card))
    assert exp_plan.plan_targets(cfg, db, [hid]) == []  # rovnaké vstupy: neopakuje sa
    db._exec("UPDATE datasets SET sha256 = 'v2' WHERE hypothesis_id = ?", (hid,))  # nová verzia súboru, rovnaký počet
    assert [h["id"] for h, _ in exp_plan.plan_targets(cfg, db, [hid])] == [hid]


def test_gate2_page_shows_verdict_axes_and_saves_user_result(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.db import DB
    from tests.test_m2_gui import APP, fixture_db

    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(tmp_path / "config.yaml")
    db = DB(cfg.db_path)
    gate1.decide(db, 1, 1, "passed", "test")
    plan = {"title": "Zmluvy", "task": "ranking", "table": "ds1", "time_column": "datum", "label": "had", "features": ["x"],
            "primary_metric": "precision@k", "primary_baseline": "value_desc", "success_threshold": 10, "steps": []}
    exp = db.add_experiment(1, None, None, "Model nájde zmluvy.", "precision@k", "value_desc", "o 10 %", plan,
                            task="ranking", problem_link="výber zmlúv")
    res = {"task": "ranking", "metriky": ["precision@k"], "vybraný_model": "logistic",
           "tabuľka": [{"postup": "logistic", "typ": "model", "precision@k": 0.484},
                       {"postup": "value_desc", "typ": "naivný", "precision@k": 0.096},
                       {"postup": "entity_rate", "typ": "naivný", "precision@k": 0.536}],
           "hlavné": {"metrika": "precision@k", "model": "logistic", "model_hodnota": 0.484, "naivný": "value_desc",
                      "naivný_hodnota": 0.096, "zlepšenie_pct": 404.17, "splnené": True},
           "osi": {"predregistrácia": {"splnené": True, "naivný": "value_desc", "zlepšenie_pct": 404.17, "prah_pct": 10},
                   "silné_porovnanie": {"naivný": "entity_rate", "hodnota": 0.536, "model_hodnota": 0.484,
                                        "zlepšenie_pct": -9.7, "prekonal": False},
                   "metodika": {"platné": True, "problémy": [], "nepresvedčivé": [], "upozornenia": []}},
           "detaily": {}, "položky": []}
    db._exec("UPDATE experiments SET status = 'done', locked_at = '2026-10-13T10:00:00', result_json = ? WHERE id = ?",
             (json.dumps(res), exp))
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate2.py").run()
    assert not at.exception, at.exception
    assert any("neprekonal silné" in x.value for x in at.error)
    labels = {m.label: m.value for m in at.metric}
    assert labels["(a) Predregistrácia"] == "splnená" and labels["(b) Silné porovnanie"] == "neprekonal"
    assert labels["(d) U používateľa"] == "ešte nemeraný"
    at.selectbox(key=f"g2_user_s_{exp}").set_value("nepotvrdené")
    at.text_input(key=f"g2_user_t_{exp}").input("pilot: kontrolór nenašiel viac zmlúv")
    next(b for b in at.button if b.label == "Uložiť výsledok u používateľa").click().run()
    assert not at.exception, at.exception
    db = DB(cfg.db_path)
    flags = json.loads(db.get_experiment(exp)["flags_json"])
    assert flags["výsledok_používateľa"]["stav"] == "nepotvrdené" and flags["verdikt"] == "neprekonal silné jednoduché porovnanie"
    db.close()
