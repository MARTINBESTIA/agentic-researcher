"""Adresát oslovenia (zadanie 17. 10. 2026, časť A): zhoda so segmentom, rola, stav potreby, používané riešenie
a stav obstarávania – každé posúdené zvlášť a s dôvodom (pravidlá kódu, nie model).

- NBS nie je zákazník pre spracovanie dopytov malej realitnej kancelárie len preto, že publikuje údaje o nehnuteľnostiach;
  ministerstvo nie je účtovná kancelária len pre účtovnú agendu. Nesúlad nevymaže kontakt: ak má organizácia dátovú
  alebo expertnú rolu, ostane v nej, inak sa označí „preveriť rolu“ a nejde do fronty prvého oslovenia zákazníkov.
- Všeobecný rozcestník prípravných trhových konzultácií je **kandidát** na preverenie; potvrdené aktívne súvisiace
  obstarávanie vyžaduje konkrétny záznam (predmet, dátum/lehotu, súvis s hypotézou). Neoverený stav neblokuje návrh,
  ale schválenie na odoslanie, kým ho človek neoverí (presný ďalší krok sa zobrazí).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from .db import DB
from .privacy import fold
from .util import utcnow

PROCUREMENT_STATUS = {"none": "žiadne známe", "candidate": "neoverené – kandidát na preverenie",
                      "confirmed": "potvrdené aktívne súvisiace obstarávanie", "closed": "ukončené / mimo lehoty"}
GENERIC_PROCUREMENT_PATH = re.compile(r"(pripravne|prípravné)[-_ ]?trhove[-_ ]?konzultacie[^/]*/?$|verejne[-_]obstaravanie/?$|"
                                      r"obstaravanie/?$|/zakazky/?$|/tendre/?$", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d{2})\b")
DEADLINE_RE = re.compile(r"lehot\w* (na predkladanie|na predloženie|do)|termín predloženia|do \d{1,2}\.\s?\d{1,2}\.\s?20\d{2}", re.I)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]{5,}", fold(text or "").lower())}


def assess_procurement(url: str, title: str, snippet: str, hypothesis_title: str, today: date | None = None) -> dict[str, Any]:
    """Stav obstarávania z výsledku vyhľadávania: confirmed iba pri konkrétnom zázname (nie rozcestník), s dátumom
    lehoty v budúcnosti a s predmetom súvisiacim s hypotézou; inak kandidát (alebo ukončené) s dôvodom."""
    today = today or date.today()
    path = urlparse(url or "").path
    text = f"{title or ''} {snippet or ''}"
    dates = []
    for d, m, y in DATE_RE.findall(text):
        try:
            dates.append(datetime(int(y), int(m), int(d)).date())
        except ValueError:
            continue
    overlap = _stems(text) & _stems(hypothesis_title)
    generic = bool(GENERIC_PROCUREMENT_PATH.search(path)) or len(path.strip("/")) == 0
    subject = (title or "").strip()[:200]
    out = {"url": url, "subject": subject or None, "date": max(dates).isoformat() if dates else None}
    if generic:
        return {**out, "status": "candidate", "reason": "všeobecná stránka obstarávania (rozcestník), nie konkrétny záznam",
                "next_step": f"otvor {url} a nájdi konkrétny záznam s predmetom súvisiacim s hypotézou a lehotou"}
    if dates and max(dates) < today:
        return {**out, "status": "closed", "reason": f"posledný dátum v zázname {max(dates).isoformat()} je v minulosti",
                "next_step": "nič – ak nejde o novšie kolo"}
    if dates and DEADLINE_RE.search(text) and len(overlap) >= 2:
        return {**out, "status": "confirmed", "reason": f"konkrétny záznam s lehotou {max(dates).isoformat()} a predmetom "
                                                        f"súvisiacim s hypotézou ({', '.join(sorted(overlap)[:3])})",
                "next_step": "komunikuj iba oficiálnou cestou (prípravné trhové konzultácie, vysvetľovanie súťažných podkladov)"}
    return {**out, "status": "candidate",
            "reason": "konkrétny záznam bez overenej lehoty alebo súvisu s hypotézou" if not generic else "rozcestník",
            "next_step": f"over predmet, lehotu a stav na {url}"}


def set_procurement(db: DB, org_id: int, hid: int, status: str, note: str = "") -> None:
    """Overenie obstarávania človekom (GUI): none = overené, nič súvisiace neprebieha; confirmed = potvrdené
    (návrhy sa blokujú, iba oficiálna cesta); candidate = vrátiť na preverenie. Zápis s časom a autorom."""
    if status not in PROCUREMENT_STATUS:
        raise ValueError(f"Neznámy stav obstarávania: {status}")
    current = db.org_hypothesis(org_id, hid)
    old = (current["procurement_note"] or "") if current is not None else ""
    stamp = f"[{utcnow()[:10]} človek: {PROCUREMENT_STATUS[status]}{' – ' + note if note else ''}]"
    db.set_org_hypothesis(org_id, hid, procurement_status=status, procurement_active=int(status == "confirmed"),
                          procurement_checked_at=utcnow(), procurement_note=f"{old} {stamp}".strip())
    if status != "confirmed":
        db.delete_procurement_placeholders(org_id, hid)


def procurement_of(org_row) -> dict[str, Any]:
    """Stav obstarávania organizácie pre hypotézu z riadku orgs_for_hypothesis / org_hypotheses (staré riadky = kandidát)."""
    keys = org_row.keys() if org_row is not None else []
    status = (org_row["procurement_status"] if "procurement_status" in keys else None) or (
        "candidate" if "procurement_active" in keys and org_row["procurement_active"] else "none")
    url = (org_row["procurement_url"] if "procurement_url" in keys else None) or ""
    note = (org_row["procurement_note"] if "procurement_note" in keys else None) or ""
    return {"status": status, "label": PROCUREMENT_STATUS.get(status, status), "url": url, "note": note,
            "subject": org_row["procurement_subject"] if "procurement_subject" in keys else None,
            "date": org_row["procurement_date"] if "procurement_date" in keys else None}


def segment_fit(db: DB, org, hyp) -> dict[str, Any]:
    """Zhoda organizácie so segmentom hypotézy pre rolu zákazníka: áno / nie / neznáme s dôvodom (bez modelu)."""
    from .outreach import is_public_org
    from .relevance import segment_kind

    seg = f"{hyp['segment'] or ''}" if hyp is not None else ""
    kind = segment_kind(f"{seg} {hyp['title'] if hyp is not None else ''}")
    public = is_public_org(org)
    sector = org["sector_kind"] if org is not None and "sector_kind" in org.keys() else None
    if kind == "private" and public:
        return {"zhoda": "nie", "dôvod": f"verejná inštitúcia nie je zákazník segmentu „{seg}“ (môže byť držiteľ dát alebo expert)"}
    if kind == "public" and sector in ("private", "nonprofit") and not public:
        return {"zhoda": "nie", "dôvod": f"súkromná organizácia nie je zákazník segmentu „{seg}“ (môže byť dodávateľ alebo expert)"}
    nace = (org["sk_nace"] if org is not None and "sk_nace" in org.keys() else None) or ""
    prefixes = []
    if hyp is not None and "segment_filter_json" in hyp.keys() and hyp["segment_filter_json"]:
        try:
            prefixes = json.loads(hyp["segment_filter_json"]).get("sk_nace_prefixes") or []
        except ValueError:
            prefixes = []
    if nace and prefixes:
        if any(nace.startswith(p) for p in prefixes):
            return {"zhoda": "áno", "dôvod": f"činnosť SK NACE {nace} zodpovedá segmentu"}
        # Filter SK NACE navrhol model pri hľadaní organizácií – nie je dosť spoľahlivý na vyradenie: test 17. 10. 2026
        # (H15 zdravotné poisťovne čakal 6520, poisťovne majú 8430) by vyradil skutočných zákazníkov. Iba upozornenie.
        return {"zhoda": "neznáme", "dôvod": f"SK NACE {nace} nie je medzi kódmi filtra segmentu ({', '.join(prefixes[:4])}; "
                                             "filter navrhol model) – over, či je to zákazník"}
    if kind == "public" and public:
        return {"zhoda": "áno", "dôvod": "verejná organizácia v segmente verejného sektora"
                                        + (" (podľa názvu alebo domény; sektor z registra chýba)" if sector is None else "")}
    if kind == "private" and sector == "private":
        return {"zhoda": "áno", "dôvod": "súkromná organizácia v segmente súkromného sektora"}
    if kind == "private" and not public:
        return {"zhoda": "neznáme", "dôvod": "pravdepodobne súkromná organizácia, sektor z registra chýba – over"}
    return {"zhoda": "neznáme", "dôvod": "segment alebo typ organizácie sa nedá určiť pravidlom – preveriť"}


NEED_LABEL = {"confirmed": "potvrdená kontaktom", "refuted": "vyvrátená kontaktom", "mixed": "rozporné odpovede",
              "solved_per_source": "podľa zdroja riešená (používa riešenie)", "hypothesis": "hypotéza – neoverená"}


def need_state(db: DB, hid: int, org_id: int | None, relation: str | None) -> str:
    """Stav potreby pre organizáciu: z jej vlastných odpovedí (posledná platí), inak zo zdroja; bez odpovede hypotéza.
    Neodpovedanie nie je vyvrátenie; odpoveď inej organizácie stav nemení."""
    if org_id:
        last = None
        for o in db.outcomes_for(hid):
            if o["org_id"] == org_id and o["problem_relevance"] in ("confirmed", "refuted"):
                last = o["problem_relevance"]
        if last:
            return last
    if relation == "uses_solution":
        return "solved_per_source"
    return "hypothesis"
