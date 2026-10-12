"""Mená osôb a organizácií: tituly, normalizované kľúče na deduplikáciu, rod iba z výslovného textu."""

from __future__ import annotations

import re
import unicodedata

# Akademické a profesijné tituly (pred aj za menom). Poradie: dlhšie skôr.
TITLES = [
    "Ing. arch.", "Mgr. art.", "akad. mal.", "akad. soch.", "doc.", "prof.", "Ing.", "Mgr.", "Bc.", "PhDr.", "JUDr.",
    "MUDr.", "MVDr.", "MDDr.", "RNDr.", "PaedDr.", "PharmDr.", "ThDr.", "ThLic.", "PhMr.", "Dr.", "PhD.", "Ph.D.",
    "ArtD.", "CSc.", "DrSc.", "MBA", "MPH", "MSc.", "MSc", "LL.M.", "LL.M", "MA", "BA", "DiS.", "Dipl. Ing.", "Dipl.-Ing.",
    "dr. h. c.", "Dr.h.c.", "PhD", "DBA", "MPA",
]
_TITLE_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(TITLES, key=len, reverse=True)) + r")(?![\w])", re.I
)
_LEGAL_FORMS = [
    "spol. s r. o.", "spol. s r.o.", "s. r. o.", "s.r.o.", "a. s.", "a.s.", "k. s.", "k.s.", "v. o. s.", "v.o.s.",
    "š. p.", "š.p.", "n. o.", "n.o.", "o. z.", "o.z.", "družstvo", "príspevková organizácia", "rozpočtová organizácia",
    "z. p. o.", "z.p.o.", "s. e.", "SE", "GmbH", "Ltd.", "Inc.",
]

# Ženská podoba funkcie je výslovný údaj o rode (R-PPL-11); koncovka -ová sama nestačí.
FEMININE_ROLES = [
    "riaditeľka", "starostka", "primátorka", "vedúca", "konateľka", "manažérka", "predsedníčka", "zástupkyňa",
    "referentka", "účtovníčka", "špecialistka", "koordinátorka", "asistentka", "prednostka", "ekonómka", "riaditeľky",
    "námestníčka", "rektorka", "dekanka", "majiteľka", "zakladateľka", "poradkyňa", "analytička", "projektová manažérka",
    "nákupkyňa", "plánovačka", "technologička", "konzultantka", "hovorkyňa", "tajomníčka", "kontrolórka",
]
# Funkcie verejných funkcionárov na funkčné oslovenie (R-OUT-5).
PUBLIC_FUNCTIONS = {
    "starosta": ("Vážený pán starosta,", "male"), "starostka": ("Vážená pani starostka,", "female"),
    "primátor": ("Vážený pán primátor,", "male"), "primátorka": ("Vážená pani primátorka,", "female"),
    "predseda": ("Vážený pán predseda,", "male"), "predsedníčka": ("Vážená pani predsedníčka,", "female"),
    "rektor": ("Vážený pán rektor,", "male"), "rektorka": ("Vážená pani rektorka,", "female"),
    "prednosta": ("Vážený pán prednosta,", "male"), "prednostka": ("Vážená pani prednostka,", "female"),
}


def fold(text: str | None) -> str:
    """Malé písmená bez diakritiky, jedna medzera."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def split_titles(name: str) -> tuple[str, str]:
    """('Ján Novák', 'Ing. PhD.') z 'Ing. Ján Novák, PhD.'."""
    titles = [m.group(1) for m in _TITLE_RE.finditer(name or "")]
    clean = _TITLE_RE.sub(" ", name or "")
    clean = re.sub(r"[,;]+", " ", clean)
    clean = " ".join(clean.split()).strip(" .,-")
    return clean, " ".join(titles)


def person_name_key(name: str) -> str:
    """Meno bez titulov a diakritiky, malými písmenami (deduplikácia, zhoda mien z rôznych zdrojov)."""
    clean, _ = split_titles(name)
    return re.sub(r"[^a-z ]", "", fold(clean)).strip()


def org_name_key(name: str) -> str:
    """Názov organizácie bez právnej formy, interpunkcie a diakritiky."""
    text = name or ""
    for form in sorted(_LEGAL_FORMS, key=len, reverse=True):
        text = re.sub(r"(?<![\w])" + re.escape(form) + r"(?![\w])", " ", text, flags=re.I)
    text = fold(text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return " ".join(text.split())


_NOT_NAMES = {
    "obecný", "mestský", "úrad", "oddelenie", "odbor", "sekretariát", "kontakt", "kontakty", "riaditeľ", "riaditeľka",
    "slovenská", "republika", "bratislava", "košice", "žilina", "ulica", "námestie", "spoločnosť", "firma", "obec", "mesto",
    "nadace", "nadácia", "združenie", "ministerstvo", "fakulta", "univerzita", "sekcia", "centrum", "agentúra",
    "inštitút", "kancelária", "katedra", "ústav", "rada", "komora", "zväz", "asociácia", "partnerství", "partnerstvo",
    "oddělení", "služby", "podpora", "obchod", "marketing", "redakcia", "recepcia", "podateľňa", "ústredňa", "kontaktujte",
}


def _name_token(token: str) -> bool:
    """Slovo mena: „Novák“, „Nováková-Kráľová“ alebo priezvisko veľkými písmenami („NOVÁK“, časté na weboch úradov)."""
    parts = token.split("-")
    return all(p[:1].isupper() and p.isalpha() and (p[1:].islower() or (p.isupper() and len(p) >= 2)) for p in parts if p) and bool(token)


def looks_like_person_name(name: str) -> bool:
    """2–4 slová s veľkým začiatočným písmenom (aj priezvisko veľkými písmenami), bez slov typu 'Obecný úrad'."""
    clean, _ = split_titles(name)
    tokens = clean.split()
    if not 2 <= len(tokens) <= 4:
        return False
    if any(t.lower() in _NOT_NAMES for t in tokens):
        return False
    if all(t.isupper() for t in tokens):
        return False  # skratky („NASES SR“); aspoň krstné meno býva s malými písmenami
    return all(_name_token(t) for t in tokens)


def name_in_text(name: str, text: str) -> int:
    """Pozícia mena v texte (bez titulov, diakritiky a veľkosti písmen) alebo -1.
    Zohľadní aj poradie 'Priezvisko Meno'."""
    clean, _ = split_titles(name)
    hay = fold(text)
    needle = fold(clean)
    if not needle:
        return -1
    pos = hay.find(needle)
    if pos >= 0:
        return pos
    parts = needle.split()
    if len(parts) >= 2:
        reversed_name = " ".join(parts[1:] + parts[:1])
        return hay.find(reversed_name)
    return -1


def gender_hint(name: str, role: str | None, context: str) -> str | None:
    """'female' / 'male' iba z výslovného textu zdroja: 'pán'/'pani' pred menom alebo ženská podoba
    funkcie v zdroji. Inak None (oslovenie 'Dobrý deň,')."""
    clean, _ = split_titles(name)
    ctx = fold(context)
    surname = fold(clean.split()[-1]) if clean.split() else ""
    if surname:
        for m in re.finditer(r"\b(pani|pan)\s+(?:[a-z.]+\s+){0,3}" + re.escape(surname) + r"\b", ctx):
            return "female" if m.group(1) == "pani" else "male"
    role_f = fold(role)
    if role_f:
        for fem in FEMININE_ROLES:
            if re.search(r"\b" + re.escape(fold(fem)) + r"\b", role_f):
                return "female"
    return None


def title_key(role: str | None) -> str:
    return re.sub(r"[^a-z ]", " ", fold(role)).strip()


def public_salutation(role: str | None, gender: str | None) -> str | None:
    """Funkčné oslovenie verejného funkcionára pri overenej funkcii, napr. 'Vážená pani starostka,'."""
    words = set(fold(role).split())
    for function, (salutation, g) in PUBLIC_FUNCTIONS.items():
        # Mužská a ženská podoba funkcie sa v slovenčine líšia (starosta / starostka), preto je funkcia výslovný údaj.
        if fold(function) in words and (gender is None or gender == g):
            return salutation
    return None
