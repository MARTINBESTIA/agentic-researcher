"""Datasety Brány 1 a 2: stiahnutie, import do DuckDB, profil, pokrytie organizácií a bezpečné dotazy.

- Súbor sa sťahuje po častiach s limitom veľkosti (gate1.max_dataset_mb) a celkovým limitom
  (gate1.max_total_gb); blokované domény (sociálne siete) sa nesťahujú ani po presmerovaní, robots.txt platí.
- Tabuľky sú v jednej DuckDB databáze na hypotézu (data/datasets/H<id>/gate1.duckdb, mimo gitu); pôvodný
  súbor sa po importe zmaže (ostáva URL, čas a SHA-256 – vieme, čo presne sa stiahlo).
- Dataset s osobnými údajmi (stĺpce meno, rodné číslo, e-mail… alebo e-maily a telefóny v hodnotách) sa
  neuloží: najprv posúdenie podľa GDPR (R-PRIV). Ostane iba profil bez ukážok.
- Dotazy od modelu (reprodukcia čísla) bežia iba na čítanie, bez prístupu k súborom a sieti (DuckDB
  enable_external_access = false), iba jeden SELECT a s časovým limitom. Čísla počíta DuckDB/Python.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
import phonenumbers

from .config import Config
from .fetch import domain_blocked

log = logging.getLogger(__name__)

FILE_EXT = ("csv", "tsv", "txt", "xlsx", "json", "parquet")
FILE_LINK_RE = re.compile(r"\.(csv|tsv|xlsx|json|parquet)(\?|$)", re.I)
PERSONAL_COLUMN_RE = re.compile(
    r"(^|_|\b)(meno|priezvisko|rodn[eé]|rod_?c|r\.?\s?č\.?|e-?mail|telef|mobil|adres|ulica|bydlisk|narod|"
    r"name|surname|birth|phone|address)", re.I)
EMAIL_VALUE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_VALUE_RE = re.compile(r"^\+?\d[\d /]{8,}$")
ICO_COLUMN_RE = re.compile(r"(^|_|\b)(i[čc]o|ic)(\b|_|$)", re.I)
TIME_COLUMN_RE = re.compile(r"(rok|year|d[áa]tum|date|obdob|mesiac|month|kvart|quarter|time|čas)", re.I)
FORBIDDEN_SQL = re.compile(
    r"\b(read_\w+|copy|attach|detach|install|load|pragma|export|import|create|insert|update|delete|drop|alter|"
    r"call|set|reset|checkpoint|vacuum|glob|httpfs|system)\b|https?://|;.+", re.I | re.S)


def _is_phone(value: str) -> bool:
    """Telefón v hodnote: začína + alebo 0 a phonenumbers ho uzná (DIČ, IČO a sumy nie sú telefóny)."""
    v = value.strip()
    if not PHONE_VALUE_RE.match(v) or not v.startswith(("+", "0")):
        return False
    try:
        return phonenumbers.is_valid_number(phonenumbers.parse(v, "SK"))
    except phonenumbers.NumberParseException:
        return False


class DatasetError(RuntimeError):
    pass


@dataclass
class Download:
    path: Path
    sha256: str
    size: int
    final_url: str
    content_type: str


def hyp_dir(cfg: Config, hid: int) -> Path:
    return cfg.path(cfg.gate1.datasets_dir) / f"H{hid}"


def duck_path(cfg: Config, hid: int) -> Path:
    return hyp_dir(cfg, hid) / "gate1.duckdb"


def file_format(url: str, content_type: str = "") -> str | None:
    path = urlparse(url).path.lower()
    for ext in FILE_EXT:
        if path.endswith("." + ext):
            return "csv" if ext in ("tsv", "txt") else ext
    ct = content_type.lower()
    if "csv" in ct:
        return "csv"
    if "spreadsheetml" in ct:
        return "xlsx"
    if "json" in ct:
        return "json"
    if "parquet" in ct:
        return "parquet"
    return None


def download(cfg: Config, fetcher, url: str, dest_dir: Path, blocked: list[str]) -> Download:
    """Stiahne súbor po častiach; preruší pri prekročení limitu. Presmerovanie na blokovanú doménu = chyba."""
    limit = cfg.gate1.max_dataset_mb * 1024 * 1024
    if domain_blocked(url, blocked):
        raise DatasetError("blokovaná doména")
    if not fetcher.allowed(url):
        raise DatasetError("robots.txt nepovoľuje")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / ("download-" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with fetcher.http.stream("GET", url, follow_redirects=True) as resp:
            hops = [str(r.url) for r in resp.history] + [str(resp.url)]
            if any(domain_blocked(h, blocked) for h in hops):
                raise DatasetError("presmerovanie na blokovanú doménu")
            if resp.status_code >= 400:
                raise DatasetError(f"HTTP {resp.status_code}")
            declared = int(resp.headers.get("content-length") or 0)
            if declared > limit:
                raise DatasetError(f"súbor má {declared / 1e6:.0f} MB, limit {cfg.gate1.max_dataset_mb} MB")
            with open(tmp, "wb") as out:
                for chunk in resp.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise DatasetError(f"súbor presiahol limit {cfg.gate1.max_dataset_mb} MB")
                    digest.update(chunk)
                    out.write(chunk)
            ctype = resp.headers.get("content-type", "")
            final = str(resp.url)
    except DatasetError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 – sieťová chyba: dataset sa zapíše ako chyba, beh pokračuje
        tmp.unlink(missing_ok=True)
        raise DatasetError(f"sťahovanie zlyhalo: {exc}") from exc
    sha = digest.hexdigest()
    fmt = file_format(final, ctype) or file_format(url, ctype) or "bin"
    path = dest_dir / f"{sha[:16]}.{fmt}"
    tmp.replace(path)
    return Download(path, sha, size, final, ctype)


def _to_utf8(path: Path) -> Path:
    """CSV z úradov býva v cp1250; DuckDB číta UTF-8. Prekódovanie po riadkoch (bez načítania celého súboru)."""
    with open(path, "rb") as fh:
        head = fh.read(1 << 20)
    try:
        head.decode("utf-8")
        return path
    except UnicodeDecodeError:
        pass
    out = path.with_suffix(".utf8.csv")
    with open(path, encoding="cp1250", errors="replace", newline="") as src, open(out, "w", encoding="utf-8", newline="") as dst:
        for line in src:
            dst.write(line)
    return out


def _xlsx_to_csv(path: Path) -> Path:
    import csv

    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    best, rows = None, -1
    for ws in wb.worksheets:  # hárok s najviac riadkami
        n = ws.max_row or 0
        if n > rows:
            best, rows = ws, n
    out = path.with_suffix(".csv")
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        for row in best.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in row])
    wb.close()
    return out


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def import_table(cfg: Config, hid: int, table: str, path: Path, fmt: str) -> None:
    """Súbor → tabuľka v DuckDB databáze hypotézy (zápis; pred dotazmi modelu sa otvorí iba na čítanie)."""
    if not re.fullmatch(r"ds\d+", table):
        raise DatasetError("neplatný názov tabuľky")
    src = path
    if fmt == "xlsx":
        src, fmt = _xlsx_to_csv(path), "csv"
    if fmt == "csv":
        src = _to_utf8(src)
        reader = "read_csv(?, auto_detect = true, ignore_errors = true, sample_size = 20000)"
    elif fmt == "json":
        # Odpovede API bývajú vnorené (prijimatel.subjekt.ico): rozbalia sa na stĺpce (ico, dic …).
        reader = "(SELECT unnest(j, recursive := true) FROM read_json_auto(?) AS j)"
    elif fmt == "parquet":
        reader = "read_parquet(?)"
    else:
        raise DatasetError(f"nepodporovaný formát {fmt}")
    con = duckdb.connect(str(duck_path(cfg, hid)))
    try:
        try:
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {reader}", [str(src)])
        except duckdb.Error:
            if fmt != "json":
                raise
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_json_auto(?)", [str(src)])
    except duckdb.Error as exc:
        raise DatasetError(f"import zlyhal: {str(exc)[:200]}") from exc
    finally:
        con.close()
        for extra in {src, path.with_suffix(".csv"), path.with_suffix(".utf8.csv")} - {path}:
            if extra.exists() and extra != path:
                extra.unlink(missing_ok=True)


def drop_table(cfg: Config, hid: int, table: str) -> None:
    if not re.fullmatch(r"ds\d+", table) or not duck_path(cfg, hid).exists():
        return
    con = duckdb.connect(str(duck_path(cfg, hid)))
    try:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        con.close()


def profile_table(cfg: Config, hid: int, table: str) -> dict[str, Any]:
    """Riadky, stĺpce (typ, podiel prázdnych, rozsah), časový rozsah a podozrenie na osobné údaje."""
    con = duckdb.connect(str(duck_path(cfg, hid)), read_only=True)
    try:
        cols = con.execute(f"DESCRIBE {table}").fetchall()
        rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        columns, personal, time_cols, ico_cols = [], [], [], []
        for name, ctype, *_ in cols:
            q = _quote(name)
            nulls = con.execute(f"SELECT COUNT(*) FILTER (WHERE {q} IS NULL) FROM {table}").fetchone()[0]
            info: dict[str, Any] = {"name": name, "type": ctype, "nulls": round(nulls / rows, 3) if rows else None}
            if any(t in ctype for t in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "DATE", "TIMESTAMP", "BIGINT")):
                lo, hi = con.execute(f"SELECT MIN({q}), MAX({q}) FROM {table}").fetchone()
                info.update(min=str(lo), max=str(hi))
            if PERSONAL_COLUMN_RE.search(name):
                personal.append(name)
            elif "VARCHAR" in ctype:
                sample = [r[0] for r in con.execute(f"SELECT {q} FROM {table} WHERE {q} IS NOT NULL LIMIT 200").fetchall()]
                hits = sum(1 for v in sample if EMAIL_VALUE_RE.search(str(v)) or _is_phone(str(v)))
                if sample and hits / len(sample) > 0.2:
                    personal.append(name)
            if TIME_COLUMN_RE.search(name) or "DATE" in ctype or "TIMESTAMP" in ctype:
                time_cols.append(name)
            if ICO_COLUMN_RE.search(name):
                ico_cols.append(name)
            columns.append(info)
        span = {}
        for name in time_cols[:3]:
            info = next(c for c in columns if c["name"] == name)
            if "min" in info:
                span[name] = [info["min"], info["max"]]
        return {"rows": rows, "columns": columns, "time_columns": time_cols, "time_span": span,
                "ico_columns": ico_cols, "personal_columns": personal}
    finally:
        con.close()


def coverage(cfg: Config, hid: int, table: str, ico_columns: list[str], orgs: list[tuple[int, str]]) -> list[tuple[int, str, int]]:
    """(org_id, stĺpec, počet riadkov) pre organizácie z DB, ktorých IČO je v datasete."""
    by_ico = {str(ico).zfill(8): oid for oid, ico in orgs if ico}
    if not by_ico or not ico_columns:
        return []
    con = duckdb.connect(str(duck_path(cfg, hid)), read_only=True)
    out = []
    try:
        for col in ico_columns:
            q = _quote(col)
            rows = con.execute(
                f"SELECT lpad(trim(CAST({q} AS VARCHAR)), 8, '0') AS ico, COUNT(*) FROM {table} "
                f"WHERE lpad(trim(CAST({q} AS VARCHAR)), 8, '0') IN (SELECT unnest(?)) GROUP BY 1",
                [list(by_ico)],
            ).fetchall()
            out += [(by_ico[ico], col, n) for ico, n in rows]
    finally:
        con.close()
    return out


def schema_text(cfg: Config, hid: int, tables: list[str], samples: int = 3) -> str:
    """Popis tabuliek pre model: stĺpce, typy a niekoľko riadkov (osobné údaje sa sem nedostanú – také datasety
    sa neukladajú)."""
    if not duck_path(cfg, hid).exists():
        return ""
    con = duckdb.connect(str(duck_path(cfg, hid)), read_only=True)
    parts = []
    try:
        for t in tables:
            cols = con.execute(f"DESCRIBE {t}").fetchall()
            rows = con.execute(f"SELECT * FROM {t} LIMIT {samples}").fetchall()
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            head = ", ".join(f"{c[0]} {c[1]}" for c in cols)
            sample = "\n".join("  " + " | ".join(str(v)[:40] for v in r) for r in rows)
            parts.append(f"TABLE {t} ({n} rows): {head}\n{sample}")
    finally:
        con.close()
    return "\n\n".join(parts)


def check_sql(sql: str) -> str:
    """Iba jeden SELECT (alebo WITH … SELECT) nad tabuľkami; žiadne súbory, sieť ani zmeny."""
    s = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", s, re.I):
        raise DatasetError("dotaz musí začínať SELECT alebo WITH")
    if FORBIDDEN_SQL.search(s):
        raise DatasetError("dotaz obsahuje zakázaný príkaz (súbory, sieť, zmeny alebo viac príkazov)")
    return s


def run_query(cfg: Config, hid: int, sql: str, params: list[Any] | None = None, timeout_s: float | None = None) -> list[tuple]:
    """Dotaz iba na čítanie, bez prístupu k súborom a sieti, s časovým limitom (con.interrupt)."""
    s = check_sql(sql)
    path = duck_path(cfg, hid)
    if not path.exists():
        raise DatasetError("hypotéza nemá žiadny dataset")
    con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})
    timer = threading.Timer(timeout_s or cfg.gate1.sql_timeout_s, con.interrupt)
    timer.start()
    try:
        return con.execute(s, params or []).fetchmany(1000)
    except duckdb.Error as exc:
        raise DatasetError(f"dotaz zlyhal: {str(exc)[:200]}") from exc
    finally:
        timer.cancel()
        con.close()


def table_columns(cfg: Config, hid: int, table: str) -> list[str]:
    if not re.fullmatch(r"ds\d+", table) or not duck_path(cfg, hid).exists():
        return []
    con = duckdb.connect(str(duck_path(cfg, hid)), read_only=True)
    try:
        return [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    except duckdb.Error:
        return []
    finally:
        con.close()
