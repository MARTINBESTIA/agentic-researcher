"""SQL pre obchodné stavy (M35): platiteľ, rozpočet, záujem o kúpu, platba – zapisuje človek s dôkazom."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import utcnow

COMMERCIAL_FIELDS = {"org_id", "person_id", "outcome_id", "interview_id", "stage", "evidence", "conditions", "amount",
                     "currency", "billing", "contract_value", "recorded_by"}


class CommercialMixin:
    conn: sqlite3.Connection

    def add_commercial_evidence(self, hid: int, **fields: Any) -> int:
        bad = set(fields) - COMMERCIAL_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia obchodného dôkazu: {bad}")
        now = utcnow()
        cols = ["hypothesis_id", "created_at", "updated_at"] + list(fields)
        cur = self.conn.execute(f"INSERT INTO commercial_evidence ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                (hid, now, now, *fields.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def commercial_evidence_for(self, hid: int, org_id: int | None = None,
                                include_withdrawn: bool = False) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, o.name AS org_name FROM commercial_evidence c "
               "LEFT JOIN organizations o ON o.id = c.org_id WHERE c.hypothesis_id = ?")
        params: list[Any] = [hid]
        if org_id is not None:
            sql += " AND c.org_id = ?"
            params.append(org_id)
        if not include_withdrawn:
            sql += " AND c.status = 'active'"
        return self.conn.execute(sql + " ORDER BY c.id", tuple(params)).fetchall()

    def get_commercial_evidence(self, ce_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM commercial_evidence WHERE id = ?", (ce_id,)).fetchone()

    def withdraw_commercial_evidence(self, ce_id: int, reason: str) -> None:
        self.conn.execute("UPDATE commercial_evidence SET status = 'withdrawn', updated_at = ?, "
                          "evidence = evidence || ' [odvolané: ' || ? || ']' WHERE id = ?", (utcnow(), reason, ce_id))
        self.conn.commit()

    def scrub_commercial_evidence_of_person(self, person_id: int) -> int:
        """forget: obchodný fakt o organizácii ostane, text dôkazu (môže obsahovať meno) a väzba na osobu nie."""
        cur = self.conn.execute("UPDATE commercial_evidence SET evidence = '[vymazané – výmaz osoby]', person_id = NULL, "
                                "updated_at = ? WHERE person_id = ?", (utcnow(), person_id))
        self.conn.commit()
        return cur.rowcount
