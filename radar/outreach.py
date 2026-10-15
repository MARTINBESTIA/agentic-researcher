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

Režimy podľa účelu (audit 15. 10. 2026, balík C): discovery (zákazník – posledný výskyt problému, prípadne
podmienená ponuka malého výstupu), data_request (držiteľ dát – jeden minimálny vstup a čo dostane späť),
expert_review (expert – jedna odborná otázka, na oplátku zhrnutie zistení), pilot (po potvrdení problému – konkrétna
skúška s otvoreným obchodným zámerom). Pevný úvod, veta o obchodných podmienkach (bezplatné demo a skúška, platenie až
po dohode pri riešení s preukázanou hodnotou – `outreach.value_offer`) a pätička sa menia s režimom; text s ponukou
netvrdí, že nejde o ponuku. Ku každému návrhu je uložený reťazec „prečo tento kontakt → predpokladaná potreba → výstup
→ minimálny vstup“ (package_json) a pripravenosť (ready / needs_input – zástupné polia, chýba zdroj dôvodu výberu).
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


def footer(cfg: Config, source_url: str, mode: str | None = None) -> str:
    s = cfg.outreach.sender
    return (
        f"Váš pracovný kontakt som našiel na {source_url}. Použijem ho iba {MODE_PURPOSE.get(mode, MODE_PURPOSE[None])} "
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

MODES = ("discovery", "data_request", "expert_review", "pilot")
MODE_OF_GROUP = {"customer": "discovery", "data_holder": "data_request", "expert": "expert_review"}
MODE_LABEL = {"discovery": "rozhovor o skúsenosti", "data_request": "žiadosť o dáta", "expert_review": "odborné overenie",
              "pilot": "návrh skúšky", None: "rozhovor (pôvodný režim)"}
MODE_SUBJECT = {"discovery": DEFAULT_SUBJECT, "data_request": "Otázka k vašim dátam",
                "expert_review": "Odborné overenie predpokladu", "pilot": "Návrh krátkej skúšky"}
MODE_INTRO = {
    "discovery": "Volám sa {name}, som {affiliation}. Skúmam tento problém a overujem, či by pomohol konkrétny malý výstup.",
    "data_request": "Volám sa {name}, som {affiliation}. Overujem, či sa dá z dostupných dát pripraviť užitočný výstup.",
    "expert_review": "Volám sa {name}, som {affiliation}. Overujem predpoklady o tomto procese.",
    "pilot": "Volám sa {name}, som {affiliation}. Pripravujem riešenie, ktoré by som neskôr chcel ponúkať aj ako "
             "platenú službu.",
}
MODE_PURPOSE = {None: "na dohodnutie tohto rozhovoru", "discovery": "na túto komunikáciu (rozhovor alebo ukážka)",
                "data_request": "na túto komunikáciu o dátach", "expert_review": "na túto komunikáciu",
                "pilot": "na dohodnutie skúšky"}
# Schválená obchodná formulácia (audit 15. 10. 2026, 8.2) – komerčný adresát, a tím experta či správcu dát pri
# zmienke o možnom budúcom využití.
POLICY_TEXT = ("Príprava dema a dohodnutá skúška sú bezplatné. O platenom používaní by sme sa dohodli až vtedy, keď by "
               "riešenie fungovalo vo vašej prevádzke a preukázateľne vám prinášalo hodnotu.")
POLICY_TEXT_TEAM = ("Ak by sa potvrdilo využitie aj pre váš tím, demo a dohodnutá skúška by boli bezplatné. O platenom "
                    "používaní by sme sa dohodli až pri riešení, ktoré funguje vo vašej prevádzke a preukázateľne vám "
                    "prináša hodnotu.")
NOT_OFFER_RE = re.compile(r"nejde o ponuku|nic nepredavam|nekomercn|iba osobny vyskum|osobny vyskum")
HEDGE_RE = re.compile(r"\?|predpoklad|možno|môže|mohl|či |overiť|ak (je|sa|by|áno)|neviem|ak to", re.I)


def policy_text(cfg: Config, mode: str | None, offer: bool, commercial: bool) -> str | None:
    """Veta o obchodných podmienkach: pri ponuke dema alebo skúšky komerčnému adresátovi a vždy v režime pilot;
    expertovi a verejnému správcovi dát iba pri zmienke o využití ich tímom (offer). Bez bezplatného dema nič."""
    vo = cfg.outreach.value_offer
    if mode is None or not (vo.demo_free and vo.trial_free):
        return None
    if mode == "pilot" or (mode == "discovery" and offer) or (mode == "data_request" and offer and commercial):
        return POLICY_TEXT
    if mode == "expert_review" and offer and commercial:
        return POLICY_TEXT_TEAM
    return None


def compose(cfg: Config, channel: str, person, org_sector: str | None, core: str, subject: str, source_url: str,
            contact_value: str | None = None, mode: str | None = None, policy: str | None = None) -> tuple[str | None, str]:
    """Zloží celú správu z jadra od modelu a pevných častí podľa režimu (None = pôvodné texty). Vráti (predmet, text)."""
    core = core.strip()
    s = cfg.outreach.sender
    if len(core) > 1 and core[0].isupper() and core[1].islower():
        core = core[0].lower() + core[1:]  # po oslovení s čiarkou pokračuje text malým písmenom
    intro = intro_line(cfg) if mode is None else MODE_INTRO[mode].format(name=s.name, affiliation=s.affiliation)
    if channel == "email":
        parts = [greeting(person, org_sector) if person is not None else "Dobrý deň,", core]
        if policy:
            parts.append(policy)
        body = "\n\n".join(parts + [intro + "\n" + DECLINE, signature(cfg)])
        subject = subject.strip() or MODE_SUBJECT.get(mode or "", DEFAULT_SUBJECT)  # model predmet občas vynechá
        return subject[: cfg.outreach.email_subject_max_chars], body + FOOTER_MARK + footer(cfg, source_url, mode)
    if channel == "linkedin":
        return None, f"Dobrý deň, {core} Info o údajoch: {s.privacy_notice_url}"
    # telefón: iba skript na ručné volanie (R-OUT-14)
    email_line = f"Je správna adresa {contact_value}?" if contact_value and "@" in contact_value else "Akú adresu mám použiť?"
    honest = "Nič nepredávam." if mode is None or (mode != "pilot" and not policy) else (policy or "")
    script = (
        f"Dobrý deň, tu {s.name}. {core}\n{honest} Máte teraz dve minúty, alebo sa vám hodí iný čas "
        f"na 20-minútový rozhovor?\n\nAk „pošlite mi e-mail“: Pošlem. {email_line} (do e-mailu potom pätička)"
    )
    return None, script


def facts_text(facts: list[dict[str, Any]]) -> str:
    return "\n".join(f"{f['id']}: {f['text']}" for f in facts)


# ======================================================================= kontroly
SENDER_GENDER = {
    "": ('The gender of the sender is unknown: avoid gendered first-person forms ("rád/rada", "chcel/chcela by som", '
         '"mal/mala som", "dozvedel/dozvedela"); write e.g. "Chcem sa opýtať", "Zaujíma ma", "Uvítam".'),
    "m": 'The sender is a man: use masculine first-person forms ("rád by som", "chcel by som").',
    "f": 'The sender is a woman: use feminine first-person forms ("rada by som", "chcela by som").',
}
# Rodovo určená 1. osoba odosielateľa (iba spojenia, ktoré nemajú iný význam: „rada“ samotné je aj podstatné meno).
GENDERED_FIRST_PERSON_RE = re.compile(
    r"\b(?:(?:rád|rada|chcel|chcela|mal|mala|bol|bola|uvítal|uvítala|dozvedel|dozvedela) by som|"
    r"by som (?:sa )?(?:rád|rada|chcel|chcela|dozvedel|dozvedela)|som (?:rád|rada)|"
    r"(?:mal|mala|bol|bola|robil|robila|hovoril|hovorila|čítal|čítala|zistil|zistila) som)\b", re.I)


def check_draft(cfg: Config, channel: str, subject: str | None, body: str, core: str, facts: list[dict[str, Any]],
                source_url: str, mode: str | None = None, policy: str | None = None, allowed_text: str = "",
                need_status: str | None = None) -> dict[str, Any]:
    """Deterministické kontroly návrhu (R-OUT-6) a podľa režimu: veta o podmienkach nie je v limite slov, text
    s ponukou netvrdí, že nejde o ponuku, potreba bez potvrdenia je hypotéza alebo otázka, čísla iba z faktov,
    návrhu prínosu alebo schváleného záväzku. Vráti {"ok": bool, "problems": [...], ...}."""
    o = cfg.outreach
    problems: list[str] = []
    main = body.split(FOOTER_MARK)[0]
    counted = words(main) - (words(policy) if policy and policy in main else 0)
    info: dict[str, Any] = {"words": counted, "chars": len(body)}
    if channel == "email":
        if subject is None or not subject.strip():
            problems.append("Chýba predmet.")
        elif len(subject) > o.email_subject_max_chars:
            problems.append(f"Predmet má {len(subject)} znakov (max. {o.email_subject_max_chars}).")
        if counted > o.email_max_words:
            problems.append(f"Text má {counted} slov bez pätičky a podmienok (max. {o.email_max_words}).")
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
    # zakázané frázy iba v texte od modelu (pevná veta o podmienkach je schválená politika); pri ponuke dema sú
    # povolené frázy z outreach.offer_allowed_phrases
    folded = fold(core if mode is not None else body)
    allowed_phrases = {fold(p) for p in o.offer_allowed_phrases} if policy else set()
    bad = [p for p in o.forbidden_phrases if fold(p) not in allowed_phrases
           and re.search(r"(?<![a-z])" + re.escape(fold(p)) + r"(?![a-z])", folded)]
    if bad:
        problems.append("Zakázané frázy: " + ", ".join(bad) + ".")
    if (policy or mode == "pilot") and NOT_OFFER_RE.search(fold(main)):
        problems.append("Text s ponukou dema alebo skúšky tvrdí, že nejde o ponuku alebo iba o osobný výskum.")
    if mode is not None and need_status != "confirmed" and core.strip() and not HEDGE_RE.search(core):
        problems.append("Potreba nie je potvrdená – formuluj ju ako predpoklad alebo otázku.")
    if not o.sender.gender:  # rod odosielateľa sa neodhaduje: buď neutrálne, alebo outreach.sender.gender
        gendered = sorted({m.group(0).lower() for m in GENDERED_FIRST_PERSON_RE.finditer(main)})
        if gendered:
            problems.append("Rodovo určená 1. osoba (" + ", ".join(gendered[:3]) + "): prepíš neutrálne alebo nastav "
                            "outreach.sender.gender v config.local.yaml.")
    allowed = {u.rstrip("/.,") for u in (o.sender.privacy_notice_url, source_url) if u}
    allowed |= {f["url"].rstrip("/.,") for f in facts if f.get("url")}
    urls = [u.rstrip("/.,;") for u in URL_RE.findall(body)]
    foreign = [u for u in urls if u not in allowed]
    if foreign:
        problems.append("Nepovolené odkazy: " + ", ".join(foreign[:3]) + ".")
    fact_text = facts_text(facts) + " " + allowed_text
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


def build_facts(cfg: Config, db: DB, router: ModelRouter, hyp, person, link, org,
                contact=None) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    def add(text: str, url: str | None, kind: str, claim_id: int | None = None) -> None:
        facts.append({"id": f"F{len(facts) + 1}", "text": text, "url": url, "kind": kind, "claim_id": claim_id})

    if person is not None:
        role = f", {person['role_title']}" if person["role_title"] else ""
        add(f"Príjemca: {split_titles(person['full_name'])[0]}{role}" + (f" ({org['name']})" if org else ""),
            person["source_url"], "person")
    elif org is not None:  # funkčná schránka organizácie – konkrétna osoba sa nevymýšľa
        add(f"Príjemca: funkčný kontakt organizácie {org['name']} (požiadať o nasmerovanie na správnu osobu)",
            contact["source_url"] if contact is not None else org["website"], "org")
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
                   facts: list[dict[str, Any]], generic: bool, mode: str | None = None,
                   package: dict[str, Any] | None = None, policy: str | None = None) -> list[dict[str, str]]:
    o = cfg.outreach
    intro = intro_line(cfg) if mode is None else MODE_INTRO[mode].format(name=o.sender.name,
                                                                         affiliation=o.sender.affiliation)
    fixed = words(intro) + words(DECLINE) + words(signature(cfg)) + 6
    max_words = max(40, o.email_max_words - fixed) if channel == "email" else o.phone_opener_max_words - 25
    linkedin_chars = o.linkedin_note_max_chars - len(f"Dobrý deň,  Info o údajoch: {o.sender.privacy_notice_url}")
    system = DRAFT_SYSTEM.format(max_words=max_words, linkedin_chars=max(60, linkedin_chars),
                                 subject_chars=o.email_subject_max_chars, sender_gender=SENDER_GENDER.get(
                                     o.sender.gender, SENDER_GENDER[""]))
    addressee = "a generic organization mailbox: ask to be directed to the right person" if generic else "the person"
    pkg = package or {}
    proposal = (f"NEED (state {pkg.get('potreba', {}).get('stav', 'hypothesis')}): {pkg.get('potreba', {}).get('text', '-')}\n"
                f"DELIVERABLE: {pkg.get('výstup') or '-'}\nDECISION IT SUPPORTS: {pkg.get('rozhodnutie') or '-'}\n"
                f"MINIMUM INPUT (the one next step): {pkg.get('minimálny_vstup') or '-'}\n"
                f"COMMITMENT: {pkg.get('záväzok') or 'none'}\nWHAT WE DO NOT KNOW: {pkg.get('čo_nevieme') or '-'}\n"
                f"BUSINESS TERMS SENTENCE ADDED: {'yes' if policy else 'no'}") if mode else ""
    user = (
        f"MODE: {mode or 'discovery'}\nCHANNEL: {channel}\nVARIANT: {variant}\nADDRESSEE: {addressee}\n"
        f"PERSON ROLE: {(person['role_title'] if person is not None else None) or '-'}\n"
        f"ORGANIZATION TYPE: {org['sector_kind'] if org is not None and org['sector_kind'] else '-'}\n"
        f"WHY THIS RECIPIENT: {link_type}\nPROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment']}\n"
        + (f"\nPROPOSAL:\n<untrusted_document>\n{proposal}\n</untrusted_document>\n" if proposal else "")
        + f"\nFACTS:\n<untrusted_document>\n{facts_text(facts)}\n</untrusted_document>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def model_check(cfg: Config, router: ModelRouter, body: str, facts: list[dict[str, Any]], outreach_ref: int | None,
                package: dict[str, Any] | None = None) -> dict[str, Any]:
    pkg = package or {}
    proposal = (f"NEED STATE: {pkg.get('potreba', {}).get('stav', 'hypothesis')}\nRECIPIENT TYPE: {pkg.get('typ', '-')}\n"
                f"DELIVERABLE: {pkg.get('výstup') or '-'}\nCOMMITMENT: {pkg.get('záväzok') or 'none'}") if pkg else ""
    res = router.run_task(
        "draft_check",
        [
            {"role": "system", "content": DRAFT_CHECK_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {"role": "user", "content": f"FACTS:\n<untrusted_document>\n{facts_text(facts)}\n</untrusted_document>\n\n"
                                        + (f"PROPOSAL:\n<untrusted_document>\n{proposal}\n</untrusted_document>\n\n"
                                           if proposal else "")
                                        + f"DRAFT:\n<untrusted_document>\n{body.split(FOOTER_MARK)[0]}\n</untrusted_document>"},
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


def need_confirmed(db: DB, hid: int, person_id: int | None, org_id: int | None) -> bool:
    """Problém potvrdil tento kontakt alebo jeho organizácia (výsledok oslovenia) – až potom režim pilot."""
    return any(o["problem_relevance"] == "confirmed" and ((person_id and o["person_id"] == person_id) or
                                                          (org_id and o["org_id"] == org_id))
               for o in db.outcomes_for(hid))


def build_package(cfg: Config, db: DB, hid: int, row: dict[str, Any], mode: str, org) -> tuple[dict[str, Any], str | None, str]:
    """Reťazec „prečo tento kontakt → predpokladaná potreba → výstup → minimálny vstup“ pre návrh (z kontaktu,
    návrhu prínosu a prehľadu domény). Vráti (balík, veta o podmienkach alebo None, povolený text pre čísla)."""
    from .value import best_proposal, domain_of, policy_version

    group = row.get("skupina") or "customer"
    proposal = best_proposal(db, hid, group)
    p = json.loads(proposal["proposal_json"]) if proposal is not None else {}
    confirmed = need_confirmed(db, hid, row.get("person_id"), row.get("org_id"))
    commitment = p.get("schválený_záväzok") if proposal is not None and proposal["delivery_commitment_approved"] else None
    brief = db.latest_brief(domain_of(db, hid)[0])
    commercial = org is None or (org["sector_kind"] or "") != "public"
    policy = policy_text(cfg, mode, bool(p.get("deliverable")), commercial)
    package = {
        "režim": mode, "typ": group,
        "prečo_tento_kontakt": {"text": row.get("účel") or row.get("prečo") or "", "dôkaz": row.get("dôkaz_účelu") or
                                row.get("dôvody") or "", "zdroj": row.get("zdroj_kontaktu") or "",
                                "úryvok": row.get("úryvok") or ""},
        "potreba": {"text": p.get("need_statement") or "nevieme – prvým krokom je otázka na skúsenosť",
                    "stav": "confirmed" if confirmed else "hypothesis", "podklad": p.get("evidence_refs") or []},
        "výstup": p.get("deliverable") or "", "rozhodnutie": p.get("decision_supported") or "",
        "minimálny_vstup": p.get("minimum_input") or row.get("čo_žiadať") or "",
        "čo_nevieme": p.get("limitations") or "", "záväzok": commitment,
        "návrh_prínosu": proposal["id"] if proposal is not None else None,
        "stav_návrhu_prínosu": proposal["status"] if proposal is not None else "chýba",
        "prehľad_domény": {"id": brief["id"], "verzia": brief["version"]} if brief is not None else None,
        "politika": policy_version(cfg), "veta_o_podmienkach": policy,
    }
    allowed = " ".join(str(p.get(k) or "") for k in ("need_statement", "deliverable", "decision_supported",
                                                     "minimum_input", "estimate_inputs")) + " " + (commitment or "")
    return package, policy, allowed


def readiness_of(package: dict[str, Any], checks: dict[str, Any], subject: str | None, body: str) -> tuple[str, list[str]]:
    """Pripravenosť na ručné odoslanie: ready, alebo needs_input s dôvodmi (rozpracovaný návrh)."""
    reasons = []
    if not package["prečo_tento_kontakt"]["zdroj"]:
        reasons.append("chýba zdroj dôvodu výberu kontaktu – doplň")
    left = placeholders_in(body) + placeholders_in(subject)
    if left:
        reasons.append("zástupné polia: " + ", ".join(left))
    if checks.get("problems"):
        reasons.append("kontroly: " + "; ".join(checks["problems"])[:200])
    model = checks.get("model") or {}
    if model.get("verdict") == "undecided":
        reasons.append("čaká na hodnotiteľa (kontrola modelom bez výsledku)")
    elif model.get("verdict") == "problems" and model.get("problems"):
        reasons.append("hodnotiteľ našiel problémy")
    if package.get("stav_návrhu_prínosu") == "pending_review":
        reasons.append("návrh prínosu čaká na hodnotiteľa")
    return ("ready" if not reasons else "needs_input"), reasons


def create_drafts_for(cfg: Config, db: DB, router: ModelRouter, hyp, row: dict[str, Any], mode: str,
                      budget: Counter) -> Counter:
    """Návrhy pre jeden riadok „Koho osloviť“ (osoba alebo organizácia cez funkčný kontakt) v danom režime."""
    stats: Counter = Counter()
    pid, org_id = row.get("person_id"), row.get("org_id")
    person = db.get_person(pid) if pid else None
    if pid and person is None:
        return stats
    org = db.get_organization(org_id) if org_id else None
    existing = db.outreach_for_person(pid, hyp["id"]) if pid else db.outreach_for_org(org_id, hyp["id"])
    if (mode == "pilot" and any(e["mode"] == "pilot" for e in existing)) or (mode != "pilot" and existing):
        return stats  # návrhy už existujú (úpravy robí človek v GUI)
    oh = db.org_hypothesis(org_id, hyp["id"]) if org_id else None
    if oh is not None and oh["procurement_active"]:
        # R-OUT-13: počas súťaže iba oficiálna cesta (PTK, vysvetľovanie súťažných podkladov)
        db.insert_outreach(pid, hyp["id"], "email", "-", None,
                           "Návrh sa nevytvoril: organizácia možno práve obstaráva súvisiacu vec. Komunikuj iba "
                           "oficiálnou cestou (prípravné trhové konzultácie, vysvetľovanie súťažných podkladov). "
                           + (oh["procurement_note"] or ""), [], {"ok": False, "problems": ["prebiehajúce obstarávanie"]},
                           "blocked", org_id=org_id, mode=mode)
        stats["blocked_procurement"] += 1
        return stats
    if person is not None:
        channels = [c for c in plan_channels(db, person, org_id) if c[0] in cfg.outreach.channels]
        link = next((lk for lk in db.links_for_person(pid) if lk["hypothesis_id"] == hyp["id"]), None)
        link_type = link["link_type"] if link is not None else "contact"
    else:
        emails = [c for c in db.org_contacts(org_id) if c["kind"] == "email"] if org_id else []
        channels = [("email", emails[0], True)] if emails and "email" in cfg.outreach.channels else []
        link, link_type = None, "organization mailbox found for the hypothesis"
    if not channels:
        stats["no_channel"] += 1
        return stats
    facts = build_facts(cfg, db, router, hyp, person, link, org, channels[0][1])
    if not any(f["kind"] == "evidence" for f in facts):
        stats["no_supported_facts"] += 1
    package, policy, allowed = build_package(cfg, db, hyp["id"], row, mode, org)
    a_ok = link is not None and link["link_type"] in A_LINKS and any(f["kind"] == "evidence" for f in facts)
    variants_for_email = ["A", "B"] if a_ok else ["B"]
    for channel, contact, generic in channels:
        variants = variants_for_email if channel == "email" else [variants_for_email[0]]
        for variant in variants:
            if cfg.outreach.max_drafts_per_run and budget["drafts"] >= cfg.outreach.max_drafts_per_run:
                stats["deferred"] += 1
                return stats
            budget["drafts"] += 1
            res = router.run_task(
                "draft",
                draft_messages(cfg, hyp, person, org, link_type, variant, channel, facts, generic, mode, package, policy),
                DraftOut,
                object_type="person" if pid else "organization",
                object_id=pid or org_id,
                max_tokens=900,
            )
            if not res.ok:
                stats["llm_failures"] += 1
                continue
            out = res.value
            source_url = contact["source_url"] if contact is not None else (person["source_url"] if person else "")
            subject, body = compose(cfg, channel, person, org["sector_kind"] if org is not None else None, out.body,
                                    out.subject, source_url, contact["value"] if contact is not None else None,
                                    mode, policy)
            checks = check_draft(cfg, channel, subject, body, out.body, facts, source_url, mode, policy, allowed,
                                 package["potreba"]["stav"])
            checks.update(core=out.body.strip(), subject_core=out.subject.strip(), facts_used=out.facts_used,
                          generic=generic, contact_kind=contact["kind"] if contact is not None else None)
            status = "draft"
            if checks["ok"]:
                mc = model_check(cfg, router, body, facts, None, package)
                checks["model"] = mc
                if mc["verdict"] == "problems" and mc["problems"]:
                    status = "needs_edit"
                elif mc["verdict"] == "undecided":
                    checks["model_note"] = "Kontrola modelom bez výsledku; prečítaj návrh pozorne."
            else:
                status = "needs_edit"
            readiness, reasons = readiness_of(package, checks, subject, body)
            db.insert_outreach(pid, hyp["id"], channel, variant, subject, body, facts, checks, status,
                               contact["id"] if contact is not None else None, org_id=org_id, mode=mode,
                               proposal_id=package["návrh_prínosu"],
                               package={**package, "pripravenosť": reasons}, readiness=readiness)
            stats[status] += 1
            stats[f"mode_{mode}"] += 1
    return stats


def _compose_row(cfg: Config, db: DB, row, core: str, subject_core: str) -> tuple[str | None, str, str, dict[str, Any]]:
    """Pevné časti návrhu podľa jeho režimu (starší návrh bez balíka = pôvodné texty). Vráti (predmet, text, zdroj, balík)."""
    pkg = json.loads(row["package_json"]) if row["package_json"] else {}
    mode = row["mode"] if pkg else None
    person = db.get_person(row["person_id"]) if row["person_id"] else None
    org_id = row["org_id"] or (person["org_id"] if person is not None else None)
    org = db.get_organization(org_id) if org_id else None
    contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
    source_url = contact["source_url"] if contact is not None else (person["source_url"] if person is not None else "")
    subject, body = compose(cfg, row["channel"], person, org["sector_kind"] if org is not None else None, core,
                            subject_core, source_url, contact["value"] if contact is not None else None, mode,
                            pkg.get("veta_o_podmienkach"))
    return subject, body, source_url, pkg


def _allowed(pkg: dict[str, Any]) -> str:
    return " ".join(str(pkg.get(k) or "") for k in ("výstup", "rozhodnutie", "minimálny_vstup", "záväzok")) + " " + str(
        (pkg.get("potreba") or {}).get("text") or "")


def refresh_draft(cfg: Config, db: DB, llm: Any, outreach_id: int) -> str:
    """Znova zloží neupravený návrh z uloženého jadra (napr. po zmene kontrol alebo profilu odosielateľa)
    a prepočíta kontroly; model sa volá iba na kontrolu faktov. Ručne upravené návrhy sa nemenia."""
    router = as_router(llm, cfg, db)
    row = db.get_outreach(outreach_id)
    checks = json.loads(row["checks_json"] or "{}")
    if row["status"] not in ("draft", "needs_edit") or checks.get("edited") or not checks.get("core"):
        return row["status"]
    facts = json.loads(row["facts_json"] or "[]")
    subject, body, source_url, pkg = _compose_row(cfg, db, row, checks["core"], checks.get("subject_core") or "")
    mode = row["mode"] if pkg else None
    new = check_draft(cfg, row["channel"], subject, body, checks["core"], facts, source_url, mode,
                      pkg.get("veta_o_podmienkach"), _allowed(pkg), (pkg.get("potreba") or {}).get("stav"))
    new.update({k: checks.get(k) for k in ("core", "subject_core", "facts_used", "generic", "contact_kind")})
    status = "needs_edit"
    if new["ok"]:
        mc = checks.get("model") or model_check(cfg, router, body, facts, outreach_id, pkg or None)
        new["model"] = mc
        status = "needs_edit" if mc["verdict"] == "problems" and mc["problems"] else "draft"
    fields: dict[str, Any] = {"subject": subject, "body": body, "checks_json": new, "status": status}
    if pkg:
        readiness, reasons = readiness_of(pkg, new, subject, body)
        fields.update(readiness=readiness, package_json={**pkg, "pripravenosť": reasons})
    db.update_outreach(outreach_id, **fields)
    return status


def run_drafts(cfg: Config, db: DB, llm: Any, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    """Návrhy pre frontu prvého oslovenia (zákazník, držiteľ dát, expert aj organizácia cez funkčný kontakt) a pilot
    pre kontakty, ktoré potvrdili problém; najprv najvyššia priorita naprieč hypotézami."""
    from .shortlist import contact_shortlist

    require_lia(cfg)
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    budget: Counter = Counter()
    ids = hypothesis_ids or [h["id"] for h in db.people_enabled_hypotheses()]
    queue = []
    for hid in ids:
        for r in contact_shortlist(cfg, db, hid):
            if r["prvé_oslovenie"]:
                mode = MODE_OF_GROUP.get(r["skupina"], "discovery")
            elif (r["stav_komunikácie"] == "nadviazanie" and r["skupina"] == "customer"
                  and need_confirmed(db, hid, r["person_id"], r["org_id"])):
                mode = "pilot"
            elif r["odporúčanie"] == "neoslovovať" and "prebiehajúce obstarávanie" in r["dôvody"]:
                mode = MODE_OF_GROUP.get(r["skupina"], "discovery")  # iba upozornenie (oficiálna cesta)
            else:
                continue
            queue.append((r["priorita"] or 0.0, hid, r, mode))
    queue.sort(key=lambda x: -x[0])
    for _, hid, row, mode in queue:
        if deadline.expired():
            break
        stats.update(create_drafts_for(cfg, db, router, db.get_hypothesis(hid), row, mode, budget))
        if cfg.outreach.max_drafts_per_run and budget["drafts"] >= cfg.outreach.max_drafts_per_run:
            break
    return dict(stats)


# ======================================================================= akcie človeka (GUI)
def recheck(cfg: Config, db: DB, outreach_id: int, body: str, subject: str | None, router: ModelRouter | None = None) -> dict:
    """Po ručnej úprave: znova kontroly kódom (a voliteľne modelom); stav draft alebo needs_edit."""
    row = db.get_outreach(outreach_id)
    facts = json.loads(row["facts_json"] or "[]")
    old = json.loads(row["checks_json"] or "{}")
    pkg = json.loads(row["package_json"]) if row["package_json"] else {}
    mode = row["mode"] if pkg else None
    person = db.get_person(row["person_id"]) if row["person_id"] else None
    contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
    source_url = contact["source_url"] if contact is not None else (person["source_url"] if person is not None else "")
    core = body.split(FOOTER_MARK)[0]
    checks = check_draft(cfg, row["channel"], subject, body, core, facts, source_url, mode,
                         pkg.get("veta_o_podmienkach"), _allowed(pkg), (pkg.get("potreba") or {}).get("stav"))
    checks.update({k: old.get(k) for k in ("facts_used", "generic", "contact_kind")}, core=old.get("core"), edited=True)
    if router is not None and checks["ok"]:
        checks["model"] = model_check(cfg, router, body, facts, outreach_id, pkg or None)
        if checks["model"]["verdict"] == "problems" and checks["model"]["problems"]:
            checks["ok"] = False
    fields: dict[str, Any] = {"body": body, "subject": subject, "checks_json": checks,
                              "status": "draft" if checks["ok"] else "needs_edit"}
    if pkg:
        readiness, reasons = readiness_of(pkg, checks, subject, body)
        fields.update(readiness=readiness, package_json={**pkg, "pripravenosť": reasons},
                      message_version=(row["message_version"] or 1) + 1)
    db.update_outreach(outreach_id, **fields)
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
    checks = json.loads(row["checks_json"] or "{}")
    body, subject = row["body"], row["subject"]
    if checks.get("core") and not checks.get("edited"):  # pevné časti s aktuálnym profilom odosielateľa
        subject, body, _, _ = _compose_row(cfg, db, row, checks["core"], checks.get("subject_core") or (row["subject"] or ""))
    left = placeholders_in(body) + placeholders_in(subject)
    if left:
        raise OutreachError("V návrhu ostali placeholdery: " + ", ".join(left))
    db.update_outreach(outreach_id, status="approved", approved_at=now.isoformat(timespec="seconds"), body=body,
                       subject=subject, **({"readiness": "ready"} if row["package_json"] else {}))
    if row["person_id"]:
        db.update_person(row["person_id"], status="approved")


def mark_sent(cfg: Config, db: DB, outreach_id: int, when: date | None = None) -> date:
    """Človek označí ručne odoslanú správu; follow-up pripadne na 5. pracovný deň (R-OUT-9)."""
    when = when or date.today()
    row = db.get_outreach(outreach_id)
    if row["status"] not in ("approved", "sent"):
        raise OutreachError("Odoslané môže byť iba schválené oslovenie.")
    due = add_business_days(when, cfg.outreach.follow_up_business_days[0], cfg.outreach.holidays)
    follow = due.isoformat() if row["follow_up_count"] < cfg.outreach.max_follow_ups else None
    db.update_outreach(outreach_id, status="sent", sent_at=when.isoformat(), follow_up_due=follow)
    if row["person_id"]:
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
    if row["person_id"]:
        db.update_person(row["person_id"], last_interaction_at=utcnow())


PERSON_STATUS_FOR = {"replied": "replied", "scheduled": "replied", "interviewed": "interviewed", "declined": "declined",
                     "no_show": "replied", "no_reply": "contacted", "bounced": "contacted"}


def set_status(cfg: Config, db: DB, outreach_id: int, status: str) -> dict:
    """Zmena stavu po ručnej akcii. Námietka spustí forget a potlačenie; odmietnutie vymaže údaje po 30 dňoch."""
    row = db.get_outreach(outreach_id)
    if row is None:
        raise OutreachError("Oslovenie neexistuje.")
    if status == "objected":  # námietka: výmaz a potlačenie (osoba, inak funkčný e-mail organizácie)
        if row["person_id"]:
            return forget(cfg, db, person_id=row["person_id"], reason="objection")
        contact = db.get_contact(row["contact_point_id"]) if row["contact_point_id"] else None
        if contact is not None and contact["kind"] == "email":
            return forget(cfg, db, email=contact["value"], reason="objection")
    db.update_outreach(outreach_id, status=status)
    person_status = PERSON_STATUS_FOR.get(status)
    if person_status and row["person_id"]:
        fields: dict[str, Any] = {"status": person_status, "last_interaction_at": utcnow()}
        if status == "declined":
            fields["purge_after"] = (datetime.now(timezone.utc) + timedelta(days=cfg.privacy.retention_days_declined)).isoformat(timespec="seconds")
        if status == "bounced":
            fields.pop("status")
        db.update_person(row["person_id"], **fields)
    return {"status": status}
