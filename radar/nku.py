"""Najvyšší kontrolný úrad SR – otvorené dáta (CC BY 4.0, https://www.nku.gov.sk/datasety, aktualizácia štvrťročne).

Kontrolné zistenia (znenie, typ nedostatku, porušený predpis, kontrolná akcia, dátumy kontroly, kontrolovaný subjekt)
sa spoja so zoznamom kontrolovateľných subjektov kvôli IČO – cez odkaz na subjekt (číslo záznamu), inak podľa názvu.
Adresa subjektu sa neukladá (minimalizácia). Výsledok je jedna tabuľka – štítky pre experimenty Brány 2
(napr. „zistenie NKÚ u objednávateľa do 2 rokov po podpise zmluvy“, operácia `lookup`).

Stránka so zoznamom správ (stránkovanie) je v robots.txt zakázaná; súbory datasetov (/documents/) a stránka
/datasety povolené sú.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from .config import Config
from .datasets import DatasetError, Download, download

log = logging.getLogger(__name__)

NKU_PAGE_RE = re.compile(r"^https?://(www\.)?nku\.gov\.sk/datasety/?$", re.I)
FILE_RE = re.compile(r'href="([^"]*?/(kontrolne_zistenia|zoznam_kontrolovatelnych_subj)_(\d{8})\.csv[^"]*)"', re.I)
ENTRY_RE = r"entryId=(\d+)"
# výstupný stĺpec → kľúčové slová v názve stĺpca (bez diakritiky, malými písmenami)
FINDING_COLS = {
    "subjekt": ("kontrolovany subjekt",), "subjekt_odkaz": ("linka", "subjekte"),
    "zaciatok_kontroly": ("zaciatok kontroly",), "koniec_kontroly": ("koniec kontroly",),
    "kontrolovane_obdobie": ("kontrolovane obdobie",), "typ_nedostatku": ("typ nedostatku",),
    "poruseny_predpis": ("poruseny predpis",), "cislo_ka": ("cislo ka",), "nazov_ka": ("nazov kontrolnej akcie",),
    "ucel_ka": ("ucel ka",), "znenie_zistenia": ("znenie zistenia",), "vysledok_url": ("linka", "vysledok"),
}
SUBJECT_COLS = {"nazov": ("nazov subjektu",), "odkaz": ("linka", "subjekte"), "ico": ("ico",)}


def is_nku_page(url: str) -> bool:
    return bool(NKU_PAGE_RE.match(url or ""))


def _ascii(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().split())


def latest_files(html: str) -> dict[str, str]:
    """Najnovší súbor zistení a zoznamu subjektov podľa dátumu v názve."""
    best: dict[str, tuple[str, str]] = {}
    for href, kind, day in FILE_RE.findall(html):
        href = href.replace("&amp;", "&")
        if href.startswith("/"):
            href = "https://www.nku.gov.sk" + href
        if kind not in best or day > best[kind][0]:
            best[kind] = (day, href)
    return {k: v[1] for k, v in best.items()}


def _pick(columns: list[str], spec: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out = {}
    for key, words in spec.items():
        match = next((c for c in columns if all(w in _ascii(c) for w in words)), None)
        if match is None and key not in ("subjekt_odkaz", "odkaz", "ucel_ka", "vysledok_url", "kontrolovane_obdobie"):
            raise DatasetError(f"NKÚ: chýba stĺpec {key} (dostupné: {', '.join(columns)[:300]})")
        if match:
            out[key] = match
    return out


def join_findings(findings: Path, subjects: Path, out: Path) -> dict[str, int]:
    """Zistenia + IČO subjektu → CSV (UTF-8). Vráti počty (zistenia, s IČO)."""
    import duckdb

    con = duckdb.connect()
    try:
        def cols(p: Path) -> list[str]:
            return [d[0] for d in con.execute("SELECT * FROM read_csv_auto(?, all_varchar=true, header=true) LIMIT 1",
                                              [str(p)]).description]

        q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        f, s = _pick(cols(findings), FINDING_COLS), _pick(cols(subjects), SUBJECT_COLS)
        con.execute(f"CREATE TABLE sub AS SELECT {q(s['nazov'])} AS nazov, "
                    + (f"regexp_extract({q(s['odkaz'])}, '{ENTRY_RE}', 1)" if "odkaz" in s else "NULL") + " AS entry, "
                    f"lpad(regexp_replace({q(s['ico'])}, '[^0-9]', '', 'g'), 8, '0') AS ico "
                    "FROM read_csv_auto(?, all_varchar=true, header=true)", [str(subjects)])
        sel = ", ".join(f"{q(src)} AS {dst}" for dst, src in f.items() if dst not in ("subjekt_odkaz",))
        entry = f"regexp_extract({q(f['subjekt_odkaz'])}, '{ENTRY_RE}', 1)" if "subjekt_odkaz" in f else "NULL"
        con.execute(f"CREATE TABLE zis AS SELECT {sel}, {entry} AS entry FROM read_csv_auto(?, all_varchar=true, header=true)",
                    [str(findings)])
        con.execute("""CREATE TABLE out AS SELECT
              COALESCE(a.ico, b.ico) AS ico, z.* EXCLUDE (entry, zaciatok_kontroly, koniec_kontroly),
              TRY_CAST(strptime(z.zaciatok_kontroly, '%d.%m.%Y') AS DATE) AS zaciatok_kontroly,
              TRY_CAST(strptime(z.koniec_kontroly, '%d.%m.%Y') AS DATE) AS koniec_kontroly
            FROM zis z
            LEFT JOIN (SELECT entry, any_value(ico) AS ico FROM sub WHERE entry != '' GROUP BY 1) a
                   ON a.entry = z.entry AND z.entry != ''
            LEFT JOIN (SELECT lower(trim(nazov)) AS n, any_value(ico) AS ico FROM sub GROUP BY 1) b
                   ON b.n = lower(trim(z.subjekt))""")
        n, with_ico = con.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE ico IS NOT NULL AND ico != '00000000') "
                                  "FROM out").fetchone()
        con.execute(f"COPY out TO '{str(out).replace(chr(39), chr(39) * 2)}' (HEADER, DELIMITER ';')")
        return {"zistenia": int(n), "s_ico": int(with_ico)}
    finally:
        con.close()


def download_nku(cfg: Config, fetcher, dest_dir: Path, blocked: list[str]) -> Download:
    page = fetcher.fetch_html("https://www.nku.gov.sk/datasety")
    files = latest_files(page.html or "")
    if set(files) != {"kontrolne_zistenia", "zoznam_kontrolovatelnych_subj"}:
        raise DatasetError("NKÚ: na stránke datasetov sa nenašli súbory zistení a subjektov")
    dest_dir.mkdir(parents=True, exist_ok=True)
    f = download(cfg, fetcher, files["kontrolne_zistenia"], dest_dir, blocked)
    s = download(cfg, fetcher, files["zoznam_kontrolovatelnych_subj"], dest_dir, blocked)
    out = dest_dir / "nku-zistenia.csv"
    try:
        counts = join_findings(f.path, s.path, out)
    finally:
        f.path.unlink(missing_ok=True)
        s.path.unlink(missing_ok=True)
    data = out.read_bytes()
    note = (f"NKÚ: {counts['zistenia']} kontrolných zistení, {counts['s_ico']} s IČO subjektu (spojené so zoznamom "
            "kontrolovateľných subjektov); adresy sa neukladajú")
    log.info(note)
    return Download(path=out, sha256=hashlib.sha256(data).hexdigest(), size=len(data), final_url=files["kontrolne_zistenia"],
                    content_type="text/csv", note=note)
