"""Obstarávanie z TED (M37, 26. 10. 2026): štruktúrované oznámenia o verejnom obstarávaní namiesto vyhľadávania
stránok `site:ted.europa.eu`.

- Zdroj: TED Search API v3 (verejné, bez kľúča), oznámenia kupujúcich zo SK so zvolenými CPV (config `procurement`).
  Ukladajú sa iba údaje o zákazke a organizáciách: kupujúci (názov, IČO, typ), predmet, CPV, dátumy, hodnoty, víťaz
  (IČO; názov iba pri právnickej osobe – živnostník je fyzická osoba). Kontaktné osoby, e-maily, telefóny ani adresy
  sa nesťahujú; e-mail alebo telefón v texte opisu sa zamaskuje.
- Priradenie k hypotéze: podobnosť embeddingov (oznámenie ↔ názov hypotézy a navrhnutý produkt). Prah aj podobnosť
  sa vždy zobrazia. Model nepíše text, čísla počíta Python.
- Signály dopytu (kód): opakovaný nákup u kupujúceho, počet nezávislých kupujúcich, otvorené a plánované zákazky,
  služba vs. softvér podľa CPV, hodnoty v EUR, menší kupujúci s podobnou potrebou.
- Stav obstarávania organizácie (Brána 0) z konkrétneho záznamu podľa IČO kupujúceho: otvorená výzva → potvrdené
  (komunikácia iba oficiálnou cestou), predbežné oznámenie → kandidát, ukončené → ukončené. Rozhodnutie človeka sa
  neprepíše; záznam TED nahradí odhad z vyhľadávania iba pri otvorenej výzve alebo keď stav nebol známy.
- TED obsahuje iba zákazky nad limitmi EÚ (a dobrovoľne zverejnené) – chýbajúci záznam neznamená, že nič neprebieha.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, Callable

import httpx
import numpy as np

from .config import Config, ProcurementConfig
from .db import DB
from .privacy import fold, mask_text
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

FIELDS = ["publication-number", "notice-type", "procedure-type", "contract-nature", "title-proc", "description-proc",
          "description-lot", "buyer-name", "buyer-identifier", "buyer-legal-type", "buyer-country", "classification-cpv",
          "main-classification-proc", "publication-date", "deadline-receipt-tender-date-lot", "estimated-value-proc",
          "estimated-value-cur-proc", "estimated-value-lot", "total-value", "total-value-cur", "winner-name",
          "winner-identifier", "winner-size"]
LANGS = ("slk", "eng")
SYNC_KEY = "procurement_sync"
EMB_KIND = "procurement"
QUERY_KIND = "procurement_query"
STAGE_LABEL = {"planned": "plánované (predbežné oznámenie)", "call": "výzva", "awarded": "zadaná",
               "modified": "zmena zmluvy", "other": "iné"}
CPV_FAMILY = {"72": "IT služby", "48": "softvér", "79": "podnikové služby (outsourcing práce)"}
STATUS_RANK = {"confirmed": 3, "candidate": 2, "closed": 1, "none": 0}
ICO_RE = re.compile(r"^\d{6,8}$")
TED_NOTE_RE = re.compile(r"\s*\[TED [^\]]*\]")
# Právna forma alebo verejná inštitúcia v názve víťaza – inak ide pravdepodobne o fyzickú osobu (názov sa neuloží).
LEGAL_ENTITY_RE = re.compile(
    r"(?<![a-z])(s\.? ?r\.? ?o\.?|spol\. ?s ?r\.? ?o\.?|a\.? ?s\.?|k\.? ?s\.?|v\.? ?o\.? ?s\.?|s\.? ?p\.?|n\.? ?o\.?|"
    r"o\.? ?z\.?|j\.? ?s\.? ?a\.?|z\.? ?z\.? ?p\.? ?o\.?|druzstvo|akciova spolocnost|spolocnost s rucenim|gmbh|ltd|"
    r"limited|inc|ag|se|s\.a\.|sp\. z o\.o\.|kft|b\.v\.|n\.v\.|oy|ab|srl|spa|univerzita|ustav|urad|ministerstvo|mesto|"
    r"obec|kraj|agentura|institut|centrum|fond|poistovna|nemocnica|konzorcium|zdruzenie)(?![a-z])")


# ======================================================================= prevod oznámenia
def pick_text(value: Any) -> str:
    """Text z viacjazyčného poľa TED: slovenčina, inak angličtina, inak prvý jazyk; zoznam → jedinečné časti."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for lang in LANGS:
            if value.get(lang):
                return pick_text(value[lang])
        return pick_text(next(iter(value.values()), ""))
    if isinstance(value, list):
        return " · ".join(dict.fromkeys(t for t in (pick_text(v) for v in value) if t))
    return str(value).strip()


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def number(value: Any) -> float | None:
    """Prvé číslo z hodnoty alebo zoznamu (TED vracia sumy ako text alebo číslo)."""
    if isinstance(value, list):
        for v in value:
            n = number(v)
            if n is not None:
                return n
        return None
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def day(value: Any) -> str | None:
    """„2025-09-02+02:00“ → „2025-09-02“ (zo zoznamu najneskorší dátum – lehota poslednej časti zákazky)."""
    if isinstance(value, list):
        days = [d for d in (day(v) for v in value) if d]
        return max(days) if days else None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return m.group(1) if m else None


def icos(value: Any) -> list[str]:
    """IČO zo zoznamu identifikátorov (DIČ a IČ DPH majú 10 číslic alebo predponu SK – vynechajú sa)."""
    out = []
    for v in value if isinstance(value, list) else [value]:
        s = re.sub(r"\s", "", str(v or ""))
        if ICO_RE.match(s):
            out.append(s.zfill(8))
    return list(dict.fromkeys(out))


def legal_entity(name: str) -> bool:
    return bool(LEGAL_ENTITY_RE.search(fold(name or "").lower()))


def stage_of(notice_type: str | None) -> str:
    """Fáza zákazky z typu oznámenia eForms (pin-cfc je výzva na súťaž, ostatné pin sú plánované obstarávanie)."""
    t = (notice_type or "").lower()
    if t.startswith("pin-cfc"):
        return "call"
    if t.startswith("pin"):
        return "planned"
    if t.startswith(("cn", "qu-sy", "subco")):
        return "call"
    if t == "can-modif":
        return "modified"
    if t.startswith("can") or t == "veat":
        return "awarded"
    return "other"


def notice_url(publication_number: str) -> str:
    return f"https://ted.europa.eu/sk/notice/-/detail/{publication_number}"


def parse_notice(raw: dict[str, Any], pc: ProcurementConfig) -> dict[str, Any]:
    """Riadok tabuľky procurement_notices z odpovede API (bez osôb a kontaktov; text opisu maskovaný)."""
    pub = str(raw.get("publication-number") or "").strip()
    stage = stage_of(raw.get("notice-type"))
    cpv = sorted({str(c) for c in raw.get("classification-cpv") or []})
    estimated = number(raw.get("estimated-value-proc"))
    if estimated is None:
        lots = raw.get("estimated-value-lot")
        values = [n for n in (number(v) for v in (lots if isinstance(lots, list) else [lots])) if n is not None]
        estimated = sum(values) if values else None
    names = [n for n in (pick_text(x) for x in _names(raw.get("winner-name"))) if n]
    winners = {"names": [n for n in dict.fromkeys(names) if legal_entity(n)],
               "ico": icos(raw.get("winner-identifier")),
               "size": list(dict.fromkeys(str(s) for s in (raw.get("winner-size") or []) if s))}
    awarded = stage in ("awarded", "modified")
    clean = {k: v for k, v in raw.items() if k != "links"}
    return {
        "source": "ted", "notice_id": pub, "notice_type": raw.get("notice-type"),
        "procedure_type": pick_text(raw.get("procedure-type")) or None,
        "contract_nature": _first(raw.get("contract-nature")), "stage": stage,
        "title": mask_text(pick_text(raw.get("title-proc")))[:500] or None,
        "description": mask_text(pick_text(raw.get("description-proc")) or pick_text(raw.get("description-lot")))[:1500]
        or None,
        "buyer_name": pick_text(raw.get("buyer-name"))[:300] or None,
        "buyer_ico": (icos(raw.get("buyer-identifier")) or [None])[0],
        "buyer_legal_type": _first(raw.get("buyer-legal-type")),
        "buyer_country": _first(raw.get("buyer-country")) or pc.buyer_country,
        "cpv_json": cpv, "main_cpv": _first(raw.get("main-classification-proc")) or (cpv[0] if cpv else None),
        "publication_date": day(raw.get("publication-date")), "deadline": day(raw.get("deadline-receipt-tender-date-lot")),
        "estimated_value": estimated, "estimated_currency": _first(raw.get("estimated-value-cur-proc")),
        "awarded_value": number(raw.get("total-value")) if awarded else None,
        "awarded_currency": _first(raw.get("total-value-cur")) if awarded else None,
        "winners_json": winners if any(winners.values()) else None,
        "url": notice_url(pub),
        "raw_hash": hashlib.sha1(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def _names(value: Any) -> list[Any]:
    """Názvy víťazov: {jazyk: [názov, …]} → zoznam názvov v jednom jazyku."""
    if isinstance(value, dict):
        for lang in LANGS:
            if value.get(lang):
                v = value[lang]
                return v if isinstance(v, list) else [v]
        first = next(iter(value.values()), [])
        return first if isinstance(first, list) else [first]
    return value if isinstance(value, list) else ([value] if value else [])


# ======================================================================= API
class TedClient:
    """TED Search API v3 (POST /v3/notices/search, režim ITERATION – bez limitu 15 000 záznamov)."""

    def __init__(self, pc: ProcurementConfig, post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
                 sleep: Callable[[float], None] = time.sleep, user_agent: str = "ProblemRadar/0.2"):
        self.pc, self.sleep, self.user_agent = pc, sleep, user_agent
        self._post = post or self._http_post
        self._client: httpx.Client | None = None

    def _http_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            transport = httpx.HTTPTransport(local_address="0.0.0.0") if self.pc.ipv4_only else httpx.HTTPTransport()
            self._client = httpx.Client(transport=transport, timeout=self.pc.timeout_seconds,
                                        headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        resp = self._client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, token: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "fields": FIELDS, "limit": self.pc.page_size, "paginationMode": "ITERATION"}
        if token:
            body["iterationNextToken"] = token
        for attempt in range(3):
            try:
                return self._post(self.pc.api_url, body)
            except httpx.HTTPStatusError as exc:
                if attempt < 2 and (exc.response.status_code == 429 or exc.response.status_code >= 500):
                    self.sleep(5.0 * (attempt + 1))
                    continue
                raise
            except httpx.TransportError:
                if attempt < 2:
                    self.sleep(5.0 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("nedosiahnuteľné")  # pragma: no cover


def build_query(pc: ProcurementConfig, since: date) -> str:
    return (f"buyer-country={pc.buyer_country} AND classification-cpv IN ({' '.join(pc.cpv)}) "
            f"AND publication-date>={since:%Y%m%d}")


def sync_notices(cfg: Config, db: DB, client: TedClient, deadline: Deadline, today: date | None = None) -> dict[str, Any]:
    """Nové a zmenené oznámenia od posledného behu (prvý beh: posledné `years_back` roky). Prerušený beh posun
    nezapíše – ďalší začne od rovnakého dátumu (zápis je idempotentný podľa čísla publikácie a hashu)."""
    pc = cfg.procurement
    today = today or date.today()
    state = db.get_setting(SYNC_KEY, {}) or {}
    since = (date.fromisoformat(state["do"]) - timedelta(days=pc.overlap_days) if state.get("do")
             else today - timedelta(days=365 * pc.years_back))
    query = build_query(pc, since)
    stats: Counter = Counter()
    token, newest, complete = None, state.get("do"), False
    while stats["strany"] < pc.max_pages_per_run and not deadline.expired():
        try:
            data = client.search(query, token)
        except Exception as exc:  # noqa: BLE001 – API nedostupné: beh pokračuje ďalšími krokmi (invariant 5)
            log.warning("TED nedostupné: %s", exc)
            stats["chyba"] = str(exc)[:200]
            break
        notices = [n for n in data.get("notices") or [] if n.get("publication-number")]
        rows = [parse_notice(n, pc) for n in notices]
        stats.update(db.upsert_procurement_notices(rows))
        stats["strany"] += 1
        for r in rows:
            if r["publication_date"] and (newest is None or r["publication_date"] > newest):
                newest = r["publication_date"]
        token = data.get("iterationNextToken")
        if not notices or not token:
            complete = True
            break
        client.sleep(pc.pause_seconds)
    out: dict[str, Any] = dict(stats)
    out["od"] = since.isoformat()
    if complete:
        db.set_setting(SYNC_KEY, {"do": newest or today.isoformat(), "kedy": utcnow(), "dopyt": query})
    elif "chyba" not in out:
        out["nedokončené"] = "ďalší beh pokračuje od rovnakého dátumu"
    out["celkom"] = db.procurement_stats()["oznámenia"]
    return out


# ======================================================================= priradenie k hypotézam
def notice_text(n) -> str:
    """Text na embedding: predmet a začiatok opisu (krátky – embeddingy bežia na CPU)."""
    return f"{n['title'] or ''}. {(n['description'] or '')[:300]}".strip()


def query_text(db: DB, h) -> str:
    """Dopyt hypotézy: problém, segment a navrhnutý produkt (výstup z problémovej karty, výstup z návrhu prínosu) –
    oznámenie opisuje, čo kupujúci kupuje, preto sa porovnáva aj s produktom, nielen s problémom."""
    parts = [h["title"] or "", h["segment"] or ""]
    card = db.latest_card(h["id"])
    if card is not None:
        parts.append((json.loads(card["card_json"] or "{}") or {}).get("helpful_output_text") or "")
    for p in db.value_proposals_for(h["id"]):
        if p["status"] in ("checked", "approved", "pending_review"):
            parts.append((json.loads(p["proposal_json"] or "{}") or {}).get("deliverable") or "")
            break
    return ". ".join(t.strip() for t in parts if t and t.strip())


def scope_hypotheses(cfg: Config, db: DB, ids: list[int] | None = None) -> list:
    """Hypotézy na porovnanie: zadané, inak vybrané na Bránu 0 a tie, čo prešli testom."""
    if ids:
        return [h for h in (db.get_hypothesis(i) for i in ids) if h is not None and h["status"] != "rejected"]
    seen, out = set(), []
    for h in list(db.people_enabled_hypotheses()) + list(db.validated_hypotheses(tuple(cfg.people.auto_select_verdicts))):
        if h["id"] not in seen and h["status"] != "rejected":
            seen.add(h["id"])
            out.append(h)
    return out


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def match_hypotheses(cfg: Config, db: DB, embedder, deadline: Deadline, ids: list[int] | None = None,
                     today: date | None = None) -> dict[str, Any]:
    """Vektory nových oznámení (cache podľa hashu textu), potom pre každú hypotézu najpodobnejšie oznámenia nad
    prahom a stav obstarávania jej organizácií. Hypotéza s nezmeneným dopytom a rovnakým počtom oznámení sa preskočí."""
    pc = cfg.procurement
    if embedder is None:
        return {"preskočené": "embeddingy vypnuté (embeddings.enabled)"}
    notices = db.procurement_notices()
    if not notices:
        return {"preskočené": "žiadne oznámenia – najprv krok procurement-sync"}
    stats: Counter = Counter()
    items = [(n["id"], notice_text(n)) for n in notices]
    have = {(e["ref_id"], e["text_hash"]) for e in db.embeddings_of_kind(EMB_KIND, embedder.model)}
    todo = [it for it in items if (it[0], _sha1(it[1])) not in have]
    for start in range(0, len(todo), 64):
        if deadline.expired():
            stats["vektory_nedokončené"] = len(todo) - start
            break
        try:
            embedder.vectors(EMB_KIND, todo[start:start + 64])
        except Exception as exc:  # noqa: BLE001 – embedding model nebeží: porovnanie počká na ďalší beh
            stats["chyba"] = str(exc)[:200]
            return dict(stats)
        stats["nové_vektory"] += len(todo[start:start + 64])
    vecs = {e["ref_id"]: (e["text_hash"], e["vector"]) for e in db.embeddings_of_kind(EMB_KIND, embedder.model)}
    keep, mats = [], []
    for nid, text in items:
        hit = vecs.get(nid)
        if hit and hit[0] == _sha1(text):
            keep.append(nid)
            mats.append(np.frombuffer(hit[1], dtype=np.float32))
    if not keep:
        return dict(stats)
    matrix = np.vstack(mats)
    done = db.procurement_matched_state()
    for h in scope_hypotheses(cfg, db, ids):
        if deadline.expired():
            stats["hypotézy_nedokončené"] += 1
            continue
        text = query_text(db, h)
        qhash = _sha1(f"{text}|{len(keep)}|{pc.match_threshold}|{pc.max_matches}")
        if (done.get(str(h["id"])) or {}).get("hash") == qhash:
            stats["bez_zmeny"] += 1
            continue
        q = embedder.vectors(QUERY_KIND, [(h["id"], text)])[0]
        sims = matrix @ q
        order = np.argsort(-sims)[: pc.max_matches]
        matches = [(keep[i], round(float(sims[i]), 4)) for i in order if sims[i] >= pc.match_threshold]
        db.set_procurement_matches(h["id"], matches, qhash)
        stats["hypotézy"] += 1
        stats["zhody"] += len(matches)
        stats.update(update_org_status(cfg, db, h["id"], today))
    return dict(stats)


# ======================================================================= stav obstarávania organizácie
def record_status(n, pc: ProcurementConfig, today: date) -> tuple[str, str]:
    """(stav pre Bránu 0, dôvod) z jedného oznámenia."""
    pub = n["publication_date"] or ""
    if n["stage"] == "call":
        end = n["deadline"] or ((date.fromisoformat(pub) + timedelta(days=pc.open_days_without_deadline)).isoformat()
                                if pub else None)
        if end and end >= today.isoformat():
            return "confirmed", (f"otvorená výzva TED {n['notice_id']}, lehota {end}" if n["deadline"] else
                                 f"výzva TED {n['notice_id']} z {pub} bez lehoty v dátach (berie sa ako otvorená "
                                 f"{pc.open_days_without_deadline} dní)")
        return "closed", f"výzva TED {n['notice_id']} – lehota {end or '?'} uplynula"
    if n["stage"] == "planned":
        return "candidate", f"predbežné oznámenie TED {n['notice_id']} z {pub} – plánované obstarávanie"
    if n["stage"] in ("awarded", "modified"):
        return "closed", f"zadaná zákazka TED {n['notice_id']} z {pub}"
    return "none", ""


def update_org_status(cfg: Config, db: DB, hid: int, today: date | None = None) -> Counter:
    """Stav obstarávania organizácií hypotézy z podobných oznámení, kde je organizácia kupujúci (zhoda IČO)."""
    pc = cfg.procurement
    today = today or date.today()
    stats: Counter = Counter()
    by_ico: dict[str, list] = defaultdict(list)
    for n in db.procurement_matches_for(hid):
        if n["buyer_ico"]:
            by_ico[n["buyer_ico"]].append(n)
    if not by_ico:
        return stats
    for org in db.orgs_for_hypothesis(hid, include_excluded=True):
        notices = by_ico.get(str(org["ico"]).zfill(8)) if org["ico"] else None
        if not notices:
            continue
        (status, reason), n = max(((record_status(x, pc, today), x) for x in notices),
                                  key=lambda t: (STATUS_RANK[t[0][0]], t[1]["publication_date"] or ""))
        if status == "none":
            continue
        oh = db.org_hypothesis(org["id"], hid)
        note = (oh["procurement_note"] or "") if oh is not None else ""
        current = ((oh["procurement_status"] if oh is not None else None)
                   or ("candidate" if oh is not None and oh["procurement_active"] else "none"))
        if "človek:" in note:  # rozhodnutie človeka (GUI) sa neprepíše
            stats["obstarávanie_overil_človek"] += 1
            continue
        from_ted = "[TED " in note
        if not (from_ted or current == "none" or status == "confirmed"):
            stats["obstarávanie_ponechaný_odhad"] += 1  # odhad z vyhľadávania môže byť zákazka pod limitmi EÚ
            continue
        base = TED_NOTE_RE.sub("", note).strip()
        db.set_org_hypothesis(org["id"], hid, procurement_status=status, procurement_active=int(status == "confirmed"),
                              procurement_subject=(n["title"] or "")[:200] or None,
                              procurement_date=n["deadline"] or n["publication_date"], procurement_url=n["url"],
                              procurement_checked_at=utcnow(),
                              procurement_note=f"{base} [TED {today.isoformat()}: {reason}]".strip())
        stats[f"obstarávanie_{status}"] += 1
    return stats


# ======================================================================= signály dopytu
def cpv_family(cpv: str | None) -> str:
    return CPV_FAMILY.get(str(cpv or "")[:2], "iné")


def _eur(value: float | None, currency: str | None) -> float | None:
    return value if value is not None and (currency or "EUR").upper() == "EUR" else None


def signals(cfg: Config, db: DB, hid: int, today: date | None = None) -> dict[str, Any]:
    """Signály dopytu z podobných oznámení (bez modelu). Hodnoty iba v EUR; chýbajúca hodnota sa nedopočítava."""
    pc = cfg.procurement
    today = today or date.today()
    rows = db.procurement_matches_for(hid)
    state = db.procurement_matched_state().get(str(hid))
    if not rows:
        return {"zákazky": 0, "porovnané": bool(state), "prah": pc.match_threshold}
    buyers: dict[str, list] = defaultdict(list)
    for n in rows:
        buyers[n["buyer_ico"] or fold(n["buyer_name"] or "?").lower()].append(n)
    value_of = {}
    for key, ns in buyers.items():
        vals = [v for v in (_eur(x["awarded_value"], x["awarded_currency"]) or _eur(x["estimated_value"], x["estimated_currency"])
                            for x in ns) if v]
        value_of[key] = sum(vals) if vals else None
    known = [v for v in value_of.values() if v]
    median_buyer = statistics.median(known) if known else None
    estimated = [v for v in (_eur(n["estimated_value"], n["estimated_currency"]) for n in rows) if v]
    awarded = [v for v in (_eur(n["awarded_value"], n["awarded_currency"]) for n in rows) if v]
    families = Counter(cpv_family(n["main_cpv"]) for n in rows)

    def brief(n) -> dict[str, Any]:
        return {"oznámenie": n["notice_id"], "predmet": n["title"], "kupujúci": n["buyer_name"], "ico": n["buyer_ico"],
                "fáza": STAGE_LABEL.get(n["stage"], n["stage"]), "zverejnené": n["publication_date"],
                "lehota": n["deadline"], "podobnosť": round(n["similarity"], 2), "url": n["url"]}

    return {
        "zákazky": len(rows), "porovnané": True, "prah": pc.match_threshold,
        "podobnosť": [round(min(n["similarity"] for n in rows), 2), round(max(n["similarity"] for n in rows), 2)],
        "kupujúci": len(buyers),
        "opakovaní_kupujúci": sorted(
            ({"kupujúci": ns[0]["buyer_name"], "ico": ns[0]["buyer_ico"], "zákazky": len(ns),
              "roky": sorted({(x["publication_date"] or "")[:4] for x in ns if x["publication_date"]})}
             for ns in buyers.values() if len(ns) >= 2), key=lambda r: -r["zákazky"]),
        "otvorené_teraz": [brief(n) for n in rows if record_status(n, pc, today)[0] == "confirmed"],
        "plánované": [brief(n) for n in rows if n["stage"] == "planned"],
        "podľa_cpv": dict(families),
        "hodnoty_eur": {"odhad_spolu": round(sum(estimated)) if estimated else None,
                        "odhad_medián": round(statistics.median(estimated)) if estimated else None,
                        "zadané_spolu": round(sum(awarded)) if awarded else None,
                        "s_hodnotou": sum(1 for n in rows if _eur(n["estimated_value"], n["estimated_currency"])
                                          or _eur(n["awarded_value"], n["awarded_currency"])),
                        "medián_na_kupujúceho": round(median_buyer) if median_buyer else None},
        "menší_kupujúci": sorted(({"kupujúci": ns[0]["buyer_name"], "ico": ns[0]["buyer_ico"],
                                   "hodnota_eur": round(value_of[k])}
                                  for k, ns in buyers.items() if value_of[k] and median_buyer and value_of[k] < median_buyer),
                                 key=lambda r: r["hodnota_eur"]),
        "zoznam": [brief(n) for n in rows],
    }


def _eur_text(v: float | None) -> str:
    return "–" if v is None else f"{v:,.0f} €".replace(",", " ")


def signals_lines(sig: dict[str, Any]) -> list[str]:
    """Signály ako odrážky (Prehľad príležitosti, CLI, GUI)."""
    if not sig.get("zákazky"):
        return ["- Obstarávanie (TED): " + ("žiadne podobné zákazky nad prahom podobnosti "
                                             f"{sig.get('prah')} (TED má iba zákazky nad limitmi EÚ)" if sig.get("porovnané")
                                             else "zatiaľ neporovnané – akcia „Obstarávanie z TED“")]
    fam = sig["podľa_cpv"]
    services = fam.get("IT služby", 0) + fam.get("podnikové služby (outsourcing práce)", 0)
    lines = [f"- Obstarávanie (TED): {sig['zákazky']} podobných zákaziek u {sig['kupujúci']} kupujúcich "
             f"(podobnosť {sig['podobnosť'][0]}–{sig['podobnosť'][1]}, prah {sig['prah']} – over, či ide naozaj o to isté)"]
    rep = sig["opakovaní_kupujúci"]
    lines.append("- Opakovaný nákup: " + (", ".join(f"{r['kupujúci']} ({r['zákazky']}×, {'/'.join(r['roky'])})"
                                                     for r in rep[:5]) if rep else "žiadny kupujúci viac ako raz"))
    lines.append(f"- Čo kupujú: " + ", ".join(f"{k} {v}" for k, v in sorted(fam.items(), key=lambda kv: -kv[1]))
                 + (" – skôr služby ako hotový softvér" if services > fam.get("softvér", 0) else ""))
    hv = sig["hodnoty_eur"]
    lines.append(f"- Hodnoty (EUR, iba zo záznamov): odhad spolu {_eur_text(hv['odhad_spolu'])}, medián zákazky "
                 f"{_eur_text(hv['odhad_medián'])}, zadané spolu {_eur_text(hv['zadané_spolu'])} "
                 f"(s hodnotou {hv['s_hodnotou']} z {sig['zákazky']})")
    if sig["otvorené_teraz"]:
        lines.append("- ⚠ Otvorené teraz (s kupujúcim komunikuj iba oficiálnou cestou): " + "; ".join(
            f"{o['kupujúci']} – {o['predmet']} (lehota {o['lehota'] or '?'}) {o['url']}" for o in sig["otvorené_teraz"][:3]))
    if sig["plánované"]:
        lines.append("- Plánované (predbežné oznámenia): " + "; ".join(
            f"{o['kupujúci']} – {o['predmet']} {o['url']}" for o in sig["plánované"][:3]))
    if sig["menší_kupujúci"]:
        lines.append("- Menší kupujúci s podobnou potrebou: " + ", ".join(
            f"{r['kupujúci']} ({_eur_text(r['hodnota_eur'])})" for r in sig["menší_kupujúci"][:5]))
    return lines
