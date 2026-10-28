"""Súkromie (R-PRIV): maskovanie logov, potlačenie, forget, purge, pseudonymizácia a brány.

Toto nie je právna rada; technické opatrenia podľa docs/SPEC_brana0.md kap. 12.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import unicodedata
from pathlib import Path

import phonenumbers

# E-mail a telefón na maskovanie v logoch.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Záloha k phonenumbers pre zápisy s medzinárodnou predvoľbou, ktoré knižnica neoverí ako platné.
INTL_PHONE_RE = re.compile(r"(?<![\w])\+\d{1,3}[\s./-]?\d{2,4}(?:[\s./-]?\d{2,4}){2,3}(?!\d)")


def mask_text(text: str) -> str:
    """Nahradí e-maily a telefónne čísla maskou (aj v správach chýb a v URL mailto:/tel:).
    Telefóny hľadá knižnica phonenumbers (SK), takže IČO ani bežné čísla sa nemaskujú."""
    if not text:
        return text
    text = EMAIL_RE.sub(lambda m: m.group(0)[0] + "***@***", text)
    if any(ch.isdigit() for ch in text):
        spans = [(m.start, m.end) for m in phonenumbers.PhoneNumberMatcher(text, "SK")]
        for start, end in reversed(spans):
            text = text[:start] + "[telefón]" + text[end:]
        text = INTL_PHONE_RE.sub("[telefón]", text)
    return text


class MaskingFilter(logging.Filter):
    """Filter na handleroch logovania: nikde v logu (ani v logs/radar.log) nie je e-mail ani telefón."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 – zlý formát správy nesmie zhodiť logovanie
            message = str(record.msg)
        masked = mask_text(message)
        if masked != message or record.args:
            record.msg, record.args = masked, None
        if record.exc_info and not record.exc_text:
            record.exc_text = mask_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = mask_text(record.exc_text)
        return True


def install_log_masking(logger: logging.Logger | None = None) -> None:
    logger = logger or logging.getLogger()
    for handler in logger.handlers:
        if not any(isinstance(f, MaskingFilter) for f in handler.filters):
            handler.addFilter(MaskingFilter())


# ---------------------------------------------------------------- normalizácia a hash
def fold(text: str) -> str:
    """Malé písmená bez diakritiky a nadbytočných medzier."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def salt_path(db_path: str, configured: str | None = None) -> Path:
    return Path(configured) if configured else Path(db_path).parent / ".salt"


def load_salt(path: Path) -> str:
    """Soľ pre hashe potlačenia; vytvorí sa pri prvom použití (súbor je v gitignorovanom data/)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def identifier_hash(salt: str, kind: str, value: str) -> str:
    return hashlib.sha256(f"{salt}|{kind}|{value}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- brány (R-PRIV-1, R-OUT-15)
class PrivacyGateError(RuntimeError):
    """Spracúvanie osobných údajov nie je povolené (chýba LIA alebo profil odosielateľa)."""


LIA_MESSAGE = (
    "Hľadanie ľudí a návrhy oslovení sú zablokované: najprv vyplň posúdenie oprávneného záujmu "
    "(šablóna docs/LIA_template.md, vyplnená kópia v data/LIA.md) a v config.yaml nastav "
    "privacy.lia_completed: true."
)
PLACEHOLDER_RE = re.compile(r"\{[A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ_ ]{3,}\}")


def require_lia(cfg) -> None:
    if not cfg.privacy.lia_completed:
        raise PrivacyGateError(LIA_MESSAGE)


def placeholders_in(text: str | None) -> list[str]:
    return PLACEHOLDER_RE.findall(text or "")


def sender_placeholders(cfg) -> list[str]:
    s = cfg.outreach.sender
    found: list[str] = []
    for value in (s.name, s.email, s.affiliation, s.privacy_notice_url, s.phone):
        found += placeholders_in(value)
    return found


# ---------------------------------------------------------------- potlačenie (R-PRIV-6)
def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str) -> str:
    try:
        number = phonenumbers.parse(value, "SK")
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return re.sub(r"[^\d+]", "", value or "")


class Suppression:
    """Hashe identifikátorov osôb, ktoré systém už nesmie zbierať ani oslovovať.
    sha256(soľ + druh + normalizovaná hodnota); soľ je v data/.salt (mimo gitu)."""

    def __init__(self, cfg, db):
        self.db = db
        self.salt = load_salt(salt_path(cfg.db_path, cfg.privacy.salt_path))

    def hash(self, kind: str, value: str) -> str:
        return identifier_hash(self.salt, kind, value)

    def keys(self, email: str | None = None, phone: str | None = None, name: str | None = None,
             org_key: str | None = None, domain: str | None = None) -> list[tuple[str, str]]:
        from .names import person_name_key

        out = []
        if email:
            out.append(("email", self.hash("email", normalize_email(email))))
        if phone:
            out.append(("phone", self.hash("phone", normalize_phone(phone))))
        if name:
            out.append(("name_org", self.hash("name_org", f"{person_name_key(name)}|{org_key or ''}")))
        if domain:
            out.append(("domain", self.hash("domain", domain.lower().removeprefix("www."))))
        return out

    def is_suppressed(self, **identifiers: str | None) -> bool:
        return self.db.suppressed([h for _, h in self.keys(**identifiers)])

    def add(self, reason: str, **identifiers: str | None) -> int:
        keys = self.keys(**identifiers)
        for kind, h in keys:
            self.db.add_suppression(h, kind, reason)
        return len(keys)


def _org_key(db, org_id: int | None) -> str:
    from .names import org_name_key

    if org_id is None:
        return ""
    org = db.get_organization(org_id)
    return org_name_key(org["name"]) if org else ""


def forget(cfg, db, person_id: int | None = None, email: str | None = None, phone: str | None = None,
           reason: str = "erasure") -> dict:
    """Výmaz osoby (námietka, žiadosť o výmaz): zmaže osobu, kontakty a oslovenia, rozhovory
    ponechá iba s pseudonymom a pridá hashe do potlačenia, aby ju systém znova nezbieral (R-PRIV-5)."""
    sup = Suppression(cfg, db)
    ids: set[int] = set()
    if person_id is not None:
        ids.add(int(person_id))
    if email:
        ids |= set(db.people_by_contact("email", normalize_email(email)))
        sup.add(reason, email=email)
    if phone:
        ids |= set(db.people_by_contact("phone", normalize_phone(phone)))
        sup.add(reason, phone=phone)
    removed = 0
    for pid in sorted(ids):
        person = db.get_person(pid)
        if person is None:
            continue
        for c in db.contacts_for_person(pid):
            if c["kind"] == "email":
                sup.add(reason, email=c["value"])
            elif c["kind"] == "phone":
                sup.add(reason, phone=c["value"])
        sup.add(reason, name=person["full_name"], org_key=_org_key(db, person["org_id"]))
        db.anonymize_interviews_of(pid)
        db.scrub_commercial_evidence_of_person(pid)  # obchodný fakt o organizácii ostane, text a väzba na osobu nie
        db.scrub_buyer_factors_of_person(pid)  # ekonomika u zákazníka (M38) – rovnako
        db.delete_person(pid)
        removed += 1
    contacts_removed = 0
    if email:
        contacts_removed += db.delete_contact_values("email", normalize_email(email))
    if phone:
        contacts_removed += db.delete_contact_values("phone", normalize_phone(phone))
    return {"people_removed": removed, "contacts_removed": contacts_removed}


def reapply(cfg, db) -> dict:
    """Po obnove zo zálohy: zmaže osoby a kontakty, ktoré sú v zozname potlačenia."""
    sup = Suppression(cfg, db)
    removed = 0
    for person in db.all_people():
        hit = sup.is_suppressed(name=person["full_name"], org_key=_org_key(db, person["org_id"]))
        for c in db.contacts_for_person(person["id"]):
            if c["kind"] == "email" and sup.is_suppressed(email=c["value"]):
                hit = True
            if c["kind"] == "phone" and sup.is_suppressed(phone=c["value"]):
                hit = True
        if hit:
            db.anonymize_interviews_of(person["id"])
            db.delete_person(person["id"])
            removed += 1
    contacts = 0
    for org in db.all_organizations():
        for c in db.org_contacts(org["id"]):
            if (c["kind"] == "email" and sup.is_suppressed(email=c["value"])) or (
                c["kind"] == "phone" and sup.is_suppressed(phone=c["value"])
            ):
                contacts += db.delete_contact_values(c["kind"], c["value"])
    return {"people_removed": removed, "contacts_removed": contacts}


# ---------------------------------------------------------------- retencia (R-PRIV-4)
def purge(cfg, db, now=None) -> dict:
    """Retencia: neoslovení po 90 dňoch, po poslednej interakcii po 12 mesiacoch, odmietnutí po
    30 dňoch, exporty po 30 dňoch. Beží aj na konci run-all."""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    p = cfg.privacy

    def iso(days: int) -> str:
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    stats = {"uncontacted": 0, "after_interaction": 0, "declined": 0, "exports": 0, "org_contacts": 0}

    def drop(person_id: int, key: str) -> None:
        db.anonymize_interviews_of(person_id)
        db.delete_person(person_id)
        stats[key] += 1

    for pid in db.uncontacted_people_before(iso(p.retention_days_uncontacted)):
        drop(pid, "uncontacted")
    declined_cutoff = iso(p.retention_days_declined)
    now_iso = now.isoformat(timespec="seconds")
    for r in db.declined_people():
        due = r["purge_after"] or ""
        if (due and due <= now_iso) or (not due and (r["updated_at"] or "") < declined_cutoff):
            drop(int(r["id"]), "declined")
    for pid in db.people_inactive_since(iso(p.retention_days_after_interaction)):
        drop(pid, "after_interaction")
    # Kontakty organizácií po rovnakej lehote ako neoslovení (môžu byť pracovné adresy konkrétnych ľudí).
    stats["org_contacts"] = db.delete_org_contacts_before(iso(p.retention_days_uncontacted))
    exports = Path(cfg.exports_dir)
    if exports.exists():
        cutoff = (now - timedelta(days=p.retention_days_exports)).timestamp()
        for f in exports.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                stats["exports"] += 1
    return stats


# ---------------------------------------------------------------- pseudonymizácia (R-PRIV-8)
def person_pseudonym(person_id: int) -> str:
    return f"Osoba P{person_id}"


def pseudonymize_text(text: str, names: list[str]) -> str:
    """Nahradí mená, e-maily a telefóny v texte (správy, exporty)."""
    out = mask_text(text or "")
    for name in sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True):
        out = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[osoba]", out, flags=re.I)
    return out
