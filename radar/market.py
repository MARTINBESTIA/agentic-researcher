"""Krok 6 – hrubý odhad veľkosti trhu.

Model navrhne iba ŠTRUKTÚRU výpočtu (súčin 2–4 faktorov). Faktory typu "data" musia
mať hodnotu z nájdeného zdroja s overeným citátom; faktory typu "assumption" sú
výslovne označené predpoklady. Násobí Python, nie model. Ak niektorý faktor nemá
zdroj, odhad sa označí ako neúplný a nič sa nevymýšľa.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Config
from .critic import evidence_block
from .db import DB
from .fetch import Fetcher
from .llm import LLMError
from .models import ModelRouter, as_router
from .prompts import MARKET_MODEL_SYSTEM, NUMBER_SYSTEM
from .search import SearxClient
from .util import Deadline, chunk_text, utc_days_ago
from .verify import number_present, numbers_missing_in_source, quote_score

log = logging.getLogger(__name__)


class Factor(BaseModel):
    name: str
    unit: str
    kind: Literal["data", "assumption"]
    search_query: str | None = None
    low: float | None = None
    high: float | None = None
    rationale: str = ""


class SizingModel(BaseModel):
    segment_definition: str
    result_unit: str
    factors: list[Factor]


class NumberCandidate(BaseModel):
    value: float
    unit: str = ""
    quote: str
    measures: str = ""


class NumberExtraction(BaseModel):
    candidates: list[NumberCandidate] = Field(default_factory=list)


# Poradie je dôležité: "miliárd" sa musí rozpoznať skôr než "mil".
_SCALES = [
    (r"miliard|miliárd|billion|\bbn\b", 1e9),
    (r"\bmld", 1e9),
    (r"\bmil|million|\bmn\b", 1e6),
    (r"\btis|thousand", 1e3),
]


def unit_kind(unit: str) -> str:
    u = (unit or "").lower()
    if "%" in u or "percent" in u:
        return "share"
    if "€" in u or "eur" in u or "$" in u:
        return "money"
    return "count"


_UNIT_IN_QUOTE = {
    "share": re.compile(r"%|percent", re.I),
    "money": re.compile(r"€|\beur|\$|\busd\b|\bkč\b|\bczk\b", re.I),
}


def unit_in_quote(unit: str, quote: str) -> bool:
    """Pri penaziach a podieloch musí byť jednotka priamo v citáte. Model inak k holému číslu
    z tabuľky (napr. skóre 5,5) pripíše jednotku, ktorú zdroj neuvádza (skúšobný beh: "€/obyvateľ")."""
    pattern = _UNIT_IN_QUOTE.get(unit_kind(unit))
    return pattern is None or bool(pattern.search(quote or ""))


def to_base(value: float, unit: str) -> float:
    """Prevedie hodnotu na základnú jednotku: percentá na podiel, 'mil. €' na €."""
    u = (unit or "").lower()
    if unit_kind(u) == "share":
        return value / 100.0
    for pattern, multiplier in _SCALES:
        if re.search(pattern, u):
            return value * multiplier
    return value


def combine(ranges: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Súčin rozpätí: (dolná hranica, stred, horná hranica). Stred je geometrický priemer."""
    low = math.prod(lo for lo, _ in ranges)
    high = math.prod(hi for _, hi in ranges)
    mids = [math.sqrt(lo * hi) if lo > 0 and hi > 0 else (lo + hi) / 2 for lo, hi in ranges]
    return low, math.prod(mids), high


def relevant_chunks(chunks: list[str], query: str, k: int = 2) -> list[int]:
    """Indexy k častí dokumentu, kde sa najviac vyskytujú slová hľadanej veličiny.
    Zo slov sa odrežú koncovky (1–2 písmená, bez diakritiky) a hľadajú sa ako začiatok slova,
    takže "obcí" nájde aj "obce" a "obciach". Dlhé PDF má číslo často až na 40. strane."""
    words = {w for w in re.findall(r"[a-z]{4,}", fold(query))}
    stems = sorted({w[: max(3, len(w) - 2)] for w in words}, key=len, reverse=True)
    if not stems or len(chunks) <= k:
        return list(range(min(k, len(chunks))))
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, stems)) + r")")
    ranked = sorted(range(len(chunks)), key=lambda i: (-len(pattern.findall(fold(chunks[i]))), i))
    return sorted(ranked[:k])


def fold(text: str) -> str:
    """Malé písmená bez diakritiky."""
    return unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode("ascii")


def confidence(factors: list[dict[str, Any]], low: float | None, high: float | None) -> dict[str, Any]:
    """Miera istoty odhadu z jeho štruktúry (nie z úsudku modelu):
    vyššia = všetky faktory zo zdrojov a rozpätie najviac 3×; stredná = najviac jeden predpoklad
    a rozpätie najviac 10×; inak nízka. Neúplný odhad má istotu žiadnu."""
    if low is None or high is None:
        return {"label": "žiadna", "reasons": ["aspoň jeden faktor nemá zdroj, výsledok sa nepočíta"]}
    assumptions = sum(1 for f in factors if f.get("status") == "assumption")
    spread = high / low if low > 0 else float("inf")
    reasons = [f"faktory zo zdroja: {len(factors) - assumptions} z {len(factors)}", f"horná/dolná hranica: {spread:.1f}×"]
    if assumptions == 0 and spread <= 3:
        label = "vyššia"
    elif assumptions <= 1 and spread <= 10:
        label = "stredná"
    else:
        label = "nízka"
    return {"label": label, "reasons": reasons}


def find_factor_values(
    cfg: Config,
    db: DB,
    llm: ModelRouter,
    searx: SearxClient,
    fetcher: Fetcher,
    factor: Factor,
    segment: str,
    deadline: Deadline,
    max_sources: int = 4,
) -> list[dict[str, Any]]:
    p = cfg.pipeline
    query = factor.search_query or factor.name
    found: list[dict[str, Any]] = []
    # Najprv oficiálna štatistika (ŠÚ SR, Eurostat), potom celý web.
    queries = [f"{query} site:{site}" for site in p.market_preferred_sites] + [query]
    urls: list[str] = []
    for q in queries:
        if len(urls) >= max_sources:
            break
        try:
            urls += [r.url for r in searx.search(q, limit=2 if "site:" in q else max_sources) if r.url not in urls]
        except Exception as exc:
            log.warning("Hľadanie hodnoty '%s' zlyhalo: %s", q, exc)
    for url in urls[:max_sources]:
        if deadline.expired() or len(found) >= 3:
            break
        page = fetcher.fetch(url)
        if page is None:
            continue
        doc_id, _ = db.upsert_document(page.url, page.title, "market", None, query, page.content_hash, page.text)
        chunks = chunk_text(page.text, p.chunk_chars, p.chunk_overlap)
        for idx in relevant_chunks(chunks, f"{factor.name} {factor.search_query or ''}"):
            chunk = chunks[idx]
            res = llm.run_task(
                    "number_extract",
                    [
                        {"role": "system", "content": NUMBER_SYSTEM},
                        {
                            "role": "user",
                            "content": f"REQUESTED QUANTITY: {factor.name} [{factor.unit}]\nSEGMENT: {segment}\n"
                            f"Source: {page.url}\n\n<untrusted_document>\n{chunk}\n</untrusted_document>",
                        },
                    ],
                    NumberExtraction,
                    object_type="document",
                    object_id=doc_id,
                    max_tokens=800,
                )
            if not res.ok:
                log.warning("Extrakcia čísla zlyhala (%s): %s", page.url, res.reason)
                continue
            extraction = res.value
            for cand in extraction.candidates[:3]:
                if unit_kind(cand.unit) != unit_kind(factor.unit) or not unit_in_quote(factor.unit, cand.quote):
                    continue  # napr. percento namiesto počtu, alebo jednotka, ktorú citát neuvádza
                score = quote_score(cand.quote, chunk)
                if (
                    score < p.quote_match_threshold
                    or not number_present(cand.value, cand.quote)
                    or numbers_missing_in_source(cand.quote, chunk)
                ):
                    continue  # citát nie je v zdroji alebo číslo nesedí
                claim_id = db.find_claim(doc_id, cand.quote)
                if claim_id is None:
                    claim_id = db.insert_claim(
                        doc_id,
                        idx,
                        None,
                        f"{factor.name}: {cand.value} {cand.unit}".strip(),
                        cand.measures,
                        "market_data",
                        cand.quote,
                        [{"value": cand.value, "unit": cand.unit, "meaning": cand.measures}],
                        origin="market",
                    )
                    db.set_claim_verification(claim_id, "verified", score, "Citát aj číslo nájdené v zdroji (automaticky).")
                found.append(
                    {
                        "value": cand.value,
                        "unit": cand.unit,
                        "base_value": to_base(cand.value, cand.unit),
                        "measures": cand.measures,
                        "quote": cand.quote,
                        "url": page.url,
                        "claim_id": claim_id,
                    }
                )
    return found


def estimate_hypothesis(
    cfg: Config, db: DB, llm: ModelRouter, searx: SearxClient, fetcher: Fetcher, hyp, evidence: list, deadline: Deadline
) -> bool:
    res = llm.run_task(
        "market_structure",
        [
            {"role": "system", "content": MARKET_MODEL_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {
                "role": "user",
                "content": f"HYPOTHESIS: {hyp['title']}\nSEGMENT: {hyp['segment']}\nDESCRIPTION: {hyp['description']}"
                f"\n\nEVIDENCE:\n{evidence_block(evidence, 10)}",
            },
        ],
        SizingModel,
        object_type="hypothesis",
        object_id=hyp["id"],
    )
    if not res.ok:
        raise LLMError(f"Štruktúra odhadu trhu sa nepodarila ({res.reason}).")
    model = res.value
    factors_out: list[dict[str, Any]] = []
    ranges: list[tuple[float, float]] = []
    complete = True
    for factor in model.factors[:4]:
        entry: dict[str, Any] = {
            "name": factor.name,
            "unit": factor.unit,
            "kind": factor.kind,
            "rationale": factor.rationale,
            "search_query": factor.search_query,
        }
        if factor.kind == "assumption":
            if factor.low is None or factor.high is None:
                entry["status"] = "missing"
                complete = False
            else:
                lo, hi = sorted((to_base(factor.low, factor.unit), to_base(factor.high, factor.unit)))
                entry.update(status="assumption", low=lo, high=hi)
                ranges.append((lo, hi))
        else:
            found = find_factor_values(cfg, db, llm, searx, fetcher, factor, hyp["segment"] or "", deadline)
            if found:
                values = [f["base_value"] for f in found]
                entry.update(status="data", low=min(values), high=max(values), evidence=found)
                ranges.append((min(values), max(values)))
            else:
                entry["status"] = "missing"
                complete = False
        factors_out.append(entry)

    complete = complete and bool(ranges)
    low = base = high = None
    if complete:
        low, base, high = combine(ranges)
    detail = {
        "segment_definition": model.segment_definition,
        "factors": factors_out,
        "confidence": confidence(factors_out, low, high),
    }
    db.add_market_estimate(hyp["id"], complete, model.result_unit, low, base, high, detail)
    return complete


def run_market(cfg: Config, db: DB, llm: Any, searx: SearxClient, fetcher: Fetcher, deadline: Deadline) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    p = cfg.pipeline
    queue = []
    for hyp in db.active_hypotheses():
        evidence = db.evidence_for(hyp["id"])
        if len(evidence) < p.market_min_claims:
            continue
        last = db.latest_market_estimate(hyp["id"])
        if last and last["created_at"] >= utc_days_ago(p.market_refresh_days):
            continue
        queue.append((len(evidence), hyp, evidence))
    queue.sort(key=lambda item: item[0], reverse=True)  # najprv hypotézy s najviac dôkazmi
    for _, hyp, evidence in queue[: p.max_market_per_run]:
        if deadline.expired():
            break
        try:
            complete = estimate_hypothesis(cfg, db, router, searx, fetcher, hyp, evidence, deadline)
        except LLMError as exc:
            log.warning("Odhad trhu pre hypotézu %s zlyhal: %s", hyp["id"], exc)
            stats["llm_failures"] += 1
            continue
        stats["complete" if complete else "incomplete"] += 1
    return dict(stats)
