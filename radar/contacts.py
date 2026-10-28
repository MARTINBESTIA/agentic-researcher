"""Deterministická extrakcia kontaktov z HTML kontaktných stránok (R-PPL-4 až R-PPL-8).

- E-mail iba z odkazov mailto: a z viditeľného textu; normalizuje sa (malé písmená, bez medzier)
  a kontroluje sa syntax. Nikdy sa nehádá podľa vzoru ostatných adries.
- Obfuskované adresy („meno(at)firma.sk“, „[at]“, „zavináč“, Cloudflare email-protection) sa
  NEDEKÓDUJÚ: vznikne iba záznam hidden_contact s URL stránky a poznámkou „otvor ručne“.
- Telefón z odkazov tel: a cez phonenumbers.PhoneNumberMatcher(text, "SK"), ukladá sa v E.164.
- HTML sa spracuje v pamäti; ukladajú sa iba úryvky (≤ 300 znakov), v ktorých hodnota doslova je.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import lxml.html
import phonenumbers
from lxml import etree

from .names import fold, org_name_key

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
EMAIL_SYNTAX = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,24}$")
# Obfuskované zápisy: meno(at)firma.sk, meno [at] firma [dot] sk, meno zavináč firma.sk …
OBFUSCATED_RE = re.compile(
    r"[A-Za-z0-9._%+-]+\s*(?:\(\s*at\s*\)|\[\s*at\s*\]|\{\s*at\s*\}|\s+at\s+|\(\s*zavin[aá][cč]\s*\)|\[\s*zavin[aá][cč]\s*\]|\s+zavin[aá][cč]\s+|\s*\(@\)\s*)"
    r"\s*[A-Za-z0-9-]+\s*(?:\.|\(\s*dot\s*\)|\[\s*dot\s*\]|\s+dot\s+|\(\s*bodka\s*\)|\s+bodka\s+)\s*[A-Za-z]{2,10}",
    re.I,
)
GENERIC_LOCAL = {
    "info", "kontakt", "contact", "office", "podatelna", "sekretariat", "secretary", "obec", "mesto", "urad", "mail",
    "email", "admin", "recepcia", "reception", "predaj", "sales", "obchod", "objednavky", "orders", "support",
    "podpora", "servis", "service", "fakturacia", "faktury", "invoice", "invoices", "uctovnictvo", "accounting", "hr",
    "kariera", "career", "careers", "jobs", "praca", "marketing", "press", "media", "pr", "noreply", "no-reply",
    "webmaster", "web", "gdpr", "dpo", "zodpovednaosoba", "osobneudaje", "privacy", "hello", "ahoj", "team", "tim",
    "podnety", "staznosti", "reklamacie", "helpdesk", "technik", "it", "riaditel", "starosta", "primator", "prednosta",
    "matrika", "ekonom", "ekonomicky", "skola", "msu", "ou", "mu", "mestskyurad", "obecnyurad", "dispecing",
}
GENERIC_PHONE_HINTS = ("ústredňa", "ustredna", "spojovateľ", "sekretariát", "sekretariat", "recepcia", "informácie",
                       "podateľňa", "podatelna", "infolinka", "zákaznícka linka", "dispečing", "fax")
BLOCK_TAGS = {"tr", "li", "article", "p", "dd", "address", "figure"}  # td nie: meno a e-mail sú v rôznych bunkách riadku
CLASS_HINTS = re.compile(r"card|person|contact|kontakt|team|tim|member|employee|zamestnan|staff|profil|vcard|osoba|clen|vedenie|management|people|ludia", re.I)
ROLE_HINTS = re.compile(
    r"riadite|vedúc|vedeni|konate|starost|primátor|prednost|manažér|manager|head of|director|ceo|cfo|coo|cto|predsed|"
    r"zástupc|referent|špecialist|koordinátor|nákup|plánova|výrob|projektov|ekonóm|účtovn|odbor|oddeleni|sekci|tajomník|"
    r"kontrolór|rektor|dekan|námestník|majiteľ|zakladateľ|obchodn",
    re.I,
)
MAX_BLOCK_CHARS = 1200
SNIPPET_CHARS = 300
PAGE_SNIPPET_CHARS = 120  # úryvok ku generickému kontaktu stránky
HIDDEN_MARK = "[skrytý e-mail – otvor ručne]"
_UP = "A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ"
_LO = "a-záäčďéíĺľňóôŕšťúýž"
_NAME_LIKE = re.compile(rf"[{_UP}][{_LO}]+\s+[{_UP}][{_LO}]+")


def _strip_contacts(text: str) -> str:
    return EMAIL_RE.sub(" ", re.sub(r"<[^>]*>", " ", text))


@dataclass
class Block:
    """Najmenší blok HTML (riadok tabuľky, karta, odsek), ktorý obsahuje kontakt alebo funkciu."""

    index: int
    text: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)  # E.164
    hidden: bool = False
    profiles: list[str] = field(default_factory=list)  # URL LinkedIn z odkazov v bloku (kanonické)


@dataclass
class PageContacts:
    url: str
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    emails: dict[str, str] = field(default_factory=dict)  # e-mail -> úryvok
    phones: dict[str, str] = field(default_factory=dict)  # E.164 -> úryvok
    hidden_snippets: list[str] = field(default_factory=list)
    generic_phones: set[str] = field(default_factory=set)  # telefóny s označením ústredňa, sekretariát…
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolútna URL, text odkazu)
    profiles: dict[str, str] = field(default_factory=dict)  # URL LinkedIn -> úryvok (stránka sa nesťahuje)
    has_form: bool = False  # kontaktný formulár (form s poľom na správu); vypĺňa ho iba človek ručne


# ---------------------------------------------------------------- LinkedIn (iba URL, nikdy sa nesťahuje)
PROFILE_MARK = "<linkedin: {}>"
PROFILE_MARK_RE = re.compile(r"<linkedin: (https://www\.linkedin\.com/(?:in|company)/[^\s>]+)>")
_SLUG_RE = re.compile(r"[\w\-.%]{2,100}")


def normalize_profile_url(url: str | None) -> str | None:
    """Kanonická URL LinkedIn: https://www.linkedin.com/in/<slug> (osoba) alebo /company/<slug>
    (organizácia); bez parametrov, sledovacích kódov a jazykovej subdomény. Iné URL → None."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
    except ValueError:  # pokazený odkaz na stránke (napr. „http://[…“)
        return None
    if parsed.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    parts = [x for x in unquote(parsed.path).split("/") if x]
    if len(parts) < 2 or parts[0].lower() not in ("in", "company") or not _SLUG_RE.fullmatch(parts[1]):
        return None
    return f"https://www.linkedin.com/{parts[0].lower()}/{parts[1]}"


def is_person_profile(url: str) -> bool:
    return url.startswith("https://www.linkedin.com/in/")


def profiles_in(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(1), m.start(), m.end()) for m in PROFILE_MARK_RE.finditer(text or "")]


def normalize_email(value: str) -> str | None:
    email = unquote(value or "").strip().strip("<>").replace(" ", "").lower()
    email = email.split("?")[0]
    return email if EMAIL_SYNTAX.match(email) else None


def is_generic_email(email: str) -> bool:
    local = email.split("@", 1)[0]
    local_key = re.sub(r"[^a-z]", "", local)
    return local in GENERIC_LOCAL or local_key in GENERIC_LOCAL or local.split(".")[0] in GENERIC_LOCAL


SECOND_LEVEL_SUFFIXES = ("gov.sk", "co.uk", "org.uk", "com.au")  # registrovaná doména má o úroveň viac


def registered_domain(host: str) -> str:
    """tv.urad.sk → urad.sk; urad.gov.sk ostane (gov.sk je zóna úradov)."""
    parts = host.lower().removeprefix("www.").split(".")
    n = 3 if any(host.lower().endswith("." + s) for s in SECOND_LEVEL_SUFFIXES) else 2
    return ".".join(parts[-n:])


def _label(host: str) -> str:
    """Meno domény bez TLD a znakov: nemocnica-mesto.sk → nemocnicamesto."""
    return re.sub(r"[^a-z]", "", fold(registered_domain(host).split(".")[0]))


def is_org_email(email: str, org_name: str | None = None, website: str | None = None,
                 official: tuple[str, ...] | list[str] = ()) -> bool:
    """Adresa samotnej organizácie (M42, spresnené po kontrole na živých dátach 28. 10. 2026):
    - generická schránka (info@, podateľňa…) na jej doméne – aj subdoména a tá istá značka s inou koncovkou alebo
      pomlčkou (tv.urad.sk ↔ urad.sk, firma.biz ↔ firma.sk, nemocnica-mesto.sk ↔ nemocnicamesto.sk), doména
      s názvom firmy (abtechnika.sk pri „A & B technika“), pri verejnom subjekte aj úradná doména (`official`,
      napr. ministerstvo.sk ↔ odbor.gov.sk);
    - alebo schránka s názvom firmy či domény (firma@firma.sk, firmareality@gmail.com).
    Generická adresa inej organizácie na tej istej stránke (partner, dodávateľ webu, vzorová adresa vo formulári) nie."""
    local, _, domain = email.lower().partition("@")
    host = urlparse(website or "").netloc.lower().removeprefix("www.")
    names = set()
    if org_name:
        words = org_name_key(org_name).split()
        names = {"".join(words), *(w for w in words if len(w) >= 5)}
    names = {k for k in (re.sub(r"[^a-z]", "", fold(n)) for n in names) if len(k) >= 4}
    site_label, mail_label = (_label(host) if host else ""), _label(domain)
    if is_generic_email(email):
        if not host or registered_domain(domain) == registered_domain(host):
            return True
        if (len(site_label) >= 3 and mail_label == site_label) or mail_label in names:
            return True
        return any(domain == d or domain.endswith("." + d) for d in official)
    local_key = re.sub(r"[^a-z]", "", fold(local))
    return any(k in local_key for k in names | ({site_label} if len(site_label) >= 4 else set()))


def org_email_ok(org, email: str, official_domains: tuple[str, ...] | list[str] = ()) -> bool:
    """Patrí e-mail tejto organizácii? Úradné domény sa uznajú iba pri verejnom subjekte alebo neznámom sektore."""
    sector = org["sector_kind"] if "sector_kind" in org.keys() else None
    return is_org_email(email, org["name"], org["website"], official_domains if sector in (None, "public") else ())


def _inside_url(text: str, start: int) -> bool:
    """Číslice vnútri URL (napr. /in/jan-novak-905123456 alebo ?id=0905…) nie sú zverejnený telefón."""
    token_start = max(text.rfind(" ", 0, start), text.rfind("\n", 0, start), text.rfind("<", 0, start)) + 1
    token = text[token_start:start]
    return "://" in token or token.startswith("www.")


def phones_in(text: str) -> list[tuple[str, int, int]]:
    """(E.164, začiatok, koniec) pre telefóny v texte (región SK)."""
    found = []
    for m in phonenumbers.PhoneNumberMatcher(text or "", "SK"):
        if _inside_url(text, m.start):
            continue
        found.append((phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164), m.start, m.end))
    return found


def normalize_phone(value: str) -> str | None:
    try:
        number = phonenumbers.parse(unquote(value or "").strip(), "SK")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def emails_in(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in EMAIL_RE.finditer(text or ""):
        email = normalize_email(m.group(0))
        if email:
            out.append((email, m.start(), m.end()))
    return out


def snippet_around(text: str, start: int, end: int, size: int = SNIPPET_CHARS) -> str:
    """Úryvok ≤ size znakov, ktorý obsahuje text[start:end]."""
    if len(text) <= size:
        return text
    pad = max(0, (size - (end - start)) // 2)
    a = max(0, start - pad)
    b = min(len(text), a + size)
    a = max(0, b - size)
    return text[a:b]


def value_in_snippet(kind: str, value: str, snippet: str) -> bool:
    """R-PPL-6: uložená hodnota musí byť (po normalizácii) doslova v úryvku."""
    if kind == "email":
        return value in {e for e, _, _ in emails_in(snippet)}
    if kind == "phone":
        return value in {p for p, _, _ in phones_in(snippet)}
    return value in snippet


def _clean_text(el) -> str:
    return " ".join(" ".join(el.itertext()).split())


def visible_text(html: str) -> str:
    """Text stránky bez skriptov a štýlov (napr. na hľadanie IČO alebo názvu)."""
    if not html:
        return ""
    try:
        doc = lxml.html.fromstring(html)
    except (etree.ParserError, ValueError):
        return re.sub(r"<[^>]+>", " ", html)
    for el in doc.xpath("//script|//style|//noscript|//template"):
        if el.getparent() is not None:
            el.drop_tree()
    return _clean_text(doc)


def _prepare(doc) -> bool:
    """Upraví strom v pamäti: odstráni skripty, cieľ mailto:/tel: odkazov vloží do textu (aby bola
    hodnota v úryvku doslova) a Cloudflare ochranu nahradí značkou bez dekódovania. Vráti, či bola
    na stránke skrytá adresa."""
    hidden = False
    for el in doc.xpath("//script|//style|//noscript|//template|//svg"):
        if el.getparent() is not None:
            el.drop_tree()
    for el in doc.xpath("//*[@data-cfemail] | //a[contains(@href, '/cdn-cgi/l/email-protection')]"):
        hidden = True
        tail = el.tail
        el.clear()
        el.text = f" {HIDDEN_MARK} "
        el.tail = tail
    for a in doc.xpath("//a[@href]"):
        href = a.get("href") or ""
        low = href.lower()
        if low.startswith("mailto:"):
            email = normalize_email(href[7:])
            text = _clean_text(a)
            if email and email not in text.lower():
                a.tail = f" <{email}>" + (a.tail or "")
        elif low.startswith("tel:"):
            raw = unquote(href[4:]).strip()
            text = _clean_text(a)
            if raw and fold(re.sub(r"\D", "", raw)) not in re.sub(r"\D", "", text):
                a.tail = f" <tel:{raw}>" + (a.tail or "")
        else:
            profile = normalize_profile_url(href)
            if profile:  # odkaz na LinkedIn z webu organizácie: URL doslova do textu bloku (ikona nemá text)
                a.tail = " " + PROFILE_MARK.format(profile) + " " + (a.tail or "")
    return hidden


def _candidate_blocks(doc) -> list:
    els = []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        cls = f"{el.get('class') or ''} {el.get('id') or ''}"
        if tag in BLOCK_TAGS or (tag in ("div", "section", "span") and CLASS_HINTS.search(cls)):
            els.append(el)
    return els


def extract_contacts(html: str, url: str) -> PageContacts:
    page = PageContacts(url=url)
    if not html or not html.strip():
        return page
    try:
        doc = lxml.html.fromstring(html)
    except (etree.ParserError, ValueError):
        return page
    title_el = doc.find(".//title")
    page.title = _clean_text(title_el) if title_el is not None else ""
    page.has_form = any(form.xpath(".//textarea") for form in doc.xpath("//form"))
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if href and not href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            try:
                page.links.append((urljoin(url, href), _clean_text(a)[:120]))
            except ValueError:  # pokazený odkaz (napr. „http://[…“) nezahodí celú stránku
                continue
    hidden_page = _prepare(doc)
    body = doc.find(".//body")
    full_text = _clean_text(body if body is not None else doc)
    # Skryté adresy sa nedekódujú a do žiadneho úryvku sa nedostanú (ani v obfuskovanej podobe).
    masked = OBFUSCATED_RE.sub(HIDDEN_MARK, full_text)

    # Kontakty na celej stránke (generické sa uložia k organizácii); krátke úryvky, nie celý zoznam ľudí.
    for email, s, e in emails_in(masked):
        page.emails.setdefault(email, snippet_around(masked, s, e, size=PAGE_SNIPPET_CHARS))
    for phone, s, e in phones_in(masked):
        page.phones.setdefault(phone, snippet_around(masked, s, e, size=PAGE_SNIPPET_CHARS))
        before = fold(masked[max(0, s - 40):s])
        if any(fold(h) in before for h in GENERIC_PHONE_HINTS):
            page.generic_phones.add(phone)  # ústredňa, sekretariát… tesne pred číslom
    if hidden_page or masked != full_text:
        for m in re.finditer(re.escape(HIDDEN_MARK), masked):
            page.hidden_snippets.append(snippet_around(masked, m.start(), m.end(), size=PAGE_SNIPPET_CHARS))
    for profile, s, e in profiles_in(masked):
        page.profiles.setdefault(profile, snippet_around(masked, s, e, size=max(PAGE_SNIPPET_CHARS, e - s + 40)))

    # Bloky s kontaktom alebo funkciou. Od najhlbších: obal (napr. celý tím) sa preskočí, ak jeho
    # kontakty pokrývajú vnútorné bloky s menom (karty osôb); inak obal nahradí vnútorné bloky
    # (napr. karta s menom v nadpise a odsekom „E-mail: …“).
    candidates = []
    for el in _candidate_blocks(doc):
        text = _clean_text(el)
        if not text or len(text) > MAX_BLOCK_CHARS:
            continue
        emails = [e for e, _, _ in emails_in(text)]
        phones = [p for p, _, _ in phones_in(text)]
        profiles = [u for u, _, _ in profiles_in(text)]
        hidden = bool(OBFUSCATED_RE.search(text)) or HIDDEN_MARK in text
        if emails or phones or hidden or profiles or ROLE_HINTS.search(text):
            depth = sum(1 for _ in el.iterancestors())
            candidates.append({"el": el, "text": text, "emails": emails, "phones": phones, "hidden": hidden,
                               "profiles": profiles, "depth": depth,
                               "named": bool(_NAME_LIKE.search(_strip_contacts(text)))})
    kept: list[dict] = []
    for cand in sorted(candidates, key=lambda c: -c["depth"]):
        inner = [k for k in kept if any(anc is cand["el"] for anc in k["el"].iterancestors())]
        contacts = set(cand["emails"]) | set(cand["phones"]) | set(cand["profiles"])
        covered = (set().union(*(set(k["emails"]) | set(k["phones"]) | set(k["profiles"]) for k in inner))
                   if inner else set())
        if inner and contacts <= covered and all(k["named"] for k in inner):
            continue
        kept = [k for k in kept if k not in inner] + [cand]
    order = {id(el): i for i, el in enumerate(doc.iter())}
    chosen = [(k["text"], k["emails"], k["phones"], k["hidden"], k["profiles"])
              for k in sorted(kept, key=lambda k: order.get(id(k["el"]), 0))]
    seen: set[str] = set()
    for text, emails, phones, hidden, profiles in chosen:
        if text in seen:
            continue
        seen.add(text)
        if hidden:
            text = OBFUSCATED_RE.sub(HIDDEN_MARK, text)
        page.blocks.append(Block(index=len(page.blocks), text=text, emails=sorted(set(emails)), phones=sorted(set(phones)),
                                 hidden=hidden, profiles=list(dict.fromkeys(profiles))))
    return page


CONTACT_PAGE_KEYWORDS = (
    "kontakt", "kontakty", "contact", "contacts", "onas", "about", "vedenie", "manazment", "management", "team", "tim",
    "struktura", "urad", "zamestnanci", "impressum", "ludia", "people", "kontaktujte",
)
CONTACT_PAGE_BIGRAMS = (("o", "nas"), ("organizacna", "struktura"), ("nas", "tim"), ("about", "us"))


def is_contact_link(text: str) -> bool:
    """Celé slová (nie podreťazce: „tim“ nie je v „optimalizácia“)."""
    tokens = [t for t in re.split(r"[^a-z]+", fold(text)) if t]
    if any(t in CONTACT_PAGE_KEYWORDS for t in tokens):
        return True
    return any(pair in CONTACT_PAGE_BIGRAMS for pair in zip(tokens, tokens[1:]))


def contact_page_links(page: PageContacts, base_url: str, limit: int = 5) -> list[str]:
    """Odkazy z úvodnej stránky na kontaktné stránky tej istej domény (R-PPL-1)."""
    host = (urlparse(base_url).hostname or "").removeprefix("www.")
    out: list[str] = []
    for href, text in page.links:
        try:
            parsed = urlparse(href)
            link_host = (parsed.hostname or "").removeprefix("www.")
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if link_host != host:
            continue
        if is_contact_link(parsed.path + " " + text):
            clean = href.split("#")[0]
            if clean not in out and clean.rstrip("/") != base_url.rstrip("/"):
                out.append(clean)
        if len(out) >= limit:
            break
    return out


def sitemap_contact_links(xml: str, base_url: str, limit: int = 5) -> list[str]:
    host = (urlparse(base_url).hostname or "").removeprefix("www.")
    out = []
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I):
        try:
            parsed = urlparse(loc)
            loc_host = (parsed.hostname or "").removeprefix("www.")
        except ValueError:
            continue
        if loc_host != host:
            continue
        if is_contact_link(parsed.path) and loc not in out:
            out.append(loc)
        if len(out) >= limit:
            break
    return out
