"""Organizácie k hypotézam vybraným na Bránu 0 (R-ORG).

Zdroje v poradí priority (R-ORG-1):
1. organizácie menované v overených dôkazoch hypotézy (org_mentions; kód overí, že názov je
   doslova v citáte alebo v okne okolo neho),
2. obstarávanie a signály dopytu (prípravné trhové konzultácie, výzvy) – takéto organizácie
   dostanú príznak procurement_active a návrh oslovenia sa pre ne nevytvorí (R-OUT-13),
3. segment: segment_filter → vyhľadávanie webov → IČO zo stránky → RPO → filter podľa hlavnej
   činnosti, veľkosti a regiónu (zložky zhody sa ukladajú zvlášť, R-ORG-5),
4. verejné zoznamy importované z CSV (orgs --import).

Web organizácie je overený iba vtedy, keď stránka obsahuje IČO alebo presný názov (R-ORG-4);
inak sa z webu ľudia nezbierajú. Organizácie z outreach.exclude_orgs sa nikdy nezaradia (R-ORG-7).
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from .config import Config
from .contacts import contact_page_links, extract_contacts, visible_text
from .db import DB
from .fetch import domain_blocked
from .models import ModelRouter, as_router
from .names import fold, org_name_key
from .prompts import ORG_MENTIONS_SYSTEM, SEGMENT_FILTER_SYSTEM
from .registers import RPO_ATTRIBUTION, OrgInfo, RPOClient, RUZClient, current_name, org_info_from_rpo
from .strength import STRENGTH_ORDER, evidence_strength, strength_at_least
from .util import Deadline, domain_of
from .verify import quote_window

log = logging.getLogger(__name__)

ICO_RE = re.compile(r"\bI[ČC]O?\b\s*[:.]?\s*(\d{2}\s?\d{3}\s?\d{3}|\d{6})(?!\d)", re.I)
PROCUREMENT_TERMS = ("prípravné trhové konzultácie", "pripravne trhove konzultacie", "výzva na predkladanie ponúk",
                     "verejné obstarávanie", "súťažné podklady", "zákazka", "oznámenie o vyhlásení")
ACTIVE_PROCUREMENT = ("prípravné trhové konzultácie", "pripravne trhove konzultacie", "výzva na predkladanie ponúk",
                      "súťažné podklady", "lehota na predkladanie")
PROCUREMENT_DOMAINS = ("uvo.gov.sk", "josephine.proebiz.com", "eks.sk", "crz.gov.sk", "eo.eks.sk", "tender.sk")
# Výsledky vyhľadávania, ktoré sú katalógom alebo rebríčkom firiem, nie webom jednej organizácie.
DIRECTORY_TITLE = re.compile(r"\b(zoznam|katal[oó]g|najlep[sš]|top\s*\d+|porovnanie|recenzi|firmy\s+v|adres[aá]r|\d+\s+firiem)", re.I)


class OrgMention(BaseModel):
    name: str
    relation: Literal["affected", "quoted", "solution", "regulator", "other"] = "other"


class OrgMentions(BaseModel):
    organizations: list[OrgMention] = Field(default_factory=list)


class SegmentFilter(BaseModel):
    sk_nace_prefixes: list[str] = Field(default_factory=list)
    sector_kind: str = "any"
    sizes: list[str] = Field(default_factory=list)
    region: str = ""
    search_queries: list[str] = Field(default_factory=list)


def icos_in(text: str) -> list[str]:
    """IČO uvedené na stránke (8 číslic; staré 6-miestne sa doplnia nulami)."""
    out = []
    for m in ICO_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(1))
        digits = digits.zfill(8)
        if digits not in out:
            out.append(digits)
    return out


def name_in(name: str, text: str) -> bool:
    """Je názov organizácie doslova v texte (bez ohľadu na diakritiku, veľkosť písmen a medzery)?"""
    n = fold(name)
    return len(n) >= 3 and n in fold(text)


def name_on_page(name: str, text: str) -> bool:
    """Názov z registra (bez právnej formy) je na stránke: celý, alebo aspoň všetky jeho dlhšie slová."""
    key = org_name_key(name)
    page = " " + " ".join(org_name_key(text).split()) + " "
    if not key:
        return False
    if f" {key} " in page:
        return True
    words = [w for w in key.split() if len(w) >= 4]
    return bool(words) and all(f" {w} " in page for w in words)


def is_excluded(cfg: Config, ico: str | None, website: str | None) -> bool:
    excluded = {x.strip().lower().removeprefix("www.") for x in cfg.outreach.exclude_orgs if x and x.strip()}
    if not excluded:
        return False
    if ico and ico in excluded:
        return True
    host = domain_of(website or "") if website else ""
    return bool(host) and any(host == d or host.endswith("." + d) for d in excluded)


ICO_PATHS = ("/kontakt", "/o-nas", "/kontakty")  # keď úvodná stránka nemá odkaz na kontakt


def root_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}/"


def select_people_hypotheses(cfg: Config, db: DB) -> list[int]:
    """Automatický výber: top N hypotéz podľa sily dôkazov (aspoň people.min_strength, nie zamietnuté
    ani kandidáti). Ručná voľba (select, GUI) má prednosť a výber ju neprepíše."""
    ranked = []
    for h in db.active_hypotheses():
        label, n_ver, _, n_dom = evidence_strength(db.evidence_for(h["id"]))
        if strength_at_least(label, cfg.people.min_strength):
            ranked.append((STRENGTH_ORDER[label], n_ver, n_dom, -h["id"], h))
    ranked.sort(reverse=True)
    top = {r[4]["id"] for r in ranked[: cfg.people.auto_select_top]}
    for h in db.active_hypotheses():
        if h["people_manual"] is None:
            db.set_people_selection(h["id"], h["id"] in top, manual=False)
    return [h["id"] for h in db.people_enabled_hypotheses()]


class OrgFinder:
    def __init__(self, cfg: Config, db: DB, router: ModelRouter, searx, fetcher, rpo: RPOClient | None,
                 ruz: RUZClient | None, deadline: Deadline, model_filters: bool = True):
        self.cfg, self.db, self.router, self.searx, self.fetcher = cfg, db, router, searx, fetcher
        self.rpo, self.ruz, self.deadline = rpo, ruz, deadline
        # False v rýchlej fáze behu: filter segmentu (presný model) pripravuje prepare-next; inak by sa pri každej
        # hypotéze striedal rýchly a presný model (reálna akcia #19: 47 z 53 hypotéz bez filtra).
        self.model_filters = model_filters
        self.stats: Counter = Counter()
        self._resolved: dict[str, OrgInfo | None] = {}

    # ------------------------------------------------------------- registre
    def resolve_name(self, name: str) -> OrgInfo | None:
        """Názov z textu -> organizácia v RPO (iba pri jednoznačnej zhode názvu)."""
        key = org_name_key(name)
        if key in self._resolved:
            return self._resolved[key]
        info = None
        if self.rpo is not None and len(key) >= 4:
            scored = []
            for r in self.rpo.search_name(name)[:20]:
                current = current_name(r)
                if r.get("termination"):
                    continue
                score = fuzz.ratio(org_name_key(current), key)
                if score >= 92:
                    scored.append((score, r))
            scored.sort(key=lambda x: -x[0])
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                entity = self.rpo.entity(int(scored[0][1]["id"]))
                if entity:
                    info = org_info_from_rpo(entity)
            elif len(scored) > 1:
                self.stats["ambiguous_names"] += 1
        self._resolved[key] = info
        return info

    def lookup_ico(self, ico: str) -> OrgInfo | None:
        return self.rpo.lookup_ico(ico) if self.rpo is not None else None

    def store(self, name: str, source: str, info: OrgInfo | None = None, source_url: str | None = None,
              website: str | None = None, verified_evidence: str | None = None) -> int | None:
        """Uloží organizáciu (bez osobných údajov); None, ak je vylúčená."""
        ico = info.ico if info else None
        if is_excluded(self.cfg, ico, website):
            self.stats["excluded"] += 1
            return None
        fields: dict[str, Any] = {}
        if info:
            fields.update(legal_form=info.legal_form, sk_nace=info.sk_nace, municipality=info.municipality,
                          region=info.region)
            fields["register_json"] = json.dumps(
                {"rpo_id": info.rpo_id, "main_activity": info.main_activity, "zdroj": RPO_ATTRIBUTION}, ensure_ascii=False
            )
        if website:
            fields["website"] = website
        if verified_evidence:
            fields.update(website_verified=1, website_evidence=verified_evidence)
        org_id, created = self.db.upsert_organization(
            info.name if info and info.name else name, org_name_key(info.name if info and info.name else name),
            source, ico=ico, source_url=source_url, **{k: v for k, v in fields.items() if v is not None},
        )
        org = self.db.get_organization(org_id)
        if org["excluded"]:
            return None
        if created:
            self.stats["orgs_created"] += 1
        if ico and self.ruz is not None and org["size_category"] is None:
            self.enrich_ruz(org_id, ico)
        return org_id

    def enrich_ruz(self, org_id: int, ico: str) -> None:
        data = self.ruz.lookup_ico(ico) if self.ruz else None
        if not data:
            return
        org = self.db.get_organization(org_id)
        updates = {k: data[k] for k in ("size_category", "sector_kind", "region", "municipality") if data.get(k) and not org[k]}
        if data.get("sk_nace") and not org["sk_nace"]:
            updates["sk_nace"] = data["sk_nace"]
        if updates:
            self.db.update_organization(org_id, **updates)

    # ------------------------------------------------------------ overenie webu
    def verify_website(self, org_id: int) -> bool:
        """Oficiálny web: stránka musí obsahovať IČO alebo presný názov (R-ORG-4)."""
        org = self.db.get_organization(org_id)
        if org["website_verified"]:
            return True
        candidates: list[str] = []
        if org["website"]:
            candidates.append(root_url(org["website"]))
        queries = [f'"{org["name"]}" {org["ico"]}' if org["ico"] else None, f'"{org["name"]}" kontakt']
        for q in [q for q in queries if q]:
            if len(candidates) >= 4 or self.deadline.expired():
                break
            try:
                results = self.searx.search(q, limit=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hľadanie webu '%s' zlyhalo: %s", q, exc)
                continue
            for r in results:
                host = domain_of(r.url)
                if not host or domain_blocked(r.url, self.cfg.blocked_domains()):
                    continue
                if any(host == a or host.endswith("." + a) for a in self.cfg.people.aggregator_domains):
                    continue
                url = root_url(r.url)
                if url not in candidates:
                    candidates.append(url)
        for url in candidates[:4]:
            if self.deadline.expired():
                break
            evidence = self._page_mentions_org(url, org)
            if evidence:
                if is_excluded(self.cfg, org["ico"], url):
                    self.db.update_organization(org_id, excluded=1)
                    self.stats["excluded"] += 1
                    return False
                self.db.update_organization(org_id, website=url, website_verified=1, website_evidence=evidence)
                self.stats["websites_verified"] += 1
                return True
        self.stats["websites_unverified"] += 1
        return False

    def _page_mentions_org(self, url: str, org) -> str | None:
        page = self.fetcher.fetch_html(url)
        if page.status != "ok":
            return None
        pages = [(url, page.html)]
        contacts = extract_contacts(page.html, url)
        for link in contact_page_links(contacts, url, limit=1):
            sub = self.fetcher.fetch_html(link)
            if sub.status == "ok":
                pages.append((link, sub.html))
        for page_url, html in pages:
            text = visible_text(html)
            if org["ico"] and org["ico"] in icos_in(text):
                return f"IČO {org['ico']} na {page_url}"
            if name_in(org["name"], text) and len(fold(org["name"])) >= 8:
                return f"presný názov na {page_url}"
        return None

    # ---------------------------------------------------------- zdroj 1: dôkazy
    def from_evidence(self, hyp) -> None:
        evidence = [e for e in self.db.evidence_for(hyp["id"]) if e["status"] == "verified"][:25]
        for claim in evidence:
            if self.deadline.expired() or self._full(hyp["id"]):
                return
            doc = self.db.get_document(claim["document_id"])
            window = quote_window(claim["quote"], doc["text"], radius=500) if doc else claim["quote"]
            res = self.router.run_task(
                "org_mentions",
                [
                    {"role": "system", "content": ORG_MENTIONS_SYSTEM},
                    {"role": "user", "content": f"PROBLEM: {hyp['title']}\nQUOTE: {claim['quote']}\n\n"
                                                f"<untrusted_document>\n{window}\n</untrusted_document>"},
                ],
                OrgMentions,
                object_type="claim",
                object_id=claim["id"],
                max_tokens=500,
            )
            if not res.ok:
                self.stats["llm_failures"] += 1
                continue
            for mention in res.value.organizations[:5]:
                if mention.relation not in ("affected", "quoted"):
                    continue
                if not (name_in(mention.name, claim["quote"]) or name_in(mention.name, window)):
                    self.stats["mentions_not_in_source"] += 1  # model si názov vymyslel alebo upravil
                    continue
                info = self.resolve_name(mention.name)
                org_id = self.store(mention.name, "evidence", info, source_url=claim["url"])
                if org_id is None:
                    continue
                if self.db.link_org_hypothesis(org_id, hyp["id"], "mentioned_with_problem", claim["id"], claim["url"],
                                               fit={"dôkaz o probléme": "áno (C%d)" % claim["id"]}):
                    self.stats["linked_evidence"] += 1

    # ------------------------------------------------------- zdroj 2: obstarávanie
    def from_procurement(self, hyp) -> None:
        keywords = " ".join(re.findall(r"[^\W\d_]{4,}", hyp["title"] or "", re.U)[:6])
        if not keywords:
            return
        queries = [f'"prípravné trhové konzultácie" {keywords}', f"site:uvo.gov.sk {keywords}"]
        for q in queries:
            if self.deadline.expired() or self._full(hyp["id"]):
                return
            try:
                results = self.searx.search(q, limit=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hľadanie obstarávania '%s' zlyhalo: %s", q, exc)
                continue
            for r in results:
                host = domain_of(r.url)
                text = f"{r.title}. {r.snippet}"
                if not any(host.endswith(d) for d in PROCUREMENT_DOMAINS) and not any(t in fold(text) for t in map(fold, PROCUREMENT_TERMS)):
                    continue
                res = self.router.run_task(
                    "org_mentions",
                    [
                        {"role": "system", "content": ORG_MENTIONS_SYSTEM},
                        {"role": "user", "content": f"PROBLEM: {hyp['title']}\nThis is a public procurement notice; the "
                                                    f"contracting authority counts as 'affected'.\n<untrusted_document>\n{text}\n</untrusted_document>"},
                    ],
                    OrgMentions,
                    max_tokens=400,
                )
                if not res.ok:
                    self.stats["llm_failures"] += 1
                    continue
                active = any(fold(t) in fold(text) for t in ACTIVE_PROCUREMENT)
                for mention in res.value.organizations[:2]:
                    if mention.relation != "affected" or not name_in(mention.name, text):
                        continue
                    info = self.resolve_name(mention.name)
                    org_id = self.store(mention.name, "procurement", info, source_url=r.url)
                    if org_id is None:
                        continue
                    new = self.db.link_org_hypothesis(org_id, hyp["id"], "procured_related", None, r.url,
                                                      fit={"dôkaz o probléme": "obstaráva súvisiacu vec"})
                    self.stats["linked_procurement"] += int(new)
                    if active:
                        self.db.set_org_hypothesis(org_id, hyp["id"], procurement_active=1,
                                                   procurement_note=f"Možné prebiehajúce obstarávanie: {r.url}")

    # ------------------------------------------------------------ zdroj 3: segment
    def segment_filter(self, hyp) -> SegmentFilter | None:
        if hyp["segment_filter_json"]:
            return SegmentFilter.model_validate_json(hyp["segment_filter_json"])
        res = self.router.run_task(
            "segment_filter",
            [
                {"role": "system", "content": SEGMENT_FILTER_SYSTEM.format(lang=self.cfg.pipeline.output_language)},
                {"role": "user", "content": f"PROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment']}\nDESCRIPTION: {hyp['description']}"},
            ],
            SegmentFilter,
            object_type="hypothesis",
            object_id=hyp["id"],
            max_tokens=600,
        )
        if not res.ok:
            self.stats["llm_failures"] += 1
            return None
        flt = res.value
        flt.sk_nace_prefixes = [re.sub(r"\D", "", p) for p in flt.sk_nace_prefixes if re.sub(r"\D", "", p)][:5]
        self.db.set_hypothesis_fields(hyp["id"], segment_filter_json=flt.model_dump_json())
        return flt

    def fit_components(self, org, flt: SegmentFilter) -> dict[str, str]:
        def comp(value, allowed, match) -> str:
            if not allowed:
                return "bez filtra"
            if not value:
                return "neznáme"
            return "zhoda" if match(value) else "nezhoda"

        return {
            "odvetvie": comp(org["sk_nace"], flt.sk_nace_prefixes, lambda v: any(str(v).startswith(p) for p in flt.sk_nace_prefixes)),
            "veľkosť": comp(org["size_category"], flt.sizes, lambda v: v in flt.sizes),
            "región": comp(org["region"], flt.region, lambda v: fold(flt.region) in fold(v) or fold(v) in fold(flt.region)),
            "typ": comp(org["sector_kind"], flt.sector_kind if flt.sector_kind not in ("", "any") else None, lambda v: v == flt.sector_kind),
            "dôkaz o probléme": "nie",
        }

    def from_segment(self, hyp) -> None:
        if not hyp["segment_filter_json"] and not self.model_filters:
            self.stats["segment_filter_pending"] += 1  # hľadanie podľa segmentu po prepare-next (ďalší beh)
            return
        flt = self.segment_filter(hyp)
        if flt is None:
            return
        for q in flt.search_queries[:5]:
            if self.deadline.expired() or self._full(hyp["id"]):
                return
            try:
                results = self.searx.search(q, limit=8)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hľadanie organizácií '%s' zlyhalo: %s", q, exc)
                continue
            for r in results:
                if self.deadline.expired() or self._full(hyp["id"]):
                    return
                try:
                    self._segment_result(hyp, flt, r)
                except Exception as exc:  # noqa: BLE001 – jedna stránka alebo odpoveď registra nezastaví beh
                    log.warning("Kandidát %s preskočený: %s", r.url, exc)
                    self.stats["errors"] += 1

    def _segment_result(self, hyp, flt: SegmentFilter, r) -> None:
        host = domain_of(r.url)
        if not host or domain_blocked(r.url, self.cfg.blocked_domains()):
            return
        if any(host == a or host.endswith("." + a) for a in self.cfg.people.aggregator_domains):
            return
        if DIRECTORY_TITLE.search(r.title or ""):
            self.stats["segment_directory"] += 1  # katalóg firiem, nie web organizácie
            return
        url = root_url(r.url)
        if is_excluded(self.cfg, None, url):
            self.stats["excluded"] += 1
            return
        if self.db.find_organization_by_website(url) is not None:
            org = self.db.find_organization_by_website(url)
            self._link_segment(hyp, flt, org["id"], r.url)
            return
        page = self.fetcher.fetch_html(url)
        if page.status != "ok":
            return
        contacts = extract_contacts(page.html, url)
        icos = icos_in(visible_text(page.html))
        if not icos:
            # IČO býva v sekcii Kontakt, O nás, obchodných podmienkach alebo impresu; najviac 3 podstránky
            # (odkazy z úvodnej stránky, inak bežné adresy). Akcia #20: 130 kandidátov bez IČO na 1 podstránke.
            links = contact_page_links(contacts, url, limit=3) + [url.rstrip("/") + path for path in ICO_PATHS]
            for link in list(dict.fromkeys(links))[:3]:
                sub = self.fetcher.fetch_html(link)
                if sub.status == "ok":
                    icos = icos_in(visible_text(sub.html))
                    if icos:
                        break
        if not icos:
            self.stats["segment_no_ico"] += 1
            log.info("Segment: bez IČO na webe %s – kandidát vynechaný", domain_of(url))
            return
        info = self.lookup_ico(icos[0])
        if info is None or info.terminated:
            return
        if not name_on_page(info.name, visible_text(page.html)):
            self.stats["segment_name_mismatch"] += 1  # IČO patrí inej firme (napr. české IČO na .cz webe)
            return
        org_id = self.store(info.name, "search", info, source_url=r.url, website=url,
                            verified_evidence=f"IČO {icos[0]} na {url}")
        if org_id is not None:
            self._link_segment(hyp, flt, org_id, r.url)

    def _link_segment(self, hyp, flt: SegmentFilter, org_id: int, url: str) -> None:
        org = self.db.get_organization(org_id)
        fit = self.fit_components(org, flt)
        if "nezhoda" in (fit["odvetvie"], fit["typ"]):
            self.stats["segment_mismatch"] += 1
            return
        if self.db.link_org_hypothesis(org_id, hyp["id"], "segment_match", None, url, fit=fit):
            self.stats["linked_segment"] += 1

    def _full(self, hid: int) -> bool:
        return len(self.db.orgs_for_hypothesis(hid)) >= self.cfg.people.max_orgs_per_hypothesis

    def run_hypothesis(self, hyp) -> None:
        for source in (self.from_evidence, self.from_procurement, self.from_segment):
            try:
                source(hyp)
            except Exception:  # noqa: BLE001 – chyba jedného zdroja nezastaví ostatné
                log.exception("Zdroj organizácií %s zlyhal pre H%s", source.__name__, hyp["id"])
                self.stats["errors"] += 1
        for org in self.db.orgs_for_hypothesis(hyp["id"]):
            if self.deadline.expired():
                break
            if not org["website_verified"]:
                self.verify_website(org["id"])
            if org["oh_status"] == "candidate":
                self.db.set_org_hypothesis(org["id"], hyp["id"], status="website_checked")


def import_public_list(cfg: Config, db: DB, hid: int, path: str | Path, rpo: RPOClient | None = None) -> dict:
    """Verejný zoznam organizácií z CSV (stĺpce name, ico, website; oddeľovač ; alebo ,)."""
    stats: Counter = Counter()
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
    for row in csv.DictReader(text.splitlines(), dialect=dialect):
        name = (row.get("name") or row.get("nazov") or "").strip()
        ico = re.sub(r"\D", "", row.get("ico") or "") or None
        website = (row.get("website") or row.get("web") or "").strip() or None
        if not name and not ico:
            continue
        if is_excluded(cfg, ico, website):
            stats["excluded"] += 1
            continue
        info = rpo.lookup_ico(ico) if rpo and ico else None
        org_id, created = db.upsert_organization(
            (info.name if info else name) or name, org_name_key((info.name if info else name) or name), "public_list",
            ico=ico, website=root_url(website) if website else None,
        )
        stats["created" if created else "existing"] += 1
        db.link_org_hypothesis(org_id, hid, "manual", None, None, fit={"zdroj": f"zoznam {Path(path).name}"})
    return dict(stats)


def run_orgs(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline, hypothesis_ids: list[int] | None = None,
             rpo: RPOClient | None = None, ruz: RUZClient | None = None, model_filters: bool = True) -> dict:
    """Organizácie pre hypotézy s people_enabled (alebo zadané hypotézy)."""
    router = as_router(llm, cfg, db)
    if rpo is None:
        rpo = RPOClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    if ruz is None:
        ruz = RUZClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    ids = hypothesis_ids or select_people_hypotheses(cfg, db)
    finder = OrgFinder(cfg, db, router, searx, fetcher, rpo, ruz, deadline, model_filters=model_filters)
    for hid in ids:
        if deadline.expired():
            break
        hyp = db.get_hypothesis(hid)
        if hyp is None:
            continue
        finder.run_hypothesis(hyp)
        finder.stats["hypotheses"] += 1
    return dict(finder.stats)
