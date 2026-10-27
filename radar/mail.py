"""Pošta (M41, 27. 10. 2026): odoslané oslovenia a odpovede z Gmailu (IMAP, iba čítanie) bez ručného zapisovania.

- Kontrola (krok `mail-sync`, bez modelu; Plánovač úloh každých 15 min): v schránke sa hľadajú IBA správy od a pre
  e-mailové adresy z oslovení (Gmail X-GM-RAW) a nedoručenky; ostatná pošta sa neotvorí. Čítajú sa iba hlavičky.
  - tvoja správa na kontakt z oslovenia → oslovenie „odoslané“ (aj návrh, ktorý si neschválil – zapíše sa ako
    odoslaný mimo schválenia), ďalšia tvoja správa vo vlákne → follow-up,
  - odpoveď (vlákno Gmailu alebo adresa kontaktu) → „odpovedal“ a čaká na rozbor; automatická odpoveď sa iba zaznamená,
  - nedoručená správa (X-Failed-Recipients) → „nedoručené“.
- Rozbor odpovede (krok `mail-extract`, úloha v poradí – lokálny rýchly model, osobné údaje nejdú do cloudu): typ
  odpovede, problém, stupeň ochoty platiť, termín a ďalší krok ako NÁVRH. Citácie musia byť doslova v texte odpovede
  (kontrola kódom), inak sa návrh oslabí na „nejasné“. Až človek návrh potvrdí (Oslovenia → Pošta), vznikne výsledok
  oslovenia a obchodný dôkaz – model sám ochotu platiť nezapíše.
- Telá e-mailov sa neukladajú (iba maskovaná citácia v návrhu); heslo aplikácie Google je iba v premennej prostredia.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
import socket
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from .config import Config, MailConfig
from .db import DB
from .privacy import fold, mask_text
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

SYNC_KEY = "mail_sync"
HEADER_FIELDS = ("FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES AUTO-SUBMITTED X-AUTOREPLY X-AUTORESPOND "
                 "PRECEDENCE X-FAILED-RECIPIENTS")
AUTO_SUBJECT_RE = re.compile(r"automatick\w* odpove|out of office|mimo kancel|dovolenk|auto(matic)?[ -]?reply|"
                             r"abwesenheit|absence", re.I)
BOUNCE_FROM_RE = re.compile(r"mailer-daemon|postmaster", re.I)
QUOTE_CUT_RE = re.compile(r"^\s*(>|On .{0,160} wrote:|D[ňn]a .{0,160} nap[íi]sal|Od:\s|From:\s|-{2,}\s*Original Message|"
                          r"-{2,}\s*P[ôo]vodn[áa] spr[áa]va|_{5,}|Sent from my|Odoslan[ée] z )", re.I | re.M)
OPEN_FOR_SEND = ("draft", "needs_edit", "approved")
AFTER_SEND = ("sent", "replied", "scheduled", "no_reply", "interviewed", "no_show")
WTP = Literal["not_asked", "unclear", "buyer_identified", "budget_verified", "purchase_intent", "payment_received",
              "no_willingness"]
REPLY_LABEL = {"relevant": "relevantná odpoveď", "redirect": "presmerovanie", "not_relevant": "nerelevantné",
               "declined": "odmietnutie"}


class MailError(RuntimeError):
    pass


def password(mc: MailConfig) -> str | None:
    return os.environ.get(mc.password_env) or None


def addresses(value: Any) -> list[str]:
    return [a.lower() for _, a in getaddresses([str(value or "")]) if "@" in a]


@dataclass
class Header:
    uid: int
    message_id: str
    thread_id: str | None
    sent_at: str
    from_addr: str
    to: list[str] = field(default_factory=list)
    subject: str = ""
    in_reply_to: str = ""
    auto: bool = False
    failed: list[str] = field(default_factory=list)


def parse_header(uid: int, thread_id: str | None, raw: bytes) -> Header:
    """Hlavička správy (bez tela): adresy, dátum, vlákno, automatická odpoveď, nedoručené adresy."""
    msg = email.message_from_bytes(raw, policy=default_policy)
    try:
        when = parsedate_to_datetime(str(msg["Date"])).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        when = utcnow()
    subject = " ".join(str(msg["Subject"] or "").split())
    auto_submitted = str(msg["Auto-Submitted"] or "no").lower()
    auto = (auto_submitted not in ("", "no") or bool(msg["X-Autoreply"]) or bool(msg["X-Autorespond"])
            or str(msg["Precedence"] or "").lower() in ("auto_reply", "junk") or bool(AUTO_SUBJECT_RE.search(subject)))
    return Header(uid=uid, message_id=str(msg["Message-ID"] or "").strip(), thread_id=thread_id, sent_at=when,
                  from_addr=(addresses(msg["From"]) or [""])[0], to=addresses(f"{msg['To'] or ''}, {msg['Cc'] or ''}"),
                  subject=subject, in_reply_to=str(msg["In-Reply-To"] or "").strip(), auto=auto,
                  failed=addresses(msg["X-Failed-Recipients"]))


def plain_text(raw: bytes) -> str:
    """Text správy: text/plain, inak text z HTML (lxml)."""
    msg = email.message_from_bytes(raw, policy=default_policy)
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_subtype() == "html":
        from lxml import html as lxml_html

        try:
            return lxml_html.fromstring(content).text_content()
        except Exception:  # noqa: BLE001 – poškodené HTML: radšej prázdny text než pád synchronizácie
            return ""
    return content


def clean_body(text: str, max_chars: int) -> str:
    """Iba nová časť odpovede: bez citovanej pôvodnej správy a podpisu; e-maily a telefóny maskované."""
    m = QUOTE_CUT_RE.search(text or "")
    text = (text or "")[: m.start()] if m else (text or "")
    text = text.split("\n-- \n")[0]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return mask_text(text)[:max_chars]


# ======================================================================= IMAP
class _IMAP4SSLv4(imaplib.IMAP4_SSL):
    """IMAP cez TLS iba na IPv4 (na notebooku IPv6 spojenia padajú pri TLS – rovnako ako pri TED)."""

    def _create_socket(self, timeout):  # noqa: ANN001 – podpis z imaplib
        sock = socket.create_connection((self.host, self.port), timeout, source_address=("0.0.0.0", 0))
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class GmailImap:
    """Gmail IMAP, iba čítanie (schránka „Všetky správy“ otvorená readonly, hlavičky a telá cez BODY.PEEK)."""

    def __init__(self, mc: MailConfig, secret: str | None, factory=None):
        self.mc, self.secret, self.factory, self.imap = mc, secret, factory, None

    def __enter__(self) -> "GmailImap":
        if not self.mc.user:
            raise MailError("chýba adresa schránky (mail.user v config.local.yaml)")
        if not self.secret:
            raise MailError(f"chýba heslo aplikácie v premennej prostredia {self.mc.password_env}")
        if self.factory is not None:
            self.imap = self.factory(self.mc)
        elif self.mc.ipv4_only:
            self.imap = _IMAP4SSLv4(self.mc.imap_host, self.mc.imap_port, timeout=self.mc.timeout_seconds)
        else:
            self.imap = imaplib.IMAP4_SSL(self.mc.imap_host, self.mc.imap_port, timeout=self.mc.timeout_seconds)
        try:
            self.imap.login(self.mc.user, self.secret)
        except imaplib.IMAP4.error as exc:
            raise MailError("prihlásenie do Gmailu zlyhalo – over heslo aplikácie a zapnutý IMAP") from exc
        typ, _ = self.imap.select(self.all_mail(), readonly=True)
        if typ != "OK":
            raise MailError("schránku „Všetky správy“ sa nepodarilo otvoriť")
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.imap.logout()
        except Exception:  # noqa: BLE001
            pass

    def all_mail(self) -> str:
        """Schránka so všetkými správami (\\All – názov závisí od jazyka Gmailu)."""
        _, data = self.imap.list()
        for line in data or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\All" in text:
                return text.rsplit(' "/" ', 1)[-1].strip()
        return _quote("[Gmail]/All Mail")

    def _uids(self, query: str) -> set[int]:
        typ, data = self.imap.uid("SEARCH", "X-GM-RAW", _quote(query))
        if typ != "OK" or not data or not data[0]:
            return set()
        return {int(x) for x in data[0].split()}

    def search(self, addrs: list[str], days: int) -> list[int]:
        uids: set[int] = set()
        step = max(1, self.mc.batch_addresses)
        for i in range(0, len(addrs), step):
            chunk = addrs[i:i + step]
            uids |= self._uids("{" + " ".join(f"from:{a} to:{a} cc:{a}" for a in chunk) + f"}} newer_than:{days}d")
        uids |= self._uids(f"from:mailer-daemon newer_than:{days}d")
        return sorted(uids)

    def headers(self, uids: list[int]) -> list[Header]:
        out: list[Header] = []
        for i in range(0, len(uids), 100):
            chunk = ",".join(str(u) for u in uids[i:i + 100])
            typ, data = self.imap.uid("FETCH", chunk, f"(X-GM-THRID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
            if typ != "OK":
                continue
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                meta = item[0].decode("ascii", "replace")
                uid = re.search(r"UID (\d+)", meta)
                thr = re.search(r"X-GM-THRID (\d+)", meta)
                if uid:
                    out.append(parse_header(int(uid.group(1)), thr.group(1) if thr else None, item[1]))
        return out

    def body(self, message_id: str) -> str:
        typ, data = self.imap.uid("SEARCH", "HEADER", "Message-ID", _quote(message_id))
        if typ != "OK" or not data or not data[0]:
            raise MailError("správa sa v schránke nenašla (zmazaná alebo presunutá)")
        uid = data[0].split()[-1].decode()
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        raw = next((item[1] for item in data or [] if isinstance(item, tuple)), b"")
        return plain_text(raw)


# ======================================================================= kontrola schránky (bez modelu)
def _pick(rows: list, subject: str) -> Any:
    """Oslovenie podľa predmetu (rovnaký kontakt môže mať oslovenia k viacerým hypotézam)."""
    return max(rows, key=lambda r: (fuzz.token_set_ratio(fold(subject).lower(), fold(r["subject"] or "").lower()), r["id"]))


def sync(cfg: Config, db: DB, client) -> dict[str, Any]:
    """Odoslané, odpovede, automatické odpovede a nedoručené pre adresy z oslovení. Iné správy sa nezapíšu."""
    from .outreach import mark_follow_up_sent, mark_sent, set_status

    me = cfg.mail.user.lower()
    rows = [r for r in db.outreach_list() if r["contact_kind"] == "email" and r["contact_value"] and r["status"] != "blocked"]
    by_addr: dict[str, list] = defaultdict(list)
    for r in rows:
        by_addr[r["contact_value"].lower()].append(r)
    if not by_addr:
        return {"preskočené": "žiadne e-mailové oslovenia"}
    known = db.mail_known_ids()
    headers = sorted((h for h in client.headers(client.search(sorted(by_addr), cfg.mail.lookback_days))
                      if h.message_id and h.message_id not in known), key=lambda h: h.sent_at)
    threads = db.mail_thread_outreach()
    stats: Counter = Counter()

    def current(oid: int):
        return db.get_outreach(oid)

    def record(h: Header, row, direction: str, kind: str, status: str = "recorded") -> None:
        db.add_mail_message(h.message_id, thread_id=h.thread_id, outreach_id=row["id"], person_id=row["person_id"],
                            direction=direction, kind=kind, sent_at=h.sent_at, subject=mask_text(h.subject)[:200],
                            status=status)
        if direction == "out" and h.thread_id:
            threads.setdefault(h.thread_id, row["id"])

    for h in headers:
        if h.from_addr == me:  # tvoja správa
            for addr in [a for a in h.to if a in by_addr]:
                row = current(threads[h.thread_id]) if h.thread_id in threads else None  # ďalšia správa vo vlákne
                candidates = [current(r["id"]) for r in by_addr[addr]]
                if row is None:
                    fresh = [r for r in candidates if r["status"] in OPEN_FOR_SEND]
                    row = _pick(fresh, h.subject) if fresh else (_pick(candidates, h.subject) if candidates else None)
                if row is None:
                    continue
                if row["status"] in OPEN_FOR_SEND:
                    mark_sent(cfg, db, row["id"], datetime.fromisoformat(h.sent_at).date(), detected=True)
                    record(h, row, "out", "sent")
                    stats["odoslané"] += 1
                else:
                    if row["follow_up_due"]:
                        mark_follow_up_sent(cfg, db, row["id"])
                    record(h, row, "out", "follow_up")
                    stats["follow_up"] += 1
                break  # jedna správa = jeden záznam (Message-ID je jedinečné)
        elif h.failed or BOUNCE_FROM_RE.search(h.from_addr):  # nedoručené
            hit = [r for a in h.failed if a in by_addr for r in by_addr[a]]
            sent = [current(r["id"]) for r in hit]
            sent = [r for r in sent if r["status"] in AFTER_SEND]
            if sent:
                row = max(sent, key=lambda r: (r["sent_at"] or "", r["id"]))
                set_status(cfg, db, row["id"], "bounced")
                record(h, row, "in", "bounce")
                stats["nedoručené"] += 1
        else:  # odpoveď (vlákno alebo adresa kontaktu)
            row = current(threads[h.thread_id]) if h.thread_id in threads else None
            if row is None and h.from_addr in by_addr:
                sent = [current(r["id"]) for r in by_addr[h.from_addr]]
                sent = [r for r in sent if r["status"] in AFTER_SEND]
                row = max(sent, key=lambda r: (r["sent_at"] or "", r["id"])) if sent else None
            if row is None:
                continue  # nesúvisí s oslovením – nezapíše sa nič
            if h.auto:
                record(h, row, "in", "auto_reply")
                stats["automatické_odpovede"] += 1
                continue
            if row["status"] in ("sent", "no_reply"):
                set_status(cfg, db, row["id"], "replied")
            record(h, row, "in", "reply", status="new")
            stats["odpovede"] += 1
    db.set_setting(SYNC_KEY, {"kedy": utcnow(), **{k: int(v) for k, v in stats.items()}})
    return dict(stats)


# ======================================================================= rozbor odpovede (lokálny model → návrh)
class MailReply(BaseModel):
    reply: Literal["relevant", "redirect", "not_relevant", "declined"]
    problem_relevance: Literal["confirmed", "refuted", "unclear"] = "unclear"
    problem_quote: str = ""
    wtp_stage: WTP = "not_asked"
    wtp_quote: str = ""
    wtp_conditions: str = ""
    meeting: Literal["none", "proposed", "agreed"] = "none"
    meeting_text: str = ""
    last_case: str = ""
    tool_used: str = ""
    buying_process: str = ""
    unsolved_part: str = ""
    next_action: str = ""
    redirect_role: str = ""


def _norm(text: str) -> str:
    return " ".join(fold(text or "").lower().split())


def _in_text(quote: str, text: str) -> bool:
    q = _norm(quote).strip(" .„“\"'")
    return len(q) >= 8 and q in _norm(text)


def verify(v: MailReply, text: str) -> dict[str, Any]:
    """Návrh z výstupu modelu: citácie musia byť doslova v texte odpovede (kód), inak sa tvrdenie oslabí."""
    p = v.model_dump()
    checks = []
    if p["problem_relevance"] in ("confirmed", "refuted") and not _in_text(p["problem_quote"], text):
        checks.append("citácia o probléme sa v odpovedi nenašla → nejasné")
        p["problem_relevance"], p["problem_quote"] = "unclear", ""
    if p["wtp_stage"] not in ("not_asked", "unclear"):
        if not _in_text(p["wtp_quote"], text):
            checks.append("citácia o ochote platiť sa v odpovedi nenašla → nejasné")
            p["wtp_stage"], p["wtp_quote"] = "unclear", ""
        elif p["wtp_stage"] == "purchase_intent" and len(" ".join(p["wtp_conditions"].split())) < 10:
            checks.append("záujem o kúpu bez podmienok (čo, rozsah, cena, termín) → nejasné")
            p["wtp_stage"] = "unclear"
    for key in ("problem_quote", "wtp_quote", "wtp_conditions", "meeting_text", "last_case", "tool_used", "buying_process",
                "unsolved_part", "next_action", "redirect_role"):
        p[key] = mask_text(" ".join(str(p[key] or "").split()))[:300]
    p["kontroly"] = checks
    return p


def extract(cfg: Config, db: DB, client, router, deadline: Deadline) -> dict[str, Any]:
    """Nové odpovede → návrhy (lokálny model; osobné údaje – cloud iba s privacy.cloud_personal_data)."""
    from .prompts import MAIL_REPLY_SYSTEM

    stats: Counter = Counter()
    for m in db.mail_messages(status="new", kind="reply", limit=cfg.mail.max_extract_per_run):
        if deadline.expired():
            stats["nedokončené"] += 1
            continue
        row = db.get_outreach(m["outreach_id"])
        hyp = db.get_hypothesis(row["hypothesis_id"]) if row is not None else None
        try:
            text = clean_body(client.body(m["message_id"]), cfg.mail.max_body_chars)
        except MailError as exc:
            db.update_mail_message(m["id"], status="failed", proposal_json={"chyba": str(exc)})
            stats["nenájdené"] += 1
            continue
        if not text:
            db.update_mail_message(m["id"], status="extracted", proposal_json={
                "reply": "relevant", "problem_relevance": "unclear", "wtp_stage": "not_asked", "meeting": "none",
                "kontroly": ["odpoveď bez textu (iba citácia pôvodnej správy)"]})
            stats["bez_textu"] += 1
            continue
        res = router.run_task(
            "mail_reply",
            [{"role": "system", "content": MAIL_REPLY_SYSTEM.format(lang=cfg.pipeline.output_language)},
             {"role": "user", "content": f"OUR MESSAGE SUBJECT: {row['subject'] if row is not None else ''}\n"
                                         f"PROBLEM WE ASKED ABOUT: {hyp['title'] if hyp is not None else ''}\n\n"
                                         f"<untrusted_document>\nREPLY:\n{text}\n</untrusted_document>"}],
            MailReply, object_type="outreach", object_id=m["outreach_id"], personal=True, max_tokens=700)
        if not res.ok:
            stats["neurčené"] += 1  # ostane „new“ – skúsi sa v ďalšej úlohe (invariant 5)
            continue
        db.update_mail_message(m["id"], status="extracted", proposal_json=verify(res.value, text))
        stats["návrhy"] += 1
    return dict(stats)


# ======================================================================= potvrdenie človekom
def pending_proposals(db: DB) -> list[dict[str, Any]]:
    """Návrhy z odpovedí na potvrdenie (s menom kontaktu a organizáciou pre GUI)."""
    rows = {r["id"]: r for r in db.outreach_list()}
    out = []
    for m in db.mail_messages(status="extracted", kind="reply"):
        o = rows.get(m["outreach_id"])
        out.append({"id": m["id"], "outreach_id": m["outreach_id"], "hypothesis_id": o["hypothesis_id"] if o else None,
                    "kontakt": (o["full_name"] if o and o["full_name"] else "funkčný kontakt"),
                    "organizácia": o["org_name"] if o else "", "kedy": (m["sent_at"] or "")[:16].replace("T", " "),
                    "predmet": m["subject"] or "", "návrh": json.loads(m["proposal_json"] or "{}")})
    return out


def apply_proposal(cfg: Config, db: DB, mail_id: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Potvrdený návrh → výsledok oslovenia (record_outcome, autor človek) vrátane ochoty platiť s citáciou."""
    from .outcomes import record_outcome

    m = db.get_mail_message(mail_id)
    if m is None or m["status"] != "extracted":
        raise MailError("Návrh už nie je na potvrdenie.")
    p = {**json.loads(m["proposal_json"] or "{}"), **(overrides or {})}
    day = (m["sent_at"] or utcnow())[:10]
    stage = p.get("wtp_stage") or "not_asked"
    willingness: dict[str, Any] = {"stage": stage}
    if stage not in ("not_asked", "unclear"):
        willingness.update(evidence=f"e-mail {day}: „{p.get('wtp_quote') or ''}“", conditions=p.get("wtp_conditions") or "")
    structured = {k: p[k] for k in ("last_case", "tool_used", "buying_process", "unsolved_part") if p.get(k)}
    note = f"Z e-mailu {day}" + (f": „{p['problem_quote']}“" if p.get("problem_quote") else "")
    next_action = p.get("next_action") or (f"termín: {p['meeting_text']}" if p.get("meeting_text") else "")
    relevance = p.get("problem_relevance")
    res = record_outcome(cfg, db, outreach_id=m["outreach_id"], reply=p.get("reply") or "relevant",
                         problem_relevance=None if relevance in (None, "unclear") else relevance,
                         next_action=next_action, note=note, structured=structured, willingness=willingness,
                         interview_agreed=p.get("meeting") == "agreed")
    db.update_mail_message(mail_id, status="applied", outcome_id=res["id"])
    return res


def dismiss(db: DB, mail_id: int) -> None:
    db.update_mail_message(mail_id, status="dismissed")
