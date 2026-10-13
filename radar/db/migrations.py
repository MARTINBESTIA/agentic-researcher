"""Migrácie schémy cez PRAGMA user_version.

Pravidlá:
- verzia 0 s existujúcimi tabuľkami = databáza z v0.1 (verzia 1, prípadne bez neskôr
  doplnených stĺpcov),
- pred každou migráciou existujúcej databázy sa vytvorí záloha
  `<priečinok databázy>/backup/radar-v<n>-<čas>.sqlite`,
- existujúcu migráciu nikdy neupravuj, pridaj novú funkciu na koniec MIGRATIONS.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from .schema import (
    SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, V1_COLUMNS, V1_INDEXES, V2_COLUMNS,
    V3_COLUMNS,
    V4_COLUMNS, V5_COLUMNS, V6_COLUMNS,
)

log = logging.getLogger(__name__)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, spec: dict[str, dict[str, str]]) -> None:
    for table, columns in spec.items():
        existing = _columns(conn, table)
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def migrate_v1(conn: sqlite3.Connection) -> None:
    """Register dôkazov v0.1 (idempotentné: dopĺňa aj staršie databázy v0.1)."""
    conn.executescript(SCHEMA_V1)
    _add_columns(conn, V1_COLUMNS)
    for ddl in V1_INDEXES:
        conn.execute(ddl)


def migrate_v2(conn: sqlite3.Connection) -> None:
    """Brána 0 agregátor: organizácie, ľudia, oslovenia, provenance modelov, kontrola, eval."""
    _add_columns(conn, V2_COLUMNS)
    conn.executescript(SCHEMA_V2)
    # Pôvod hypotéz z v0.1: projekty sú "manual" (zadal ich používateľ), ostatné z tém.
    conn.execute("UPDATE hypotheses SET origin = 'manual' WHERE project IS NOT NULL AND origin = 'topic'")
    conn.execute("UPDATE hypotheses SET first_seen_at = created_at WHERE first_seen_at IS NULL")
    # Tvrdenia, ktoré v0.1 už posúdila, majú výsledok kontroly kódom implicitne v stave.
    conn.execute(
        "UPDATE claims SET det_status = CASE status WHEN 'partial' THEN 'partial' ELSE 'ok' END "
        "WHERE status IN ('verified', 'partial') AND det_status IS NULL"
    )


def migrate_v3(conn: sqlite3.Connection) -> None:
    """Testovanie všetkých hypotéz (stav, verdikt kódu) a úlohy spúšťané z GUI."""
    _add_columns(conn, V3_COLUMNS)
    conn.executescript(SCHEMA_V3)


def migrate_v4(conn: sqlite3.Connection) -> None:
    """Brána 1 (polia, zdroje, datasety, reprodukcia, infozákon), Brána 2 (experimenty), nastavenia z GUI."""
    _add_columns(conn, V4_COLUMNS)
    conn.executescript(SCHEMA_V4)


def migrate_v5(conn: sqlite3.Connection) -> None:
    """Typ vydavateľa zdroja (úrad, médiá, výskum, združenie, dodávateľ) – dodávatelia sa nepočítajú do sily dôkazov."""
    _add_columns(conn, V5_COLUMNS)
    conn.executescript(SCHEMA_V5)


def migrate_v6(conn: sqlite3.Connection) -> None:
    """Témy: zhluky podobných hypotéz (nedeštruktívne – hypotézy sa nezlučujú)."""
    _add_columns(conn, V6_COLUMNS)
    conn.executescript(SCHEMA_V6)


def migrate_v7(conn: sqlite3.Connection) -> None:
    """Vstup človeka pre Bránu 1 (dáta alebo schéma) a posúdenie ich vhodnosti."""
    conn.executescript(SCHEMA_V7)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [migrate_v1, migrate_v2, migrate_v3, migrate_v4, migrate_v5,
                                                          migrate_v6, migrate_v7]
LATEST = len(MIGRATIONS)


def has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def backup(conn: sqlite3.Connection, db_path: Path, version: int) -> Path:
    """Konzistentná kópia aj pri WAL (sqlite3 backup API)."""
    folder = db_path.parent / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"radar-v{version}-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    dest = sqlite3.connect(target)
    try:
        conn.commit()
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(conn: sqlite3.Connection, db_path: Path | None = None) -> dict:
    """Zmigruje databázu na najnovšiu verziu. Vráti {"from": n, "to": m, "backup": cesta|None}."""
    current = user_version(conn)
    existing = has_tables(conn)
    if current == 0 and existing:
        current = 0  # v0.1 bez user_version: migrate_v1 je idempotentná, takže začneme od nej
    result = {"from": current, "to": current, "backup": None}
    if current >= LATEST:
        return result
    if existing and db_path is not None and db_path.exists():
        result["backup"] = str(backup(conn, db_path, max(current, 1)))
        log.info("Záloha databázy pred migráciou: %s", result["backup"])
    for version in range(current, LATEST):
        MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {version + 1}")
        conn.commit()
        log.info("Databáza zmigrovaná na verziu %d", version + 1)
    result["to"] = LATEST
    return result
