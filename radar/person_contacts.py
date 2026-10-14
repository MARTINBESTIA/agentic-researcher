"""Kontakty ku konkrétnej osobe (krok `contacts`, bez modelu).

Zber ľudí berie kontakty z najviac 5 kontaktných stránok webu organizácie. Štatutári z RPO a ľudia
citovaní v dôkazoch tam často nie sú (reálny stav 11. 10. 2026: 24 z 38 vybraných ľudí bez osobného
kontaktu). Tento krok pre každého vybraného človeka bez osobného e-mailu a telefónu:

1. v sitemap.xml webu organizácie nájde stránky, ktorých adresa obsahuje priezvisko (bez vyhľadávača),
   potom vyhľadá `"meno" site:web-organizácie`, `"meno" "organizácia" kontakt` a PDF na webe organizácie
   (telefónne zoznamy, organizačné poriadky; z PDF sa berú okná textu okolo mena),
2. stiahne iba stránky z overeného webu jeho organizácie alebo z úradných domén (people.official_domains),
3. kontakt uloží, iba ak je v tom istom bloku HTML ako meno, najbližšie meno pred kontaktom je práve
   táto osoba (zoznam viacerých ľudí), vzdialenosť ≤ 400 znakov a hodnota je doslova v úryvku.

E-maily sa nehádajú, obfuskované adresy sa nedekódujú (iba „skrytý kontakt – otvor ručne“), LinkedIn
a sociálne siete sa nesťahujú. Pred uložením kontrola zoznamu potlačenia. Nič sa neodosiela.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

from .config import Config
from .contacts import HIDDEN_MARK, emails_in, extract_contacts, is_generic_email, phones_in, snippet_around
from .names import fold
from .db import DB
from .names import looks_like_person_name, name_in_text, split_titles
from .privacy import require_lia
from .util import Deadline, domain_of

log = logging.getLogger(__name__)

WINDOW = 400
_UP, _LO = "A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ", "a-záäčďéíĺľňóôŕšťúýž"
# Mená v bloku (aj „Meno PRIEZVISKO“ z webov úradov): kontakt patrí poslednému menu pred ním.
OTHER_NAME_RE = re.compile(rf"\b[{_UP}][{_LO}]+\s+(?:[{_UP}][{_LO}]+|[{_UP}]{{2,}})\b")


def allowed_domain(url: str, org_domain: str | None, official: list[str]) -> bool:
    host = domain_of(url)
    if org_domain and (host == org_domain or host.endswith("." + org_domain)):
        return True
    return any(host == d or host.endswith("." + d) for d in official)


def _positions(text: str, kind: str, value: str) -> list[int]:
    if kind == "phone":
        return [s for v, s, _ in phones_in(text) if v == value]
    low = text.lower()
    return [m.start() for m in re.finditer(re.escape(value.lower()), low)]


def contacts_of_name(text: str, name: str, emails: list[str], phones: list[str]) -> list[tuple[str, str, str]]:
    """(druh, hodnota, úryvok) kontaktov v bloku, ktoré patria práve tomuto menu (deterministicky)."""
    pos = name_in_text(name, text)
    if pos < 0:
        return []
    end = pos + len(split_titles(name)[0])
    names = sorted({m.start() for m in OTHER_NAME_RE.finditer(text) if looks_like_person_name(m.group(0))
                    and not pos <= m.start() < end} | {pos})
    out = []
    for kind, values in (("email", [e for e in emails if not is_generic_email(e)]), ("phone", phones)):
        for value in values:
            for p in _positions(text, kind, value):
                preceding = [q for q in names if q <= p]
                owner = max(preceding) if preceding else min(names, key=lambda q: abs(q - p))
                if owner == pos and abs(p - pos) <= WINDOW:
                    a, b = min(pos, p), max(end, p + len(value))
                    out.append((kind, value, snippet_around(text, a, b, size=max(300, b - a + 20))))
                    break
    if HIDDEN_MARK in text:
        h = text.find(HIDDEN_MARK)
        preceding = [q for q in names if q <= h]
        if preceding and max(preceding) == pos and abs(h - pos) <= WINDOW:
            out.append(("hidden_contact", "", snippet_around(text, pos, h + len(HIDDEN_MARK))))
    return out


LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
DOMAIN_INDEX_DAYS = 30
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")


def person_links(urls: list[str], org_domain: str, name: str, limit: int = 3) -> list[str]:
    """Adresy webu organizácie, ktorých cesta obsahuje priezvisko (napr. /urad/jana-novakova)."""
    surname = fold(split_titles(name)[0]).split()[-1] if split_titles(name)[0] else ""
    if len(surname) < 3:
        return []
    out = []
    for loc in urls:
        if domain_of(loc) == org_domain and surname in fold(loc.replace("-", " ").replace("_", " ")):
            out.append(loc)
        if len(out) >= limit:
            break
    return out


def sitemap_person_links(xml: str, org_domain: str, name: str, limit: int = 3) -> list[str]:
    """URL zo sitemap.xml webu organizácie, ktorých cesta obsahuje priezvisko (napr. /urad/jana-novakova)."""
    return person_links(LOC_RE.findall(xml or ""), org_domain, name, limit)


def domain_index(db: DB, fetcher, org_domain: str, max_sitemaps: int = 12, max_urls: int = 5000) -> list[str]:
    """Lokálny index webu organizácie: adresy zo sitemap (aj z robots.txt a vnorených sitemap, napr. WordPress),
    v cache 30 dní a spoločný pre všetkých ľudí organizácie – bez vyhľadávača, ktorý býva blokovaný. Predtým sa
    pri každej osobe sťahovala iba /sitemap.xml (vnorené sitemapy sa nečítali)."""
    key = f"domain-index:{org_domain}"
    cached = db.cache_get(key, DOMAIN_INDEX_DAYS)
    if cached is not None:
        return json.loads(cached)
    sitemaps = getattr(fetcher, "sitemaps", None)
    queue = list(sitemaps(f"https://{org_domain}/") if sitemaps else []) + [f"https://{org_domain}{p}" for p in SITEMAP_PATHS]
    seen: set[str] = set()
    urls: list[str] = []
    while queue and len(seen) < max_sitemaps and len(urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen or domain_of(sm) != org_domain:
            continue
        seen.add(sm)
        page = fetcher.fetch_html(sm)
        if page.status != "ok":
            continue
        locs = [u for u in LOC_RE.findall(page.html or "") if domain_of(u) == org_domain]
        if "<sitemapindex" in (page.html or "").lower():
            queue += locs
        else:
            urls += locs
    urls = list(dict.fromkeys(urls))[:max_urls]
    db.cache_put(key, json.dumps(urls))
    return urls


def text_windows(text: str, name: str, radius: int = WINDOW) -> list[str]:
    """Okná textu okolo výskytov mena (PDF nemá bloky HTML; pravidlo „najbližšie meno pred kontaktom“ platí ďalej)."""
    from .names import fold

    hay, needle = fold(text), fold(split_titles(name)[0])
    out, start = [], 0
    while needle and (pos := hay.find(needle, start)) >= 0 and len(out) < 5:
        out.append(text[max(0, pos - 80): pos + len(needle) + radius])
        start = pos + len(needle)
    return out


def is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")


def has_personal_contact(db: DB, person_id: int) -> bool:
    for c in db.contacts_for_person(person_id):
        if c["kind"] == "phone" or (c["kind"] == "email" and not is_generic_email(c["value"])):
            return True
    return False


def find_person_contacts(cfg: Config, db: DB, searx, fetcher, person, deadline: Deadline, saver) -> Counter:
    stats: Counter = Counter()
    org = db.get_organization(person["org_id"]) if person["org_id"] else None
    org_domain = domain_of(org["website"]) if org is not None and org["website_verified"] and org["website"] else None
    official = cfg.people.official_domains
    name = person["full_name"]
    queries = ([f'"{name}" site:{org_domain}'] if org_domain else []) + (
        [f'"{name}" "{org["name"]}" kontakt'] if org is not None else [f'"{name}" kontakt']) + (
        [f'"{name}" site:{org_domain} filetype:pdf'] if org_domain else [])  # telefónne zoznamy, organizačné poriadky
    urls: list[str] = []
    if org_domain:  # lokálny index webu nepotrebuje vyhľadávač (ten býva preťažený alebo blokovaný)
        index = domain_index(db, fetcher, org_domain)
        stats["index_urls"] += len(index)
        urls += person_links(index, org_domain, name)
    for q in queries:
        try:
            results = searx.search(q, limit=8)
        except Exception as exc:  # noqa: BLE001 – vyhľadávač nedostupný: osoba sa skúsi pri ďalšom behu
            log.warning("Hľadanie kontaktu zlyhalo: %s", exc)
            continue
        urls += [r.url for r in results if allowed_domain(r.url, org_domain, official) and r.url not in urls]
    found_any = False
    for url in urls[: cfg.people.max_contact_pages_per_person]:
        if deadline.expired():
            break
        stats["pages"] += 1
        if is_pdf_url(url):  # PDF: text z pypdf, okná okolo mena namiesto blokov HTML
            doc = fetcher.fetch(url)
            if doc is None:
                continue
            for window in text_windows(doc.text, name):
                emails = [e for e, _, _ in emails_in(window)]
                phones = [p for p, _, _ in phones_in(window)]
                for kind, value, snippet in contacts_of_name(window, name, emails, phones):
                    if kind != "hidden_contact":
                        saver(person["id"], person["org_id"], kind, value, doc.final_url or url, snippet, "person_pdf")
                        stats[f"found_{kind}"] += 1
                        found_any = True
            if found_any:
                break
            continue
        page = fetcher.fetch_html(url)
        if page.status != "ok":
            continue
        contacts = extract_contacts(page.html, page.final_url or url)
        for block in contacts.blocks:
            for kind, value, snippet in contacts_of_name(block.text, name, block.emails, block.phones):
                if kind == "hidden_contact":
                    saver(person["id"], person["org_id"], kind, page.final_url or url, page.final_url or url, snippet,
                          "obfuscated")
                else:
                    saver(person["id"], person["org_id"], kind, value, page.final_url or url, snippet, "person_page")
                stats[f"found_{kind}"] += 1
                found_any = True
        if found_any:
            break  # stačí jedna stránka s kontaktom osoby
    stats["people_found" if found_any else "people_not_found"] += 1
    return stats


def run_person_contacts(cfg: Config, db: DB, searx, fetcher, deadline: Deadline,
                        hypothesis_ids: list[int] | None = None) -> dict:
    """Kontakty k vybraným ľuďom (väzba s prioritou), ktorí ešte nemajú osobný e-mail ani telefón."""
    from .people import PeopleCollector

    require_lia(cfg)
    collector = PeopleCollector(cfg, db, None, searx, fetcher, deadline)
    stats: Counter = Counter()
    ids = hypothesis_ids or [h["id"] for h in db.people_enabled_hypotheses()]
    seen: set[int] = set()
    for hid in ids:
        for link in db.links_for_hypothesis(hid):
            if deadline.expired():
                return dict(stats + collector.stats)
            pid = link["person_id"]
            if link["priority"] is None or pid in seen or link["person_status"] == "excluded":
                continue
            seen.add(pid)
            if has_personal_contact(db, pid):
                stats["already_has_contact"] += 1
                continue
            stats.update(find_person_contacts(cfg, db, searx, fetcher, db.get_person(pid), deadline,
                                              collector.save_contact))
    return dict(stats + collector.stats)
