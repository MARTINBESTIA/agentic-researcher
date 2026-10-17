"""Použiteľnosť dôkazu voči konkrétnemu cieľu (zadanie 17. 10. 2026, časť A).

Metadáta zdroja (citát, dokument, vydavateľ, dátum získania) ostávajú v `claims` a `documents`; tu sa ukladá **posúdenie
voči cieľu** – jeden zdroj môže byť pre doménu iba kontext a pre kartu organizácie priamy (tabuľka
`evidence_assessments`, kľúč cieľa `domain:<kľúč>` alebo `card:<org>:<hypotéza>:<proces>:<rola>`):

- význam: problém, opis procesu, existujúce riešenie, funkcia produktu, skúsenosť po nasadení, protiargument, kontext;
- relevantnosť: priama / prenositeľná analógia / iba kontext / nesúvisiaca / nejasná – s krátkym odôvodnením;
- stav potreby: aktuálna potvrdená (iba z odpovede kontaktu) / historická / podľa zdroja riešená / neznáma – stav
  v zdroji nie je automaticky aktuálny stav organizácie;
- program a jurisdikcia (eurofondy, LIFE, …), organizácia, o ktorej zdroj hovorí; neznáme dátumy ostávajú prázdne.

Pravidlá kódu dávajú **strop**: postup orgánov s platobnou funkciou pri eurofondoch nie je dôkaz procesu komerčnej
účtovnej kancelárie (zhoda slova „účtovníctvo“ nestačí), referencia používania produktu nie je aktuálna nevyriešená
potreba. Voliteľné spresnenie modelom (úloha `evidence_relevance`) strop neprekročí. Dodávateľ ostáva označený
a jeho citáty idú do skupiny existujúcich riešení (funkcie, integrácie, referencie) – nie do sily dôkazov problému
a jeho deklarovaná úspora sa neprenáša na inú firmu.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .privacy import fold

log = logging.getLogger(__name__)

LOGIC_VERSION = "rel-1"
MEANINGS = {"problem": "problém", "process": "opis procesu", "existing_solution": "existujúce riešenie",
            "product_feature": "funkcia produktu", "post_deployment": "skúsenosť po nasadení",
            "counterargument": "protiargument", "context": "kontext"}
RELEVANCE = {"direct": "priama", "analogy": "prenositeľná analógia", "context": "iba kontext", "unrelated": "nesúvisiaca",
             "unclear": "nejasná"}
RELEVANCE_RANK = {"unrelated": 0, "context": 1, "unclear": 2, "analogy": 3, "direct": 4}
NEED_STATES = {"current_confirmed": "aktuálna potvrdená", "historical": "historická",
               "solved_per_source": "podľa zdroja riešená", "unknown": "neznáma"}
PUBLISHER_LABEL = {"government": "úrad", "media": "médiá", "research": "výskum", "association": "združenie",
                   "vendor": "dodávateľ", "other": "iné", None: "neznámy"}
SOLUTION_MEANINGS = ("existing_solution", "product_feature", "post_deployment")

# Program alebo agenda, ktorá viaže citát na špecifický kontext (iný než bežná firma v segmente).
PROGRAMS = {
    "eurofondy": re.compile(r"eurofond|e[šs]if|\biúi\b|integrovan\w* územn|\bnfp\b|nenávratn\w* finančn|riadiac\w* orgán|"
                            r"sprostredkovateľsk\w* orgán|orgán\w* s platobnou funkciou|platobn\w* jednotk|"
                            r"program\w* slovensko|operačn\w* program|žiadost\w* o platbu|prijímate[lľ]\w* (pomoci|príspevku|nfp)", re.I),
    "LIFE": re.compile(r"\blife\b|program\w* life", re.I),
    "verejné obstarávanie": re.compile(r"verejn\w* obstarávan|\búvo\b|obstarávate[lľ]", re.I),
    "štátny rozpočet": re.compile(r"štátn\w* rozpočt|rozpočtov\w* organizáci|kapitol\w* rozpočtu", re.I),
}
PUBLIC_RE = re.compile(r"\b(obc[eíi]|obec|mest[aoá]|mestsk|kraj|vúc|samospráv|ministerst|úrad|štátn|verejn\w* (správ|inštit|sektor|"
                       r"obstarávate)|orgán|inštitúci|prijímate[lľ]|rozpočtov|kontrolór|nkú)", re.I)
PRIVATE_RE = re.compile(r"(kancelári|firm|podnik|výrobc|predajc|maklér|s\.r\.o|živnost|malé a stredné|\bmsp\b|e-shop|obchod|"
                        r"poradensk|klient|zákazník|reality|realitn|účtovn\w* kancelári)", re.I)
POST_DEPLOYMENT_RE = re.compile(r"po zaveden|po nasaden|odkedy (používa|zaviedl)|nameral|zrýchl|nárast efektivit|ušetr|"
                                r"vďaka (systém|riešeni|aplikáci|nástroj)|zlepšil|odpadá", re.I)
FEATURE_RE = re.compile(r"umožňuj|automatick|integrác|funkci|modul|\bimport|\bexport|napoj|rozpozn|\bocr\b|vyťaž|"
                        r"digitaliz|aplikáci|softvér|systém", re.I)
HISTORICAL_RE = re.compile(r"\b(zistil|zistili|nesplnil|nesplnila|neboli|nebol|bola|bolo|boli|v roku 20\d\d|k \d{1,2}\. ?\d{1,2}\. ?20\d\d)", re.I)


def segment_kind(text: str) -> str:
    """Typ segmentu z textu: public | private | mixed | unknown (bez modelu)."""
    pub, priv = bool(PUBLIC_RE.search(text or "")), bool(PRIVATE_RE.search(text or ""))
    return "mixed" if pub and priv else "public" if pub else "private" if priv else "unknown"


def programs_in(text: str) -> list[str]:
    return [name for name, rx in PROGRAMS.items() if rx.search(text or "")]


class Target(BaseModel):
    """Cieľ posúdenia: doména (prehľad) alebo karta organizácie."""

    key: str
    title: str = ""
    segment: str = ""
    process: str = ""
    org_id: int | None = None
    org_name: str = ""
    org_public: bool | None = None
    programs: list[str] = Field(default_factory=list)  # program cieľa (napr. pri hypotéze o eurofondoch)

    @property
    def kind(self) -> str:
        if self.org_public is not None:
            return "public" if self.org_public else "private"
        return segment_kind(f"{self.segment} {self.title}")


def _publisher(e) -> str | None:
    keys = e.keys()
    typ = e["source_type"] if "source_type" in keys else None
    if typ:
        return typ
    from .publishers import rule_type

    return rule_type(e["domain"] if "domain" in keys else "")


def assess_claim(e, target: Target, org_names: dict[int, str] | None = None) -> dict[str, Any]:
    """Posúdenie jedného tvrdenia voči cieľu pravidlami kódu (strop relevantnosti, význam, stav potreby, program)."""
    from .orgs import name_in

    keys = e.keys()
    quote, problem = e["quote"] or "", e["problem"] or ""
    affected = (e["affected_party"] if "affected_party" in keys else "") or ""
    text = f"{problem} {affected} {quote}"
    publisher = _publisher(e)
    ctype = e["claim_type"] if "claim_type" in keys else "pain"
    if ctype == "existing_solution":
        meaning = ("post_deployment" if POST_DEPLOYMENT_RE.search(quote) else
                   "product_feature" if publisher == "vendor" and FEATURE_RE.search(quote) else "existing_solution")
    elif ctype == "counter":
        meaning = "counterargument"
    elif ctype in ("pain", "cost", "frequency", "demand_signal"):
        meaning = "problem"
    elif ctype == "regulation":
        meaning = "process"
    else:
        meaning = "context"
    if publisher == "vendor" and meaning == "problem":
        meaning = "product_feature" if FEATURE_RE.search(quote) else "existing_solution"  # marketing nie je dôkaz problému
    progs = programs_in(text)
    reasons: list[str] = []
    relevance = "direct"
    target_kind = target.kind
    claim_public = bool(PUBLIC_RE.search(affected)) or bool(progs)
    foreign = [p for p in progs if p not in target.programs]
    if target_kind == "private" and foreign:
        relevance = "context"
        reasons.append(f"program/agenda {', '.join(foreign)} – iný kontext ako segment „{target.segment or target.title}“")
    elif target_kind == "private" and claim_public and not PRIVATE_RE.search(affected):
        relevance = "analogy" if meaning in ("problem", "process") else "context"
        reasons.append(f"dotknutý je verejný subjekt („{affected[:60]}“) – pre firmu v segmente iba analógia")
    elif target_kind == "public" and affected and PRIVATE_RE.search(affected) and not PUBLIC_RE.search(affected):
        relevance = "analogy"
        reasons.append(f"dotknutá je firma („{affected[:60]}“) – pre verejný subjekt iba analógia")
    if meaning in SOLUTION_MEANINGS and relevance == "direct":
        reasons.append("existujúce riešenie / funkcia – pre porovnanie riešení, nie dôkaz aktuálnej potreby")
    org_id = None
    for oid, name in (org_names or {}).items():
        if name and len(fold(name)) >= 4 and (name_in(name, quote) or name_in(name, affected)):
            org_id = oid
            break
    if meaning in ("post_deployment",) or (meaning == "existing_solution" and POST_DEPLOYMENT_RE.search(quote)):
        need = "solved_per_source"
    elif publisher == "government" and HISTORICAL_RE.search(quote):
        need = "historical"
    else:
        need = "unknown"
    retrieved = e["fetched_at"] if "fetched_at" in keys else None
    published = e["effective_date"] if "effective_date" in keys and ctype == "regulation" else None
    return {"publisher_type": publisher, "meaning": meaning, "relevance": relevance,
            "relevance_reason": "; ".join(reasons) or "zodpovedá segmentu a procesu podľa pravidiel kódu",
            "need_state": need, "program": ", ".join(progs) or None, "organization_id": org_id,
            "jurisdiction": "EÚ" if "eurofondy" in progs or "LIFE" in progs else None,
            "published_at": published, "retrieved_at": retrieved}


# ======================================================================= spresnenie modelom (voliteľné)
class RelevanceItem(BaseModel):
    id: int
    relevance: Literal["direct", "analogy", "context", "unrelated", "unclear"] = "unclear"
    meaning: Literal["problem", "process", "existing_solution", "product_feature", "post_deployment", "counterargument",
                     "context"] = "context"
    need_state: Literal["historical", "solved_per_source", "unknown"] = "unknown"
    process: str = ""
    role: str = ""
    reason: str = ""


class RelevanceOut(BaseModel):
    items: list[RelevanceItem] = Field(default_factory=list)


def refine_with_model(cfg: Config, db: DB, router, target: Target, rows: list[tuple[Any, dict[str, Any]]]) -> int:
    """Model spresní relevantnosť a význam; strop kódu neprekročí (iba zníži) a aktuálnu potrebu nikdy nepotvrdí."""
    from .prompts import EVIDENCE_RELEVANCE_SYSTEM, language_name

    if not rows:
        return 0
    lines = "\n".join(f"[{e['id']}] ({PUBLISHER_LABEL.get(a['publisher_type'], a['publisher_type'])}) {e['problem']} "
                      f"„{e['quote']}“ – dotknutý: {e['affected_party'] or '-'}" for e, a in rows)
    res = router.run_task("evidence_relevance", [
        {"role": "system", "content": EVIDENCE_RELEVANCE_SYSTEM.format(lang=language_name(cfg.pipeline.output_language))},
        {"role": "user", "content": f"TARGET: {target.title}\nSEGMENT: {target.segment or '-'}\nPROCESS: {target.process or '-'}\n"
                                    f"ORGANIZATION: {target.org_name or '-'}\n\nQUOTES:\n<untrusted_document>\n{lines}\n"
                                    "</untrusted_document>"}],
        RelevanceOut, object_type="assessment", max_tokens=3000)
    if not res.ok:
        return 0
    by_id = {e["id"]: (e, a) for e, a in rows}
    n = 0
    for item in res.value.items:
        if item.id not in by_id:
            continue
        e, a = by_id[item.id]
        cap = a["relevance"]
        rel = item.relevance if RELEVANCE_RANK[item.relevance] <= RELEVANCE_RANK[cap] else cap
        meaning = a["meaning"] if a["meaning"] in SOLUTION_MEANINGS or a["publisher_type"] == "vendor" else item.meaning
        need = a["need_state"] if a["need_state"] != "unknown" else item.need_state
        db.upsert_assessment(target.key, LOGIC_VERSION, claim_id=e["id"], **{
            **a, "relevance": rel, "meaning": meaning, "need_state": need, "process": item.process[:200] or None,
            "role": item.role[:120] or None, "assessed_by": "model", "model": getattr(res, "model", None),
            "relevance_reason": (a["relevance_reason"] + " · model: " + item.reason)[:400]})
        n += 1
    return n


# ======================================================================= posúdenie a výber
def assess_target(cfg: Config, db: DB, target: Target, claims: list, router=None,
                  org_names: dict[int, str] | None = None) -> list[tuple[Any, dict[str, Any]]]:
    """Posúdi tvrdenia voči cieľu (kód; existujúce posúdenie človeka ostáva) a vráti dvojice (tvrdenie, posúdenie)."""
    stored = {r["claim_id"]: r for r in db.assessments_for(target.key, LOGIC_VERSION) if r["claim_id"]}
    out = []
    fresh = []
    for e in claims:
        row = stored.get(e["id"])
        if row is not None:
            a = {k: row[k] for k in ("publisher_type", "meaning", "relevance", "relevance_reason", "need_state", "program",
                                     "organization_id", "jurisdiction", "published_at", "retrieved_at", "process", "role",
                                     "assessed_by")}
        else:
            a = assess_claim(e, target, org_names)
            db.upsert_assessment(target.key, LOGIC_VERSION, claim_id=e["id"], assessed_by="code", **a)
            a["assessed_by"] = "code"
            fresh.append((e, a))
        out.append((e, a))
    if router is not None and fresh and cfg.org_cards.model_relevance:
        unclear = [(e, a) for e, a in fresh if a["relevance"] in ("direct", "unclear", "analogy")][:20]
        if refine_with_model(cfg, db, router, target, unclear):
            stored = {r["claim_id"]: r for r in db.assessments_for(target.key, LOGIC_VERSION) if r["claim_id"]}
            out = [(e, {**a, **{k: stored[e["id"]][k] for k in ("meaning", "relevance", "relevance_reason", "need_state",
                                                                 "process", "role", "assessed_by")}})
                   if e["id"] in stored else (e, a) for e, a in out]
    db.conn.commit()
    return out


BUCKETS = {"process_problem": "proces a problém", "organization": "o tejto organizácii",
           "solutions": "existujúce riešenia a funkcie (aj dodávatelia)", "counter": "protiargumenty",
           "context": "iba kontext (nie dôkaz pre cieľ)"}


def select_buckets(pairs: list[tuple[Any, dict[str, Any]]], limit: int = 25, per_doc: int = 2,
                   org_id: int | None = None) -> dict[str, list[tuple[Any, dict[str, Any]]]]:
    """Rozdelenie podkladov pre syntézu: proces/problém, organizácia, existujúce riešenia, protiargumenty, kontext.
    Prednosť má relevantnosť a rôznorodosť (najviac `per_doc` citátov z jedného dokumentu – viac citátov jednej
    stránky nie je viac nezávislých potvrdení), nesúvisiace sa vynechajú."""
    buckets: dict[str, list] = defaultdict(list)
    for e, a in sorted(pairs, key=lambda p: (-RELEVANCE_RANK.get(p[1]["relevance"], 0), p[0]["id"])):
        if a["relevance"] == "unrelated":
            continue
        if org_id is not None and a.get("organization_id") == org_id:
            buckets["organization"].append((e, a))
        elif a["meaning"] in SOLUTION_MEANINGS:
            buckets["solutions"].append((e, a))
        elif a["meaning"] == "counterargument":
            buckets["counter"].append((e, a))
        elif a["relevance"] in ("direct", "analogy", "unclear") and a["meaning"] in ("problem", "process"):
            buckets["process_problem"].append((e, a))
        else:
            buckets["context"].append((e, a))
    quota = {"process_problem": 0.45, "organization": 0.2, "solutions": 0.2, "counter": 0.1, "context": 0.05}
    out: dict[str, list] = {}
    per_document: dict[Any, int] = defaultdict(int)
    for name in BUCKETS:
        take = max(1 if buckets.get(name) else 0, round(limit * quota[name]))
        chosen = []
        for e, a in buckets.get(name, []):
            doc = e["document_id"] if "document_id" in e.keys() else e["id"]
            if per_document[doc] >= per_doc:
                continue
            per_document[doc] += 1
            chosen.append((e, a))
            if len(chosen) >= take:
                break
        out[name] = chosen
    return out


def bucket_text(buckets: dict[str, list[tuple[Any, dict[str, Any]]]]) -> str:
    """Podklady pre prompt po skupinách s označením vydavateľa, významu, relevantnosti a stavu potreby."""
    parts = []
    for name, label in BUCKETS.items():
        items = buckets.get(name) or []
        if not items:
            continue
        lines = [f"[{e['id']}] ({PUBLISHER_LABEL.get(a['publisher_type'], a['publisher_type'])}; "
                 f"{MEANINGS.get(a['meaning'], a['meaning'])}; relevantnosť {RELEVANCE.get(a['relevance'], a['relevance'])}; "
                 f"stav potreby v zdroji {NEED_STATES.get(a['need_state'], a['need_state'])}"
                 + (f"; program {a['program']}" if a.get("program") else "") + f") {e['problem']} „{e['quote']}“ "
                 f"({e['domain'] if 'domain' in e.keys() else ''})" for e, a in items]
        parts.append(f"## {label.upper()}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def allowed_for(buckets: dict[str, list], parts: tuple[str, ...]) -> set[int]:
    return {e["id"] for name in parts for e, _ in buckets.get(name) or []}


def assessment_rows(db: DB, target_key: str) -> list[dict[str, Any]]:
    """Posúdenia na zobrazenie (GUI, export)."""
    return [{"tvrdenie": f"C{r['claim_id']}" if r["claim_id"] else f"D{r['domain_evidence_id']}",
             "vydavateľ": PUBLISHER_LABEL.get(r["publisher_type"], r["publisher_type"]),
             "význam": MEANINGS.get(r["meaning"], r["meaning"]), "relevantnosť": RELEVANCE.get(r["relevance"], r["relevance"]),
             "dôvod": r["relevance_reason"] or "", "stav potreby v zdroji": NEED_STATES.get(r["need_state"], r["need_state"]),
             "program": r["program"] or "", "posúdil": r["assessed_by"]}
            for r in db.assessments_for(target_key, LOGIC_VERSION)]


def org_names_for(db: DB, hid: int) -> dict[int, str]:
    return {o["id"]: o["name"] for o in db.orgs_for_hypothesis(hid, include_excluded=True)}


def json_safe(pairs: list[tuple[Any, dict[str, Any]]]) -> str:
    return json.dumps([(e["id"], a["relevance"], a["meaning"]) for e, a in pairs])
