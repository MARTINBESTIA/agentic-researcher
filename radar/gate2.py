"""Brána 2 – experimenty a demo (pre hypotézy, ktoré prešli Bránou 1: pripravenosť dát alebo vhodnosť tvojich dát
≥ 0,5, alebo tvoje rozhodnutie).

Jednotka je dvojica hypotéza × organizácia (alebo región): nájdené dáta môžu patriť jednej alebo viacerým
organizáciám (pokrytie podľa IČO z Brány 1).

1. gate2-prereg (presný model): návrh predregistrácie – tvrdenie, tabuľka a stĺpce, metrika, naivný odhad,
   model a pravidlo úspechu. Človek ho v GUI potvrdí a uzamkne (hash + čas); potom sa už nemení.
2. gate2-backtest (iba Python a DuckDB): spätný test na historických dátach – predpoveď na posledných
   obdobiach (rolling origin) modelom aj naivným odhadom, MAE/MAPE, bootstrap interval rozdielu.
   Modely: lineárny trend, AR(1), exponenciálne vyrovnávanie (ses, holt_damped), trend + sezónnosť a „auto“
   (výber modelu iba z histórie pred každou predpoveďou). Sezóna: mesiac 12, štvrťrok 4, inak z rozostupu dátumov.
   Príznak „model neprekonal naivný odhad“ (nevyraďuje sa automaticky).
3. gate2-demo: HTML správa pre konkrétnu organizáciu (graf, čísla, pôvod dát, predregistrácia); krátky text
   píše model iba z vypočítaných faktov a kód kontroluje, že nepoužil iné čísla.
Spätnú väzbu expertov („prináša vám to niečo nové?“) zapisuješ v GUI; rozhodnutie brány je tvoje.
"""

from __future__ import annotations

import hashlib
import html
import itertools
import json
import logging
import re
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel

from .config import Config
from .datasets import DatasetError, base_table, run_query, schema_text, table_columns
from .db import DB
from .models import as_router
from .prompts import GATE2_DEMO_SYSTEM, GATE2_PREREG_SYSTEM
from .util import Deadline, utcnow

log = logging.getLogger(__name__)


class Prereg(BaseModel):
    statement: str
    table: str
    time_column: str
    value_column: str
    entity_column: str = ""
    filters: dict[str, str] = {}  # rovnosť stĺpec = hodnota (kraj, ukazovateľ v dlhej tabuľke)
    aggregation: Literal["sum", "avg", "count"] = "sum"
    period: Literal["year", "quarter", "month", "none"] = "none"  # zoskupenie dátumového stĺpca
    metric: Literal["MAE", "MAPE"] = "MAE"
    baseline: Literal["last_value", "mean", "seasonal_naive"] = "last_value"
    model: Literal["linear_trend", "ar1", "ses", "holt_damped", "seasonal_trend", "auto"] = "linear_trend"
    success_rule: str = "MAE modelu je nižšia ako MAE naivného odhadu"


class DemoText(BaseModel):
    text: str


# ======================================================================= ciele (hypotéza × organizácia)
def targets(cfg: Config, db: DB, hid: int) -> list[dict[str, Any]]:
    """Organizácie, ktoré sú v dátach hypotézy (podľa riadkov); bez pokrytia jeden cieľ za celý dataset."""
    seen: dict[int, dict[str, Any]] = {}
    for c in db.coverage_for(hid):
        if c["org_id"] not in seen:
            seen[c["org_id"]] = {"org_id": c["org_id"], "org_name": c["org_name"], "ico": c["ico"], "table": c["table_name"],
                                 "entity_column": c["column_name"], "rows": c["rows"]}
    out = sorted(seen.values(), key=lambda t: -t["rows"])[: cfg.gate2.max_targets_per_hypothesis]
    if not out and db.datasets_for(hid, only_ok=True):
        out = [{"org_id": None, "org_name": None, "ico": None, "table": None, "entity_column": "", "rows": None}]
    return out


def validate_spec(cfg: Config, hid: int, spec: dict[str, Any]) -> list[str]:
    """Chyby špecifikácie: tabuľka a stĺpce musia existovať (inak by spätný test nemal zmysel)."""
    cols = table_columns(cfg, hid, spec.get("table", ""))
    if not cols:
        return [f"tabuľka {spec.get('table')!r} neexistuje"]
    errors = [f"stĺpec {spec.get(k)!r} nie je v tabuľke" for k in ("time_column", "value_column") if spec.get(k) not in cols]
    if spec.get("entity_column") and spec["entity_column"] not in cols:
        errors.append(f"stĺpec entity {spec['entity_column']!r} nie je v tabuľke")
    filters = spec.get("filters") or {}
    if not isinstance(filters, dict):
        errors.append("filters musí byť objekt {\"stĺpec\": \"hodnota\"}")
    else:
        errors += [f"stĺpec filtra {k!r} nie je v tabuľke" for k in filters if k not in cols]
    if spec.get("model", "linear_trend") not in MODELS:
        errors.append(f"neznámy model {spec.get('model')!r} (možnosti: {', '.join(MODELS)})")
    return errors


def run_prereg(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    hyps = [db.get_hypothesis(i) for i in ids] if ids else db.gate2_hypotheses()
    for hyp in [h for h in hyps if h is not None]:
        hid = hyp["id"]
        existing = {(e["org_id"], e["region"]) for e in db.experiments_for(hid)}
        tables = [d["table_name"] for d in db.datasets_for(hid, only_ok=True)]
        if not tables:
            stats["no_datasets"] += 1
            continue
        schema = schema_text(cfg, hid, tables)
        for t in targets(cfg, db, hid):
            if deadline.expired():
                return dict(stats)
            if (t["org_id"], None) in existing:
                continue
            who = (f"ORGANIZATION: {t['org_name']} (IČO {t['ico']}), in table {t['table']} column {t['entity_column']}"
                   if t["org_id"] else "ORGANIZATION: none – use the whole dataset (region or country level)")
            res = router.run_task(
                "gate2_prereg",
                [{"role": "system", "content": GATE2_PREREG_SYSTEM.format(lang=cfg.pipeline.output_language)},
                 {"role": "user", "content": f"PROBLEM: {hyp['title']}\n{who}\n\n<untrusted_document>\n{schema}\n"
                                             "</untrusted_document>"}],
                Prereg, object_type="hypothesis", object_id=hid, max_tokens=800,
            )
            if not res.ok:
                stats["llm_failures"] += 1
                continue
            p = res.value
            spec = {"table": p.table, "time_column": p.time_column, "value_column": p.value_column,
                    "entity_column": p.entity_column if t["org_id"] else "", "entity_value": t["ico"] if t["org_id"] else None,
                    "aggregation": p.aggregation, "period": p.period, "model": p.model, "filters": p.filters}
            if t["org_id"] and t["entity_column"] and not spec["entity_column"]:
                spec["entity_column"] = t["entity_column"]
            errors = validate_spec(cfg, hid, spec)
            exp_id = db.add_experiment(hid, t["org_id"], None, p.statement, p.metric, p.baseline, p.success_rule, spec)
            if errors:
                db.update_experiment(exp_id, flags_json={"chyby_návrhu": errors})
            stats["drafts"] += 1
    return dict(stats)


def prereg_hash(exp) -> str:
    payload = {k: exp[k] for k in ("statement", "metric", "baseline", "success_rule", "spec_json")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def lock(cfg: Config, db: DB, exp_id: int) -> str:
    """Predregistrácia: po uzamknutí sa hypotéza, metrika a postup nedajú zmeniť (hash a čas sú v DB)."""
    exp = db.get_experiment(exp_id)
    if exp is None:
        raise ValueError("Experiment neexistuje.")
    if exp["locked_at"]:
        return exp["prereg_hash"]
    errors = validate_spec(cfg, exp["hypothesis_id"], json.loads(exp["spec_json"]))
    if errors:
        raise ValueError("Predregistráciu nemožno uzamknúť: " + "; ".join(errors))
    digest = prereg_hash(exp)
    db.update_experiment(exp_id, prereg_hash=digest, locked_at=utcnow(), status="locked")
    return digest


# ======================================================================= spätný test (iba Python)
def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def series(cfg: Config, hid: int, spec: dict[str, Any]) -> list[tuple[Any, float]]:
    """Časový rad (obdobie, hodnota) z DuckDB; SQL skladá kód z overených názvov stĺpcov (nie model)."""
    if validate_spec(cfg, hid, spec):
        raise DatasetError("špecifikácia odkazuje na neexistujúcu tabuľku alebo stĺpec")
    agg = {"sum": "SUM", "avg": "AVG", "count": "COUNT"}[spec.get("aggregation", "sum")]
    conds, params = [], []
    if spec.get("entity_column") and spec.get("entity_value"):
        conds.append(f"lpad(trim(CAST({_q(spec['entity_column'])} AS VARCHAR)), 8, '0') = ?")
        params.append(str(spec["entity_value"]).zfill(8))
    for col, value in (spec.get("filters") or {}).items():
        conds.append(f"trim(CAST({_q(col)} AS VARCHAR)) = ?")
        params.append(str(value).strip())
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    period = spec.get("period") or "none"
    t = _q(spec["time_column"])
    if period in ("year", "quarter", "month"):  # dátum → obdobie (každá zmluva by inak bola samostatné obdobie)
        raw = t
        t = f"CAST(date_trunc('{period}', TRY_CAST({raw} AS TIMESTAMP)) AS DATE)"
        # Preklepy v dátumoch (CRZ: roky 0002, 3202) by vytvorili nezmyselné obdobia na začiatku a konci radu.
        where += (" AND " if where else "WHERE ") + (f"TRY_CAST({raw} AS TIMESTAMP) BETWEEN TIMESTAMP '1990-01-01' "
                                                   "AND current_date + INTERVAL 366 DAY")
    sql = (f"SELECT {t} AS t, {agg}(TRY_CAST({_q(spec['value_column'])} AS DOUBLE)) AS y "
           f"FROM {spec['table']} {where} GROUP BY 1 ORDER BY 1")
    rows = run_query(cfg, hid, sql, params)
    return [(t, float(y)) for t, y in rows if t is not None and y is not None]


MODELS = ("linear_trend", "ar1", "ses", "holt_damped", "seasonal_trend", "auto")
AUTO_CANDIDATES = ("linear_trend", "ar1", "ses", "holt_damped", "seasonal_trend")
GRID = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9)


def season_length(period: str | None, labels: list[Any]) -> int:
    """Dĺžka sezóny: podľa zoskupenia (mesiac 12, štvrťrok 4, rok 1), inak z mediánu rozostupu dátumov."""
    if period in ("month", "quarter", "year"):
        return {"month": 12, "quarter": 4, "year": 1}[period]
    days = []
    for t in labels:
        try:
            days.append(date.fromisoformat(str(t)[:10]).toordinal())
        except ValueError:
            return 1  # roky ako čísla, texty: bez sezónnosti
    gaps = [b - a for a, b in zip(days, days[1:]) if b > a]
    if not gaps:
        return 1
    gap = median(gaps)
    return 12 if gap <= 35 else 4 if 80 <= gap <= 100 else 1


def _ses(y: np.ndarray, alpha: float) -> tuple[float, float]:
    """(predpoveď na ďalšie obdobie, súčet štvorcov chýb predpovedí o jedno obdobie v histórii)."""
    level, sse = y[0], 0.0
    for v in y[1:]:
        sse += (v - level) ** 2
        level = alpha * v + (1 - alpha) * level
    return float(level), float(sse)


def _holt_damped(y: np.ndarray, alpha: float, beta: float, phi: float = 0.9) -> tuple[float, float]:
    level, trend, sse = y[1], y[1] - y[0], 0.0
    for v in y[2:]:
        pred = level + phi * trend
        sse += (v - pred) ** 2
        new_level = alpha * v + (1 - alpha) * pred
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        level = new_level
    return float(level + phi * trend), float(sse)


def _auto(history: np.ndarray, season: int, inner: int = 3) -> str:
    """Model s najmenšou chybou predpovedí o jedno obdobie na posledných `inner` bodoch histórie
    (každá z predchádzajúcich hodnôt) – testované obdobie sa do výberu nedostane."""
    n = len(history)
    start = max(3, n - inner)
    if start >= n:
        return "linear_trend"
    errors = {k: float(np.mean([abs(forecast(history[:i], k, season) - history[i]) for i in range(start, n)]))
              for k in AUTO_CANDIDATES}
    return min(AUTO_CANDIDATES, key=lambda k: errors[k])  # pri zhode vyhrá jednoduchší (poradie zoznamu)


def forecast(history: np.ndarray, kind: str, season: int = 1) -> float:
    n = len(history)
    if kind == "last_value":
        return float(history[-1])
    if kind == "mean":
        return float(history.mean())
    if kind == "seasonal_naive":
        return float(history[-season]) if season > 1 and n >= season else float(history[-1])
    if kind == "linear_trend":
        if n < 2:
            return float(history[-1])
        a, b = np.polyfit(np.arange(n), history, 1)
        return float(a * n + b)
    if kind == "ar1":  # y_t = c + phi * y_{t-1} (najmenšie štvorce)
        x, y = history[:-1], history[1:]
        if len(x) < 2 or np.allclose(x, x[0]):
            return float(history[-1])
        phi, c = np.polyfit(x, y, 1)
        return float(c + phi * history[-1])
    if kind == "ses":  # jednoduché exponenciálne vyrovnávanie, alfa z mriežky podľa chýb v histórii
        if n < 3:
            return float(history[-1])
        return min((_ses(history, a) for a in GRID), key=lambda r: r[1])[0]
    if kind == "holt_damped":  # Holt s tlmeným trendom (phi 0,9)
        if n < 4:
            return forecast(history, "ses", season)
        return min((_holt_damped(history, a, b) for a, b in itertools.product(GRID, (0.1, 0.3))), key=lambda r: r[1])[0]
    if kind == "seasonal_trend":  # trend + sezónne fiktívne premenné (najmenšie štvorce); bez sezóny = trend
        if season <= 1 or n < 2 * season:
            return forecast(history, "linear_trend", season)
        t = np.arange(n + 1)
        X = np.column_stack([np.ones(n + 1), t] + [(t % season == s).astype(float) for s in range(1, season)])
        coef, *_ = np.linalg.lstsq(X[:n], history, rcond=None)
        return float(X[n] @ coef)
    if kind == "auto":
        return forecast(history, _auto(history, season), season)
    raise ValueError(f"neznámy postup {kind}")


def backtest_series(values: list[float], baseline: str, model: str, folds: int, min_history: int, metric: str = "MAE",
                    bootstrap: int = 1000, seed: int = 0, season: int = 1) -> dict[str, Any]:
    """Rolling origin: pre každé z posledných `folds` období predpoveď z predchádzajúcich hodnôt."""
    y = np.asarray(values, dtype=float)
    if len(y) < min_history + 1:
        raise ValueError(f"málo období ({len(y)}), treba aspoň {min_history + 1}")
    folds = min(folds, len(y) - min_history)
    rows = []
    for i in range(len(y) - folds, len(y)):
        hist = y[:i]
        row = {"index": i, "actual": float(y[i]), "model": forecast(hist, model, season),
               "baseline": forecast(hist, baseline, season)}
        if model == "auto":
            row["vybraný_model"] = _auto(hist, season)
        rows.append(row)
    err = lambda pred, act: abs(pred - act) / abs(act) * 100 if metric == "MAPE" and act else abs(pred - act)  # noqa: E731
    e_model = np.array([err(r["model"], r["actual"]) for r in rows])
    e_base = np.array([err(r["baseline"], r["actual"]) for r in rows])
    diff = e_base - e_model  # > 0: model je lepší
    rng = np.random.default_rng(seed)
    boots = [rng.choice(diff, size=len(diff), replace=True).mean() for _ in range(bootstrap)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    m_model, m_base = float(e_model.mean()), float(e_base.mean())
    improvement = (1 - m_model / m_base) if m_base else 0.0
    return {"folds": rows, "metric": metric, "model_error": round(m_model, 4), "baseline_error": round(m_base, 4),
            "improvement": round(improvement, 4), "diff_ci95": [round(float(lo), 4), round(float(hi), 4)],
            "n_periods": int(len(y)), "n_folds": len(rows), "model": model, "season": season}


def run_backtest(cfg: Config, db: DB, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    hyps = [db.get_hypothesis(i) for i in ids] if ids else db.gate2_hypotheses()
    for hyp in [h for h in hyps if h is not None]:
        for exp in db.experiments_for(hyp["id"]):
            if deadline.expired():
                return dict(stats)
            if exp["status"] != "locked":  # bez predregistrácie sa spätný test nespúšťa
                continue
            spec = json.loads(exp["spec_json"])
            flags = json.loads(exp["flags_json"] or "{}")
            try:
                data = series(cfg, hyp["id"], spec)
                res = backtest_series([v for _, v in data], exp["baseline"], spec.get("model", "linear_trend"),
                                      cfg.gate2.backtest_folds, cfg.gate2.min_history, exp["metric"], cfg.gate2.bootstrap,
                                      season=season_length(spec.get("period"), [t for t, _ in data]))
            except (DatasetError, ValueError, np.linalg.LinAlgError) as exc:
                flags["chyba_testu"] = str(exc)[:200]
                db.update_experiment(exp["id"], status="failed", flags_json=flags)
                stats["failed"] += 1
                continue
            res["periods"] = [str(t) for t, _ in data]
            res["values"] = [v for _, v in data]
            flags.pop("chyba_testu", None)
            flags["model_neprekonal_naivny_odhad"] = res["improvement"] <= 0
            flags["malo_obdobi"] = res["n_folds"] < 4
            db.update_experiment(exp["id"], status="done", result_json=res, flags_json=flags)
            stats["done"] += 1
    return dict(stats)


# ======================================================================= demo
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") for n in NUM_RE.findall(text or "")}


def demo_facts(exp, res: dict[str, Any]) -> list[str]:
    who = exp["org_name"] or "celý dataset"
    return [
        f"F1: Organizácia: {who}.",
        f"F2: Spätný test na posledných {res['n_folds']} obdobiach z {res['n_periods']}.",
        f"F3: Chyba modelu ({res['metric']}): {res['model_error']}.",
        f"F4: Chyba naivného odhadu ({exp['baseline']}): {res['baseline_error']}.",
        f"F5: Zlepšenie oproti naivnému odhadu: {res['improvement'] * 100:.1f} %.",
        f"F6: 95 % interval rozdielu chýb: {res['diff_ci95'][0]} až {res['diff_ci95'][1]}.",
    ]


def svg_chart(periods: list[str], values: list[float], folds: list[dict[str, Any]], width: int = 640, height: int = 240) -> str:
    """Jednoduchý čiarový graf (skutočnosť, model, naivný odhad) ako inline SVG, bez knižníc."""
    allv = values + [f["model"] for f in folds] + [f["baseline"] for f in folds]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1.0
    n = len(values)
    px = lambda i: 40 + i * (width - 60) / max(1, n - 1)  # noqa: E731
    py = lambda v: height - 30 - (v - lo) / span * (height - 50)  # noqa: E731
    line = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
    pts = lambda key, color: "".join(  # noqa: E731
        f"<circle cx='{px(f['index']):.1f}' cy='{py(f[key]):.1f}' r='4' fill='{color}'/>" for f in folds)
    labels = "".join(f"<text x='{px(i):.1f}' y='{height - 10}' font-size='10' text-anchor='middle'>{html.escape(p)}</text>"
                     for i, p in enumerate(periods) if n <= 12 or i % max(1, n // 12) == 0)
    return (f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='Spätný test'>"
            f"<polyline points='{line}' fill='none' stroke='#334155' stroke-width='2'/>{pts('model', '#2563eb')}"
            f"{pts('baseline', '#d97706')}{labels}</svg>"
            "<p><small>čiara: skutočnosť · modré body: model · oranžové body: naivný odhad</small></p>")


def build_demo(cfg: Config, db: DB, llm: Any, exp) -> Path:
    router = as_router(llm, cfg, db)
    res = json.loads(exp["result_json"])
    hyp = db.get_hypothesis(exp["hypothesis_id"])
    facts = demo_facts(exp, res)
    text, note = "", ""
    out = router.run_task(
        "gate2_demo",
        [{"role": "system", "content": GATE2_DEMO_SYSTEM.format(lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"PROBLEM: {hyp['title']}\nFACTS:\n" + "\n".join(facts)}],
        DemoText, object_type="experiment", object_id=exp["id"], max_tokens=600,
    )
    if out.ok:
        allowed = _numbers(re.sub(r"\bF\d+:", "", "\n".join(facts)))  # označenia faktov (F5) nie sú čísla
        extra = _numbers(re.sub(r"\bF\d+\b", "", out.value.text)) - allowed
        if extra:
            note = "Text modelu obsahoval čísla mimo faktov (" + ", ".join(sorted(extra)[:5]) + ") a nepoužil sa."
        else:
            text = out.value.text
    spec = json.loads(exp["spec_json"])
    datasets = [d for d in db.datasets_for(exp["hypothesis_id"], only_ok=True) if d["table_name"] == base_table(spec.get("table", ""))]
    prov = "".join(f"<li>{html.escape(d['url'])} · stiahnuté {html.escape(d['fetched_at'][:10])} · SHA-256 "
                   f"{html.escape((d['sha256'] or '')[:16])}…</li>" for d in datasets)
    rows = "".join(f"<tr><td>{html.escape(res['periods'][f['index']])}</td><td>{f['actual']:.2f}</td>"
                   f"<td>{f['model']:.2f}</td><td>{f['baseline']:.2f}</td></tr>" for f in res["folds"])
    title = f"{hyp['title']} – {exp['org_name'] or 'celý dataset'}"
    body = f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#0f172a}}
table{{border-collapse:collapse}}td,th{{border:1px solid #cbd5e1;padding:4px 8px;text-align:right}}</style></head><body>
<h1>{html.escape(title)}</h1>
<p><b>Predregistrovaná hypotéza:</b> {html.escape(exp['statement'])}<br><b>Pravidlo úspechu:</b>
{html.escape(exp['success_rule'])}<br><small>uzamknuté {html.escape(exp['locked_at'] or '')} · hash
{html.escape((exp['prereg_hash'] or '')[:16])}…</small></p>
{svg_chart(res['periods'], res['values'], res['folds'])}
<ul>{''.join(f'<li>{html.escape(f)}</li>' for f in facts)}</ul>
<table><tr><th>obdobie</th><th>skutočnosť</th><th>model</th><th>naivný odhad</th></tr>{rows}</table>
{f'<p>{html.escape(text)}</p>' if text else ''}{f'<p><small>{html.escape(note)}</small></p>' if note else ''}
<h2>Pôvod dát</h2><ul>{prov}</ul>
<p><small>Lokálny výpočet (DuckDB, Python). Text vyššie napísal lokálny model iba z uvedených faktov.</small></p>
</body></html>"""
    out_dir = cfg.path(cfg.gate2.demos_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"H{exp['hypothesis_id']}_E{exp['id']}.html"
    path.write_text(body, encoding="utf-8")
    return path


def run_demo(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    hyps = [db.get_hypothesis(i) for i in ids] if ids else db.gate2_hypotheses()
    for hyp in [h for h in hyps if h is not None]:
        for exp in db.experiments_for(hyp["id"]):
            if deadline.expired():
                return dict(stats)
            if exp["status"] != "done" or exp["demo_path"]:
                continue
            path = build_demo(cfg, db, llm, exp)
            db.update_experiment(exp["id"], demo_path=str(path))
            stats["demos"] += 1
    return dict(stats)
