"""Migrácie schémy cez PRAGMA user_version.

Pravidlá:
- verzia 0 s existujúcimi tabuľkami = databáza z v0.1 (verzia 1, prípadne bez neskôr
  doplnených stĺpcov),
- pred každou migráciou existujúcej databázy sa vytvorí záloha
  `<priečinok databázy>/backup/radar-v<n>-<čas>.sqlite`,
- existujúcu migráciu nikdy neupravuj, pridaj novú funkciu na koniec MIGRATIONS.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .schema import (
    SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9, SCHEMA_V10, SCHEMA_V11, SCHEMA_V12,
    SCHEMA_V13, SCHEMA_V13_OUTREACH, SCHEMA_V14, SCHEMA_V15, SCHEMA_V16, SCHEMA_V17, SCHEMA_V18, ESCO_FTS, V1_COLUMNS,
    V15_COLUMNS, V17_COLUMNS, V18_COLUMNS,
    V1_INDEXES, V2_COLUMNS, V3_COLUMNS, V4_COLUMNS, V5_COLUMNS, V6_COLUMNS, V8_COLUMNS, V10_COLUMNS, V12_COLUMNS,
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


def migrate_v8(conn: sqlite3.Connection) -> None:
    """Problémové karty, podhypotézy (rozdelenie širokej hypotézy) a experimenty Brány 2 podľa typu úlohy."""
    _add_columns(conn, V8_COLUMNS)
    conn.executescript(SCHEMA_V8)


def migrate_v9(conn: sqlite3.Connection) -> None:
    """Lokálny index Národného katalógu otvorených dát (hľadanie podľa významu, nielen podľa slov)."""
    conn.executescript(SCHEMA_V9)


def migrate_v10(conn: sqlite3.Connection) -> None:
    """Hľadanie kontaktov podľa hypotézy: plán cieľov (zákazník, držiteľ dát, expert) a väzba organizácie na cieľ."""
    _add_columns(conn, V10_COLUMNS)
    conn.executescript(SCHEMA_V10)


def migrate_v11(conn: sqlite3.Connection) -> None:
    """Lievik: záznam krokov „jedna hypotéza po druhej“ (test → kontakty → Brána 1 → Brána 2)."""
    conn.executescript(SCHEMA_V11)


def migrate_v12(conn: sqlite3.Connection) -> None:
    """Dôveryhodnosť dát: väzby polí na stĺpce, autor a odtlačok dát rozhodnutia brány, dostupnosť poľa, obchodný stav."""
    _add_columns(conn, V12_COLUMNS)
    conn.executescript(SCHEMA_V12)


def migrate_v13(conn: sqlite3.Connection) -> None:
    """Oslovenia podľa účelu (aj organizácie bez osoby), prehľady domén, návrhy prínosu, výsledky oslovení,
    cloudové modely (náklady, cache). Tabuľka outreach sa prestavia (person_id nepovinné) – história ostáva."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(SCHEMA_V13_OUTREACH)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_V13)


def migrate_v14(conn: sqlite3.Connection) -> None:
    """Doménový výskum (stav, podklady, tvrdenia, otázky, mapovanie rolí), lokálny ESCO index, event logy a procesné
    reporty. Fulltext ESCO cez FTS5, ak ho SQLite má (inak hľadanie cez normalizovaný text)."""
    conn.executescript(SCHEMA_V14)
    try:
        conn.execute(ESCO_FTS)
    except sqlite3.OperationalError:  # SQLite bez FTS5
        pass


def migrate_v15(conn: sqlite3.Connection) -> None:
    """Od doménového prehľadu ku konkrétnej ponuke (zadanie 17. 10. 2026): posúdenia dôkazov voči cieľu, karty
    organizácií, výskumné úlohy a väzby návrhu, výsledku a žiadosti o event log na verziu karty (iba pridávanie).
    Staré nejasné údaje sa označia, nič sa nevymýšľa ani nemaže:
    - „prebiehajúce obstarávanie“ odvodené zo všeobecnej stránky je kandidát na overenie (nie potvrdený stav),
    - väzba organizácie z citátu typu existujúce riešenie je „používa riešenie“, nie „problém“,
    - položka potrebných dát bez odkazu je návrh požadovaného vstupu, nie zdrojový fakt."""
    conn.executescript(SCHEMA_V15)
    for table, cols in V15_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute(
        """UPDATE org_hypotheses SET procurement_status = 'candidate', procurement_active = 0,
                  procurement_url = TRIM(REPLACE(COALESCE(procurement_note, ''), 'Možné prebiehajúce obstarávanie:', '')),
                  procurement_note = COALESCE(procurement_note, '') || ' [v15: neoverené – iba stránka obstarávania]'
           WHERE procurement_active = 1""")
    conn.execute(
        """UPDATE org_hypotheses SET relation = 'uses_solution'
           WHERE relation = 'mentioned_with_problem' AND evidence_claim_id IN (
             SELECT id FROM claims WHERE claim_type = 'existing_solution')""")
    conn.execute(
        """UPDATE domain_claims SET status = 'proposal',
                  applicability = 'požadovaný vstup – nie zdrojový fakt o dostupnosti dát (označené pri migrácii v15)'
           WHERE part = 'data' AND status = 'source_fact'
             AND COALESCE(json_array_length(json_extract(evidence_json, '$.claims')), 0) = 0
             AND COALESCE(json_array_length(json_extract(evidence_json, '$.evidence')), 0) = 0""")


def migrate_v16(conn: sqlite3.Connection) -> None:
    """Obstarávanie z TED (M37): štruktúrované oznámenia a ich podobnosť s hypotézami (iba pridávanie)."""
    conn.executescript(SCHEMA_V16)


def migrate_v17(conn: sqlite3.Connection) -> None:
    """Obchodné stavy (M35): kto platí ≠ ochota platiť. Staré údaje sa prenesú iba ako to, čo naozaj dokladajú:
    - `payer_confirmed` (text „kto platí“) a hotový krok „platiteľ“ obchodného stavu → platiteľ identifikovaný,
    - príznak `paid` výsledku oslovenia bez sumy → platba s poznámkou „doplň sumu“ (nič sa nevymýšľa)."""
    conn.executescript(SCHEMA_V17)
    _add_columns(conn, V17_COLUMNS)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in conn.execute("SELECT id, payer_confirmed, business_json FROM hypotheses").fetchall():
        notes = []
        if (row[1] or "").strip():
            notes.append(f"v17: payer_confirmed = „{row[1].strip()[:300]}“ (identifikovaný platiteľ, nie ochota platiť)")
        try:
            step = (json.loads(row[2] or "{}") or {}).get("platiteľ") or {}
        except ValueError:
            step = {}
        if step.get("hotové"):
            notes.append(f"v17: obchodný stav „platiteľ“: {(step.get('dôkaz') or '').strip()[:300]}")
        if notes:
            conn.execute("INSERT INTO commercial_evidence (hypothesis_id, stage, evidence, recorded_by, created_at, "
                         "updated_at) VALUES (?, 'buyer_identified', ?, 'migration', ?, ?)", (row[0], " · ".join(notes), now, now))
    conn.execute(
        """INSERT INTO commercial_evidence (hypothesis_id, org_id, person_id, outcome_id, stage, evidence, recorded_by,
                                           created_at, updated_at)
           SELECT hypothesis_id, org_id, person_id, id, 'payment_received',
                  'v17: výsledok oslovenia označený „platené“ bez sumy – doplň sumu, menu a spôsob platby', 'migration',
                  recorded_at, recorded_at
           FROM outreach_outcomes WHERE paid = 1""")


def migrate_v18(conn: sqlite3.Connection) -> None:
    """TED (kalibrácia 26. 10. 2026): oznámenia jednej zákazky sa zlučujú podľa identifikátora postupu a zhodu
    s hypotézou potvrdzuje model. Stav sťahovania a porovnania sa vynuluje: ďalší beh stiahne oznámenia znova
    (doplní identifikátor postupu; vektory ostanú, text sa nemení) a hypotézy porovná novým postupom."""
    _add_columns(conn, V18_COLUMNS)
    conn.executescript(SCHEMA_V18)
    conn.execute("DELETE FROM settings WHERE key IN ('procurement_sync', 'procurement_matched')")
    conn.execute("DELETE FROM procurement_matches")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [migrate_v1, migrate_v2, migrate_v3, migrate_v4, migrate_v5,
                                                          migrate_v6, migrate_v7, migrate_v8, migrate_v9, migrate_v10,
                                                          migrate_v11, migrate_v12, migrate_v13, migrate_v14, migrate_v15,
                                                          migrate_v16, migrate_v17, migrate_v18]
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
