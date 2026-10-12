"""Krok 3 – overenie. Žiadne tvrdenie sa nedostane do hypotéz bez kontroly voči zdroju.

1. Deterministicky (bez modelu): nachádza sa citát naozaj v dokumente? Sú čísla v citáte?
   Toto zachytí väčšinu halucinácií malých modelov. Tvrdenie, ktoré prejde, ostáva 'pending'.
2. Modelom (úloha entail, v profile balanced presný model): vyplýva tvrdenie z citátu, alebo ho
   rýchly model prehnane zovšeobecnil? Generátor (fast) tak neposudzuje sám seba.
   Model smie tvrdenie zamietnuť alebo zoslabiť, nikdy nepovýši neoverené na overené.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from .config import Config
from .db import DB
from .models import ModelRouter, TaskResult, as_router
from .prompts import ENTAIL_GATE_SYSTEM, ENTAIL_SYSTEM
from .util import Deadline, chunk_text

log = logging.getLogger(__name__)

_TRANS = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'",
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        "–": "-", "—": "-", "−": "-",
        " ": " ", " ": " ", " ": " ",
        "­": None,  # mäkký spojovník z PDF
    }
)


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_TRANS)).strip().lower()


def quote_score(quote: str, text: str) -> float:
    """0–100: ako dobre sa citát nachádza v texte. Citát skrátený '…' sa kontroluje po častiach."""
    q, t = normalize(quote), normalize(text)
    if not q or not t:
        return 0.0
    segments = [s.strip() for s in re.split(r"\.\.\.|…|\[\.\.\.\]", q) if len(s.strip()) >= 15] or [q]
    scores = []
    for seg in segments:
        scores.append(100.0 if seg in t else float(fuzz.partial_ratio(seg, t)))
    return min(scores)


# Čísla v slovenskom aj anglickom zápise: "2 890", "1 444,5", "1,444", "2.781", "15,9", "2026".
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:[   ]\d{3}(?!\d))+(?:[.,]\d+)?|[-+]?\d+(?:[.,]\d+)*")


def number_candidates(token: str) -> set[float]:
    """Možné hodnoty čísla podľa zápisu (napr. '2.781' môže byť 2781 alebo 2,781)."""
    t = re.sub(r"[   ]", "", token)
    sign = -1.0 if t.startswith("-") else 1.0
    t = t.lstrip("+-")
    values: set[float] = set()
    try:
        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):  # 1.444,5 -> desatinná čiarka
                values.add(float(t.replace(".", "").replace(",", ".")))
            else:  # 1,444.5 -> desatinná bodka
                values.add(float(t.replace(",", "")))
        elif "," in t:
            parts = t.split(",")
            if len(parts) == 2:
                values.add(float(t.replace(",", ".")))
                if len(parts[1]) == 3:
                    values.add(float(t.replace(",", "")))  # anglický oddeľovač tisícov
            else:
                values.add(float(t.replace(",", "")))
        elif "." in t:
            parts = t.split(".")
            if len(parts) == 2:
                values.add(float(t))
            if len(parts) > 2 or len(parts[1]) == 3:
                values.add(float(t.replace(".", "")))  # bodka ako oddeľovač tisícov
        else:
            values.add(float(t))
    except ValueError:
        return set()
    return {sign * v for v in values}


def numbers_in_text(text: str) -> set[float]:
    found: set[float] = set()
    for match in _NUM_RE.finditer(text or ""):
        found |= number_candidates(match.group())
    return found


def number_present(value: float, text: str, rel_tol: float = 0.005) -> bool:
    """Je číslo v texte? Znamienko sa ignoruje: model "prepad (-1,27)" zapíše ako 1,27
    a smer zmeny nesie text tvrdenia, nie číslo."""
    value = abs(value)
    return any(abs(abs(c) - value) <= max(1e-9, value * rel_tol) for c in numbers_in_text(text))


def numbers_missing_in_source(quote: str, source: str) -> list[str]:
    """Čísla z citátu, ktoré v zdroji nie sú. Fuzzy zhoda totiž prepustí aj citát
    s pozmeneným číslom ("1,5 mld." namiesto "1,444 mld."), preto ich kontrolujeme zvlášť."""
    source_values = numbers_in_text(source)
    missing = []
    for match in _NUM_RE.finditer(quote or ""):
        candidates = number_candidates(match.group())
        if candidates and not any(abs(c - s) <= max(1e-9, abs(c) * 1e-9) for c in candidates for s in source_values):
            missing.append(match.group())
    return missing


class Entailment(BaseModel):
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str


def quote_window(quote: str, text: str, radius: int = 2500) -> str:
    """Úsek textu okolo citátu – modelu netreba posielať celý dlhý dokument."""
    if len(text) <= 2 * radius:
        return text
    alignment = fuzz.partial_ratio_alignment(normalize(quote)[:300], text.lower())
    start = max(0, alignment.dest_start - radius)
    return text[start : alignment.dest_end + radius]


def claim_context(cfg: Config, doc_text: str, chunk_index: int) -> str:
    """Text, z ktorého model citát vzal (rovnaké delenie ako pri extrakcii)."""
    chunks = chunk_text(doc_text, cfg.pipeline.chunk_chars, cfg.pipeline.chunk_overlap)
    if 0 <= chunk_index < len(chunks):
        return chunks[chunk_index]
    return doc_text[: cfg.pipeline.chunk_chars]


def deterministic_check(cfg: Config, claim, doc_text: str) -> tuple[str, float, str, str]:
    """Kontrola kódom. Vráti (výsledok, skóre citátu, poznámka, kontext): výsledok je
    'rejected', 'ok' (citát aj čísla sedia) alebo 'partial' (čísla tvrdenia mimo citátu)."""
    p = cfg.pipeline
    context = claim_context(cfg, doc_text, claim["chunk_index"])
    score = quote_score(claim["quote"], context)
    if score < p.quote_match_threshold and len(doc_text) <= 60_000:
        context_score = quote_score(claim["quote"], doc_text)
        if context_score > score:
            score, context = context_score, doc_text
    if score < p.quote_match_threshold:
        return "rejected", score, "Citát sa v zdroji nenašiel (pravdepodobne vymyslený alebo prepísaný).", context
    changed = numbers_missing_in_source(claim["quote"], context)
    if changed:
        return "rejected", score, "Citát obsahuje čísla, ktoré v zdroji nie sú: " + ", ".join(changed) + ".", context
    numbers = json.loads(claim["numbers_json"] or "[]")
    missing = [n for n in numbers if not number_present(float(n.get("value", 0)), claim["quote"])]
    if missing:
        note = "Čísla mimo citátu: " + ", ".join(f"{m.get('value')} {m.get('unit', '')}".strip() for m in missing) + "."
        return "partial", score, note, context
    return "ok", score, "Citát nájdený v zdroji.", context


def entail_messages(cfg: Config, claim, context: str, gate: bool = False) -> list[dict[str, str]]:
    system = ENTAIL_GATE_SYSTEM if gate else ENTAIL_SYSTEM
    return [
        {"role": "system", "content": system.format(lang=cfg.pipeline.output_language)},
        {
            "role": "user",
            "content": f"CLAIM: {claim['problem']}\nWHO HAS THE PROBLEM: {claim['affected_party']}\n"
            f"QUOTE: {claim['quote']}\n\nCONTEXT:\n<untrusted_document>\n"
            f"{quote_window(claim['quote'], context)}\n</untrusted_document>",
        },
    ]


def judge_entailment(cfg: Config, router: ModelRouter, claim, doc_text: str, task: str = "entail") -> TaskResult:
    """Posúdenie modelom (entail alebo entail_gate); neurčený výsledok ide do kontroly."""
    context = claim_context(cfg, doc_text, claim["chunk_index"])
    if quote_score(claim["quote"], context) < cfg.pipeline.quote_match_threshold and len(doc_text) <= 60_000:
        context = doc_text
    return router.run_task(
        task,
        entail_messages(cfg, claim, context, gate=task == "entail_gate"),
        Entailment,
        object_type="claim",
        object_id=claim["id"],
        vote_key=lambda e: e.verdict,
        max_tokens=400,
        review=True,
        review_detail={"problem": claim["problem"], "quote": claim["quote"]},
    )


def apply_entailment(db: DB, claim, verdict: str, reason: str, low_agreement: bool = False) -> str:
    """Zapíše výsledok posúdenia. Model smie tvrdenie iba zamietnuť alebo zoslabiť."""
    det = claim["det_status"] or "ok"
    db.set_claim_entailment(claim["id"], verdict, low_agreement)
    if verdict == "unsupported":
        status = "rejected"
        note = "Citát existuje, ale tvrdenie z neho nevyplýva: " + reason
    else:
        status = "partial" if verdict == "partial" or det == "partial" else "verified"
        det_note = claim["verify_note"] if det == "partial" else ""
        note = " ".join(x for x in (det_note, reason) if x)
        if low_agreement:
            note += " (nízka zhoda modelu)"
    db.set_claim_verification(claim["id"], status, claim["quote_score"], note or "Citát nájdený v zdroji.")
    return status


def run_verify(
    cfg: Config,
    db: DB,
    llm: Any,
    deadline: Deadline,
    claim_ids: list[int] | None = None,
    deterministic_only: bool = False,
) -> dict:
    """1) kontrola kódom pre nové tvrdenia, 2) posúdenie modelom (ak je zapnuté).
    Tvrdenie bez výsledku modelu ostáva 'pending' a skúsi sa pri ďalšom behu (alebo ho rozhodne človek)."""
    stats: Counter = Counter()
    docs: dict[int, str] = {}

    def text_of(doc_id: int) -> str:
        if doc_id not in docs:
            docs[doc_id] = db.get_document(doc_id)["text"]
        return docs[doc_id]

    use_model = cfg.pipeline.verify_with_llm and llm is not None
    for claim in db.claims_for_deterministic(limit=5000, ids=claim_ids):
        if deadline.expired():
            break
        result, score, note, _ = deterministic_check(cfg, claim, text_of(claim["document_id"]))
        if result == "rejected":
            db.set_claim_verification(claim["id"], "rejected", score, note)
            stats["rejected"] += 1
        elif use_model:
            db.set_claim_deterministic(claim["id"], result, score, note)
        else:  # bez modelu rozhoduje iba kód
            db.set_claim_verification(claim["id"], "partial" if result == "partial" else "verified", score, note)
            stats["partial" if result == "partial" else "verified"] += 1
    if not use_model or deterministic_only:
        return dict(stats)

    router = as_router(llm, cfg, db)
    limit = cfg.pipeline.max_entail_per_run or 100_000
    for claim in db.claims_for_entailment(limit=limit, ids=claim_ids):
        if deadline.expired():
            break
        res = judge_entailment(cfg, router, claim, text_of(claim["document_id"]))
        if not res.ok:
            stats["llm_failures"] += 1
            continue  # zostane 'pending': timeout sa skúsi znova, orezaný/neplatný výstup rozhodne človek
        status = apply_entailment(db, claim, res.value.verdict, res.value.reason, res.low_agreement)
        stats[status] += 1
    return dict(stats)
