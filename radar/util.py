"""Drobné pomocné funkcie zdieľané celým projektom."""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref_src|refererPlid)$", re.I)


def canonical_url(url: str) -> str:
    """URL bez sledovacích parametrov a kotvy – rovnaký článok z RSS a z vyhľadávania sa nezduplikuje."""
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)])
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.params, query, ""))


def awake_clock() -> float:
    """Sekundy, keď počítač nespal. Vo Windows QueryUnbiasedInterruptTime (bez spánku a hibernácie), inak
    monotonic. Reálny beh 12. 10. 2026: notebook v noci zaspal na 5 h, monotonic čas bežal ďalej a po
    prebudení sa všetky zvyšné kroky preskočili pre vypršaný limit."""
    if os.name == "nt":
        import ctypes

        value = ctypes.c_ulonglong()
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
            return value.value / 1e7
    return time.monotonic()


@contextmanager
def keep_awake():
    """Počas behu nedovolí Windows uspať počítač pre nečinnosť (zatvorenie veka alebo ručné uspanie áno)."""
    if os.name != "nt":
        yield
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(es_continuous)


class Deadline:
    """Časový rozpočet behu – kroky ho kontrolujú a včas skončia. Ráta sa iba čas, keď počítač nespal."""

    def __init__(self, minutes: float):
        self.end = awake_clock() + minutes * 60

    def expired(self) -> bool:
        return awake_clock() >= self.end

    def remaining_minutes(self) -> float:
        return max(0.0, (self.end - awake_clock()) / 60)

    def sub(self, minutes: float) -> "Deadline":
        """Kratší rozpočet pre jednu fázu (nikdy nepresiahne rozpočet celého behu)."""
        child = Deadline(0)
        child.end = min(self.end, awake_clock() + max(0.0, minutes) * 60)
        return child


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_thinking(text: str | None) -> str:
    """Odstráni bloky <think>…</think>, ktoré niektoré modely vkladajú do odpovede."""
    text = _THINK_RE.sub("", text or "")
    lower = text.lower()
    if "<think>" in lower:  # neuzavretý blok – model skončil uprostred premýšľania
        text = text[: lower.index("<think>")]
    return text.strip()


# E-maily a slovenské telefónne čísla – takéto citáty neukladáme (GDPR).
PERSONAL_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+|(?:\+421|00421|\b0)\s?\d{3}\s?\d{3}\s?\d{3}\b"
)


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Rozdelí text na časti s prekryvom; delí prednostne na koncoch odsekov a viet.

    Výsledok je deterministický, takže index časti (chunk_index) sa dá neskôr
    zopakovať pri overovaní citátu.
    """
    text = text or ""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def md_cell(text: object, max_len: int | None = None) -> str:
    """Bezpečný text do bunky Markdown tabuľky."""
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    if max_len and len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def md_url(url: str) -> str:
    return (url or "").replace(" ", "%20").replace(")", "%29").replace("(", "%28")


def fmt_num(x: float | None) -> str:
    """Číslo v slovenskom zápise (medzera ako oddeľovač tisícov, desatinná čiarka)."""
    if x is None:
        return "–"
    if abs(x) >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    return f"{x:.3g}".replace(".", ",")


LONG_COMMANDS = ("workflow", "run-all")


def preload_modules() -> list[str]:
    """Načíta všetky moduly balíka radar (okrem GUI) hneď pri štarte dlhého behu. Kroky inak importujú moduly až
    vtedy, keď na ne príde rad, a úprava kódu počas behu by zmiešala staré a nové verzie (reálny beh 12. 10. 2026:
    ImportError a chýbajúce pole konfigurácie v dvoch krokoch run-all). Beh tak používa kód z chvíle štartu."""
    import importlib
    import pkgutil

    import radar

    loaded = []
    for mod in pkgutil.walk_packages(radar.__path__, "radar."):
        if mod.name.startswith("radar.gui") or mod.name == "radar.__main__":
            continue
        importlib.import_module(mod.name)
        loaded.append(mod.name)
    return loaded


_TLS = None


def tls_context():
    """Overenie certifikátov cez úložisko operačného systému (truststore): Windows doplní chýbajúce medzicertifikáty,
    ktoré veľa slovenských úradných webov neposiela (SOPK, ÚRHH, EÚ SAV, pes.minv.sk – s balíkom certifi zlyhajú).
    Overovanie ostáva zapnuté; bez knižnice truststore predvolené overenie httpx."""
    global _TLS
    if _TLS is None:
        try:
            import ssl

            import truststore

            _TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            _TLS = True
    return _TLS

