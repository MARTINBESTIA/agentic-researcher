"""SQL pre event logy (dodatok 16. 10. 2026): žiadosti o event log, importy (metadáta – udalosti sú v DuckDB
organizácie) a procesné reporty."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

REQUEST_FIELDS = {"hypothesis_id", "organization_id", "outreach_id", "step", "process_scope", "case_definition",
                  "fields_json", "sample_scope", "intended_output", "why_event_log", "unknowns_json", "status",
                  "alternative", "text", "card_id"}
IMPORT_FIELDS = {"request_id", "hypothesis_id", "file_name", "file_format", "data_kind", "columns_json", "mapping_json",
                 "mapping_version", "mapping_confirmed", "timezone_policy", "case_definition", "period",
                 "transformations_json", "quality_json", "availability_json", "rows_total", "retained_rows",
                 "quarantined_rows", "status"}
REPORT_FIELDS = {"analysis_version", "mapping_version", "supported_json", "results_json", "denominators_json",
                 "limitations_json", "followups_json", "review_json", "status"}


def _prep(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: (json.dumps(v, ensure_ascii=False, default=str) if k.endswith("_json") and not isinstance(v, (str, type(None)))
                else v) for k, v in fields.items()}


class EventLogMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ žiadosti
    def add_eventlog_request(self, **fields: Any) -> int:
        bad = set(fields) - REQUEST_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia žiadosti: {bad}")
        data = {**_prep(fields), "created_at": utcnow(), "updated_at": utcnow()}
        cur = self._exec(f"INSERT INTO eventlog_requests ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                         list(data.values()))
        return int(cur.lastrowid)

    def get_eventlog_request(self, request_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM eventlog_requests WHERE id = ?", (request_id,))

    def eventlog_requests(self, hid: int | None = None, org_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT r.*, o.name AS org_name FROM eventlog_requests r LEFT JOIN organizations o "
               "ON o.id = r.organization_id WHERE 1 = 1")
        params: list[Any] = []
        if hid is not None:
            sql += " AND r.hypothesis_id = ?"
            params.append(hid)
        if org_id is not None:
            sql += " AND r.organization_id = ?"
            params.append(org_id)
        return self._all(sql + " ORDER BY r.id DESC", params)

    def update_eventlog_request(self, request_id: int, **fields: Any) -> None:
        bad = set(fields) - REQUEST_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia žiadosti: {bad}")
        data = {**_prep(fields), "updated_at": utcnow()}
        self._exec(f"UPDATE eventlog_requests SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                   [*data.values(), request_id])

    # ------------------------------------------------------------------ importy
    def eventlog_import_by_hash(self, org_id: int, file_hash: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM eventlog_imports WHERE organization_id = ? AND file_hash = ?", (org_id, file_hash))

    def add_eventlog_import(self, org_id: int, file_hash: str, **fields: Any) -> int:
        bad = set(fields) - IMPORT_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia importu: {bad}")
        data = {"organization_id": org_id, "file_hash": file_hash, **_prep(fields), "created_at": utcnow(),
                "updated_at": utcnow()}
        data.setdefault("status", "uploaded")
        cur = self._exec(f"INSERT INTO eventlog_imports ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                         list(data.values()))
        return int(cur.lastrowid)

    def get_eventlog_import(self, import_id: int) -> sqlite3.Row | None:
        return self._one("SELECT i.*, o.name AS org_name FROM eventlog_imports i LEFT JOIN organizations o "
                         "ON o.id = i.organization_id WHERE i.id = ?", (import_id,))

    def eventlog_imports(self, org_id: int | None = None, hid: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT i.*, o.name AS org_name FROM eventlog_imports i LEFT JOIN organizations o "
               "ON o.id = i.organization_id WHERE 1 = 1")
        params: list[Any] = []
        if org_id is not None:
            sql += " AND i.organization_id = ?"
            params.append(org_id)
        if hid is not None:
            sql += " AND i.hypothesis_id = ?"
            params.append(hid)
        return self._all(sql + " ORDER BY i.id DESC", params)

    def update_eventlog_import(self, import_id: int, **fields: Any) -> None:
        bad = set(fields) - IMPORT_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia importu: {bad}")
        data = {**_prep(fields), "updated_at": utcnow()}
        self._exec(f"UPDATE eventlog_imports SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                   [*data.values(), import_id])

    # ------------------------------------------------------------------ procesné reporty
    def add_process_report(self, import_id: int, **fields: Any) -> int:
        bad = set(fields) - REPORT_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia reportu: {bad}")
        data = {"import_id": import_id, **_prep(fields), "created_at": utcnow()}
        cur = self._exec(f"INSERT INTO process_reports ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                         list(data.values()))
        return int(cur.lastrowid)

    def process_reports(self, import_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM process_reports WHERE import_id = ? ORDER BY id DESC", (import_id,))

    def get_process_report(self, report_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM process_reports WHERE id = ?", (report_id,))

    def update_process_report(self, report_id: int, **fields: Any) -> None:
        bad = set(fields) - REPORT_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia reportu: {bad}")
        data = _prep(fields)
        self._exec(f"UPDATE process_reports SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                   [*data.values(), report_id])
