"""Ľudia a kontakty k hypotézam vybraným na Bránu 0 (R-PPL).

Dva kroky (kvôli poradiu modelov v nočnom behu):

1. collect (rýchly model + kód): citované osoby z dôkazov, štatutári z RPO (meno a funkcia),
   kontaktné stránky overených webov organizácií. Model iba navrhne trojice meno–funkcia–kontakt,
   kód overí, že meno, funkcia aj kontakt sú v tom istom bloku HTML, kontakt je do 400 znakov
   a najbližšie práve k tomuto menu a že na stránke naozaj je. K organizácii sa ukladajú iba
   generické kontakty (info@, podateľňa, ústredňa), najviac 2 z každého druhu.
2. rank (presný model + kód): mapa rolí hypotézy (k=3, zhoda väčšiny), zhoda funkcie s mapou,
   väzby osoba–problém a priorita so zložkami. Vyberie najviac 15 osôb na hypotézu a 2 na
   organizáciu (experti zvlášť, najviac 3); ostatní kandidáti sa hneď vymažú (minimalizácia).

LinkedIn: URL profilu sa uloží z odkazu na webe organizácie (v bloku osoby), z výsledku
vyhľadávania (celé meno v titulku a názov organizácie) alebo ručne z GUI; stránka LinkedIn sa
nikdy nesťahuje a záznam má označenie „over ručne“.

Nič sa neodosiela. Pred uložením osoby alebo kontaktu prebehne kontrola zoznamu potlačenia.
Beží iba po potvrdení LIA (privacy.lia_completed).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from openai import APIConnectionError
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from .config import Config
from .contacts import (
    Block, PageContacts, contact_page_links, extract_contacts, is_generic_email, is_org_email, is_person_profile,
    normalize_profile_url, sitemap_contact_links, snippet_around, value_in_snippet,
)
from .db import DB
from .models import ModelRouter, as_router
from .names import fold, gender_hint, looks_like_person_name, name_in_text, org_name_key, person_name_key, split_titles, title_key
from .orgs import OrgFinder, least_recently_searched, name_in, select_people_hypotheses
from .privacy import Suppression, require_lia
from .prompts import CONTACT_ASSOC_SYSTEM, PERSON_MENTIONS_SYSTEM, ROLE_FIT_SYSTEM, ROLE_MAP_SYSTEM
from .registers import RPO_ATTRIBUTION, RPOClient, RUZClient
from .util import Deadline, domain_of
from .verify import quote_window

log = logging.getLogger(__name__)

ROLE_FIT_CALLS_PER_HYPOTHESIS = 15  # posúdenia funkcie presným modelom na hypotézu a beh (~20 s)
WINDOW = 400  # meno a kontakt musia byť v tom istom bloku alebo v okne ≤ 400 znakov (R-PPL-10)
# Verzia pravidiel zberu ľudí z webu (M42). Organizácia zozbieraná staršou verziou sa pri ďalšom behu zozbiera znova:
# výber s menším limitom na organizáciu ľudí natrvalo vymazal (minimalizácia) a stav „people_done“ zber zastavil.
# Pri zmene pravidiel zberu alebo výberu zvýš.
PEOPLE_COLLECT_VERSION = 2
LINK_STRENGTH = {"quoted_about_problem": 1.0, "role_owner": 0.7, "data_holder": 0.6, "process_user": 0.55, "org_affected": 0.5,
                 "procurement_contact": 0.4, "expert": 0.3}
COLLECTED_LINKS = ("quoted_about_problem", "procurement_contact")
# Funkcie ľudí, ktorí v organizácii, ktorá drží dáta, vedia poslať vzorku alebo schému: začiatky slov bez diakritiky
# (dátový, databáza, údaje, štatistik, analytik, informatik, informačný, otvorené dáta, metodik, register, evidencia,
# hovorca – vybavuje žiadosti podľa 211/2000). Celé slovo, nie časť („podateľňa“ nie je „dát…“).
DATA_ROLE_PREFIXES = ("dat", "udaj", "statist", "analyt", "informatik", "informacn", "otvoren", "opendata", "cdo",
                      "metodik", "registr", "evidenc", "hovorc")
CHANNEL_VALUE = {"email": 1.0, "phone": 0.7, "hidden_contact": 0.6, "generic_email": 0.4, "profile_url": 0.3}
GENERAL_MANAGEMENT = ("konatel", "riaditel", "generalny riaditel", "ceo", "majitel", "starosta", "primator", "prednosta",
                      "predseda predstavenstva", "statutar", "rektor", "managing director", "general manager",
                      "konatelka", "riaditelka", "starostka", "primatorka", "prednostka", "majitelka", "predsednicka")
# Poradca a konzultant nie sú automaticky experti: v niektorých segmentoch (účtovníctvo, dane) sú to zákazníci.
EXPERT_ROLES = ("vyskumn", "analytik", "docent", "profesor", "expert", "lektor")
EXPERT_ORG_WORDS = ("zdruzenie", "zvaz", "komora", "asociacia", "univerzita", "akademia", "institut", "think", "nadacia")


class PersonProposal(BaseModel):
    block: int = 0
    full_name: str
    titles: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""


class ContactAssoc(BaseModel):
    persons: list[PersonProposal] = Field(default_factory=list)


class PersonMention(BaseModel):
    full_name: str
    titles: str = ""
    role: str = ""
    organization: str = ""
    about_problem: bool = False


class PersonMentions(BaseModel):
    persons: list[PersonMention] = Field(default_factory=list)


class RoleCategory(BaseModel):
    category: str
    examples: list[str] = Field(default_factory=list)
    rationale: str = ""


class RoleMap(BaseModel):
    roles: list[RoleCategory] = Field(default_factory=list)


class RoleFit(BaseModel):
    fit: Literal["exact", "related", "general", "none"]
    category: str = ""
    rationale: str = ""


FIT_SCORE = {"exact": 1.0, "related": 0.5, "general": 0.3, "none": 0.0}


@dataclass
class VerifiedPerson:
    full_name: str
    titles: str
    role: str
    emails: list[str]
    phones: list[str]
    hidden: bool
    snippet: str
    block_text: str
    profiles: list[str] = field(default_factory=list)


# ======================================================================= overenie kódom
def verify_proposals(page: PageContacts, proposals: list[PersonProposal]) -> tuple[list[VerifiedPerson], dict[str, int]]:
    """Kód overí návrhy modelu (R-PPL-10): meno doslova v bloku, funkcia doslova v bloku, kontakt
    medzi kontaktmi bloku, v okne ≤ 400 znakov od mena a zároveň najbližšie práve k tomuto menu
    (blok so zoznamom viacerých ľudí). Kontakt navrhnutý viacerým osobám sa nepriradí nikomu.
    Nič, čo na stránke nie je, sa neuloží (R-PPL-6)."""
    stats: Counter = Counter()
    by_block: dict[int, list[PersonProposal]] = defaultdict(list)
    for p in proposals:
        name, _ = split_titles(p.full_name)
        block = p.block if 0 <= p.block < len(page.blocks) else -1
        if block < 0 or name_in_text(name, page.blocks[block].text) < 0:
            # Model občas uvedie zlé číslo bloku: meno platí iba v jedinom bloku, kde doslova je.
            found = [b.index for b in page.blocks if name_in_text(name, b.text) >= 0]
            if len(found) == 1:
                block = found[0]
                stats["block_corrected"] += 1
        if block >= 0:
            by_block[block].append(p)
        else:
            stats["bad_block"] += 1
    out: list[VerifiedPerson] = []
    for idx, props in by_block.items():
        block: Block = page.blocks[idx]
        text = block.text
        valid = []
        seen: set[str] = set()
        for p in props:
            name, titles = split_titles(p.full_name)
            pos = name_in_text(name, text)
            if pos < 0 or not looks_like_person_name(name):
                stats["name_not_in_block"] += 1
                continue
            if person_name_key(name) in seen:
                continue
            seen.add(person_name_key(name))
            valid.append((p, name, titles or p.titles, pos))
        name_positions = [pos for *_, pos in valid]
        claimed: Counter = Counter()
        for p, *_ in valid:
            for v in {_email_of(p.email), _phone_from(p.phone, block.phones)}:
                if v:
                    claimed[v] += 1

        def owns(contact_pos: int, pos: int) -> bool:
            if contact_pos < 0:
                return False
            if len(valid) == 1:
                return len(text) <= WINDOW or abs(contact_pos - pos) <= WINDOW
            # Kontakt patrí poslednému menu pred ním (zoznam „meno, funkcia, e-mail, telefón“);
            # ak žiadne meno pred ním nie je, najbližšiemu menu za ním.
            preceding = [q for q in name_positions if q <= contact_pos]
            owner = max(preceding) if preceding else min(name_positions, key=lambda q: abs(contact_pos - q))
            return owner == pos and abs(contact_pos - pos) <= WINDOW

        for p, name, titles, pos in valid:
            role = p.role.strip()
            if role and fold(role) not in fold(text):
                role = ""  # funkcia, ktorá v bloku nie je, sa neuloží
                stats["role_not_in_block"] += 1
            emails, phones = [], []
            email = _email_of(p.email)
            if email:
                if email in block.emails and claimed[email] == 1 and owns(text.lower().find(email), pos):
                    emails.append(email)
                else:
                    stats["email_rejected"] += 1  # nie je na stránke, patrí viacerým, alebo je bližšie inému menu
            phone = _phone_from(p.phone, block.phones)
            if phone and claimed[phone] == 1 and owns(_phone_pos(text, phone), pos):
                phones.append(phone)
            elif p.phone:
                stats["phone_rejected"] += 1
            # LinkedIn z odkazu v bloku (model ho nenavrhuje): patrí tomu istému menu ako e-mail
            # (posledné meno pred odkazom, ≤ 400 znakov); vo viacmennom bloku musí slug obsahovať priezvisko.
            profiles = [u for u in block.profiles if is_person_profile(u) and owns(text.find(u), pos)
                        and (len(valid) == 1 or _slug_has_surname(u, name))]
            if not role and not emails and not phones and not profiles:
                stats["no_role_no_contact"] += 1  # samotné meno bez funkcie a kontaktu sa neukladá
                continue
            positions = [pos, pos + len(name)] + [text.lower().find(e) for e in emails] + [_phone_pos(text, ph) for ph in phones]
            positions = [x for x in positions if x >= 0]
            start, end = max(0, min(positions) - 30), min(len(text), max(positions) + 60)
            snippet = text[start:end] if end - start <= 300 else text[pos: pos + 300]
            out.append(VerifiedPerson(name, titles, role, emails, phones, block.hidden and len(valid) == 1, snippet, text,
                                      profiles))
            stats["verified_people"] += 1
    return out, dict(stats)


def _slug_has_surname(url: str, name: str) -> bool:
    parts = [fold(x) for x in name.split() if len(x) >= 3]
    slug = fold(url.rsplit("/", 1)[-1])
    return bool(parts) and parts[-1] in slug


def _near(text: str, contact_pos: int, name_pos: int) -> bool:
    """Kontakt v okne ≤ 400 znakov od mena (krátky blok celý)."""
    if len(text) <= WINDOW:
        return True
    return contact_pos >= 0 and abs(contact_pos - name_pos) <= WINDOW


def _email_of(value: str) -> str | None:
    email = (value or "").strip().lower().replace(" ", "")
    return email or None


def _phone_pos(text: str, phone: str) -> int:
    from .contacts import phones_in

    for value, s, _ in phones_in(text):
        if value == phone:
            return s
    return -1


def _phone_from(value: str, block_phones: list[str]) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    for p in block_phones:
        if digits and (p.endswith(digits[-9:]) or digits.endswith(p[-9:])):
            return p
    return None


# ======================================================================= zber (krok 1)
class PeopleCollector:
    def __init__(self, cfg: Config, db: DB, router: ModelRouter, searx, fetcher, deadline: Deadline,
                 rpo: RPOClient | None = None, ruz: RUZClient | None = None):
        self.cfg, self.db, self.router, self.searx, self.fetcher, self.deadline = cfg, db, router, searx, fetcher, deadline
        self.rpo = rpo
        self.orgs = OrgFinder(cfg, db, router, searx, fetcher, rpo, ruz, deadline)
        self.sup = Suppression(cfg, db)
        self.stats: Counter = Counter()

    # ----------------------------------------------------------- uloženie
    def save_person(self, hid: int, org_id: int | None, full_name: str, titles: str, role: str, source_url: str,
                    snippet: str, source_kind: str, context: str = "") -> int | None:
        name, extra_titles = split_titles(full_name)
        titles = " ".join(x for x in (titles, extra_titles) if x).strip()
        key = person_name_key(name)
        org_key = org_name_key(self.db.get_organization(org_id)["name"]) if org_id else ""
        if not key or self.sup.is_suppressed(name=name, org_key=org_key):
            self.stats["suppressed"] += 1 if key else 0
            return None
        existing = self.db.find_person(org_id, key)
        hint = gender_hint(name, role, context or snippet)
        if existing is not None:
            updates = {}
            if role and not existing["role_title"]:
                updates["role_title"] = role
            if hint and not existing["gender_hint"]:
                updates["gender_hint"] = hint
            if updates:
                self.db.update_person(existing["id"], **updates)
            return int(existing["id"])
        pid = self.db.insert_person(
            org_id, name, key, source_url, snippet, hid, titles=titles or None, role_title=role or None,
            gender_hint=hint, source_kind=source_kind,
        )
        self.stats["people_saved"] += 1
        return pid

    def save_contact(self, person_id: int | None, org_id: int | None, kind: str, value: str, source_url: str,
                     snippet: str, extraction: str, generic: bool = False, extra_checks: dict[str, Any] | None = None) -> None:
        if kind == "email" and self.sup.is_suppressed(email=value):
            self.stats["suppressed"] += 1
            return
        if kind == "phone" and self.sup.is_suppressed(phone=value):
            self.stats["suppressed"] += 1
            return
        if kind in ("email", "phone", "profile_url") and not value_in_snippet(kind, value, snippet):
            self.stats["contact_not_in_snippet"] += 1  # R-PPL-6: hodnota musí byť doslova v úryvku
            return
        checks = {"v_úryvku": True, "over_ručne": kind in ("hidden_contact", "profile_url")}
        if kind == "profile_url":
            checks["stiahnuté"] = False  # LinkedIn sa nesťahuje
        checks.update(extra_checks or {})
        if self.db.add_contact_point(person_id, org_id, kind, value, source_url, snippet, extraction, generic, checks):
            self.stats[f"contacts_{kind}"] += 1

    # -------------------------------------------------- citované osoby
    def quoted_people(self, hyp) -> None:
        evidence = [e for e in self.db.evidence_for(hyp["id"]) if e["status"] == "verified"][:25]
        for claim in evidence:
            if self.deadline.expired():
                return
            doc = self.db.get_document(claim["document_id"])
            window = quote_window(claim["quote"], doc["text"], radius=600) if doc else claim["quote"]
            cached = self.db.cached_output("person_mentions", "claim", claim["id"])
            if cached is not None:  # tvrdenie už model prečítal: osoby a väzby iba z kódu (overenie mena v okne ostáva)
                mentions = PersonMentions.model_validate_json(cached)
                self.stats["person_mentions_cached"] += 1
            else:
                res = self.router.run_task(
                    "person_mentions",
                    [
                        {"role": "system", "content": PERSON_MENTIONS_SYSTEM},
                        {"role": "user", "content": f"PROBLEM: {hyp['title']}\nQUOTE: {claim['quote']}\n\n"
                                                    f"<untrusted_document>\n{window}\n</untrusted_document>"},
                    ],
                    PersonMentions,
                    object_type="claim",
                    object_id=claim["id"],
                    max_tokens=600,
                )
                if not res.ok:
                    self.stats["llm_failures"] += 1
                    continue
                mentions = res.value
            for m in mentions.persons[:4]:
                if not m.about_problem:
                    continue
                name, _ = split_titles(m.full_name)
                pos = name_in_text(name, window)
                if pos < 0 or not looks_like_person_name(name):
                    self.stats["mentions_not_in_source"] += 1
                    continue
                org_id = None
                if m.organization and name_in(m.organization, window):
                    info = self.orgs.resolve_name(m.organization)
                    org_id = self.orgs.store(m.organization, "evidence", info, source_url=claim["url"])
                    if org_id is not None:
                        self.db.link_org_hypothesis(org_id, hyp["id"], "quoted_person", claim["id"], claim["url"],
                                                    fit={"dôkaz o probléme": f"citovaná osoba (C{claim['id']})"})
                role = m.role if m.role and fold(m.role) in fold(window) else ""
                snippet = snippet_around(window, max(0, pos - 100), pos + len(name) + 150)
                pid = self.save_person(hyp["id"], org_id, name, m.titles, role, claim["url"], snippet, "evidence", window)
                if pid is not None:
                    self.db.upsert_link(pid, hyp["id"], "quoted_about_problem", claim["id"], claim["url"],
                                        rationale=f"Verejne sa vyjadril(a) k problému (C{claim['id']}).")
                    self.stats["quoted_people"] += 1

    # ---------------------------------------------------------- RPO
    def statutory_people(self, hyp, org) -> None:
        if self.rpo is None or not org["ico"]:
            return
        info = self.rpo.lookup_ico(org["ico"])
        if info is None:
            return
        url = self.rpo.entity_url(info.rpo_id) if info.rpo_id else "https://api.statistics.sk/rpo/v1"
        for person in info.statutory[:3]:
            snippet = f"RPO – štatutárny orgán {org['name']}: {person.full_name}, {person.function}. {RPO_ATTRIBUTION}"
            self.save_person(hyp["id"], org["id"], person.full_name, person.titles, person.function, url, snippet, "rpo")
            self.stats["statutory_people"] += 1

    # ------------------------------------------------- kontaktné stránky
    def website_people(self, hyp, org) -> None:
        """Úvodná stránka + najviac 4 kontaktné stránky (odkazy a sitemap.xml s kľúčovými slovami, R-PPL-1).
        HTML sa spracuje v pamäti a neukladá sa; v DB ostanú iba metadáta stránky (R-PPL-2)."""
        home = org["website"]
        limit = self.cfg.people.max_contact_pages_per_org
        page = self.fetcher.fetch_html(home)
        self.db.add_contact_page(org["id"], home, page.content_hash or None, page.status)
        if page.status != "ok":
            return
        home_contacts = extract_contacts(page.html, home)
        links = contact_page_links(home_contacts, home, limit=limit)
        # Stránka, cez ktorú sa organizácia našla (hľadanie podľa hypotézy, zdroj dát), ak je na jej webe: často je to
        # práve stránka tímu alebo odboru s ľuďmi, ktorých treba.
        hint = org["evidence_url"] if "evidence_url" in org.keys() else None
        if hint and domain_of(hint) == domain_of(home) and hint.rstrip("/") != home.rstrip("/") and hint not in links:
            links.insert(0, hint)
        if len(links) < 2:
            sm = self.fetcher.fetch_html(home.rstrip("/") + "/sitemap.xml")
            if sm.status == "ok":
                links += [u for u in sitemap_contact_links(sm.html, home, limit=limit) if u not in links]
        self.process_page(hyp, org, home_contacts)
        for url in links[: limit - 1]:
            if self.deadline.expired():
                return
            sub = self.fetcher.fetch_html(url)
            self.db.add_contact_page(org["id"], url, sub.content_hash or None, sub.status)
            if sub.status == "ok":
                self.process_page(hyp, org, extract_contacts(sub.html, url))

    def process_page(self, hyp, org, page: PageContacts) -> None:
        blocks = [b for b in page.blocks if re.search(r"[A-ZÁČĎÉÍĽĹŇÓÔŠŤÚÝŽ][a-záäčďéíĺľňóôŕšťúýž]+\s+[A-ZÁČĎÉÍĽĹŇÓÔŠŤÚÝŽ]", b.text)]
        assigned: set[str] = set()
        verified_all: list[VerifiedPerson] = []
        for start in range(0, len(blocks), 10):
            if self.deadline.expired():
                break
            batch = blocks[start : start + 10]
            listing = "\n\n".join(
                f"[BLOCK {b.index}]\n{b.text}\nDETECTED E-MAILS: {', '.join(b.emails) or '-'}\nDETECTED PHONES: {', '.join(b.phones) or '-'}"
                for b in batch
            )
            res = self.router.run_task(
                "contact_assoc",
                [
                    {"role": "system", "content": CONTACT_ASSOC_SYSTEM},
                    {"role": "user", "content": f"<untrusted_document>\n{listing}\n</untrusted_document>"},
                ],
                ContactAssoc,
                object_type="organization",
                object_id=org["id"],
                max_tokens=1200,
            )
            if not res.ok:
                self.stats["llm_failures"] += 1
                continue
            verified, vstats = verify_proposals(page, res.value.persons)
            self.stats.update(vstats)
            verified_all += verified
        for vp in verified_all:
            pid = self.save_person(hyp["id"], org["id"], vp.full_name, vp.titles, vp.role, page.url, vp.snippet, "website",
                                   vp.block_text)
            if pid is None:
                continue
            for email in vp.emails:
                self.save_contact(pid, org["id"], "email", email, page.url, _snip_for(vp.block_text, email), "mailto_or_text")
                assigned.add(email)
            for phone in vp.phones:
                self.save_contact(pid, org["id"], "phone", phone, page.url, _snip_for_phone(vp.block_text, phone), "phonenumbers")
                assigned.add(phone)
            if vp.hidden:
                self.save_contact(pid, org["id"], "hidden_contact", page.url, page.url, vp.snippet, "obfuscated")
            for url in vp.profiles:
                self.save_contact(pid, org["id"], "profile_url", url, page.url, _snip_for_value(vp.block_text, url), "page_link")
                assigned.add(url)
        # K organizácii jej vlastné kontakty: generické (info@, podateľňa, ústredňa…) a adresa s názvom firmy alebo
        # domény (firma@firma.sk, M42), najviac people.max_org_contacts_per_kind z každého druhu. Nepriradené osobné
        # kontakty zamestnancov ani generické adresy iných organizácií (partneri na tej istej stránke) sa neukladajú
        # (minimalizácia, R-PRIV-2).
        limit = self.cfg.people.max_org_contacts_per_kind
        existing = Counter(c["kind"] for c in self.db.org_contacts(org["id"]))
        for email, snip in page.emails.items():
            if email in assigned or not is_org_email(email, org["name"], org["website"] or page.url):
                continue
            if existing["email"] < limit:
                self.save_contact(None, org["id"], "email", email, page.url, snip, "mailto_or_text", generic=True)
                existing["email"] += 1
        phones = [(ph, snip) for ph, snip in page.phones.items() if ph not in assigned]
        for phone, snip in phones:
            if (phone in page.generic_phones or len(page.phones) <= 2) and existing["phone"] < limit:
                self.save_contact(None, org["id"], "phone", phone, page.url, snip, "phonenumbers", generic=True)
                existing["phone"] += 1
        if page.has_form and not existing["contact_form"]:
            # Formulár iba ako odkaz (posledná možnosť; vypĺňa ho človek ručne a pošta ho nespáruje).
            self.save_contact(None, org["id"], "contact_form", page.url, page.url, (page.title or "kontaktný formulár")[:120],
                              "page_form", generic=True, extra_checks={"over_ručne": True})
            existing["contact_form"] += 1
        if page.hidden_snippets and not any(vp.hidden for vp in verified_all) and not existing["hidden_contact"]:
            self.save_contact(None, org["id"], "hidden_contact", page.url, page.url, page.hidden_snippets[0], "obfuscated", generic=True)
        # Stránka organizácie na LinkedIn (/company/) ako jej kontakt; profily osôb iba cez blok osoby.
        if not existing["profile_url"]:
            for url, snip in page.profiles.items():
                if not is_person_profile(url):
                    self.save_contact(None, org["id"], "profile_url", url, page.url, snip, "page_link", generic=True)
                    break

    # ----------------------------------------------------------- beh
    def run_hypothesis(self, hyp) -> None:
        try:
            self.quoted_people(hyp)
        except Exception:  # noqa: BLE001 – zber citovaných osôb nesmie zastaviť zber z webov
            log.exception("Citované osoby pre H%s zlyhali", hyp["id"])
            self.stats["errors"] += 1
        processed = 0
        for org in self.db.orgs_for_hypothesis(hyp["id"]):
            if self.deadline.expired() or processed >= self.cfg.people.max_orgs_per_hypothesis:
                break
            if org["oh_status"] == "people_done" and (org["oh_people_version"] or 0) >= PEOPLE_COLLECT_VERSION:
                continue
            if org["oh_status"] == "people_done":
                self.stats["orgs_recollected"] += 1  # staršie pravidlá zberu (PEOPLE_COLLECT_VERSION)
            try:
                if org["relation"] != "data_holder":  # pri úrade, ktorý drží dáta, by to bol minister alebo prednosta
                    self.statutory_people(hyp, org)  # bez overeného webu iba meno a funkcia z RPO
                if org["website_verified"] and org["website"]:
                    self.website_people(hyp, org)
                    processed += 1
                    self.stats["orgs_processed"] += 1
            except Exception as exc:  # noqa: BLE001 – jedna organizácia nezastaví ostatné
                log.warning("Organizácia %s preskočená: %s", org["name"], exc)
                self.stats["errors"] += 1
            self.db.set_org_hypothesis(org["id"], hyp["id"], status="people_done", people_version=PEOPLE_COLLECT_VERSION)


def _snip_for_value(text: str, value: str) -> str:
    pos = text.find(value)
    return snippet_around(text, pos, pos + len(value), size=max(300, len(value) + 60)) if pos >= 0 else text[:300]


def _snip_for(text: str, email: str) -> str:
    pos = text.lower().find(email)
    return snippet_around(text, max(pos, 0), max(pos, 0) + len(email)) if pos >= 0 else text[:300]


def _snip_for_phone(text: str, phone: str) -> str:
    from .contacts import phones_in

    for value, s, e in phones_in(text):
        if value == phone:
            return snippet_around(text, s, e)
    return text[:300]


def run_people_collect(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline,
                       hypothesis_ids: list[int] | None = None, rpo: RPOClient | None = None,
                       ruz: RUZClient | None = None) -> dict:
    require_lia(cfg)
    router = as_router(llm, cfg, db)
    if rpo is None:
        rpo = RPOClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    ids = least_recently_searched(db, hypothesis_ids or select_people_hypotheses(cfg, db))
    collector = PeopleCollector(cfg, db, router, searx, fetcher, deadline, rpo, ruz)
    for hid in ids:
        if deadline.expired():
            break
        hyp = db.get_hypothesis(hid)
        if hyp is not None:
            collector.run_hypothesis(hyp)
            collector.stats["hypotheses"] += 1
    return dict(collector.stats)


# ======================================================================= poradie (krok 2)
def role_map_for(cfg: Config, db: DB, router: ModelRouter, hyp) -> list[dict[str, Any]]:
    """Mapa rolí hypotézy: k vzoriek presného modelu, kategórie zo zhody väčšiny (R-PPL-14)."""
    existing = db.get_role_map(hyp["id"])
    if existing is not None:
        return existing["roles"]
    evidence = db.evidence_for(hyp["id"])[:6]
    ev = "\n".join(f"- {e['problem']} (who: {e['affected_party']})" for e in evidence)
    samples = router.run_samples(
        "role_map",
        [
            {"role": "system", "content": ROLE_MAP_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {"role": "user", "content": f"PROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment']}\nDESCRIPTION: {hyp['description']}\n\nEVIDENCE:\n{ev}"},
        ],
        RoleMap,
        object_type="hypothesis",
        object_id=hyp["id"],
        max_tokens=1200,
    )
    if not samples:
        db.enqueue_review("role_map", "hypothesis", hyp["id"], "undecided", {"title": hyp["title"]})
        return []
    roles, low = consensus_roles([s.roles for s in samples])
    db.set_role_map(hyp["id"], roles, low_agreement=low)
    if low:
        db.enqueue_review("role_map", "hypothesis", hyp["id"], "low_agreement", {"roles": roles})
    return roles


def consensus_roles(samples: list[list[RoleCategory]]) -> tuple[list[dict[str, Any]], bool]:
    """Kategórie, na ktorých sa zhodne väčšina vzoriek (podobnosť názvu ≥ 80). Pri slabej zhode
    sa použije prvá vzorka a príznak low_agreement."""
    if not samples:
        return [], True
    if len(samples) == 1:
        return [r.model_dump() for r in samples[0]], False
    need = len(samples) // 2 + 1
    clusters: list[dict[str, Any]] = []
    for si, roles in enumerate(samples):
        for r in roles:
            key = fold(r.category)
            for c in clusters:
                if fuzz.token_set_ratio(key, c["key"]) >= 80:
                    c["samples"].add(si)
                    c["examples"] += [e for e in r.examples if e not in c["examples"]]
                    break
            else:
                clusters.append({"key": key, "category": r.category, "examples": list(r.examples), "rationale": r.rationale,
                                 "samples": {si}})
    agreed = [c for c in clusters if len(c["samples"]) >= need]
    if len(agreed) < 2:
        return [r.model_dump() for r in samples[0]], True
    return [{"category": c["category"], "examples": c["examples"][:6], "rationale": c["rationale"]} for c in agreed], False


def deterministic_fit(role: str | None, roles: list[dict[str, Any]]) -> tuple[float | None, str]:
    """Zhoda funkcie s mapou rolí (R-PPL-15): presná 1,0, príbuzná 0,5, všeobecné vedenie 0,3.
    None = nejasné (rozhodne model)."""
    key = title_key(role)
    if not key:
        return 0.0, ""
    best, best_cat = 0.0, ""
    for r in roles:
        for text in [r.get("category", "")] + list(r.get("examples", [])):
            score = fuzz.token_set_ratio(key, title_key(text))
            if score > best:
                best, best_cat = score, r.get("category", "")
    if best >= 90:
        return 1.0, best_cat
    if best >= 75:
        return 0.5, best_cat
    if any(g in key for g in GENERAL_MANAGEMENT):
        return 0.3, "všeobecné vedenie"
    return None, ""


def role_fit_for(cfg: Config, db: DB, router: ModelRouter, hyp, roles: list[dict[str, Any]], role: str | None,
                 budget: Counter) -> tuple[float, str, str]:
    """(skóre, kategória, zdroj) – kód, cache, alebo presný model (rozpočet volaní na beh)."""
    score, cat = deterministic_fit(role, roles)
    if score is not None:
        return score, cat, "kód"
    key = title_key(role)
    cached = db.get_role_fit(hyp["id"], key)
    if cached is not None:
        return float(cached["score"]), cached["category"] or "", "cache"
    if budget[f"role_fit:{hyp['id']}"] >= ROLE_FIT_CALLS_PER_HYPOTHESIS or not roles:
        return 0.0, "", "neposúdené"
    budget["role_fit"] += 1
    budget[f"role_fit:{hyp['id']}"] += 1
    listing = "\n".join(f"- {r['category']}: {', '.join(r.get('examples', []))}" for r in roles)
    res = router.run_task(
        "role_fit",
        [
            {"role": "system", "content": ROLE_FIT_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {"role": "user", "content": f"JOB TITLE: {role}\nROLE CATEGORIES:\n{listing}"},
        ],
        RoleFit,
        object_type="hypothesis",
        object_id=hyp["id"],
        vote_key=lambda r: r.fit,
        max_tokens=300,
    )
    if not res.ok:
        return 0.0, "", "neurčené"
    s = FIT_SCORE[res.value.fit]
    db.put_role_fit(hyp["id"], key, s, res.value.category, res.value.rationale)
    return s, res.value.category, "model"


def channel_of(db: DB, person_id: int, org_id: int | None) -> tuple[float, str]:
    """Najlepší kanál osoby (R-PPL-17)."""
    kinds = {c["kind"]: c for c in db.contacts_for_person(person_id)}
    if "email" in kinds:
        return (CHANNEL_VALUE["generic_email"], "generický e-mail") if is_generic_email(kinds["email"]["value"]) else (CHANNEL_VALUE["email"], "osobný e-mail")
    if "phone" in kinds:
        return CHANNEL_VALUE["phone"], "osobný telefón"
    if "hidden_contact" in kinds:
        return CHANNEL_VALUE["hidden_contact"], "skrytý kontakt (otvor ručne)"
    if org_id is not None and any(c["kind"] == "email" for c in db.org_contacts(org_id)):
        return CHANNEL_VALUE["generic_email"], "generický e-mail organizácie"
    if "profile_url" in kinds:
        return CHANNEL_VALUE["profile_url"], "iba profil"
    return 0.0, "bez kontaktu"


def org_fit_of(org_row) -> tuple[float, str]:
    if org_row is None:
        return 0.5, "bez organizácie"
    relation = org_row["relation"]
    if relation in ("mentioned_with_problem", "quoted_person"):
        return 1.0, "organizácia v dôkaze"
    if relation == "data_holder":
        return 1.0, "drží dáta k hypotéze"
    if relation == "procured_related":
        return 0.7, "obstarávanie"
    if relation == "expert_org":
        return 0.7, "expertná organizácia (hľadanie podľa hypotézy)"
    if relation in ("segment_match", "contact_search"):
        import json as _json

        fit = _json.loads(org_row["fit_json"] or "{}")
        parts = [v for k, v in fit.items() if k in ("odvetvie", "veľkosť", "región", "typ") and v != "bez filtra"]
        label = "segment" if relation == "segment_match" else "hľadanie podľa hypotézy"
        if not parts:
            return (0.5, label) if relation == "segment_match" else (0.6, label)
        score = sum(1.0 if v == "zhoda" else 0.5 if v == "neznáme" else 0.0 for v in parts) / len(parts)
        return round(score, 2), label + ": " + ", ".join(f"{k} {v}" for k, v in fit.items() if k in ("odvetvie", "veľkosť", "región", "typ"))
    return 0.5, relation


def is_expert(person, org) -> bool:
    role = fold(person["role_title"])
    if any(w in role for w in EXPERT_ROLES):
        return True
    if org is not None:
        if org["relation"] == "expert_org":
            return True
        name = fold(org["name"])
        return org["sector_kind"] == "nonprofit" or any(w in name for w in EXPERT_ORG_WORDS)
    return False


# Účel kontaktu (audit 14. 10. 2026, P1): rozlišovať používateľa riešenia, vlastníka procesu, správcu dát,
# schvaľovateľa rozpočtu a experta – každá rola potrebuje vlastný dôkaz (funkcia zverejnená organizáciou alebo
# zadaná človekom). Politici a členovia orgánov (predstavenstvo, dozorná rada) nie sú vlastníci procesu; osoba
# spomenutá v článku nemá doloženú kompetenciu (H11: poslanci a členovia predstavenstiev v zozname).
POLITICAL_RE = re.compile(r"poslan|\bminist|statn\w* tajomn|europoslan|senator|zastupitel|politi|"
                          r"(predsed|podpredsed)\w* (vlady|strany|parlament|nr sr|narodnej rady)")
BOARD_RE = re.compile(r"(clen|predsed|podpredsed)\w* (predstavenstva|dozornej rady|spravnej rady|statutarneho organu|"
                      r"vedeckej rady|rady)|\bboard\b")
RESPONSIBILITY_RE = re.compile(r"veduc|riaditel|manazer|manager|\bhead\b|zodpoved|koordinator|garant|owner|\bsef|\bhlavn|"
                               r"prednost|konatel|majitel")
BUDGET_RE = re.compile(r"financ|ekonom|rozpoc|\bcfo\b|controll|konatel|generaln\w* riaditel|starost|primator|prednost|"
                       r"majitel")
USER_RE = re.compile(r"referent|specialist|uctov|operator|asistent|technik|dispecer|samostatn|odborn\w* pracovn")
OWN_SOURCES = {"website": "web organizácie", "rpo": "register RPO", "catalog": "katalóg dát", "procurement": "obstarávanie",
               "manual": "zadané tebou"}
PURPOSES = ("vlastník procesu", "používateľ riešenia", "správca dát", "schvaľovateľ rozpočtu", "expert")


def small_org(org, hyp=None) -> bool:
    """Mikro alebo malá organizácia. Neznáma veľkosť (RÚZ bez údaja, M42: 7 zo 14 organizácií H3) sa ráta ako malá,
    ak bola organizácia vybraná podľa segmentu hypotézy, ktorý cieli iba na mikro a malé organizácie."""
    if org is None:
        return False
    if org["size_category"] in ("micro", "small"):
        return True
    if org["size_category"] or hyp is None or "relation" not in org.keys() or org["relation"] != "segment_match":
        return False
    try:
        sizes = set(json.loads(hyp["segment_filter_json"] or "{}").get("sizes") or [])
    except ValueError:
        return False
    return bool(sizes) and sizes <= {"micro", "small"}


def is_small_org_lead(person, org, hyp=None) -> bool:
    """Vedenie malej organizácie (konateľ, majiteľ, riaditeľ; štatutár z RPO) – rozhoduje aj platí (M42)."""
    if not small_org(org, hyp):
        return False
    title = fold(person["role_title"] or "")
    if POLITICAL_RE.search(title) or BOARD_RE.search(title):
        return False
    return person["source_kind"] == "rpo" or any(g in title_key(person["role_title"]) for g in GENERAL_MANAGEMENT)


def contact_purposes(person, fit: float, org=None, expert: bool = False, small: bool | None = None) -> list[dict[str, str]]:
    """Účely kontaktu, každý s vlastným dôkazom (kód, bez modelu). Vlastník procesu: funkcia zodpovednosti (vedúci,
    riaditeľ, manažér…) zodpovedá roli z mapy (role_fit ≥ 0,5) a zverejnila ju organizácia alebo register."""
    title = fold(person["role_title"] or "")
    src = person["source_kind"] or ""
    where = (f"funkcia „{person['role_title'] or '?'}“ ({OWN_SOURCES.get(src, src or 'zdroj')}: "
             f"{person['source_url']})")
    if POLITICAL_RE.search(title) or BOARD_RE.search(title):
        return [{"rola": "politik alebo člen orgánu", "dôkaz": where + " – nie je vlastník procesu ani správca dát"}]
    own = src in OWN_SOURCES or (not src and org is not None and org["website"]  # staršie záznamy bez druhu zdroja
                                 and domain_of(person["source_url"] or "") == domain_of(org["website"]))
    if not own:
        return [{"rola": "citovaná osoba", "dôkaz": f"{where} – kompetencia nedoložená (nie zo zverejnenia organizácie)"}]
    out = []
    if expert:
        out.append({"rola": "expert", "dôkaz": where})
    small = small_org(org) if small is None else small
    if fit >= 0.5 and (RESPONSIBILITY_RE.search(title) or src == "manual"):
        out.append({"rola": "vlastník procesu", "dôkaz": where + (" · vedenie malej organizácie" if small and
                                                                  "konatel" in title else "")})
    elif fit >= 0.5 or (fit >= 0.3 and USER_RE.search(title)):  # funkcia z mapy rolí bez zodpovednosti: zažíva problém
        out.append({"rola": "používateľ riešenia", "dôkaz": where})
    if "kontaktny bod" in title or any(t.startswith(DATA_ROLE_PREFIXES) for t in re.findall(r"\w+", title)):
        out.append({"rola": "správca dát", "dôkaz": where})
    if BUDGET_RE.search(title):
        out.append({"rola": "schvaľovateľ rozpočtu", "dôkaz": where})
    return out


def data_role_fit(role: str | None, target_roles: list[str]) -> float:
    """Funkcia osoby v organizácii, ktorá drží dáta: 1,0 pri práci s dátami alebo s funkciou z cieľa plánu,
    0,5 pri príbuznej funkcii z cieľa, inak 0 (kód, bez modelu)."""
    key = fold(role)
    if not key:
        return 0.0
    if "kontaktny bod" in key or any(t.startswith(DATA_ROLE_PREFIXES) for t in re.findall(r"\w+", key)):
        return 1.0
    score, _ = deterministic_fit(role, [{"category": r, "examples": []} for r in target_roles])
    return score if score in (1.0, 0.5) else 0.0


def _target_roles(target) -> list[str]:
    import json as _json

    if target is None:
        return []
    try:
        return [str(r) for r in _json.loads(target["roles_json"] or "[]")]
    except ValueError:
        return []


def rank_hypothesis(cfg: Config, db: DB, router: ModelRouter, hyp, budget: Counter) -> dict:
    """Väzby, priorita a výber: zákazníci (people.max_people_per_hypothesis, najviac max_people_per_org na
    organizáciu), držitelia dát (max_data_holders_per_hypothesis) a experti (max_experts_per_hypothesis) zvlášť.
    Ručne pridaní ľudia (source_kind „manual“) ostávajú vždy a do limitov sa nepočítajú."""
    stats: Counter = Counter()
    hid = hyp["id"]
    roles = role_map_for(cfg, db, router, hyp)
    orgs = {o["id"]: o for o in db.orgs_for_hypothesis(hid)}
    targets = {t["id"]: t for t in db.contact_targets_for(hid)}
    people = {}
    for org_id in orgs:
        for p in db.people_of_org(org_id):
            people[p["id"]] = p
    for link in db.links_for_hypothesis(hid):
        people.setdefault(link["person_id"], db.get_person(link["person_id"]))
    w = cfg.people.priority_weights
    scored = []
    for pid, person in people.items():
        if person is None or person["status"] == "excluded":
            continue
        org = orgs.get(person["org_id"])
        relation = org["relation"] if org is not None else None
        target = targets.get(org["target_id"]) if org is not None and org["target_id"] else None
        existing = {l["link_type"]: l for l in db.links_for_person(pid) if l["hypothesis_id"] == hid}
        manual = person["source_kind"] == "manual"
        if manual:
            # Ručne pridaný človek (GUI, people-add): väzbu zadal človek, fit sa iba zobrazí.
            types = set(existing) or {"role_owner"}
            group = "data_holder" if "data_holder" in types else "expert" if "expert" in types else "customer"
            fit, cat, fit_src = role_fit_for(cfg, db, router, hyp, roles, person["role_title"], budget)
            fit_src = "ručne pridaný; " + fit_src
        elif (relation == "data_holder" or person["source_kind"] == "catalog") and "quoted_about_problem" not in existing:
            fit = data_role_fit(person["role_title"], _target_roles(target))
            cat, fit_src = "práca s dátami", "kód (dáta)"
            if person["source_kind"] == "catalog":
                fit, cat, fit_src = 1.0, "kontaktný bod datasetu", "úradné zverejnenie"
            if fit < 0.5:
                stats["data_holder_role_mismatch"] += 1
                stats["no_link"] += 1
                continue
            types, group = {"data_holder"}, "data_holder"
        else:
            fit, cat, fit_src = role_fit_for(cfg, db, router, hyp, roles, person["role_title"], budget)
            if fit == FIT_SCORE["general"] and small_org(org, hyp):
                # V malej organizácii vedenie zvyčajne samo rozhoduje o procese s problémom (R-PPL-14, príklad „konateľ malej firmy“).
                fit, cat, fit_src = FIT_SCORE["related"], "vedenie malej organizácie", fit_src + " + veľkosť"
            expert = is_expert(person, org) and relation != "mentioned_with_problem"
            if expert and relation == "expert_org" and target is not None:
                tfit, tcat = deterministic_fit(person["role_title"], [{"category": r, "examples": []} for r in _target_roles(target)])
                if tfit is not None and tfit > fit:
                    fit, cat, fit_src = tfit, tcat or cat, "kód (cieľ plánu)"
            # Väzby zo zberu (citácia, obstarávanie) ostávajú; ostatné sa pri každom poradí počítajú znova.
            types = {t for t in existing if t in COLLECTED_LINKS}
            purposes = {p_["rola"] for p_ in contact_purposes(person, fit, org, expert, small_org(org, hyp))}
            if expert:
                types.add("expert")
            else:
                if "vlastník procesu" in purposes:  # dôkaz zodpovednosti, nie iba podobná funkcia
                    types.add("role_owner")
                elif "používateľ riešenia" in purposes:
                    types.add("process_user")
                elif fit >= 0.5:
                    stats["role_without_evidence"] += 1  # politik, člen orgánu alebo osoba z článku
                if relation in ("mentioned_with_problem", "quoted_person") and (fit >= 0.3 or person["source_kind"] == "rpo") \
                        and "politik alebo člen orgánu" not in purposes:
                    types.add("org_affected")
                if relation == "procured_related" and person["source_kind"] == "procurement":
                    types.add("procurement_contact")
            group = "expert" if expert else "customer"
        if cat and cat != (person["role_category"] or ""):
            db.update_person(pid, role_category=cat)
        if not types:
            stats["no_link"] += 1
            continue
        link_type = max(types, key=lambda t: LINK_STRENGTH.get(t, 0.0))
        channel, channel_label = channel_of(db, pid, person["org_id"])
        org_fit, org_label = org_fit_of(org)
        components = {
            "role_fit": fit, "role_fit_zdroj": fit_src, "kategória": cat,
            "väzba": LINK_STRENGTH.get(link_type, 0.0), "väzba_typ": link_type,
            "kanál": channel, "kanál_popis": channel_label,
            "zhoda_org": org_fit, "zhoda_org_popis": org_label,
            "skupina": group, "váhy": w.model_dump(),
            "účely": contact_purposes(person, fit, org, group == "expert", small_org(org, hyp)) if not manual else
            [{"rola": "vlastník procesu" if "role_owner" in types else "podľa teba", "dôkaz": "zadané tebou"}],
        }
        if target is not None:
            components["cieľ"] = target["who"]
        priority = round(w.role_fit * fit + w.link * LINK_STRENGTH.get(link_type, 0.0) + w.channel * channel
                         + w.org_fit * org_fit, 3)
        scored.append((priority, pid, person, org, types, link_type, components, group, manual))

    scored.sort(key=lambda x: (-x[0], x[1]))
    limits = {"customer": cfg.people.max_people_per_hypothesis, "expert": cfg.people.max_experts_per_hypothesis,
              "data_holder": cfg.people.max_data_holders_per_hypothesis}
    per_org: dict[str, Counter] = {g: Counter() for g in limits}
    chosen: list = [item for item in scored if item[8]]  # ručne pridaní vždy
    count: Counter = Counter()
    # Vedenie malej organizácie (konateľ, majiteľ) sa vyberie vždy, najviac jeden na organizáciu a mimo
    # max_people_per_org (M42): s limitom 2 na organizáciu predbehli konateľku mikro kancelárie dve maklérky
    # s presnou zhodou funkcie a výber ju natrvalo vymazal.
    leads: set[int] = set()
    for item in scored:
        priority, pid, person, org, types, link_type, comp, group, manual = item
        if manual or group != "customer" or person["org_id"] in leads or count[group] >= limits[group]:
            continue
        if is_small_org_lead(person, org, hyp):
            comp["vedenie_malej_organizácie"] = "vybraté vždy (mimo limitu na organizáciu)"
            chosen.append(item)
            leads.add(person["org_id"])
            count[group] += 1
            stats["small_org_leads"] += 1
    # Prednosť majú rôzne organizácie: najprv najlepší z každej organizácie, potom druhí, tretí…
    for round_ in range(1, max(1, cfg.people.max_people_per_org) + 1):
        for item in scored:
            priority, pid, person, org, types, link_type, comp, group, manual = item
            if manual or item in chosen:
                continue
            key = person["org_id"] or -pid
            if per_org[group][key] >= round_ or count[group] >= limits[group]:
                continue
            chosen.append(item)
            per_org[group][key] += 1
            count[group] += 1
    keep = {item[1] for item in chosen}
    for priority, pid, person, org, types, link_type, comp, group, manual in chosen:
        if db.get_person(pid) is None:  # medzitým vymazaná (výmaz v GUI počas behu)
            stats["removed_meanwhile"] += 1
            continue
        db.prune_links(pid, hid, types)
        for t in types:
            evidence_url = None
            evidence_claim = None
            if t == "org_affected" and org is not None:
                evidence_claim, evidence_url = org["evidence_claim_id"], org["evidence_url"]
            if t == "data_holder" and org is not None:
                evidence_url = org["evidence_url"]
            # Ručne pridaný človek si nechá odôvodnenie a „čo žiadať“ zo zadania (COALESCE v DB).
            link_id = db.upsert_link(pid, hid, t, evidence_claim, evidence_url,
                                     rationale=None if manual else _rationale(t, comp, org),
                                     verdict="supported" if manual or t != "role_owner" or comp["role_fit"] >= 0.5 else "weak")
            if t == link_type:
                db.set_link_priority(link_id, priority, comp)
        db.update_person(pid, is_expert=int(group == "expert"))
        stats[{"expert": "experts", "data_holder": "data_holders"}.get(group, "selected")] += 1
        if manual:
            stats["manual_kept"] += 1
    # Nevybraní kandidáti sa vymažú (minimalizácia); osoby s ďalšou komunikáciou alebo inou hypotézou ostanú.
    for pid, person in people.items():
        if person is None or pid in keep:
            continue
        others = [l for l in db.links_for_person(pid) if l["hypothesis_id"] != hid]
        if person["status"] == "candidate" and person["source_kind"] != "manual" and not others and not db.outreach_for_person(pid):
            db.delete_person(pid)
            stats["dropped"] += 1
        else:
            db.unlink_person(pid, hid)
    return dict(stats)


def _rationale(link_type: str, comp: dict[str, Any], org) -> str:
    org_name = org["name"] if org is not None else "organizácia"
    if link_type == "role_owner":
        return (f"Zodpovedá za proces: funkcia zodpovedá roli „{comp.get('kategória') or '?'}“ z mapy rolí a je "
                "zverejnená organizáciou.")
    if link_type == "process_user":
        return f"Pracuje v procese s problémom („{comp.get('kategória') or '?'}“) – zažíva ho, nerozhoduje o ňom."
    if link_type == "org_affected":
        return f"{org_name} má problém podľa dôkazu; osoba je vo vedení."
    if link_type == "procurement_contact":
        return "Kontaktná osoba v oznámení o obstarávaní."
    if link_type == "expert":
        return "Rozumie problému (výskum, zväz, poradenstvo), nie je zákazník."
    if link_type == "data_holder":
        source = f" ({comp.get('kategória')})" if comp.get("kategória") else ""
        return f"{org_name} drží dáta k hypotéze; osoba s nimi pracuje{source}."
    return "Verejne sa vyjadril(a) k problému."


PROFILE_QUERIES = ('"{name}" "{org}" linkedin', '"{name}" linkedin {org_short}')


def profile_match(person, results) -> tuple[str, str] | None:
    """Prvý výsledok vyhľadávania, ktorý je profil LinkedIn tejto osoby: URL /in/, celé meno v titulku
    a slovo z názvu organizácie v titulku alebo úryvku. Vráti (kanonická URL, úryvok)."""
    name_words = set(fold(person["full_name"]).split())
    # Živnostník má v názve vlastné meno: zhoda „organizácie“ by bola samozrejmá, preto sa profil neuloží.
    org_words = [w for w in org_name_key(person["org_name"] or "").split() if len(w) >= 4 and w not in name_words][:3]
    if not org_words:
        return None
    for r in results:
        url = normalize_profile_url(r.url)
        if url is None or not is_person_profile(url):
            continue
        if name_in_text(person["full_name"], r.title) < 0:
            continue
        if not any(w in fold(r.title + " " + r.snippet) for w in org_words):
            continue
        return url, f"{r.title} – {r.snippet}"[:300]
    return None


def search_profiles(cfg: Config, db: DB, searx, hid: int, limit: int | None = None,
                    deadline: Deadline | None = None) -> int:
    """URL profilu LinkedIn iba z výsledku vyhľadávania, bez stiahnutia stránky (R-PPL-13), pre všetkých
    vybraných ľudí hypotézy (aj s e-mailom). Označí sa „over ručne“; nepotvrdený profil sa neuloží."""
    if not cfg.people.search_profiles:
        return 0
    sup = Suppression(cfg, db)
    found = 0
    for link in db.links_for_hypothesis(hid)[: limit or cfg.people.max_people_per_hypothesis]:
        if deadline is not None and deadline.expired():
            break
        person = db.get_person(link["person_id"])
        if person is None or not person["org_name"] or person["status"] == "excluded":
            continue
        if any(c["kind"] == "profile_url" for c in db.contacts_for_person(person["id"])):
            continue
        if sup.is_suppressed(name=person["full_name"], org_key=org_name_key(person["org_name"])):
            continue
        org_short = " ".join(org_name_key(person["org_name"]).split()[:3])
        for template in PROFILE_QUERIES:
            query = template.format(name=person["full_name"], org=person["org_name"], org_short=org_short)
            try:
                results = searx.search(query, limit=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hľadanie profilu zlyhalo: %s", exc)
                break
            match = profile_match(person, results)
            if match is None:
                continue
            url, snippet = match
            source = next((r.url for r in results if normalize_profile_url(r.url) == url), url)
            db.add_contact_point(person["id"], person["org_id"], "profile_url", url, source, snippet, "search_result",
                                 False, {"over_ručne": True, "stiahnuté": False, "zhoda_mena": True,
                                         "zhoda_organizácie": True})
            found += 1
            break
    return found


def run_profiles(cfg: Config, db: DB, searx, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    """Doplní LinkedIn profily už vybraným ľuďom (napr. po zmene pravidiel); nič nesťahuje."""
    require_lia(cfg)
    ids = hypothesis_ids or [h["id"] for h in db.people_enabled_hypotheses()]
    stats: Counter = Counter()
    for hid in ids:
        if deadline.expired():
            break
        stats["profiles"] += search_profiles(cfg, db, searx, hid, deadline=deadline)
        stats["hypotheses"] += 1
    return dict(stats)


def add_manual_profile(cfg: Config, db: DB, person_id: int, url: str) -> str:
    """Ručne zadaná URL profilu LinkedIn (GUI). Uloží sa iba kanonická URL /in/…; nič sa nesťahuje."""
    value = normalize_profile_url(url)
    if value is None or not is_person_profile(value):
        raise ValueError("Zadaj URL profilu LinkedIn v tvare https://www.linkedin.com/in/…")
    person = db.get_person(person_id)
    if person is None:
        raise ValueError(f"Osoba {person_id} neexistuje.")
    if Suppression(cfg, db).is_suppressed(name=person["full_name"], org_key=org_name_key(person["org_name"] or "")):
        raise ValueError("Osoba je v zozname potlačenia (forget); profil sa neuloží.")
    db.add_contact_point(person_id, person["org_id"], "profile_url", value, value, "Zadané ručne v GUI.", "manual",
                         False, {"zadané_ručne": True, "stiahnuté": False})
    return value


def run_people_rank(cfg: Config, db: DB, llm: Any, searx, deadline: Deadline, hypothesis_ids: list[int] | None = None) -> dict:
    require_lia(cfg)
    router = as_router(llm, cfg, db)
    budget: Counter = Counter()
    stats: Counter = Counter()
    ids = hypothesis_ids or [h["id"] for h in db.people_enabled_hypotheses()]
    for hid in ids:
        if deadline.expired():
            break
        hyp = db.get_hypothesis(hid)
        if hyp is None:
            continue
        try:
            stats.update(rank_hypothesis(cfg, db, router, hyp, budget))
            if searx is not None:
                stats["profiles"] += search_profiles(cfg, db, searx, hid, deadline=deadline)
        except APIConnectionError:
            raise  # model nebeží: ďalšie hypotézy by zlyhali tiež
        except Exception:  # noqa: BLE001 – chyba jednej hypotézy nezastaví poradie ostatných (akcia #20)
            log.exception("Poradie ľudí pre H%s zlyhalo", hid)
            stats["errors"] += 1
    stats["role_fit_model_calls"] = budget["role_fit"]
    return dict(stats)
