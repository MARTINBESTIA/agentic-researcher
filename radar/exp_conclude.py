"""Záver experimentu Brány 2 pre problém z karty: fakty skladá kód z výsledku, model z nich napíše text (čo sa
testovalo, čo to znamená pre rozhodnutie, robustnosť, obmedzenia, otázky do rozhovorov). Číslo mimo faktov →
text modelu sa zahodí a ostane záver poskladaný kódom. Správa experimentu (HTML) nahrádza demo.

Pomocníci pre test: zatriedenie textov embeddingmi a kontrola vzorky zatriedenia modelom.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import numpy as np
from pydantic import BaseModel, Field

from .cards import FIELD_LABELS
from .config import Config
from .db import DB
from .exp_ops import concentration_text
from .exp_plan import TASK_LABELS
from .models import as_router
from .prompts import CATEGORY_CHECK_SYSTEM, EXPERIMENT_CONCLUSION_SYSTEM
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
THOUSANDS_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?![\d])")  # „75 726“ = 75726


class Conclusion(BaseModel):
    what_tested: str = ""
    result_for_decision: str = ""
    robustness: str = ""
    limits: str = ""
    interview_questions: list[str] = Field(default_factory=list)


class CategoryChecks(BaseModel):
    class Item(BaseModel):
        i: int
        ok: bool

    checks: list[Item] = Field(default_factory=list)


def _n(v: Any, d: int = 2) -> str:
    if v is None:
        return "–"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return f"{float(v):.{d}f}".rstrip("0").rstrip(".").replace(".", ",") if isinstance(v, float) else str(v)


def facts(card: dict[str, Any] | None, exp, res: dict[str, Any]) -> list[str]:
    plan = json.loads(exp["spec_json"])
    prim = res.get("hlavné") or {}
    det = res.get("detaily") or {}
    out = []
    if card:
        out.append(f"F1: Rozhodnutie: {(card.get('decision') or {}).get('text', '')} (kto: {(card.get('who') or {}).get('text', '')}).")
    out.append(f"F2: Úloha: {TASK_LABELS.get(plan['task'], plan['task'])}; dáta {plan['table']}, rozsah "
               f"{(res.get('rozsah_dát') or {}).get('od')} – {(res.get('rozsah_dát') or {}).get('do')}.")
    if plan["task"] == "ranking":
        out.append(f"F3: Test na obdobie {det.get('test')}: {det.get('test_riadky')} položiek, z toho {det.get('test_pozitívne')} "
                   f"so štítkom {plan.get('label')} (podiel {_n(det.get('podiel_pozitívnych_test'), 4)}); k = {det.get('k')}.")
        if prim.get("model_hodnota") is not None and det.get("k"):
            k = det["k"]
            out.append(f"F4: Z prvých {k} položiek podľa modelu by malo štítok približne {round(prim['model_hodnota'] * k)} "
                       f"(pri hlavnej metrike precision@k), pri naivnom odhade {prim['naivný']} približne "
                       f"{round((prim.get('naivný_hodnota') or 0) * k)}." if prim["metrika"] == "precision@k" else
                       f"F4: {prim['metrika']} modelu {_n(prim['model_hodnota'], 4)}, naivného odhadu {prim['naivný']} "
                       f"{_n(prim.get('naivný_hodnota'), 4)}.")
        imp = det.get("dôležitosť_príznakov") or {}
        if imp:
            top = sorted(imp.items(), key=lambda kv: -kv[1])[:3]
            out.append("F5: Najdôležitejšie príznaky: " + ", ".join(k for k, _ in top) + ".")
    elif plan["task"] in ("forecast", "anomaly"):
        out.append(f"F3: {det.get('entity')} entít, testovacie obdobia {', '.join(det.get('testovacie_obdobia') or [])}.")
        if plan["task"] == "anomaly":
            out.append(f"F4: Označených položiek na kontrolu: {len(res.get('položky') or [])}.")
    elif plan["task"] == "threshold":
        out.append(f"F3: Hodnôt: {det.get('hodnôt')}; okno pod prahom {_n(det.get('okno_okolo_prahu_pct'), 0)} % prahu.")
    elif plan["task"] == "comparison":
        out.append(f"F3: Riadkov {det.get('riadky')}, kategórií s aspoň 20 riadkami {det.get('kategórie_s_20')}.")
    if prim.get("metrika"):
        out.append(f"F6: Hlavná metrika {prim['metrika']}: model {prim.get('model')} {_n(prim.get('model_hodnota'), 4)}, "
                   f"naivný odhad {prim.get('naivný')} {_n(prim.get('naivný_hodnota'), 4)}, zlepšenie "
                   f"{_n(prim.get('zlepšenie_pct'), 1)} % (prah {_n(prim.get('prah_pct'), 0)} %); pravidlo úspechu "
                   f"{'splnené' if prim.get('splnené') else 'nesplnené'}.")
        if prim.get("ci95_rozdielu"):
            lo, hi = prim["ci95_rozdielu"]
            out.append(f"F7: 95 % interval rozdielu model − naivný: {_n(lo, 4)} až {_n(hi, 4)}.")
    else:
        out.append("F6: Porovnanie je popisné (bez štítku sa úspech nevyhodnocuje).")
    from .exp_tasks import strongest_baseline

    strong = strongest_baseline(res)
    if strong and not strong["je_hlavný"]:
        out.append(f"F9: Najsilnejší naivný odhad nie je predregistrovaný: {strong['naivný']} {_n(strong['hodnota'], 4)}; "
                   f"proti nemu je zlepšenie modelu {_n(strong['zlepšenie_pct'], 1)} % (predregistrované pravidlo sa "
                   "vyhodnocuje proti hlavnému naivnému odhadu).")
    conc = det.get("koncentrácia_štítku") or {}
    if conc.get("sústredený"):
        out.append("F10: " + concentration_text(conc, plan.get("entity_column") or ""))
    others = [r for r in res.get("tabuľka") or [] if r["postup"] != prim.get("model") and prim.get("metrika") in r]
    if others:
        out.append("F8: Ďalšie postupy (" + str(prim.get("metrika")) + "): "
                   + ", ".join(f"{r['postup']} {_n(r.get(prim['metrika']), 4)}" for r in others) + ".")
    return out


def _numbers(text: str) -> set[str]:
    """Čísla v texte; oddeľovač tisícov medzerou sa zlúči (model píše „75 726“, fakt „75726“ – E6 sa inak zamietol)."""
    text = THOUSANDS_RE.sub(lambda m: re.sub(r"\s", "", m.group()), text or "")
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n
            for n in NUM_RE.findall(text)}


def code_conclusion(card: dict[str, Any] | None, exp, res: dict[str, Any], fact_list: list[str]) -> dict[str, Any]:
    """Záver bez modelu (vždy k dispozícii): fakty v krátkej forme."""
    prim = res.get("hlavné") or {}
    return {"what_tested": fact_list[1][4:] if len(fact_list) > 1 else "",
            "result_for_decision": next((f[4:] for f in fact_list if f.startswith(("F4", "F6"))), ""),
            "robustness": " ".join(f[4:] for f in fact_list if f.startswith(("F7", "F9"))) or "interval nie je k dispozícii",
            "limits": " ".join(["Štítok je iba náhradný ukazovateľ problému; výsledok platí pre testované obdobie a tieto "
                                "dáta."] + [f[5:] for f in fact_list if f.startswith("F10: ")]),
            "interview_questions": [], "splnené": prim.get("splnené")}


def conclude(cfg: Config, db: DB, llm: Any, exp) -> dict[str, Any]:
    res = json.loads(exp["result_json"] or "{}")
    card_row = db.get_card(exp["card_id"]) if exp["card_id"] else db.latest_card(exp["hypothesis_id"])
    card = json.loads(card_row["card_json"]) if card_row else None
    fact_list = facts(card, exp, res)
    out = {"fakty": fact_list, "zdroj": "kód", "text": code_conclusion(card, exp, res, fact_list), "čas": utcnow()}
    router = as_router(llm, cfg, db)
    card_lines = "\n".join(f"- {label}: {(card.get(k) or {}).get('text', '')}" for k, label in FIELD_LABELS.items()) if card else ""
    r = router.run_task(
        "experiment_conclusion",
        [{"role": "system", "content": EXPERIMENT_CONCLUSION_SYSTEM.format(lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"PROBLEM CARD:\n{card_lines}\n\nEXPERIMENT: {exp['statement']}\nLINK TO DECISION: "
                                     f"{exp['problem_link'] or ''}\n\nFACTS:\n" + "\n".join(fact_list)}],
        Conclusion, object_type="experiment", object_id=exp["id"], max_tokens=1200)
    if r.ok:
        allowed = _numbers(re.sub(r"\bF\d+:", "", "\n".join(fact_list) + card_lines + (exp["statement"] or "")))
        text = r.value.model_dump()

        def invented(t: str) -> set[str]:
            return {x for x in _numbers(re.sub(r"\bF\d+\b", "", t)) - allowed if not (x.isdigit() and int(x) <= 12)}

        extra = invented(" ".join([text["what_tested"], text["result_for_decision"], text["robustness"], text["limits"]]))
        questions = [q for q in text["interview_questions"] if not invented(q)]
        dropped = len(text["interview_questions"]) - len(questions)
        if extra:
            out["poznámka"] = "Text modelu obsahoval čísla mimo faktov (" + ", ".join(sorted(extra)[:5]) + ") – použitý záver z kódu."
        else:  # vymyslené číslo iba v otázke do rozhovoru: vypadne len tá otázka (E6: „o 200 položiek viac“)
            out |= {"text": text | {"interview_questions": questions, "splnené": (res.get("hlavné") or {}).get("splnené")},
                    "zdroj": "model", "model": r.model}
            if dropped:
                out["poznámka"] = f"Vynechané otázky s číslom mimo faktov: {dropped}."
    return out


def report_html(exp, res: dict[str, Any], concl: dict[str, Any], hyp_title: str) -> str:
    esc = html.escape
    t = concl.get("text") or {}
    rows = res.get("tabuľka") or []
    metrics = [m for m in res.get("metriky") or [] if any(m in r for r in rows)]
    table = "<tr><th>postup</th><th>typ</th>" + "".join(f"<th>{esc(m)}</th>" for m in metrics) + "</tr>" + "".join(
        f"<tr><td>{esc(str(r['postup']))}</td><td>{esc(str(r.get('typ', '')))}</td>"
        + "".join(f"<td>{esc(_n(r.get(m), 4))}</td>" for m in metrics) + "</tr>" for r in rows)
    items = res.get("položky") or []
    cols = list(items[0]) if items else []
    item_tbl = ("<tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td>{esc(str(i.get(c, '')))[:120]}</td>" for c in cols) + "</tr>" for i in items)) if items else ""
    qs = "".join(f"<li>{esc(x)}</li>" for x in t.get("interview_questions") or [])
    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>E{exp['id']} – {esc(hyp_title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1e293b}}
table{{border-collapse:collapse;font-size:13px;margin:8px 0}}td,th{{border:1px solid #cbd5e1;padding:4px 8px;text-align:left}}
.ok{{color:#15803d}}.no{{color:#b91c1c}}</style></head><body>
<h1>{esc(exp['statement'] or '')}</h1>
<p><b>Problém a rozhodnutie:</b> {esc(exp['problem_link'] or '')}</p>
<p><b>Čo sa testovalo:</b> {esc(t.get('what_tested', ''))}</p>
<p><b>Výsledok pre rozhodnutie:</b> {esc(t.get('result_for_decision', ''))}
<span class="{'ok' if t.get('splnené') else 'no'}">({'pravidlo úspechu splnené' if t.get('splnené') else 'pravidlo úspechu nesplnené'})</span></p>
<p><b>Robustnosť:</b> {esc(t.get('robustness', ''))}</p><p><b>Obmedzenia:</b> {esc(t.get('limits', ''))}</p>
{f'<p><b>Otázky do rozhovorov:</b></p><ul>{qs}</ul>' if qs else ''}
<h2>Výsledková tabuľka</h2><table>{table}</table>
{f"<h2>{esc(res.get('položky_popis', 'Položky'))}</h2><table>{item_tbl}</table>" if item_tbl else ''}
<p><small>Predregistrované {esc(exp['locked_at'] or '')} · hash {esc((exp['prereg_hash'] or '')[:16])} · záver: {esc(concl.get('zdroj', ''))}</small></p>
</body></html>"""


def run_conclusions(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    hyps = [h for h in (db.get_hypothesis(i) for i in ids) if h] if ids else list(db.gate2_hypotheses())
    for hyp in hyps:
        for exp in db.experiments_for(hyp["id"]):
            if deadline.expired():
                return dict(stats)
            if not exp["task"] or exp["status"] != "done" or exp["conclusion_json"]:
                continue
            concl = conclude(cfg, db, llm, exp)
            path = Path(cfg.path(cfg.gate2.demos_dir)) / f"H{hyp['id']}_E{exp['id']}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report_html(exp, json.loads(exp["result_json"]), concl, hyp["title"]), encoding="utf-8")
            db.update_experiment(exp["id"], conclusion_json=concl, demo_path=str(path))
            stats["conclusions_" + concl["zdroj"]] += 1
    return dict(stats)


# ----------------------------------------------------------------------- zatriedenie textu (pre exp_ops)
def make_categorizer(embedder) -> Callable[[list[str], list[str]], list[tuple[str, float]]] | None:
    """Zatriedenie embeddingmi: kategória s najväčšou kosínusovou podobnosťou (vektory textov sa ukladajú do cache)."""
    if embedder is None:
        return None

    def run(texts: list[str], cats: list[str]) -> list[tuple[str, float]]:
        cvec = embedder.embed_texts(cats)
        items = [(int(hashlib.sha1(t.encode("utf-8")).hexdigest()[:12], 16), t[:500]) for t in texts]
        tvec = embedder.vectors("exp_text", items)
        sims = tvec @ cvec.T
        best = sims.argmax(axis=1)
        return [(cats[b], float(sims[i, b])) for i, b in enumerate(best)]

    return run


def make_category_checker(cfg: Config, db: DB, llm: Any, exp_id: int) -> Callable:
    router = as_router(llm, cfg, db)

    def run(pairs: list[tuple[str, str]], cats: list[str]) -> float | None:
        listing = "\n".join(f"{i + 1}. [{c}] {t[:200]}" for i, (t, c) in enumerate(pairs))
        r = router.run_task("category_check",
                            [{"role": "system", "content": CATEGORY_CHECK_SYSTEM},
                             {"role": "user", "content": f"CATEGORIES: {', '.join(cats)}\n\n<untrusted_document>\n{listing}\n"
                                                         "</untrusted_document>"}],
                            CategoryChecks, object_type="experiment", object_id=exp_id, max_tokens=1200)
        if not r.ok or not r.value.checks:
            return None
        return sum(c.ok for c in r.value.checks) / len(r.value.checks)

    return run
