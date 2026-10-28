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
from datetime import date, timedelta
from typing import Any

from .config import Config
from .contact_scout import DEFAULT_ASK, KIND_LABEL, target_ask, target_roles
from .contacts import is_generic_email, org_email_ok
from .db import DB
from .people import LINK_STRENGTH
from .recipients import procurement_of

TIERS = ("oslov najprv", "oslov", "doplň kontakt", "preveriť rolu", "neoslovovať")
GROUP_OF_RELATION = {"data_holder": "data_holder", "expert_org": "expert"}
LINK_LABEL = {"quoted_about_problem": "verejne o probléme", "role_owner": "vlastník procesu", "data_holder": "drží dáta",
              "process_user": "používateľ (zažíva problém)", "org_affected": "vedenie dotknutej org.",
              "procurement_contact": "kontakt v obstarávaní", "expert": "expert"}
STRONG_LINKS = ("quoted_about_problem", "role_owner", "data_holder", "process_user")
DONE_STATUSES = {"contacted": "oslovený", "replied": "odpovedal", "interviewed": "rozhovor hotový"}
# Stav komunikácie s kontaktom naprieč hypotézami (audit 15. 10. 2026: odporúčanie nerozlišovalo nové oslovenie od
# prebiehajúcej komunikácie – oslovený aj odpovedajúci kontakt bol stále „oslov najprv“).
STAGES = ("nové", "návrh", "pripravené na odoslanie", "čaká na odpoveď", "nadviazanie", "uzavreté")
CLOSED = {"declined", "objected"}
ENGAGED = {"replied", "scheduled", "interviewed", "no_show"}
WAITING = {"sent", "no_reply", "bounced"}


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
        # adresa organizácie iba ak jej naozaj patrí (staršie záznamy mohli uložiť adresu partnera z tej istej stránky)
        org_emails = ([c for c in db.org_contacts(link["org_id"]) if c["kind"] == "email"
                       and org_email_ok(org, c["value"], cfg.people.official_domains)] if org is not None else [])
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
        if org is not None and procurement_of(org)["status"] == "confirmed":
            blockers.append("prebiehajúce obstarávanie: " + (procurement_of(org)["subject"] or "konkrétny záznam"))
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
            "účel": ", ".join(p_["rola"] for p_ in comp.get("účely") or []),
            "dôkaz_účelu": " | ".join(p_["dôkaz"] for p_ in comp.get("účely") or [])[:400],
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
        emails = [c for c in db.org_contacts(org["id"]) if c["kind"] == "email"
                  and org_email_ok(org, c["value"], cfg.people.official_domains)]
        if not emails:
            continue
        group = GROUP_OF_RELATION.get(org["relation"], "customer")
        target = targets.get(org["target_id"]) if org["target_id"] else None
        roles = target_roles(target) if target is not None else []
        blockers = (["organizácia vylúčená"] if org["excluded"] or org["oh_status"] == "excluded" else []) + (
            ["prebiehajúce obstarávanie: " + (procurement_of(org)["subject"] or "konkrétny záznam")]
            if procurement_of(org)["status"] == "confirmed" else [])
        ask = _org_ask(org, targets, group)
        if roles:
            ask = f"Požiadať o nasmerovanie na: {', '.join(roles[:3])}. {ask}"
        fit = _json(org["fit_json"])
        why = {"data_holder": f"drží dáta k hypotéze ({fit.get('zdroj dát') or fit.get('cieľ') or 'zdroj Brány 1'})",
               "expert": "expertná organizácia"}.get(group) or {
            "mentioned_with_problem": "organizácia v dôkaze", "quoted_person": "citovaná osoba je z nej",
            "uses_solution": "používa riešenie (podľa zdroja) – over zostávajúcu medzeru",
            "procured_related": "výsledok hľadania obstarávania (stav pozri v stĺpci obstarávanie)",
            "contact_search": "hľadanie podľa hypotézy",
            "segment_match": "zhoda so segmentom", "manual": "zoznam"}.get(org["relation"], org["relation"])
        rows.append({
            "odporúčanie": "neoslovovať" if blockers else "oslov", "typ": KIND_LABEL.get(group, group),
            "osoba": "", "funkcia": "", "organizácia": org["name"], "e-mail": emails[0]["value"],
            "e-mail_druh": "organizácie", "telefón": next((c["value"] for c in db.org_contacts(org["id"]) if c["kind"] == "phone"), ""),
            "prečo": why, "čo_žiadať": ask,
            "dôvody": "; ".join([why, "e-mail organizácie (bez konkrétnej osoby)"] + [f"⛔ {b}" for b in blockers]),
            "účel": "nasmerovanie", "dôkaz_účelu": "",
            "cieľ": target["who"] if target is not None else (fit.get("cieľ") or ""), "stav": "",
            "zdroj_kontaktu": emails[0]["source_url"], "úryvok": emails[0]["source_snippet"],
            "priorita": None, "väzba_sila": 0.0, "kanál": 0.4, "role_fit": 0.0,
            "person_id": None, "org_id": org["id"], "skupina": group,
        })
    _recipient_columns(db, hid, rows, orgs)
    rows.sort(key=lambda r: (TIERS.index(r["odporúčanie"]), -r["väzba_sila"], -(r["kanál"] or 0), -(r["role_fit"] or 0),
                             r["organizácia"], r["osoba"]))
    apply_queue(cfg, db, hid, rows)
    _value_columns(db, hid, rows)
    return rows


def _recipient_columns(db: DB, hid: int, rows: list[dict[str, Any]], orgs: dict[int, Any]) -> None:
    """Adresát posúdený zvlášť (zadanie 17. 10. 2026): zhoda so segmentom, stav potreby, používané riešenie, obstarávanie.
    Zákazník mimo segmentu: s dátovou/expertnou väzbou prejde do tejto roly, inak „preveriť rolu“ (mimo fronty)."""
    from .recipients import NEED_LABEL, need_state, segment_fit

    hyp = db.get_hypothesis(hid)
    cards = {}
    try:
        for c in db.org_cards_for(hid):
            cards.setdefault(c["organization_id"], c)
    except Exception:  # noqa: BLE001 – staršia schéma bez kariet
        cards = {}
    for r in rows:
        org = orgs.get(r["org_id"]) if r["org_id"] else None
        if org is None and r["org_id"]:
            org = db.get_organization(r["org_id"])
        relation = org["relation"] if org is not None and "relation" in org.keys() else None
        fit = segment_fit(db, org, hyp) if org is not None else {"zhoda": "neznáme", "dôvod": "bez organizácie"}
        r["zhoda_segmentu"] = f"{fit['zhoda']} – {fit['dôvod']}"
        r["stav_potreby"] = NEED_LABEL.get(need_state(db, hid, r["org_id"], relation), "")
        proc = procurement_of(org) if org is not None else {"status": "none"}
        r["obstarávanie"] = {"candidate": "neoverené", "confirmed": "potvrdené", "closed": "ukončené"}.get(proc["status"], "")
        if proc["status"] == "candidate":
            r["dôvody"] += f"; ⚠ obstarávanie neoverené – over na {proc['url'] or 'webe organizácie'} (schválenie čaká)"
        card = cards.get(r["org_id"])
        if card is not None:
            data = json.loads(card["card_json"] or "{}")
            tools = [(s.get("tool") or {}).get("text") for s in data.get("current_solution") or [] if isinstance(s, dict)]
            r["riešenie"] = ", ".join(t for t in tools if t) or ""
        else:
            r["riešenie"] = "zdroj: používa riešenie" if relation == "uses_solution" else ""
        if r["skupina"] == "customer" and fit["zhoda"] == "nie" and r["odporúčanie"] != "neoslovovať":
            if relation in ("data_holder", "expert_org"):
                r["skupina"] = GROUP_OF_RELATION[relation]
                r["typ"] = KIND_LABEL.get(r["skupina"], r["skupina"])
                r["dôvody"] += f"; rola upravená: {fit['dôvod']}"
            else:
                r["odporúčanie"] = "preveriť rolu"
                r["dôvody"] += f"; ⚠ nesúlad so segmentom: {fit['dôvod']}"


def _value_columns(db: DB, hid: int, rows: list[dict[str, Any]]) -> None:
    """Čo kontakt dostane, čo minimálne potrebujeme a čo ešte nevieme – z návrhu prínosu pre jeho typ."""
    from .value import best_proposal

    cache: dict[str, Any] = {}
    for r in rows:
        group = r["skupina"]
        if group not in cache:
            row = best_proposal(db, hid, group)
            cache[group] = json.loads(row["proposal_json"]) if row is not None else {}
        p = cache[group]
        r["čo_dostanú"] = p.get("deliverable") or ""
        r["potrebný_vstup"] = p.get("minimum_input") or r["čo_žiadať"]
        r["čo_nevieme"] = p.get("limitations") or ("či problém naozaj majú – potreba je hypotéza" if p else "")


def _stage(history: list, person_status: str | None, hid: int, today: date) -> tuple[str, str]:
    """(stav komunikácie, ďalšia akcia) z oslovení osoby vo všetkých hypotézach a stavu osoby."""
    statuses = {h["status"] for h in history}
    other = sorted({h["hypothesis_id"] for h in history if h["hypothesis_id"] != hid})
    where = f" (H{', H'.join(map(str, other))})" if other else ""
    if statuses & CLOSED or person_status in ("declined", "excluded"):
        return "uzavreté", "nič – odmietol(a) alebo vylúčený"
    if statuses & ENGAGED or person_status in ("replied", "interviewed"):
        if "interviewed" in statuses or person_status == "interviewed":
            return "nadviazanie", "zapísať výsledok rozhovoru a dohodnúť ďalší krok" + where
        return "nadviazanie", "dohodnúť rozhovor alebo ďalší krok" + where
    waiting = [h for h in history if h["status"] in WAITING]
    if waiting:
        if any(h["status"] == "bounced" for h in waiting) and not any(h["status"] != "bounced" for h in waiting):
            return "čaká na odpoveď", "e-mail sa vrátil – doplň iný kontakt"
        due = min((h["follow_up_due"] for h in waiting if h["follow_up_due"]), default=None)
        if due and due <= today.isoformat():
            return "čaká na odpoveď", f"poslať pripomienku (follow-up od {due})" + where
        return "čaká na odpoveď", (f"čakať na odpoveď (pripomienka {due})" if due else "čakať na odpoveď") + where
    if "approved" in statuses:
        return "pripravené na odoslanie", "odoslať ručne zo svojej schránky" + where
    if statuses & {"draft", "needs_edit"}:
        here = [h for h in history if h["hypothesis_id"] == hid and h["status"] in ("draft", "needs_edit")]
        if here:
            return "návrh", "upraviť návrh" if any(h["status"] == "needs_edit" for h in here) else "skontrolovať a schváliť návrh"
        return "návrh", "návrh pripravený v inej hypotéze" + where
    return "nové", "pripraviť návrh oslovenia"


def apply_queue(cfg: Config, db: DB, hid: int, rows: list[dict[str, Any]], today: date | None = None) -> None:
    """Doplní stav komunikácie, ďalšiu akciu a či kontakt patrí do fronty prvého oslovenia. Do fronty nepatrí
    oslovený ani odmietnutý kontakt (v ktorejkoľvek hypotéze), kanál už použitý pre iný kontakt (spoločný e-mail),
    organizácia oslovená v posledných `outreach.org_cooldown_days` dňoch a druhý kontakt tej istej organizácie."""
    today = today or date.today()
    history = db.outreach_history()
    by_person: dict[int, list] = {}
    by_org: dict[int, list] = {}
    by_value: dict[str, list] = {}
    for h in history:
        if h["person_id"]:
            by_person.setdefault(h["person_id"], []).append(h)
        if h["org_id"]:
            by_org.setdefault(h["org_id"], []).append(h)
        if h["contact_value"]:
            by_value.setdefault(h["contact_value"], []).append(h)
    cooldown = (today - timedelta(days=cfg.outreach.org_cooldown_days)).isoformat()
    queued_orgs: dict[int, str] = {}
    queued_channels: dict[str, str] = {}
    for r in rows:
        mine = by_person.get(r["person_id"], []) if r["person_id"] else [
            h for h in by_org.get(r["org_id"], []) if not h["person_id"]]
        stage, action = _stage(mine, None if not r["person_id"] else _person_status(db, r["person_id"]), hid, today)
        if r["odporúčanie"] == "neoslovovať" and stage in ("nové", "návrh"):
            stage, action = "uzavreté", "neoslovovať (" + r["dôvody"].split("⛔ ")[-1] + ")"
        eligible = stage in ("nové", "návrh") and r["odporúčanie"] in ("oslov najprv", "oslov")
        channel = (r["e-mail"] or "").lower()
        if eligible and channel:
            used = [h for h in by_value.get(channel, []) if h["status"] not in ("draft", "needs_edit", "blocked")
                    and h["person_id"] != r["person_id"]]
            if used:
                eligible, action = False, "kanál už použitý pre iný kontakt (spoločný e-mail) – počkaj na odpoveď"
            elif channel in queued_channels:
                eligible, action = False, f"spoločný e-mail – vo fronte už je {queued_channels[channel]}"
        org_id = r["org_id"]
        if eligible and org_id:
            recent = [h for h in by_org.get(org_id, []) if h["status"] not in ("draft", "needs_edit", "blocked")
                      and (h["sent_at"] or h["approved_at"] or "")[:10] >= cooldown]
            if recent:
                last = max((h["sent_at"] or h["approved_at"] or "")[:10] for h in recent)
                eligible, action = False, (f"organizácia oslovená {last} (H{recent[-1]['hypothesis_id']}) – ďalšie prvé "
                                           f"oslovenie najskôr po {cfg.outreach.org_cooldown_days} dňoch")
            elif org_id in queued_orgs:
                eligible, action = False, f"z organizácie je vo fronte {queued_orgs[org_id]} – jedno prvé oslovenie naraz"
        if eligible and not r.get("zdroj_kontaktu"):  # dodatok 16. 10.: personalizácia sa nevyrába z domnienky
            action = "doplniť podklad: zdroj dôvodu výberu (URL a úryvok) – bez neho návrh nepôjde na odoslanie"
        if eligible:
            if org_id:
                queued_orgs[org_id] = r["osoba"] or r["organizácia"]
            if channel:
                queued_channels[channel] = r["osoba"] or r["organizácia"]
        r["stav_komunikácie"] = stage
        r["ďalšia_akcia"] = action
        r["prvé_oslovenie"] = eligible


def _person_status(db: DB, pid: int) -> str | None:
    p = db.get_person(pid)
    return p["status"] if p is not None else None


def shortlist_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Súhrn: fronta prvého oslovenia počíta unikátne kanály a organizácie; odmietnutí a oslovení sa do nej nerátajú."""
    queue = [r for r in rows if r.get("prvé_oslovenie")]
    return {"odporúčanie": dict(Counter(r["odporúčanie"] for r in rows)), "typ": dict(Counter(r["typ"] for r in rows)),
            "s_e-mailom": len({(r["e-mail"] or "").lower() for r in rows if r["e-mail"] and r["odporúčanie"] != "neoslovovať"
                               and r.get("stav_komunikácie") != "uzavreté"}),
            "stav": dict(Counter(r.get("stav_komunikácie", "nové") for r in rows)),
            "fronta_prvého_oslovenia": len(queue),
            "fronta_organizácie": len({r["org_id"] or -i for i, r in enumerate(queue)}),
            "fronta_kanály": len({(r["e-mail"] or "").lower() or f"#{i}" for i, r in enumerate(queue)})}
