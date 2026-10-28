"""Konkurencia ako dôvod zamietnutia (M39, audit 26. 10. 2026).

Kritik doteraz iba ukladal odkazy z vyhľadávania. Pre hypotézy na Bráne 0 sa posúdia **alternatívy** – aj
nesoftvérové (človek, tabuľka, externá služba, modul ERP, zrušenie procesu): či pokrývajú proces (áno / čiastočne /
nie s citátom), ako sa kupujú, bariéry zavedenia a ktorá časť ostáva nevyriešená (s dôkazom).
Zdroje: overené tvrdenia hypotézy, existujúce riešenia z prehľadu domény, dnešné riešenia z kariet organizácií
a stránky z odkazov kritika na konkurenciu (stiahnuté s robots.txt, začiatok textu).
Model (`alternatives_assess`) iba navrhuje; každý citát musí kód nájsť doslova v zdroji, inak sa tvrdenie zoslabí
na „nedoložené“ (generátor nie je overovateľ, model nič nepovýši). Stav **`no_defensible_gap`** kód **navrhne**,
keď každá alternatíva pokrýva proces doloženým citátom a nevyriešená časť doložená nie je; **potvrdzuje človek**
(súhlas používateľa 26. 10. 2026). Potvrdenú hypotézu lievik a autopilot vynechajú; človek ju môže obnoviť.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .config import Config
from .db import DB
from .models import as_router
from .prompts import ALTERNATIVES_SYSTEM
from .util import Deadline
from .verify import quote_score

log = logging.getLogger(__name__)

KINDS = {"software": "softvér", "person": "človek (ručne)", "spreadsheet": "tabuľka (Excel)",
         "external_service": "externá služba", "erp_module": "modul ERP / účtovného systému",
         "process_elimination": "zrušenie procesu", "other": "iné"}
COVERS = {"yes": "pokrýva proces", "partial": "čiastočne", "no": "nepokrýva", "unknown": "nedoložené"}
PROPOSALS = {"no_defensible_gap": "bez obhájiteľnej medzery – navrhnuté vynechať (potvrdzuje človek)",
             "gap_evidence": "nevyriešená časť je doložená – hypotéza má medzeru",
             "open": "nejasné – alternatívy pokrývajú proces čiastočne alebo bez dôkazu (over v rozhovore)",
             "insufficient": "zdroje nepomenovali žiadnu alternatívu"}
GAP_STATUS = "no_defensible_gap"
MAX_SOURCES, MAX_WEB, WEB_CHARS, MAX_ALTERNATIVES = 24, 4, 2500, 8


class AltOut(BaseModel):
    name: str = ""
    kind: str = "other"
    covers: str = "unknown"
    covers_quote: str = ""
    covers_source: int = -1
    how_bought: str = ""
    barriers: list[str] = Field(default_factory=list)
    unresolved: str = ""
    unresolved_quote: str = ""
    unresolved_source: int = -1

    @field_validator("covers_source", "unresolved_source", mode="before")
    @classmethod
    def _int(cls, v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    @field_validator("barriers", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [str(x) for x in (v or []) if str(x).strip()]


class AlternativesOut(BaseModel):
    alternatives: list[AltOut] = Field(default_factory=list)


def _t(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def inputs_key(cfg: Config, db: DB, hid: int) -> str:
    """Odtlačok vstupov bez sťahovania: tvrdenia, prehľad domény, karty, kritika a verzia promptu."""
    from .prompts import prompt_version
    from .value import domain_of

    claims = sorted(e["id"] for e in db.evidence_for(hid) if e["status"] == "verified")
    brief = db.latest_brief(domain_of(db, hid)[0])
    cards = sorted((r["id"], r["version"]) for r in db.org_cards_for(hid))
    crit = db.latest_critique(hid)
    raw = json.dumps([claims, brief["id"] if brief is not None else None, cards,
                      crit["created_at"] if crit else None, prompt_version("alternatives_assess")])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def gather_sources(cfg: Config, db: DB, hid: int, fetcher=None, max_web: int = MAX_WEB) -> list[dict[str, Any]]:
    """Číslované zdroje s textom (model z nich cituje, kód citáty overuje)."""
    from .value import domain_of

    out: list[dict[str, Any]] = []
    ev = [e for e in db.evidence_for(hid) if e["status"] == "verified"]
    ev.sort(key=lambda e: (e["claim_type"] not in ("existing_solution", "counter"), e["id"]))
    for e in ev[:12]:
        out.append({"druh": "dôkaz" + (" (dodávateľ)" if e["source_type"] == "vendor" else ""), "názov": e["doc_title"] or "",
                    "url": e["url"], "text": e["quote"] or ""})
    brief = db.latest_brief(domain_of(db, hid)[0])
    if brief is not None:
        for s in (json.loads(brief["brief_json"] or "{}").get("existing_solutions") or [])[:5]:
            if _t(s).strip():
                out.append({"druh": "prehľad domény – existujúce riešenie", "názov": "", "url": "", "text": _t(s)})
    for card in db.org_cards_for(hid):
        for cs in (json.loads(card["card_json"] or "{}").get("current_solution") or [])[:3]:
            parts = [f"{label}: {_t(cs.get(k))}" for k, label in (("tool", "nástroj"), ("functions", "funkcie"),
                                                                  ("deployment", "nasadenie"), ("gap", "medzera"))
                     if _t(cs.get(k)).strip()]
            if parts:
                out.append({"druh": f"karta organizácie {card['org_name']}", "názov": "", "url": "",
                            "text": "; ".join(parts)})
    crit = db.latest_critique(hid)
    if fetcher is not None and crit:
        for link in (crit["content"].get("competitor_links") or [])[: max_web]:
            try:
                page = fetcher.fetch(link["url"])
            except Exception as exc:  # noqa: BLE001 – nedostupná stránka nezastaví posúdenie
                log.info("Stránka konkurencie %s nedostupná: %s", link.get("url"), exc)
                continue
            if page is not None and (page.text or "").strip():
                out.append({"druh": "web (výsledok hľadania konkurencie)", "názov": link.get("title") or "",
                            "url": link["url"], "text": page.text[:WEB_CHARS]})
    return [s for s in out if s["text"].strip()][:MAX_SOURCES]


def sources_text(sources: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[SOURCE {i}] ({s['druh']}{', ' + s['url'] if s['url'] else ''})\n{s['text']}"
                       for i, s in enumerate(sources))


def _quoted(quote: str, idx: int, sources: list[dict[str, Any]]) -> bool:
    return bool(quote.strip()) and 0 <= idx < len(sources) and quote_score(quote, sources[idx]["text"]) >= 100


def verify(out: AlternativesOut, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kontrola kódom: citát doslova v uvedenom zdroji; inak sa tvrdenie zoslabí (nikdy nepovýši)."""
    items = []
    for a in out.alternatives[:MAX_ALTERNATIVES]:
        if not a.name.strip():
            continue
        kind = a.kind if a.kind in KINDS else "other"
        model_covers = a.covers if a.covers in COVERS else "unknown"
        covers_ok = _quoted(a.covers_quote, a.covers_source, sources)
        unresolved_ok = bool(a.unresolved.strip()) and _quoted(a.unresolved_quote, a.unresolved_source, sources)
        items.append({
            "názov": a.name.strip(), "druh": kind,
            "pokrýva": model_covers if covers_ok or model_covers in ("no", "unknown") else "unknown",
            "pokrýva_model": model_covers, "pokrýva_citát": a.covers_quote.strip() if covers_ok else "",
            "pokrýva_zdroj": sources[a.covers_source]["url"] or sources[a.covers_source]["druh"] if covers_ok else "",
            "ako_sa_kupuje": a.how_bought.strip(), "bariéry": a.barriers[:4],
            "nevyriešené": a.unresolved.strip(), "nevyriešené_doložené": unresolved_ok,
            "nevyriešené_citát": a.unresolved_quote.strip() if unresolved_ok else "",
            "nevyriešené_zdroj": (sources[a.unresolved_source]["url"] or sources[a.unresolved_source]["druh"])
            if unresolved_ok else "",
        })
    return items


def propose(items: list[dict[str, Any]]) -> tuple[str, str]:
    """Návrh kódom (nie modelom)."""
    if not items:
        return "insufficient", PROPOSALS["insufficient"]
    gaps = [i for i in items if i["nevyriešené_doložené"]]
    if gaps:
        return "gap_evidence", "doložená nevyriešená časť: " + "; ".join(f"{i['názov']} – {i['nevyriešené']}" for i in gaps[:3])
    if all(i["pokrýva"] == "yes" for i in items):
        return GAP_STATUS, ("každá alternatíva pokrýva proces doloženým citátom (" + ", ".join(i["názov"] for i in items[:4])
                            + ") a nevyriešená časť nie je doložená")
    return "open", PROPOSALS["open"]


def assess(cfg: Config, db: DB, router, fetcher, hid: int, key: str | None = None) -> dict[str, Any]:
    hyp = db.get_hypothesis(hid)
    key = key or inputs_key(cfg, db, hid)
    sources = gather_sources(cfg, db, hid, fetcher)
    if not sources:
        aid = db.add_alternative_assessment(hid, key, [], [], "insufficient", "k hypotéze nie sú zdroje o riešeniach")
        return {"id": aid, "návrh": "insufficient"}
    res = router.run_task(
        "alternatives_assess",
        [{"role": "system", "content": ALTERNATIVES_SYSTEM.format(lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"PROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment'] or '-'}\n"
                                     f"DESCRIPTION: {hyp['description'] or '-'}\n\nSOURCES:\n<untrusted_document>\n"
                                     f"{sources_text(sources)}\n</untrusted_document>"}],
        AlternativesOut, object_type="hypothesis", object_id=hid, max_tokens=2500,
    )
    if not res.ok:
        return {"chyba": res.reason}  # položka ostane na ďalší beh (invariant 5)
    items = verify(res.value, sources)
    proposal, reason = propose(items)
    meta = [{k: s[k] for k in ("druh", "názov", "url")} for s in sources]
    aid = db.add_alternative_assessment(hid, key, meta, items, proposal, reason)
    return {"id": aid, "návrh": proposal}


def pending(cfg: Config, db: DB, ids: list[int] | None = None) -> list[int]:
    """Hypotézy, ktorých vstupy sa od posledného posúdenia zmenili: zadané (aj človekom pre konkrétnu hypotézu), inak
    hypotézy pre automatiku (bez potvrdených „bez obhájiteľnej medzery“ a ručnej kohorty benchmarku)."""
    out = []
    hyps = [h for h in (db.get_hypothesis(i) for i in ids) if h is not None] if ids else db.automation_hypotheses()
    for h in hyps:
        last = db.latest_alternative_assessment(h["id"])
        if last is None or last["inputs_hash"] != inputs_key(cfg, db, h["id"]):
            out.append(h["id"])
    return out


def run_alternatives(cfg: Config, db: DB, llm: Any, fetcher, deadline: Deadline, ids: list[int] | None = None) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    for hid in pending(cfg, db, ids):
        if deadline.expired():
            break
        try:
            out = assess(cfg, db, router, fetcher, hid)
        except Exception:  # noqa: BLE001 – jedna hypotéza nezastaví ostatné
            log.exception("Posúdenie alternatív H%s zlyhalo", hid)
            stats["errors"] += 1
            continue
        if "chyba" in out:
            stats["llm_failures"] += 1
        else:
            stats["assessed"] += 1
            stats[f"návrh_{out['návrh']}"] += 1
    return dict(stats)


def decide(db: DB, assessment_id: int, decision: str, note: str = "") -> None:
    """Rozhodnutie človeka: potvrdiť návrh „bez obhájiteľnej medzery“ (hypotéza sa vynechá v lieviku a autopilote),
    alebo ho zamietnuť (hypotéza pokračuje)."""
    row = db.get_alternative_assessment(assessment_id)
    if row is None:
        raise ValueError("Posúdenie neexistuje.")
    if decision not in ("confirmed", "rejected"):
        raise ValueError(f"Neznáme rozhodnutie {decision!r}.")
    if decision == "confirmed" and row["proposal"] != GAP_STATUS:
        raise ValueError("Potvrdiť sa dá iba návrh kódu „bez obhájiteľnej medzery“.")
    db.decide_alternative_assessment(assessment_id, decision, note.strip() or None)
    if decision == "confirmed":
        db.set_gap_status(row["hypothesis_id"], GAP_STATUS, note.strip() or row["proposal_reason"])


def reopen(db: DB, hid: int, note: str = "") -> None:
    """Človek hypotézu obnoví (napr. rozhovor ukázal nevyriešenú časť)."""
    db.set_gap_status(hid, None, note.strip() or "obnovené človekom")


def state(db: DB, hid: int) -> dict[str, Any] | None:
    row = db.latest_alternative_assessment(hid)
    if row is None:
        return None
    hyp = db.get_hypothesis(hid)
    return {"id": row["id"], "kedy": row["created_at"], "alternatívy": json.loads(row["alternatives_json"] or "[]"),
            "zdroje": json.loads(row["sources_json"] or "[]"), "návrh": row["proposal"],
            "návrh_text": PROPOSALS.get(row["proposal"], row["proposal"]), "dôvod": row["proposal_reason"],
            "rozhodnutie": row["decision"], "poznámka": row["decision_note"],
            "stav_hypotézy": hyp["gap_status"] if hyp is not None else None}
