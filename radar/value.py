"""Prehľad domény a návrh prínosu pre oslovenia (audit 15. 10. 2026, balík B).

Štyri vrstvy, ktoré sa nesmú zlievať: (1) fakt o segmente – doložený citátom, (2) fakt o organizácii alebo osobe –
z jej vlastného zverejnenia, (3) hypotéza potreby – z pracovnej pozície ani segmentu ju ešte nevieme, (4) návrh
pomoci – malý dodateľný výstup, ak je hypotéza relevantná.

Prehľad domény (DomainBrief) – ~5 minút čítania, zdieľaný kontaktmi domény (téma, inak hypotéza): proces (≤ 5 krokov),
5 pojmov, používateľ / správca dát / rozhodovateľ, rozhodnutie, dnešný postup a riešenia, potrebné dáta a úskalia ich
významu, 3 otázky, ktoré môžu nápad vyvrátiť (problém vzniká / nie je už vyriešený / dáta umožnia zlepšenie), neznáme.
Tvrdenie bez citátu kód presunie medzi neznáme (medzery sa nevypĺňajú). Nová verzia vznikne iba pri zmene podkladov
(hash vstupov: tvrdenia, karty, ciele kontaktov, výsledky oslovení); staršie verzie ostávajú.

Návrh prínosu (ValueProposal) – pre typ adresáta (zákazník, držiteľ dát, expert) najviac `outreach.value_proposals_max`
malých výstupov: hypotéza potreby (stav hypothesis, kým ju nepotvrdí odpoveď), výstup, rozhodnutie, ktoré podporí,
jeden minimálny vstup, náročnosť, obmedzenia. Kód overí povinné polia, citáty, čísla, sľuby a primeranosť požiadavky;
hodnotiteľ (rola reviewer) významovú oporu a realizovateľnosť. Konkrétny záväzok (rozsah, termín) potvrdí iba človek.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .cards import card_text
from .config import Config
from .db import DB
from .models import as_router
from .prompts import DOMAIN_BRIEF_CHECK_SYSTEM, DOMAIN_BRIEF_SYSTEM, VALUE_CHECK_SYSTEM, VALUE_PROPOSAL_SYSTEM
from .strength import is_vendor
from .util import Deadline
from .verify import numbers_missing_in_source

log = logging.getLogger(__name__)

KINDS = ("customer", "data_holder", "expert")
KIND_TEXT = {"customer": "potential user or process owner in an affected organization",
             "data_holder": "holder or steward of the data (often a public body publishing a dataset) – not a buyer",
             "expert": "domain expert (research, association, consultancy) – not a buyer"}
ASPECTS = ("problem_exists", "already_solved", "data_enables")
ASPECT_LABEL = {"problem_exists": "vzniká problém?", "already_solved": "nie je už vyriešený?",
                "data_enables": "umožnia dáta zlepšenie?"}
MAX_EVIDENCE = 25
PROMISE_RE = re.compile(r"\bgarant|\bzaruč|\bsľubuj|\bistotou ušetr|\d+\s*%", re.I)
BIG_ASK_RE = re.compile(r"cel[úa] databáz|všetk\w* (dáta|údaje|záznamy)|kompletn\w* export|full database|entire database",
                        re.I)
BUYER_RE = re.compile(r"\bkúp|\bcen[auy]\b|\bplaten|licenci|predplat", re.I)


# ======================================================================= modely výstupov
class Cited(BaseModel):
    text: str = ""
    claims: list[int] = Field(default_factory=list)


class Term(BaseModel):
    term: str = ""
    explanation: str = ""
    claims: list[int] = Field(default_factory=list)


class Roles(BaseModel):
    user: Cited = Field(default_factory=Cited)
    data_steward: Cited = Field(default_factory=Cited)
    decision_maker: Cited = Field(default_factory=Cited)


class DataItem(BaseModel):
    field: str = ""
    meaning_pitfall: str = ""
    claims: list[int] = Field(default_factory=list)


class Question(BaseModel):
    aspect: str = "problem_exists"
    question: str = ""


class DomainBriefOut(BaseModel):
    process_steps: list[Cited] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    decision: Cited = Field(default_factory=Cited)
    current_practice: Cited = Field(default_factory=Cited)
    data_needed: list[DataItem] = Field(default_factory=list)
    falsifying_questions: list[Question] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class ReviewProblem(BaseModel):
    sentence: str = ""
    issue: str = ""


class ReviewOut(BaseModel):
    verdict: Literal["ok", "problems"] = "ok"
    problems: list[ReviewProblem] = Field(default_factory=list)


class ValueItem(BaseModel):
    need_statement: str = ""
    evidence_refs: list[int] = Field(default_factory=list)
    deliverable: str = ""
    decision_supported: str = ""
    minimum_input: str = ""
    recipient_effort: str = ""
    delivery_effort: str = ""
    dependencies: str = ""
    limitations: str = ""
    benefit_hypothesis: str = ""
    success_metric: str = ""
    baseline: str = ""
    savings_estimate: str = ""
    estimate_inputs: str = ""
    estimate_method: str = ""


class ValueOut(BaseModel):
    items: list[ValueItem] = Field(default_factory=list)


# ======================================================================= doména a podklady
def domain_of(db: DB, hid: int) -> tuple[str, str, list[int]]:
    """(kľúč domény, názov, hypotézy domény): téma hypotézy, inak hypotéza sama."""
    h = db.get_hypothesis(hid)
    if h is not None and h["theme_id"]:
        theme = db.get_theme(h["theme_id"])
        members = [m["id"] for m in db.theme_members(h["theme_id"])]
        if theme is not None and members:
            return f"theme:{theme['id']}", theme["name"] or h["title"], [hid] + [m for m in members if m != hid]
    return f"hypothesis:{hid}", h["title"] if h is not None else f"H{hid}", [hid]


def _evidence(db: DB, ids: list[int]) -> list:
    """Overené tvrdenia domény bez dodávateľov (najprv hlavnej hypotézy), najviac MAX_EVIDENCE."""
    out, seen = [], set()
    for hid in ids:
        for e in db.evidence_for(hid):
            if e["id"] in seen or e["status"] != "verified" or is_vendor(e):
                continue
            seen.add(e["id"])
            out.append(e)
    return out[:MAX_EVIDENCE]


def evidence_text(evidence: list) -> str:
    return "\n".join(f"[{e['id']}] {e['problem']} „{e['quote']}“ ({e['domain']})" for e in evidence)


def _cards(db: DB, ids: list[int]) -> str:
    out = []
    for hid in ids[:5]:
        row = db.latest_card(hid)
        if row is not None:
            out.append(f"H{hid}:\n" + card_text(json.loads(row["card_json"] or "{}")))
    return "\n\n".join(out)


def _need_signals(db: DB, ids: list[int]) -> list[str]:
    """Výsledky oslovení, ktoré menia stav potreby (potvrdený / vyvrátený problém) – vstup prehľadu aj návrhov."""
    out = []
    for hid in ids:
        for o in db.outcomes_for(hid):
            if o["problem_relevance"] in ("confirmed", "refuted"):
                out.append(f"H{hid}: {o['problem_relevance']} – {(o['note'] or '')[:200]}")
    return out


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]


def brief_inputs(db: DB, hid: int) -> tuple[str, str, list[int], list, str, list[str], str]:
    key, title, ids = domain_of(db, hid)
    evidence = _evidence(db, ids)
    cards = _cards(db, ids)
    signals = _need_signals(db, ids)
    roles = sorted({r for h in ids for t in db.contact_targets_for(h) for r in json.loads(t["roles_json"] or "[]")})
    digest = _hash([(e["id"], e["quote"]) for e in evidence], cards, signals, roles)
    return key, title, ids, evidence, cards, signals, digest


# ======================================================================= prehľad domény
def code_check_brief(out: DomainBriefOut, allowed: set[int]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Kontroly kódom: počty častí, platné citáty; tvrdenie bez citátu sa presunie medzi neznáme. Vráti
    (prehľad na uloženie, kontroly)."""
    problems: list[str] = []
    moved: list[str] = []
    unknowns = [u.strip() for u in out.unknowns if u.strip()]

    def cited(item: Cited, label: str) -> dict[str, Any]:
        claims = [c for c in item.claims if c in allowed]
        text = item.text.strip()
        if text and not claims:
            moved.append(label)
            unknowns.append(f"Predpoklad bez zdroja ({label}): {text}")
            return {"text": "", "claims": []}
        return {"text": text, "claims": claims}

    steps = [cited(s, f"krok {i + 1}") for i, s in enumerate(out.process_steps[:5])]
    steps = [s for s in steps if s["text"]]
    if len(out.process_steps) > 5:
        problems.append(f"proces má {len(out.process_steps)} krokov (najviac 5) – ponechaných prvých 5")
    terms = [{"term": t.term.strip(), "explanation": t.explanation.strip(), "claims": [c for c in t.claims if c in allowed]}
             for t in out.terms if t.term.strip()][:5]
    if len(terms) != 5:
        problems.append(f"pojmov je {len(terms)} (treba 5)")
    roles = {k: cited(getattr(out.roles, k), label) for k, label in
             (("user", "kto pracuje s problémom"), ("data_steward", "kto spravuje dáta"),
              ("decision_maker", "kto rozhoduje o rozpočte"))}
    data = []
    for d in out.data_needed[:5]:
        if d.field.strip():
            data.append({"field": d.field.strip(), "meaning_pitfall": d.meaning_pitfall.strip(),
                         "claims": [c for c in d.claims if c in allowed]})
    questions = []
    for aspect in ASPECTS:
        q = next((x for x in out.falsifying_questions if x.aspect == aspect and x.question.strip()), None)
        if q is None:
            problems.append(f"chýba otázka „{ASPECT_LABEL[aspect]}“")
        else:
            questions.append({"aspect": aspect, "question": q.question.strip()})
    brief = {"process_steps": steps, "terms": terms, "roles": roles,
             "decision": cited(out.decision, "rozhodnutie"), "current_practice": cited(out.current_practice, "dnešný postup"),
             "data_needed": data, "falsifying_questions": questions, "unknowns": unknowns}
    n_words = len(re.findall(r"\w+", json.dumps(brief, ensure_ascii=False)))
    checks = {"problémy": problems, "presunuté_medzi_neznáme": moved, "slová": n_words,
              "pojmy_bez_zdroja": sum(1 for t in terms if not t["claims"])}
    if n_words > 1100:
        checks["upozornenie"] = f"prehľad má ~{n_words} slov (cieľ 500–700)"
    return brief, checks


def review(cfg: Config, router, task: str, system: str, content: str, object_type: str, object_id: int | None,
           **fmt: Any) -> dict[str, Any]:
    """Kontrola hodnotiteľom; zlyhanie = čaká na hodnotenie (nikdy nie automatické schválenie)."""
    res = router.run_task(task, [{"role": "system", "content": system.format(lang=cfg.pipeline.output_language, **fmt)},
                                 {"role": "user", "content": content}], ReviewOut, object_type=object_type,
                          object_id=object_id, max_tokens=900)
    if not res.ok:
        return {"verdict": "pending", "problems": [], "model": None}
    return {"verdict": res.value.verdict, "problems": [p.model_dump() for p in res.value.problems],
            "model": getattr(res, "model", None)}


def brief_status(checks: dict[str, Any]) -> str:
    rev = checks.get("hodnotiteľ") or {}
    if rev.get("verdict") == "pending":
        return "pending_review"
    if checks.get("problémy") or (rev.get("verdict") == "problems" and rev.get("problems")):
        return "needs_review"
    return "ok"


def ensure_brief(cfg: Config, db: DB, llm: Any, hid: int, reason: str | None = None, force: bool = False) -> int | None:
    """Platný prehľad domény hypotézy; generuje sa iba pri zmene podkladov (alebo `force`). Vráti id prehľadu."""
    key, title, ids, evidence, cards, signals, digest = brief_inputs(db, hid)
    latest = db.latest_brief(key)
    if latest is not None and latest["inputs_hash"] == digest and not force:
        return int(latest["id"])
    if not evidence and not cards:
        return int(latest["id"]) if latest is not None else None
    router = as_router(llm, cfg, db)
    user = (f"DOMAIN: {title}\n\nPROBLEM CARDS:\n<untrusted_document>\n{cards or '-'}\n</untrusted_document>\n\n"
            f"EVIDENCE:\n<untrusted_document>\n{evidence_text(evidence) or '-'}\n</untrusted_document>"
            + ("\n\nREPLIES FROM CONTACTS (need confirmed/refuted):\n<untrusted_document>\n" + "\n".join(signals)
               + "\n</untrusted_document>" if signals else ""))
    res = router.run_task("domain_brief", [{"role": "system", "content": DOMAIN_BRIEF_SYSTEM.format(
        lang=cfg.pipeline.output_language)}, {"role": "user", "content": user}], DomainBriefOut,
        object_type="hypothesis", object_id=hid, max_tokens=3000)
    if not res.ok:
        return int(latest["id"]) if latest is not None else None
    allowed = {e["id"] for e in evidence}
    brief, checks = code_check_brief(res.value, allowed)
    checks["hodnotiteľ"] = review(cfg, router, "domain_brief_check", DOMAIN_BRIEF_CHECK_SYSTEM,
                                  f"EVIDENCE:\n<untrusted_document>\n{evidence_text(evidence)}\n</untrusted_document>\n\n"
                                  f"BRIEF:\n<untrusted_document>\n{json.dumps(brief, ensure_ascii=False)}\n</untrusted_document>",
                                  "hypothesis", hid)
    used = sorted({c for part in (brief["process_steps"], brief["terms"], brief["data_needed"],
                                  list(brief["roles"].values()), [brief["decision"], brief["current_practice"]])
                   for item in part for c in item.get("claims", [])})
    sources = [{"claim_id": e["id"], "url": e["url"], "domain": e["domain"], "stiahnuté": (e["fetched_at"] or "")[:10]}
               for e in evidence if e["id"] in used]
    return db.add_brief(key, title, brief, sources, digest, checks, brief_status(checks), getattr(res, "model", None),
                        reason or ("prvá verzia" if latest is None else "zmena podkladov"))


def brief_markdown(row) -> str:
    """Prehľad domény ako Markdown (GUI a export)."""
    b = json.loads(row["brief_json"] or "{}")
    src = {s["claim_id"]: s for s in json.loads(row["sources_json"] or "[]")}

    def refs(claims: list[int]) -> str:
        return " " + " ".join(f"[{c}]" for c in claims) if claims else ""

    lines = [f"# {row['title']} – prehľad domény (verzia {row['version']}, {row['created_at'][:10]}, stav {row['status']})",
             "", "## Proces"]
    lines += [f"{i + 1}. {s['text']}{refs(s['claims'])}" for i, s in enumerate(b.get("process_steps") or [])] or ["- chýba"]
    lines += ["", "## Pojmy"]
    lines += [f"- **{t['term']}** – {t['explanation']}{refs(t['claims'])}" for t in b.get("terms") or []]
    lines += ["", "## Kto"]
    for k, label in (("user", "Pracuje s problémom"), ("data_steward", "Spravuje dáta"), ("decision_maker", "Rozhoduje o rozpočte")):
        item = (b.get("roles") or {}).get(k) or {}
        lines.append(f"- {label}: {item.get('text') or 'neznáme'}{refs(item.get('claims') or [])}")
    for k, label in (("decision", "Rozhodnutie, ktoré má výstup podporiť"), ("current_practice", "Dnešný postup a riešenia")):
        item = b.get(k) or {}
        lines += ["", f"## {label}", (item.get("text") or "neznáme") + refs(item.get("claims") or [])]
    lines += ["", "## Potrebné dáta a úskalia významu"]
    lines += [f"- {d['field']}: {d['meaning_pitfall']}{refs(d['claims'])}" for d in b.get("data_needed") or []] or ["- chýba"]
    lines += ["", "## Otázky, ktoré môžu nápad vyvrátiť"]
    lines += [f"- ({ASPECT_LABEL.get(q['aspect'], q['aspect'])}) {q['question']}" for q in b.get("falsifying_questions") or []]
    lines += ["", "## Neznáme a predpoklady na overenie"]
    lines += [f"- {u}" for u in b.get("unknowns") or []] or ["- žiadne uvedené"]
    lines += ["", "## Zdroje"]
    lines += [f"- [{c}] {s['url']} (stiahnuté {s['stiahnuté']})" for c, s in src.items()] or ["- žiadne"]
    return "\n".join(lines) + "\n"


# ======================================================================= návrh prínosu
def policy_version(cfg: Config) -> str:
    return _hash(cfg.outreach.value_offer.model_dump())[:12]


def code_check_value(item: ValueItem, kind: str, allowed: set[int], allowed_text: str,
                     estimate_inputs: str) -> tuple[dict[str, Any], list[str]]:
    """Kontroly kódom; vráti (uložený návrh, problémy). Potreba je vždy hypotéza, kým ju nepotvrdí odpoveď."""
    data = item.model_dump()
    data["evidence_refs"] = [c for c in item.evidence_refs if c in allowed]
    problems = []
    for key, label in (("need_statement", "hypotéza potreby"), ("deliverable", "výstup"),
                       ("decision_supported", "podporené rozhodnutie"), ("minimum_input", "minimálny vstup")):
        if not (data.get(key) or "").strip():
            problems.append(f"chýba {label}")
    if not estimate_inputs and (data.get("savings_estimate") or "").strip():
        data["savings_estimate"] = ""  # odhad bez dohľadateľných vstupov sa nesmie objaviť v texte
        problems.append("odhad úspory bez vstupov – odstránený (prínos je kvalitatívny)")
    if data.get("savings_estimate") and not (data.get("estimate_inputs") and data.get("estimate_method")):
        problems.append("odhad úspory potrebuje vstupy a spôsob výpočtu")
    text = " ".join(str(data.get(k) or "") for k in ("need_statement", "deliverable", "decision_supported",
                                                       "benefit_hypothesis", "minimum_input", "success_metric"))
    if PROMISE_RE.search(text):
        problems.append("sľub (garancia, percentá) – prínos opíš ako hypotézu a meranie pri skúške")
    missing = numbers_missing_in_source(text, allowed_text + " " + estimate_inputs)
    if missing:
        problems.append("čísla bez podkladu: " + ", ".join(missing[:5]))
    if BIG_ASK_RE.search(data.get("minimum_input") or ""):
        problems.append("požiadavka je priveľká – stačí potvrdenie relevantnosti, vysvetlenie polí alebo malá vzorka")
    if kind in ("data_holder", "expert") and BUYER_RE.search(text):
        problems.append("adresát nie je nákupca – bez ceny, platenia a licencií")
    data["need_status"] = "hypothesis"
    return data, problems


def proposal_inputs(cfg: Config, db: DB, hid: int, kind: str, brief_id: int | None) -> tuple[str, list, str, str]:
    """(text podkladov pre model, citáty, vstupy odhadu, hash)."""
    from .progress import business_state

    _, title, ids, evidence, cards, signals, _ = brief_inputs(db, hid)
    brief = db.get_brief(brief_id) if brief_id else None
    targets = [t for t in db.contact_targets_for(hid) if t["kind"] == kind]
    target_txt = "\n".join(f"- {t['who']}: roles {', '.join(json.loads(t['roles_json'] or '[]'))}; ask: {t['ask'] or ''}"
                           for t in targets) or "-"
    fields = ", ".join(f"{f['name']} ({f['status']})" for f in db.fields_for(hid) if f["is_key"]) or "-"
    business = business_state(db, hid)
    measured = business["nameraná_úspora"]
    estimate = measured.get("dôkaz", "") if measured.get("hotové") else ""
    text = (f"PROBLEM: {title}\nRECIPIENT TYPE: {kind}\nCONTACT TARGETS:\n{target_txt}\nKEY DATA FIELDS: {fields}\n\n"
            f"PROBLEM CARD:\n<untrusted_document>\n{cards or '-'}\n</untrusted_document>\n\n"
            f"DOMAIN BRIEF:\n<untrusted_document>\n{brief['brief_json'] if brief is not None else '-'}\n</untrusted_document>\n\n"
            f"EVIDENCE:\n<untrusted_document>\n{evidence_text(evidence) or '-'}\n</untrusted_document>\n\n"
            f"ESTIMATE INPUTS: {estimate or 'none'}"
            + ("\nREPLIES: " + "; ".join(signals) if signals else ""))
    return text, evidence, estimate, _hash(text, policy_version(cfg))


def ensure_value_proposals(cfg: Config, db: DB, llm: Any, hid: int, kind: str, brief_id: int | None,
                           force: bool = False) -> Counter:
    """Návrhy prínosu pre typ adresáta; nové iba pri zmene podkladov. Schválené záväzky človeka ostávajú."""
    stats: Counter = Counter()
    text, evidence, estimate, digest = proposal_inputs(cfg, db, hid, kind, brief_id)
    current = db.value_proposals_for(hid, kind)
    if current and not force and all(p["inputs_hash"] == digest for p in current
                                      if not p["delivery_commitment_approved"]):
        return stats
    router = as_router(llm, cfg, db)
    system = VALUE_PROPOSAL_SYSTEM.format(max_items=cfg.outreach.value_proposals_max, recipient=KIND_TEXT[kind],
                                          lang=cfg.pipeline.output_language)
    res = router.run_task("value_proposal", [{"role": "system", "content": system}, {"role": "user", "content": text}],
                          ValueOut, object_type="hypothesis", object_id=hid, max_tokens=2500)
    if not res.ok:
        stats["llm_failures"] += 1
        return stats
    allowed = {e["id"] for e in evidence}
    allowed_text = " ".join(f"{e['problem']} {e['quote']}" for e in evidence)
    items = []
    for item in res.value.items[: cfg.outreach.value_proposals_max]:
        data, problems = code_check_value(item, kind, allowed, allowed_text, estimate)
        items.append((data, problems))
    rev = review(cfg, router, "value_check", VALUE_CHECK_SYSTEM,
                 f"EVIDENCE:\n<untrusted_document>\n{evidence_text(evidence)}\n</untrusted_document>\n\nITEMS:\n"
                 f"<untrusted_document>\n{json.dumps([d for d, _ in items], ensure_ascii=False)}\n</untrusted_document>",
                 "hypothesis", hid, recipient=KIND_TEXT[kind]) if items else {"verdict": "ok", "problems": []}
    db.delete_value_proposals(hid, kind)
    for data, problems in items:
        mine = [p for p in rev.get("problems") or [] if p.get("sentence") and any(
            p["sentence"].strip()[:60] in str(v) for v in data.values())] if rev.get("verdict") == "problems" else []
        status = ("needs_edit" if problems or mine else "pending_review" if rev.get("verdict") == "pending"
                  else "checked")
        db.add_value_proposal(hid, kind, data, need_status="hypothesis", evidence_refs_json=data["evidence_refs"],
                              checks_json={"kód": problems, "hodnotiteľ": {**rev, "problems": mine}}, status=status,
                              commercial_policy_version=policy_version(cfg), inputs_hash=digest,
                              model=getattr(res, "model", None), brief_id=brief_id)
        stats[status] += 1
    return stats


def best_proposal(db: DB, hid: int, kind: str):
    """Návrh pre oslovenie: schválený záväzok, inak skontrolovaný, inak čakajúci na hodnotenie (s označením)."""
    rows = db.value_proposals_for(hid, kind)
    for status in ("approved", "checked", "pending_review"):
        for r in rows:
            if r["status"] == status or (status == "approved" and r["delivery_commitment_approved"]):
                return r
    return None


def approve_commitment(db: DB, proposal_id: int, scope: str) -> None:
    """Človek potvrdí konkrétny záväzok (rozsah, termín, kapacita) – až potom ho smie text sľúbiť."""
    scope = (scope or "").strip()
    if len(scope) < 10:
        raise ValueError("Napíš rozsah a termín, ktoré vieš dodať (napr. „rozbor 20 riadkov do 10 pracovných dní“).")
    row = db.get_value_proposal(proposal_id)
    data = json.loads(row["proposal_json"])
    data["schválený_záväzok"] = scope
    db.update_value_proposal(proposal_id, proposal_json=data, delivery_commitment_approved=1, status="approved")


def recipient_kinds(cfg: Config, db: DB, hid: int) -> list[str]:
    """Typy adresátov hypotézy: zákazník vždy, držiteľ dát a expert iba keď sú medzi kontaktmi alebo v pláne."""
    from .shortlist import contact_shortlist

    groups = {r["skupina"] for r in contact_shortlist(cfg, db, hid)} | {t["kind"] for t in db.contact_targets_for(hid)}
    return [k for k in KINDS if k == "customer" or k in groups]


def run_value(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None = None) -> dict:
    """Krok `value`: prehľad domény a návrhy prínosu pre hypotézy na Bráne 0 (iba pri zmene podkladov)."""
    stats: Counter = Counter()
    hyps = ids or [h["id"] for h in db.people_enabled_hypotheses()]
    done_domains: set[str] = set()
    for hid in hyps:
        if deadline.expired():
            break
        key = domain_of(db, hid)[0]
        before = db.latest_brief(key)
        brief_id = ensure_brief(cfg, db, llm, hid) if key not in done_domains else (before["id"] if before else None)
        done_domains.add(key)
        if brief_id is not None and (before is None or before["id"] != brief_id):
            stats["briefs"] += 1
        for kind in recipient_kinds(cfg, db, hid):
            if deadline.expired():
                break
            stats.update({f"value_{k}": v for k, v in ensure_value_proposals(cfg, db, llm, hid, kind, brief_id).items()})
    return dict(stats)
