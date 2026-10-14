"""Koho osloviť k hypotéze (Brána 0): ľudia a organizácie, ktorých sa oplatí kontaktovať, s e-mailom, dôvodom
a tým, čo od nich žiadať.

Odporúčanie je pravidlo kódu, nie skóre modelu (invariant 7) – každý riadok ukáže svoje dôvody:
- „oslov najprv“: vlastný pracovný e-mail a silná väzba na problém (citovaný v dôkaze, funkcia z mapy rolí,
  kontaktný bod dát), funkcia sedí (role_fit ≥ 0,5) a nič neblokuje,
- „oslov“: e-mail osoby alebo organizácie (generický – požiadať o nasmerovanie), nič neblokuje,
- „doplň kontakt“: iba telefón, skrytý e-mail (otvor stránku ručne), LinkedIn alebo nič,
- „neoslovovať“: vylúčený, odmietol, prebiehajúce obstarávanie organizácie, vylúčená organizácia.
Zoradenie: odporúčanie, sila väzby, kanál, zhoda funkcie (lexikograficky, zložky sú v stĺpcoch).
Nič sa neodosiela – zoznam slúži na kontrolu, „Chcem osloviť“ a návrhy oslovení.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .config import Config
from .contact_scout import DEFAULT_ASK, KIND_LABEL, target_ask, target_roles
from .contacts import is_generic_email
from .db import DB
from .people import LINK_STRENGTH

TIERS = ("oslov najprv", "oslov", "doplň kontakt", "neoslovovať")
GROUP_OF_RELATION = {"data_holder": "data_holder", "expert_org": "expert"}
LINK_LABEL = {"quoted_about_problem": "verejne o probléme", "role_owner": "vlastník procesu", "data_holder": "drží dáta",
              "org_affected": "vedenie dotknutej org.", "procurement_contact": "kontakt v obstarávaní", "expert": "expert"}
STRONG_LINKS = ("quoted_about_problem", "role_owner", "data_holder")
DONE_STATUSES = {"contacted": "oslovený", "replied": "odpovedal", "interviewed": "rozhovor hotový"}


def _json(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value or "{}") or {}
    except ValueError:
        return {}


def _org_ask(org, targets: dict[int, Any], group: str) -> str:
    keys = org.keys() if org is not None else []
    target = targets.get(org["target_id"]) if "target_id" in keys and org["target_id"] else None
    if target is not None:
        return target_ask(target, group)
    fit = _json(org["fit_json"]) if "fit_json" in keys else {}
    if group == "data_holder" and fit.get("pole"):
        return (f"Schéma a 20 anonymizovaných riadkov pre „{fit['pole']}“"
                + (f" (zdroj {fit['zdroj dát']})" if fit.get("zdroj dát") else "") + "; potom väčšiu vzorku.")
    return DEFAULT_ASK.get(group, "")


def contact_shortlist(cfg: Config, db: DB, hid: int) -> list[dict[str, Any]]:
    """Jeden riadok na človeka (hlavná väzba) a na organizáciu s e-mailom, z ktorej nikto vybraný nie je."""
    targets = {t["id"]: t for t in db.contact_targets_for(hid)}
    orgs = {o["id"]: o for o in db.orgs_for_hypothesis(hid, include_excluded=True)}
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    orgs_with_people: set[int] = set()
    for link in db.links_for_hypothesis(hid):  # najprv najvyššia priorita: prvá väzba osoby je hlavná
        pid = link["person_id"]
        if link["priority"] is None or pid in seen:
            continue
        seen.add(pid)
        comp = _json(link["priority_json"])
        org = orgs.get(link["org_id"]) if link["org_id"] else None
        if org is None and link["org_id"]:
            org = db.get_organization(link["org_id"])
        group = comp.get("skupina") or ("data_holder" if link["link_type"] == "data_holder"
                                        else "expert" if link["is_expert"] else "customer")
        contacts = db.contacts_for_person(pid)
        personal = [c for c in contacts if c["kind"] == "email" and not is_generic_email(c["value"])]
        person_generic = [c for c in contacts if c["kind"] == "email" and is_generic_email(c["value"])]
        org_emails = [c for c in db.org_contacts(link["org_id"]) if c["kind"] == "email"] if link["org_id"] else []
        phones = [c for c in contacts if c["kind"] == "phone"]
        hidden = [c for c in contacts if c["kind"] == "hidden_contact"]
        profiles = [c for c in contacts if c["kind"] == "profile_url"]
        email_row = (personal or person_generic or org_emails or [None])[0]
        email_kind = ("osobný" if personal else "funkčný" if person_generic else "organizácie" if org_emails else "")
        if link["org_id"]:
            orgs_with_people.add(link["org_id"])
        reasons = [LINK_LABEL.get(link["link_type"], link["link_type"])]
        if comp.get("role_fit") is not None:
            reasons.append(f"funkcia {comp.get('role_fit'):g}" + (f" ({comp['kategória']})" if comp.get("kategória") else ""))
        if comp.get("zhoda_org_popis"):
            reasons.append(str(comp["zhoda_org_popis"]))
        reasons.append(f"e-mail {email_kind}" if email_row is not None else
                       "iba telefón" if phones else "skrytý e-mail – otvor ručne" if hidden else
                       "iba LinkedIn" if profiles else "bez kontaktu")
        blockers = []
        if link["person_status"] in ("excluded", "declined"):
            blockers.append({"excluded": "vylúčený", "declined": "odmietol"}[link["person_status"]])
        if org is not None and org["excluded"]:
            blockers.append("organizácia vylúčená")
        if org is not None and "procurement_active" in org.keys() and org["procurement_active"]:
            blockers.append("prebiehajúce obstarávanie")
        strong = (link["link_type"] in STRONG_LINKS and (comp.get("role_fit") or 0) >= 0.5) or link["link_type"] == "quoted_about_problem"
        if blockers:
            tier = "neoslovovať"
        elif personal and strong:
            tier = "oslov najprv"
        elif email_row is not None:
            tier = "oslov"
        else:
            tier = "doplň kontakt"
        target = targets.get(org["target_id"]) if org is not None and "target_id" in org.keys() and org["target_id"] else None
        rationale = link["rationale"] or ""
        if rationale.startswith("Pridané ručne. Čo žiadať: "):
            ask = rationale.split("Čo žiadať: ", 1)[1]
        else:
            ask = _org_ask(org, targets, group) if org is not None else DEFAULT_ASK.get(group, "")
        name = f"{link['titles'] + ' ' if link['titles'] else ''}{link['full_name']}"
        rows.append({
            "odporúčanie": tier, "typ": KIND_LABEL.get(group, group), "osoba": name, "funkcia": link["role_title"] or "",
            "organizácia": link["org_name"] or "", "e-mail": email_row["value"] if email_row is not None else "",
            "e-mail_druh": email_kind, "telefón": phones[0]["value"] if phones else "",
            "prečo": rationale if not rationale.startswith("Pridané ručne") else "pridané ručne",
            "čo_žiadať": ask, "dôvody": "; ".join(reasons + [f"⛔ {b}" for b in blockers]),
            "cieľ": target["who"] if target is not None else (comp.get("cieľ") or ""),
            "stav": DONE_STATUSES.get(link["person_status"], link["person_status"]),
            "zdroj_kontaktu": (email_row["source_url"] if email_row is not None else link["person_source_url"]) or "",
            "úryvok": (email_row["source_snippet"] if email_row is not None else link["person_snippet"]) or "",
            "priorita": link["priority"], "väzba_sila": LINK_STRENGTH.get(link["link_type"], 0.0),
            "kanál": comp.get("kanál", 0.0), "role_fit": comp.get("role_fit") or 0.0,
            "person_id": pid, "org_id": link["org_id"], "skupina": group,
        })
    # Organizácie s e-mailom, z ktorých nikto nie je vybraný: oslovenie cez generický kontakt s prosbou o nasmerovanie.
    for org in orgs.values():
        if org["id"] in orgs_with_people:
            continue
        emails = [c for c in db.org_contacts(org["id"]) if c["kind"] == "email"]
        if not emails:
            continue
        group = GROUP_OF_RELATION.get(org["relation"], "customer")
        target = targets.get(org["target_id"]) if org["target_id"] else None
        roles = target_roles(target) if target is not None else []
        blockers = (["organizácia vylúčená"] if org["excluded"] or org["oh_status"] == "excluded" else []) + (
            ["prebiehajúce obstarávanie"] if org["procurement_active"] else [])
        ask = _org_ask(org, targets, group)
        if roles:
            ask = f"Požiadať o nasmerovanie na: {', '.join(roles[:3])}. {ask}"
        fit = _json(org["fit_json"])
        why = {"data_holder": f"drží dáta k hypotéze ({fit.get('zdroj dát') or fit.get('cieľ') or 'zdroj Brány 1'})",
               "expert": "expertná organizácia"}.get(group) or {
            "mentioned_with_problem": "organizácia v dôkaze", "quoted_person": "citovaná osoba je z nej",
            "procured_related": "obstaráva súvisiacu vec", "contact_search": "hľadanie podľa hypotézy",
            "segment_match": "zhoda so segmentom", "manual": "zoznam"}.get(org["relation"], org["relation"])
        rows.append({
            "odporúčanie": "neoslovovať" if blockers else "oslov", "typ": KIND_LABEL.get(group, group),
            "osoba": "", "funkcia": "", "organizácia": org["name"], "e-mail": emails[0]["value"],
            "e-mail_druh": "organizácie", "telefón": next((c["value"] for c in db.org_contacts(org["id"]) if c["kind"] == "phone"), ""),
            "prečo": why, "čo_žiadať": ask,
            "dôvody": "; ".join([why, "e-mail organizácie (bez konkrétnej osoby)"] + [f"⛔ {b}" for b in blockers]),
            "cieľ": target["who"] if target is not None else (fit.get("cieľ") or ""), "stav": "",
            "zdroj_kontaktu": emails[0]["source_url"], "úryvok": emails[0]["source_snippet"],
            "priorita": None, "väzba_sila": 0.0, "kanál": 0.4, "role_fit": 0.0,
            "person_id": None, "org_id": org["id"], "skupina": group,
        })
    rows.sort(key=lambda r: (TIERS.index(r["odporúčanie"]), -r["väzba_sila"], -(r["kanál"] or 0), -(r["role_fit"] or 0),
                             r["organizácia"], r["osoba"]))
    return rows


def shortlist_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"odporúčanie": dict(Counter(r["odporúčanie"] for r in rows)), "typ": dict(Counter(r["typ"] for r in rows)),
            "s_e-mailom": sum(1 for r in rows if r["e-mail"] and r["odporúčanie"] != "neoslovovať")}
