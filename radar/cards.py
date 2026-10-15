"""Problémová karta: z hypotézy a jej citátov – aký je problém, kto ho má, aké rozhodnutie robí, ako dnes, aký výstup
mu pomôže a ako sa meria prínos. Každé tvrdenie karty odkazuje na citáty (C<id>); kód overí, že citáty patria
hypotéze, sudca posúdi podporu kľúčových polí, človek kartu potvrdí v GUI. Experimenty Brány 2 vychádzajú z karty.

Široká hypotéza (citáty o rôznych problémoch, rôznych ľuďoch alebo rozhodnutiach) sa rozdelí na podhypotézy:
citáty podproblému prejdú do podhypotézy, rodič si ponechá všeobecné. Podhypotéza zdedí dáta Brány 1 rodiča
(zdieľaná DuckDB) a ak rodič Bránou 1 prešiel, prejde aj ona s dôvodom „zdedené“.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Config
from .datasets import share_datasets
from .db import DB
from .models import as_router
from .prompts import CARD_CHECK_SYSTEM, PROBLEM_CARD_SYSTEM, prompt_version
from .util import Deadline

log = logging.getLogger(__name__)

OUTPUTS = ("ranking", "alert", "forecast", "comparison", "test")
OUTPUT_LABELS = {"ranking": "poradie (čo skontrolovať skôr)", "alert": "upozornenie na neobvyklé", "forecast": "predpoveď",
                 "comparison": "porovnanie s podobnými", "test": "test, či vzor existuje"}
FIELD_LABELS = {"problem": "Problém", "who": "Kto ho má", "decision": "Rozhodnutie", "current_practice": "Ako to robí dnes",
                "cost_of_error": "Cena chyby"}


class CardField(BaseModel):
    text: str = ""
    claims: list[int] = Field(default_factory=list)


class SubProblem(BaseModel):
    title: str = ""
    problem: str = ""
    segment: str = ""
    claims: list[int] = Field(default_factory=list)


class ProblemCardOut(BaseModel):
    problem: CardField = Field(default_factory=CardField)
    who: CardField = Field(default_factory=CardField)
    decision: CardField = Field(default_factory=CardField)
    current_practice: CardField = Field(default_factory=CardField)
    cost_of_error: CardField = Field(default_factory=CardField)
    helpful_output: Literal["ranking", "alert", "forecast", "comparison", "test"] = "ranking"
    helpful_output_text: str = ""
    business_metric: str = ""
    falsification: str = ""
    subproblems: list[SubProblem] = Field(default_factory=list)


class CardCheck(BaseModel):
    verdict: Literal["supported", "partial", "unsupported"] = "unsupported"
    reason: str = ""


def evidence_listing(claims, limit: int, groups: list[list[int]] | None = None) -> str:
    line = lambda c: f"C{c['id']} [{c['claim_type'] or '?'}] {c['problem']} | quote: {(c['quote'] or '')[:300]}"  # noqa: E731
    claims = claims[:limit]
    if not groups:
        return "\n".join(line(c) for c in claims)
    by_id = {c["id"]: c for c in claims}
    out = []
    for n, g in enumerate(groups, 1):
        out.append(f"GROUP {n}:" if len(g) > 1 else "UNGROUPED:")
        out += [line(by_id[i]) for i in g if i in by_id]
    return "\n".join(out)


def claim_groups(cfg: Config, embedder, claims) -> list[list[int]] | None:
    """Skupiny podobných citátov (embeddingy, priemerná väzba ako pri témach) – pomôcka pre rozdelenie; samostatné
    citáty spolu na konci. Bez embeddingov None."""
    from .embed import claim_text
    from .themes import average_linkage

    if embedder is None or len(claims) < cfg.cards.group_min_claims:
        return None
    try:
        vec = embedder.vectors("claim", [(c["id"], claim_text(c)) for c in claims])
    except Exception as exc:  # noqa: BLE001 – bez embeddingov karta vznikne aj tak
        log.warning("Skupiny citátov sa nepodarili: %s", exc)
        return None
    groups = average_linkage(vec @ vec.T, cfg.cards.group_threshold, len(claims))
    ids = [c["id"] for c in claims]
    multi = sorted((g for g in groups if len(g) > 1), key=len, reverse=True)
    single = [i for g in groups if len(g) == 1 for i in g]
    return [[ids[i] for i in g] for g in multi] + ([[ids[i] for i in single]] if single else [])


def card_targets(cfg: Config, db: DB, ids: list[int] | None) -> list:
    """Hypotézy na kartu: zadané, inak tie, čo prešli Bránou 1, potom vybrané na Bránu 0 – bez karty alebo so
    zastaranou kartou (pribudlo aspoň `cards.refresh_new_claims` citátov)."""
    if ids:
        hyps = [h for h in (db.get_hypothesis(i) for i in ids) if h is not None]
    else:
        seen, hyps = set(), []
        for h in list(db.gate2_hypotheses()) + list(db.people_enabled_hypotheses()):
            for x in [h, *db.children_of(h["id"])]:  # podhypotézy rozdelených hypotéz dostanú vlastnú kartu
                if x["id"] not in seen and x["status"] != "rejected":
                    seen.add(x["id"])
                    hyps.append(x)
    out = []
    for h in hyps:
        card = db.latest_card(h["id"])
        n = len(db.evidence_for(h["id"]))
        if n < cfg.cards.min_claims:
            continue
        if card is None or (card["status"] == "draft" and n - json.loads(card["card_json"]).get("počet_citátov", 0)
                            >= cfg.cards.refresh_new_claims):
            out.append(h)
    return out


def _valid_claims(ids: list[int], allowed: set[int]) -> list[int]:
    return [i for i in dict.fromkeys(ids) if i in allowed]


def make_card(cfg: Config, db: DB, llm: Any, hyp, embedder=None) -> dict[str, Any] | None:
    """Karta z modelu (presná rola) + kontrola citátov kódom + posúdenie kľúčových polí sudcom."""
    router = as_router(llm, cfg, db)
    claims = db.evidence_for(hyp["id"])[: cfg.cards.max_claims]
    groups = claim_groups(cfg, embedder, claims)
    allowed = {c["id"] for c in claims}
    quotes = {c["id"]: c["quote"] for c in claims}
    res = router.run_task(
        "problem_card",
        [{"role": "system", "content": PROBLEM_CARD_SYSTEM.format(lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"HYPOTHESIS: {hyp['title']}\nSEGMENT: {hyp['segment'] or '?'}\n"
                                     f"DESCRIPTION: {hyp['description'] or ''}\n\n<untrusted_document>\nEVIDENCE:\n"
                                     f"{evidence_listing(claims, cfg.cards.max_claims, groups)}\n</untrusted_document>"}],
        ProblemCardOut, object_type="hypothesis", object_id=hyp["id"], max_tokens=2500,
    )
    if not res.ok:
        return None
    out = res.value
    card: dict[str, Any] = {"počet_citátov": len(claims)}
    for key in FIELD_LABELS:
        f = getattr(out, key)
        card[key] = {"text": f.text.strip(), "claims": _valid_claims(f.claims, allowed)}
    card |= {"helpful_output": out.helpful_output, "helpful_output_text": out.helpful_output_text.strip(),
             "business_metric": out.business_metric.strip(), "falsification": out.falsification.strip()}
    used: set[int] = set()
    subs = []
    for sp in out.subproblems:
        ids = [i for i in _valid_claims(sp.claims, allowed) if i not in used]
        if len(ids) >= cfg.cards.min_claims_per_subproblem and sp.title.strip():
            used.update(ids)
            subs.append({"title": sp.title.strip()[:160], "problem": sp.problem.strip(), "segment": sp.segment.strip(),
                         "claims": ids})
    card["subproblems"] = subs if len(subs) >= 2 else []
    checks = {}
    for key in cfg.cards.check_fields:  # sudca: podporujú citované citáty tvrdenie karty?
        field = card.get(key) or {}
        if not field.get("claims"):
            checks[key] = {"verdict": "unsupported", "reason": "bez citátu"}
            continue
        cited = "\n".join(f"C{i}: {quotes[i]}" for i in field["claims"][:6])
        chk = router.run_task(
            "card_check",
            [{"role": "system", "content": CARD_CHECK_SYSTEM.format(lang=cfg.pipeline.output_language)},
             {"role": "user", "content": f"STATEMENT: {field['text']}\n\n<untrusted_document>\n{cited}\n</untrusted_document>"}],
            CardCheck, object_type="hypothesis", object_id=hyp["id"], max_tokens=400,
        )
        checks[key] = chk.value.model_dump() if chk.ok else {"verdict": "undecided", "reason": chk.reason or ""}
    card["kontrola"] = checks
    card["model"] = res.model
    if groups:
        card["skupiny_citátov"] = [g for g in groups if len(g) > 1]
    return card


def split_hypothesis(cfg: Config, db: DB, hyp, card: dict[str, Any]) -> list[int]:
    """Podhypotézy z podproblémov karty: presun citátov, väzba na rodiča, dedenie dát Brány 1."""
    from .gate1 import OPEN_STATUSES

    if hyp["parent_id"] or not card.get("subproblems"):  # podhypotéza sa ďalej nedelí (najviac jedna úroveň)
        return []
    children = []
    has_data = bool(db.fields_for(hyp["id"]))
    for sp in card["subproblems"]:
        child = db.create_hypothesis(sp["title"], sp["problem"], sp["segment"] or hyp["segment"] or "")
        db.set_parent(child, hyp["id"])
        db.move_claims(sp["claims"], hyp["id"], child)
        if has_data:
            db.clone_gate1(hyp["id"], child)
            share_datasets(cfg, child, hyp["id"])
            status = hyp["gate1_status"]
            if status in ("passed", "needs_revalidation"):
                # Dieťa zdedí odkazy na dáta, nie prechod: je to iná otázka a musí potvrdiť vlastné dátové požiadavky
                # (audit 14. 10. 2026 – H581/H582, H673/H674 zdedili „prešla“).
                db.add_gate_decision(1, child, "needs_revalidation", f"Zdedené z H{hyp['id']}: odkazy na dáta, nie prechod "
                                     "– potvrď stĺpce kľúčových polí pre túto otázku.", {}, decided_by="rule")
                db.set_gate_status(child, 1, "needs_revalidation")
            elif status in OPEN_STATUSES and status != "not_started":
                db.set_gate_status(child, 1, status)
        children.append(child)
    db.mark_split(hyp["id"])
    return children


def run_cards(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None, embedder=None) -> dict:
    stats: Counter = Counter()
    for hyp in card_targets(cfg, db, ids)[: cfg.cards.max_per_run]:
        if deadline.expired():
            break
        card = make_card(cfg, db, llm, hyp, embedder)
        if card is None:
            stats["llm_failures"] += 1
            continue
        if cfg.cards.auto_split and card["subproblems"] and not hyp["parent_id"]:
            card["rozdelené_na"] = split_hypothesis(cfg, db, db.get_hypothesis(hyp["id"]), card)
            stats["splits"] += 1
            stats["children"] += len(card["rozdelené_na"])
        db.add_card(hyp["id"], card, model=card.get("model"), prompt_version=prompt_version("problem_card"))
        stats["cards"] += 1
    return dict(stats)


def card_text(card: dict[str, Any]) -> str:
    """Karta pre ďalšie prompty (návrh experimentu, záver)."""
    lines = [f"- {label}: {(card.get(k) or {}).get('text', '')}" for k, label in FIELD_LABELS.items()]
    lines += [f"- Výstup, ktorý pomôže: {card.get('helpful_output', '')} – {card.get('helpful_output_text', '')}",
              f"- Obchodná metrika: {card.get('business_metric', '')}", f"- Čo by hypotézu vyvrátilo: {card.get('falsification', '')}"]
    return "\n".join(lines)
