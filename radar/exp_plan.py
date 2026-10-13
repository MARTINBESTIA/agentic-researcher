"""Návrh experimentov Brány 2 z problémovej karty a profilu dát – univerzálne pre každú hypotézu.

Model (presná rola) navrhne niekoľko experimentov: typ úlohy (poradie, predpoveď, anomália, prah, porovnanie),
prípravu dát (operácie z `exp_ops`), štítok, príznaky, hlavnú metriku, hlavný naivný odhad a prah úspechu.
Kód návrh overí skúšobným behom na dátach (stĺpce, výrazy, únik budúcich dát, počet pozitívnych prípadov,
dĺžka radu); chyby vráti modelu na jednu opravu. Uzamyká človek (predregistrácia), testuje kód (`exp_tasks`).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .cards import card_text
from .config import Config
from .datasets import DatasetError, base_table, planner_profile
from .db import DB
from .exp_ops import Step, Workspace, check_expr, q
from .models import as_router
from .prompts import EXPERIMENT_PLAN_SYSTEM
from .util import Deadline

log = logging.getLogger(__name__)

TASKS = ("ranking", "forecast", "anomaly", "threshold", "comparison")
# metrika → smer („higher“ = väčšia je lepšia)
METRICS: dict[str, dict[str, str]] = {
    "ranking": {"precision@k": "higher", "recall@k": "higher", "avg_precision": "higher", "roc_auc": "higher"},
    "forecast": {"MASE": "lower", "MAE": "lower", "RMSE": "lower", "sMAPE": "lower", "MAPE": "lower"},
    "anomaly": {"flag_precision": "higher", "coverage95_error": "lower", "MASE": "lower"},
    "threshold": {"excess_ratio": "higher"},
    "comparison": {"flag_precision": "higher", "lift": "higher"},
}
BASELINES: dict[str, tuple[str, ...]] = {
    "ranking": ("random", "value_desc", "flag_count", "entity_rate"),
    "forecast": ("last_value", "mean", "seasonal_naive", "drift"),
    "anomaly": ("seasonal_naive", "last_value", "mean"),
    "threshold": ("smooth_density",),
    "comparison": ("random",),
}
TASK_LABELS = {"ranking": "poradie (čo skontrolovať skôr)", "forecast": "predpoveď", "anomaly": "neobvyklé obdobia",
               "threshold": "hromadenie pod prahom", "comparison": "porovnanie s podobnými"}


class ExperimentPlan(BaseModel):
    title: str = ""
    statement: str = ""
    problem_link: str = ""
    task: Literal["ranking", "forecast", "anomaly", "threshold", "comparison"] = "ranking"
    table: str = ""
    time_column: str = ""
    entity_column: str = ""
    steps: list[Step] = Field(default_factory=list)
    label: str = ""
    value: str = ""
    aggregation: Literal["sum", "count", "avg"] = "sum"
    period: Literal["month", "quarter", "year"] = "month"
    features: list[str] = Field(default_factory=list)
    category: str = ""
    thresholds: list[float] = Field(default_factory=list)
    threshold_claims: list[int] = Field(default_factory=list)
    capacity: int = 50
    primary_metric: str = ""
    primary_baseline: str = ""
    success_threshold: float = 10.0


class PlanSet(BaseModel):
    experiments: list[ExperimentPlan] = Field(default_factory=list)
    missing_data: str = ""  # keď dáta rozhodnutie otestovať nedokážu: aké dáta by boli treba


AUTO_LOCK_KEY = "gate2_auto_lock"  # {"enabled": bool, "since": čas, "by": "GUI"} – uzamknúť bezchybné návrhy hneď


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """Odtlačok plánu bez skúšobného behu a metadát – podľa neho sa zistí, či človek plán pred uzamknutím zmenil."""
    import hashlib

    core = {k: v for k, v in plan.items() if k not in ("skúšobný_beh", "návrh")}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def plan_quality(db: DB) -> dict[str, Any]:
    """Ako sa darí modelu navrhovať experimenty: na prvý pokus bez chýb, po oprave, uzamknuté bez úprav."""
    rows = db._all("SELECT spec_json, flags_json, locked_at FROM experiments WHERE task IS NOT NULL")
    c: Counter = Counter()
    for r in rows:
        meta = (json.loads(r["spec_json"] or "{}") or {}).get("návrh") or {}
        flags = json.loads(r["flags_json"] or "{}") or {}
        c["návrhy"] += 1
        c["bez_chýb_na_prvý_pokus"] += bool(meta.get("bez_chýb_na_prvý_pokus"))
        c["bez_chýb_po_oprave"] += bool(meta.get("bez_chýb_po_oprave")) and not meta.get("bez_chýb_na_prvý_pokus")
        c["s_chybami"] += bool(flags.get("chyby_návrhu"))
        c["uzamknuté"] += bool(r["locked_at"])
        c["uzamknuté_bez_úprav"] += bool(r["locked_at"]) and flags.get("upravené_pred_uzamknutím") is False
        c["uzamknuté_automaticky"] += bool(flags.get("uzamknuté_automaticky"))
    return dict(c)


def success_rule(plan: dict[str, Any]) -> str:
    """Pravidlo úspechu píše kód (prah vyhodnocuje `gate2.rule_threshold`)."""
    if plan["task"] == "threshold":
        return (f"Nadbytok hodnôt tesne pod prahom je aspoň o {plan['success_threshold']:g} % nad hladkým rozdelením "
                "a 95 % interval nezahŕňa nulu.")
    return (f"{plan['primary_metric']} vybraného modelu je aspoň o {plan['success_threshold']:g} % lepšia ako naivný odhad "
            f"{plan['primary_baseline']} na testovacom období.")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in re.findall(r"\d[\d  .]*(?:,\d+)?", text or ""):
        try:
            out.add(float(m.replace(" ", "").replace(" ", "").replace(".", "").replace(",", ".")))
        except ValueError:
            pass
    return out


ICO_COL_RE = re.compile(r"(^|_)i[cč]o($|_)", re.I)
DATE_COL_RE = re.compile(r"datum|dátum|date|zaciatok|koniec|_at$|obdobie", re.I)


def _table_columns(cfg: Config, hid: int, table: str) -> list[str]:
    from .datasets import table_columns

    return table_columns(cfg, hid, table) if table else []


def precheck_steps(cfg: Config, hid: int, plan: dict[str, Any]) -> list[str]:
    """Chýbajúce parametre krokov naraz (s názvami polí JSON a ponukou stĺpcov) – model ich doplní v jednej oprave.
    Bezpečné doplnenie kódom: lookup s jediným stĺpcom IČO v druhej tabuľke dostane target_column a match „ico“."""
    errors = []
    base = _table_columns(cfg, hid, plan.get("table", ""))
    created = {s.get("name") for s in plan.get("steps") or [] if s.get("name")}
    feats = []
    for i, f in enumerate(plan.get("features") or []):  # 27B občas zadá výraz (TRY_CAST(...)) namiesto stĺpca
        if f and f not in base and f not in created and re.search(r"[()+*/<>=]|\bcase\b", f, re.I):
            name = f"priznak_{i + 1}"
            plan.setdefault("steps", []).append({"op": "derive", "name": name, "expr": f,
                                                 "why": "príznak z výrazu (doplnil kód)"})
            created.add(name)
            feats.append(name)
        else:
            feats.append(f)
    if plan.get("features"):
        plan["features"] = feats
    created = set()
    for i, s in enumerate(plan.get("steps") or []):
        op, name = s.get("op"), s.get("name", "")
        where = f'krok {i + 1} ({op} "{name}")'
        if op != "filter" and not name:
            errors.append(f'{where}: chýba "name" (nový stĺpec, malé písmená a _)')
        if op in ("filter", "derive") and not (s.get("expr") or "").strip():
            errors.append(f'{where}: chýba "expr"')
        if op == "lookup":
            other = _table_columns(cfg, hid, s.get("table", ""))
            if not other:
                errors.append(f'{where}: "table" musí byť iná tabuľka z DATA')
                continue
            icos = [c for c in other if ICO_COL_RE.search(c)]
            if not s.get("target_column") and len(icos) == 1:
                s["target_column"], s["match"] = icos[0], "ico"  # jednoznačné – doplní kód
            if s.get("target_column") and ICO_COL_RE.search(s["target_column"]):
                s["match"] = "ico"
            row_icos = [c for c in base + sorted(created) if ICO_COL_RE.search(c)]
            if len(s.get("keys") or []) != 1:
                errors.append(f'{where}: "keys" musí byť zoznam s jedným stĺpcom riadku, napr. '
                              + " alebo ".join(f'["{c}"]' for c in row_icos[:3]))
            if not s.get("target_column"):
                errors.append(f'{where}: chýba "target_column" (stĺpec v {s.get("table")}, napr. {", ".join(icos[:3]) or other[0]})')
            if int(s.get("days") or 0) != 0 and not s.get("other_time"):
                dates = [c for c in other if DATE_COL_RE.search(c)]
                errors.append(f'{where}: "days" ≠ 0 potrebuje "other_time" (dátum v {s.get("table")}: {", ".join(dates[:4])})')
            if int(s.get("days") or 0) == 0:
                errors.append(f'{where}: nastav "days" – záporné = história pred dátumom riadku (príznak), kladné = '
                              "udalosť po ňom (štítok); 0 = kedykoľvek iba ak čas nie je známy")
            elif -7 < int(s.get("days") or 0) < 0:  # reálny beh: 27B dal -1 v zmysle „celá história“ (okno 1 deň)
                errors.append(f'{where}: "days" {s.get("days")} je okno iba {-int(s["days"])} dní pred riadkom – pre celú '
                              'históriu použi napr. -3650, pre posledný rok -365')
        if op in ("linked_agg", "future_agg") and int(s.get("days") or 0) <= 0:
            errors.append(f'{where}: "days" musí byť kladné (okno po dátume riadku)')
        if op == "linked_agg" and not s.get("link"):
            errors.append(f'{where}: chýba "link" (stĺpec z kroku link_records)')
        if op == "link_records":
            for f in ("column", "pattern", "target_column"):
                if not s.get(f):
                    errors.append(f'{where}: chýba "{f}"')
        if op == "text_category" and (not s.get("column") or len(s.get("categories") or []) < 2):
            errors.append(f'{where}: potrebuje "column" a aspoň 2 "categories"')
        if s.get("agg") in ("sum", "avg", "max") and op in ("prior_agg", "future_agg", "linked_agg") and not s.get("column"):
            errors.append(f'{where}: agg {s.get("agg")} potrebuje "column" (číselný stĺpec)')
        if s.get("agg") in ("sum", "avg", "max") and op == "lookup" and not s.get("other_value"):
            errors.append(f'{where}: agg {s.get("agg")} potrebuje "other_value"')
        if name:
            created.add(name)
    if plan.get("task") == "ranking" and plan.get("primary_baseline") == "value_desc" and not (plan.get("value") or "").strip():
        errors.append('naivný odhad value_desc potrebuje "value" (napr. suma zmluvy) – doplň "value" alebo zvoľ iný naivný odhad')
    if plan.get("task") == "ranking" and plan.get("primary_baseline") == "entity_rate" and not plan.get("entity_column"):
        errors.append('naivný odhad entity_rate potrebuje "entity_column"')
    return errors


FUTURE_OPS = ("future_agg", "linked_agg", "link_records")


def plan_signature(plan: dict[str, Any]) -> str:
    """Čo experiment naozaj meria (bez názvov a textov): úloha + ako vzniká štítok alebo hodnota. Rovnaký podpis =
    duplikát (reálny beh: E7 bol E6 s iným názvom)."""
    task = plan.get("task")
    def step_sig(s):
        return (s.get("op"), s.get("table", ""), tuple(s.get("keys") or []), s.get("target_column", ""),
                int(s.get("days") or 0), (s.get("expr") or "").strip().lower(), s.get("agg", ""))
    if task in ("ranking", "anomaly", "comparison") and plan.get("label"):
        future = sorted(step_sig(s) for s in plan.get("steps") or [] if s.get("op") in FUTURE_OPS or
                        (s.get("op") == "lookup" and int(s.get("days") or 0) >= 0))
        core = (task, tuple(future), plan.get("entity_column", "") if task == "ranking" else plan.get("category", ""))
    elif task == "threshold":
        core = (task, (plan.get("value") or "").lower(), tuple(sorted(plan.get("thresholds") or [])))
    else:
        core = (task, (plan.get("value") or "").lower(), plan.get("entity_column", ""), plan.get("period", ""),
                plan.get("category", ""))
    return json.dumps(core, ensure_ascii=False, default=str)


def validate_plan(cfg: Config, db: DB, hid: int, plan: dict[str, Any], quotes: dict[int, str] | None = None,
                  workspace: Workspace | None = None) -> dict[str, Any]:
    """Skúšobný beh návrhu: {"chyby": [...], "upozornenia": [...], "štatistiky": {...}}. Chyby blokujú uzamknutie."""
    from .gate2 import data_limits

    errors, warnings, stats = [], [], {}
    task = plan.get("task")
    if task not in TASKS:
        return {"chyby": [f"neznámy typ úlohy {task!r}"], "upozornenia": [], "štatistiky": {}}
    tables = {d["table_name"] for d in db.datasets_for(hid, only_ok=True)}
    if base_table(plan.get("table", "")) not in tables:
        return {"chyby": [f"tabuľka {plan.get('table')!r} nie je medzi datasetmi hypotézy ({', '.join(sorted(tables))})"],
                "upozornenia": [], "štatistiky": {}}
    pre = precheck_steps(cfg, hid, plan)
    if pre:
        return {"chyby": pre, "upozornenia": [], "štatistiky": {}}
    if plan.get("primary_metric") not in METRICS[task]:
        errors.append(f"hlavná metrika {plan.get('primary_metric')!r} nie je pre {task} (možnosti: {', '.join(METRICS[task])})")
    if plan.get("primary_baseline") not in BASELINES[task]:
        errors.append(f"naivný odhad {plan.get('primary_baseline')!r} nie je pre {task} (možnosti: {', '.join(BASELINES[task])})")
    for key in ("statement", "problem_link"):
        if not (plan.get(key) or "").strip():
            errors.append(f"chýba {key} (výrok, väzba na rozhodnutie z karty)")
    ws = workspace
    try:
        ws = ws or Workspace(cfg, hid, plan["table"], plan.get("time_column", ""), data_limits(db, hid, plan["table"]))
    except DatasetError as exc:
        return {"chyby": errors + [str(exc)], "upozornenia": warnings, "štatistiky": stats}
    try:
        for i, raw in enumerate(plan.get("steps") or []):
            try:
                if raw.get("op") == "lookup" and base_table(raw.get("table", "")) not in tables:
                    raise DatasetError(f"tabuľka {raw.get('table')!r} nie je medzi datasetmi hypotézy")
                ws.apply(Step.model_validate(raw))
            except (DatasetError, ValueError) as exc:
                errors.append(f"krok {i + 1} ({raw.get('op')} {raw.get('name', '')}): {exc}")
                return {"chyby": errors, "upozornenia": warnings, "štatistiky": stats}
        cols = ws.columns()
        stats["riadky"] = ws.rows()
        ent = plan.get("entity_column") or ""
        if ent and ent not in cols:
            errors.append(f"stĺpec entity {ent!r} neexistuje")
        value = (plan.get("value") or "").strip()
        if value:
            try:
                check_expr(value)
                ws.query(f"SELECT TRY_CAST(({value}) AS DOUBLE) FROM {ws.table} LIMIT 5")
            except (DatasetError, Exception) as exc:  # noqa: BLE001 – chyba výrazu od modelu
                errors.append(f"hodnota {value!r}: {str(exc)[:200]}")
                value = ""
        if task == "ranking":
            _check_ranking(cfg, ws, plan, cols, errors, warnings, stats)
        elif task in ("forecast", "anomaly"):
            if not value:
                errors.append("predpoveď potrebuje „value“ (číselný stĺpec alebo výraz)")
            else:
                _check_series(cfg, ws, plan, value, errors, stats)
            if task == "anomaly" and plan.get("label") and plan["label"] not in cols:
                errors.append(f"štítok {plan['label']!r} neexistuje")
        elif task == "threshold":
            _check_threshold(ws, plan, value, quotes or {}, errors, warnings, stats)
        elif task == "comparison":
            cat = plan.get("category") or ""
            if cat not in cols:
                errors.append(f"stĺpec kategórie {cat!r} neexistuje")
            elif value:
                groups = ws.query(f"SELECT COUNT(*) FROM (SELECT {q(cat)} FROM {ws.table} GROUP BY 1 HAVING COUNT(*) >= 20)")[0][0]
                stats["kategórie_s_20_riadkami"] = int(groups)
                if groups < 3:
                    errors.append(f"iba {groups} kategórie s aspoň 20 riadkami – porovnanie nemá s čím porovnať")
            if not value:
                errors.append("porovnanie potrebuje „value“")
            if plan.get("label") and plan["label"] not in cols:
                errors.append(f"štítok {plan['label']!r} neexistuje")
            elif not plan.get("label"):
                warnings.append("bez štítku je porovnanie iba popisné (úspech sa nevyhodnotí)")
        stats["protokol"] = ws.protocol
    finally:
        if workspace is None:
            ws.close()
    return {"chyby": errors, "upozornenia": warnings, "štatistiky": stats}


def _check_ranking(cfg, ws: Workspace, plan, cols, errors, warnings, stats) -> None:
    label = plan.get("label") or ""
    if label not in cols:
        errors.append(f"štítok {label!r} neexistuje (vytvor ho krokom future_agg, linked_agg alebo derive)")
        return
    if plan.get("entity_column") and plan.get("primary_baseline") != "entity_rate":
        # reálny beh E6: štítok sa opakoval u toho istého dodávateľa – „najväčšie najprv“ bolo slabé porovnanie (+900 %)
        warnings.append("hlavný naivný odhad nie je doterajšia miera entity (entity_rate); keď sa štítok opakuje u tej "
                        "istej entity, je to najsilnejší naivný odhad – zváž ho ako hlavný")
    feats = [f for f in plan.get("features") or [] if f]
    missing = [f for f in feats if f not in cols]
    if missing:
        errors.append(f"príznaky neexistujú: {', '.join(missing)}")
    leaks = [f for f in feats if f in ws.future or f == label]
    if leaks:
        errors.append(f"príznaky z budúcnosti alebo štítok medzi príznakmi (únik): {', '.join(leaks)}")
    if len(feats) - len(missing) - len(leaks) < 1:
        errors.append("poradie potrebuje aspoň jeden príznak známy v deň riadku")
    if label not in ws.future and not ws._uses_future(label):
        warnings.append("štítok nevznikol z budúcnosti – model by predpovedal súčasný stav, nie budúcu udalosť")
    ws.matured(label)
    pos, n = ws.query(f"SELECT SUM(CASE WHEN TRY_CAST({q(label)} AS DOUBLE) > 0 THEN 1 ELSE 0 END), COUNT(*) "
                      f"FROM {ws.table}")[0]
    pos, n = int(pos or 0), int(n or 0)
    stats |= {"riadky_so_štítkom": n, "pozitívne": pos, "podiel_pozitívnych": round(pos / n, 4) if n else None}
    test_pos = pos * cfg.experiments.test_share
    if test_pos < cfg.experiments.min_positives:
        errors.append(f"málo pozitívnych prípadov ({pos}, v teste ~{test_pos:.0f}; treba aspoň "
                      f"{cfg.experiments.min_positives} v teste)")
    if n and pos / n > 0.5:
        warnings.append(f"štítok má {pos / n:.0%} pozitívnych – poradie má malý zmysel, zváž prísnejšiu podmienku")
    cap = int(plan.get("capacity") or 0)
    if not 5 <= cap <= 5000:
        errors.append("capacity (koľko položiek používateľ skontroluje) musí byť 5–5000")


def _check_series(cfg, ws: Workspace, plan, value, errors, stats) -> None:
    period = plan.get("period") or "month"
    ent = plan.get("entity_column") or ""
    group = f", {q(ent)}" if ent and ent in ws.columns() else ""
    n = ws.query(f"SELECT COUNT(DISTINCT date_trunc('{period}', _t)) FROM {ws.table}")[0][0]
    stats["obdobia"] = int(n)
    need = cfg.gate2.min_history + 1
    if n < need:
        errors.append(f"dáta majú {n} období po {period}; spätný test potrebuje aspoň {need} – zvoľ kratšie obdobie")
    if group:
        ents = ws.query(f"SELECT COUNT(*) FROM (SELECT {q(ent)} FROM {ws.table} GROUP BY 1 "
                        f"HAVING COUNT(DISTINCT date_trunc('{period}', _t)) >= {need})")[0][0]
        stats["entity_s_dostatkom_období"] = int(ents)
        if ents < 1:
            errors.append("žiadna entita nemá dosť období")


def _check_threshold(ws: Workspace, plan, value, quotes, errors, warnings, stats) -> None:
    if not value:
        errors.append("prah potrebuje „value“")
        return
    ths = [float(t) for t in plan.get("thresholds") or [] if t]
    if not ths:
        errors.append("chýbajú prahy (thresholds)")
        return
    lo, hi = ws.query(f"SELECT MIN(TRY_CAST(({value}) AS DOUBLE)), MAX(TRY_CAST(({value}) AS DOUBLE)) FROM {ws.table}")[0]
    out = [t for t in ths if lo is None or not (lo < t < hi)]
    if out:
        errors.append(f"prahy mimo rozsahu hodnôt ({lo} – {hi}): {out}")
    cited = set().union(*[_numbers(quotes.get(int(c), "")) for c in plan.get("threshold_claims") or []]) if quotes else set()
    unsourced = [t for t in ths if t not in cited]
    if unsourced:
        warnings.append(f"prah {', '.join(f'{t:g}' for t in unsourced)} nie je doslova v citovaných citátoch – over ho "
                        "(limity zákaziek sa menia novelami)")
    stats["prahy"] = ths


# ----------------------------------------------------------------------- návrh modelom
def describe_plan(e) -> str:
    """Pre model: čo existujúci experiment meria (úloha + ako vzniká štítok alebo hodnota), nie iba názov."""
    plan = json.loads(e["spec_json"])
    label_steps = [s for s in plan.get("steps") or [] if s.get("op") in FUTURE_OPS or
                   (s.get("op") == "lookup" and int(s.get("days") or 0) >= 0)]
    how = "; ".join(f"{s.get('op')} {s.get('table', '')} {','.join(s.get('keys') or [])} {s.get('days', '')} "
                    f"{s.get('expr', '')}".strip() for s in label_steps)
    what = (f"label {plan.get('label')} ({how})" if plan.get("label") else
            f"value {plan.get('value')} per {plan.get('period', '')} {plan.get('entity_column', '')}")
    return f"E{e['id']} {plan.get('task')}: {what}"


def _data_text(cfg: Config, db: DB, hid: int) -> str:
    """Profil dát a okno (počíta sa raz na hypotézu – nad miliónmi riadkov trvá desiatky sekúnd)."""
    from .gate2 import window_text

    tables = [d["table_name"] for d in db.datasets_for(hid, only_ok=True)]
    return f"DATA:\n{planner_profile(cfg, hid, tables)}\n\n{window_text(db, hid, tables)}"


def _prompt_user(cfg: Config, db: DB, hid: int, card: dict[str, Any], existing: list[str], data_text: str) -> str:
    claims = [c for c in db.evidence_for(hid) if json.loads(c["numbers_json"] or "[]")]
    numbers = "\n".join(f"C{c['id']}: {(c['quote'] or '')[:250]}" for c in claims[:15])
    return (f"PROBLEM CARD:\n{card_text(card)}\n\nEXISTING EXPERIMENTS (design a different one): "
            f"{'; '.join(existing) or 'none'}\n\n<untrusted_document>\nEVIDENCE WITH NUMBERS:\n{numbers or '(none)'}\n\n"
            f"{data_text}\n</untrusted_document>")


def plan_targets(cfg: Config, db: DB, ids: list[int] | None) -> list:
    hyps = [h for h in (db.get_hypothesis(i) for i in ids) if h] if ids else list(db.gate2_hypotheses())
    out = []
    for h in hyps:
        if h["split_at"]:  # rozdelená hypotéza: experimenty majú podhypotézy (jasnejší problém)
            continue
        card = db.latest_card(h["id"])
        if card is None or not db.datasets_for(h["id"], only_ok=True):
            continue
        missing = (json.loads(card["card_json"]) or {}).get("chýbajúce_dáta_pre_experiment") or {}
        if missing and missing.get("datasety") == len(db.datasets_for(h["id"], only_ok=True)):
            continue  # model povedal, že s týmito dátami sa rozhodnutie otestovať nedá; čaká sa na nový dataset
        new = [e for e in db.experiments_for(h["id"]) if e["task"] and e["status"] not in ("failed", "invalid")]
        if len(new) < cfg.experiments.plans_per_hypothesis:
            out.append((h, card))
    return out


def recheck_drafts(cfg: Config, db: DB, hid: int, quotes: dict[int, str]) -> Counter:
    """Neuzamknuté návrhy s chybami znova cez kontrolu (opravy kódom: výraz ako príznak, jednoznačné IČO);
    duplikát skoršieho experimentu dostane chybu."""
    stats: Counter = Counter()
    seen: dict[str, int] = {}
    for e in db.experiments_for(hid):
        if not e["task"]:
            continue
        plan = json.loads(e["spec_json"])
        sig = plan_signature(plan)
        flags = json.loads(e["flags_json"] or "{}")
        if e["status"] in ("draft", "invalid") and not e["locked_at"] and (flags.get("chyby_návrhu") or sig in seen):
            check = validate_plan(cfg, db, hid, plan, quotes)
            if sig in seen:
                check["chyby"].append(f"experiment meria to isté ako E{seen[sig]} – navrhni iný typ úlohy alebo štítok")
            plan["skúšobný_beh"] = check["štatistiky"]
            new_flags = {k: v for k, v in flags.items() if k not in ("chyby_návrhu", "upozornenia")}
            new_flags |= {k: v for k, v in (("chyby_návrhu", check["chyby"]), ("upozornenia", check["upozornenia"])) if v}
            db.update_experiment(e["id"], spec_json=plan, flags_json=new_flags,
                                 status="invalid" if check["chyby"] else "draft")
            stats["rechecked_ok" if not check["chyby"] else "rechecked_errors"] += 1
        seen.setdefault(sig, e["id"])
    return stats


def run_plans(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    system = EXPERIMENT_PLAN_SYSTEM.format(lang=cfg.pipeline.output_language,
                                           min_periods=cfg.gate2.min_history + cfg.gate2.backtest_folds)
    for hyp, card_row in plan_targets(cfg, db, ids):
        if deadline.expired():
            break
        hid = hyp["id"]
        card = json.loads(card_row["card_json"])
        quotes = {c["id"]: c["quote"] for c in db.evidence_for(hid)}
        stats.update(recheck_drafts(cfg, db, hid, quotes))  # staršie návrhy s chybami podľa aktuálnych kontrol
        data_text = _data_text(cfg, db, hid)
        todo = cfg.experiments.plans_per_hypothesis - len([e for e in db.experiments_for(hid) if e["task"]
                                                           and e["status"] not in ("failed", "invalid")])
        for _ in range(max(0, todo)):  # jeden plán na volanie (27B dáva úplnejší plán, ďalší sa líši od predošlých)
            if deadline.expired():
                break
            titles = [describe_plan(e) for e in db.experiments_for(hid) if e["task"]]
            user = _prompt_user(cfg, db, hid, card, titles, data_text)
            res = router.run_task("experiment_plan", [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}],
                                  PlanSet, object_type="hypothesis", object_id=hid, max_tokens=3000)
            if not res.ok:
                stats["llm_failures"] += 1
                break
            if not res.value.experiments:
                card["chýbajúce_dáta_pre_experiment"] = {"text": res.value.missing_data.strip(), "model": res.model,
                                                        "datasety": len(db.datasets_for(hid, only_ok=True))}
                db.update_card(card_row["id"], card=card)
                stats["no_suitable_data"] += 1
                break
            plan = res.value.experiments[0]
            data = plan.model_dump()
            taken = {plan_signature(json.loads(e["spec_json"])): e["id"] for e in db.experiments_for(hid) if e["task"]}

            def checked(d: dict[str, Any]) -> dict[str, Any]:
                c = validate_plan(cfg, db, hid, d, quotes)
                if plan_signature(d) in taken:
                    c["chyby"].append(f"experiment meria to isté ako E{taken[plan_signature(d)]} (rovnaký štítok alebo "
                                      "hodnota) – navrhni iný typ úlohy alebo iný štítok")
                return c

            check = checked(data)
            first_ok, attempts = not check["chyby"], 1
            for _ in range(cfg.experiments.max_plan_retries):
                if not check["chyby"] or deadline.expired():
                    break
                fix = router.run_task(
                    "experiment_plan",
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\nYOUR EXPERIMENT:\n" + json.dumps(data, ensure_ascii=False)
                      + "\n\nERRORS FOUND BY THE CODE (fix all, keep the intent):\n- " + "\n- ".join(check["chyby"])}],
                    ExperimentPlan, object_type="hypothesis", object_id=hid, max_tokens=2500)
                stats["repairs"] += 1
                if not fix.ok:
                    break
                data = fix.value.model_dump()
                check = checked(data)
                attempts += 1
            data["skúšobný_beh"] = {k: v for k, v in check["štatistiky"].items()}
            data["návrh"] = {"pokusy": attempts, "bez_chýb_na_prvý_pokus": first_ok, "bez_chýb_po_oprave": not check["chyby"],
                             "model": res.model, "odtlačok": plan_fingerprint(data)}
            flags = {k: v for k, v in (("chyby_návrhu", check["chyby"]), ("upozornenia", check["upozornenia"])) if v}
            exp_id = db.add_experiment(hid, None, None, data["statement"].strip() or data["title"], data["primary_metric"] or "?",
                                       data["primary_baseline"] or "?", success_rule(data), data, task=data["task"],
                                       card_id=card_row["id"], problem_link=data["problem_link"].strip())
            if flags:
                db.update_experiment(exp_id, flags_json=flags)
            if check["chyby"]:  # neplatný návrh ostáva kvôli meraniu kvality, ale neráta sa (ďalší beh navrhne náhradu)
                db.update_experiment(exp_id, status="invalid")
            stats["plans_ok" if not check["chyby"] else "plans_with_errors"] += 1
            auto = db.get_setting(AUTO_LOCK_KEY, {}) or {}
            if auto.get("enabled") and not check["chyby"] and not check["upozornenia"]:
                from .gate2 import lock  # predregistrácia ešte pred testom; človek to vopred povolil v GUI

                try:
                    lock(cfg, db, exp_id)
                    db.update_experiment(exp_id, flags_json={**flags, "uzamknuté_automaticky": True})
                    stats["auto_locked"] += 1
                except ValueError as exc:
                    log.warning("Automatické uzamknutie E%s zlyhalo: %s", exp_id, exc)
    return dict(stats)
