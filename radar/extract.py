"""Krok 2 – extrakcia: z každej časti dokumentu vytiahne tvrdenia o problémoch s doslovným citátom.

Dokumenty z objavovania (R-DISC-6) najprv posúdi úloha `screen` podľa začiatku textu; plná
extrakcia beží iba pri kladnom výsledku. Pri nich sú povinné `affected_party` a `sector_hint`.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .models import ModelRouter, as_router
from .prompts import EXTRACT_SYSTEM, SCREEN_SYSTEM
from .util import PERSONAL_RE, Deadline, chunk_text

log = logging.getLogger(__name__)

ClaimType = Literal[
    "pain", "cost", "frequency", "regulation", "demand_signal", "market_data", "existing_solution", "other"
]


class NumberFact(BaseModel):
    value: float = Field(description="Číslo presne ako je v citáte (1,444 -> 1.444); mierku nechaj v jednotke.")
    unit: str = Field(default="", description="Jednotka aj s mierkou, napr. 'mld. €', '%', 'obcí'.")
    meaning: str = Field(default="", description="Čo číslo meria.")


class RegulationInfo(BaseModel):
    who: str = ""
    what: str = ""
    sanction: str = ""


class DemandInfo(BaseModel):
    who: str = ""
    what: str = ""


class ExtractedClaim(BaseModel):
    problem: str
    affected_party: str
    claim_type: ClaimType
    quote: str
    numbers: list[NumberFact] = Field(default_factory=list)
    sector_hint: str = ""
    effective_date: str | None = None
    regulation: RegulationInfo | None = None
    demand: DemandInfo | None = None


class ExtractionResult(BaseModel):
    relevant: bool
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ScreenResult(BaseModel):
    relevant: bool
    sector_hint: str = ""
    reason: str = ""


def extraction_messages(cfg: Config, topic: str | None, url: str, chunk: str) -> list[dict[str, str]]:
    p = cfg.pipeline
    return [
        {"role": "system", "content": EXTRACT_SYSTEM.format(lang=p.output_language, max_claims=p.max_claims_per_chunk)},
        {
            "role": "user",
            "content": f"Research topic: {topic or 'general search for problems'}\nSource: {url}\n\n"
            f"<untrusted_document>\n{chunk}\n</untrusted_document>",
        },
    ]


_DATE_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?$")


def checked_effective_date(value: str | None, chunk: str) -> str | None:
    """Dátum účinnosti iba v tvare YYYY-MM(-DD) a iba ak je rok v texte zdroja (nič sa nevymýšľa)."""
    value = (value or "").strip()
    m = _DATE_RE.match(value)
    if not m or m.group(1) not in chunk:
        return None
    return value


# Signál dopytu = niekto aktívne hľadá riešenie (R-DISC-4). Rýchly model tak označoval aj blogy dodávateľov,
# preto ho kód prijme iba pri výraze z obstarávania alebo výzvy priamo v citáte.
DEMAND_TERMS = re.compile(
    r"pr[ií]pravn\w*\s+trhov\w*\s+konzult|\bPTK\b|v[ýy]zv[aeuyo]\w*|s[úu][ťt]a[žz]\w*|z[áa]kazk\w*|obstar[áa]va\w*|"
    r"tender|h[ľl]ad[áa]\w*\s+dod[áa]vate[ľl]|request\s+for\s+(proposal|information|quotation)|\bRF[PIQ]\b|call\s+for\s+(proposals|tenders)",
    re.I,
)


def checked_claim_type(claim_type: str, quote: str) -> str:
    if claim_type == "demand_signal" and not DEMAND_TERMS.search(quote or ""):
        return "pain"
    return claim_type


def claim_details(claim: ExtractedClaim) -> dict[str, Any] | None:
    if claim.claim_type == "regulation" and claim.regulation:
        return {"regulation": claim.regulation.model_dump()}
    if claim.claim_type == "demand_signal" and claim.demand:
        return {"demand": claim.demand.model_dump()}
    return None


def run_screen(cfg: Config, db: DB, llm: Any, deadline: Deadline, limit: int = 200) -> dict:
    """Objavovanie: lacné posúdenie začiatku dokumentu pred plnou extrakciou (R-DISC-6)."""
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    lang = cfg.pipeline.output_language
    for doc in db.documents_to_screen(limit):
        if deadline.expired():
            break
        head = (doc["text"] or "")[: cfg.discovery.screen_chars]
        res = router.run_task(
            "screen",
            [
                {"role": "system", "content": SCREEN_SYSTEM.format(lang=lang)},
                {"role": "user", "content": f"Source: {doc['url']}\n<untrusted_document>\n{head}\n</untrusted_document>"},
            ],
            ScreenResult,
            object_type="document",
            object_id=doc["id"],
            max_tokens=300,
        )
        if not res.ok:
            stats["llm_failures"] += 1
            continue
        db.set_screened(doc["id"], res.value.relevant)
        stats["relevant" if res.value.relevant else "not_relevant"] += 1
    return dict(stats)


def run_extract(cfg: Config, db: DB, llm: Any, deadline: Deadline) -> dict:
    router: ModelRouter = as_router(llm, cfg, db)
    stats: Counter = Counter()
    p = cfg.pipeline
    for doc in db.documents_to_extract(p.max_docs_to_extract):
        discovery = doc["source_kind"] == "discovery"
        chunks = chunk_text(doc["text"], p.chunk_chars, p.chunk_overlap)[: p.max_chunks_per_doc]
        finished = True
        for idx in range(doc["extract_progress"], len(chunks)):
            if deadline.expired():
                finished = False
                break
            res = router.run_task(
                "extract",
                extraction_messages(cfg, doc["topic"], doc["url"], chunks[idx]),
                ExtractionResult,
                object_type="document",
                object_id=doc["id"],
            )
            if not res.ok:
                log.warning("Extrakcia zlyhala (dokument %s, časť %s): %s", doc["id"], idx, res.reason)
                stats["llm_failures"] += 1
                db.set_extract_progress(doc["id"], idx + 1)
                continue
            result = res.value
            claims = result.claims[: p.max_claims_per_chunk] if result.relevant else []
            if not result.relevant:
                stats["irrelevant_chunks"] += 1
            for claim in claims:
                quote = claim.quote.strip()
                if not quote or PERSONAL_RE.search(quote):
                    stats["skipped_claims"] += 1
                    continue
                if discovery and (not claim.affected_party.strip() or not claim.sector_hint.strip()):
                    stats["skipped_claims"] += 1  # pri objavovaní sú segment aj odvetvie povinné
                    continue
                if db.find_claim(doc["id"], quote) is not None:  # napr. z prekryvu dvoch častí
                    stats["duplicate_claims"] += 1
                    continue
                db.insert_claim(
                    doc["id"],
                    idx,
                    doc["topic"],
                    claim.problem.strip(),
                    claim.affected_party.strip(),
                    checked_claim_type(claim.claim_type, quote),
                    quote,
                    [n.model_dump() for n in claim.numbers],
                    sector_hint=claim.sector_hint.strip() or None,
                    effective_date=checked_effective_date(claim.effective_date, chunks[idx]),
                    details=claim_details(claim),
                )
                stats["claims"] += 1
            db.set_extract_progress(doc["id"], idx + 1)  # po páde alebo vypršaní času pokračujeme odtiaľto
        if not finished:
            break
        db.mark_extracted(doc["id"])
        stats["documents"] += 1
    return dict(stats)
