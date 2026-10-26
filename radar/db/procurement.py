"""SQL pre obstarávanie z TED (M37): oznámenia a ich podobnosť s hypotézami."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

NOTICE_FIELDS = ("source", "notice_id", "notice_type", "procedure_type", "contract_nature", "stage", "title",
                 "description", "buyer_name", "buyer_ico", "buyer_legal_type", "buyer_country", "cpv_json", "main_cpv",
                 "publication_date", "deadline", "estimated_value", "estimated_currency", "awarded_value",
                 "awarded_currency", "winners_json", "url", "raw_hash", "procedure_id")


class ProcurementMixin:
    conn: sqlite3.Connection

    def upsert_procurement_notices(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Vloží nové oznámenia a zmenené (iný hash) prepíše; vráti počty nových a zmenených."""
        out = {"nové": 0, "zmenené": 0}
        now = utcnow()
        for r in rows:
            data = {k: (json.dumps(r[k], ensure_ascii=False) if k.endswith("_json") and not isinstance(r.get(k), (str, type(None)))
                        else r.get(k)) for k in NOTICE_FIELDS}
            data["source"] = data["source"] or "ted"
            old = self.conn.execute("SELECT id, raw_hash FROM procurement_notices WHERE source = ? AND notice_id = ?",
                                    (data["source"], data["notice_id"])).fetchone()
            if old is None:
                cols = list(data) + ["fetched_at"]
                self.conn.execute(f"INSERT INTO procurement_notices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                  (*data.values(), now))
                out["nové"] += 1
            elif old["raw_hash"] != data["raw_hash"]:
                sets = ", ".join(f"{k} = ?" for k in data)
                self.conn.execute(f"UPDATE procurement_notices SET {sets}, fetched_at = ? WHERE id = ?",
                                  (*data.values(), now, old["id"]))
                out["zmenené"] += 1
        self.conn.commit()
        return out

    def procurement_notices(self, ids: list[int] | None = None) -> list[sqlite3.Row]:
        if ids is None:
            return self.conn.execute("SELECT * FROM procurement_notices ORDER BY id").fetchall()
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn.execute(f"SELECT * FROM procurement_notices WHERE id IN ({marks}) ORDER BY id", tuple(ids)).fetchall()

    def procurement_stats(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT COUNT(*) AS n, MAX(publication_date) AS last, MAX(fetched_at) AS fetched "
                                "FROM procurement_notices").fetchone()
        return {"oznámenia": row["n"], "najnovšie": row["last"], "stiahnuté": row["fetched"]}

    def set_procurement_matches(self, hid: int, matches: list[tuple[int, float, str, str, str]], query_hash: str,
                                done: bool = True) -> None:
        """Nahradí zhody hypotézy: (oznámenie, podobnosť, kľúč zákazky, verdikt, dôvod). Prázdny zoznam je výsledok
        („nič podobné“). done=False: kandidáti ešte čakajú na posúdenie – hypotéza sa porovná v ďalšom behu."""
        now = utcnow()
        self.conn.execute("DELETE FROM procurement_matches WHERE hypothesis_id = ?", (hid,))
        self.conn.executemany(
            "INSERT INTO procurement_matches (hypothesis_id, notice_id, similarity, query_hash, matched_at, procedure_key, "
            "verdict, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(hid, nid, float(sim), query_hash, now, key, verdict, reason) for nid, sim, key, verdict, reason in matches])
        if not done:
            self.conn.commit()
            return
        state = self.conn.execute("SELECT value_json FROM settings WHERE key = 'procurement_matched'").fetchone()
        done = json.loads(state["value_json"]) if state is not None else {}
        done[str(hid)] = {"hash": query_hash, "kedy": now, "zhody": len(matches)}
        self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES ('procurement_matched', ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (json.dumps(done, ensure_ascii=False), now))
        self.conn.commit()

    def procurement_judgment(self, hid: int, key: str, query_hash: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM procurement_judgments WHERE hypothesis_id = ? AND procedure_key = ? "
                                 "AND query_hash = ?", (hid, key, query_hash)).fetchone()

    def put_procurement_judgment(self, hid: int, key: str, query_hash: str, verdict: str, reason: str,
                                 model: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO procurement_judgments (hypothesis_id, procedure_key, query_hash, verdict, reason, model, "
            "judged_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (hid, key, query_hash, verdict, reason, model, utcnow()))
        self.conn.commit()

    def procurement_matched_state(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT value_json FROM settings WHERE key = 'procurement_matched'").fetchone()
        return json.loads(row["value_json"]) if row is not None else {}

    def procurement_matches_for(self, hid: int) -> list[sqlite3.Row]:
        """Podobné oznámenia hypotézy (najpodobnejšie prvé) so všetkými stĺpcami oznámenia."""
        return self.conn.execute(
            """SELECT n.*, m.similarity, m.matched_at, m.procedure_key, m.verdict, m.reason FROM procurement_matches m
               JOIN procurement_notices n ON n.id = m.notice_id
               WHERE m.hypothesis_id = ? ORDER BY m.similarity DESC, n.publication_date DESC""", (hid,)).fetchall()
