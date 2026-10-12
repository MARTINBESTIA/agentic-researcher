"""Klienti otvorených registrov: RPO (Register právnických osôb, ŠÚ SR) a RÚZ (Register účtovných závierok).

- RPO: licencia CC BY 4.0 – pri zobrazení a exporte uvádzaj zdroj (RPO_ATTRIBUTION).
  Zo štatutárov sa ukladá iba meno a funkcia. Adresa fyzickej osoby sa odstráni hneď po
  stiahnutí, ešte pred uložením do cache, a nikde sa neloguje (R-ORG-2).
- RÚZ: licencia CC0. Zoznamové endpointy nefiltrujú podľa SK NACE, preto sa register
  nestahuje celý, iba detaily konkrétnych IČO (R-ORG-3).
- Odpovede sa cachujú (people.registry_cache_days) a medzi volaniami je pauza (people.registry_delay_s).
- ORSR a komerčné agregátory bez API sa nesťahujú (R-ORG-8).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .db import DB

log = logging.getLogger(__name__)

RPO_BASE = "https://api.statistics.sk/rpo/v1"
RUZ_BASE = "https://www.registeruz.sk/cruz-public"
RPO_ATTRIBUTION = "Zdroj: Register právnických osôb, Štatistický úrad SR (CC BY 4.0)"
RUZ_ATTRIBUTION = "Zdroj: Register účtovných závierok (CC0)"

REGIONS = {
    "SK010": "Bratislavský kraj", "SK021": "Trnavský kraj", "SK022": "Trenčiansky kraj", "SK023": "Nitriansky kraj",
    "SK031": "Žilinský kraj", "SK032": "Banskobystrický kraj", "SK041": "Prešovský kraj", "SK042": "Košický kraj",
}
# RÚZ: veľkosť organizácie podľa počtu zamestnancov -> kategória
_SIZE = {
    **{c: "micro" for c in ("01", "02", "03", "04", "05")},
    **{c: "small" for c in ("06", "07", "11")},
    **{c: "medium" for c in ("12", "21", "22", "23")},
}
# RÚZ: druh vlastníctva -> typ sektora
_OWNERSHIP = {"1": "public", "4": "public", "5": "public", "2": "private", "3": "private", "7": "private", "8": "private",
              "9": "private", "6": "nonprofit"}


def size_category(code: str | None) -> str | None:
    if not code or code == "00":
        return None
    return _SIZE.get(code, "large" if code[:1] in "23" and code not in _SIZE else None)


def sector_kind(ownership_code: str | None) -> str | None:
    return _OWNERSHIP.get(str(ownership_code or ""))


def sanitize_rpo(data: Any) -> Any:
    """Odstráni z odpovede RPO osobné údaje okrem mena a funkcie štatutárov.
    Adresy štatutárov, spoločníci (stakeholders) a adresy organizácie (pri živnostníkoch je to
    často bydlisko) sa nezachovávajú; z adresy ostane iba obec a jej kód."""
    if isinstance(data, list):
        return [sanitize_rpo(x) for x in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "statutoryBodies":
            out[key] = [
                {k: v for k, v in body.items() if k in ("personName", "stakeholderType", "validFrom", "validTo")}
                for body in (value or [])
                if isinstance(body, dict)
            ]
        elif key in ("stakeholders", "deposits", "otherLegalFacts", "authorizations"):
            continue
        elif key == "addresses":
            out[key] = [
                {"municipality": a.get("municipality"), "validFrom": a.get("validFrom"), "validTo": a.get("validTo")}
                for a in (value or [])
                if isinstance(a, dict)
            ]
        elif key == "license":
            out[key] = str(value)[:200]
        else:
            out[key] = sanitize_rpo(value)
    return out


def _current(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Platná položka (bez validTo), inak posledná."""
    items = [i for i in (items or []) if isinstance(i, dict)]
    if not items:
        return None
    current = [i for i in items if not i.get("validTo")]
    return (current or items)[-1]


def current_name(record: dict[str, Any]) -> str:
    """Aktuálny názov z RPO (platná položka fullNames). Zoznam obsahuje aj historické názvy v ľubovoľnom poradí –
    porovnávať s posledným prvkom zlúčilo „Dôvera“ (zdravotná poisťovňa z článku) so spoločenstvom vlastníkov
    bytov, ktoré malo kedysi podobný názov."""
    return str(_value(_current(record.get("fullNames"))) or "")


def _texts(items: Any) -> list[str]:
    """Zoznam reťazcov z poľa RPO, kde položka býva reťazec alebo číselníková hodnota {"value": …}."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            item = item.get("value")
            if isinstance(item, dict):
                item = item.get("value")
        if item:
            out.append(str(item))
    return out


def _value(item: dict[str, Any] | None) -> Any:
    if not item:
        return None
    v = item.get("value")
    return v.get("value") if isinstance(v, dict) else v


@dataclass
class StatutoryPerson:
    """Meno a funkcia štatutára – nič viac (R-ORG-2)."""

    full_name: str
    function: str
    titles: str = ""


@dataclass
class OrgInfo:
    name: str
    ico: str | None = None
    legal_form: str | None = None
    sk_nace: str | None = None
    main_activity: str | None = None
    municipality: str | None = None
    region: str | None = None
    terminated: bool = False
    statutory: list[StatutoryPerson] = field(default_factory=list)
    rpo_id: int | None = None


def org_info_from_rpo(entity: dict[str, Any]) -> OrgInfo:
    entity = sanitize_rpo(entity)
    name = current_name(entity)
    ico = _value(_current(entity.get("identifiers")))
    legal = _current(entity.get("legalForms"))
    stat = entity.get("statisticalCodes") or {}
    main = stat.get("mainActivity") or {}
    address = _current(entity.get("addresses")) or {}
    muni = address.get("municipality") or {}
    code = str(muni.get("code") or "")
    people = []
    for body in entity.get("statutoryBodies") or []:
        if body.get("validTo"):
            continue
        pn = body.get("personName") or {}
        given = " ".join(_texts(pn.get("givenNames")))
        family = " ".join(_texts(pn.get("familyNames")))
        full = " ".join(x for x in (given, family) if x).strip()
        if full:
            titles = " ".join(_texts(pn.get("prefixes")) + _texts(pn.get("postfixes")))
            function = (body.get("stakeholderType") or {}).get("value") or ""
            people.append(StatutoryPerson(full_name=full, function=str(function), titles=titles))
    return OrgInfo(
        name=name,
        ico=str(ico) if ico else None,
        legal_form=_value(legal),
        sk_nace=str(main.get("code")) if main.get("code") else None,
        main_activity=main.get("value"),
        municipality=muni.get("value"),
        region=REGIONS.get(code[:5]),
        terminated=bool(entity.get("termination")),
        statutory=people,
        rpo_id=entity.get("id"),
    )


class RegistryClient:
    # Istič: po FAIL_LIMIT zlyhaniach za sebou (časový limit, chyba spojenia, HTTP 5xx) sa register na PAUSE_S
    # vynechá. Reálny beh 12. 10. 2026: RPO neodpovedalo a krok orgs čakal 60 s pri každej organizácii.
    FAIL_LIMIT = 3
    PAUSE_S = 900.0

    def __init__(self, db: DB, delay_s: float = 1.0, cache_days: float = 30, user_agent: str = "ProblemRadar/0.2",
                 http: httpx.Client | None = None):
        self.db = db
        self.delay_s = delay_s
        self.cache_days = cache_days
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0),
                                         headers={"User-Agent": user_agent, "Accept": "application/json"})
        self._last = 0.0
        self._fails = 0
        self._paused_until = 0.0
        self.skipped = 0  # dopyty vynechané počas prestávky ističa

    def _failed(self, url: str, reason: str) -> None:
        self._fails += 1
        if self._fails >= self.FAIL_LIMIT:
            self._paused_until = time.monotonic() + self.PAUSE_S
            self._fails = 0
            log.warning("Register nedostupný (%s): %s – %d× za sebou, %d min sa vynechá", url, reason,
                        self.FAIL_LIMIT, self.PAUSE_S // 60)
        else:
            log.warning("Register nedostupný (%s): %s", url, reason)

    def _get_json(self, url: str, params: dict[str, Any] | None = None, sanitize=None) -> Any:
        full = url + ("?" + urlencode(sorted((params or {}).items())) if params else "")
        cached = self.db.cache_get(full, self.cache_days)
        if cached is not None:
            return json.loads(cached)
        if time.monotonic() < self._paused_until:
            self.skipped += 1
            return None
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            resp = self.http.get(url, params=params)
        except httpx.HTTPError as exc:
            self._failed(url, str(exc))
            return None
        if resp.status_code >= 500:
            self._failed(url, f"HTTP {resp.status_code}")
            return None
        self._fails = 0
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            log.warning("Register vrátil HTTP %s (%s)", resp.status_code, url)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("Register nevrátil JSON (%s)", url)
            return None
        if sanitize is not None:
            data = sanitize(data)
        self.db.cache_put(full, json.dumps(data, ensure_ascii=False))
        return data


class RPOClient(RegistryClient):
    """Register právnických osôb (ŠÚ SR), otvorené API."""

    def search_ico(self, ico: str) -> list[dict[str, Any]]:
        data = self._get_json(f"{RPO_BASE}/search", {"identifier": ico}, sanitize_rpo)
        return (data or {}).get("results", [])

    def search_name(self, name: str) -> list[dict[str, Any]]:
        data = self._get_json(f"{RPO_BASE}/search", {"fullName": name}, sanitize_rpo)
        return (data or {}).get("results", [])

    def entity(self, entity_id: int) -> dict[str, Any] | None:
        return self._get_json(f"{RPO_BASE}/entity/{entity_id}", {"showHistoricalData": "false"}, sanitize_rpo)

    def entity_url(self, entity_id: int) -> str:
        return f"{RPO_BASE}/entity/{entity_id}"

    def lookup_ico(self, ico: str) -> OrgInfo | None:
        for result in self.search_ico(ico):
            entity = self.entity(int(result["id"])) if result.get("id") is not None else None
            if entity:
                return org_info_from_rpo(entity)
        return None


class RUZClient(RegistryClient):
    """Register účtovných závierok, Open API (CC0)."""

    def unit_ids(self, ico: str) -> list[int]:
        data = self._get_json(f"{RUZ_BASE}/api/uctovne-jednotky", {"zmenene-od": "2000-01-01", "ico": ico})
        return [int(i) for i in (data or {}).get("id", [])]

    def unit(self, unit_id: int) -> dict[str, Any] | None:
        data = self._get_json(f"{RUZ_BASE}/api/uctovna-jednotka", {"id": unit_id})
        if not data:
            return None
        # Ulica a PSČ sa nepotrebujú (pri živnostníkoch môže ísť o bydlisko).
        return {k: v for k, v in data.items() if k not in ("ulica", "psc")}

    def annual_report(self, report_id: int) -> dict[str, Any] | None:
        return self._get_json(f"{RUZ_BASE}/api/vyrocna-sprava", {"id": report_id})

    @staticmethod
    def attachment_url(attachment_id: int) -> str:
        return f"{RUZ_BASE}/domain/financialreport/attachment/{attachment_id}"

    def lookup_ico(self, ico: str) -> dict[str, Any] | None:
        """skNace, veľkosť, vlastníctvo, kraj a ID výročných správ pre IČO."""
        for unit_id in self.unit_ids(ico)[:1]:
            unit = self.unit(unit_id)
            if unit:
                return {
                    "ruz_id": unit_id,
                    "name": unit.get("nazovUJ"),
                    "sk_nace": unit.get("skNace"),
                    "size_category": size_category(unit.get("velkostOrganizacie")),
                    "sector_kind": sector_kind(unit.get("druhVlastnictva")),
                    "region": REGIONS.get(str(unit.get("kraj") or "")),
                    "municipality": unit.get("mesto"),
                    "annual_report_ids": unit.get("idVyrocnychSprav") or [],
                }
        return None
