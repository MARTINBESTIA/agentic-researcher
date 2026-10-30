"""Organizácie k hypotézam vybraným na Bránu 0 (R-ORG).

Zdroje v poradí priority (R-ORG-1):
1. organizácie menované v overených dôkazoch hypotézy (org_mentions; kód overí, že názov je
   doslova v citáte alebo v okne okolo neho),
2. obstarávanie a signály dopytu (prípravné trhové konzultácie, výzvy) – takéto organizácie
   dostanú príznak procurement_active a návrh oslovenia sa pre ne nevytvorí (R-OUT-13),
3. segment: segment_filter → vyhľadávanie webov → IČO zo stránky → RPO → filter podľa hlavnej
   činnosti, veľkosti a regiónu (zložky zhody sa ukladajú zvlášť, R-ORG-5),
4. verejné zoznamy importované z CSV (orgs --import).

Web organizácie je overený iba vtedy, keď stránka obsahuje IČO alebo presný názov (R-ORG-4) – pri samotnom názve
musí názvu zodpovedať aj doména a stránka s viacerými IČO je katalóg; inak sa z webu ľudia nezbierajú. Organizácie z outreach.exclude_orgs sa nikdy nezaradia (R-ORG-7).
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timedelta, timezone
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


# Slová, ktoré nič nehovoria o konkrétnej organizácii (začiatok takého slova v doméne nie je zhoda s názvom).
GENERIC_NAME_WORDS = {
    "pre", "na", "vo", "zo", "ku", "and", "the", "of", "spol", "sro", "as", "ministerstvo", "urad", "uradu", "narodna",
    "narodny", "narodnej", "statna", "statny", "statnej", "slovenskej", "slovenska", "slovensky", "slovenskeho",
    "republiky", "republika", "spolocnost", "spolocnosti", "mesto", "mesta", "obec", "obce", "kraj", "samospravny",
    "agentura", "institut", "centrum", "fond", "zdruzenie", "group", "company", "sluzby", "stredisko", "kancelaria",
}
# Viac rôznych IČO na stránke = katalóg alebo zoznam firiem, nie web jednej organizácie.
DIRECTORY_MIN_ICOS = 4


def _host_text(url: str) -> str:
    """Doména bez TLD a „www“ spojená do jedného reťazca (napr. tv.nrsr.sk → tvnrsr, trnava-vuc.sk → trnavavuc)."""
    labels = domain_of(url).split(".")
    return "".join(labels[:-1] if len(labels) > 1 else labels).replace("-", "")


def domain_matches_name(url: str, name: str) -> bool:
    """Patrí doména k názvu organizácie? Skratka (Úrad pre verejné obstarávanie → uvo, Ministerstvo financií SR → mfsr),
    začiatok výrazného slova (SLOVNAFT → slovn, Žilinský kraj → zilin) alebo spojenie začiatkov dvoch slov (Sociálna
    poisťovňa → socpoi). Katalóg firiem či cudzí web (kontakttelefon.sk pri ÚVO) zhodou nie je – pri overení webu iba
    podľa názvu na stránke to bráni tomu, aby sa za web organizácie považoval adresár, ktorý ju len spomína."""
    host = _host_text(url)
    words = re.findall(r"[a-z0-9]+", fold(name))
    sig = [w for w in words if w not in {"a", "s", "r", "o", "v", "z", "k", "pre", "na", "vo", "zo", "ku", "and", "the", "of"}]
    if not host or not sig:
        return False
    cands: set[str] = set()
    for seq in (sig, sig[1:]):  # skratka celého názvu aj bez prvého slova („Kancelária NR SR“ → nrsr)
        if len(seq) >= 2:
            cands.add("".join(w[0] for w in seq))
            cands.add("".join(w if len(w) <= 3 else w[0] for w in seq))
    distinct = [w for w in sig if w not in GENERIC_NAME_WORDS and len(w) >= 4]
    cands |= {w[:5] if len(w) >= 6 else w[:4] for w in distinct}
    cands |= {a[:3] + b[:3] for a, b in zip(sig, sig[1:]) if len(a) >= 3 and len(b) >= 3}
    return any(len(c) >= 3 and c in host for c in cands)


def revoke_website(db: DB, org_id: int, reason: str) -> dict[str, int]:
    """Zruší chybne overený web organizácie (napr. katalóg, ktorý ju len spomína): kontakty zo stránok tej domény
    nie sú zverejnené samotnou organizáciou, preto sa odstránia spolu s neodoslanými návrhmi na ne. Odoslané
    oslovenia a ľudia ostávajú (ľudia môžu mať iný zdroj, napr. register)."""
    org = db.get_organization(org_id)
    if org is None or not org["website"]:
        return {"kontakty": 0, "návrhy": 0}
    host = domain_of(org["website"])
    bad = [c["id"] for c in db.org_all_contacts(org_id)
           if c["source_url"] and (domain_of(c["source_url"]) == host or domain_of(c["source_url"]).endswith("." + host))]
    drafts = db.delete_unsent_outreach_for_contacts(bad)
    for cid in bad:
        db.delete_contact_point(cid)
    db.update_organization(org_id, website=None, website_verified=0,
                           website_evidence=f"zrušené: {reason} (pôvodne {org['website']})")
    return {"kontakty": len(bad), "návrhy": drafts}


def is_excluded(cfg: Config, ico: str | None, website: str | None) -> bool:
    excluded = {x.strip().lower().removeprefix("www.") for x in cfg.outreach.exclude_orgs if x and x.strip()}
    if not excluded:
        return False
    if ico and ico in excluded:
        return True
    host = domain_of(website or "") if website else ""
    return bool(host) and any(host == d or host.endswith("." + d) for d in excluded)


ICO_PATHS = ("/kontakt", "/o-nas", "/kontakty")  # keď úvodná stránka nemá odkaz na kontakt
# Mestá na rozšírenie dopytov podľa segmentu (M42): krajské mesto prvé, potom ďalšie okresné mestá kraja.
REGION_TOWNS = {
    "Bratislavský kraj": ["Bratislava", "Pezinok", "Senec", "Malacky"],
    "Trnavský kraj": ["Trnava", "Piešťany", "Dunajská Streda", "Galanta", "Hlohovec", "Senica", "Skalica"],
    "Trenčiansky kraj": ["Trenčín", "Prievidza", "Považská Bystrica", "Nové Mesto nad Váhom", "Partizánske", "Púchov",
                         "Bánovce nad Bebravou", "Myjava"],
    "Nitriansky kraj": ["Nitra", "Nové Zámky", "Komárno", "Levice", "Topoľčany", "Šaľa", "Zlaté Moravce"],
    "Žilinský kraj": ["Žilina", "Martin", "Liptovský Mikuláš", "Ružomberok", "Čadca", "Dolný Kubín", "Námestovo",
                      "Kysucké Nové Mesto", "Bytča", "Tvrdošín", "Turčianske Teplice", "Liptovský Hrádok"],
    "Banskobystrický kraj": ["Banská Bystrica", "Zvolen", "Lučenec", "Rimavská Sobota", "Brezno", "Žiar nad Hronom",
                             "Banská Štiavnica", "Detva", "Revúca", "Veľký Krtíš"],
    "Prešovský kraj": ["Prešov", "Poprad", "Humenné", "Bardejov", "Vranov nad Topľou", "Kežmarok", "Stará Ľubovňa",
                       "Svidník", "Snina", "Levoča"],
    "Košický kraj": ["Košice", "Michalovce", "Spišská Nová Ves", "Trebišov", "Rožňava", "Moldava nad Bodvou"],
}
# Slová, ktoré nie sú jadrom dopytu („realitná kancelária Žilina kontakt“ → „realitná kancelária“).
QUERY_FILLER = {"kontakt", "kontakty", "web", "webstranka", "webova", "stranka", "email", "mail", "telefon", "tel", "kraj",
                "kraji", "okres", "okrese", "mesto", "sk", "slovensko", "v", "na", "a"}


def _place_tokens() -> set[str]:
    out = set()
    for region, towns in REGION_TOWNS.items():
        for name in [region, *towns]:
            out.update(t for t in re.findall(r"\w+", fold(name)) if len(t) >= 3 and t not in ("nad", "kraj"))
    return out


def query_core(query: str) -> str:
    """Dopyt bez miesta a výplňových slov (kontakt, web, e-mail, telefón)."""
    places = _place_tokens()
    words = [w for w in re.findall(r"[\w-]+", query) if fold(w).replace("-", "") not in QUERY_FILLER
             and fold(w) not in places]
    return " ".join(words)


def segment_query_plan(flt: "SegmentFilter", expand: bool = True) -> list[str]:
    """Dopyty segmentu (M42): pôvodné z filtra, potom jadro dopytu s mestami – najprv kraj hypotézy, potom krajské
    mestá a ostatné okresné mestá. Mesto, ktoré už je v pôvodnom dopyte, sa nezopakuje."""
    base = [q.strip() for q in flt.search_queries[:5] if q.strip()]
    if not expand:
        return base
    used = {t for q in base for t in re.findall(r"\w+", fold(q))}
    region = next((r for r in REGION_TOWNS if flt.region and (fold(r) in fold(flt.region) or fold(flt.region) in fold(r))),
                  None)
    towns = list(REGION_TOWNS.get(region, []))
    towns += [ts[0] for r, ts in REGION_TOWNS.items() if r != region]  # krajské mestá
    towns += [t for r, ts in REGION_TOWNS.items() if r != region for t in ts[1:]]
    cores = list(dict.fromkeys(c for c in (query_core(q) for q in base) if len(c) >= 4))
    out = list(base)
    for town in towns:
        if all(t in used for t in re.findall(r"\w+", fold(town)) if len(t) >= 3):
            continue
        out += [f"{core} {town}" for core in cores]
    return list(dict.fromkeys(out))


def root_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}/"


def validated_for_people(cfg: Config, db: DB) -> set[int]:
    """Hypotézy, ktorých test skončil verdiktom z people.auto_select_verdicts (napr. „podložená“)."""
    if not cfg.people.auto_select_validated:
        return set()
    return {h["id"] for h in db.validated_hypotheses(tuple(cfg.people.auto_select_verdicts))}


def auto_select_validated(cfg: Config, db: DB, ids: list[int] | None = None) -> list[int]:
    """Zapne Bránu 0 hypotézam, ktorých test prešiel (people.auto_select_validated). Ručné vypnutie má prednosť
    (people_manual = 0 sa nemení). Vráti novo zapnuté hypotézy."""
    enabled = []
    for hid in sorted(validated_for_people(cfg, db)):
        if ids is not None and hid not in ids:
            continue
        h = db.get_hypothesis(hid)
        if h is not None and h["people_manual"] is None and not h["people_enabled"]:
            db.set_people_selection(hid, True, manual=False)
            enabled.append(hid)
    if enabled:
        log.info("Brána 0 zapnutá po teste hypotézy: %s", ", ".join(f"H{h}" for h in enabled))
    return enabled


def select_people_hypotheses(cfg: Config, db: DB) -> list[int]:
    """Automatický výber: top N hypotéz podľa sily dôkazov (aspoň people.min_strength, nie zamietnuté
    ani kandidáti) a hypotézy, ktorých test prešiel (people.auto_select_validated). Ručná voľba (select, GUI)
    má prednosť a výber ju neprepíše."""
    ranked = []
    for h in db.active_hypotheses():
        label, n_ver, _, n_dom = evidence_strength(db.evidence_for(h["id"]))
        if strength_at_least(label, cfg.people.min_strength):
            ranked.append((STRENGTH_ORDER[label], n_ver, n_dom, -h["id"], h))
    ranked.sort(reverse=True)
    top = {r[4]["id"] for r in ranked[: cfg.people.auto_select_top]} | validated_for_people(cfg, db)
    for h in db.active_hypotheses():
        if h["people_manual"] is None:
            db.set_people_selection(h["id"], h["id"] in top, manual=False)
    return [h["id"] for h in db.people_enabled_hypotheses()]


def least_recently_searched(db: DB, ids: list[int]) -> list[int]:
    """Najprv hypotézy bez hľadania kontaktov, potom najdlhšie neprehľadané – pri mnohých hypotézach (automatický
    výber po teste) by inak každý beh začínal tými istými a nové by sa nedostali na rad."""
    rows = [(db.get_hypothesis(h), h) for h in ids]
    return [h for row, h in sorted(rows, key=lambda x: ((x[0]["contacts_searched_at"] or "") if x[0] else "", x[1]))]


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
        # Web bez IČO sa v rámci behu neskúša znova. Úloha #411: jeden úrad 39-krát za hodinu, 4 stránky s pauzami zakaždým.
        self._no_ico: set[str] = set()

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
                # doména zo zoznamu agregátorov je webom iba organizácie, ktorej názvu zodpovedá (ÚVO → uvo.gov.sk)
                if any(host == a or host.endswith("." + a) for a in self.cfg.people.aggregator_domains) and \
                        not domain_matches_name(r.url, org["name"]):
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
            icos = icos_in(text)
            if len(icos) >= DIRECTORY_MIN_ICOS:  # katalóg alebo zoznam firiem (napr. kontakttelefon.sk), nie web organizácie
                continue
            if org["ico"] and org["ico"] in icos:
                return f"IČO {org['ico']} na {page_url}"
            # samotný názov na stránke stačí iba vtedy, keď mu zodpovedá aj doména (adresár názov spomína tiež)
            if name_in(org["name"], text) and len(fold(org["name"])) >= 8 and domain_matches_name(page_url, org["name"]):
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
            cached = self.db.cached_output("org_mentions", "claim", claim["id"])
            if cached is not None:  # tvrdenie už model prečítal (iný beh alebo iná hypotéza): väzby iba z kódu
                mentions = OrgMentions.model_validate_json(cached)
                self.stats["org_mentions_cached"] += 1
            else:
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
                mentions = res.value
            for mention in mentions.organizations[:5]:
                if mention.relation not in ("affected", "quoted"):
                    continue
                if not (name_in(mention.name, claim["quote"]) or name_in(mention.name, window)):
                    self.stats["mentions_not_in_source"] += 1  # model si názov vymyslel alebo upravil
                    continue
                info = self.resolve_name(mention.name)
                org_id = self.store(mention.name, "evidence", info, source_url=claim["url"])
                if org_id is None:
                    continue
                solution = claim["claim_type"] == "existing_solution"
                relation = "uses_solution" if solution else "mentioned_with_problem"
                fit = ({"používa riešenie": "C%d – historický problém alebo riešenie, nie potvrdená aktuálna potreba" % claim["id"]}
                       if solution else {"dôkaz o probléme": "áno (C%d)" % claim["id"]})
                if self.db.link_org_hypothesis(org_id, hyp["id"], relation, claim["id"], claim["url"], fit=fit):
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
                from .recipients import assess_procurement

                proc = assess_procurement(r.url, r.title, r.snippet, hyp["title"])
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
                    current = self.db.org_hypothesis(org_id, hyp["id"])
                    if current is not None and current["procurement_status"] == "confirmed" and proc["status"] != "confirmed":
                        continue  # potvrdený stav neprepíše slabší nález
                    self.db.set_org_hypothesis(
                        org_id, hyp["id"], procurement_status=proc["status"],
                        procurement_active=int(proc["status"] == "confirmed"), procurement_url=r.url,
                        procurement_subject=proc["subject"], procurement_date=proc["date"],
                        procurement_note=f"{proc['reason']} · ďalej: {proc['next_step']}")
                    self.stats[f"procurement_{proc['status']}"] += 1

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
        # Vyskúšané dopyty (M42): pôvodných 5 dopytov sa opakovalo pri každom behu a nové kancelárie nepribúdali
        # (H3: 6 realitných kancelárií); ďalší beh pokračuje mestami, vyskúšaný dopyt sa zopakuje po contact_research_days.
        key = f"segment_queries:{hyp['id']}"
        tried: dict[str, str] = self.db.get_setting(key, {}) or {}
        since = (datetime.now(timezone.utc) - timedelta(days=self.cfg.people.contact_research_days)).isoformat()
        plan = [q for q in segment_query_plan(flt, self.cfg.people.segment_expand_places) if tried.get(q, "") < since]
        for q in plan[: self.cfg.people.segment_queries_per_run]:
            if self.deadline.expired() or self._full(hyp["id"]):
                return
            try:
                results = self.searx.search(q, limit=8)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hľadanie organizácií '%s' zlyhalo: %s", q, exc)
                continue
            tried[q] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.db.set_setting(key, tried)
            self.stats["segment_queries"] += 1
            for r in results:
                if self.deadline.expired() or self._full(hyp["id"]):
                    return
                try:
                    self._segment_result(hyp, flt, r)
                except Exception as exc:  # noqa: BLE001 – jedna stránka alebo odpoveď registra nezastaví beh
                    log.warning("Kandidát %s preskočený: %s", r.url, exc)
                    self.stats["errors"] += 1

    def _segment_result(self, hyp, flt: SegmentFilter, r) -> None:
        if DIRECTORY_TITLE.search(r.title or ""):
            self.stats["segment_directory"] += 1  # katalóg firiem, nie web organizácie
            return
        org_id = self.org_from_result(r.url, "search")
        if org_id is not None:
            self._link_segment(hyp, flt, org_id, r.url)

    def org_from_result(self, result_url: str, source: str, allow_domains: list[str] | None = None) -> int | None:
        """Výsledok vyhľadávania → web organizácie → IČO na stránke → RPO → overená organizácia (R-ORG-1 bod 3,
        R-ORG-4). Bez IČO na webe, pri inej firme s týmto IČO alebo na webe katalógu či médií vráti None.
        allow_domains: domény z aggregator_domains, ktoré sú pri tomto hľadaní webom organizácie (napr. NKÚ)."""
        host = domain_of(result_url)
        if not host or domain_blocked(result_url, self.cfg.blocked_domains()):
            return None
        allowed = any(host == a or host.endswith("." + a) for a in (allow_domains or []))
        if not allowed and any(host == a or host.endswith("." + a) for a in self.cfg.people.aggregator_domains):
            return None
        url = root_url(result_url)
        if is_excluded(self.cfg, None, url):
            self.stats["excluded"] += 1
            return None
        known = self.db.find_organization_by_website(url)
        if known is not None:
            return None if known["excluded"] else int(known["id"])
        if url in self._no_ico:
            return None
        page = self.fetcher.fetch_html(url)
        if page.status != "ok":
            return None
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
            self._no_ico.add(url)
            log.info("Web bez IČO %s – kandidát vynechaný", domain_of(url))
            return None
        info = self.lookup_ico(icos[0])
        if info is None or info.terminated:
            return None
        if not name_on_page(info.name, visible_text(page.html)):
            self.stats["segment_name_mismatch"] += 1  # IČO patrí inej firme (napr. české IČO na .cz webe)
            return None
        return self.store(info.name, source, info, source_url=result_url, website=url,
                          verified_evidence=f"IČO {icos[0]} na {url}")

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
    excluded = db.automation_excluded_ids()  # M39/M40: automatika ich vynecháva (zadané ids áno)
    ids = least_recently_searched(db, hypothesis_ids or [i for i in select_people_hypotheses(cfg, db) if i not in excluded])
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
