"""Brána 1 (dáta a dataset), Brána 2 (experimenty a demo) a nastavenia z GUI – jediné miesto s ich SQL."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

GATE_STATUSES = ("not_started", "running", "ready", "needs_input", "passed", "paused", "stopped")


class GatesMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ nastavenia
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), utcnow()),
        )

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------ stav brán
    def set_gate_status(self, hid: int, gate: int, status: str) -> None:
        if gate not in (1, 2) or status not in GATE_STATUSES:
            raise ValueError(f"Neznáma brána {gate} alebo stav {status!r}")
        col = "gate1_status" if gate == 1 else "gate2_status"
        self._exec(f"UPDATE hypotheses SET {col} = ?, updated_at = ? WHERE id = ?", (status, utcnow(), hid))

    def start_gate1(self, hid: int) -> None:
        """Prvý prieskum dát: od tohto času beží lehota na kľúčové pole (6 týždňov)."""
        self._exec(
            "UPDATE hypotheses SET gate1_started_at = COALESCE(gate1_started_at, ?), "
            "gate1_status = CASE WHEN gate1_status = 'not_started' THEN 'running' ELSE gate1_status END WHERE id = ?",
            (utcnow(), hid),
        )

    # ---------------------------------------------------------------- vstup človeka (Brána 1)
    def add_gate1_input(self, hid: int, text: str, file_name: str | None = None, dataset_id: int | None = None) -> int:
        cur = self._exec("INSERT INTO gate1_inputs (hypothesis_id, text, file_name, dataset_id, status, created_at) "
                         "VALUES (?, ?, ?, ?, 'pending', ?)", (hid, text.strip(), file_name, dataset_id, utcnow()))
        return int(cur.lastrowid)

    def gate1_inputs_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM gate1_inputs WHERE hypothesis_id = ? ORDER BY id DESC", (hid,))

    def pending_gate1_inputs(self, ids: list[int] | None = None) -> list[sqlite3.Row]:
        rows = self._all("SELECT * FROM gate1_inputs WHERE status = 'pending' ORDER BY id")
        return [r for r in rows if not ids or r["hypothesis_id"] in ids]

    def set_gate1_input_dataset(self, input_id: int, dataset_id: int) -> None:
        self._exec("UPDATE gate1_inputs SET dataset_id = ? WHERE id = ?", (dataset_id, input_id))

    def set_gate1_input_result(self, input_id: int, status: str, score: float | None, evaluation: dict[str, Any]) -> None:
        self._exec("UPDATE gate1_inputs SET status = ?, score = ?, evaluation_json = ?, evaluated_at = ? WHERE id = ?",
                   (status, score, json.dumps(evaluation, ensure_ascii=False), utcnow(), input_id))

    def set_gate1_summary(self, hid: int, summary: dict[str, Any]) -> None:
        self._exec("UPDATE hypotheses SET gate1_json = ? WHERE id = ?", (json.dumps(summary, ensure_ascii=False), hid))

    def gate1_hypotheses(self) -> list[sqlite3.Row]:
        """Brána 1 beží pre hypotézy vybrané na Bránu 0 (súbežne s rozhovormi) a pre tie, ktoré už začali."""
        return self._all(
            "SELECT * FROM hypotheses WHERE (people_enabled = 1 OR gate1_status != 'not_started') "
            "AND gate1_status NOT IN ('stopped') ORDER BY gate1_started_at IS NULL, id"
        )

    def gate2_hypotheses(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE gate1_status = 'passed' AND gate2_status != 'stopped' ORDER BY id")

    # ------------------------------------------------------------------ polia
    def upsert_field(self, hid: int, name: str, description: str, is_key: bool, rationale: str) -> int:
        """Pole od modelu; ručne upravené pole sa neprepíše."""
        now = utcnow()
        self._exec(
            """INSERT INTO data_fields (hypothesis_id, name, description, is_key, rationale, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (hypothesis_id, name) DO UPDATE SET
                 description = CASE WHEN edited_by_human THEN description ELSE excluded.description END,
                 is_key = CASE WHEN edited_by_human THEN is_key ELSE excluded.is_key END,
                 rationale = CASE WHEN edited_by_human THEN rationale ELSE excluded.rationale END,
                 updated_at = excluded.updated_at""",
            (hid, name, description, int(is_key), rationale, now, now),
        )
        return int(self._one("SELECT id FROM data_fields WHERE hypothesis_id = ? AND name = ?", (hid, name))["id"])

    def fields_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM data_fields WHERE hypothesis_id = ? ORDER BY is_key DESC, id", (hid,))

    def get_field(self, field_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM data_fields WHERE id = ?", (field_id,))

    def update_field(self, field_id: int, **fields: Any) -> None:
        allowed = {"status", "status_note", "is_key", "description", "edited_by_human"}
        if set(fields) - allowed:
            raise ValueError(f"Neznáme polia: {sorted(set(fields) - allowed)}")
        fields["updated_at"] = utcnow()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE data_fields SET {sets} WHERE id = ?", (*fields.values(), field_id))

    def delete_field(self, field_id: int) -> None:
        self._exec("DELETE FROM data_fields WHERE id = ?", (field_id,))

    # ------------------------------------------------------------------ zdroje
    def add_source(self, field_id: int, hid: int, name: str, url: str, origin: str, publisher: str | None = None,
                   access: str = "unknown", license: str | None = None, update_frequency: str | None = None,
                   formats: str | None = None, evidence_url: str | None = None, evidence_snippet: str | None = None,
                   checks: dict[str, Any] | None = None) -> int | None:
        cur = self._exec(
            """INSERT OR IGNORE INTO data_sources (field_id, hypothesis_id, name, url, publisher, access, license,
                   update_frequency, formats, evidence_url, evidence_snippet, checks_json, origin, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (field_id, hid, name, url, publisher, access, license, update_frequency, formats, evidence_url,
             evidence_snippet, json.dumps(checks or {}, ensure_ascii=False), origin, utcnow()),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def sources_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT s.*, f.name AS field_name, f.is_key FROM data_sources s JOIN data_fields f ON f.id = s.field_id
               WHERE s.hypothesis_id = ? ORDER BY f.is_key DESC, f.id, s.id""",
            (hid,),
        )

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM data_sources WHERE id = ?", (source_id,))

    def update_source(self, source_id: int, **fields: Any) -> None:
        allowed = {"status", "quality_json", "checks_json", "access", "license", "update_frequency", "formats"}
        if set(fields) - allowed:
            raise ValueError(f"Neznáme polia: {sorted(set(fields) - allowed)}")
        for key in ("quality_json", "checks_json"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE data_sources SET {sets} WHERE id = ?", (*fields.values(), source_id))

    # ------------------------------------------------------------------ datasety
    def add_dataset(self, hid: int, source_id: int | None, url: str, table_name: str, **fields: Any) -> int:
        cols = ["hypothesis_id", "source_id", "url", "table_name", "fetched_at"] + list(fields)
        vals = [hid, source_id, url, table_name, utcnow()] + [
            json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols[2:])
        self._exec(
            f"INSERT INTO datasets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT (hypothesis_id, url) DO UPDATE SET {sets}",
            tuple(vals),
        )
        return int(self._one("SELECT id FROM datasets WHERE hypothesis_id = ? AND url = ?", (hid, url))["id"])

    def datasets_for(self, hid: int, only_ok: bool = False) -> list[sqlite3.Row]:
        extra = " AND status = 'ok'" if only_ok else ""
        return self._all(f"SELECT * FROM datasets WHERE hypothesis_id = ?{extra} ORDER BY id", (hid,))

    def get_dataset(self, dataset_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))

    def has_dataset_url(self, hid: int, url: str) -> bool:
        return self._one("SELECT 1 FROM datasets WHERE hypothesis_id = ? AND url = ?", (hid, url)) is not None

    def datasets_total_bytes(self) -> int:
        return int(self._one("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM datasets WHERE status = 'ok'")["n"])

    def set_coverage(self, dataset_id: int, org_id: int, column: str, rows: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO dataset_coverage (dataset_id, org_id, column_name, rows) VALUES (?, ?, ?, ?)",
            (dataset_id, org_id, column, rows),
        )

    def clear_coverage(self, dataset_id: int) -> None:
        self._exec("DELETE FROM dataset_coverage WHERE dataset_id = ?", (dataset_id,))

    def coverage_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT c.*, d.table_name, d.url, o.name AS org_name, o.ico FROM dataset_coverage c
               JOIN datasets d ON d.id = c.dataset_id JOIN organizations o ON o.id = c.org_id
               WHERE d.hypothesis_id = ? ORDER BY c.rows DESC""",
            (hid,),
        )

    def organizations_with_ico(self) -> list[sqlite3.Row]:
        return self._all("SELECT id, name, ico FROM organizations WHERE ico IS NOT NULL AND ico != '' ORDER BY id")

    # ------------------------------------------------------------------ reprodukcia
    def add_reproduction(self, hid: int, claim_id: int | None, verdict: str, **fields: Any) -> int:
        cols = ["hypothesis_id", "claim_id", "verdict", "created_at"] + list(fields)
        vals = [hid, claim_id, verdict, utcnow()] + list(fields.values())
        cur = self._exec(f"INSERT INTO reproductions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals))
        return int(cur.lastrowid)

    def reproductions_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM reproductions WHERE hypothesis_id = ? ORDER BY id DESC", (hid,))

    def number_claims(self, hid: int, limit: int = 10) -> list[sqlite3.Row]:
        """Overené tvrdenia hypotézy s číslom – ciele na reprodukciu („známe číslo“)."""
        return self._all(
            """SELECT c.*, d.url, d.domain FROM hypothesis_claims hc JOIN claims c ON c.id = hc.claim_id
               JOIN documents d ON d.id = c.document_id
               WHERE hc.hypothesis_id = ? AND c.status = 'verified' AND c.numbers_json NOT IN ('[]', '') ORDER BY c.id
               LIMIT ?""",
            (hid, limit),
        )

    # ------------------------------------------------------------------ infozákon
    def add_info_request(self, hid: int, field_id: int | None, authority: str, subject: str, body: str) -> int:
        cur = self._exec(
            "INSERT INTO info_requests (hypothesis_id, field_id, authority, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hid, field_id, authority, subject, body, utcnow()),
        )
        return int(cur.lastrowid)

    def info_requests_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT r.*, f.name AS field_name FROM info_requests r LEFT JOIN data_fields f ON f.id = r.field_id
               WHERE r.hypothesis_id = ? ORDER BY r.id""",
            (hid,),
        )

    def update_info_request(self, request_id: int, **fields: Any) -> None:
        allowed = {"status", "sent_at", "due_at", "answered_at", "note", "body", "subject", "authority"}
        if set(fields) - allowed:
            raise ValueError(f"Neznáme polia: {sorted(set(fields) - allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE info_requests SET {sets} WHERE id = ?", (*fields.values(), request_id))

    def delete_draft_requests(self, field_id: int) -> int:
        """Neodoslané návrhy žiadostí pre pole, ku ktorému sa medzitým našiel otvorený zdroj."""
        return self._exec("DELETE FROM info_requests WHERE field_id = ? AND status = 'draft'", (field_id,)).rowcount

    # ------------------------------------------------------------------ rozhodnutia
    def add_gate_decision(self, gate: int, hid: int, decision: str, reason: str, metrics: dict[str, Any],
                          org_id: int | None = None) -> None:
        self._exec(
            "INSERT INTO gate_decisions (gate, hypothesis_id, org_id, decision, reason, metrics_json, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gate, hid, org_id, decision, reason, json.dumps(metrics, ensure_ascii=False), utcnow()),
        )

    def gate_decisions_for(self, hid: int, gate: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM gate_decisions WHERE hypothesis_id = ? AND gate = ? ORDER BY id DESC", (hid, gate))

    # ------------------------------------------------------------------ Brána 2
    def add_experiment(self, hid: int, org_id: int | None, region: str | None, statement: str, metric: str,
                       baseline: str, success_rule: str, spec: dict[str, Any]) -> int:
        now = utcnow()
        cur = self._exec(
            """INSERT INTO experiments (hypothesis_id, org_id, region, statement, metric, baseline, success_rule,
                   spec_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hid, org_id, region, statement, metric, baseline, success_rule, json.dumps(spec, ensure_ascii=False), now, now),
        )
        return int(cur.lastrowid)

    def experiments_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT e.*, o.name AS org_name, o.ico FROM experiments e LEFT JOIN organizations o ON o.id = e.org_id
               WHERE e.hypothesis_id = ? ORDER BY e.id""",
            (hid,),
        )

    def get_experiment(self, exp_id: int) -> sqlite3.Row | None:
        return self._one(
            "SELECT e.*, o.name AS org_name, o.ico FROM experiments e LEFT JOIN organizations o ON o.id = e.org_id "
            "WHERE e.id = ?", (exp_id,))

    def update_experiment(self, exp_id: int, **fields: Any) -> None:
        allowed = {"statement", "metric", "baseline", "success_rule", "spec_json", "prereg_hash", "locked_at", "status",
                   "result_json", "flags_json", "demo_path", "expert_json", "region"}
        if set(fields) - allowed:
            raise ValueError(f"Neznáme polia: {sorted(set(fields) - allowed)}")
        exp = self.get_experiment(exp_id)
        locked = {"statement", "metric", "baseline", "success_rule", "spec_json"} & set(fields)
        if exp is not None and exp["locked_at"] and locked:
            raise ValueError("Experiment je predregistrovaný (uzamknutý); hypotéza, metrika a postup sa už nemenia.")
        for key in ("spec_json", "result_json", "flags_json", "expert_json"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = utcnow()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE experiments SET {sets} WHERE id = ?", (*fields.values(), exp_id))
