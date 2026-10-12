"""Návrhy oslovení pre Bránu 0 (R-OUT). Systém NIČ NEODOSIELA: iba návrh, kopírovanie a odkaz mailto:.

- Generátor (úloha draft) dostane očíslované povolené fakty F1…Fn (overené dôkazy, ktoré prešli
  entail_gate, a údaje o osobe a organizácii zo zdroja) a píše iba stredovú časť správy.
  Oslovenie, predstavenie odosielateľa, ľahké odmietnutie, podpis a pätička podľa čl. 14 GDPR
  sa skladajú deterministicky (R-OUT-4, R-OUT-5).
- Kontroly kódom (R-OUT-6): dĺžky, zakázané frázy, pätička a odkaz, povolené URL, čísla iba
  z faktov, placeholdery. Model (draft_check) navyše hľadá vety mimo faktov (R-OUT-7).
- Stavy a limity: denný limit schválení, jeden follow-up po 5–7 pracovných dňoch, námietka
  spustí forget, odmietnutie vymaže údaje po 30 dňoch (R-OUT-8 až R-OUT-11).
- Prebiehajúce súvisiace obstarávanie: návrh sa nevytvorí, iba upozornenie (R-OUT-13).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from .config import Config
from .contacts import is_generic_email
from .db import DB
from .models import ModelRouter, as_router
from .names import fold, public_salutation, split_titles
from .privacy import forget, placeholders_in, require_lia, sender_placeholders
from .prompts import DRAFT_CHECK_SYSTEM, DRAFT_SYSTEM
from .util import Deadline, utcnow
from .verify import apply_entailment, judge_entailment, numbers_missing_in_source

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"')]+|www\.[^\s<>\"')]+", re.I)
MINUTES_RE = re.compile(r"\b\d{1,2}\s*(?:(?:[–-]|až|do)\s*\d{1,2}\s*)?[–-]?\s*(?:minút\w*|minut\w*|min\.?)", re.I)
FOOTER_MARK = "\n\n--\n"
A_LINKS = ("quoted_about_problem", "org_affected")


class DraftOut(BaseModel):
    subject: str = ""
    body: str
    facts_used: list[str] = Field(default_factory=list)


class DraftProblem(BaseModel):
    sentence: str
    issue: str = ""


class DraftCheck(BaseModel):
    verdict: Literal["ok", "problems"]
    problems: list[DraftProblem] = Field(default_factory=list)


class OutreachError(ValueError):
    """Akcia s oslovením nie je povolená (limit, placeholder, stav)."""


# ======================================================================= pomocné
def words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def add_business_days(start: date, n: int, holidays: list[str] | None = None) -> date:
    """n-tý pracovný deň po dátume (víkendy a zadané sviatky sa nepočítajú)."""
    skip = set(holidays or [])
    d, count = start, 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in skip:
            count += 1
    return d


def mailto_link(email: str, subject: str, body: str, max_chars: int = 1800) -> str | None:
    """mailto: s predmetom a telom (URL-encoded aj diakritika a nové riadky); dlhý odkaz = iba kopírovanie."""
    if not email:
        return None
    link = "mailto:" + quote(email, safe="@") + "?subject=" + quote(subject or "", safe="") + "&body=" + quote(
        (body or "").replace("\r\n", "\n").replace("\n", "\r\n"), safe=""
    )
    return link if len(link) <= max_chars else None


def retention_months(cfg: Config) -> int:
    return max(1, round(cfg.privacy.retention_days_after_interaction / 30.4))


def surname_of(full_name: str) -> str:
    clean, _ = split_titles(full_name)
    return clean.split()[-1] if clean.split() else ""


def greeting(person, org_sector: str | None) -> str:
    """Oslovenie iba z overených údajov (R-OUT-5)."""
    gender = person["gender_hint"]
    if org_sector == "public":
        functional = public_salutation(person["role_title"], gender)
        if functional:
            return functional
    if gender == "male":
        return f"Dobrý deň, pán {surname_of(person['full_name'])},"
    if gender == "female":
        return f"Dobrý deň, pani {surname_of(person['full_name'])},"
    return "Dobrý deň,"


def footer(cfg: Config, source_url: str) -> str:
    s = cfg.outreach.sender
    return (
        f"Váš pracovný kontakt som našiel na {source_url}. Použijem ho iba na dohodnutie tohto rozhovoru "
        f"(oprávnený záujem), uchovám ho najviac {retention_months(cfg)} mesiacov a na požiadanie ho hneď vymažem; "
        f"spracúvaniu môžete kedykoľvek namietať.\nViac: {s.privacy_notice_url}"
    )


def signature(cfg: Config) -> str:
    s = cfg.outreach.sender
    lines = ["S pozdravom", s.name, s.email]
    if s.phone:
        lines.append(s.phone)
    return "\n".join(lines)


def intro_line(cfg: Config) -> str:
    s = cfg.outreach.sender
    return f"Volám sa {s.name}, som {s.affiliation}. Nejde o ponuku ani predaj, robím osobný výskum tohto problému."


DECLINE = "Ak to pre vás nie je téma, stačí odpísať „nie“ a viac sa neozvem."
DEFAULT_SUBJECT = "Krátka otázka k vašej skúsenosti – 20 minút?"


def compose(cfg: Config, channel: str, person, org_sector: str | None, core: str, subject: str, source_url: str,
            contact_value: str | None = None) -> tuple[str | None, str]:
    """Zloží celú správu z jadra od modelu a pevných častí. Vráti (predmet, text)."""
    core = core.strip()
    s = cfg.outreach.sender
    if len(core) > 1 and core[0].isupper() and core[1].islower():
        core = core[0].lower() + core[1:]  # po oslovení s čiarkou pokračuje text malým písmenom
    if channel == "email":
        body = "\n\n".join([greeting(person, org_sector), core, intro_line(cfg) + "\n" + DECLINE, signature(cfg)])
        subject = subject.strip() or DEFAULT_SUBJECT  # model predmet občas vynechá
        return subject[: cfg.outreach.email_subject_max_chars], body + FOOTER_MARK + footer(cfg, source_url)
    if channel == "linkedin":
        return None, f"Dobrý deň, {core} Info o údajoch: {s.privacy_notice_url}"
    # telefón: iba skript na ručné volanie (R-OUT-14)
    email_line = f"Je správna adresa {contact_value}?" if contact_value and "@" in contact_value else "Akú adresu mám použiť?"
    script = (
        f"Dobrý deň, tu {s.name}. {core}\nNič nepredávam. Máte teraz dve minúty, alebo sa vám hodí iný čas "
        f"na 20-minútový rozhovor?\n\nAk „pošlite mi e-mail“: Pošlem. {email_line} (do e-mailu potom pätička)"
    )
    return None, script


def facts_text(facts: list[dict[str, Any]]) -> str:
    return "\n".join(f"{f['id']}: {f['text']}" for f in facts)


# ======================================================================= kontroly
def check_draft(cfg: Config, channel: str, subject: str | None, body: str, core: str, facts: list[dict[str, Any]],
                source_url: str) -> dict[str, Any]:
    """Deterministické kontroly návrhu (R-OUT-6). Vráti {"ok": bool, "problems": [...], ...}."""
    o = cfg.outreach
    problems: list[str] = []
    main = body.split(FOOTER_MARK)[0]
    info: dict[str, Any] = {"words": words(main), "chars": len(body)}
    if channel == "email":
        if subject is None or not subject.strip():
            problems.append("Chýba predmet.")
        elif len(subject) > o.email_subject_max_chars:
            problems.append(f"Predmet má {len(subject)} znakov (max. {o.email_subject_max_chars}).")
        if words(main) > o.email_max_words:
            problems.append(f"Text má {words(main)} slov bez pätičky (max. {o.email_max_words}).")
        if FOOTER_MARK not in body or "oprávnený záujem" not in body:
            problems.append("Chýba pätička s informáciou o spracúvaní.")
    elif channel == "linkedin":
        if len(body) > o.linkedin_note_max_chars:
            problems.append(f"Poznámka má {len(body)} znakov vrátane odkazu (max. {o.linkedin_note_max_chars}).")
    elif channel == "phone":
        opener = body.split("\n\n")[0]
        if words(opener) > o.phone_opener_max_words:
            problems.append(f"Úvod má {words(opener)} slov (max. {o.phone_opener_max_words}).")
    if channel in ("email", "linkedin") and o.sender.privacy_notice_url not in body:
        problems.append("Chýba odkaz na informáciu o spracúvaní.")
    folded = fold(body)
    bad = [p for p in o.forbidden_phrases if re.search(r"(?<![a-z])" + re.escape(fold(p)) + r"(?![a-z])", folded)]
    if bad:
        problems.append("Zakázané frázy: " + ", ".join(bad) + ".")
    allowed = {u.rstrip("/.,") for u in (o.sender.privacy_notice_url, source_url) if u}
    allowed |= {f["url"].rstrip("/.,") for f in facts if f.get("url")}
    urls = [u.rstrip("/.,;") for u in URL_RE.findall(body)]
    foreign = [u for u in urls if u not in allowed]
    if foreign:
        problems.append("Nepovolené odkazy: " + ", ".join(foreign[:3]) + ".")
    fact_text = facts_text(facts)
    # Dĺžka rozhovoru v prosbe („20 minút“, „20–30 min“) nie je tvrdenie o príjemcovi.
    checked = MINUTES_RE.sub(" ", f"{subject or ''} {core}")
    numbers = numbers_missing_in_source(checked, fact_text)
    if numbers:
        problems.append("Čísla mimo faktov: " + ", ".join(numbers) + ".")
    info["placeholders"] = placeholders_in(body) + placeholders_in(subject)
    info["problems"] = problems
    info["ok"] = not problems
    return info


# ======================================================================= fakty
def gate_claim(cfg: Config, db: DB, router: ModelRouter, claim) -> str:
    """entail_gate (k hlasov presného modelu) pre tvrdenie použité ako fakt v oslovení."""
    if claim["gate_verdict"]:
        return claim["gate_verdict"]
    doc = db.get_document(claim["document_id"])
    res = judge_entailment(cfg, router, claim, doc["text"], task="entail_gate")
    verdict = res.value.verdict if res.ok else "undecided"
    db.set_claim_gate(claim["id"], verdict, res.low_agreement)
    if res.ok and verdict == "unsupported":
        apply_entailment(db, db.get_claim(claim["id"]), "unsupported", res.value.reason)  # fakt neobstál ani v overení
    return verdict


def build_facts(cfg: Config, db: DB, router: ModelRouter, hyp, person, link, org) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    def add(text: str, url: str | None, kind: str, claim_id: int | None = None) -> None:
        facts.append({"id": f"F{len(facts) + 1}", "text": text, "url": url, "kind": kind, "claim_id": claim_id})

    role = f", {person['role_title']}" if person["role_title"] else ""
    add(f"Príjemca: {split_titles(person['full_name'])[0]}{role}" + (f" ({org['name']})" if org else ""),
        person["source_url"], "person")
    claims = []
    if link is not None and link["evidence_claim_id"]:
        c = db.get_claim(link["evidence_claim_id"])
        if c is not None:
            claims.append(c)
    for e in db.evidence_for(hyp["id"]):
        if len(claims) >= cfg.outreach.facts_per_draft + (1 if link is not None and link["evidence_claim_id"] else 0):
            break
        if e["status"] == "verified" and all(e["id"] != c["id"] for c in claims):
            claims.append(e)
    for c in claims:
        if gate_claim(cfg, db, router, c) != "supported":
            continue
        doc = db.get_document(c["document_id"])
        when = (doc["fetched_at"] or "")[:7] if doc else ""
        add(f"{c['problem']} Citát: „{c['quote']}“ (zdroj {doc['domain'] if doc else '?'}, stiahnuté {when})",
            doc["url"] if doc else None, "evidence", c["id"])
    return facts


# ======================================================================= návrhy
def draft_messages(cfg: Config, hyp, person, org, link_type: str, variant: str, channel: str,
                   facts: list[dict[str, Any]], generic: bool) -> list[dict[str, str]]:
    o = cfg.outreach
    fixed = words(intro_line(cfg)) + words(DECLINE) + words(signature(cfg)) + 6
    max_words = max(40, o.email_max_words - fixed) if channel == "email" else o.phone_opener_max_words - 25
    linkedin_chars = o.linkedin_note_max_chars - len(f"Dobrý deň,  Info o údajoch: {o.sender.privacy_notice_url}")
    system = DRAFT_SYSTEM.format(max_words=max_words, linkedin_chars=max(60, linkedin_chars),
                                 subject_chars=o.email_subject_max_chars)
    addressee = "a generic organization mailbox: ask to be directed to the right person" if generic else "the person"
    user = (
        f"CHANNEL: {channel}\nVARIANT: {variant}\nADDRESSEE: {addressee}\n"
        f"PERSON ROLE: {person['role_title'] or '-'}\nORGANIZATION TYPE: {org['sector_kind'] if org is not None and org['sector_kind'] else '-'}\n"
        f"WHY THIS PERSON: {link_type}\nPROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment']}\n\n"
        f"FACTS:\n<untrusted_document>\n{facts_text(facts)}\n</untrusted_document>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def model_check(cfg: Config, router: ModelRouter, body: str, facts: list[dict[str, Any]], outreach_ref: int | None) -> dict[str, Any]:
    res = router.run_task(
        "draft_check",
        [
            {"role": "system", "content": DRAFT_CHECK_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {"role": "user", "content": f"FACTS:\n<untrusted_document>\n{facts_text(facts)}\n</untrusted_document>\n\n"
                                        f"DRAFT:\n<untrusted_document>\n{body.split(FOOTER_MARK)[0]}\n</untrusted_document>"},
        ],
        DraftCheck,
        object_type="outreach" if outreach_ref else None,
        object_id=outreach_ref,
        vote_key=lambda r: r.verdict,
        max_tokens=600,
    )
    if not res.ok:
        return {"verdict": "undecided", "problems": []}
    return {"verdict": res.value.verdict, "problems": [p.model_dump() for p in res.value.problems]}


def plan_channels(db: DB, person, org_id: int | None) -> list[tuple[str, Any, bool]]:
    """(kanál, kontakt, generický) podľa dostupných kontaktov osoby."""
    contacts = db.contacts_for_person(person["id"])
    out = []
    personal_email = next((c for c in contacts if c["kind"] == "email" and not is_generic_email(c["value"])), None)
    any_email = personal_email or next((c for c in contacts if c["kind"] == "email"), None)
    hidden = next((c for c in contacts if c["kind"] == "hidden_contact"), None)
    if any_email is not None:
        out.append(("email", any_email, any_email is not personal_email))
    elif hidden is not None:
        out.append(("email", hidden, False))  # adresu treba otvoriť ručne na stránke
    elif org_id is not None:
        generic = next((c for c in db.org_contacts(org_id) if c["kind"] == "email"), None)
        if generic is not None:
            out.append(("email", generic, True))
    profile = next((c for c in contacts if c["kind"] == "profile_url"), None)
    if profile is not None:
        out.append(("linkedin", profile, False))
    phone = next((c for c in contacts if c["kind"] == "phone"), None)
    if phone is not None and any_email is None:
        out.append(("phone", phone, False))
    return out


def create_drafts_for(cfg: Config, db: DB, router: ModelRouter, hyp, link, budget: Counter) -> Counter:
    stats: Counter = Counter()
    person = db.get_person(link["person_id"])
    org = db.get_organization(person["org_id"]) if person["org_id"] else None
    oh = db.org_hypothesis(person["org_id"], hyp["id"]) if person["org_id"] else None
    if db.outreach_for_person(person["id"], hyp["id"]):
        return stats  # návrhy už existujú (úpravy robí človek v GUI)
    if oh is not None and oh["procurement_active"]:
        # R-OUT-13: počas súťaže iba oficiálna cesta (PTK, vysvetľovanie súťažných podkladov)
        db.insert_outreach(person["id"], hyp["id"], "email", "-", None,
                           "Návrh sa nevytvoril: organizácia možno práve obstaráva súvisiacu vec. Komunikuj iba "
                           "oficiálnou cestou (prípravné trhové konzultácie, vysvetľovanie súťažných podkladov). "
                           + (oh["procurement_note"] or ""), [], {"ok": False, "problems": ["prebiehajúce obstarávanie"]}, "blocked")
        stats["blocked_procurement"] += 1
        return stats
    channels = [c for c in plan_channels(db, person, person["org_id"]) if c[0] in cfg.outreach.channels]
    if not channels:
        stats["no_channel"] += 1
        return stats
    facts = build_facts(cfg, db, router, hyp, person, link, org)
    if not any(f["kind"] == "evidence" for f in facts):
        stats["no_supported_facts"] += 1
    variants_for_email = ["A", "B"] if link["link_type"] in A_LINKS and any(f["kind"] == "evidence" for f in facts) else ["B"]
    for channel, contact, generic in channels:
        variants = variants_for_email if channel == "email" else [variants_for_email[0]]
        for variant in variants:
            if cfg.outreach.max_drafts_per_run and budget["drafts"] >= cfg.outreach.max_drafts_per_run:
                stats["deferred"] += 1
                return stats
            budget["drafts"] += 1
            res = router.run_task(
                "draft",
                draft_messages(cfg, hyp, person, org, link["link_type"], variant, channel, facts, generic),
                DraftOut,
                object_type="person",
                object_id=person["id"],
                max_tokens=900,
            )
            if not res.ok:
                stats["llm_failures"] += 1
                continue
            out = res.value
            source_url = contact["source_url"] if contact is not None else person["source_url"]
            subject, body = compose(cfg, channel, person, org["sector_kind"] if org is not None else None, out.body,
                                    out.subject, source_url, contact["value"] if contact is not None else None)
            checks = check_draft(cfg, channel, subject, body, out.body, facts, source_url)
            checks.update(core=out.body.strip(), subject_core=out.subject.strip(), facts_used=out.facts_used,
                          generic=generic, contact_kind=contact["kind"] if contact is not None else None)
            status = "draft"
            if checks["ok"]:
                mc = model_check(cfg, router, body, facts, None)
                checks["model"] = mc
                if mc["verdict"] == "problems" and mc["problems"]:
                    status = "needs_edit"
                elif mc["verdict"] == "undecided":
                    checks["model_note"] = "Kontrola modelom bez výsledku; prečítaj návrh pozorne."
            else:
                status = "needs_edit"
            db.insert_outreach(person["id"], hyp["id"], channel, variant, subject, body, facts, checks, status,
                               contact["id"] if contact is not None else None)
            stats[status] += 1
    return stats


def refresh_draft(cfg: Config, db: DB, llm: Any, outreach_id: int) -> str:
    """Znova zloží neupravený návrh z uloženého jadra (napr. po zmene kontrol alebo profilu odosielateľa)
    a prepočíta kontroly; model sa volá iba na kontrolu faktov. Ručne upravené návrhy sa nemenia."""
    router = as_router(llm, cfg, db)
    row = db.get_outreach(outreach_id)
    checks = json.loads(row["checks_json"] or "{}")
    if row["status"] not in ("draft", "needs_edit") or checks.get("edited") or not checks.get("core"):
        return row["status"]
    person = db.get_person(row["person_id"])
    org = db.get_organization(person["org_id"]) if person["org_id"] else None
    contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
    source_url = contact["source_url"] if contact is not None else person["source_url"]
    facts = json.loads(row["facts_json"] or "[]")
    subject, body = compose(cfg, row["channel"], person, org["sector_kind"] if org is not None else None, checks["core"],
                            checks.get("subject_core") or "", source_url, contact["value"] if contact is not None else None)
    new = check_draft(cfg, row["channel"], subject, body, checks["core"], facts, source_url)
    new.update({k: checks.get(k) for k in ("core", "subject_core", "facts_used", "generic", "contact_kind")})
    status = "needs_edit"
    if new["ok"]:
        mc = checks.get("model") or model_check(cfg, router, body, facts, outreach_id)
        new["model"] = mc
        status = "needs_edit" if mc["verdict"] == "problems" and mc["problems"] else "draft"
    db.update_outreach(outreach_id, subject=subject, body=body, checks_json=new, status=status)
    return status


def run_drafts(cfg: Config, db: DB, llm: Any, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    """Návrhy pre vybraných ľudí (najprv najvyššia priorita naprieč hypotézami)."""
    require_lia(cfg)
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    budget: Counter = Counter()
    ids = hypothesis_ids or [h["id"] for h in db.people_enabled_hypotheses()]
    queue = []
    for hid in ids:
        seen: set[int] = set()
        for link in db.links_for_hypothesis(hid):
            if link["priority"] is None or link["person_id"] in seen or link["is_expert"]:
                continue
            seen.add(link["person_id"])
            queue.append((link["priority"], hid, link))
    queue.sort(key=lambda x: -x[0])
    for _, hid, link in queue:
        if deadline.expired():
            break
        stats.update(create_drafts_for(cfg, db, router, db.get_hypothesis(hid), link, budget))
        if cfg.outreach.max_drafts_per_run and budget["drafts"] >= cfg.outreach.max_drafts_per_run:
            break
    return dict(stats)


# ======================================================================= akcie človeka (GUI)
def recheck(cfg: Config, db: DB, outreach_id: int, body: str, subject: str | None, router: ModelRouter | None = None) -> dict:
    """Po ručnej úprave: znova kontroly kódom (a voliteľne modelom); stav draft alebo needs_edit."""
    row = db.get_outreach(outreach_id)
    facts = json.loads(row["facts_json"] or "[]")
    old = json.loads(row["checks_json"] or "{}")
    person = db.get_person(row["person_id"])
    contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
    source_url = contact["source_url"] if contact is not None else person["source_url"]
    core = body.split(FOOTER_MARK)[0]
    checks = check_draft(cfg, row["channel"], subject, body, core, facts, source_url)
    checks.update({k: old.get(k) for k in ("facts_used", "generic", "contact_kind")}, core=old.get("core"), edited=True)
    if router is not None and checks["ok"]:
        checks["model"] = model_check(cfg, router, body, facts, outreach_id)
        if checks["model"]["verdict"] == "problems" and checks["model"]["problems"]:
            checks["ok"] = False
    db.update_outreach(outreach_id, body=body, subject=subject, checks_json=checks,
                       status="draft" if checks["ok"] else "needs_edit")
    return checks


def approve(cfg: Config, db: DB, outreach_id: int, now: datetime | None = None) -> None:
    """Schválenie na ručné odoslanie. Zablokované pri placeholderoch v profile odosielateľa (R-OUT-15)
    a po dennom limite (R-OUT-10)."""
    require_lia(cfg)
    now = now or datetime.now(timezone.utc)
    row = db.get_outreach(outreach_id)
    if row is None:
        raise OutreachError("Oslovenie neexistuje.")
    if row["status"] != "draft":
        raise OutreachError(f"Schváliť sa dá iba návrh bez problémov (stav je {row['status']}).")
    missing = sender_placeholders(cfg)
    if missing:
        raise OutreachError("Najprv doplň profil odosielateľa v config.yaml (outreach.sender): " + ", ".join(missing))
    day = now.date().isoformat()
    if db.approvals_on(day) >= cfg.outreach.daily_cap:
        raise OutreachError(f"Denný limit {cfg.outreach.daily_cap} schválení je vyčerpaný.")
    person = db.get_person(row["person_id"])
    checks = json.loads(row["checks_json"] or "{}")
    body, subject = row["body"], row["subject"]
    if checks.get("core") and not checks.get("edited"):  # pevné časti s aktuálnym profilom odosielateľa
        contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
        org = db.get_organization(person["org_id"]) if person["org_id"] else None
        subject, body = compose(cfg, row["channel"], person, org["sector_kind"] if org is not None else None,
                                checks["core"], checks.get("subject_core") or (row["subject"] or ""),
                                contact["source_url"] if contact is not None else person["source_url"],
                                contact["value"] if contact is not None else None)
    left = placeholders_in(body) + placeholders_in(subject)
    if left:
        raise OutreachError("V návrhu ostali placeholdery: " + ", ".join(left))
    db.update_outreach(outreach_id, status="approved", approved_at=now.isoformat(timespec="seconds"), body=body, subject=subject)
    db.update_person(person["id"], status="approved")


def mark_sent(cfg: Config, db: DB, outreach_id: int, when: date | None = None) -> date:
    """Človek označí ručne odoslanú správu; follow-up pripadne na 5. pracovný deň (R-OUT-9)."""
    when = when or date.today()
    row = db.get_outreach(outreach_id)
    if row["status"] not in ("approved", "sent"):
        raise OutreachError("Odoslané môže byť iba schválené oslovenie.")
    due = add_business_days(when, cfg.outreach.follow_up_business_days[0], cfg.outreach.holidays)
    follow = due.isoformat() if row["follow_up_count"] < cfg.outreach.max_follow_ups else None
    db.update_outreach(outreach_id, status="sent", sent_at=when.isoformat(), follow_up_due=follow)
    db.update_person(row["person_id"], status="contacted", last_interaction_at=utcnow())
    return due


def follow_up_text(row) -> tuple[str, str]:
    subject = f"Re: {row['subject']}" if row["subject"] else "Re:"
    body = ("Dobrý deň, pripomínam sa s otázkou nižšie. Ak to pre vás nie je téma, pochopím a viac sa neozvem.\n"
            "S pozdravom\n\n> " + (row["body"] or "").split(FOOTER_MARK)[0].replace("\n", "\n> "))
    return subject, body


def mark_follow_up_sent(cfg: Config, db: DB, outreach_id: int) -> None:
    row = db.get_outreach(outreach_id)
    db.update_outreach(outreach_id, follow_up_count=row["follow_up_count"] + 1, follow_up_due=None)
    db.update_person(row["person_id"], last_interaction_at=utcnow())


PERSON_STATUS_FOR = {"replied": "replied", "scheduled": "replied", "interviewed": "interviewed", "declined": "declined",
                     "no_show": "replied", "no_reply": "contacted", "bounced": "contacted"}


def set_status(cfg: Config, db: DB, outreach_id: int, status: str) -> dict:
    """Zmena stavu po ručnej akcii. Námietka spustí forget a potlačenie; odmietnutie vymaže údaje po 30 dňoch."""
    row = db.get_outreach(outreach_id)
    if row is None:
        raise OutreachError("Oslovenie neexistuje.")
    if status == "objected":
        return forget(cfg, db, person_id=row["person_id"], reason="objection")
    db.update_outreach(outreach_id, status=status)
    person_status = PERSON_STATUS_FOR.get(status)
    if person_status:
        fields: dict[str, Any] = {"status": person_status, "last_interaction_at": utcnow()}
        if status == "declined":
            fields["purge_after"] = (datetime.now(timezone.utc) + timedelta(days=cfg.privacy.retention_days_declined)).isoformat(timespec="seconds")
        if status == "bounced":
            fields.pop("status")
        db.update_person(row["person_id"], **fields)
    return {"status": status}
