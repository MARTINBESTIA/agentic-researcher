"""Výsledky oslovení a učenie z odpovedí (audit 15. 10. 2026, balík E).

Výsledok zapisuje človek (systém nič neodosiela ani nečíta poštu): odpoveď, či kontakt problém potvrdil alebo
vyvrátil, vzorka, rozhovor, demo, pilot, preukázaná hodnota, platba, dôvod odmietnutia, ďalší krok, čas prípravy
a dodania. Z výsledku:

- stav oslovenia (odpovedal, dohodnuté, odmietol – odmietnutie spustí retenciu ako doteraz);
- stav potreby návrhov prínosu pre typ adresáta (hypotéza → potvrdená / vyvrátená / zmiešaná, s počtom organizácií);
  návrh pre inú organizáciu ostáva podmienený, kým ju nepotvrdí ona (`outreach.need_confirmed`);
- prehľad domény sa pri najbližšom kroku `value` aktualizuje (podklady obsahujú potvrdenia a vyvrátenia, nová verzia
  s dôvodom „odpoveď kontaktu“, staršie ostávajú);
- metriky po **unikátnych organizáciách**, nie po počte správ.

Osobné údaje: výsledok patrí k osobe (forget a retencia ho mažú spolu s ňou); poznámka sa ukladá s maskovanými
e-mailmi a telefónmi a do promptov ide pseudonymizovaná.
"""

from __future__ import annotations

import json
from statistics import median
from typing import Any

from .config import Config
from .db import DB
from .privacy import mask_text

REPLIES = {"none": "bez odpovede", "relevant": "relevantná odpoveď", "redirect": "presmerovanie na iného človeka",
           "not_relevant": "nerelevantné", "declined": "odmietnutie"}
RELEVANCE = {"confirmed": "problém potvrdený", "refuted": "problém vyvrátený", "unclear": "nejasné"}
FLAGS = {"sample_obtained": "vzorka dát", "interview_agreed": "rozhovor dohodnutý", "demo_done": "demo",
         "pilot_agreed": "pilot dohodnutý", "value_shown": "preukázaná hodnota", "paid": "platené"}
STATUS_FOR_REPLY = {"relevant": "replied", "redirect": "replied", "not_relevant": "replied", "declined": "declined"}
FINAL_STATUSES = {"interviewed", "declined", "bounced", "blocked"}
# Dôvod nezáujmu (zadanie 17. 10. 2026, časť F): odmietnutie nie je automaticky vyvrátenie problému
DECLINE_REASONS = {"no_problem": "problém neexistuje", "already_solved": "už vyriešené", "wrong_recipient": "nesprávny adresát",
                   "low_priority": "nízka priorita", "budget": "rozpočet", "unfit_offer": "nevhodná ponuka", "other": "iné",
                   "unknown": "nezistené"}
# Štruktúrované časti odpovede (podľa dostupnosti; prázdne = nezistené)
STRUCTURED = {"team_role": "tím / rola", "last_case": "posledný prípad", "frequency": "frekvencia",
              "tool_used": "používaný nástroj", "unsolved_part": "nevyriešená časť", "process_owner": "vlastník procesu",
              "buying_process": "spôsob rozhodovania o kúpe", "data_availability": "dostupnosť dát"}
BRIEF_REASON_KEY = "brief_update_reasons"  # doména → dôvod ďalšej verzie prehľadu („odpoveď kontaktu …“)


class OutcomeError(ValueError):
    pass


def recipient_kind(db: DB, outreach_id: int | None) -> str:
    """Typ adresáta z balíka návrhu (zákazník, držiteľ dát, expert); bez balíka zákazník."""
    if not outreach_id:
        return "customer"
    row = db.get_outreach(outreach_id)
    pkg = json.loads(row["package_json"] or "{}") if row is not None and row["package_json"] else {}
    return pkg.get("typ") or "customer"


def need_summary(db: DB, hid: int, kind: str = "customer") -> dict[str, Any]:
    """Stav potreby pre typ adresáta podľa posledného výsledku každej organizácie (alebo osoby bez organizácie)."""
    last: dict[str, str] = {}
    for o in db.outcomes_for(hid):
        if o["problem_relevance"] not in ("confirmed", "refuted"):
            continue
        if recipient_kind(db, o["outreach_id"]) != kind:
            continue
        who = f"org{o['org_id']}" if o["org_id"] else f"p{o['person_id']}"
        last[who] = o["problem_relevance"]
    confirmed = sum(1 for v in last.values() if v == "confirmed")
    refuted = sum(1 for v in last.values() if v == "refuted")
    status = ("mixed" if confirmed and refuted else "confirmed" if confirmed else "refuted" if refuted else "hypothesis")
    return {"stav": status, "potvrdili": confirmed, "vyvrátili": refuted}


def refresh_need_status(db: DB, hid: int, kind: str) -> dict[str, Any]:
    summary = need_summary(db, hid, kind)
    for p in db.value_proposals_for(hid, kind):
        if p["need_status"] != summary["stav"]:
            db.update_value_proposal(p["id"], need_status=summary["stav"])
    return summary


def record_outcome(cfg: Config, db: DB, *, outreach_id: int | None = None, hid: int | None = None,
                   person_id: int | None = None, org_id: int | None = None, reply: str = "none",
                   problem_relevance: str | None = None, rejection_reason: str = "", next_action: str = "",
                   note: str = "", prep_minutes: float | None = None, delivery_days: float | None = None,
                   answers: dict[int, dict[str, Any]] | None = None, card_id: int | None = None,
                   question_id: int | None = None, decline_reason: str | None = None, structured: dict[str, str] | None = None,
                   source: str = "human", contradicts: bool = False, **flags: bool) -> dict[str, Any]:
    """Zapíše výsledok oslovenia (ručne zadaný človekom) a premietne ho do stavu oslovenia, potreby a prehľadu.
    answers: odpovede na otázky domény {id otázky: {"text": …, "contradicts": bool}} – otázka sa uzavrie s históriou
    a vznikne tvrdenie „uviedol kontakt“ (výpoveď, nie meraný výsledok).
    v15: odpoveď sa viaže na organizáciu, tím/rolu, otázku a verziu karty; štruktúrované časti (`structured`), dôvod
    nezáujmu; automaticky vyťažené (source != human) ide na kontrolu človekom. Na obnovu sa označia iba karty a návrhy
    tejto organizácie; nová odpoveď proti schválenej karte je viditeľný rozpor."""
    from .outreach import set_status
    from .value import brief_inputs

    if reply not in REPLIES:
        raise OutcomeError(f"Neznáma odpoveď {reply!r}.")
    if problem_relevance not in (None, "", *RELEVANCE):
        raise OutcomeError(f"Neznámy stav problému {problem_relevance!r}.")
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise OutcomeError(f"Neznáme príznaky: {', '.join(sorted(unknown))}.")
    row = db.get_outreach(outreach_id) if outreach_id else None
    if outreach_id and row is None:
        raise OutcomeError("Oslovenie neexistuje.")
    if row is not None:
        hid, person_id = row["hypothesis_id"], row["person_id"]
        org_id = row["org_id"] or org_id
    if hid is None or not (person_id or org_id):
        raise OutcomeError("Výsledok potrebuje hypotézu a osobu alebo organizáciu.")
    for value, label in ((prep_minutes, "čas prípravy"), (delivery_days, "čas dodania")):
        if value is not None and value < 0:
            raise OutcomeError(f"{label} nemôže byť záporný.")
    if decline_reason not in (None, "", *DECLINE_REASONS):
        raise OutcomeError(f"Neznámy dôvod nezáujmu {decline_reason!r}.")
    bad = set(structured or {}) - set(STRUCTURED)
    if bad:
        raise OutcomeError(f"Neznáme časti odpovede: {', '.join(sorted(bad))}.")
    card_version = None
    if card_id is None and row is not None and "card_id" in row.keys():
        card_id = row["card_id"]
    if card_id:
        card = db.get_org_card(card_id)
        if card is None:
            raise OutcomeError("Karta organizácie neexistuje.")
        if org_id and card["organization_id"] != org_id:
            raise OutcomeError("Karta patrí inej organizácii – odpoveď jednej organizácie nemení kartu druhej.")
        card_version = card["version"]
    key = brief_inputs(db, hid)[0]
    before = brief_inputs(db, hid)[-1]
    outcome_id = db.add_outcome(
        hid, outreach_id=outreach_id, person_id=person_id, org_id=org_id,
        proposal_id=row["proposal_id"] if row is not None else None,
        message_version=row["message_version"] if row is not None else None, reply=reply,
        problem_relevance=problem_relevance or None, rejection_reason=(rejection_reason or "").strip() or None,
        next_action=(next_action or "").strip() or None, note=mask_text((note or "").strip()) or None,
        prep_minutes=prep_minutes, delivery_days=delivery_days, card_id=card_id, card_version=card_version,
        question_id=question_id, decline_reason=decline_reason or None, source=source,
        needs_human_review=int(source != "human"),
        **{k: mask_text(str(v).strip())[:500] for k, v in (structured or {}).items() if str(v or "").strip()},
        **{k: bool(v) for k, v in flags.items()})
    result: dict[str, Any] = {"id": outcome_id}
    if answers:
        from .domain import record_answer

        result["odpovede"] = [record_answer(db, int(qid), (a or {}).get("text", ""), f"kontakt (výsledok #{outcome_id})",
                                            outcome_id=outcome_id, organization_id=org_id,
                                            contradicts=bool((a or {}).get("contradicts")))
                              for qid, a in answers.items() if (a or {}).get("text")]
    if row is not None and row["status"] not in FINAL_STATUSES:
        new = "scheduled" if flags.get("interview_agreed") and reply != "declined" else STATUS_FOR_REPLY.get(reply)
        if new and new != row["status"]:
            set_status(cfg, db, outreach_id, new)
            result["stav_oslovenia"] = new
    kind = recipient_kind(db, outreach_id)
    result["potreba"] = refresh_need_status(db, hid, kind)
    try:
        from .opportunity import on_outcome

        result["karty"] = on_outcome(cfg, db, hid, org_id, outcome_id, contradicts=contradicts or any(
            (a or {}).get("contradicts") for a in (answers or {}).values()))
    except Exception as exc:  # noqa: BLE001 – staršia schéma bez kariet: zápis odpovede sa nesmie stratiť
        result["karty"] = {"chyba": type(exc).__name__}
    if brief_inputs(db, hid)[-1] != before:  # potvrdenie/vyvrátenie mení podklady prehľadu domény
        reasons = db.get_setting(BRIEF_REASON_KEY, {}) or {}
        reasons[key] = f"odpoveď kontaktu (výsledok #{outcome_id}: {RELEVANCE.get(problem_relevance or '', '')})"
        db.set_setting(BRIEF_REASON_KEY, reasons)
        result["prehľad_domény"] = "aktualizuje sa pri najbližšom kroku value"
    return result


def pending_brief_reason(db: DB, key: str, pop: bool = False) -> str | None:
    reasons = db.get_setting(BRIEF_REASON_KEY, {}) or {}
    reason = reasons.get(key)
    if pop and reason is not None:
        reasons.pop(key)
        db.set_setting(BRIEF_REASON_KEY, reasons)
    return reason


# ======================================================================= metriky po organizáciách
CONTACTED = {"sent", "replied", "scheduled", "interviewed", "no_reply", "declined", "no_show"}


def _org_key(org_id: int | None, person_id: int | None) -> str:
    return f"org{org_id}" if org_id else f"p{person_id}"


def outcome_metrics(db: DB, hids: list[int] | None = None) -> dict[str, Any]:
    """Postup po unikátnych organizáciách (osoba bez organizácie sa ráta zvlášť): oslovené → relevantná odpoveď →
    presmerovanie → potvrdený problém → rozhovor → vzorka → demo → pilot → hodnota → platené; čas prípravy
    a dodania (medián). Rozpad podľa režimu oslovenia."""
    hids = hids if hids is not None else [h["id"] for h in db.people_enabled_hypotheses()]
    contacted: dict[str, set[str]] = {}
    by_mode_contacted: dict[str, set[str]] = {}
    for hid in hids:
        for o in db.outreach_list(hid):
            if o["status"] in CONTACTED or o["sent_at"]:
                who = _org_key(o["org_id"], o["person_id"])
                contacted.setdefault("all", set()).add(who)
                by_mode_contacted.setdefault(o["mode"] or "discovery", set()).add(who)
    reached: dict[str, set[str]] = {k: set() for k in ("relevant", "redirect", "confirmed", "refuted", *FLAGS)}
    by_mode_relevant: dict[str, set[str]] = {}
    prep, delivery = [], []
    for hid in hids:
        for o in db.outcomes_for(hid):
            who = _org_key(o["org_id"], o["person_id"])
            if o["reply"] in ("relevant", "redirect"):
                reached[o["reply"]].add(who)
            if o["problem_relevance"] in ("confirmed", "refuted"):
                reached[o["problem_relevance"]].add(who)
            for flag in FLAGS:
                if o[flag]:
                    reached[flag].add(who)
            if o["prep_minutes"] is not None:
                prep.append(o["prep_minutes"])
            if o["delivery_days"] is not None:
                delivery.append(o["delivery_days"])
            if o["reply"] == "relevant" and o["outreach_id"]:
                row = db.get_outreach(o["outreach_id"])
                if row is not None:
                    by_mode_relevant.setdefault(row["mode"] or "discovery", set()).add(who)
    base = len(contacted.get("all", set()))

    def share(n: int) -> str:
        return f"{n / base:.0%}" if base else "–"

    labels = [("relevant", "relevantná odpoveď"), ("redirect", "presmerovanie"), ("confirmed", "potvrdený problém"),
              ("refuted", "vyvrátený problém")] + list(FLAGS.items())
    rows = [{"metrika": "oslovené organizácie", "organizácie": base, "podiel z oslovených": "100 %" if base else "–"}]
    rows += [{"metrika": label, "organizácie": len(reached[k]), "podiel z oslovených": share(len(reached[k]))}
             for k, label in labels]
    modes = [{"režim": m, "oslovené": len(v), "relevantná odpoveď": len(by_mode_relevant.get(m, set()))}
             for m, v in sorted(by_mode_contacted.items())]
    return {"riadky": rows, "režimy": modes, "oslovené": base,
            "príprava_min_medián": round(median(prep), 1) if prep else None, "príprava_n": len(prep),
            "dodanie_dni_medián": round(median(delivery), 1) if delivery else None, "dodanie_n": len(delivery)}
