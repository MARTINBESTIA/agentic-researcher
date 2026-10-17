"""Od doménového prehľadu ku konkrétnej ponuke (schéma v15): posúdenia dôkazov voči cieľu, karty organizácií
s verziami a výskumné úlohy z medzier karty – jediné miesto s ich SQL."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


ASSESSMENT_FIELDS = ("organization_id", "publisher_type", "meaning", "relevance", "relevance_reason", "need_state",
                     "process", "role", "program", "jurisdiction", "published_at", "event_period", "retrieved_at",
                     "assessed_by", "model")
CARD_FIELDS = {"status", "card_json", "sources_json", "checks_json", "readiness_json", "evaluation_json", "selected_option",
               "stale_reason", "conflict_json", "approved_at", "brief_id", "provider", "model"}
TASK_FIELDS = {"status", "result_json", "sources_json", "next_step", "queries_json", "attempts", "cost_json", "priority"}
OPEN_CARD_STATUSES = ("draft", "checked", "needs_review", "pending_review", "stale", "edited")


class OpportunityMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ posúdenia dôkazov
    def upsert_assessment(self, target_key: str, logic_version: str, *, claim_id: int | None = None,
                          domain_evidence_id: int | None = None, **fields: Any) -> int:
        """Posúdenie zdroja voči cieľu; rovnaká verzia logiky prepíše predošlé (človek má prednosť pred modelom)."""
        bad = set(fields) - set(ASSESSMENT_FIELDS)
        if bad:
            raise ValueError(f"Neznáme polia posúdenia: {bad}")
        row = self._one(
            """SELECT id, assessed_by FROM evidence_assessments WHERE target_key = ? AND logic_version = ?
               AND claim_id IS ? AND domain_evidence_id IS ?""", (target_key, logic_version, claim_id, domain_evidence_id))
        if row is not None:
            if row["assessed_by"] == "human" and fields.get("assessed_by") != "human":
                return int(row["id"])
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE evidence_assessments SET {sets} WHERE id = ?", (*fields.values(), row["id"]))
            return int(row["id"])
        cols = ["target_key", "logic_version", "claim_id", "domain_evidence_id", "created_at"] + list(fields)
        vals = [target_key, logic_version, claim_id, domain_evidence_id, utcnow()] + list(fields.values())
        cur = self._exec(f"INSERT INTO evidence_assessments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def assessments_for(self, target_key: str, logic_version: str | None = None) -> list[sqlite3.Row]:
        if logic_version is None:
            return self._all("SELECT * FROM evidence_assessments WHERE target_key = ? ORDER BY id", (target_key,))
        return self._all("SELECT * FROM evidence_assessments WHERE target_key = ? AND logic_version = ? ORDER BY id",
                         (target_key, logic_version))

    # ------------------------------------------------------------------ karty organizácií
    def add_org_card(self, org_id: int, hid: int, process_key: str, recipient_role: str, card: dict[str, Any], *,
                     inputs_hash: str, status: str, author: str = "model", **fields: Any) -> int:
        """Nová verzia karty (organizácia + hypotéza + pracovný prípad + rola). Predošlé verzie ostávajú (superseded),
        schválenie staršej verzie sa neprenáša potichu – nová verzia sa schvaľuje znova."""
        extra = {k: v for k, v in fields.items() if k in CARD_FIELDS | {"schema_version", "prompt_version", "policy_version"}}
        bad = set(fields) - set(extra)
        if bad:
            raise ValueError(f"Neznáme polia karty: {bad}")
        row = self._one("""SELECT MAX(version) AS v FROM org_cards WHERE organization_id = ? AND hypothesis_id = ?
                           AND process_key = ? AND recipient_role = ?""", (org_id, hid, process_key, recipient_role))
        version = int(row["v"] or 0) + 1
        self._exec("""UPDATE org_cards SET status = 'superseded', updated_at = ? WHERE organization_id = ?
                      AND hypothesis_id = ? AND process_key = ? AND recipient_role = ? AND status != 'superseded'""",
                   (utcnow(), org_id, hid, process_key, recipient_role))
        now = utcnow()
        cols = ["organization_id", "hypothesis_id", "process_key", "recipient_role", "version", "status", "card_json",
                "inputs_hash", "author", "created_at", "updated_at"] + list(extra)
        vals = [org_id, hid, process_key, recipient_role, version, status, _dumps(card), inputs_hash, author, now, now] + [
            _dumps(v) if isinstance(v, (dict, list)) else v for v in extra.values()]
        cur = self._exec(f"INSERT INTO org_cards ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def update_org_card(self, card_id: int, **fields: Any) -> None:
        bad = set(fields) - CARD_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia karty: {bad}")
        vals = [_dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self._exec(f"UPDATE org_cards SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE id = ?",
                   (*vals, utcnow(), card_id))

    def get_org_card(self, card_id: int) -> sqlite3.Row | None:
        return self._one("""SELECT c.*, o.name AS org_name FROM org_cards c JOIN organizations o
                            ON o.id = c.organization_id WHERE c.id = ?""", (card_id,))

    def org_cards_for(self, hid: int | None = None, org_id: int | None = None, current_only: bool = True) -> list[sqlite3.Row]:
        """Karty (predvolene iba aktuálne verzie) hypotézy alebo organizácie."""
        sql = ("SELECT c.*, o.name AS org_name FROM org_cards c JOIN organizations o ON o.id = c.organization_id WHERE 1 = 1"
               + (" AND c.status != 'superseded'" if current_only else ""))
        params: list[Any] = []
        if hid is not None:
            sql += " AND c.hypothesis_id = ?"
            params.append(hid)
        if org_id is not None:
            sql += " AND c.organization_id = ?"
            params.append(org_id)
        return self._all(sql + " ORDER BY c.organization_id, c.process_key, c.recipient_role, c.version DESC", tuple(params))

    def card_history(self, org_id: int, hid: int, process_key: str, recipient_role: str) -> list[sqlite3.Row]:
        return self._all("""SELECT * FROM org_cards WHERE organization_id = ? AND hypothesis_id = ? AND process_key = ?
                            AND recipient_role = ? ORDER BY version DESC""", (org_id, hid, process_key, recipient_role))

    def mark_org_cards_stale(self, reason: str, *, org_id: int | None = None, hid: int | None = None,
                             card_ids: list[int] | None = None) -> int:
        """Označí závislé karty na obnovu (bez prepísania; schválená karta ostane schválená, zmena sa zobrazí)."""
        sql = "UPDATE org_cards SET stale_reason = ?, updated_at = ? WHERE status != 'superseded'"
        params: list[Any] = [reason, utcnow()]
        if card_ids is not None:
            if not card_ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(card_ids))})"
            params += card_ids
        if org_id is not None:
            sql += " AND organization_id = ?"
            params.append(org_id)
        if hid is not None:
            sql += " AND hypothesis_id = ?"
            params.append(hid)
        return self._exec(sql, tuple(params)).rowcount

    # ------------------------------------------------------------------ výskumné úlohy
    def add_research_task(self, card_id: int, question: str, decision_changed: str, best_source: str, *,
                          gap_part: str | None = None, priority: int = 2, queries: list[str] | None = None,
                          status: str = "open", next_step: str | None = None) -> int:
        now = utcnow()
        cur = self._exec(
            """INSERT INTO research_tasks (card_id, question, gap_part, decision_changed, best_source, queries_json,
                                           status, next_step, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_id, question, gap_part, decision_changed, best_source, _dumps(queries or []), status, next_step,
             priority, now, now))
        return int(cur.lastrowid)

    def research_tasks_for(self, card_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM research_tasks WHERE card_id = ? ORDER BY priority, id", (card_id,))

    def get_research_task(self, task_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM research_tasks WHERE id = ?", (task_id,))

    def update_research_task(self, task_id: int, **fields: Any) -> None:
        bad = set(fields) - TASK_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia výskumnej úlohy: {bad}")
        vals = [_dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self._exec(f"UPDATE research_tasks SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE id = ?",
                   (*vals, utcnow(), task_id))

    def research_results_for_org(self, org_id: int, hid: int) -> list[sqlite3.Row]:
        """Výsledky cielených úloh pre organizáciu a hypotézu (vstup ďalšej verzie karty)."""
        return self._all("""SELECT t.* FROM research_tasks t JOIN org_cards c ON c.id = t.card_id
                            WHERE c.organization_id = ? AND c.hypothesis_id = ? AND t.status != 'open'
                            ORDER BY t.id""", (org_id, hid))
