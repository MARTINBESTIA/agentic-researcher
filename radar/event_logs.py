"""Event log ako podporovaný typ žiadosti (dodatok 16. 10. 2026, časti 6–7).

Event log je história udalostí procesu: jeden riadok = udalosť konkrétneho prípadu (nie aktuálny stav celého prípadu).
Hodí sa najmä pre hypotézy o priebehu, opakovaní krokov, odovzdávaní a dĺžke procesov; pri hypotéze o obsahu
dokumentov, cenách alebo kvalite údajov je vhodnejšia iná vzorka – researcher vysvetlí, prečo žiada práve event log.

Žiadosť je dvojstupňová: (1) dátový slovník a malá pseudonymizovaná ukážka histórie niekoľkých celých prípadov
(20–50 riadkov je príklad, nie minimum zaručujúce analýzu), (2) po dohode väčší export pre obdobie alebo kohortu.
Text žiadosti je pevná šablóna (nie model) s podmienkami bezplatného dema a skúšky pre komerčného adresáta; verejnému
správcovi a expertovi sa protihodnota prispôsobí. Žiadosť nesľubuje „anonymné“ dáta len preto, že chýba meno.
Ak event log neexistuje, ponúkne sa alternatíva s vlastným typom dát (snapshot sa nikdy nespracuje ako história).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Config
from .db import DB

MIN_FIELDS = [
    {"pole": "case_id", "požiadavka": "povinné", "význam": "stabilné pseudonymizované ID dohodnutého prípadu "
     "(objednávka, doklad, zákazka)"},
    {"pole": "activity", "požiadavka": "povinné", "význam": "udalosť alebo vykonaný krok, napr. „objednávka prijatá“"},
    {"pole": "timestamp", "požiadavka": "povinné pre časovú analýzu", "význam": "čas vzniku udalosti s offsetom alebo "
     "dokumentovanou zónou – nie čas exportu, zápisu ani synchronizácie"},
    {"pole": "event_id", "požiadavka": "odporúčané", "význam": "stabilné ID udalosti na kontrolu duplicít"},
    {"pole": "lifecycle", "požiadavka": "voliteľné", "význam": "start/complete, ak systém eviduje začiatok aj koniec"},
    {"pole": "activity_instance_id", "požiadavka": "voliteľné", "význam": "párovanie začiatku a konca tej istej aktivity"},
    {"pole": "role", "požiadavka": "voliteľné", "význam": "rola alebo tím – nie meno pracovníka"},
    {"pole": "sequence", "požiadavka": "voliteľné", "význam": "poradie udalostí s rovnakým časom, ak ho určuje zdroj"},
    {"pole": "source_system", "požiadavka": "voliteľné", "význam": "pôvod udalosti pri spájaní systémov"},
]
DUE_FIELD = {"pole": "due_at / planned_at", "požiadavka": "podľa hypotézy", "význam": "termín s definíciou (pôvodný "
             "prísľub alebo zmenený plán) a prípadne história jeho zmien"}
ACCOMPANYING = ["stručný dátový slovník (význam polí a hodnôt)", "definícia prípadu", "výberové obdobie",
                "spôsob výberu prípadov (všetky / iba dokončené / iba tím…)", "informácia o otvorených prípadoch",
                "význam časov (vznik udalosti vs. zápis, synchronizácia, export) a časová zóna"]
ALTERNATIVES = {"walkthrough": "prehliadka posledného konkrétneho prípadu počas rozhovoru",
                "state_schema": "opis stavov a schéma systému (aktuálny stav, nie história)",
                "aggregate_report": "existujúci agregovaný report",
                "manual_logging": "dohodnuté krátke manuálne zaznamenávanie krokov"}
REQUEST_STATUS = {"draft": "návrh", "sent": "odoslané", "received": "prijaté", "not_available": "event log nemajú",
                  "alternative": "dohodnutá alternatíva", "declined": "odmietnuté"}
FIT_RE = re.compile(r"priebeh|proces|krok|mešk|oneskor|termín|lehot|opakov|schvaľ|odovzd|čakan|dĺžk|trvan|vybav|"
                    r"workflow|urgen|eskal|reklamác|objednáv|dodáv", re.I)
DUE_RE = re.compile(r"termín|mešk|oneskor|lehot|plán|prísľub|due", re.I)
FIRST_STEP = ("dátový slovník a malá pseudonymizovaná ukážka – napríklad 20–50 riadkov histórie niekoľkých celých "
              "prípadov (nie náhodné izolované udalosti); počet je príklad na overenie formátu a významu, nie minimum, "
              "ktoré zaručí analýzu")
SECOND_STEP = ("po dohode väčší export pre konkrétne obdobie alebo kohortu; veľkosť podľa frekvencie, variability "
               "a cieľa analýzy – malá schémová vzorka nie je reprezentatívna na odhad prínosu")
DEFAULT_OUTPUT = "prehľad zaznamenaných priebehov, opakujúcich sa krokov a časových odstupov na ďalšie preverenie"


class EventLogError(ValueError):
    pass


def event_log_fit(db: DB, hid: int) -> tuple[bool, str]:
    """Je event log vhodný typ dát pre hypotézu? (Vysvetlenie, prečo žiadame práve históriu udalostí.)"""
    from .value import domain_of

    h = db.get_hypothesis(hid)
    text = f"{h['title']} {h['description'] or ''}" if h is not None else ""
    latest = db.latest_brief(domain_of(db, hid)[0])
    if latest is not None:
        b = json.loads(latest["brief_json"] or "{}")
        text += " " + " ".join(s.get("text", "") for s in b.get("process_steps") or [])
    hits = sorted({m.group(0).lower() for m in FIT_RE.finditer(text)})
    if hits:
        return True, ("hypotéza je o priebehu procesu (" + ", ".join(hits[:4]) + "): história udalostí ukáže "
                      "zaznamenané priebehy, opakovania krokov, odovzdávanie a časové odstupy")
    return False, ("hypotéza nie je zjavne o priebehu procesu – vhodnejšia môže byť iná vzorka (ukážka dokumentov, "
                   "cenník, záznamy o kvalite údajov); event log žiadaj iba s vlastným zdôvodnením")


def _org(db: DB, org_id: int | None):
    return db.get_organization(org_id) if org_id else None


def request_text(row, org, commercial: bool, policy: str | None) -> tuple[str, str]:
    """Pevná šablóna žiadosti (bez modelu). Vráti (jadro správy, predmet)."""
    case = row["case_definition"] or "prípade"
    p1 = (f"Ak váš systém uchováva históriu krokov pri {case}, na prvé overenie by postačil opis polí a malá "
          "pseudonymizovaná ukážka s identifikátorom prípadu, názvom udalosti a časom jej vzniku. Mená pracovníkov "
          "ani obsah komunikácie na tento prvý rozbor nepotrebujem.")
    if row["step"] == 2:
        p1 = (f"Ďakujem za ukážku histórie pri {case}. Ak by to bolo pre vás užitočné, ďalším krokom by bol väčší "
              "pseudonymizovaný export za dohodnuté obdobie alebo skupinu prípadov – rozsah by sme určili podľa toho, "
              "ako často sa prípady vyskytujú a čo má výstup ukázať.")
    output = row["intended_output"] or DEFAULT_OUTPUT
    p2 = (f"Z použiteľnej histórie viem pripraviť {output}. Najprv by sme overili význam údajov a to, či výstup "
          "pomáha pri vašom rozhodovaní.")
    if not commercial:
        p2 += " Na oplátku pošlem stručný prehľad zistení a nejasností v údajoch."
    q = "Viete takúto históriu z vášho systému získať, prípadne ma nasmerovať na jej správcu?"
    if row["step"] == 2:
        q = "Bolo by pre vás takéto rozšírenie zaujímavé?"
    subject = f"Ukážka histórie krokov – {case}" if row["step"] != 2 else f"Väčší export histórie – {case}"
    return "\n\n".join([p1, p2, q]), subject[:70]


def build_request(cfg: Config, db: DB, hid: int, org_id: int | None, case_definition: str = "",
                  outreach_id: int | None = None, step: int = 1, why: str = "", process_scope: str = "") -> int:
    """Žiadosť o event log s účelom, definíciou prípadu, minimálnou schémou, malým prvým krokom, prínosom
    a neznámymi. Pri nevhodnej hypotéze iba s vlastným zdôvodnením (`why`)."""
    from .domain import BLOCKS
    from .value import best_proposal, domain_of

    fit, fit_why = event_log_fit(db, hid)
    if not fit and not why.strip():
        raise EventLogError(fit_why)
    h = db.get_hypothesis(hid)
    fields = list(MIN_FIELDS)
    if DUE_RE.search(f"{h['title']} {h['description'] or ''}"):
        fields.append(DUE_FIELD)
    proposal = best_proposal(db, hid, "customer")
    p = json.loads(proposal["proposal_json"]) if proposal is not None else {}
    output = p.get("deliverable") or DEFAULT_OUTPUT
    key = domain_of(db, hid)[0]
    unknowns = ["či systém uchováva históriu krokov (nie iba aktuálny stav)", "časová zóna a presnosť časov",
                "otvorené a odrezané prípady vo výbere"]
    unknowns += [f"{q['question']} (blokuje: {BLOCKS.get(q['blocks_action'], q['blocks_action'])})"
                 for q in db.domain_questions(key, "open") if q["blocks_action"] in ("data_request", "duration_analysis")]
    return db.add_eventlog_request(
        hypothesis_id=hid, organization_id=org_id, outreach_id=outreach_id, step=step,
        process_scope=process_scope or h["title"], case_definition=case_definition.strip() or "dohodnutom prípade "
        "(napr. objednávke, doklade alebo zákazke)", fields_json=fields + [{"pole": "sprievodné informácie",
                                                                           "požiadavka": "k exportu",
                                                                           "význam": "; ".join(ACCOMPANYING)}],
        sample_scope=FIRST_STEP if step == 1 else SECOND_STEP, intended_output=output,
        why_event_log=why.strip() or fit_why, unknowns_json=unknowns, status="draft")


def request_markdown(row) -> str:
    fields = json.loads(row["fields_json"] or "[]")
    lines = [f"# Žiadosť o event log (krok {row['step']}) – {row['org_name'] or 'organizácia'}", "",
             f"**Účel:** {row['why_event_log']}", f"**Rozsah procesu:** {row['process_scope']}",
             f"**Definícia prípadu:** {row['case_definition']}", f"**Prvý krok / rozsah ukážky:** {row['sample_scope']}",
             f"**Zamýšľaný výstup:** {row['intended_output']}", "", "| pole | požiadavka | význam |", "|---|---|---|"]
    lines += [f"| {f['pole']} | {f['požiadavka']} | {f['význam']} |" for f in fields]
    lines += ["", "**Čo ešte nevieme:**"] + [f"- {u}" for u in json.loads(row["unknowns_json"] or "[]")]
    lines += ["", "Pri importe sa nič nemaže potichu: problémové riadky sa označia a report kvality povie, ktoré "
              "analýzy dáta podporia. Mená pracovníkov ani obsah komunikácie sa nežiadajú."]
    return "\n".join(lines) + "\n"


def draft_request_message(cfg: Config, db: DB, request_id: int) -> int:
    """Návrh správy so žiadosťou (oslovenie v režime data_request) pre kontakt pôvodného oslovenia alebo funkčný kontakt
    organizácie. Systém nič neodosiela – návrh ide do Oslovení na kontrolu, schválenie a ručné odoslanie."""
    from .outreach import check_draft, compose, need_confirmed, policy_text, readiness_of
    from .value import policy_version

    row = db.get_eventlog_request(request_id)
    if row is None:
        raise EventLogError("Žiadosť neexistuje.")
    orig = db.get_outreach(row["outreach_id"]) if row["outreach_id"] else None
    org = _org(db, row["organization_id"] or (orig["org_id"] if orig is not None else None))
    person = db.get_person(orig["person_id"]) if orig is not None and orig["person_id"] else None
    contact = db.get_contact(orig["contact_point_id"]) if orig is not None and orig["contact_point_id"] else None
    if contact is None and org is not None:
        emails = [c for c in db.org_contacts(org["id"]) if c["kind"] == "email"]
        contact = emails[0] if emails else None
    if contact is None:
        raise EventLogError("Chýba kontakt: žiadosť patrí k osloveniu alebo k organizácii so zverejneným e-mailom.")
    commercial = org is None or (org["sector_kind"] or "") != "public"
    policy = policy_text(cfg, "data_request", True, commercial)
    core, subject_core = request_text(row, org, commercial, policy)
    source_url = contact["source_url"] or ""
    subject, body = compose(cfg, "email", person, org["sector_kind"] if org is not None else None, core, subject_core,
                            source_url, contact["value"], "data_request", policy)
    orig_pkg = json.loads(orig["package_json"] or "{}") if orig is not None and orig["package_json"] else {}
    confirmed = need_confirmed(db, row["hypothesis_id"], person["id"] if person else None, org["id"] if org else None)
    package = {
        "režim": "data_request", "typ": orig_pkg.get("typ") or "customer",
        "prečo_tento_kontakt": orig_pkg.get("prečo_tento_kontakt") or {
            "text": "funkčný kontakt organizácie", "dôkaz": "", "zdroj": source_url, "úryvok": contact["source_snippet"] or ""},
        "potreba": {"text": row["process_scope"], "stav": "confirmed" if confirmed else "hypothesis", "podklad": []},
        "výstup": row["intended_output"], "rozhodnutie": orig_pkg.get("rozhodnutie") or "",
        "minimálny_vstup": row["sample_scope"], "čo_nevieme": "; ".join(json.loads(row["unknowns_json"] or "[]")),
        "záväzok": None, "návrh_prínosu": orig_pkg.get("návrh_prínosu"), "stav_návrhu_prínosu": orig_pkg.get(
            "stav_návrhu_prínosu") or "šablóna", "prehľad_domény": orig_pkg.get("prehľad_domény"),
        "politika": policy_version(cfg), "veta_o_podmienkach": policy, "žiadosť_event_log": request_id,
        "hlavná_otázka": {"id": None, "text": core.split("\n\n")[-1], "blokuje": "žiadosť o dáta"},
    }
    checks = check_draft(cfg, "email", subject, body, core, [], source_url, "data_request", policy,
                         f"{row['sample_scope']} {row['intended_output']}", package["potreba"]["stav"])
    checks.update(core=core, subject_core=subject_core, template="event_log_request", generic=person is None,
                  contact_kind=contact["kind"])
    readiness, reasons = readiness_of(package, checks, subject, body)
    oid = db.insert_outreach(person["id"] if person else None, row["hypothesis_id"], "email", "B", subject, body, [],
                             checks, "draft" if checks["ok"] else "needs_edit", contact["id"],
                             org_id=org["id"] if org is not None else None, mode="data_request",
                             proposal_id=package["návrh_prínosu"], package={**package, "pripravenosť": reasons},
                             readiness=readiness)
    db.update_eventlog_request(request_id, outreach_id=oid, text=body)
    return oid


def set_request_status(db: DB, request_id: int, status: str, alternative: str | None = None) -> None:
    """Stav žiadosti; „event log nemajú“ → alternatíva s vlastným typom dát (snapshot sa nespracuje ako história)."""
    if status not in REQUEST_STATUS:
        raise EventLogError(f"Neznámy stav {status!r}.")
    if alternative is not None and alternative not in ALTERNATIVES:
        raise EventLogError(f"Neznáma alternatíva {alternative!r}.")
    fields: dict[str, Any] = {"status": status}
    if alternative:
        fields.update(alternative=alternative, status="alternative")
    db.update_eventlog_request(request_id, **fields)


def next_step_without_log(row) -> list[str]:
    """Ďalšie kroky, keď event log neexistuje (dodatok 6.3)."""
    return [f"{label}" for label in ALTERNATIVES.values()]
