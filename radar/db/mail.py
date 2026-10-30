"""SQL pre poštu (M41): metadáta správ spárovaných s osloveniami a návrhy z odpovedí (bez tiel e-mailov)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

MAIL_FIELDS = {"thread_id", "outreach_id", "person_id", "direction", "kind", "sent_at", "subject", "status",
               "proposal_json", "outcome_id"}


class MailMixin:
    conn: sqlite3.Connection

    def mail_known_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT message_id FROM mail_messages")}

    def add_mail_message(self, message_id: str, **fields: Any) -> int:
        bad = set(fields) - MAIL_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia správy: {bad}")
        now = utcnow()
        data = {k: (json.dumps(v, ensure_ascii=False) if k == "proposal_json" and not isinstance(v, (str, type(None)))
                    else v) for k, v in fields.items()}
        cols = ["message_id", "created_at", "updated_at"] + list(data)
        cur = self.conn.execute(f"INSERT OR IGNORE INTO mail_messages ({', '.join(cols)}) VALUES "
                                f"({', '.join('?' * len(cols))})", (message_id, now, now, *data.values()))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_mail_message(self, mail_id: int, **fields: Any) -> None:
        bad = set(fields) - MAIL_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia správy: {bad}")
        data = {k: (json.dumps(v, ensure_ascii=False) if k == "proposal_json" and not isinstance(v, (str, type(None)))
                    else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in data)
        self.conn.execute(f"UPDATE mail_messages SET {sets}, updated_at = ? WHERE id = ?", (*data.values(), utcnow(), mail_id))
        self.conn.commit()

    def get_mail_message(self, mail_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()

    def mail_messages(self, status: str | None = None, kind: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mail_messages WHERE 1 = 1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return self.conn.execute(sql + " ORDER BY sent_at, id LIMIT ?", (*params, limit)).fetchall()

    def mail_thread_outreach(self) -> dict[str, int]:
        """Vlákno Gmailu → oslovenie (z odoslaných správ) – odpoveď v tom istom vlákne patrí k nemu."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT thread_id, outreach_id FROM mail_messages WHERE direction = 'out' AND thread_id IS NOT NULL "
            "AND outreach_id IS NOT NULL ORDER BY id")}

    def mail_by_outreach(self) -> dict[int, list[sqlite3.Row]]:
        """Oslovenie → jeho správy z pošty v časovom poradí (iba druh a čas, bez predmetu a návrhu)."""
        out: dict[int, list[sqlite3.Row]] = {}
        for r in self.conn.execute("SELECT outreach_id, direction, kind, sent_at FROM mail_messages "
                                   "WHERE outreach_id IS NOT NULL ORDER BY sent_at, id"):
            out.setdefault(r["outreach_id"], []).append(r)
        return out

    def mail_stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT kind, status, COUNT(*) AS n FROM mail_messages GROUP BY kind, status").fetchall()
        out: dict[str, int] = {}
        for r in rows:
            out[r["kind"]] = out.get(r["kind"], 0) + r["n"]
            out[f"stav:{r['status']}"] = out.get(f"stav:{r['status']}", 0) + r["n"]
        return out
