"""M14: problémové karty, rozdelenie hypotéz a univerzálne experimenty Brány 2 (offline, syntetické dáta so známou
odpoveďou: dodatky závislé od príznaku, sezónne rady so skokom, hromadenie pod prahom)."""

import json
from datetime import date, timedelta

import numpy as np
import pytest

from radar import cards, exp_conclude, exp_plan, gate1, gate2
from radar.config import Config, EmbeddingsConfig, Gate1Config, ModelsConfig, PeopleConfig, PrivacyConfig
from radar.datasets import DatasetError, hyp_dir, import_table, run_query
from radar.db import DB
from radar.exp_ops import Step, Workspace
from radar.util import Deadline
from tests.fakes import Recorder, make_router


def setup(tmp_path):
    cfg = Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(), embeddings=EmbeddingsConfig(enabled=False),
                 privacy=PrivacyConfig(lia_completed=True), people=PeopleConfig(search_profiles=False),
                 gate1=Gate1Config(nkod_enabled=False), config_dir=str(tmp_path))
    cfg.gate2.demos_dir = str(tmp_path / "demos")
    db = DB(cfg.db_path)
    rec = Recorder()
    return cfg, db, rec, make_router(cfg, db, rec)


def claim(db, hid, text, quote=None, n=[0]):
    n[0] += 1
    doc, _ = db.upsert_document(f"https://zdroj.sk/{n[0]}", "Zdroj", "search", "t", "q", f"h{n[0]}", quote or text)
    cid = db.insert_claim(doc, 0, "t", text, "úrady", "pain", quote or text, [])
    db.set_claim_verification(cid, "verified", 100, "ok")
    db.assign_claim(hid, cid)
    return cid


def dataset(cfg, db, hid, header, rows, od, do, name="ds1"):
    hyp_dir(cfg, hid).mkdir(parents=True, exist_ok=True)
    path = cfg.path("data") / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(";".join(str(x) for x in r) for r in rows) + "\n", encoding="utf-8")
    import_table(cfg, hid, name, path, "csv")
    return db.add_dataset(hid, None, f"https://data.sk/{name}.csv", name, status="ok",
                          profile_json={"časové_okno": {"od": od, "do": do}, "ico_columns": []})


def contracts_with_amendments(seed=1):
    """Zmluvy 2024–2025; dodatok (do 200 dní) s pravdepodobnosťou rastúcou s príznakom x."""
    rng = np.random.default_rng(seed)
    rows, start = [], date(2024, 1, 1)
    for i in range(4000):
        d = start + timedelta(days=int(rng.integers(0, 730)))
        x = float(rng.normal())
        buyer = f"B{int(rng.integers(0, 20))}"
        amount = round(float(rng.lognormal(10, 1)), 2)
        rows.append((i, buyer, f"Z-{i}", f"Zmluva o dielo {i}", d.isoformat(), amount, round(x, 3)))
        if rng.random() < 1 / (1 + np.exp(2.5 - 2 * x)):
            rows.append((10000 + i, buyer, f"D-{i}", f"Dodatok č. 1 k zmluve č. Z-{i}",
                         (d + timedelta(days=int(rng.integers(10, 200)))).isoformat(), round(amount * 0.2, 2), 0))
    return "cid;buyer;number;subject;datum;amount;x", rows


RANKING_PLAN = {
    "title": "Zmluvy s budúcim dodatkom", "statement": "Model nájde zmluvy s dodatkom lepšie ako náhodný výber.",
    "problem_link": "Kontrolór vyberá, ktoré zmluvy preverí.", "task": "ranking", "table": "ds1", "time_column": "datum",
    "entity_column": "buyer", "label": "had_amend", "value": "amount", "features": ["x", "amount", "buyer_prior"],
    "capacity": 50, "primary_metric": "precision@k", "primary_baseline": "random", "success_threshold": 10,
    "steps": [
        {"op": "link_records", "name": "parent", "column": "subject", "pattern": r"k zmluve č\. (\S+)",
         "target_column": "number", "keys": ["buyer"]},
        {"op": "linked_agg", "name": "amend_count", "link": "parent", "agg": "count", "days": 365},
        {"op": "derive", "name": "had_amend", "expr": "amend_count > 0"},
        {"op": "filter", "expr": "subject NOT LIKE 'Dodatok%'"},
        {"op": "prior_agg", "name": "buyer_prior", "keys": ["buyer"], "agg": "count", "days": 90},
    ]}


# ======================================================================= karty a rozdelenie
def test_card_cites_own_claims_checks_key_fields_and_splits_hypothesis(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Neprehľadnosť financií štátu", "Široká", "štát")
    ids = [claim(db, hid, f"Problém {i}") for i in range(6)]
    fid = db.upsert_field(hid, "Suma zmluvy", "", True, "")
    db.update_field(fid, status="found")
    dataset(cfg, db, hid, "datum;suma", [("2025-01-01", 1)] * 3, "2025-01-01", "2025-06-30")
    gate1.decide(db, hid, 1, "passed", "test")
    rec.responses["ProblemCardOut"] = {
        "problem": {"text": "Kontrolóri nevedia, ktoré zmluvy preveriť.", "claims": [ids[0], 99999]},
        "who": {"text": "Hlavný kontrolór obce", "claims": [ids[1]]}, "decision": {"text": "Výber zmlúv na kontrolu"},
        "helpful_output": "ranking", "helpful_output_text": "Poradie zmlúv", "business_metric": "podiel zmlúv s nálezom",
        "subproblems": [{"title": "Kontrola zmlúv", "problem": "Výber zmlúv", "claims": [ids[0], ids[1], ids[2]]},
                        {"title": "Náklady IT projektov", "problem": "Neoveriteľné náklady", "claims": [ids[3], ids[4], ids[2]]}]}
    rec.responses["CardCheck"] = {"verdict": "supported", "reason": "citát to uvádza"}

    class Emb:  # dve skupiny podobných citátov (embeddingy bez siete)
        def vectors(self, kind, items):
            return np.array([[1.0, 0.0] if i < 3 else [0.0, 1.0] for i in range(len(items))], dtype=np.float32)

    seen = []
    orig = rec.responses["ProblemCardOut"]
    rec.responses["ProblemCardOut"] = lambda m: seen.append(m[-1]["content"]) or orig
    stats = cards.run_cards(cfg, db, router, Deadline(1), [hid], embedder=Emb())
    assert "GROUP 1:" in seen[0] and "GROUP 2:" in seen[0]  # citáty zoskupené pred zadaním modelu
    assert stats == {"cards": 1, "splits": 1, "children": 2}
    card = json.loads(db.latest_card(hid)["card_json"])
    assert card["problem"]["claims"] == [ids[0]]  # cudzí citát 99999 kód vyradil
    assert card["kontrola"]["decision"]["verdict"] == "unsupported"  # pole bez citátu sudca neposudzuje
    assert rec.models_for("CardCheck") == ["radar-qwen3.6-27b"] * 2
    c1, c2 = card["rozdelené_na"]
    assert {c["id"] for c in db.evidence_for(c1)} == set(ids[:3])
    assert {c["id"] for c in db.evidence_for(c2)} == {ids[3], ids[4]}  # C2 už patrí prvej podhypotéze
    assert {c["id"] for c in db.evidence_for(hid)} == {ids[5]}  # rodičovi ostáva všeobecné
    child = db.get_hypothesis(c1)
    assert child["parent_id"] == hid and child["gate1_status"] == "passed" and db.get_hypothesis(hid)["split_at"]
    assert "Zdedené z H" in db.gate_decisions_for(c1, 1)[0]["reason"]
    assert hyp_dir(cfg, c1) == hyp_dir(cfg, hid) and run_query(cfg, c1, "SELECT COUNT(*) FROM ds1")[0][0] == 3
    assert [d["table_name"] for d in db.datasets_for(c1)] == ["ds1"] and db.fields_for(c1)[0]["status"] == "found"
    # podhypotézy dostanú vlastnú kartu, rozdelená hypotéza už nie
    assert {h["id"] for h in cards.card_targets(cfg, db, None)} >= {c1, c2} and hid not in {
        h["id"] for h in cards.card_targets(cfg, db, None)}


# ======================================================================= operácie a únik budúcich dát
def test_workspace_links_records_and_keeps_history_and_future_apart(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("H", "", "x")
    rows = [(1, "B", "Z-1", "Zmluva", "2025-01-10", 100), (2, "B", "D-1", "Dodatok č. 1 k zmluve č. Z-1", "2025-03-01", 30),
            (3, "B", "Z-2", "Zmluva", "2025-01-10", 50), (4, "C", "D-9", "Dodatok k zmluve č. Z-1", "2025-02-01", 5),
            (5, "B", "Z-3", "Zmluva", "2025-02-15", 70)]
    dataset(cfg, db, hid, "cid;buyer;number;subject;datum;amount", rows, "2025-01-01", "2025-04-30")
    ws = Workspace(cfg, hid, "ds1", "datum", {"od": "2025-01-01", "do": "2025-04-30"})
    ws.apply(Step(op="link_records", name="parent", column="subject", pattern=r"k zmluve č\. (\S+)", target_column="number",
                  keys=["buyer"]))
    ws.apply(Step(op="linked_agg", name="amends", link="parent", agg="sum", column="amount", days=90))
    ws.apply(Step(op="prior_agg", name="prior", keys=["buyer"], agg="count", days=60))
    ws.apply(Step(op="future_agg", name="later", keys=["buyer"], agg="count", days=40))
    got = {r[0]: r[1:] for r in ws.query(f"SELECT cid, amends, prior, later FROM {ws.table} ORDER BY cid")}
    assert got[1] == (30, 0, 1)  # dodatok iného objednávateľa (C) sa nenaviaže; v ten istý deň nie je história
    assert got[3][1] == 0 and got[5][1] == 2  # história: iba skoršie dni, rovnaký objednávateľ
    assert ws.protocol[1]["naviazaných"] == 1 and set(ws.future) == {"amends", "later"}
    ws.apply(Step(op="derive", name="had", expr="amends > 0"))
    assert ws.future["had"] == 90  # odvodené zo štítka z budúcnosti
    ws.apply(Step(op="derive", name="big", expr="amount > 60"))
    ws.apply(Step(op="prior_agg", name="big_share", keys=["buyer"], agg="avg", column="big", days=0))  # avg áno/nie
    assert {r[0]: r[1] for r in ws.query(f"SELECT cid, big_share FROM {ws.table}")}[5] == 0.5
    with pytest.raises(DatasetError):
        ws.apply(Step(op="filter", expr="1=1; DROP TABLE ds1"))
    with pytest.raises(DatasetError):
        ws.apply(Step(op="derive", name="Bad Name", expr="1"))
    before = ws.rows()
    assert ws.matured("had") == 90 and ws.rows() < before  # riadky bez celého budúceho okna vypadli
    ws.close()
    plan = RANKING_PLAN | {"features": ["amount", "later"], "label": "later", "steps": [
        {"op": "future_agg", "name": "later", "keys": ["buyer"], "agg": "count", "days": 40}]}
    errors = exp_plan.validate_plan(cfg, db, hid, plan)["chyby"]
    assert any("únik" in e for e in errors)  # príznak z budúcnosti kód odmietne


# ======================================================================= návrh: skúšobný beh, oprava, uzamknutie
def test_plan_dry_run_repair_and_lock(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    claim(db, hid, "Kontrolóri nestíhajú")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    db.add_card(hid, {"decision": {"text": "Výber zmlúv na kontrolu", "claims": []}})
    broken = RANKING_PLAN | {"features": ["x", "neexistuje"]}
    hopeless = RANKING_PLAN | {"title": "Zlý", "table": "ds1", "label": "nic"}
    cfg.experiments.plans_per_hypothesis = 2
    rec.responses["PlanSet"] = [{"experiments": [broken]}, {"experiments": [hopeless]}]  # jeden plán na volanie
    rec.responses["ExperimentPlan"] = [RANKING_PLAN, hopeless, hopeless]
    stats = exp_plan.run_plans(cfg, db, router, Deadline(5), [hid])
    assert stats == {"repairs": 3, "plans_ok": 1, "plans_with_errors": 1}  # druhý návrh ani po 2 opravách
    good, bad = db.experiments_for(hid)
    assert good["task"] == "ranking" and good["problem_link"] and not json.loads(good["flags_json"] or "{}").get("chyby_návrhu")
    run = json.loads(good["spec_json"])["skúšobný_beh"]
    assert run["pozitívne"] >= 30 and run["protokol"][1]["naviazaných"] > 100
    assert gate2.rule_threshold(good["success_rule"]) == 10
    assert json.loads(bad["flags_json"])["chyby_návrhu"] and bad["status"] == "invalid"  # nepočíta sa do 3 návrhov
    assert [h["id"] for h, _ in exp_plan.plan_targets(cfg, db, [hid])] == [hid]
    db.update_experiment(bad["id"], status="draft")  # starší rozbitý návrh v stave „návrh“ tiež nesmie blokovať
    assert [h["id"] for h, _ in exp_plan.plan_targets(cfg, db, [hid])] == [hid]
    with pytest.raises(ValueError):
        gate2.lock(cfg, db, bad["id"])
    assert len(gate2.lock(cfg, db, good["id"])) == 64
    assert rec.models_for("PlanSet") == ["radar-qwen3.6-27b"] * 2


# ======================================================================= typy úloh
def test_ranking_beats_random_and_value_with_full_scorecard(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    exp = db.add_experiment(hid, None, None, "s", "precision@k", "random", exp_plan.success_rule(RANKING_PLAN), RANKING_PLAN,
                            task="ranking", problem_link="výber zmlúv")
    gate2.lock(cfg, db, exp)
    assert gate2.run_backtest(cfg, db, Deadline(5), [hid]) == {"done": 1}
    e = db.get_experiment(exp)
    res = json.loads(e["result_json"])
    table = {r["postup"]: r for r in res["tabuľka"]}
    assert set(table) == {"logistic", "boosting", "random", "value_desc", "entity_rate"}
    assert all(set(exp_plan.METRICS["ranking"]) <= set(r) for r in table.values())
    prim = res["hlavné"]
    assert prim["model"] in ("logistic", "boosting") and prim["zlepšenie_pct"] > 50 and prim["splnené"]
    assert table[prim["model"]]["avg_precision"] > table["value_desc"]["avg_precision"]
    assert max(res["detaily"]["dôležitosť_príznakov"], key=res["detaily"]["dôležitosť_príznakov"].get) == "x"
    assert len(res["položky"]) == 20 and res["položky"][0]["poradie"] == 1
    assert [p["op"] for p in res["protokol"]][-1] == "mature"
    assert json.loads(e["flags_json"])["pravidlo_nesplnené"] is False


def series_rows(spike=False, seed=2):
    rng = np.random.default_rng(seed)
    rows = []
    for e in range(20):
        base = 100 + 30 * e
        for m in range(36):
            d = date(2023 + m // 12, m % 12 + 1, 15)
            v = base * (1 + 0.5 * np.sin(2 * np.pi * m / 12)) + rng.normal(0, base * 0.05)
            if spike and e == 0 and m == 35:
                v *= 6
            rows.append((f"O{e}", d.isoformat(), round(float(v), 2)))
    return "org;datum;amount", rows


def test_forecast_pooled_and_anomaly_flags_spike(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Plánovanie", "", "obce")
    header, rows = series_rows(spike=True)
    dataset(cfg, db, hid, header, rows, "2023-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    base = {"title": "t", "statement": "s", "problem_link": "p", "table": "ds1", "time_column": "datum",
            "entity_column": "org", "value": "amount", "aggregation": "sum", "period": "month", "steps": []}
    fc = db.add_experiment(hid, None, None, "s", "MASE", "last_value", "MASE o aspoň 10 %",
                           base | {"task": "forecast", "primary_metric": "MASE", "primary_baseline": "last_value",
                                   "success_threshold": 10}, task="forecast", problem_link="p")
    an = db.add_experiment(hid, None, None, "s", "coverage95_error", "seasonal_naive", "x",
                           base | {"task": "anomaly", "primary_metric": "coverage95_error", "primary_baseline": "seasonal_naive",
                                   "success_threshold": 0}, task="anomaly", problem_link="p")
    for x in (fc, an):
        gate2.lock(cfg, db, x)
    assert gate2.run_backtest(cfg, db, Deadline(10), [hid]) == {"done": 2}
    res = json.loads(db.get_experiment(fc)["result_json"])
    table = {r["postup"]: r for r in res["tabuľka"]}
    assert set(table) == {"ets", "pooled_boosting", "last_value", "mean", "seasonal_naive", "drift"}
    assert all(set(exp_plan.METRICS["forecast"]) <= set(r) for r in table.values())
    assert res["hlavné"]["zlepšenie_pct"] > 0 and table["seasonal_naive"]["MASE"] < table["last_value"]["MASE"]
    assert res["detaily"]["entity"] == 20 and len(res["detaily"]["testovacie_obdobia"]) == 3
    ares = json.loads(db.get_experiment(an)["result_json"])
    assert ares["položky"][0]["entita"] == "O0" and ares["položky"][0]["obdobie"] == "2025-12-01"
    assert "najväčšie_riadky" in ares["položky"][0] and "pokrytie95" in ares["tabuľka"][0]


def test_threshold_bunching_and_comparison_lift(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Delenie zákaziek", "", "obce")
    q = claim(db, hid, "Limit zákazky", "Zákazka s nízkou hodnotou do 50 000 eur sa nemusí súťažiť.")
    rng = np.random.default_rng(3)
    rows = [(f"E{i % 30}", "2025-03-01", round(float(rng.lognormal(10.3, 0.6)), 2), f"K{i % 5}") for i in range(6000)]
    rows += [("E99", "2025-03-01", round(float(rng.uniform(45500, 49990)), 2), "K1") for _ in range(250)]
    dataset(cfg, db, hid, "buyer;datum;amount;cat", rows, "2025-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    plan = {"title": "t", "statement": "s", "problem_link": "p", "task": "threshold", "table": "ds1", "time_column": "datum",
            "entity_column": "buyer", "value": "amount", "thresholds": [50000], "threshold_claims": [q], "steps": [],
            "primary_metric": "excess_ratio", "primary_baseline": "smooth_density", "success_threshold": 10}
    check = exp_plan.validate_plan(cfg, db, hid, plan, {q: db.evidence_for(hid)[0]["quote"]})
    assert check["chyby"] == [] and check["upozornenia"] == []  # prah 50 000 je doslova v citáte
    assert any("nie je doslova" in w for w in exp_plan.validate_plan(cfg, db, hid, plan | {"thresholds": [40000]}, {})["upozornenia"])
    exp = db.add_experiment(hid, None, None, "s", "excess_ratio", "smooth_density", exp_plan.success_rule(plan), plan,
                            task="threshold", problem_link="p")
    gate2.lock(cfg, db, exp)
    gate2.run_backtest(cfg, db, Deadline(5), [hid])
    res = json.loads(db.get_experiment(exp)["result_json"])
    assert res["hlavné"]["zlepšenie_pct"] > 50 and res["hlavné"]["splnené"] and res["položky"][0]["entita"] == "E99"
    comp = {"title": "t", "statement": "s", "problem_link": "p", "task": "comparison", "table": "ds1", "time_column": "datum",
            "value": "amount", "category": "cat", "label": "hi", "steps": [{"op": "derive", "name": "hi",
                                                                             "expr": "amount > 60000"}],
            "primary_metric": "lift", "primary_baseline": "random", "success_threshold": 50}
    exp2 = db.add_experiment(hid, None, None, "s", "lift", "random", exp_plan.success_rule(comp), comp, task="comparison",
                             problem_link="p")
    gate2.lock(cfg, db, exp2)
    gate2.run_backtest(cfg, db, Deadline(5), [hid])
    res2 = json.loads(db.get_experiment(exp2)["result_json"])
    assert {r["postup"]: r for r in res2["tabuľka"]}["horný decil kategórie"]["lift"] > 2


# ======================================================================= záver
def test_conclusion_from_facts_rejects_invented_numbers_and_writes_report(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    card = db.add_card(hid, {"decision": {"text": "Výber zmlúv na kontrolu", "claims": []}, "who": {"text": "kontrolór"}})
    exp = db.add_experiment(hid, None, None, "Model nájde zmluvy s dodatkom.", "precision@k", "random",
                            exp_plan.success_rule(RANKING_PLAN), RANKING_PLAN, task="ranking", card_id=card,
                            problem_link="výber zmlúv")
    gate2.lock(cfg, db, exp)
    gate2.run_backtest(cfg, db, Deadline(5), [hid])
    rec.responses["Conclusion"] = [{"what_tested": "t", "result_for_decision": "Ušetrí 777 hodín.", "robustness": "",
                                    "limits": "", "interview_questions": ["Ako vyberáte zmluvy?"]},
                                   {"what_tested": "Poradie zmlúv.", "result_for_decision": "Model je lepší ako náhodný výber.",
                                    "robustness": "Interval je kladný.", "limits": "Štítok je náhrada.",
                                    "interview_questions": ["Koľko zmlúv ročne preveríte?"]}]
    assert exp_conclude.run_conclusions(cfg, db, router, Deadline(1), [hid]) == {"conclusions_kód": 1}
    e = db.get_experiment(exp)
    concl = json.loads(e["conclusion_json"])
    assert "777" in concl["poznámka"] and concl["text"]["splnené"] is True
    assert any(f.startswith("F4: Z prvých 50") for f in concl["fakty"])
    from radar.exp_tasks import strongest_baseline
    strong = strongest_baseline(json.loads(e["result_json"]))
    assert strong["naivný"] != "random" or strong["je_hlavný"]  # najsilnejší naivný odhad je známy
    if not strong["je_hlavný"]:
        assert any(f.startswith("F9: Najsilnejší naivný odhad") for f in concl["fakty"])
    html_text = open(e["demo_path"], encoding="utf-8").read()
    assert "Výsledková tabuľka" in html_text and "logistic" in html_text and "Ušetrí" not in html_text
    db.update_experiment(exp, conclusion_json=None)
    exp_conclude.run_conclusions(cfg, db, router, Deadline(1), [hid])
    concl = json.loads(db.get_experiment(exp)["conclusion_json"])
    assert concl["zdroj"] == "model" and concl["text"]["interview_questions"] == ["Koľko zmlúv ročne preveríte?"]
    db.update_experiment(exp, conclusion_json=None)
    rec.responses["Conclusion"] = {"what_tested": "Poradie zmlúv.", "result_for_decision": "Model je lepší.",
                                   "robustness": "", "limits": "", "interview_questions": ["Preveríte o 200 viac?", "Ako vyberáte?"]}
    exp_conclude.run_conclusions(cfg, db, router, Deadline(1), [hid])
    concl = json.loads(db.get_experiment(exp)["conclusion_json"])
    assert concl["zdroj"] == "model" and concl["text"]["interview_questions"] == ["Ako vyberáte?"]  # vypadne iba otázka


# ======================================================================= iná tabuľka (lookup) a NKÚ
def test_lookup_joins_other_table_by_ico_within_time_window(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola", "", "obce")
    dataset(cfg, db, hid, "cid;buyer;datum", [(1, "00313271", "2024-01-10"), (2, "313271", "2025-06-01"),
                                              (3, "00999999", "2024-03-01")], "2024-01-01", "2025-12-31")
    dataset(cfg, db, hid, "ico;koniec_kontroly;typ", [("00313271", "2024-06-30", "Finančná nezrovnalosť"),
                                                      ("00313271", "2025-03-01", "Formálna nezrovnalosť")],
            "2024-01-01", "2025-12-31", name="ds2")
    ws = Workspace(cfg, hid, "ds1", "datum", {"od": "2024-01-01", "do": "2025-12-31"})
    ws.apply(Step(op="lookup", name="fin_after", table="ds2", keys=["buyer"], target_column="ico", match="ico",
                  other_time="koniec_kontroly", days=365, expr="typ = 'Finančná nezrovnalosť'"))
    ws.apply(Step(op="lookup", name="before", table="ds2", keys=["buyer"], target_column="ico", match="ico",
                  other_time="koniec_kontroly", days=-730))
    got = {r[0]: r[1:] for r in ws.query(f"SELECT cid, fin_after, before FROM {ws.table} ORDER BY cid")}
    assert got == {1: (1, 0), 2: (0, 2), 3: (0, 0)}  # IČO „313271“ = „00313271“; zistenie po podpise vs. pred
    assert "fin_after" in ws.future and "before" not in ws.future  # budúcnosť iba ako štítok
    ws.close()
    bad = RANKING_PLAN | {"steps": [{"op": "lookup", "name": "x", "table": "ds77", "keys": ["buyer"], "target_column": "ico"}]}
    assert any("iná tabuľka z DATA" in e for e in exp_plan.validate_plan(cfg, db, hid, bad)["chyby"])


NKU_FINDINGS = ("Kontrolované obdobie,Začiatok kontroly,Koniec kontroly,Výsledky kontroly,Linka (URL) na výsledok kontroly,"
                "Znenie zistenia,Typ nedostatku,Porušený predpis,Číslo KA,Názov kontrolnej akcie,Skrátený názov KA,Etapa KA,"
                "Účel KA,Kontrolovaný subjekt,Linka (URL) na informácie o kontroľovatelnom subjekte\n"
                "2020,01.02.2023,30.06.2023,a.pdf,https://x/a.pdf,Obec nezverejnila zmluvu,Odchýlka od súladu,211/2000,KA-1,"
                "Zverejňovanie,Zv,Správa,Účel,Obec Fiktívna,https://www.nku.gov.sk/s?_kisdataPortlet_entryId=11\n"
                "2021,01.03.2024,30.09.2024,b.pdf,https://x/b.pdf,Prekročený rozpočet,Finančná nezrovnalosť,523/2004,KA-2,"
                "Rozpočet,Ro,Správa,Účel,Mesto Vzorové,https://www.nku.gov.sk/s?_kisdataPortlet_entryId=12\n")
NKU_SUBJECTS = ("Názov subjektu,Linka (URL) na informácie o kontrolovateľnom subjekte,Adresa,IČO\n"
                "Obec Fiktívna,https://www.nku.gov.sk/s?_kisdataPortlet_entryId=11,Hlavná 1,00313271\n"
                "Mesto Vzorové,https://www.nku.gov.sk/s?_kisdataPortlet_entryId=12,Námestie 2,603481\n")


def test_nku_findings_get_ico_without_addresses_and_dataset_is_shared_with_subhypotheses(tmp_path):
    import httpx

    from radar.datasets import share_datasets
    from tests.fakes import mock_fetcher

    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola", "", "obce")
    dataset(cfg, db, hid, "cid;datum", [(1, "2025-01-01")], "2025-01-01", "2025-12-31")
    child = db.create_hypothesis("Podproblém", "", "obce")
    db.set_parent(child, hid)
    share_datasets(cfg, child, hid)
    page = ('<a href="/documents/1/kontrolne_zistenia_20260101.csv/a?download=true">old</a>'
            '<a href="/documents/1/kontrolne_zistenia_20261001.csv/b?download=true">new</a>'
            '<a href="/documents/1/zoznam_kontrolovatelnych_subj_20261001.csv/c?download=true">s</a>')
    routes = {"https://www.nku.gov.sk/datasety": httpx.Response(200, text=page, headers={"content-type": "text/html"}),
              "https://www.nku.gov.sk/documents/1/kontrolne_zistenia_20261001.csv/b?download=true":
                  httpx.Response(200, content=NKU_FINDINGS.encode(), headers={"content-type": "text/csv"}),
              "https://www.nku.gov.sk/documents/1/zoznam_kontrolovatelnych_subj_20261001.csv/c?download=true":
                  httpx.Response(200, content=NKU_SUBJECTS.encode(), headers={"content-type": "text/csv"})}
    stats = gate1.add_dataset_url(cfg, db, hid, "https://www.nku.gov.sk/datasety", "NKÚ zistenia", fetcher=mock_fetcher(routes))
    assert stats["stiahnuté"] and stats["datasets"] == 1
    ds = [d for d in db.datasets_for(hid) if "nku" in d["url"]][0]
    cols = json.loads(ds["columns_json"])
    assert ds["status"] == "ok" and "ico" in cols and not any("adres" in c.lower() for c in cols)
    assert sorted(run_query(cfg, hid, f"SELECT ico, typ_nedostatku FROM {ds['table_name']}")) == [
        ("00313271", "Odchýlka od súladu"), ("00603481", "Finančná nezrovnalosť")]
    assert [d["table_name"] for d in db.datasets_for(child) if "nku" in d["url"]] == [ds["table_name"]]
    assert db.fields_for(hid)[0]["name"] == gate1.MANUAL_FIELD


def test_plan_quality_is_measured_and_optional_auto_lock(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    db.add_card(hid, {"decision": {"text": "Výber zmlúv na kontrolu", "claims": []}})
    cfg.experiments.plans_per_hypothesis = 1
    rec.responses["PlanSet"] = {"experiments": [RANKING_PLAN | {"features": ["x", "zle"]}]}
    rec.responses["ExperimentPlan"] = RANKING_PLAN
    exp_plan.run_plans(cfg, db, router, Deadline(5), [hid])
    q = exp_plan.plan_quality(db)
    assert q["návrhy"] == 1 and q["bez_chýb_po_oprave"] == 1 and q["bez_chýb_na_prvý_pokus"] == 0
    e = db.experiments_for(hid)[0]
    gate2.lock(cfg, db, e["id"])
    assert exp_plan.plan_quality(db)["uzamknuté_bez_úprav"] == 1  # človek plán nezmenil
    db.set_setting(exp_plan.AUTO_LOCK_KEY, {"enabled": True})
    cfg.experiments.plans_per_hypothesis = 2
    other = [dict(st, days=200) if st["op"] == "linked_agg" else st for st in RANKING_PLAN["steps"]]  # iný štítok
    rec.responses["PlanSet"] = {"experiments": [RANKING_PLAN | {"title": "Druhý", "capacity": 40, "steps": other,
                                                                "primary_baseline": "entity_rate"}]}  # bez upozornenia
    assert exp_plan.run_plans(cfg, db, router, Deadline(5), [hid])["auto_locked"] == 1
    second = db.experiments_for(hid)[1]
    assert second["status"] == "locked" and json.loads(second["flags_json"])["uzamknuté_automaticky"]


def test_planner_may_say_data_cannot_test_decision_and_waits_for_new_dataset(tmp_path):
    """H581 (náklady investičných projektov): v CRZ a NKÚ nie je hodnotenie ÚHP – návrh nemá vynútiť náhradný štítok."""
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Neoveriteľné náklady projektov", "", "ministerstvá")
    dataset(cfg, db, hid, "cid;datum;suma", [(1, "2025-01-01", 5)], "2025-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    card = db.add_card(hid, {"decision": {"text": "Úroveň detailu rozpočtu projektu", "claims": []}})
    seen = []
    rec.responses["PlanSet"] = lambda m: seen.append(m) or {"experiments": [], "missing_data": "hodnotenia projektov ÚHP"}
    assert exp_plan.run_plans(cfg, db, router, Deadline(5), [hid]) == {"no_suitable_data": 1}
    assert "EXAMPLE of one complete experiment" in seen[0][0]["content"] and "missing_data" in seen[0][0]["content"]
    assert json.loads(db.get_card(card)["card_json"])["chýbajúce_dáta_pre_experiment"]["text"] == "hodnotenia projektov ÚHP"
    assert exp_plan.plan_targets(cfg, db, None) == []  # bez nového datasetu sa neopakuje (autopilot)
    dataset(cfg, db, hid, "projekt;datum;hodnotenie", [(1, "2025-02-01", "negatívne")], "2025-01-01", "2025-12-31", name="ds2")
    assert [h["id"] for h, _ in exp_plan.plan_targets(cfg, db, None)] == [hid]


def test_precheck_lists_all_missing_step_parameters_and_fills_unambiguous_ico(tmp_path):
    """27B v reálnom behu vynechal pri lookup keys, target_column aj days a opravu zopakoval bez zmeny."""
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola", "", "obce")
    dataset(cfg, db, hid, "cid;ico_objednavatel;ico_dodavatel;datum;suma", [(1, "1", "2", "2024-01-10", 5)],
            "2024-01-01", "2025-12-31")
    dataset(cfg, db, hid, "ico;koniec_kontroly;typ", [("1", "2024-06-30", "x")], "2024-01-01", "2025-12-31", name="ds2")
    plan = RANKING_PLAN | {"value": "", "primary_baseline": "value_desc", "steps": [
        {"op": "lookup", "name": "nku_past", "table": "ds2", "agg": "count", "other_time": "koniec_kontroly"},
        {"op": "future_agg", "name": "later", "keys": ["ico_objednavatel"], "agg": "sum"}]}
    errors = exp_plan.precheck_steps(cfg, hid, plan)
    joined = " | ".join(errors)
    assert '"keys"' in joined and '["ico_objednavatel"] alebo ["ico_dodavatel"]' in joined
    assert '"days"' in joined and "kladné" in joined and 'agg sum potrebuje "column"' in joined
    assert "value_desc potrebuje" in joined and plan["steps"][0]["target_column"] == "ico"  # jediné IČO doplnil kód
    assert plan["steps"][0]["match"] == "ico"
    short = RANKING_PLAN | {"steps": [{"op": "lookup", "name": "x", "table": "ds2", "keys": ["ico_objednavatel"],
                                       "target_column": "ico", "other_time": "koniec_kontroly", "days": -1}]}
    assert any("-3650" in e for e in exp_plan.precheck_steps(cfg, hid, short))  # -1 = jeden deň, nie celá história


def test_expression_feature_becomes_column_and_duplicate_plan_is_rejected(tmp_path):
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Kontrola zmlúv", "", "obce")
    header, rows = contracts_with_amendments()
    dataset(cfg, db, hid, header, rows, "2024-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    db.add_card(hid, {"decision": {"text": "Výber zmlúv na kontrolu", "claims": []}})
    plan = RANKING_PLAN | {"features": ["x", "TRY_CAST(amount AS DOUBLE) / 1000"], "steps": list(RANKING_PLAN["steps"])}
    assert exp_plan.validate_plan(cfg, db, hid, plan)["chyby"] == []
    assert plan["features"][1] == "priznak_2" and plan["steps"][-1]["expr"] == "TRY_CAST(amount AS DOUBLE) / 1000"
    cfg.experiments.plans_per_hypothesis = 2
    rec.responses["PlanSet"] = {"experiments": [RANKING_PLAN]}
    rec.responses["ExperimentPlan"] = RANKING_PLAN | {"title": "Rovnaké znova"}
    stats = exp_plan.run_plans(cfg, db, router, Deadline(5), [hid])
    assert stats["plans_ok"] == 1 and stats["plans_with_errors"] == 1  # druhý je duplikát aj po opravách
    dup = db.experiments_for(hid)[1]
    assert any("meria to isté ako E" in e and '"task"' in e for e in json.loads(dup["flags_json"])["chyby_návrhu"])
    with_cat = {"steps": [{"op": "text_category", "name": "predmet_cat"}, {"op": "derive", "name": "price_ratio"}]}
    assert '"category": "predmet_cat"' in exp_plan.alternatives(with_cat) and "anomaly" in exp_plan.alternatives(with_cat)


def test_hypothesis_without_any_valid_plan_is_marked_and_skipped(tmp_path):
    """Autopilot by inak H581 skúšal každé kolo (neplatné návrhy sa nerátajú) – ~15 min 27B zakaždým."""
    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Neoveriteľné náklady", "", "ministerstvá")
    dataset(cfg, db, hid, "cid;datum;suma", [(1, "2025-01-01", 5)], "2025-01-01", "2025-12-31")
    gate1.decide(db, hid, 1, "passed", "test")
    card = db.add_card(hid, {"decision": {"text": "Detail rozpočtu", "claims": []}})
    cfg.experiments.plans_per_hypothesis = 2
    hopeless = RANKING_PLAN | {"label": "neexistuje"}
    rec.responses["PlanSet"] = {"experiments": [hopeless]}
    rec.responses["ExperimentPlan"] = hopeless
    stats = exp_plan.run_plans(cfg, db, router, Deadline(5), [hid])
    assert stats["plans_with_errors"] == 2 and stats["no_valid_plan"] == 1
    assert json.loads(db.get_card(card)["card_json"])["chýbajúce_dáta_pre_experiment"]["automaticky"] is True
    assert exp_plan.plan_targets(cfg, db, None) == []


def test_label_on_few_entities_is_flagged_and_nku_supplier_join_warned(tmp_path):
    """E6: štítok NKÚ podľa IČO dodávateľa mali iba verejné inštitúcie (206 z 88 759) – vlastnosť entity, nie zmluvy."""
    from radar.exp_conclude import code_conclusion, facts
    from radar.exp_ops import label_concentration

    cfg, db, rec, router = setup(tmp_path)
    hid = db.create_hypothesis("Riziká obstarávania", "", "kontrolóri")
    rows = [(i, f"E{i % 200}", (date(2025, 1, 1) + timedelta(days=i % 300)).isoformat(),
             1 if i % 200 < 2 else 0, 1 if i % 97 == 0 else 0) for i in range(2000)]
    dataset(cfg, db, hid, "cid;ent;datum;fixed;spread", rows, "2025-01-01", "2025-12-31")
    ws = Workspace(cfg, hid, "ds1", "datum", {"od": "2025-01-01", "do": "2025-12-31"})
    conc = label_concentration(ws, "fixed", "ent")
    assert conc["entity_so_štítkom"] == 2 and conc["entity"] == 200 and conc["sústredený"]
    assert conc["očakávané_entity"] > 15 and conc["najčastejšie"][0]["štítkov"] == 10
    assert not label_concentration(ws, "spread", "ent")["sústredený"]  # rozptýlený štítok
    ws.close()
    exp = db.add_experiment(hid, None, None, "Poradie", "precision@k", "random", "",
                            {"task": "ranking", "table": "ds1", "entity_column": "ent"}, task="ranking")
    res = {"hlavné": {}, "detaily": {"koncentrácia_štítku": conc}}
    fl = facts(None, db.get_experiment(exp), res)
    assert any(f.startswith("F10: Štítok má iba 2 z 200 entít") for f in fl)
    assert "KTORÉ entity" in code_conclusion(None, db.get_experiment(exp), res, fl)["limits"]
    db.add_dataset(hid, None, "https://www.nku.gov.sk/datasety", "ds9", status="ok", profile_json={})
    plan = {"steps": [{"op": "lookup", "name": "nku", "table": "ds9", "keys": ["ico_dodavatel"], "days": 365}]}
    assert exp_plan.source_warnings(db, hid, plan) and not exp_plan.source_warnings(
        db, hid, {"steps": [{"op": "lookup", "name": "nku", "table": "ds9", "keys": ["ico_objednavatel"], "days": 365}]})


def test_conclusion_number_check_reads_thousands_separators():
    from radar.exp_conclude import _numbers

    assert _numbers("239 z 75 726 entít, 2024 2025, 0,536") == {"239", "75726", "2024", "2025", "0.536"}
    assert _numbers("75\u00a0726 a 1 234 567") == {"75726", "1234567"}

