"""Eval harness (R-ACC-7): porovnanie profilov modelov na položkách s gold labelom.

Profily (config models.eval_profiles): A fast bez uvažovania, B fast s uvažovaním, C accurate bez
uvažovania, D accurate s uvažovaním. Metriky: presnosť, úplnosť a F1 pri binárnych úlohách,
inak podiel správnych; 95 % Wilsonov interval (pri malých vzorkách je rozdiel profilov často
v rámci šumu); priemerný čas a tokeny na položku. Pred záverom treba aspoň 50 labelov na úlohu.

Labely: (1) z kontroly v GUI, (2) zo stratifikovanej vzorky model_judgments (eval --sample),
(3) zo syntetických prípadov s nastrčenou chybou (eval --synthetic).
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config, TaskRoute
from .db import DB
from .models import ModelRouter
from .verify import Entailment, entail_messages, quote_score

log = logging.getLogger(__name__)

MIN_LABELS = 50
EVAL_TASKS = ("entail", "extract", "cluster_assign", "role_fit", "contact_assoc", "draft_check")
# Pozitívna trieda pri binárnom pohľade (čo chceme zachytiť).
POSITIVE = {"entail": "unsupported", "draft_check": "problems", "extract": "correct", "contact_assoc": "correct",
            "cluster_assign": "correct"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilsonov interval pre podiel k/n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def binary_metrics(pairs: list[tuple[str, str]], positive: str) -> dict[str, Any]:
    """Presnosť (precision), úplnosť (recall) a F1 pre pozitívnu triedu, s Wilsonovými intervalmi."""
    tp = sum(1 for g, p in pairs if p == positive and g == positive)
    fp = sum(1 for g, p in pairs if p == positive and g != positive)
    fn = sum(1 for g, p in pairs if p != positive and g == positive)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "positive": positive, "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "precision_ci": wilson(tp, tp + fp),
        "recall": recall, "recall_ci": wilson(tp, tp + fn), "f1": f1,
    }


def compute_metrics(pairs: list[tuple[str, str]], positive: str | None) -> dict[str, Any]:
    n = len(pairs)
    correct = sum(1 for g, p in pairs if g == p)
    out: dict[str, Any] = {"n": n, "accuracy": correct / n if n else 0.0, "accuracy_ci": wilson(correct, n)}
    if positive is not None:
        out["binary"] = binary_metrics(pairs, positive)
    return out


@dataclass
class EvalItem:
    object_type: str
    object_id: int
    gold: str
    payload: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ adaptéry úloh
def entail_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    if item.object_type == "eval_case":
        claim, context = item.payload["claim"], item.payload["context"]
    else:
        claim = db.get_claim(item.object_id)
        context = db.get_document(claim["document_id"])["text"]
    res = router.run_task("entail", entail_messages(cfg, claim, context), Entailment, max_tokens=400,
                          object_type=item.object_type, object_id=item.object_id)
    return res.value.verdict if res.ok else "undecided"


def draft_check_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    from .outreach import model_check

    row = db.get_outreach(item.object_id)
    if row is None:
        return "undecided"
    result = model_check(cfg, router, row["body"], json.loads(row["facts_json"] or "[]"), None)
    return result["verdict"]


def role_fit_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    from .people import RoleFit
    from .prompts import ROLE_FIT_SYSTEM

    person = db.get_person(item.object_id)
    if person is None:
        return "undecided"
    rm = db.get_role_map(person["collected_for_hypothesis_id"]) or {"roles": []}
    listing = "\n".join(f"- {r['category']}: {', '.join(r.get('examples', []))}" for r in rm["roles"])
    res = router.run_task("role_fit", [
        {"role": "system", "content": ROLE_FIT_SYSTEM.format(lang=cfg.pipeline.output_language)},
        {"role": "user", "content": f"JOB TITLE: {person['role_title']}\nROLE CATEGORIES:\n{listing}"},
    ], RoleFit, max_tokens=300)
    return res.value.fit if res.ok else "undecided"


def extract_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    """Vytiahne profil to isté tvrdenie z tej istej časti dokumentu? (citát sa zhoduje aspoň na 90 %)"""
    from .extract import ExtractionResult, extraction_messages
    from .verify import claim_context

    claim = db.get_claim(item.object_id)
    doc = db.get_document(claim["document_id"])
    chunk = claim_context(cfg, doc["text"], claim["chunk_index"])
    res = router.run_task("extract", extraction_messages(cfg, claim["topic"], doc["url"], chunk), ExtractionResult)
    if not res.ok:
        return "undecided"
    found = any(quote_score(c.quote, claim["quote"]) >= 90 for c in res.value.claims)
    return "correct" if found else "incorrect"


def contact_assoc_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    """Priradí profil kontakt z úryvku tej istej osobe? (úryvok je to, čo sa smie uložiť)"""
    from .contacts import emails_in, phones_in
    from .people import ContactAssoc
    from .prompts import CONTACT_ASSOC_SYSTEM

    if item.object_type == "eval_case":
        snippet, person_name, value = item.payload["snippet"], item.payload["person"], item.payload["value"]
    else:
        c = db.get_contact(item.object_id)
        person = db.get_person(c["person_id"]) if c and c["person_id"] else None
        if c is None or person is None:
            return "undecided"
        snippet, person_name, value = c["source_snippet"], person["full_name"], c["value"]
    listing = (f"[BLOCK 0]\n{snippet}\nDETECTED E-MAILS: {', '.join(e for e, _, _ in emails_in(snippet)) or '-'}\n"
               f"DETECTED PHONES: {', '.join(p for p, _, _ in phones_in(snippet)) or '-'}")
    res = router.run_task("contact_assoc", [
        {"role": "system", "content": CONTACT_ASSOC_SYSTEM},
        {"role": "user", "content": f"<untrusted_document>\n{listing}\n</untrusted_document>"},
    ], ContactAssoc, max_tokens=800)
    if not res.ok:
        return "undecided"
    from .names import person_name_key
    from .people import _phone_from

    for p in res.value.persons:
        if person_name_key(p.full_name) == person_name_key(person_name):
            assigned = {p.email.strip().lower(), _phone_from(p.phone, [value]) or ""}
            return "correct" if value in assigned else "incorrect"
    return "incorrect"


def cluster_predict(cfg: Config, db: DB, router: ModelRouter, item: EvalItem) -> str:
    from .cluster import Assignment, text_candidates
    from .prompts import CLUSTER_SYSTEM

    claim = db.get_claim(item.object_id)
    current = db.hypothesis_of_claim(item.object_id)
    hyps = db.assignable_hypotheses()
    cands = text_candidates(claim["problem"], hyps)
    listing = "\n".join(f"- id={h['id']}: {h['title']} (segment: {h['segment'] or '?'})" for h in cands)
    res = router.run_task("cluster_assign", [
        {"role": "system", "content": CLUSTER_SYSTEM.format(lang=cfg.pipeline.output_language)},
        {"role": "user", "content": f"NEW CLAIM: {claim['problem']}\nWHO: {claim['affected_party']}\n\nCANDIDATE HYPOTHESES:\n{listing}"},
    ], Assignment, max_tokens=600)
    if not res.ok:
        return "undecided"
    same = res.value.decision == "existing" and res.value.hypothesis_id == current
    return "correct" if same else "incorrect"


PREDICTORS: dict[str, Callable[[Config, DB, ModelRouter, EvalItem], str]] = {
    "entail": entail_predict, "draft_check": draft_check_predict, "role_fit": role_fit_predict,
    "extract": extract_predict, "contact_assoc": contact_assoc_predict, "cluster_assign": cluster_predict,
}


def eval_items(db: DB, task: str, limit: int | None = None) -> list[EvalItem]:
    items = []
    for label in db.labels_for(task):
        if label["label"] == "unsure":
            continue
        payload = {}
        if label["object_type"] == "eval_case":
            case = db.get_eval_case(label["object_id"])
            if case is None:
                continue
            payload = json.loads(case["payload_json"])
        items.append(EvalItem(label["object_type"], int(label["object_id"]), label["label"], payload))
    return items[:limit] if limit else items


# ------------------------------------------------------------------ syntetické prípady
def _change_number(text: str) -> str | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    value = m.group(0)
    digits = re.sub(r"\D", "", value)
    changed = str(int(digits) * 3 + 7) if digits else None
    return text[: m.start()] + changed + text[m.end():] if changed else None


def make_synthetic_cases(db: DB, task: str = "entail", n: int = 30, seed: int = 7) -> int:
    """Prípady s nastrčenou chybou: vymyslený citát, zmenené číslo (entail); cudzí kontakt (contact_assoc)."""
    rng = random.Random(seed)
    created = 0
    if task == "entail":
        claims = list(db.verified_extracted_claims_with_text())
        rng.shuffle(claims)
        for c in claims:
            if created >= n:
                break
            base = {k: c[k] for k in ("id", "problem", "affected_party", "quote", "chunk_index")}
            context = c["doc_text"][:20000]
            changed = _change_number(c["problem"])
            if changed and rng.random() < 0.5:
                case = {"claim": {**base, "problem": changed}, "context": context, "error": "zmenené číslo"}
            else:
                other = rng.choice(claims)
                if other["id"] == c["id"]:
                    continue
                case = {"claim": {**base, "quote": other["quote"]}, "context": context, "error": "citát z iného dokumentu"}
            case_id = db.add_eval_case("entail", case)
            db.add_label("entail", "eval_case", case_id, "unsupported", case["error"])
            created += 1
    elif task == "contact_assoc":
        rows = db.person_email_contacts()
        for r in rows[:n]:
            others = [x for x in rows if x["person_id"] != r["person_id"]]
            if not others:
                break
            foreign = rng.choice(others)["value"]
            snippet = r["source_snippet"].replace(r["value"], foreign) if r["value"] in r["source_snippet"] else r["source_snippet"]
            case_id = db.add_eval_case("contact_assoc", {"snippet": snippet, "person": r["full_name"], "value": r["value"]})
            db.add_label("contact_assoc", "eval_case", case_id, "incorrect", "cudzí kontakt")
            created += 1
    return created


def sample_for_review(db: DB, task: str, n: int = 20, seed: int = 0) -> int:
    """Stratifikovaná vzorka posúdení (rovnaký počet z každého verdiktu) do kontroly na ručné označenie."""
    verdicts = db.judgment_verdicts(task)
    per = max(1, n // max(1, len(verdicts)))
    added = 0
    for v in verdicts:
        for j in db.sample_judgments(task, v, per, seed):
            if not db.is_in_review(task, j["object_type"], j["object_id"]):
                db.enqueue_review(task, j["object_type"], j["object_id"], "sample", {"verdict": j["verdict"]})
                added += 1
    return added


# ------------------------------------------------------------------ beh
def run_eval(cfg: Config, db: DB, router: ModelRouter, task: str, profiles: list[str], limit: int | None = None) -> dict[str, Any]:
    if task not in PREDICTORS:
        raise ValueError(f"Eval pre úlohu {task} nie je; dostupné: {', '.join(PREDICTORS)}")
    items = eval_items(db, task, limit)
    results: dict[str, Any] = {"task": task, "n_labels": len(items), "profiles": {}}
    if not cfg.models:
        raise ValueError("Eval porovnáva roly modelov; v config.yaml chýba sekcia models.")
    for name in profiles:
        prof = cfg.models.eval_profiles[name]
        router.overrides[task] = TaskRoute(role=prof.model, thinking=prof.thinking, k=1)
        pairs, times, tokens = [], [], []
        for item in items:
            start = time.monotonic()
            pred = PREDICTORS[task](cfg, db, router, item)
            times.append(time.monotonic() - start)
            info = getattr(router.client(router.route(task).role, prof.thinking), "last", None)
            tokens.append((getattr(info, "prompt_tokens", 0) or 0) + (getattr(info, "completion_tokens", 0) or 0))
            pairs.append((item.gold, pred))
        router.overrides.pop(task, None)
        metrics = compute_metrics(pairs, POSITIVE.get(task))
        metrics["undecided"] = sum(1 for _, p in pairs if p == "undecided")
        metrics["avg_s"] = sum(times) / len(times) if times else None
        metrics["avg_tokens"] = sum(tokens) / len(tokens) if tokens else None
        results["profiles"][name] = {"model": router.role_config(prof.model).model, "thinking": prof.thinking, **metrics}
        db.add_eval_result(task, name, router.role_config(prof.model).model, prof.thinking, len(pairs), metrics)
    results["recommendation"] = recommend(results["profiles"], len(items))
    return results


def eval_ready(db: DB) -> dict[str, int]:
    """Úlohy s dosť gold labelmi na eval (≥ MIN_LABELS) a ich počet."""
    counts = db.label_counts()
    return {t: counts.get(t, 0) for t in EVAL_TASKS if counts.get(t, 0) >= MIN_LABELS}


def run_eval_all(cfg: Config, db: DB, router: ModelRouter, deadline=None) -> dict[str, Any]:
    """Eval všetkých profilov pre každú úlohu s dosť labelmi (akcia „Eval presnosti modelov“, aj po zmene modelu)."""
    out: dict[str, Any] = {}
    if not cfg.models:
        return {"preskočené": "v config.yaml chýba sekcia models"}
    for task in eval_ready(db):
        if deadline is not None and deadline.expired():
            out["nedokončené"] = True
            break
        res = run_eval(cfg, db, router, task, list(cfg.models.eval_profiles))
        write_eval_report(cfg, res)
        out[task] = res["recommendation"]
    return out or {"preskočené": f"žiadna úloha nemá aspoň {MIN_LABELS} gold labelov (Eval → Pridať na označenie)"}


def recommend(profiles: dict[str, Any], n: int) -> str:
    """Najlacnejší profil, ktorého interval sa prekrýva s najlepším. Pri < 50 labeloch nedostatok dát."""
    if n < MIN_LABELS:
        return f"nedostatok dát ({n} labelov, treba aspoň {MIN_LABELS})"
    if not profiles:
        return "bez výsledkov"
    best = max(profiles.items(), key=lambda kv: kv[1]["accuracy"])
    lo_best = best[1]["accuracy_ci"][0]
    overlapping = [(kv[1]["avg_s"] or 1e9, kv[0]) for kv in profiles.items() if kv[1]["accuracy_ci"][1] >= lo_best]
    return f"profil {min(overlapping)[1]} (najlacnejší, ktorého interval sa prekrýva s najlepším {best[0]})"


def write_eval_report(cfg: Config, results: dict[str, Any]) -> Path:
    def pct(x: float) -> str:
        return f"{100 * x:.0f} %"

    lines = [f"# Eval – úloha {results['task']} ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Položky s gold labelom: {results['n_labels']}. Odporúčanie: **{results['recommendation']}**.", "",
             "| Profil | Model | Uvažovanie | n | Podiel správnych (95 % interval) | Presnosť | Úplnosť | F1 | Neurčené | Čas/položku | Tokeny/položku |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, m in results["profiles"].items():
        b = m.get("binary") or {}
        lo, hi = m["accuracy_ci"]
        thinking = "áno" if m["thinking"] else "nie"
        if not m["n"]:
            lines.append(f"| {name} | {m['model']} | {thinking} | 0 | – | – | – | – | – | – | – |")
            continue
        precision = pct(b["precision"]) if b else "–"
        recall = pct(b["recall"]) if b else "–"
        f1 = f"{b['f1']:.2f}" if b else "–"
        lines.append(
            f"| {name} | {m['model']} | {thinking} | {m['n']} | {pct(m['accuracy'])} ({pct(lo)}–{pct(hi)}) | "
            f"{precision} | {recall} | {f1} | {m['undecided']} | {m['avg_s']:.1f} s | {m['avg_tokens']:.0f} |"
        )
    if results["n_labels"] < MIN_LABELS:
        lines += ["", f"_Nedostatok dát: pred záverom treba aspoň {MIN_LABELS} labelov na úlohu. "
                      "Označuj položky v GUI (Kontrola) alebo pridaj vzorku: `python -m radar eval --task T --sample 30`._"]
    out = Path(cfg.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{results['task']}_{datetime.now():%Y-%m-%d_%H%M}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
