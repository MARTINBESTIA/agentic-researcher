"""Centrálny register zmlúv (CRZ): denné zmenové dávky https://www.crz.gov.sk/export/RRRR-MM-DD.zip.

Národný katalóg uvádza ako distribúciu iba adresár /export/ (výpis je zakázaný, HTTP 403); súbory majú dátum v
názve. Dávka obsahuje zmluvy zmenené v daný deň (aj staré), preto sa záznamy zlúčia podľa ID (platí posledná zmena).

Minimalizácia: ukladajú sa iba stĺpce potrebné na analýzu zmlúv (objednávateľ a jeho IČO, IČO dodávateľa, predmet,
sumy, dátumy, rezort, typ). Meno dodávateľa (zs2 – často fyzická osoba) ani adresy sa neukladajú.
robots.txt žiada crawl-delay 10 s – medzi súbormi sa čaká.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from lxml import etree

from .config import Config
from .datasets import DatasetError, Download, download
from .util import Deadline

log = logging.getLogger(__name__)

CRZ_EXPORT_DIR_RE = re.compile(r"^https?://(www\.)?crz\.gov\.sk/export/?$", re.I)
CRZ_FILE = "https://www.crz.gov.sk/export/{day}.zip"
FIELDS = ("ID", "nazov", "zs1", "ico1", "ico", "predmet", "datum", "datum_ucinnost", "datum_platnost_do",
          "datum_zverejnene", "suma_zmluva", "suma_spolu", "rezort", "typ", "druh", "stav", "uvo")
EMPTY_DATES = {"0000-00-00", "0000-00-00 00:00:00", ""}
CONTROL_CHARS = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Zrozumiteľné názvy stĺpcov (kód rozpozná IČO podľa názvu, model vidí význam): zs1 = objednávateľ, ico = dodávateľ.
RENAME = {"zs1": "objednavatel", "ico1": "ico_objednavatel", "ico": "ico_dodavatel", "nazov": "cislo_zmluvy"}


def is_crz_export(url: str) -> bool:
    return bool(CRZ_EXPORT_DIR_RE.match(url or ""))


def contracts(xml: bytes) -> list[dict]:
    """Zmluvy z dávky: iba vybrané polia (bez mien a adries), prázdne dátumy → None. Bez externých entít."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    # Riadiace znaky, ktoré XML nepovoľuje (reálna dávka 2026-01-20 mala \x02 v texte) – inak by vypadol celý deň.
    root = etree.fromstring(CONTROL_CHARS.sub(b"", xml), parser)
    out = []
    for z in root.iter("zmluva"):
        row: dict = {}
        for name in FIELDS:
            el = z.find(name)  # priame dieťa (nie <nazov> prílohy)
            value = (el.text or "").strip() if el is not None else ""
            row[name] = None if (name.startswith("datum") and value in EMPTY_DATES) or value == "" else value
        for name in ("suma_zmluva", "suma_spolu"):
            try:
                row[name] = float(row[name].replace(",", ".")) if row[name] is not None else None
            except ValueError:
                row[name] = None
        if row.get("ID"):
            out.append({RENAME.get(k, k): v for k, v in row.items()})
    return out


def download_crz(cfg: Config, fetcher, dest_dir: Path, blocked: list[str], days: int, deadline: Deadline | None = None,
                 end: date | None = None, sleep: Callable[[float], None] = time.sleep) -> Download:
    """Dávky za posledných `days` dní → JSONL s poľom _den (deň dávky); zlúčenie podľa ID robí `dedupe_latest` po
    importe (v DuckDB – stovky tisíc zmlúv by v pamäti Pythonu zabrali stovky MB). Chýbajúci deň (404) sa preskočí."""
    end = end or date.today() - timedelta(days=1)
    digest = hashlib.sha256()
    got = missing = rows = total = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "crz-export.jsonl"
    with open(out, "w", encoding="utf-8") as fh:
        for i in range(days, 0, -1):
            if deadline is not None and deadline.expired():
                break
            day = (end - timedelta(days=i - 1)).isoformat()
            if got or missing:
                sleep(cfg.gate1.crz_delay_s)
            try:
                dl = download(cfg, fetcher, CRZ_FILE.format(day=day), dest_dir, blocked)
            except DatasetError as exc:
                missing += 1
                log.info("CRZ %s: %s", day, exc)
                continue
            try:
                data = dl.path.read_bytes()
                total += len(data)
                if total > cfg.gate1.max_dataset_mb * 1024 * 1024:
                    raise DatasetError(f"dávky CRZ presiahli limit {cfg.gate1.max_dataset_mb} MB")
                digest.update(data)
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    member = max(zf.infolist(), key=lambda m: m.file_size)
                    if member.file_size > cfg.gate1.max_dataset_mb * 1024 * 1024:
                        raise DatasetError("dávka CRZ presahuje limit veľkosti")
                    for row in contracts(zf.read(member)):
                        fh.write(json.dumps({**row, "_den": day}, ensure_ascii=False) + "\n")
                        rows += 1
                got += 1
            except (zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
                missing += 1
                log.warning("CRZ %s: poškodená dávka (%s)", day, exc)
            finally:
                dl.path.unlink(missing_ok=True)
    if not rows:
        out.unlink(missing_ok=True)
        raise DatasetError(f"CRZ: žiadna dávka sa nepodarila ({missing} chýba)")
    note = (f"CRZ: {got} denných dávok ({missing} chýba), {rows} záznamov pred zlúčením podľa ID; bez mien "
            "dodávateľov a adries (minimalizácia)")
    return Download(path=out, sha256=digest.hexdigest(), size=total, final_url=CRZ_FILE.format(day="RRRR-MM-DD"),
                    content_type="application/json", note=note)


def dedupe_latest(cfg: Config, hid: int, table: str) -> int:
    """Zmluva zmenená vo viacerých dňoch: ostane posledná zmena (podľa _den). Vráti počet zmlúv."""
    import duckdb

    from .datasets import duck_path

    if not re.fullmatch(r"ds\d+", table):
        raise DatasetError("neplatný názov tabuľky")
    con = duckdb.connect(str(duck_path(cfg, hid)))
    try:
        con.execute(f'CREATE OR REPLACE TABLE {table} AS SELECT * EXCLUDE (_den) FROM {table} '
                    f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "ID" ORDER BY _den DESC) = 1')
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def append_days(cfg: Config, fetcher, hid: int, table: str, days: list[str], blocked: list[str],
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Doplní konkrétne dni (napr. dávky, ktoré predtým zlyhali) do existujúcej tabuľky: pridá iba zmluvy, ktoré
    v nej ešte nie sú (verziu už uloženej zmluvy nemožno porovnať – deň dávky sa po zlúčení neukladá)."""
    import duckdb

    from .datasets import duck_path, hyp_dir

    if not re.fullmatch(r"ds\d+", table):
        raise DatasetError("neplatný názov tabuľky")
    tmp = hyp_dir(cfg, hid) / "crz-append.jsonl"
    got, failed, n = [], [], 0
    with open(tmp, "w", encoding="utf-8") as fh:
        for i, day in enumerate(days):
            if i:
                sleep(cfg.gate1.crz_delay_s)
            try:
                dl = download(cfg, fetcher, CRZ_FILE.format(day=day), hyp_dir(cfg, hid), blocked)
            except DatasetError as exc:
                failed.append(day)
                log.info("CRZ %s: %s", day, exc)
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(dl.path.read_bytes())) as zf:
                    member = max(zf.infolist(), key=lambda m: m.file_size)
                    for row in contracts(zf.read(member)):
                        fh.write(json.dumps({**row, "_den": day}, ensure_ascii=False) + "\n")
                        n += 1
                got.append(day)
            except (zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
                failed.append(day)
                log.warning("CRZ %s: poškodená dávka (%s)", day, exc)
            finally:
                dl.path.unlink(missing_ok=True)
    added = 0
    if n:
        con = duckdb.connect(str(duck_path(cfg, hid)))
        try:
            before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            target = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
            source = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_json_auto(?)", [str(tmp)]).fetchall()}
            cols = ", ".join('"' + c.replace('"', '""') + '"' for c in target if c in source)  # spoločné stĺpce
            con.execute(f'INSERT INTO {table} ({cols}) SELECT {cols} FROM read_json_auto(?) '
                        f'WHERE CAST("ID" AS VARCHAR) NOT IN (SELECT CAST("ID" AS VARCHAR) FROM {table}) '
                        f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "ID" ORDER BY _den DESC) = 1', [str(tmp)])
            added = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - before
        finally:
            con.close()
    tmp.unlink(missing_ok=True)
    return {"days_ok": got, "days_failed": failed, "added": added}
