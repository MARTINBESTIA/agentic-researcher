"""Doménové podklady (dodatok 16. 10. 2026): stav výskumu domény, priority, tvrdenia so stavom a rozsahom, otázky
s blokovanou akciou, jedna hlavná otázka do prvého oslovenia a odpovede s históriou.

Doménová karta (prehľad domény z `value.py`) je **neblokujúce obohatenie**: Bránu 0, 1 ani 2 sama neoznačí za
úspešnú a neprebije pravidlá kontaktovania (potlačenie, odmietnutie, kontroly). Tri pripravenosti sú oddelené:

- rozhovor: overený dôvod výberu kontaktu, zrozumiteľná hypotéza, jedna relevantná otázka;
- žiadosť o dáta / demo: konkrétny možný výstup, účel dát, primeraný prvý vstup;
- analýza a tvrdenie o výsledku: použiteľné dáta, význam polí, metóda a doložený výpočet (Brány 1 a 2).

Stav podkladov: `not_requested → queued → researching → ready | partial | needs_research`; `stale` je príznak
verzie (podklady sa zmenili od poslednej verzie prehľadu). `ready` znamená pokrytie všetkých častí podkladom alebo
označenou hypotézou s otázkou – nie potvrdený problém firmy.
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from .config import Config
from .db import DB
from .privacy import fold
from .util import utcnow

STATUS_LABEL = {"not_requested": "nepožiadané", "queued": "v poradí", "researching": "prebieha", "ready": "pripravené",
                "partial": "čiastočné", "needs_research": "treba doplniť"}
PARTS = {"roles": "roly", "process": "proces", "decision": "rozhodnutie alebo pracovný úkon a kto môže konať",
         "solutions": "dnešné a existujúce riešenia", "data": "význam dát",
         "alternatives": "alternatívne vysvetlenia (prečo nový nástroj nemusí pomôcť)"}
EXTRA_PARTS = {"terms": "pojmy", "other": "iné"}
CLAIM_STATUS = {"source_fact": "zo zdroja", "hypothesis": "hypotéza", "contact_reported": "uviedol kontakt",
                "data_observed": "pozorované v dátach", "contradicted": "vyvrátené",
                "proposal": "návrh požadovaného vstupu (nie zdrojový fakt)", "unknown": "neznáme"}
# Pokrytie časti: podložené / hypotéza (výrok bez zdroja na overenie) / len otázka / chýba. Otázka sama časť
# nepokrýva (zadanie 17. 10. 2026: prázdny proces s otázkou hlásil „pripravené – pokrytie všetkých častí“).
COVERAGE_LABELS = ("podložené", "hypotéza", "len otázka", "chýba")
CORE_PARTS = ("process", "decision")  # bez nich nie je „pripravené“ ani pri inak dobrom pokrytí
SUPPORTED = ("source_fact", "contact_reported", "data_observed")
SCOPES = {"occupation": "všeobecne pre povolanie – nie fakt o konkrétnej organizácii",
          "sector": "pre odvetvie podľa zdroja – nie fakt o konkrétnej organizácii",
          "organization": "pre uvedenú organizáciu", "dataset": "pre uvedený dataset"}
SCOPE_RANK = {"occupation": 0, "sector": 1, "dataset": 2, "organization": 3}
BLOCKS = {"none": "nič", "contact_choice": "výber správneho adresáta", "data_request": "žiadosť o dáta",
          "duration_analysis": "analýzu trvania a meškania", "prediction": "predikciu",
          "quantified_savings_claim": "tvrdenie o konkrétnej úspore"}
TARGETS = {"customer": "zákazník", "data_holder": "držiteľ dát", "expert": "expert", "any": "ktokoľvek"}


def norm_question(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", fold(text or "").lower()).strip()


# ======================================================================= domény a priority
def gate0_domains(cfg: Config, db: DB) -> dict[str, dict[str, Any]]:
    """Domény hypotéz na Bráne 0 (téma, inak hypotéza) so silou dôkazov na automatické poradie."""
    from .strength import evidence_strength
    from .value import domain_of

    out: dict[str, dict[str, Any]] = {}
    for h in db.automation_hypotheses():  # M39/M40: vynechané hypotézy nedostanú automatický doménový výskum
        key, title, ids = domain_of(db, h["id"])
        _, verified, _, domains = evidence_strength(db.evidence_for(h["id"]))
        d = out.setdefault(key, {"key": key, "title": title, "hypotheses": [], "score": (0, 0)})
        d["hypotheses"].append(h["id"])
        d["score"] = max(d["score"], (domains, verified))
    return out


def sync_priorities(cfg: Config, db: DB) -> list[dict[str, Any]]:
    """Zaeviduje domény Brány 0 a automaticky zaradí prvú dávku (`initial_priority_domain_count`) podľa sily dôkazov.
    Ručná voľba (zapnutá aj vypnutá) má prednosť; priorita riadi poradie výpočtu, nie právo osloviť kontakt."""
    domains = gate0_domains(cfg, db)
    states = {r["domain_key"]: r for r in db.research_list()}
    manual_on = [k for k, r in states.items() if r["priority_source"] == "manual" and r["priority"]]
    free = max(0, cfg.domain_research.initial_priority_domain_count)  # ručne zaradené domény sú navyše
    ranked = sorted((d for k, d in domains.items() if k not in manual_on and
                     (states.get(k) is None or states[k]["priority_source"] != "manual_off")),
                    key=lambda d: (-d["score"][0], -d["score"][1], d["key"]))
    auto = {d["key"] for d in ranked[:free]}
    for key, d in domains.items():
        row = states.get(key)
        if row is not None and row["priority_source"] in ("manual", "manual_off"):
            db.research_upsert(key, d["title"])
            continue
        prio = 1 if key in auto else 0
        fields: dict[str, Any] = {"priority": prio, "priority_source": "auto"}
        if prio and (row is None or row["status"] == "not_requested"):
            fields.update(status="queued", requested_at=utcnow())
        db.research_upsert(key, d["title"], **fields)
    return [dict(r) | {"hypotézy": domains.get(r["domain_key"], {}).get("hypotheses", [])} for r in db.research_list()]


def set_priority(db: DB, key: str, title: str, on: bool) -> None:
    row = db.research_get(key)
    fields: dict[str, Any] = {"priority": int(on), "priority_source": "manual" if on else "manual_off"}
    if on and (row is None or row["status"] == "not_requested"):
        fields.update(status="queued", requested_at=utcnow())
    db.research_upsert(key, title, **fields)


def request_research(db: DB, key: str, title: str) -> None:
    """Výslovná žiadosť (tlačidlo „Obnoviť doménové podklady“): nový pokus aj pri rovnakej verzii vstupu."""
    db.research_upsert(key, title, status="queued", attempts=0, requested_at=utcnow(), stop_reason=None)


def attempt_allowed(cfg: Config, db: DB, key: str, input_hash: str) -> tuple[bool, str]:
    """Smie sa pre túto verziu vstupu skúsiť nový pokus? (Nekonečné opakovanie toho istého pokusu nie.)"""
    row = db.research_get(key)
    if row is None or row["input_hash"] != input_hash:
        return True, "nová verzia vstupu"
    if row["status"] == "queued" and row["attempts"] == 0:
        return True, "výslovná žiadosť"
    if row["attempts"] < cfg.domain_research.max_attempts_per_input_version:
        return True, "ďalší pokus v limite"
    return False, "rovnaká verzia vstupu už mala pokus – čaká na nové podklady alebo tvoju žiadosť"


def start_attempt(db: DB, key: str, title: str, input_hash: str) -> None:
    row = db.research_get(key)
    attempts = (row["attempts"] if row is not None and row["input_hash"] == input_hash else 0) + 1
    db.research_upsert(key, title, status="researching", input_hash=input_hash, attempts=attempts, started_at=utcnow())


def finish_attempt(db: DB, key: str, title: str, status: str, stop_reason: str, next_step: str,
                   coverage: dict[str, Any] | None = None, stats: dict[str, Any] | None = None) -> None:
    fields: dict[str, Any] = {"status": status, "stop_reason": stop_reason, "next_step": next_step,
                              "finished_at": utcnow()}
    if coverage is not None:
        fields["coverage_json"] = coverage
    if stats is not None:
        fields["stats_json"] = stats
    db.research_upsert(key, title, **fields)


# ======================================================================= pokrytie, tvrdenia, otázky
def coverage(db: DB, key: str) -> tuple[dict[str, str], str]:
    """Pokrytie častí (podložené / hypotéza / len otázka / chýba) a stav podľa neho – nie podľa počtu slov či zdrojov.
    „Pripravené“ vyžaduje proces a rozhodnutie (úkon) aspoň ako hypotézu, žiadnu prázdnu časť a aspoň dve podložené
    časti – jedna podložená dátová položka alebo otázka bez obsahu nevytvorí dojem hotového prehľadu."""
    claims = db.domain_claims(key)
    questions = db.domain_questions(key, "open")
    out = {}
    for part in PARTS:
        mine = [c for c in claims if c["part"] == part]
        if any(c["status"] in SUPPORTED for c in mine):
            out[part] = "podložené"
        elif any(c["status"] == "hypothesis" for c in mine):
            out[part] = "hypotéza"
        elif any(q["part"] == part for q in questions):
            out[part] = "len otázka"
        else:
            out[part] = "chýba"
    supported = sum(1 for v in out.values() if v == "podložené")
    core_ok = all(out[p] in ("podložené", "hypotéza") for p in CORE_PARTS)
    if core_ok and all(v in ("podložené", "hypotéza") for v in out.values()) and supported >= 2:
        status = "ready"
    elif supported:
        status = "partial"
    else:
        status = "needs_research"
    return out, status


def _scope(db: DB, refs: dict[str, list[int]]) -> str:
    scopes = []
    for eid in refs.get("evidence") or []:
        ev = db.get_domain_evidence(eid)
        if ev is not None:
            scopes.append(ev["scope"])
    if refs.get("claims"):
        scopes.append("sector")  # tvrdenia hypotéz sú z verejných zdrojov o odvetví
    return min(scopes, key=lambda s: SCOPE_RANK.get(s, 0)) if scopes else "sector"


def persist_brief(db: DB, key: str, brief_id: int, brief: dict[str, Any], moved: list[tuple[str, str]]) -> dict[str, int]:
    """Tvrdenia a otázky novej verzie prehľadu. Výpovede kontaktov a pozorovania v dátach ostávajú; zodpovedané
    otázky sa zachovajú (zhoda podľa textu), nezodpovedané otázky staršej verzie sa označia ako nahradené."""
    db.deactivate_brief_claims(key)
    stats = {"tvrdenia": 0, "hypotézy": 0, "otázky": 0}

    def claim(part: str, item: dict[str, Any]) -> None:
        text = (item.get("text") or "").strip()
        if not text:
            return
        refs = {"claims": item.get("claims") or [], "evidence": item.get("evidence") or []}
        if not refs["claims"] and not refs["evidence"]:
            # bez odkazu nie je zdrojový fakt: pri dátach návrh požadovaného vstupu, inak hypotéza na overenie
            status = "proposal" if part == "data" else "hypothesis"
            db.add_domain_claim(key, part, text, status, refs, applicability=(
                "požadovaný vstup – nie zdrojový fakt o dostupnosti dát" if part == "data" else "predpoklad bez zdroja – overiť"),
                brief_id=brief_id)
            stats["hypotézy"] += 1
            return
        scope = _scope(db, refs)
        db.add_domain_claim(key, part, text, "source_fact", refs, scope=scope, applicability=SCOPES[scope],
                            brief_id=brief_id)
        stats["tvrdenia"] += 1

    for step in brief.get("process_steps") or []:
        claim("process", step)
    for role in (brief.get("roles") or {}).values():
        claim("roles", role)
    claim("decision", brief.get("decision") or {})
    claim("solutions", brief.get("current_practice") or {})
    for item in brief.get("existing_solutions") or []:
        claim("solutions", item)
    for item in brief.get("alternative_explanations") or []:
        claim("alternatives", item)
    for item in brief.get("data_needed") or []:
        claim("data", {"text": f"{item.get('field', '')}: {item.get('meaning_pitfall', '')}".strip(": "),
                       "claims": item.get("claims"), "evidence": item.get("evidence")})
    unknown_ids = []
    for part, text in moved:
        cid = db.add_domain_claim(key, part, text, "hypothesis", {"claims": [], "evidence": []},
                                  applicability="predpoklad bez zdroja – overiť", brief_id=brief_id)
        unknown_ids.append((cid, part, text))
        stats["hypotézy"] += 1

    seen: set[int] = set()
    existing = db.domain_questions(key)

    def question(text: str, kind: str, part: str | None, **fields: Any) -> None:
        text = (text or "").strip()
        if not text:
            return
        norm = norm_question(text)
        match = next((q for q in existing if q["id"] not in seen and (q["norm"] == norm or fuzz.token_set_ratio(
            q["norm"], norm) >= 92)), None)
        if match is not None:
            seen.add(match["id"])
            update = {"brief_id": brief_id}
            if match["status"] == "superseded":
                update["status"] = "open"
            db.update_domain_question(match["id"], reason="v novej verzii prehľadu" if update.get("status") else "",
                                      **update)
            return
        qid = db.add_domain_question(key, text, norm, kind, brief_id=brief_id, part=part, **fields)
        seen.add(qid)
        stats["otázky"] += 1

    for q in brief.get("falsifying_questions") or []:
        question(q.get("question", ""), "falsifying", None, aspect=q.get("aspect"), target_role="customer",
                 blocks_action="none", priority=1 if q.get("aspect") == "problem_exists" else 2)
    for q in brief.get("open_questions") or []:
        question(q.get("question", ""), "open", q.get("about") if q.get("about") in PARTS else None,
                 target_role=q.get("target_role") if q.get("target_role") in TARGETS else "any",
                 blocks_action=q.get("blocks_action") if q.get("blocks_action") in BLOCKS else "none",
                 priority=2)
    for cid, part, text in unknown_ids:
        short = re.sub(r"^Predpoklad bez zdroja \([^)]*\):\s*", "", text)
        question(f"Platí vo vašej praxi: {short.rstrip('.')}?", "open", part if part in PARTS else None,
                 target_role="any", blocks_action="none", priority=3, unknown_claim_id=cid)
    for q in existing:
        if q["id"] not in seen and q["status"] == "open":
            db.update_domain_question(q["id"], reason="nie je v novej verzii prehľadu", status="superseded")
    db.conn.commit()
    return stats


def main_question(db: DB, key: str, kind: str = "customer"):
    """Jedna hlavná otázka do prvého oslovenia (zvyšok ostáva v príprave rozhovoru): otvorená otázka pre túto rolu,
    najvyššia priorita; inak vyvracajúca otázka „problém vzniká“."""
    open_q = [q for q in db.domain_questions(key, "open") if q["target_role"] in (kind, "any", None)]
    if kind == "customer":
        first = next((q for q in open_q if q["aspect"] == "problem_exists"), None)
        if first is not None:
            return first
    if kind == "data_holder":
        data_q = [q for q in open_q if q["blocks_action"] in ("data_request", "duration_analysis", "prediction")]
        if data_q:
            return sorted(data_q, key=lambda q: (q["priority"], q["id"]))[0]
    return sorted(open_q, key=lambda q: (q["priority"], q["id"]))[0] if open_q else None


def interview_questions(db: DB, key: str, kind: str = "customer", skip: int | None = None, limit: int = 6) -> list:
    rows = [q for q in db.domain_questions(key, "open") if q["target_role"] in (kind, "any", None) and q["id"] != skip]
    return sorted(rows, key=lambda q: (q["priority"], q["kind"] != "falsifying", q["id"]))[:limit]


def record_answer(db: DB, question_id: int, answer: str, source: str, *, outcome_id: int | None = None,
                  organization_id: int | None = None, contradicts: bool = False) -> int | None:
    """Odpoveď na otázku domény (napr. z výsledku oslovenia): otázka sa uzavrie s históriou a vznikne tvrdenie
    „uviedol kontakt“ (alebo sa súvisiace tvrdenie označí ako vyvrátené). Výpoveď nie je meraný výsledok."""
    q = db.get_domain_question(question_id)
    answer = (answer or "").strip()
    if q is None or not answer:
        return None
    db.update_domain_question(question_id, reason=source, status="answered", answer=answer, answer_source=source,
                              answered_at=utcnow(), **({"outcome_id": outcome_id} if outcome_id else {}))
    part = q["part"] or "other"
    if q["unknown_claim_id"]:
        db.update_domain_claim(q["unknown_claim_id"], reason=f"odpoveď: {source}",
                               status="contradicted" if contradicts else "contact_reported")
    cid = db.add_domain_claim(q["domain_key"], part, f"{q['question']} → {answer}",
                              "contradicted" if contradicts else "contact_reported", {"claims": [], "evidence": []},
                              scope="organization" if organization_id else "sector",
                              applicability=SCOPES["organization"] if organization_id else "výpoveď kontaktu",
                              organization_id=organization_id, reasoning_note=source)
    db.conn.commit()
    return cid


def is_stale(db: DB, hid: int) -> bool:
    """Podklady prehľadu sa zmenili od poslednej verzie (stará karta nie je nepravdivá, len to treba vidieť)."""
    from .value import brief_inputs

    key, *_, digest = brief_inputs(db, hid)
    latest = db.latest_brief(key)
    return latest is not None and latest["inputs_hash"] != digest


def answered_signals(db: DB, key: str) -> list[str]:
    """Zodpovedané otázky domény ako vstup novej verzie prehľadu (bez mien – odpoveď zapisuje človek)."""
    from .privacy import mask_text

    return [f"Q: {q['question']} A: {mask_text(q['answer'] or '')[:200]}" for q in db.domain_questions(key, "answered")]
