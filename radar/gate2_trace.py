"""Postup experimentu Brány 2 krok po kroku – rekonštrukcia z uložených údajov (výber organizácie, návrh modelu,
úpravy kódom, uzamknutie, dáta a SQL, kolá spätného testu, výpočet výsledku, demo). Nič sa tu nepočíta nanovo okrem
výberu modelu „auto“ (deterministický, z uložených hodnôt radu), aby bolo vidno, prečo vyhral daný model."""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .config import Config
from .datasets import base_table
from .db import DB
from .gate2 import AUTO_CANDIDATES, auto_errors, rule_threshold, series_sql, targets

MODEL_TEXT = {
    "last_value": "posledná známa hodnota (hodnota predchádzajúceho obdobia)",
    "mean": "priemer všetkých predchádzajúcich období",
    "seasonal_naive": "hodnota spred jednej sezóny (napr. rovnaký mesiac minulý rok)",
    "linear_trend": "priamka cez históriu (najmenšie štvorce)",
    "ar1": "AR(1): ďalšia hodnota = c + φ · posledná hodnota",
    "ses": "exponenciálne vyrovnávanie (úroveň bez trendu, váha α z mriežky podľa chýb v histórii)",
    "holt_damped": "Holtova metóda s tlmeným trendom",
    "seasonal_trend": "trend + sezónne efekty (potrebuje aspoň 2 sezóny, inak priamka)",
    "auto": "pred každou predpoveďou vyberie kandidáta s najmenšou chybou na posledných 3 bodoch histórie",
}


def _n(v: float | None, digits: int = 0) -> str:
    if v is None:
        return "–"
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def _err(pred: float, actual: float, metric: str) -> float:
    return abs(pred - actual) / abs(actual) * 100 if metric == "MAPE" and actual else abs(pred - actual)


def _judgment_before(db: DB, task: str, object_type: str, object_id: int, at: str):
    rows = [r for r in db.judgments_for(task, object_type, object_id) if r["created_at"] <= at]
    return rows[-1] if rows else None


def explain(cfg: Config, db: DB, exp) -> list[dict[str, Any]]:
    """Kroky: {"krok", "text" (markdown), voliteľne "tabuľka" (zoznam riadkov), "kód" (SQL)}."""
    hid = exp["hypothesis_id"]
    spec = json.loads(exp["spec_json"])
    flags = json.loads(exp["flags_json"] or "{}")
    res = json.loads(exp["result_json"] or "{}")
    steps: list[dict[str, Any]] = []

    # 1. výber organizácie
    cands = targets(cfg, db, hid)
    names = {e["org_id"]: e["id"] for e in db.experiments_for(hid)}
    if exp["org_id"]:
        rank = next((i + 1 for i, t in enumerate(cands) if t["org_id"] == exp["org_id"]), None)
        cov = next((t for t in cands if t["org_id"] == exp["org_id"]), None)
        text = (f"**{exp['org_name']}** (IČO {exp['ico']}) sa našla v stiahnutých dátach podľa IČO"
                + (f" v stĺpci `{cov['entity_column']}` tabuľky `{cov['table']}` v {_n(cov['rows'])} riadkoch" if cov else "")
                + f". Brána 2 vytvorí experiment pre najviac {cfg.gate2.max_targets_per_hypothesis} organizácií "
                  "s najviac riadkami v dátach; pri organizácii, ktorá je v dátach na viacerých stranách zmluvy, "
                  "rozhoduje jej vlastná strana (objednávateľ, prijímateľ), nie protistrana (dodávateľ)."
                + (f" Teraz je {rank}. v poradí." if rank else " Teraz už nie je medzi kandidátmi (pokrytie sa zmenilo)."))
    else:
        text = "Organizácia nie je v dátach podľa IČO – experiment je za celý dataset (kraj alebo celé Slovensko)."
    steps.append({"krok": "1. Prečo táto organizácia", "text": text, "tabuľka": [
        {"poradie": i + 1, "organizácia": t["org_name"], "stĺpec": t["entity_column"], "riadky": t["rows"],
         "experiment": f"E{names[t['org_id']]}" if t["org_id"] in names else "–"} for i, t in enumerate(cands)]})

    # 2. návrh predregistrácie
    j = _judgment_before(db, "gate2_prereg", "hypothesis", hid, exp["created_at"])
    who = (f"Navrhol model **{j['model']}** (verzia promptu {j['prompt_version']}, {j['created_at'][:16].replace('T', ' ')} UTC)"
           if j else "Návrh modelu sa v záznamoch volaní nenašiel")
    edits = []
    if flags.get("obdobie_upravené"):
        edits.append(f"zoskupenie zmenil kód – {flags['obdobie_upravené']}")
    if spec.get("entity_value"):
        edits.append(f"IČO `{spec['entity_value']}` a stĺpec organizácie doplnil kód (nie model)")
    if flags.get("chyby_návrhu"):
        edits.append("chyby návrhu: " + "; ".join(flags["chyby_návrhu"]))
    steps.append({"krok": "2. Návrh predregistrácie", "text": (
        f"{who}. Dostal iba: názov problému, organizáciu s IČO a stĺpcom, popis tabuliek (stĺpce, typy, 3 ukážkové "
        "riadky) a rozsah dát. Model vyberá stĺpce a postup, **čísla nepočíta**. Úpravy kódom: "
        + ("; ".join(edits) if edits else "žiadne") + ". Text výroku a pravidla mohol pred uzamknutím upraviť človek."),
        "tabuľka": [{"pole": k, "hodnota": str(v)} for k, v in (
            ("výrok", exp["statement"]), ("tabuľka", spec.get("table")), ("stĺpec času", spec.get("time_column")),
            ("stĺpec hodnoty", spec.get("value_column")), ("agregácia za obdobie", spec.get("aggregation")),
            ("obdobie", spec.get("period")), ("filtre", spec.get("filters") or "–"), ("model", spec.get("model")),
            ("naivný odhad", exp["baseline"]), ("metrika", exp["metric"]), ("pravidlo úspechu", exp["success_rule"]))]})

    # 3. uzamknutie
    steps.append({"krok": "3. Uzamknutie (predregistrácia)", "text": (
        f"Uzamknuté {exp['locked_at'][:16].replace('T', ' ')} UTC, odtlačok (SHA-256) `{(exp['prereg_hash'] or '')[:16]}…` "
        "z výroku, metriky, naivného odhadu, pravidla a postupu. Od tejto chvíle sa nedajú zmeniť, takže výsledok "
        "nemohol ovplyvniť, čo sa testuje." if exp["locked_at"] else
        "Ešte neuzamknuté – spätný test sa spustí až po uzamknutí.")})
    if not res:
        return steps

    # 4. dáta
    sql, params = series_sql(spec)
    ds = next((d for d in db.datasets_for(hid, only_ok=True) if d["table_name"] == base_table(spec.get("table", ""))), None)
    lim = res.get("rozsah_dát") or {}
    folds = res.get("folds", [])
    test_idx = {f["index"]: k + 1 for k, f in enumerate(folds)}
    data_text = ((f"Zdroj: {ds['url']} (stiahnuté {ds['fetched_at'][:10]}, {_n(ds['rows'])} riadkov). " if ds else "")
                 + (f"Dáta pokrývajú {lim.get('od') or 'od začiatku'} – {lim.get('do')}; použijú sa iba celé obdobia v tomto "
                    "rozsahu. " if lim else "")
                 + (f"Vyradené ako neúplné: {', '.join(res['vyradené_neúplné'])}. " if res.get("vyradené_neúplné") else "")
                 + (f"Obdobia bez riadkov (hodnota 0): {', '.join(res['doplnené_nuly'])}. " if res.get("doplnené_nuly") else "")
                 + f"Rad má {res['n_periods']} období; SQL zostavil kód z overených názvov stĺpcov.")
    steps.append({"krok": "4. Dáta pre test", "text": data_text,
                  "kód": sql + (f"\n-- parametre: {params}" if params else ""),
                  "tabuľka": [{"obdobie": p, "hodnota": _n(v, 2),
                               "úloha": f"test, kolo {test_idx[i]}" if i in test_idx else "iba história"}
                              for i, (p, v) in enumerate(zip(res["periods"], res["values"]))]})

    # 5. kolá spätného testu
    metric, values = res["metric"], np.asarray(res["values"], dtype=float)
    rows, auto_rows = [], []
    for k, f in enumerate(folds):
        i = f["index"]
        rows.append({"kolo": k + 1, "predpovedané obdobie": res["periods"][i],
                     "história": f"{res['periods'][0]} – {res['periods'][i - 1]} ({i} období)",
                     "model": f.get("vybraný_model", spec.get("model")), "predpoveď modelu": _n(f["model"], 2),
                     "naivný odhad": _n(f["baseline"], 2), "skutočnosť": _n(f["actual"], 2),
                     f"chyba modelu ({metric})": _n(_err(f["model"], f["actual"], metric), 2),
                     f"chyba naivného ({metric})": _n(_err(f["baseline"], f["actual"], metric), 2)})
        if spec.get("model") == "auto":
            errs = auto_errors(values[:i], res.get("season", 1))
            auto_rows.append({"kolo": k + 1, **{c: _n(errs.get(c), 0) for c in AUTO_CANDIDATES},
                              "vybraný": f.get("vybraný_model")})
    steps.append({"krok": "5. Spätný test po kolách (rolling origin)", "text": (
        f"Pre každé z posledných {res['n_folds']} období model aj naivný odhad vidia **iba hodnoty pred ním** "
        f"(najmenej {cfg.gate2.min_history} období histórie) a predpovedajú jedno obdobie dopredu; potom sa porovnajú so "
        f"skutočnosťou. Model: {MODEL_TEXT.get(spec.get('model'), spec.get('model'))}. Naivný odhad: "
        f"{MODEL_TEXT.get(exp['baseline'], exp['baseline'])}."), "tabuľka": rows})
    if auto_rows:
        steps.append({"krok": "5a. Prečo „auto“ vybral daný model", "text": (
            "Pred každým kolom sa každý kandidát vyskúša na posledných 3 bodoch histórie (predpoveď o jedno obdobie, "
            "testované obdobie sa nepoužije). V tabuľke je ich priemerná absolútna chyba; vyhrá najmenšia (pri zhode "
            "jednoduchší model)."), "tabuľka": auto_rows})

    # 6. vyhodnotenie
    threshold = rule_threshold(exp["success_rule"])
    lo, hi = res["diff_ci95"]
    verdict = ("Pravidlo nemá číselný prah – posúď sám." if threshold is None else
               f"Pravidlo (zlepšenie aspoň {_n(threshold, 0)} %): **{'splnené' if res['improvement'] * 100 >= threshold else 'nesplnené'}** "
               f"({_n(res['improvement'] * 100, 1)} %).")
    unit = "percentuálneho bodu" if metric == "MAPE" else "jednotky"
    steps.append({"krok": "6. Výpočet výsledku", "text": (
        ("MAPE kola = |predpoveď − skutočnosť| / skutočnosť × 100. " if metric == "MAPE" else
         "MAE kola = |predpoveď − skutočnosť|. ")
        + f"Priemer cez {res['n_folds']} kolá: model {_n(res['model_error'], 2)}, naivný odhad {_n(res['baseline_error'], 2)}. "
        f"Zlepšenie = 1 − model / naivný = **{_n(res['improvement'] * 100, 1)} %** (záporné = model je horší). "
        f"95 % interval rozdielu chýb (naivný − model, bootstrap {cfg.gate2.bootstrap}× z kôl): {_n(lo, 2)} až {_n(hi, 2)} "
        f"{unit}; ak obsahuje 0, rozdiel nie je preukázaný"
        + (f" – pri {res['n_folds']} kolách je interval iba orientačný" if res["n_folds"] < 4 else "") + f". {verdict}")})

    # 7. demo
    if exp["demo_path"]:
        dj = _judgment_before(db, "gate2_demo", "experiment", exp["id"], "9999")
        steps.append({"krok": "7. Demo", "text": (
            (f"Text napísal model **{dj['model']}** (verzia promptu {dj['prompt_version']}) " if dj else "Text napísal model ")
            + "iba z faktov F1–F6 (organizácia, počet kôl, obe chyby, zlepšenie, interval). Kód potom skontroluje, či text "
            "neobsahuje číslo mimo faktov – ak áno, text sa nepoužije. Graf a tabuľky dopĺňa kód.")})
    return steps


def as_markdown(exp, steps: list[dict[str, Any]]) -> str:
    """Postup ako Markdown na stiahnutie (napr. príloha k demu alebo do poznámok)."""
    out = [f"# Experiment E{exp['id']} – {exp['org_name'] or 'celý dataset'}", ""]
    for s in steps:
        out += [f"## {s['krok']}", "", s["text"], ""]
        if s.get("kód"):
            out += ["```sql", s["kód"], "```", ""]
        if s.get("tabuľka"):
            cols = list(s["tabuľka"][0])
            out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            out += ["| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in cols) + " |" for r in s["tabuľka"]]
            out.append("")
    return "\n".join(out)
