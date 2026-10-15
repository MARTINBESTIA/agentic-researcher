"""Spätné testy experimentov Brány 2 podľa typu úlohy (počíta iba Python: numpy, scikit-learn, statsmodels).

Každý test vráti výsledkovú tabuľku – všetky modely aj naivné odhady × všetky metriky typu úlohy – a hlavné
porovnanie z predregistrácie (hlavná metrika, hlavný naivný odhad, prah). Model sa vyberá na validačnom období,
hodnotí na testovacom (neskoršom) období; interval je bootstrap. Protokol prípravy dát je z `exp_ops`.

- ranking: logistická regresia a gradient boosting proti náhodnému výberu, „najväčšie najprv“, počtu varovných
  príznakov a doterajšej miere entity; precision@k, recall@k, priemerná presnosť, ROC AUC.
- forecast / anomaly: rad za obdobie (a entitu – spoločný model cez entity); exponenciálne vyrovnávanie (ETS)
  a spoločný gradient boosting na oneskorených hodnotách proti naivným odhadom; MASE, MAE, RMSE, sMAPE, MAPE;
  pri anomáliách intervaly z chýb na validačnom období (konformne) a označené obdobia s rozpadom na riadky.
- threshold: nadbytok hodnôt tesne pod prahom oproti hladkému rozdeleniu (polynóm mimo okolia prahu).
- comparison: hodnota oproti podobným v tej istej kategórii (horný decil), presnosť a lift so štítkom.
"""

from __future__ import annotations

import json
import logging
import warnings
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np

from .config import Config
from .datasets import DatasetError
from .db import DB
from .exp_ops import Step, Workspace, check_expr, label_concentration, q
from .exp_plan import METRICS

log = logging.getLogger(__name__)

RNG_SEED = 0
EXPERIMENT_CODE_VERSION = "2026-10-15"  # verzia výpočtu (bez úniku po foldoch, dozretie, silné porovnanie, osi)


# ======================================================================= spoločné
def improvement(model: float | None, base: float | None, direction: str) -> float | None:
    """Zlepšenie v % proti naivnému odhadu (kladné = lepšie)."""
    if model is None or base is None or not np.isfinite(model) or not np.isfinite(base):
        return None
    if direction == "lower":
        return (1 - model / base) * 100 if base else None
    return (model - base) / abs(base) * 100 if base else (100.0 if model > 0 else 0.0)


def strongest_baseline(res: dict[str, Any]) -> dict[str, Any] | None:
    """Najsilnejší naivný odhad na hlavnej metrike a zlepšenie modelu proti nemu – predregistrovaný naivný odhad
    môže byť slabý (E6: „najväčšie zmluvy“ +900 %, doterajšia miera dodávateľa o rád silnejšia)."""
    prim = res.get("hlavné") or {}
    metric, direction = prim.get("metrika"), prim.get("smer", "higher")
    rows = [r for r in res.get("tabuľka") or [] if r.get("typ") == "naivný" and r.get(metric) is not None]
    if not metric or not rows or prim.get("model_hodnota") is None:
        return None
    best = (max if direction == "higher" else min)(rows, key=lambda r: r[metric])
    imp = improvement(prim["model_hodnota"], best[metric], direction)
    return {"naivný": best["postup"], "hodnota": best[metric], "zlepšenie_pct": None if imp is None else round(imp, 2),
            "je_hlavný": best["postup"] == prim.get("naivný")}


def _primary(plan: dict[str, Any], table: list[dict[str, Any]], chosen: str, ci: list[float] | None) -> dict[str, Any]:
    metric, base = plan["primary_metric"], plan["primary_baseline"]
    direction = next((m[metric] for m in METRICS.values() if metric in m), "higher")
    mv = next((r.get(metric) for r in table if r["postup"] == chosen), None)
    bv = next((r.get(metric) for r in table if r["postup"] == base), None)
    imp = improvement(mv, bv, direction)
    thr = float(plan.get("success_threshold") or 0)
    return {"metrika": metric, "smer": direction, "model": chosen, "model_hodnota": mv, "naivný": base,
            "naivný_hodnota": bv, "zlepšenie_pct": None if imp is None else round(imp, 2), "ci95_rozdielu": ci,
            "prah_pct": thr, "splnené": bool(imp is not None and imp >= thr and (ci is None or
                                                                                 (ci[0] > 0 if direction == "higher" else ci[1] < 0)))}


def _r(x: Any, d: int = 4) -> Any:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return round(f, d) if np.isfinite(f) else None


def _text_column(ws: Workspace) -> str | None:
    """Najdlhší textový stĺpec (popis riadku v zozname na kontrolu, napr. predmet zmluvy)."""
    best, length = None, 0.0
    for name, ctype, *_ in ws.query(f"DESCRIBE {ws.table}"):
        if ctype == "VARCHAR" and not name.startswith("_"):
            avg = ws.query(f"SELECT AVG(length({q(name)})) FROM (SELECT {q(name)} FROM {ws.table} LIMIT 5000)")[0][0] or 0
            if avg > length and avg > 15:
                best, length = name, avg
    return best


# ======================================================================= ranking
def _top_k(scores: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    k = min(k, len(scores))
    tie = rng.random(len(scores)) * 1e-9  # zhody náhodne, ale opakovateľne
    s = np.where(np.isfinite(scores), scores, -np.inf) + tie
    return np.argpartition(-s, k - 1)[:k] if k > 0 else np.array([], dtype=int)


def rank_metrics(y: np.ndarray, scores: np.ndarray | None, k: int, rng: np.random.Generator,
                 only: str | None = None) -> dict[str, float | None]:
    """Metriky poradia; `only` = iba jedna (bootstrap stovky ráz – priemerná presnosť a AUC triedia celý test)."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    pos = y.sum()
    if scores is None:  # náhodný výber: očakávané hodnoty
        prev = pos / len(y) if len(y) else 0
        out = {"precision@k": prev, "recall@k": min(k, len(y)) / len(y) if len(y) else None, "avg_precision": prev,
               "roc_auc": 0.5}
        return {only: out[only]} if only else out
    want = (only,) if only else ("precision@k", "recall@k", "avg_precision", "roc_auc")
    out: dict[str, float | None] = {}
    if {"precision@k", "recall@k"} & set(want):
        top = _top_k(scores, k, rng)
        out["precision@k"] = float(y[top].mean()) if len(top) else None
        out["recall@k"] = float(y[top].sum() / pos) if pos else None
    if {"avg_precision", "roc_auc"} & set(want):
        s = np.nan_to_num(scores, nan=-1e18, neginf=-1e18)
        both = 0 < pos < len(y)
        out["avg_precision"] = float(average_precision_score(y, s)) if both else None
        out["roc_auc"] = float(roc_auc_score(y, s)) if both else None
    return {m: out.get(m) for m in want}


class _Encoder:
    """Číselné príznaky (doplnenie mediánom + indikátor) a kategórie (najčastejších 30, inak „iné“)."""

    def __init__(self, num: np.ndarray, cat: np.ndarray):
        self.med = np.nanmedian(num, axis=0) if num.size else np.array([])
        self.med = np.where(np.isfinite(self.med), self.med, 0.0) if num.size else self.med
        self.levels = []
        for j in range(cat.shape[1]):
            vals, counts = np.unique(cat[:, j].astype(str), return_counts=True)
            self.levels.append(list(vals[np.argsort(-counts)][:30]))

    def dense(self, num: np.ndarray, cat: np.ndarray) -> np.ndarray:
        parts = [num] if num.size else []
        for j, lv in enumerate(self.levels):
            idx = {v: i for i, v in enumerate(lv)}
            parts.append(np.array([idx.get(v, -1) for v in cat[:, j].astype(str)], dtype=float)[:, None])
        return np.hstack(parts) if parts else np.zeros((len(num), 0))

    def onehot(self, num: np.ndarray, cat: np.ndarray, scaler=None):
        filled = np.where(np.isnan(num), self.med, num) if num.size else num
        ind = np.isnan(num).astype(float) if num.size else num
        parts = [filled, ind] if num.size else []
        for j, lv in enumerate(self.levels):
            col = cat[:, j].astype(str)
            parts.append(np.stack([(col == v).astype(float) for v in lv], axis=1) if lv else np.zeros((len(col), 0)))
        return np.hstack(parts) if parts else np.zeros((len(cat), 0))


def entity_rates(y: np.ndarray, t: np.ndarray, known_at: np.ndarray, ents: np.ndarray, cutoff: Any,
                 m: float = 5.0) -> np.ndarray:
    """Doterajšia miera entity bez úniku (audit 14. 10. 2026, P0 3): riadok pred hranicou `cutoff` vidí iba štítky
    známe pred svojím dňom (priebežný historický súhrn), riadok od hranice iba štítky známe pred hranicou (zmrazené).
    Štítok riadku j je známy dňom `known_at[j]` (dátum + okno štítka + oneskorenie zverejnenia). Prior (miera všetkých)
    rovnako – validačné ani testovacie odpovede sa do príznakov učenia nedostanú."""
    n = len(y)
    out = np.full(n, np.nan)
    order = np.argsort(known_at, kind="stable")
    pos: dict[str, float] = {}
    cnt: dict[str, int] = {}
    tot = [0.0, 0]
    k = 0

    def release(limit) -> None:
        nonlocal k
        while k < n and known_at[order[k]] < limit:
            j = order[k]
            pos[ents[j]] = pos.get(ents[j], 0.0) + y[j]
            cnt[ents[j]] = cnt.get(ents[j], 0) + 1
            tot[0] += y[j]
            tot[1] += 1
            k += 1

    def rate(e: str) -> float:
        prior = tot[0] / tot[1] if tot[1] else 0.0
        return (pos.get(e, 0.0) + prior * m) / (cnt.get(e, 0) + m)

    for i in np.argsort(t, kind="stable"):
        if t[i] >= cutoff:
            break
        release(t[i])
        out[i] = rate(ents[i])
    release(cutoff)
    for i in np.where(t >= cutoff)[0]:
        out[i] = rate(ents[i])
    return out


def _by_unit(y: np.ndarray, scores: np.ndarray | None, ents: np.ndarray | None) -> tuple[np.ndarray, np.ndarray | None]:
    """Jednotka rozhodnutia „entity“: subjekt má štítok, keď ho má niektorý jeho riadok; skóre = najvyššie z riadkov."""
    if ents is None:
        return y, scores
    uniq, inv = np.unique(ents, return_inverse=True)
    yy = np.zeros(len(uniq), dtype=int)
    np.maximum.at(yy, inv, y)
    if scores is None:
        return yy, None
    ss = np.full(len(uniq), -np.inf)
    np.maximum.at(ss, inv, np.where(np.isfinite(scores), scores, -np.inf))
    return yy, ss


def clusters_of(ents: np.ndarray | None, t: np.ndarray) -> tuple[np.ndarray, str]:
    """Zhluky pre bootstrap: entity (záznamy toho istého subjektu nie sú nezávislé), inak kalendárne mesiace."""
    if ents is not None:
        return np.unique(ents, return_inverse=True)[1], "po entitách"
    months = np.asarray(t, dtype="datetime64[M]")
    return np.unique(months, return_inverse=True)[1], "po časových blokoch (mesiace)"


def cluster_bootstrap(groups: np.ndarray, stat: Callable[[np.ndarray], float | None], n_boot: int,
                      rng: np.random.Generator) -> list[float] | None:
    """95 % interval štatistiky pri prevzorkovaní celých zhlukov (entít alebo časových blokov) s opakovaním."""
    ids = np.unique(groups)
    members = [np.where(groups == g)[0] for g in ids]
    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(ids), size=len(ids))
        idx = np.concatenate([members[i] for i in pick]) if len(pick) else np.array([], dtype=int)
        v = stat(idx)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return [round(float(np.percentile(vals, 2.5)), 4), round(float(np.percentile(vals, 97.5)), 4)]


def _ranking(cfg: Config, ws: Workspace, plan: dict[str, Any]) -> dict[str, Any]:
    """Poradie bez úniku: hranice učenie | validácia | test podľa času; štítky iba dozreté pred každou hranicou
    (dátum + okno štítka + oneskorenie zverejnenia); príznaky odvodené zo štítkov (miera entity) po foldoch;
    silný naivný odhad vybraný na validácii a zmrazený pred testom; interval bootstrap po entitách/časových blokoch."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    from .exp_plan import apply_population

    label, feats = plan["label"], [f for f in plan["features"] if f]
    apply_population(ws, plan)
    horizon = ws.matured(label)
    value = (plan.get("value") or "").strip()
    ent = plan.get("entity_column") or ""
    unit_entity = (plan.get("decision_unit") or "row") == "entity" and bool(ent)
    sel = ", ".join(f"{q(f)} AS f{i}" for i, f in enumerate(feats))
    data = ws.con.execute(
        f"SELECT _rid, _t, {sel}, CASE WHEN TRY_CAST({q(label)} AS DOUBLE) > 0 THEN 1 ELSE 0 END AS _y"
        + (f", TRY_CAST(({check_expr(value)}) AS DOUBLE) AS _value" if value else "")
        + (f", CAST({q(ent)} AS VARCHAR) AS _ent" if ent else "")
        + f" FROM {ws.table} ORDER BY _t, _rid").fetchnumpy()
    n = len(data["_y"])
    if n < 100:
        raise DatasetError(f"málo riadkov so štítkom ({n})")
    y = np.asarray(data["_y"], dtype=int)
    t = np.asarray(data["_t"]).astype("datetime64[D]")
    known_at = t + np.timedelta64(int(horizon), "D")  # deň, keď je štítok riadku známy
    ents_all = np.asarray([str(v) for v in np.ma.filled(data["_ent"], "")], dtype=object) if ent else None
    base_num, cat_cols = [], []
    for i, f in enumerate(feats):
        col = data[f"f{i}"]
        try:
            arr = np.asarray(np.ma.filled(np.ma.asarray(col, dtype=float), np.nan), dtype=float)
            base_num.append((f, arr))
        except (TypeError, ValueError):
            cat_cols.append((f, np.asarray([("∅" if v is None else str(v)) for v in np.ma.filled(col, None)], dtype=object)))
    cat = np.column_stack([a for _, a in cat_cols]) if cat_cols else np.zeros((n, 0), dtype=object)
    test_from = int(n * (1 - cfg.experiments.test_share))
    val_from = int(test_from * 0.8)
    t_val, t_test = t[val_from], t[test_from]
    idx = np.arange(n)
    train_val = (idx < val_from) & (known_at < t_val)  # učenie pre výber modelu: štítky známe pred validáciou
    train_test = (idx < test_from) & (known_at < t_test)  # učenie pre test: štítky známe pred testom
    val_rows, test_rows = np.arange(val_from, test_from), np.arange(test_from, n)
    if y[test_rows].sum() < 5 or y[train_val].sum() < 5:
        raise DatasetError("málo pozitívnych prípadov v učení (dozretých pred hranicou) alebo v teste"
                           + (f" – štítok je známy až {horizon} dní po dátume riadku" if horizon else ""))

    def fold_num(cutoff) -> tuple[np.ndarray, list[str]]:
        """Číselné príznaky pre fold s hranicou `cutoff` (miera entity sa počíta pre každý fold zvlášť)."""
        cols = list(base_num)
        if ent:
            cols.append(("miera_entity", entity_rates(y, t, known_at, ents_all, cutoff)))
        num = np.column_stack([a for _, a in cols]) if cols else np.zeros((n, 0))
        return num, [f for f, _ in cols]

    num_val, names = fold_num(t_val)
    num_test, _ = fold_num(t_test)
    names = names + [f for f, _ in cat_cols]
    rng = np.random.default_rng(RNG_SEED)
    k = int(plan.get("capacity") or 50)
    primary = plan["primary_metric"]
    unit_ents = (lambda rows: ents_all[rows]) if unit_entity else (lambda rows: None)

    def evaluate(rows: np.ndarray, scores: np.ndarray | None, kk: int, only: str | None = None) -> dict[str, float | None]:
        yy, ss = _by_unit(y[rows], scores, unit_ents(rows))
        return rank_metrics(yy, ss, kk, rng, only=only)

    def fit(kind: str, num: np.ndarray, mask: np.ndarray):
        enc = _Encoder(num[mask], cat[mask])
        if kind == "logistic":
            scaler = StandardScaler()
            xtr = scaler.fit_transform(enc.onehot(num[mask], cat[mask]))
            m = LogisticRegression(max_iter=2000, class_weight="balanced").fit(xtr, y[mask])
            return lambda rows: m.predict_proba(scaler.transform(enc.onehot(num[rows], cat[rows])))[:, 1]
        catmask = [False] * num.shape[1] + [True] * cat.shape[1]
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06,
                                           categorical_features=catmask if any(catmask) else None,
                                           random_state=RNG_SEED).fit(enc.dense(num[mask], cat[mask]), y[mask])
        return lambda rows: m.predict_proba(enc.dense(num[rows], cat[rows]))[:, 1]

    k_val = max(1, int(k * len(val_rows) / max(1, len(test_rows))))
    table, scores, notes, val_scores, models = [], {}, {}, {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for kind in ("logistic", "boosting"):
            try:
                f = fit(kind, num_val, train_val)
                val_scores[kind] = evaluate(val_rows, f(val_rows), k_val)
                models[kind] = fit(kind, num_test, train_test)  # znova naučený na štítkoch známych pred testom
                scores[kind] = models[kind](test_rows)
            except Exception as exc:  # noqa: BLE001 – jeden model nezastaví ostatné
                notes[kind] = f"{type(exc).__name__}: {str(exc)[:150]}"
    if not scores:
        raise DatasetError("žiadny model sa nepodarilo naučiť: " + "; ".join(notes.values()))
    chosen = max(val_scores, key=lambda m: val_scores[m].get(primary) or -1)
    for kind, sc in scores.items():
        table.append({"postup": kind, "typ": "model", **evaluate(test_rows, sc, k)})

    def baselines(rows: np.ndarray, num: np.ndarray, cutoff) -> dict[str, np.ndarray | None]:
        out: dict[str, np.ndarray | None] = {"random": None}
        if value:
            out["value_desc"] = np.asarray(np.ma.filled(np.ma.asarray(data["_value"], dtype=float), np.nan))[rows]
        flags = [i for i, (fname, a) in enumerate(base_num) if set(np.unique(a[~np.isnan(a)])) <= {0.0, 1.0}]
        if flags:
            out["flag_count"] = np.nansum(num[np.ix_(rows, flags)], axis=1)
        if ent:  # zmrazená miera entity pred hranicou (rovnaká ako príznak)
            out["entity_rate"] = num[rows, len(base_num)]
        return out

    base_val = baselines(val_rows, num_val, t_val)
    base_scores = baselines(test_rows, num_test, t_test)
    for name, sc in base_scores.items():
        table.append({"postup": name, "typ": "naivný", **evaluate(test_rows, sc, k)})
    for row in table:
        for key in list(row):
            row[key] = _r(row[key]) if key not in ("postup", "typ") else row[key]
    # silný naivný odhad: vybraný na validácii podľa hlavnej metriky a zmrazený pred testom (nie dodatočne na teste)
    base_val_scores = {b: evaluate(val_rows, sc, k_val, only=primary).get(primary) for b, sc in base_val.items()}
    direction = METRICS["ranking"].get(primary, "higher")
    cand = {b: v for b, v in base_val_scores.items() if v is not None and b != "random"} or \
        {b: v for b, v in base_val_scores.items() if v is not None}
    strong = (max if direction == "higher" else min)(cand, key=cand.get) if cand else None
    # intervaly rozdielu (vybraný model − naivný odhad): bootstrap zhlukov testu (entity, inak mesiace)
    groups, how = clusters_of(ents_all[test_rows] if ent else None, t[test_rows])
    yt = y[test_rows]

    def diff_ci(base: str) -> list[float] | None:
        if base not in base_scores or chosen not in scores:
            return None
        bs = base_scores[base]

        def stat(ix: np.ndarray) -> float | None:
            rows = test_rows[ix]
            mk = evaluate(rows, scores[chosen][ix], k, only=primary).get(primary)
            bk = evaluate(rows, None if bs is None else bs[ix], k, only=primary).get(primary)
            return None if mk is None or bk is None else mk - bk

        return cluster_bootstrap(groups, stat, cfg.experiments.bootstrap, rng)

    ci = diff_ci(plan["primary_baseline"])
    strong_row = next((r for r in table if r["postup"] == strong), None)
    chosen_row = next(r for r in table if r["postup"] == chosen)
    strong_out = None
    if strong_row is not None:
        imp = improvement(chosen_row.get(primary), strong_row.get(primary), direction)
        sci = ci if strong == plan["primary_baseline"] else diff_ci(strong)
        strong_out = {"naivný": strong, "metrika": primary, "validácia": _r(base_val_scores.get(strong)),
                      "hodnota": strong_row.get(primary), "model_hodnota": chosen_row.get(primary),
                      "zlepšenie_pct": None if imp is None else round(imp, 2), "ci95_rozdielu": sci,
                      "vybraný_na": "validácia (zmrazené pred testom)",
                      "prekonal": bool(imp is not None and imp > 0 and (sci is None or (
                          sci[0] > 0 if direction == "higher" else sci[1] < 0)))}
    # dôležitosť príznakov: pokles priemernej presnosti po premiešaní príznaku (vzorka testu)
    importance = {}
    sample = rng.choice(test_rows, size=min(20000, len(test_rows)), replace=False)
    try:
        from sklearn.metrics import average_precision_score

        g = models[chosen]  # model číta num_test a cat – premiešaný stĺpec sa po meraní vráti
        base_ap = average_precision_score(y[sample], g(sample))
        for j, fname in enumerate(names):
            mat, col = (num_test, j) if j < num_test.shape[1] else (cat, j - num_test.shape[1])
            saved = mat[sample, col].copy()
            mat[sample, col] = rng.permutation(saved)
            try:
                importance[fname] = round(float(base_ap - average_precision_score(y[sample], g(sample))), 4)
            finally:
                mat[sample, col] = saved
    except Exception as exc:  # noqa: BLE001
        notes["dôležitosť"] = str(exc)[:150]
    # položky na kontrolu: prvých k testovacích riadkov podľa vybraného modelu
    top = _top_k(scores[chosen], min(k, cfg.experiments.top_items), rng)
    rids = [int(data["_rid"][test_from + i]) for i in top]
    items = _items(ws, rids, label, feats, value, ent, order=[float(scores[chosen][i]) for i in top])
    dropped_val = int(((idx < val_from) & ~train_val).sum())
    dropped_test = int(((idx < test_from) & ~train_test).sum())
    return {"tabuľka": table, "metriky": list(METRICS["ranking"]), "vybraný_model": chosen,
            "výber": f"vybraný na validačnom období podľa {primary}: " + ", ".join(
                f"{m} {_r(v.get(primary))}" for m, v in val_scores.items()),
            "hlavné": _primary(plan, table, chosen, ci),
            "silné_porovnanie": strong_out,
            "detaily": {"riadky": n, "učenie": f"{t[0]} – {t[val_from - 1]}", "validácia": f"{t_val} – {t[test_from - 1]}",
                        "test": f"{t_test} – {t[-1]}", "test_riadky": int(len(yt)), "test_pozitívne": int(yt.sum()),
                        "podiel_pozitívnych_test": _r(yt.mean()), "k": k, "dôležitosť_príznakov": importance,
                        "jednotka_rozhodnutia": "subjekt" if unit_entity else "riadok",
                        "dozretie_štítka_dní": int(horizon),
                        "nedozreté_pred_validáciou": dropped_val, "nedozreté_pred_testom": dropped_test,
                        "naivné_na_validácii": {b: _r(v) for b, v in base_val_scores.items()},
                        "interval": f"bootstrap {how}",
                        "koncentrácia_štítku": label_concentration(ws, label, ent), "poznámky": notes},
            "položky": items, "položky_popis": f"prvých {len(items)} položiek testovacieho obdobia podľa modelu {chosen}"}


def _items(ws: Workspace, rids: list[int], label: str, feats: list[str], value: str, ent: str,
           order: list[float] | None = None) -> list[dict[str, Any]]:
    if not rids:
        return []
    text = _text_column(ws)
    cols = ["_rid", "_t"] + ([ent] if ent else []) + ([text] if text and text not in feats else []) + feats + \
           ([label] if label else [])
    sel = ", ".join(q(c) for c in dict.fromkeys(cols))
    rows = ws.query(f"SELECT {sel}" + (f", TRY_CAST(({value}) AS DOUBLE) AS _hodnota" if value else "")
                    + f" FROM {ws.table} WHERE _rid IN ({', '.join(str(r) for r in rids)})")
    names = list(dict.fromkeys(cols)) + (["_hodnota"] if value else [])
    by = {r[0]: {n: (str(v)[:120] if isinstance(v, str) else _r(v) if isinstance(v, (int, float)) else str(v))
                 for n, v in zip(names, r)} for r in rows}
    out = []
    for i, rid in enumerate(rids):
        if rid in by:
            item = {"poradie": i + 1, **{k: v for k, v in by[rid].items() if k != "_rid"}}
            if order is not None:
                item["skóre"] = _r(order[i])
            out.append(item)
    return out


# ======================================================================= forecast / anomaly
def _ets(h: np.ndarray, season: int) -> float:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    n = len(h)
    seasonal = "add" if season > 1 and n >= 2 * season else None
    trend = "add" if n >= 6 else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = ExponentialSmoothing(h, trend=trend, damped_trend=trend is not None, seasonal=seasonal,
                                 seasonal_periods=season if seasonal else None, initialization_method="estimated").fit()
        f = float(m.forecast(1)[0])
    return f if np.isfinite(f) else float(h[-1])


def _naive(h: np.ndarray, kind: str, season: int) -> float:
    if kind == "last_value":
        return float(h[-1])
    if kind == "mean":
        return float(np.mean(h))
    if kind == "seasonal_naive":
        return float(h[-season]) if season > 1 and len(h) >= season else float(h[-1])
    if kind == "drift":
        return float(h[-1] + (h[-1] - h[0]) / (len(h) - 1)) if len(h) > 1 else float(h[-1])
    raise ValueError(kind)


def _pooled_forecasts(panel: np.ndarray, origin: int, season: int, nonneg: bool) -> np.ndarray:
    """Spoločný model cez všetky entity: oneskorené hodnoty, kĺzavé priemery, obdobie v roku, priemer entity."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    def feats(i: int, tt: int) -> list[float]:
        h = panel[i, :tt]
        lag = lambda j: h[-j] if len(h) >= j else np.nan  # noqa: E731
        return [lag(1), lag(2), lag(3), lag(season) if season > 1 else np.nan, np.nanmean(h[-3:]),
                np.nanmean(h[-12:]), np.nanmean(h), tt % max(season, 1)]

    tf = (lambda v: np.log1p(np.maximum(v, 0))) if nonneg else (lambda v: v)
    itf = (lambda v: np.expm1(v)) if nonneg else (lambda v: v)
    X, Y = [], []
    for i in range(panel.shape[0]):
        for tt in range(3, origin):
            if np.isfinite(panel[i, tt]):
                X.append([tf(x) if np.isfinite(x) and j < 7 else x for j, x in enumerate(feats(i, tt))])
                Y.append(tf(panel[i, tt]))
    if len(Y) < 30:
        return np.full(panel.shape[0], np.nan)
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.06, random_state=RNG_SEED).fit(np.array(X), np.array(Y))
    Xo = [[tf(x) if np.isfinite(x) and j < 7 else x for j, x in enumerate(feats(i, origin))] for i in range(panel.shape[0])]
    return itf(m.predict(np.array(Xo)))


def _series_panel(cfg: Config, ws: Workspace, plan: dict[str, Any]) -> tuple[list[str], list[date], np.ndarray]:
    from .gate2 import _period_end, _period_start

    period = plan.get("period") or "month"
    agg = {"sum": "SUM", "count": "COUNT", "avg": "AVG"}[plan.get("aggregation") or "sum"]
    value = check_expr(plan["value"])
    ent = plan.get("entity_column") or ""
    e_sql = f"CAST({q(ent)} AS VARCHAR)" if ent else "'všetko'"
    val = "*" if agg == "COUNT" else f"TRY_CAST(({value}) AS DOUBLE)"
    rows = ws.query(f"SELECT CAST(date_trunc('{period}', _t) AS DATE) AS p, {e_sql} AS e, {agg}({val}) AS y "
                    f"FROM {ws.table} GROUP BY 1, 2")
    lo, hi = date.fromisoformat(ws.limits["od"]), min(date.fromisoformat(ws.limits["do"]), date.today())
    start = lo if _period_start(lo, period) == lo else _period_end(lo, period) + timedelta(days=1)
    periods = []
    cur = start
    while _period_end(cur, period) <= hi:
        periods.append(cur)
        cur = _period_end(cur, period) + timedelta(days=1)
    if len(periods) < cfg.gate2.min_history + 2:
        raise DatasetError(f"iba {len(periods)} úplných období po {period} v rozsahu dát")
    totals: dict[str, float] = {}
    for p, e, yv in rows:
        if p in periods and yv is not None:
            totals[e] = totals.get(e, 0.0) + abs(float(yv))
    ents = [e for e, _ in sorted(totals.items(), key=lambda kv: -kv[1])][: cfg.experiments.max_entities]
    pos = {p: j for j, p in enumerate(periods)}
    eidx = {e: i for i, e in enumerate(ents)}
    fill = 0.0 if agg in ("SUM", "COUNT") else np.nan
    panel = np.full((len(ents), len(periods)), fill)
    for p, e, yv in rows:
        if e in eidx and p in pos and yv is not None:
            panel[eidx[e], pos[p]] = float(yv)
    return ents, periods, panel


def _forecast_metrics(actual: np.ndarray, pred: np.ndarray, scale: np.ndarray) -> dict[str, float | None]:
    ok = np.isfinite(actual) & np.isfinite(pred)
    if not ok.any():
        return {m: None for m in METRICS["forecast"]}
    a, f, s = actual[ok], pred[ok], scale[ok]
    err = np.abs(f - a)
    nz = a != 0
    den = np.abs(f) + np.abs(a)
    smape = np.where(den > 0, 200 * err / np.where(den > 0, den, 1), 0)
    sc = s > 0
    return {"MASE": float(np.mean(err[sc] / s[sc])) if sc.any() else None, "MAE": float(err.mean()),
            "RMSE": float(np.sqrt(np.mean(err ** 2))), "sMAPE": float(smape.mean()),
            "MAPE": float(np.mean(err[nz] / np.abs(a[nz])) * 100) if nz.any() else None}


def _forecast(cfg: Config, ws: Workspace, plan: dict[str, Any], anomaly: bool = False) -> dict[str, Any]:
    from .gate2 import season_length

    ents, periods, panel = _series_panel(cfg, ws, plan)
    season = season_length(plan.get("period"), [])
    nonneg = plan.get("aggregation") in ("sum", "count") and np.nanmin(panel) >= 0
    nper = len(periods)
    folds = min(cfg.experiments.forecast_folds, nper - cfg.gate2.min_history)
    origins = list(range(nper - folds, nper))
    n_val = max(1, folds // 2) if folds >= 2 else 0
    val_o, test_o = origins[:n_val], origins[n_val:] or origins
    names = ["last_value", "mean", "seasonal_naive", "drift", "ets", "pooled_boosting"]
    preds = {m: np.full((len(ents), nper), np.nan) for m in names}
    for o in origins:
        pooled = _pooled_forecasts(panel, o, season, nonneg)
        for i in range(len(ents)):
            h = panel[i, :o]
            h = h[np.isfinite(h)]
            if len(h) < cfg.gate2.min_history:
                continue
            for m in names[:4]:
                preds[m][i, o] = _naive(h, m, season)
            try:
                preds["ets"][i, o] = _ets(h, season)
            except Exception:  # noqa: BLE001 – ETS pri krátkom alebo konštantnom rade
                preds["ets"][i, o] = h[-1]
            preds["pooled_boosting"][i, o] = pooled[i]
    first_test = test_o[0]
    scale = np.array([np.nanmean(np.abs(np.diff(panel[i, :first_test]))) if first_test > 1 else np.nan
                      for i in range(len(ents))])
    scale_mat = np.repeat(scale[:, None], nper, axis=1)

    def metrics_for(m: str, cols: list[int]) -> dict[str, float | None]:
        return _forecast_metrics(panel[:, cols].ravel(), preds[m][:, cols].ravel(), scale_mat[:, cols].ravel())

    primary = plan["primary_metric"] if plan["primary_metric"] in METRICS["forecast"] else "MASE"
    models = ["ets", "pooled_boosting"]
    val_scores = {m: metrics_for(m, val_o) for m in models} if val_o and test_o != origins else {}
    chosen = (min(val_scores, key=lambda m: val_scores[m].get(primary) if val_scores[m].get(primary) is not None
                  else np.inf) if val_scores else "ets")
    table = [{"postup": m, "typ": "model" if m in models else "naivný", **{k: _r(v) for k, v in metrics_for(m, test_o).items()}}
             for m in models + names[:4]]
    rng = np.random.default_rng(RNG_SEED)
    ci = None
    base = plan["primary_baseline"]
    if anomaly:
        result = _anomaly(cfg, ws, plan, ents, periods, panel, preds, chosen, val_o or origins[:1], test_o, table, scale)
    else:
        result = {}
    if base in preds and primary in ("MASE", "MAE", "sMAPE", "RMSE", "MAPE"):
        per_e = []
        for i in range(len(ents)):
            mm = _forecast_metrics(panel[i, test_o], preds[chosen][i, test_o], scale_mat[i, test_o]).get(primary)
            bb = _forecast_metrics(panel[i, test_o], preds[base][i, test_o], scale_mat[i, test_o]).get(primary)
            if mm is not None and bb is not None:
                per_e.append(mm - bb)
        if per_e:
            arr = np.array(per_e)
            boots = [rng.choice(arr, size=len(arr), replace=True).mean() for _ in range(cfg.experiments.bootstrap)]
            ci = [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]
    strong_out = None
    if val_scores and not anomaly:  # silný naivný odhad vybraný na validačných obdobiach, zmrazený pred testom
        base_val = {b: metrics_for(b, val_o).get(primary) for b in names[:4]}
        cand = {b: v for b, v in base_val.items() if v is not None}
        if cand:
            strong = min(cand, key=cand.get)
            sv = next(r.get(primary) for r in table if r["postup"] == strong)
            mv = next(r.get(primary) for r in table if r["postup"] == chosen)
            imp = improvement(mv, sv, "lower")
            diffs = []
            for i in range(len(ents)):
                mm = _forecast_metrics(panel[i, test_o], preds[chosen][i, test_o], scale_mat[i, test_o]).get(primary)
                bb = _forecast_metrics(panel[i, test_o], preds[strong][i, test_o], scale_mat[i, test_o]).get(primary)
                if mm is not None and bb is not None:
                    diffs.append(mm - bb)
            sci = None
            if diffs:  # bootstrap po entitách (zhluk = entita)
                arr = np.array(diffs)
                boots = [rng.choice(arr, size=len(arr), replace=True).mean() for _ in range(cfg.experiments.bootstrap)]
                sci = [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]
            strong_out = {"naivný": strong, "metrika": primary, "validácia": _r(cand[strong]), "hodnota": sv,
                          "model_hodnota": mv, "zlepšenie_pct": None if imp is None else round(imp, 2),
                          "ci95_rozdielu": sci, "vybraný_na": "validačné obdobia (zmrazené pred testom)",
                          "prekonal": bool(imp is not None and imp > 0 and (sci is None or sci[1] < 0))}
    plan_eff = plan if plan["primary_metric"] in [r for r in table[0]] else {**plan, "primary_metric": primary}
    out = {"tabuľka": table, "metriky": list(METRICS["forecast"]), "vybraný_model": chosen,
           "výber": (f"vybraný na validačných obdobiach ({', '.join(str(periods[o]) for o in val_o)}) podľa {primary}: "
                     + ", ".join(f"{m} {_r(v.get(primary))}" for m, v in val_scores.items())) if val_scores else
                    "príliš málo období na výber – použité ETS",
           "hlavné": _primary(plan_eff, table, chosen, ci), "silné_porovnanie": strong_out,
           "detaily": {"entity": len(ents), "obdobia": [str(p) for p in periods], "testovacie_obdobia":
                       [str(periods[o]) for o in test_o], "sezóna": season,
                       "rad_súčet": [_r(float(np.nansum(panel[:, j])), 2) for j in range(nper)],
                       "predpoveď_súčet": {m: [_r(float(np.nansum(preds[m][:, j])), 2) if j in origins else None
                                               for j in range(nper)] for m in (chosen, base) if m in preds}}}
    if anomaly:
        out |= result
    return out


def _anomaly(cfg, ws, plan, ents, periods, panel, preds, chosen, val_o, test_o, table, scale) -> dict[str, Any]:
    """Konformné intervaly z relatívnych chýb na validačných obdobiach; označené obdobia mimo 95 % intervalu."""
    label = plan.get("label") or ""
    ent = plan.get("entity_column") or ""
    period = plan.get("period") or "month"

    def intervals(m: str) -> tuple[float, float, float, float]:
        res = []
        for o in val_o:
            f, a = preds[m][:, o], panel[:, o]
            sc = np.maximum(np.abs(f), np.where(np.isfinite(scale), scale, 0) + 1e-9)
            ok = np.isfinite(f) & np.isfinite(a)
            res += list(((a - f) / sc)[ok])
        if len(res) < 10:
            res = [0.0] * 10
        return tuple(float(np.percentile(res, p)) for p in (2.5, 97.5, 10, 90))

    def flags_for(m: str) -> tuple[list[tuple[int, int, float]], float, float]:
        lo95, hi95, lo80, hi80 = intervals(m)
        flagged, inside95, inside80, total = [], 0, 0, 0
        for o in test_o:
            f, a = preds[m][:, o], panel[:, o]
            sc = np.maximum(np.abs(f), np.where(np.isfinite(scale), scale, 0) + 1e-9)
            for i in range(len(ents)):
                if not (np.isfinite(f[i]) and np.isfinite(a[i])):
                    continue
                z = (a[i] - f[i]) / sc[i]
                total += 1
                inside95 += lo95 <= z <= hi95
                inside80 += lo80 <= z <= hi80
                if not lo95 <= z <= hi95:
                    flagged.append((i, o, float(z)))
        return flagged, inside95 / total if total else np.nan, inside80 / total if total else np.nan

    labeled = set()
    if label:
        e_sql = f"CAST({q(ent)} AS VARCHAR)" if ent else "'všetko'"
        for p, e in ws.query(f"SELECT DISTINCT CAST(date_trunc('{period}', _t) AS DATE), {e_sql} FROM {ws.table} "
                             f"WHERE TRY_CAST({q(label)} AS DOUBLE) > 0"):
            labeled.add((str(e), p))
    extra = {}
    for row in table:
        m = row["postup"]
        flagged, c95, c80 = flags_for(m)
        row["coverage95_error"] = _r(abs(c95 - 0.95)) if np.isfinite(c95) else None
        row["pokrytie95"] = _r(c95)
        row["pokrytie80"] = _r(c80)
        row["označené"] = len(flagged)
        if label:
            hits = sum((ents[i], periods[o]) in labeled for i, o, _ in flagged)
            row["flag_precision"] = _r(hits / len(flagged)) if flagged else None
        extra[m] = flagged
    flagged = sorted(extra[chosen], key=lambda x: -abs(x[2]))[: cfg.experiments.top_items]
    items = []
    text = _text_column(ws)
    for i, o, z in flagged:
        p = periods[o]
        cond = (f"CAST({q(ent)} AS VARCHAR) = ? AND " if ent else "") + f"CAST(date_trunc('{period}', _t) AS DATE) = ?"
        params = ([ents[i]] if ent else []) + [p]
        top = ws.query(f"SELECT {q(text) if text else 'NULL'}, TRY_CAST(({plan['value']}) AS DOUBLE) AS v FROM {ws.table} "
                       f"WHERE {cond} ORDER BY v DESC NULLS LAST LIMIT 3", params)
        total = panel[i, o]
        items.append({"entita": ents[i], "obdobie": str(p), "skutočnosť": _r(total, 2), "predpoveď": _r(preds[chosen][i, o], 2),
                      "odchýlka_z": _r(z, 2), "najväčšie_riadky": "; ".join(
                          f"{(str(tx)[:80] if tx else '?')}: {_r(v, 0)}" for tx, v in top),
                      "podiel_najväčšieho": _r(top[0][1] / total, 3) if top and top[0][1] and total else None})
    return {"metriky": list(METRICS["anomaly"]) + ["pokrytie95", "pokrytie80", "označené"] + list(METRICS["forecast"]),
            "položky": items, "položky_popis": f"obdobia mimo 95 % intervalu modelu {chosen} (testovacie obdobia), "
                                               "s najväčšími riadkami"}


# ======================================================================= threshold
def _threshold(cfg: Config, ws: Workspace, plan: dict[str, Any]) -> dict[str, Any]:
    value = check_expr(plan["value"])
    ent = plan.get("entity_column") or ""
    data = ws.con.execute(f"SELECT TRY_CAST(({value}) AS DOUBLE) AS v" + (f", CAST({q(ent)} AS VARCHAR) AS e" if ent else "")
                          + f" FROM {ws.table} WHERE TRY_CAST(({value}) AS DOUBLE) > 0").fetchnumpy()
    v = np.asarray(data["v"], dtype=float)
    rng = np.random.default_rng(RNG_SEED)
    table, items = [], []
    w_share = cfg.experiments.threshold_window

    def excess(vals: np.ndarray, t: float) -> tuple[float, float, float]:
        w = w_share * t
        b = w / 5
        edges = np.arange(t - 4 * w, t + 4 * w + b / 2, b)
        counts, _ = np.histogram(vals, bins=edges)
        centers = (edges[:-1] + edges[1:]) / 2
        below = (centers >= t - w) & (centers < t)
        fit_mask = ~((centers >= t - w) & (centers < t + w))
        if counts[fit_mask].sum() < 20:
            return np.nan, np.nan, np.nan
        coef = np.polyfit(centers[fit_mask] - t, counts[fit_mask], 2)
        expected = np.maximum(np.polyval(coef, centers[below] - t), 0.5).sum()
        observed = counts[below].sum()
        return float(observed), float(expected), float((observed - expected) / expected)

    for t in [float(x) for x in plan.get("thresholds") or []]:
        obs, exp_, ratio = excess(v, t)
        boots = []
        for _ in range(min(cfg.experiments.bootstrap, 300)):
            r = excess(rng.choice(v, size=len(v), replace=True), t)[2]
            if np.isfinite(r):
                boots.append(r)
        ci = [round(float(np.percentile(boots, 2.5)) * 100, 2), round(float(np.percentile(boots, 97.5)) * 100, 2)] if boots else None
        table.append({"postup": f"prah {t:g}", "typ": "model", "pozorované_pod_prahom": _r(obs, 0),
                      "očakávané_pod_prahom": _r(exp_, 1), "excess_ratio": _r(ratio * 100 if np.isfinite(ratio) else None, 2),
                      "ci95": ci})
        if ent:
            e = np.asarray([str(x) for x in np.ma.filled(data["e"], "")], dtype=object)
            w = w_share * t
            near = (v >= t - 4 * w) & (v < t + 4 * w)
            pop = ((v >= t - w) & (v < t))[near].mean() if near.any() else np.nan
            for name in np.unique(e[near]):
                sel = near & (e == name)
                n = int(sel.sum())
                if n >= 10 and np.isfinite(pop) and pop > 0:
                    share = float(((v >= t - w) & (v < t))[sel].mean())
                    z = (share - pop) / np.sqrt(pop * (1 - pop) / n) if 0 < pop < 1 else 0
                    items.append({"prah": t, "entita": name, "zmlúv_v_okolí": n, "podiel_tesne_pod": _r(share, 3),
                                  "podiel_všetci": _r(pop, 3), "z": _r(z, 2)})
    table.append({"postup": "smooth_density", "typ": "naivný", "excess_ratio": 0.0})
    items = sorted(items, key=lambda x: -(x["z"] or 0))[: cfg.experiments.top_items]
    best = max((r for r in table if r["typ"] == "model" and r.get("excess_ratio") is not None),
               key=lambda r: r["excess_ratio"], default=None)
    prim = {"metrika": "excess_ratio", "smer": "higher", "model": best["postup"] if best else None,
            "model_hodnota": best["excess_ratio"] if best else None, "naivný": "smooth_density", "naivný_hodnota": 0.0,
            "zlepšenie_pct": best["excess_ratio"] if best else None, "ci95_rozdielu": best.get("ci95") if best else None,
            "prah_pct": float(plan.get("success_threshold") or 0)}
    prim["splnené"] = bool(best and best["excess_ratio"] >= prim["prah_pct"] and best.get("ci95") and best["ci95"][0] > 0)
    return {"tabuľka": table, "metriky": ["excess_ratio", "pozorované_pod_prahom", "očakávané_pod_prahom", "ci95"],
            "vybraný_model": prim["model"], "výber": "prah s najväčším nadbytkom", "hlavné": prim,
            "detaily": {"hodnôt": int(len(v)), "okno_okolo_prahu_pct": w_share * 100},
            "položky": items, "položky_popis": "entity s najväčším podielom hodnôt tesne pod prahom (z-skóre)"}


# ======================================================================= comparison
def _comparison(cfg: Config, ws: Workspace, plan: dict[str, Any]) -> dict[str, Any]:
    value, cat, label = check_expr(plan["value"]), plan["category"], plan.get("label") or ""
    rows = ws.con.execute(
        f"SELECT _rid, CAST({q(cat)} AS VARCHAR) AS c, TRY_CAST(({value}) AS DOUBLE) AS v"
        + (f", CASE WHEN TRY_CAST({q(label)} AS DOUBLE) > 0 THEN 1 ELSE 0 END AS y" if label else "")
        + f" FROM {ws.table} WHERE TRY_CAST(({value}) AS DOUBLE) IS NOT NULL").fetchnumpy()
    c = np.asarray([str(x) for x in np.ma.filled(rows["c"], "∅")], dtype=object)
    v = np.asarray(rows["v"], dtype=float)
    flag = np.zeros(len(v), dtype=bool)
    ratio = np.full(len(v), np.nan)
    for name in np.unique(c):
        sel = c == name
        if sel.sum() >= 20:
            p90 = np.percentile(v[sel], 90)
            flag[sel] = v[sel] >= p90
            med = np.median(v[sel])
            ratio[sel] = v[sel] / med if med else np.nan
    row = {"postup": "horný decil kategórie", "typ": "model", "flag_rate": _r(flag.mean())}
    base = {"postup": "random", "typ": "naivný", "flag_rate": _r(flag.mean())}
    ci = None
    if label:
        y = np.asarray(rows["y"], dtype=int)
        prev = y.mean()
        prec = y[flag].mean() if flag.any() else np.nan
        row |= {"flag_precision": _r(prec), "lift": _r(prec / prev if prev else None)}
        base |= {"flag_precision": _r(prev), "lift": 1.0}
        rng = np.random.default_rng(RNG_SEED)
        idx_all = np.arange(len(y))
        diffs = []
        for _ in range(min(cfg.experiments.bootstrap, 300)):
            idx = rng.choice(idx_all, size=len(idx_all), replace=True)
            f, yy = flag[idx], y[idx]
            if f.any():
                diffs.append(yy[f].mean() - yy.mean())
        if diffs:
            ci = [round(float(np.percentile(diffs, 2.5)), 4), round(float(np.percentile(diffs, 97.5)), 4)]
    table = [row, base]
    order = np.argsort(-np.nan_to_num(ratio, nan=-1))[: cfg.experiments.top_items]
    items = _items(ws, [int(rows["_rid"][i]) for i in order], label, [cat], value, plan.get("entity_column") or "",
                   order=[float(ratio[i]) for i in order])
    prim = _primary(plan, table, row["postup"], ci) if label else {
        "metrika": None, "model": row["postup"], "zlepšenie_pct": None, "splnené": None, "popisné": True}
    return {"tabuľka": table, "metriky": ["flag_rate", "flag_precision", "lift"], "vybraný_model": row["postup"],
            "výber": "pevné pravidlo: hodnota v hornom decile svojej kategórie", "hlavné": prim,
            "detaily": {"riadky": int(len(v)), "kategórie_s_20": int(sum((c == n).sum() >= 20 for n in np.unique(c)))},
            "položky": items, "položky_popis": "riadky s najväčším násobkom mediánu svojej kategórie"}


# ======================================================================= spustenie
def assess(cfg: Config, res: dict[str, Any], plan: dict[str, Any], method_errors: list[str],
           method_warnings: list[str]) -> dict[str, Any]:
    """Tri osi výsledku z dát (audit 14. 10. 2026, P1 5): (a) splnenie predregistrovanej hypotézy, (b) prínos oproti
    silnému jednoduchému postupu vybranému na validácii a zmrazenému pred testom, (c) metodická platnosť testu.
    Štvrtá os – výsledok u používateľa – sa dopĺňa ručne (`verdict`). Najlepší naivný odhad na teste je iba diagnostika."""
    prim = res.get("hlavné") or {}
    det = res.get("detaily") or {}
    strong = res.get("silné_porovnanie")
    problems = list(method_errors)
    weak: list[str] = []
    conc = det.get("koncentrácia_štítku") or {}
    if conc.get("sústredený") and det.get("jednotka_rozhodnutia", "riadok") == "riadok":
        problems.append(f"štítok je sústredený na {conc.get('entity_so_štítkom')} z {conc.get('entity')} entít – test "
                        "meria vlastnosť entity, nie riadku (jednotka rozhodnutia)")
    if plan.get("task") == "ranking" and det.get("test_pozitívne") is not None \
            and det["test_pozitívne"] < cfg.experiments.min_positives:
        weak.append(f"v teste iba {det['test_pozitívne']} pozitívnych prípadov")
    if plan.get("task") in ("forecast", "anomaly") and len(det.get("testovacie_obdobia") or []) < 4:
        weak.append(f"iba {len(det.get('testovacie_obdobia') or [])} testovacie obdobia")
    ci = prim.get("ci95_rozdielu")
    if ci and ci[0] <= 0 <= ci[1]:
        weak.append("95 % interval rozdielu proti hlavnému naivnému odhadu obsahuje nulu")
    test_best = strongest_baseline(res)
    return {
        "predregistrácia": {"splnené": prim.get("splnené"), "naivný": prim.get("naivný"),
                            "zlepšenie_pct": prim.get("zlepšenie_pct"), "prah_pct": prim.get("prah_pct")},
        "silné_porovnanie": strong or {"prekonal": None, "text": "pre túto úlohu sa nevyhodnocuje"},
        "metodika": {"platné": not problems, "problémy": problems, "nepresvedčivé": weak, "upozornenia": method_warnings},
        "diagnostika_test": test_best,  # najlepší naivný odhad na teste – nie nová predregistrácia
    }


def verdict(res: dict[str, Any], flags: dict[str, Any] | None = None) -> dict[str, Any]:
    """Súhrn štyroch osí; (d) výsledok u používateľa je ručný záznam (pilot, nameraná zmena práce)."""
    osi = res.get("osi") or {}
    user = (flags or {}).get("výsledok_používateľa") or {"stav": "chýba",
                                                          "text": "výsledok u používateľa (pilot) ešte nie je zapísaný"}
    a = (osi.get("predregistrácia") or {}).get("splnené")
    b = (osi.get("silné_porovnanie") or {}).get("prekonal")
    m = osi.get("metodika") or {}
    reasons = []
    if not osi:
        text, go = "staršie vyhodnotenie bez osí – spusti spätný test znova", False
    elif not m.get("platné", True):
        text, go = "neplatné – metodika", False
        reasons += m.get("problémy") or []
    elif a is False:
        text, go = "nesplnené – predregistrované pravidlo", False
    elif b is False:
        text, go = "neprekonal silné jednoduché porovnanie", False
        s = osi["silné_porovnanie"]
        reasons.append(f"{s.get('naivný')}: {s.get('hodnota')} vs. model {s.get('model_hodnota')} "
                       f"({s.get('zlepšenie_pct')} %)")
    elif m.get("nepresvedčivé"):
        text, go = "nepresvedčivé", False
        reasons += m["nepresvedčivé"]
    elif user.get("stav") == "potvrdené":
        text, go = "potvrdené u používateľa", True
    elif user.get("stav") == "nepotvrdené":
        text, go = "u používateľa nepotvrdené", False
    else:
        text, go = "prínos v dátach – over u používateľa (pilot)", True
    return {"verdikt": text, "pokračovať": go, "dôvody": reasons, "používateľ": user}


def versions(db: DB, exp, plan: dict[str, Any]) -> dict[str, Any]:
    """Verzie, s ktorými sa experiment počítal: kód, parser, schéma DB, zdrojové dáta (SHA-256) a špecifikácia."""
    from .datasets import PARSER_VERSION, base_table

    tables = {base_table(plan.get("table", ""))} | {base_table(st.get("table", "")) for st in plan.get("steps") or []
                                                    if st.get("table")}
    data = {d["table_name"]: (d["sha256"] or "")[:16] for d in db.datasets_for(exp["hypothesis_id"]) if d["table_name"] in tables}
    return {"kód": EXPERIMENT_CODE_VERSION, "parser": PARSER_VERSION, "schéma": db.schema_version(), "dáta": data,
            "špecifikácia": (exp["prereg_hash"] or "")[:16]}


def run_experiment(cfg: Config, db: DB, exp, categorize: Callable | None = None,
                   check_categories: Callable | None = None) -> dict[str, Any]:
    from .exp_plan import methodology
    from .gate2 import data_limits

    plan = json.loads(exp["spec_json"])
    hid = exp["hypothesis_id"]
    ws = Workspace(cfg, hid, plan["table"], plan["time_column"], data_limits(db, hid, plan["table"]),
                   categorize=categorize, check_categories=check_categories)
    try:
        for raw in plan.get("steps") or []:
            ws.apply(Step.model_validate(raw))
        meth_errors, meth_warnings = methodology(db, hid, plan, ws)
        runner = {"ranking": _ranking, "forecast": _forecast, "threshold": _threshold, "comparison": _comparison,
                  "anomaly": lambda c, w, p: _forecast(c, w, p, anomaly=True)}[plan["task"]]
        out = runner(cfg, ws, plan)
        out["protokol"] = ws.protocol
        out["task"] = plan["task"]
        out["rozsah_dát"] = ws.limits
        out["osi"] = assess(cfg, out, plan, meth_errors, meth_warnings)
        out["verzie"] = versions(db, exp, plan)
        return out
    finally:
        ws.close()
