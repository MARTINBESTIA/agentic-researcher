"""Krok 4 – zhlukovanie: overené tvrdenia priradí k hypotézam (problém + segment) alebo založí novú.

Poradie pre každé tvrdenie:
1. Prebratý text: rovnaký citát už máme z iného dokumentu (napr. tlačová správa na viacerých
   portáloch) – tvrdenie sa označí ako kópia, priradí sa k hypotéze originálu a nezvyšuje silu dôkazov.
2. Kandidáti: najpodobnejšie hypotézy podľa embeddingov (záloha: textová podobnosť) a hypotéza
   projektu, ak tvrdenie pochádza z dopytov rozpracovaného projektu. Zvažujú sa aj hypotézy
   v stave "candidate", inak by sa nikdy nepovýšili (R-DISC-7).
3. Novosť (objavovanie): ak je najvyššia podobnosť pod discovery.novelty_threshold, vznikne
   hypotéza "candidate" bez volania modelu.
4. Model (úloha cluster_assign) rozhodne: existujúca hypotéza, alebo nová. Nová hypotéza
   z objavovania má stav "candidate".
Na konci sa podobné hypotézy (embeddingy + potvrdenie modelom) zlúčia, pozri consolidate(),
a kandidáti s aspoň 2 overenými tvrdeniami z 2 domén sa povýšia na "new".
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Literal

import numpy as np
from openai import APIError, APITimeoutError
from pydantic import BaseModel
from rapidfuzz import fuzz, process

from .config import Config
from .db import DB
from .embed import Embedder, rank_hypotheses, similar_pairs
from .models import ModelRouter, as_router
from .prompts import CLUSTER_SYSTEM, MERGE_SYSTEM
from .strength import NOT_SUPPORTING, evidence_strength
from .util import Deadline
from .verify import normalize

log = logging.getLogger(__name__)


class Assignment(BaseModel):
    decision: Literal["existing", "new"]
    hypothesis_id: int | None = None
    title: str | None = None
    description: str | None = None
    segment: str | None = None


def sync_projects(cfg: Config, db: DB) -> dict:
    """Rozpracované projekty z config.yaml ako hypotézy (stav 'watch')."""
    stats: Counter = Counter()
    for project in cfg.projects:
        _, created = db.upsert_project_hypothesis(project.name, project.title, project.description, project.segment)
        stats["project_created" if created else "project_synced"] += 1
    return dict(stats)


def text_candidates(problem: str, hypotheses: list, show_all_up_to: int = 15, k: int = 8) -> list:
    """Záloha bez embeddingov: malý počet hypotéz pošli celý, inak vyber najpodobnejšie podľa textu."""
    if len(hypotheses) <= show_all_up_to:
        return list(hypotheses)
    choices = {h["id"]: f"{h['title']}. {h['description'] or ''} {h['segment'] or ''}" for h in hypotheses}
    ranked = process.extract(problem, choices, scorer=fuzz.token_set_ratio, limit=k)
    by_id = {h["id"]: h for h in hypotheses}
    return [by_id[key] for _, _, key in ranked]


class DuplicateFinder:
    """Rozpozná prebratý text: takmer rovnaký citát z iného dokumentu, ktorý je už zaradený."""

    def __init__(self, db: DB, threshold: int):
        self.threshold = threshold
        self.rows = {r["id"]: r for r in db.assigned_quotes()}
        self.choices = {cid: normalize(r["quote"]) for cid, r in self.rows.items()}

    def find(self, claim) -> tuple[int, int] | None:
        """(id originálu, id hypotézy) alebo None."""
        quote = normalize(claim["quote"])
        if len(quote) < 40 or not self.choices:  # krátke citáty sa opakujú aj nezávisle
            return None
        match = process.extractOne(quote, self.choices, scorer=fuzz.ratio, score_cutoff=self.threshold)
        if match is None:
            return None
        original = self.rows[match[2]]
        if original["document_id"] == claim["document_id"]:
            return None
        return int(original["id"]), int(original["hypothesis_id"])

    def add(self, claim, hid: int) -> None:
        self.rows[claim["id"]] = {"id": claim["id"], "document_id": claim["document_id"], "hypothesis_id": hid}
        self.choices[claim["id"]] = normalize(claim["quote"])


def _is_discovery(claim) -> bool:
    """Objavovanie: dokument z kroku discover alebo tvrdenie agenta, ktorý hľadá nové problémy (scout)."""
    keys = claim.keys()
    return ("doc_kind" in keys and claim["doc_kind"] == "discovery") or ("origin" in keys and claim["origin"] == "scout")


def _hint_of(claim) -> int | None:
    return claim["hypothesis_hint"] if "hypothesis_hint" in claim.keys() else None


def best_similarity(embedder: Embedder, claim, hypotheses: list) -> float:
    if not hypotheses:
        return 0.0
    sims = embedder.hypothesis_matrix(hypotheses) @ embedder.claim_vector(claim)
    return float(np.max(sims))


def promote_candidates(cfg: Config, db: DB) -> int:
    """Kandidát sa stane hypotézou ("new") až s aspoň 2 overenými tvrdeniami z 2 domén,
    aby jeden článok nevyrobil hypotézu (R-DISC-7)."""
    rule = cfg.discovery.candidate_promotion
    promoted = 0
    for hyp in db.candidate_hypotheses():
        _, n_ver, _, n_dom = evidence_strength(db.evidence_for(hyp["id"]))
        if n_ver >= rule.min_claims and n_dom >= rule.min_domains:
            db.set_hypothesis_status(hyp["id"], "new")
            promoted += 1
    return promoted


def run_cluster(cfg: Config, db: DB, llm: Any, deadline: Deadline, embedder: Embedder | None = None,
                claim_ids: set[int] | None = None) -> dict:
    """claim_ids: zaradí iba tieto tvrdenia a bez zlučovania hypotéz (dávka testu hypotéz); None = všetky."""
    router: ModelRouter = as_router(llm, cfg, db)
    stats: Counter = Counter(sync_projects(cfg, db))
    lang = cfg.pipeline.output_language
    project_by_topic = {p.topic: p.name for p in cfg.projects}
    duplicates = DuplicateFinder(db, cfg.pipeline.duplicate_quote_threshold)
    budget = cfg.pipeline.max_cluster_per_run or 100_000
    model_calls = 0

    for claim in db.unassigned_evidence(limit=5000):  # obmedzuje to časový limit a rozpočet volaní
        if deadline.expired():
            break
        if claim_ids is not None and claim["id"] not in claim_ids:
            continue
        dup = duplicates.find(claim)
        if dup is not None:
            original_id, hid = dup
            db.mark_duplicate(claim["id"], original_id)
            db.assign_claim(hid, claim["id"])
            stats["duplicates"] += 1
            continue
        if model_calls >= budget:
            stats["deferred"] += 1
            continue

        discovery = _is_discovery(claim)
        hypotheses = db.assignable_hypotheses()
        candidates = None
        if embedder is not None:
            try:
                if discovery and hypotheses and best_similarity(embedder, claim, hypotheses) < cfg.discovery.novelty_threshold:
                    hid = db.create_hypothesis(
                        claim["problem"][:200], claim["problem"], claim["affected_party"] or "", status="candidate",
                        origin="discovery", sector=claim["sector_hint"],
                    )
                    db.assign_claim(hid, claim["id"])
                    duplicates.add(claim, hid)
                    stats["new_candidates"] += 1
                    continue
                candidates = rank_hypotheses(embedder, claim, hypotheses)
            except (APIError, APITimeoutError) as exc:
                log.warning("Embeddingy nie sú dostupné (%s); používam textovú podobnosť.", exc)
                embedder = None
        if candidates is None:
            candidates = text_candidates(claim["problem"], hypotheses)
        project = project_by_topic.get(claim["topic"] or "")
        if project:
            own = db.project_hypothesis(project)
            if own is not None and own["status"] != "rejected" and own["id"] not in {h["id"] for h in candidates}:
                candidates = [own] + candidates
        hint = _hint_of(claim)
        if hint is not None and hint not in {h["id"] for h in candidates}:
            # Tvrdenie z testu hypotézy: testovaná hypotéza je vždy medzi kandidátmi (aj zamietnutá),
            # ale priradenie rozhodne presný model – agent ho nepriradí sám.
            tested = db.get_hypothesis(hint)
            if tested is not None:
                candidates = [tested] + candidates

        listing = "\n".join(
            f"- id={h['id']}{' [existing project]' if h['project'] else ''}: {h['title']} "
            f"(segment: {h['segment'] or '?'}) – {h['description'] or ''}"
            for h in candidates
        ) or "(no hypotheses yet)"
        model_calls += 1
        res = router.run_task(
            "cluster_assign",
            [
                {"role": "system", "content": CLUSTER_SYSTEM.format(lang=lang)},
                {
                    "role": "user",
                    "content": f"NEW CLAIM: {claim['problem']}\nWHO: {claim['affected_party']}\n"
                    f"TOPIC: {claim['topic'] or '-'}\n\nCANDIDATE HYPOTHESES:\n{listing}",
                },
            ],
            Assignment,
            object_type="claim",
            object_id=claim["id"],
            vote_key=lambda a: f"{a.decision}:{a.hypothesis_id if a.decision == 'existing' else ''}",
            max_tokens=600,
        )
        if not res.ok:
            log.warning("Zaradenie tvrdenia %s zlyhalo: %s", claim["id"], res.reason)
            stats["llm_failures"] += 1
            continue
        result = res.value
        valid_ids = {h["id"] for h in candidates}
        if result.decision == "existing" and result.hypothesis_id in valid_ids:
            hid = result.hypothesis_id
            stats["assigned_existing"] += 1
        elif claim["claim_type"] in NOT_SUPPORTING:
            # Protidôkaz alebo existujúce riešenie, ktoré nepatrí k žiadnej hypotéze: nová hypotéza
            # z neho nevznikne (nie je to problém) a viac sa nezaraďuje.
            db.mark_claim_unrelated(claim["id"])
            stats["unrelated_not_supporting"] += 1
            continue
        else:
            hid = db.create_hypothesis(
                (result.title or claim["problem"])[:200],
                result.description or claim["problem"],
                result.segment or claim["affected_party"] or "",
                status="candidate" if discovery else "new",
                origin="discovery" if discovery else "topic",
                topic=claim["topic"],
                sector=claim["sector_hint"],
            )
            stats["new_candidates" if discovery else "new_hypotheses"] += 1
        db.assign_claim(hid, claim["id"])
        duplicates.add(claim, hid)

    if embedder is not None and claim_ids is None and not deadline.expired():
        try:
            stats.update(consolidate(cfg, db, router, embedder, deadline))
        except (APIError, APITimeoutError) as exc:
            log.warning("Zlučovanie hypotéz preskočené, embeddingy nie sú dostupné: %s", exc)
    promoted = promote_candidates(cfg, db)
    if promoted:
        stats["promoted"] = promoted
    return dict(stats)


class MergeDecision(BaseModel):
    same: bool
    title: str | None = None
    description: str | None = None
    segment: str | None = None


def consolidate(cfg: Config, db: DB, llm: Any, embedder: Embedder, deadline: Deadline, max_merges: int = 30) -> Counter:
    """Zlúči hypotézy, ktoré sú podľa embeddingov podobné a podľa modelu ten istý problém.

    Malý model zakladá pri postupnom zaraďovaní priveľa hypotéz (v skúšobnom behu 30 zo 47
    tvrdení). Dvojice s podobnosťou nad prahom (0,70) posúdi model; cieľom je projekt, inak
    hypotéza s viac dôkazmi. Ručne posunuté hypotézy (interviewing) sa nezlučujú a raz
    zamietnutá dvojica sa už znova nepýta.
    """
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    lang = cfg.pipeline.output_language
    hypotheses = [h for h in db.assignable_hypotheses() if h["status"] != "interviewing"]
    pairs = similar_pairs(embedder, hypotheses, cfg.embeddings.similar_hypotheses)
    merged_into: dict[int, int] = {}

    def current(hid: int) -> int:
        while hid in merged_into:
            hid = merged_into[hid]
        return hid

    for a, b, sim in pairs:
        if deadline.expired() or stats["merged"] >= max_merges:
            break
        a, b = current(a), current(b)
        if a == b or db.merge_checked(a, b):
            continue
        ha, hb = db.get_hypothesis(a), db.get_hypothesis(b)
        if ha["project"] and hb["project"]:
            continue  # dva rozpracované projekty sa nezlučujú
        res = router.run_task(
            "merge_check",
            [
                {"role": "system", "content": MERGE_SYSTEM.format(lang=lang)},
                {
                    "role": "user",
                    "content": "\n".join(
                        f"HYPOTHESIS {label}: {h['title']} (segment: {h['segment'] or '?'}) – {h['description'] or ''}"
                        for label, h in (("A", ha), ("B", hb))
                    ),
                },
            ],
            MergeDecision,
            object_type="hypothesis_pair",
            object_id=a * 1_000_000 + b,
            max_tokens=400,
        )
        if not res.ok:
            log.warning("Posúdenie H%s a H%s zlyhalo: %s", a, b, res.reason)
            stats["llm_failures"] += 1
            continue
        decision = res.value
        db.record_merge_check(a, b, decision.same)
        if not decision.same:
            stats["merge_rejected"] += 1
            continue

        # Cieľ: projekt, inak aktívna hypotéza pred kandidátom, potom viac dôkazov (pri zhode staršia).
        def weight(h):
            return (1 if h["project"] else 0, 0 if h["status"] == "candidate" else 1, len(db.evidence_for(h["id"])), -h["id"])

        target, source = (ha, hb) if weight(ha) >= weight(hb) else (hb, ha)
        db.merge_hypotheses(
            source["id"], target["id"], note=f"Automaticky zlúčené z H{source['id']} (podobnosť {sim:.2f}).", source="agent"
        )
        if not target["project"] and decision.title:
            db.update_hypothesis_text(
                target["id"], decision.title[:200], decision.description or target["description"] or "",
                decision.segment or target["segment"] or "",
            )
        merged_into[source["id"]] = target["id"]
        stats["merged"] += 1
    return stats
