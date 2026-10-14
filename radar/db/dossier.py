"""Dosiér hypotézy: čítanie všetkého, čo k hypotézam patrí (aj zamietnuté tvrdenia, všetky verzie kariet,
volania modelov) – jediné miesto s jeho SQL. Nič nemení."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

CHUNK = 500  # počet hodnôt v IN (...) na jeden dotaz


class DossierMixin:
    conn: sqlite3.Connection

    def _in(self, sql: str, ids: Iterable[Any], params: tuple = ()) -> list[sqlite3.Row]:
        """`sql` obsahuje {ids} na mieste zoznamu; dotaz sa rozdelí po CHUNK hodnotách."""
        ids = list(dict.fromkeys(i for i in ids if i is not None))
        rows: list[sqlite3.Row] = []
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            rows += self._all(sql.format(ids=", ".join("?" * len(part))), (*params, *part))
        return rows

    def dossier_claims(self, hids: list[int]) -> list[sqlite3.Row]:
        """Všetky tvrdenia priradené k hypotézam – aj neoverené, zamietnuté a duplikáty (stav je v stĺpci)."""
        return self._in(
            """SELECT hc.hypothesis_id, c.*, d.url, d.domain, d.title AS doc_title, d.fetched_at, d.content_hash,
                      d.source_kind, d.source_type
               FROM hypothesis_claims hc JOIN claims c ON c.id = hc.claim_id JOIN documents d ON d.id = c.document_id
               WHERE hc.hypothesis_id IN ({ids}) ORDER BY hc.hypothesis_id, c.id""",
            hids,
        )

    def documents_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        return self._in("SELECT * FROM documents WHERE id IN ({ids}) ORDER BY id", ids)

    def judgments_for_objects(self, object_type: str, ids: list[int]) -> list[sqlite3.Row]:
        return self._in("SELECT * FROM model_judgments WHERE object_type = ? AND object_id IN ({ids}) ORDER BY id",
                        ids, (object_type,))

    def labels_for_objects(self, object_type: str, ids: list[int]) -> list[sqlite3.Row]:
        return self._in("SELECT * FROM labels WHERE object_type = ? AND object_id IN ({ids}) ORDER BY id",
                        ids, (object_type,))

    def people_for_hypotheses(self, hids: list[int]) -> list[sqlite3.Row]:
        """Ľudia s väzbou na hypotézy alebo zozbieraní pre ne (aj ešte nezoradení), s organizáciou."""
        marks = ", ".join("?" * len(hids))
        return self._all(
            f"""SELECT p.*, o.name AS org_name, o.ico AS org_ico, o.sector_kind AS org_sector_kind FROM people p
                LEFT JOIN organizations o ON o.id = p.org_id
                WHERE p.collected_for_hypothesis_id IN ({marks})
                   OR p.id IN (SELECT person_id FROM person_problem_links WHERE hypothesis_id IN ({marks}))
                ORDER BY p.id""",
            (*hids, *hids),
        ) if hids else []

    def cards_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM problem_cards WHERE hypothesis_id = ? ORDER BY id", (hid,))

    def critiques_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM critiques WHERE hypothesis_id = ? ORDER BY id", (hid,))

    def market_estimates_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM market_estimates WHERE hypothesis_id = ? ORDER BY id", (hid,))
