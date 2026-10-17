"""SQLite databáza = register dôkazov + agregátor Brány 0. Jediné miesto s SQL je balík radar.db.

- evidence.py: dokumenty, tvrdenia, hypotézy (register dôkazov z v0.1),
- gate0.py: organizácie, ľudia, kontakty, oslovenia, rozhovory, rozhodnutia, potlačenie,
- gates.py: Brána 1 (polia, zdroje, datasety, reprodukcia, infozákon), Brána 2 (experimenty), nastavenia,
- ops.py: provenance volaní modelov, kontrola, labely, benchmark, štatistiky, cache, eval, úlohy,
- dossier.py: čítanie všetkého k hypotéze pre dosiér (export),
- value.py: prehľady domén, návrhy prínosu, výsledky oslovení, cloudové náklady a cache,
- opportunity.py: posúdenia dôkazov voči cieľu, karty organizácií (verzie, pripravenosť) a výskumné úlohy z medzier,
- domain.py: doménový výskum (stav, podklady, tvrdenia, otázky, mapovanie rolí), lokálny ESCO index,
- eventlogs.py: žiadosti o event log, importy (udalosti sú v DuckDB organizácie), procesné reporty,
- schema.py a migrations.py: schéma po verziách a migrácie cez PRAGMA user_version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .domain import DomainMixin
from .dossier import DossierMixin
from .eventlogs import EventLogMixin
from .evidence import ACTIVE_STATUSES, ASSIGNABLE_STATUSES, EXTRACTABLE_KINDS, HYPOTHESIS_STATUSES, EvidenceMixin
from .gate0 import Gate0Mixin
from .gates import GatesMixin
from .migrations import LATEST, migrate
from .opportunity import OpportunityMixin
from .ops import OpsMixin
from .themes import ThemesMixin
from .value import ValueMixin

__all__ = [
    "DB",
    "ACTIVE_STATUSES",
    "ASSIGNABLE_STATUSES",
    "EXTRACTABLE_KINDS",
    "HYPOTHESIS_STATUSES",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = LATEST


class DB(EvidenceMixin, Gate0Mixin, GatesMixin, OpsMixin, ThemesMixin, DossierMixin, ValueMixin, DomainMixin, EventLogMixin,
         OpportunityMixin):
    def __init__(self, path: str | Path, migrate_schema: bool = True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # check_same_thread=False: GUI (Streamlit) beží v inom vlákne než vytvorenie spojenia.
        self.conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # GUI a nočný beh môžu písať naraz: počkaj až 5 s, kým druhý zápis skončí (R-GUI-5).
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.migration = migrate(self.conn, path) if migrate_schema else None
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur
