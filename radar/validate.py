"""Testovanie uložených hypotéz: každá hypotéza (aj zamietnutá) postupne prejde testom.

1. validate-search (rýchly model): agent hľadá nové nezávislé zdroje, protidôkazy a existujúce
   riešenia. Tvrdenia dostanú hypothesis_hint; hypotéza prejde do stavu „searched“.
2. Bežné kroky: kontrola citátu kódom a posúdenie presným modelom (verify), zaradenie do
   hypotézy presným modelom (cluster) – agent nič nepriradí ani neoverí sám.
3. validate-finish (bez modelu): keď už nič z testu nečaká, kód určí verdikt zo zložiek
   (počty overených tvrdení, domény, protidôkazy) a hypotéza prejde do stavu „tested“.

Verdikt je o podloženosti problému verejnými zdrojmi, nie o záujme zákazníkov (to rieši Brána 0).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from openai import APIConnectionError

from .agent import ResearchAgent
from .config import Config
from .critic import evidence_block
from .db import DB
from .strength import evidence_strength
from .util import Deadline, utc_days_ago

log = logging.getLogger(__name__)

VERDICTS = {
    "podložená": "aspoň 2 overené tvrdenia z 2 domén a protidôkazov je menej",
    "sporná": "overených protidôkazov je aspoň toľko ako podporných tvrdení",
    "slabá": "iba 1 overené podporné tvrdenie",
    "nepodložená": "žiadne overené podporné tvrdenie ani po teste",
}
STALE_DAYS = 3  # verdikt sa určí aj vtedy, ak tvrdenia z testu čakajú dlhšie (napr. na človeka v kontrole)


def validation_task(db: DB, hyp) -> str:
    evidence = db.evidence_for(hyp["id"])
    return (
        f"Test this hypothesis: {hyp['title']}\nSegment: {hyp['segment'] or '?'}\nDescription: {hyp['description'] or ''}\n\n"
        f"Known evidence (already saved, do not record it again):\n{evidence_block(evidence, 8) or '(none)'}\n\n"
        "Find NEW independent sources (other websites than the known evidence) that document the problem, "
        "counter-evidence, and existing solutions. Search in Slovak first, then Czech or English."
    )


def claims_from_read_pages(cfg: Config, db: DB, llm: Any, agent: ResearchAgent, hyp, max_pages: int = 4,
                           chunks_per_page: int = 2) -> list[int]:
    """Agent pri teste často iba číta (reálny beh 12. 10. 2026, MoE 35B: 52× read_page, 0× record_evidence).
    Z prečítaných stránok preto vytiahne tvrdenia bežná extrakcia (štruktúrovaný JSON); dostanú hypothesis_hint
    a prejdú rovnakým overením (kód → presný model) a zaradením presným modelom ako všetko ostatné."""
    from .extract import ExtractionResult, checked_claim_type, extraction_messages
    from .models import as_router
    from .util import PERSONAL_RE

    from rapidfuzz import fuzz

    router = as_router(llm, cfg, db)
    found: list[tuple[int, int, Any]] = []
    for url, (doc_id, chunks) in list(agent.pages.items())[:max_pages]:
        for idx, chunk in enumerate(chunks[:chunks_per_page]):
            res = router.run_task("extract", extraction_messages(cfg, hyp["title"], url, chunk), ExtractionResult,
                                  object_type="document", object_id=doc_id)
            if not res.ok or not res.value.relevant:
                continue
            for claim in res.value.claims[: cfg.pipeline.max_claims_per_chunk]:
                quote = claim.quote.strip()
                if quote and not PERSONAL_RE.search(quote) and db.find_claim(doc_id, quote) is None:
                    found.append((doc_id, idx, claim))
    # Najviac N tvrdení najpodobnejších testovanej hypotéze: každé stojí ~40 s presného modelu (posúdenie + zaradenie);
    # reálny beh: jedna hypotéza dostala z 4 stránok 30 tvrdení.
    target = f"{hyp['title']} {hyp['segment'] or ''}"
    found.sort(key=lambda f: -fuzz.token_set_ratio(f"{f[2].problem} {f[2].affected_party}".lower(), target.lower()))
    ids: list[int] = []
    for doc_id, idx, claim in found[: cfg.workflows.validate_max_page_claims]:
        quote = claim.quote.strip()
        ids.append(db.insert_claim(doc_id, idx, hyp["title"], claim.problem.strip(), claim.affected_party.strip(),
                                   checked_claim_type(claim.claim_type, quote), quote,
                                   [n.model_dump() for n in claim.numbers], origin="validation",
                                   sector_hint=claim.sector_hint.strip() or None, hypothesis_hint=hyp["id"]))
    return ids


def run_validate_search(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline, limit: int | None = None,
                        ids: list[int] | None = None) -> dict:
    w = cfg.workflows
    stats: Counter = Counter()
    queue = db.validation_queue(limit or w.validate_per_run, utc_days_ago(w.retest_days), ids)
    for hyp in queue:
        if deadline.expired():
            break
        agent = ResearchAgent(cfg, db, llm, searx, fetcher, topic=hyp["title"], hypothesis_id=hyp["id"],
                              task_name="validate", claim_origin="validation", hypothesis_hint=hyp["id"])
        info: dict[str, Any] = {"ok": True}
        try:
            summary = agent.run(validation_task(db, hyp), w.validate_max_steps, deadline=deadline)
            db.add_note(hyp["id"], f"Test hypotézy (agent): {summary}", source="agent")
        except APIConnectionError:
            raise  # model nebeží: hypotéza ostane vo fronte a test sa zopakuje pri ďalšom behu
        except Exception as exc:  # noqa: BLE001 – hypotéza sa aj tak posúdi z doterajších dôkazov
            log.warning("Test H%s: agent zlyhal: %s", hyp["id"], exc)
            info = {"ok": False, "chyba": str(exc)[:200]}
            stats["errors"] += 1
        if info["ok"] and not agent.claim_ids and agent.pages and not deadline.expired():
            extracted = claims_from_read_pages(cfg, db, llm, agent, hyp)
            agent.claim_ids += extracted
            info["z_prečítaných_stránok"] = len(extracted)
            stats["claims_from_pages"] += len(extracted)
        info.update(kroky=agent.steps, tvrdenia=len(agent.claim_ids), prečítané_zdroje=len(agent.pages))
        if agent.stopped_by_deadline and not agent.claim_ids:
            stats["deferred"] += 1  # čas behu vypršal skôr, než agent niečo našiel: test pri ďalšom behu
            break
        if info["ok"] and not agent.pages and not agent.claim_ids:
            # Agent neotvoril ani jeden zdroj: to nie je test. Prvýkrát sa hypotéza vráti na koniec
            # svojej skupiny vo fronte; opakovane sa uzavrie s príznakom, že test je neúplný.
            if hyp["validation_runs"] < 1:
                db.count_validation_attempt(hyp["id"])
                stats["retry_later"] += 1
                log.info("Test H%s: agent neprečítal žiadny zdroj, hypotéza sa zopakuje neskôr", hyp["id"])
                continue
            info.update(ok=False, chyba="agent neprečítal žiadny zdroj")
        db.set_validation_searched(hyp["id"], {"agent": info})
        stats["searched"] += 1
        stats["claims"] += len(agent.claim_ids)
        log.info("Test H%s: agent uložil %d tvrdení na overenie", hyp["id"], len(agent.claim_ids))
    return dict(stats)


def validation_result(db: DB, hid: int, search_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verdikt kódu a jeho zložky (nie skóre modelu)."""
    evidence = db.evidence_for(hid)
    label, n_ver, n_part, n_dom = evidence_strength(evidence)
    counter = [e for e in evidence if e["claim_type"] == "counter" and e["status"] == "verified"]
    solutions = [e for e in evidence if e["claim_type"] == "existing_solution" and e["status"] == "verified"]
    hinted = db.hinted_claims(hid)

    def assigned(c) -> set[str]:
        return set((c["assigned_to"] or "").split(",")) - {""}

    here = [c for c in hinted if str(hid) in assigned(c) and c["status"] in ("verified", "partial")]
    elsewhere = [c for c in hinted if assigned(c) and str(hid) not in assigned(c)]
    rejected = [c for c in hinted if c["status"] == "rejected"]
    if counter and len(counter) >= max(1, n_ver):
        verdict = "sporná"
    elif label in ("silnejšia", "stredná"):
        verdict = "podložená"
    elif n_ver == 1:
        verdict = "slabá"
    else:
        verdict = "nepodložená"
    return {
        "verdikt": verdict, "pravidlo": VERDICTS[verdict], "sila": label, "overené": n_ver, "čiastočne": n_part,
        "domény": n_dom, "protidôkazy": len(counter), "existujúce_riešenia": len(solutions),
        "z_testu": {"nájdené": len(hinted), "priradené_sem": len(here), "priradené_inam": len(elsewhere),
                    "zamietnuté": len(rejected)},
        "agent": (search_info or {}).get("agent"),
    }


def run_validate_finish(cfg: Config, db: DB, deadline: Deadline) -> dict:
    stats: Counter = Counter()
    stale = utc_days_ago(STALE_DAYS)
    for hyp in db.hypotheses_in_validation():
        if deadline.expired():
            break
        if db.validation_blocking(hyp["id"]) and (hyp["validation_started_at"] or "") > stale:
            stats["waiting"] += 1  # tvrdenia z testu ešte čakajú na posúdenie alebo zaradenie
            continue
        try:
            info = json.loads(hyp["validation_json"] or "{}")
        except ValueError:
            info = {}
        result = validation_result(db, hyp["id"], info)
        db.set_validation_result(hyp["id"], result)
        stats["tested"] += 1
        stats[f"verdict:{result['verdikt']}"] += 1
    return dict(stats)
