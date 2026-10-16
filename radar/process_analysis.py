"""Procesný report z event logu (dodatok 16. 10. 2026, časť 7.3): iba metriky, ktoré import podporil, s menovateľmi,
obmedzeniami a najviac tromi miestami na ďalšie preverenie.

- Výpočty sú deterministické (Python nad DuckDB organizácie, iba čítanie, bez externého prístupu). Model iba posúdi
  formulácie (úloha `process_report_review`, rola hodnotiteľa) a dostane len agregáty – nikdy surový log; jeho
  zlyhanie nechá report „čaká na hodnotiteľa“, deterministické výsledky ostávajú.
- Varianty a priamo nadväzujúce kroky iba z jednoznačne usporiadaných prípadov (pokrytie sa uvedie) – žiadne
  vymyslené prechody. Odstup je uplynutý kalendárny čas, nie automaticky čakanie ani práca; pracovný čas sa nepočíta
  bez dohodnutého kalendára. Trvanie: dokončené prípady zvlášť od otvorených a odrezaných. Čas aktivity iba
  z jednoznačne spárovaných start/complete. Percentily iba pri dostatočnom počte (p75 od 5, p90 od 10 hodnôt).
- Report navrhuje hypotézy na rozhovor alebo meranie, nie príčinu, finančnú stratu, úsporu ani chybu pracovníka.
- Voliteľný adaptér PM4Py (ak je nainštalovaný v overenej verzii) iba porovná priamo nadväzujúce kroky; jeho
  chýbanie nič neblokuje.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from .config import Config
from .db import DB
from .event_logs import METRICS, EventLogError, _connect

ANALYSIS_VERSION = "process-2"  # 16. 10. 2026: štart+complete = jeden výskyt, termín bez času do konca dňa
PM4PY_TESTED = ("2.7",)  # verzie, voči ktorým je adaptér overený (discover_dfg)
FORBIDDEN_RE = re.compile(r"úspor|garant|zaruč|chyb\w* pracovn|vin[ay]\b|strat[auy] \d|neefektívn\w* pracovn", re.I)
# Formulácie, ktoré report o sebe nesmie povedať (kontrolujú testy na pevných šablónach kandidátov).


def _pct(values: list[float]) -> dict[str, Any]:
    import numpy as np

    out: dict[str, Any] = {"n": len(values)}
    if not values:
        return out
    out["medián"] = round(float(median(values)), 2)
    if len(values) >= 5:
        out["p75"] = round(float(np.percentile(values, 75)), 2)
    if len(values) >= 10:
        out["p90"] = round(float(np.percentile(values, 90)), 2)
    return out


def _hours(a, b) -> float:
    return (b - a).total_seconds() / 3600


def pm4py_dfg(sequences: dict[str, list[str]]) -> tuple[dict[tuple[str, str], int] | None, str]:
    """Priamo nadväzujúce kroky cez PM4Py (iba overená verzia). Vráti (DFG alebo None, poznámka)."""
    try:
        import pm4py  # type: ignore[import-not-found]
    except ImportError:
        return None, "PM4Py nie je nainštalovaný – použitý vlastný deterministický výpočet"
    version = getattr(pm4py, "__version__", "?")
    if not any(version.startswith(v) for v in PM4PY_TESTED):
        return None, f"PM4Py {version} nie je overená verzia ({', '.join(PM4PY_TESTED)}) – nepoužitý"
    try:
        import pandas as pd

        rows = [{"case:concept:name": c, "concept:name": a, "time:timestamp": pd.Timestamp(2000, 1, 1) + pd.Timedelta(i, "s")}
                for c, acts in sequences.items() for i, a in enumerate(acts)]
        dfg, _, _ = pm4py.discover_dfg(pd.DataFrame(rows))
        return {tuple(k): int(v) for k, v in dfg.items()}, f"PM4Py {version}: discover_dfg"
    except Exception as exc:  # noqa: BLE001 – voliteľná závislosť nesmie zhodiť report
        return None, f"PM4Py zlyhal ({type(exc).__name__}) – použitý vlastný výpočet"


def compute(cfg: Config, db: DB, import_id: int) -> dict[str, Any]:
    """Podporované metriky importu (bez modelu). Vráti štruktúru reportu."""
    imp = db.get_eventlog_import(import_id)
    if imp is None or imp["status"] != "checked" or not imp["mapping_confirmed"]:
        raise EventLogError("Najprv potvrď mapovanie a nechaj skontrolovať kvalitu importu.")
    quality = json.loads(imp["quality_json"] or "{}")
    avail = json.loads(imp["availability_json"] or "{}")
    mapping = json.loads(imp["mapping_json"] or "{}")
    ok = {m for m, v in avail.items() if v["stav"] in ("áno", "čiastočne")}
    con = _connect(cfg, imp["organization_id"], read_only=True)
    try:
        con.execute("SET enable_external_access = false")
        ev = con.execute(f"SELECT case_id, activity, ts, sequence, _row, usable_time, lifecycle, pair_start_row, due_at "
                         f"FROM events_{import_id} WHERE usable_count").fetchall()
        cases = {r[0]: {"ordered": r[1], "open": r[2], "lt": r[3], "rt": r[4], "first": r[5], "last": r[6]}
                 for r in con.execute(f"SELECT case_id, ordered, open, left_truncated, right_truncated, first_ts, last_ts "
                                      f"FROM cases_{import_id}").fetchall()}
        ts_by_row = dict(con.execute(f"SELECT _row, ts FROM events_{import_id} WHERE usable_time").fetchall())
    finally:
        con.close()
    by_case: dict[str, list[tuple]] = defaultdict(list)
    for e in ev:
        by_case[e[0]].append(e)
    # výskyt aktivity: spárovaný štart a complete sú jeden výskyt (štart sa v sekvenciách a opakovaní nepočíta zvlášť,
    # inak by „Prepis → Prepis“ vyzeral ako opakovaný krok v každom prípade)
    paired_starts = {e[7] for e in ev if e[7]}
    inst_by_case: dict[str, list[tuple]] = {cid: [e for e in items if e[4] not in paired_starts]
                                            for cid, items in by_case.items()}
    n_cases = len(by_case)
    results: dict[str, Any] = {}
    denominators: dict[str, Any] = {"prípady": n_cases, "udalosti": len(ev)}
    # počty a frekvencie
    if "počty" in ok:
        complete = sum(1 for c in cases.values() if c["open"] is False and not c["lt"] and not c["rt"])
        open_ = sum(1 for c in cases.values() if c["open"] is True)
        trunc = sum(1 for c in cases.values() if c["lt"] or c["rt"])
        results["počty"] = {"prípady": n_cases, "udalosti": len(ev), "dokončené": complete, "otvorené": open_,
                            "možno_odrezané": trunc,
                            "bez_určených_hraníc": sum(1 for c in cases.values() if c["open"] is None)}
    if "frekvencie_aktivít" in ok:
        freq = Counter(e[1] for items in inst_by_case.values() for e in items)
        case_freq = Counter(a for items in by_case.values() for a in {e[1] for e in items})
        results["frekvencie_aktivít"] = [{"aktivita": a, "výskyty": n, "prípady": case_freq[a],
                                          "podiel_prípadov": round(case_freq[a] / max(1, n_cases), 4)}
                                         for a, n in freq.most_common()]
    # usporiadané prípady
    ordered = {cid: sorted(items, key=lambda e: (e[2], e[3] if e[3] is not None else float("inf")))
               for cid, items in by_case.items() if cases.get(cid, {}).get("ordered")}
    denominators["usporiadané_prípady"] = len(ordered)
    if paired_starts:
        denominators["spárované_štarty"] = len(paired_starts)  # štart+complete = jeden výskyt aktivity
    ordered_inst = {cid: [e for e in items if e[4] not in paired_starts] for cid, items in ordered.items()}
    sequences = {cid: [e[1] for e in items] for cid, items in ordered_inst.items()}
    if "varianty_a_nadväznosti" in ok and ordered:
        variants = Counter(tuple(s) for s in sequences.values())
        results["varianty"] = [{"priebeh": " → ".join(v), "prípady": n, "podiel": round(n / len(ordered), 4)}
                               for v, n in variants.most_common(10)]
        results["počet_variantov"] = len(variants)
        dfg: Counter = Counter()
        waits: dict[tuple[str, str], list[float]] = defaultdict(list)
        for cid, items in ordered_inst.items():
            for a, b in zip(items, items[1:]):
                dfg[(a[1], b[1])] += 1
                waits[(a[1], b[1])].append(_hours(a[2], b[2]))
        results["nadväznosti"] = [{"z": a, "do": b, "prípady_prechodov": n,
                                   **({"odstup_h": _pct(waits[(a, b)])} if "odstupy" in ok else {})}
                                  for (a, b), n in dfg.most_common()]
        pm, note = pm4py_dfg(sequences)
        results["pm4py"] = {"poznámka": note, "zhoda_s_vlastným_výpočtom": (pm == dict(dfg)) if pm is not None else None}
    # trvanie prípadov
    if "trvanie_prípadu" in ok and ordered:
        groups: dict[str, list[float]] = defaultdict(list)
        for cid, items in ordered.items():
            c = cases[cid]
            d = _hours(items[0][2], items[-1][2]) / 24
            key = ("dokončené" if c["open"] is False and not c["lt"] and not c["rt"] else
                   "otvorené alebo odrezané" if c["open"] or c["lt"] or c["rt"] else "hranice neurčené")
            groups[key].append(d)
        results["trvanie_dni"] = {k: _pct(v) for k, v in groups.items()}
    # opakovanie kroku
    if "opakovanie_kroku" in ok:
        rep = Counter()
        for items in inst_by_case.values():
            counts = Counter(e[1] for e in items)
            for a, n in counts.items():
                if n > 1:
                    rep[a] += 1
        results["opakovanie"] = [{"aktivita": a, "prípady_s_opakovaním": n, "podiel_prípadov": round(n / max(1, n_cases), 4)}
                                 for a, n in rep.most_common()]
    # čas aktivity zo spárovaných start/complete
    if "čas_aktivity" in ok:
        dur: dict[str, list[float]] = defaultdict(list)
        for e in ev:
            if e[7] and e[5] and e[7] in ts_by_row and e[2] is not None:
                dur[e[1]].append(_hours(ts_by_row[e[7]], e[2]))
        results["čas_aktivity_h"] = {a: _pct(v) for a, v in dur.items()}
    # odchýlka od termínu
    if "odchýlka_od_termínu" in ok and ordered:
        dev = []
        for cid, items in ordered.items():
            due = next((e[8] for e in items if e[8] is not None), None)
            if due is not None and cases[cid]["open"] is False:
                dev.append(_hours(due, items[-1][2]) / 24)
        if dev:
            results["odchýlka_od_termínu_dni"] = {**_pct(dev), "neskoro": sum(1 for x in dev if x > 0),
                                                  "termín": mapping.get("termín") or "neznámy význam"}
    limitations = [f"{METRICS[m]}: {v['dôvod']}" for m, v in avail.items() if v["stav"] != "áno"]
    limitations += quality.get("upozornenia") or []
    limitations += ["Odstupy a trvania sú kalendárny čas (nie pracovný; rozdiel piatok–pondelok nie je automaticky "
                    "odstrániteľné zdržanie).", "Varianty a nadväznosti sú pozorované sekvencie, nie príčinné vzťahy.",
                    "Report nedokazuje príčinu, finančnú stratu ani úsporu dosiahnuteľnú novým nástrojom."]
    return {"výsledky": results, "menovatele": denominators, "obmedzenia": limitations,
            "podporované": sorted(ok), "nepodporované": {m: v["dôvod"] for m, v in avail.items() if m not in ok},
            "kandidáti": followups(cfg, results),
            "pôvod": {"import": import_id, "súbor": imp["file_name"], "sha256": imp["file_hash"],
                      "verzia_mapovania": imp["mapping_version"], "verzia_analýzy": ANALYSIS_VERSION,
                      "verzia_parsera": quality.get("verzia_parsera"), "metóda": "deterministický výpočet nad DuckDB "
                      "(iba jednoznačne usporiadané prípady pre sekvencie a časy)"}}


def followups(cfg: Config, results: dict[str, Any]) -> list[dict[str, str]]:
    """Najviac `event_logs.max_followups` miest na ďalšie preverenie – ako otázky na rozhovor alebo meranie,
    nie zistené príčiny, úspory či chyby ľudí."""
    out: list[dict[str, str]] = []
    pairs = [p for p in results.get("nadväznosti") or [] if (p.get("odstup_h") or {}).get("n", 0) >= 5]
    if pairs:
        p = max(pairs, key=lambda x: x["odstup_h"]["medián"])
        out.append({"miesto": f"odstup {p['z']} → {p['do']}",
                    "zistenie": f"medián {p['odstup_h']['medián']} h (n = {p['odstup_h']['n']}, kalendárny čas)",
                    "preveriť": "Ide o čakanie, prácu alebo dohodnutú lehotu? Otázka do rozhovoru, nie zistená príčina."})
    reps = [r for r in results.get("opakovanie") or [] if r["podiel_prípadov"] >= 0.1]
    if reps:
        r = reps[0]
        out.append({"miesto": f"opakovanie kroku {r['aktivita']}",
                    "zistenie": f"v {r['prípady_s_opakovaním']} prípadoch ({r['podiel_prípadov']:.0%})",
                    "preveriť": "Oprava, doplnenie alebo legitímny cyklus? Kandidát na meranie pri skúške."})
    counts = results.get("počty") or {}
    if counts.get("prípady") and counts.get("otvorené", 0) / counts["prípady"] >= 0.2:
        out.append({"miesto": "otvorené prípady",
                    "zistenie": f"{counts['otvorené']} z {counts['prípady']} prípadov nemá koncovú udalosť",
                    "preveriť": "Sú stále rozpracované, alebo sa koniec neeviduje? Ovplyvňuje to trvania."})
    return out[: cfg.event_logs.max_followups]


def review_report(cfg: Config, router, report: dict[str, Any], import_id: int) -> dict[str, Any]:
    """Hodnotiteľ posúdi formulácie (iba agregáty, žiadny surový log). Zlyhanie = čaká; nikdy automatické „ok“."""
    from .prompts import PROCESS_REPORT_REVIEW_SYSTEM, language_name
    from .value import ReviewOut

    summary = {"podporované": report["podporované"], "kandidáti": report["kandidáti"],
               "obmedzenia": report["obmedzenia"][:12], "menovatele": report["menovatele"]}
    res = router.run_task("process_report_review", [
        {"role": "system", "content": PROCESS_REPORT_REVIEW_SYSTEM.format(lang=language_name(cfg.pipeline.output_language))},
        {"role": "user", "content": f"REPORT SUMMARY:\n<untrusted_document>\n{json.dumps(summary, ensure_ascii=False)}"
                                    "\n</untrusted_document>"}], ReviewOut, object_type="eventlog_import",
        object_id=import_id, max_tokens=1500)
    if not res.ok:
        return {"verdict": "pending", "dôvod": res.reason, "problems": []}
    return {"verdict": res.value.verdict, "problems": [p.model_dump() for p in res.value.problems]}


def analyze(cfg: Config, db: DB, import_id: int, router=None) -> int:
    """Vypočíta report a uloží ho; s routerom aj posúdenie formulácií hodnotiteľom."""
    report = compute(cfg, db, import_id)
    review = review_report(cfg, router, report, import_id) if router is not None else {"verdict": "pending",
                                                                                       "dôvod": "nepožiadané"}
    status = {"pending": "pending_review", "ok": "ok"}.get(review["verdict"], "needs_review")
    imp = db.get_eventlog_import(import_id)
    return db.add_process_report(
        import_id, analysis_version=ANALYSIS_VERSION, mapping_version=imp["mapping_version"],
        supported_json={"podporované": report["podporované"], "nepodporované": report["nepodporované"]},
        results_json=report["výsledky"], denominators_json=report["menovatele"], limitations_json=report["obmedzenia"],
        followups_json={"kandidáti": report["kandidáti"], "pôvod": report["pôvod"]}, review_json=review, status=status)


def rereview(cfg: Config, db: DB, report_id: int, router) -> str:
    """Posúdenie existujúceho reportu (napr. po výpadku cloudu)."""
    row = db.get_process_report(report_id)
    report = {"podporované": json.loads(row["supported_json"])["podporované"],
              "kandidáti": json.loads(row["followups_json"])["kandidáti"],
              "obmedzenia": json.loads(row["limitations_json"]), "menovatele": json.loads(row["denominators_json"])}
    review = review_report(cfg, router, report, row["import_id"])
    status = {"pending": "pending_review", "ok": "ok"}.get(review["verdict"], "needs_review")
    db.update_process_report(report_id, review_json=review, status=status)
    return status


def report_markdown(row, imp) -> str:
    """Report ako Markdown: pôvod, podporované metriky, výsledky s menovateľmi, obmedzenia, kandidáti."""
    res = json.loads(row["results_json"])
    sup = json.loads(row["supported_json"])
    fol = json.loads(row["followups_json"])
    lines = [f"# Procesný report – {imp['org_name']} ({imp['file_name']})", "",
             f"Pôvod: import #{imp['id']}, SHA-256 {imp['file_hash'][:16]}…, mapovanie v{row['mapping_version']}, "
             f"analýza {row['analysis_version']}, stav {row['status']}.", "",
             "## Podporované metriky", ", ".join(METRICS.get(m, m) for m in sup["podporované"]) or "žiadne"]
    if sup["nepodporované"]:
        lines += ["", "## Nepodporované"] + [f"- {METRICS.get(m, m)}: {r}" for m, r in sup["nepodporované"].items()]
    if res.get("počty"):
        lines += ["", "## Počty", json.dumps(res["počty"], ensure_ascii=False)]
    if res.get("varianty"):
        lines += ["", f"## Najčastejšie priebehy (z {json.loads(row['denominators_json'])['usporiadané_prípady']} "
                      "usporiadaných prípadov)"]
        lines += [f"- {v['priebeh']} – {v['prípady']} ({v['podiel']:.0%})" for v in res["varianty"]]
    if res.get("frekvencie_aktivít"):
        lines += ["", "## Frekvencie aktivít (výskyty; štart a complete jednej aktivity = 1)"]
        lines += [f"- {f['aktivita']}: {f.get('výskyty', f.get('udalosti'))} výskytov v {f['prípady']} prípadoch "
                  f"({f['podiel_prípadov']:.0%})" for f in res["frekvencie_aktivít"][:12]]
    if res.get("trvanie_dni"):
        lines += ["", "## Trvanie prípadov (dni, kalendárny čas)"]
        lines += [f"- {k}: {v}" for k, v in res["trvanie_dni"].items()]
    if res.get("opakovanie"):
        lines += ["", "## Opakovanie kroku v tom istom prípade (kandidát na opravu alebo legitímny cyklus)"]
        lines += [f"- {r['aktivita']}: {r['prípady_s_opakovaním']} prípadov ({r['podiel_prípadov']:.0%})"
                  for r in res["opakovanie"][:10]]
    if res.get("čas_aktivity_h"):
        lines += ["", "## Čas výkonu aktivity (hodiny, spárovaný štart → complete)"]
        lines += [f"- {a}: {v}" for a, v in res["čas_aktivity_h"].items()]
    if res.get("odchýlka_od_termínu_dni"):
        d = res["odchýlka_od_termínu_dni"]
        lines += ["", "## Odchýlka od termínu (dni; kladná = po termíne, uzavreté prípady)",
                  f"- termín: {d.get('termín')}; po termíne {d.get('neskoro')} z {d.get('n')}; "
                  + json.dumps({k: d[k] for k in ('medián', 'p75', 'p90') if k in d}, ensure_ascii=False)]
    lines += ["", "## Obmedzenia"] + [f"- {x}" for x in json.loads(row["limitations_json"])]
    lines += ["", "## Na ďalšie preverenie (najviac 3)"]
    lines += [f"- **{f['miesto']}**: {f['zistenie']} – {f['preveriť']}" for f in fol["kandidáti"]] or ["- nič"]
    return "\n".join(lines) + "\n"
