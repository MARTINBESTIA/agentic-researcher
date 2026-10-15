"""Oslovenia podľa účelu (schéma v13): prehľady domén, návrhy prínosu, výsledky oslovení a cloudové náklady
s cache odpovedí – jediné miesto s ich SQL."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


VALUE_FIELDS = {"proposal_json", "need_status", "evidence_refs_json", "checks_json", "status",
                "delivery_commitment_approved", "commercial_policy_version", "inputs_hash", "model", "brief_id"}
OUTCOME_FIELDS = {"outreach_id", "person_id", "org_id", "proposal_id", "message_version", "reply", "problem_relevance",
                  "sample_obtained", "interview_agreed", "demo_done", "pilot_agreed", "value_shown", "paid",
                  "rejection_reason", "next_action", "note", "prep_minutes", "delivery_days"}


class ValueMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ prehľady domén
    def add_brief(self, domain_key: str, title: str, brief: dict[str, Any], sources: list[dict[str, Any]],
                  inputs_hash: str, checks: dict[str, Any], status: str, model: str | None, reason: str | None) -> int:
        """Nová verzia prehľadu domény; predchádzajúce ostávajú (stav superseded) – história je dohľadateľná."""
        row = self._one("SELECT MAX(version) AS v FROM domain_briefs WHERE domain_key = ?", (domain_key,))
        version = int(row["v"] or 0) + 1
        self._exec("UPDATE domain_briefs SET status = 'superseded' WHERE domain_key = ? AND status != 'superseded'",
                   (domain_key,))
        cur = self._exec(
            """INSERT INTO domain_briefs (domain_key, title, version, brief_json, sources_json, inputs_hash, checks_json,
                                          status, model, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (domain_key, title, version, _dumps(brief), _dumps(sources), inputs_hash, _dumps(checks), status, model,
             reason, utcnow()))
        return int(cur.lastrowid)

    def latest_brief(self, domain_key: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_briefs WHERE domain_key = ? ORDER BY version DESC LIMIT 1", (domain_key,))

    def brief_history(self, domain_key: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM domain_briefs WHERE domain_key = ? ORDER BY version DESC", (domain_key,))

    def get_brief(self, brief_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_briefs WHERE id = ?", (brief_id,))

    def update_brief(self, brief_id: int, **fields: Any) -> None:
        allowed = {"checks_json", "status"}
        if set(fields) - allowed:
            raise ValueError(f"Nepovolené polia prehľadu: {set(fields) - allowed}")
        vals = [_dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self._exec(f"UPDATE domain_briefs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*vals, brief_id))

    # ------------------------------------------------------------------ návrhy prínosu
    def add_value_proposal(self, hid: int, recipient_kind: str, proposal: dict[str, Any], **fields: Any) -> int:
        bad = set(fields) - VALUE_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia návrhu prínosu: {bad}")
        now = utcnow()
        cols = ["hypothesis_id", "recipient_kind", "proposal_json", "created_at", "updated_at"] + list(fields)
        vals = [hid, recipient_kind, _dumps(proposal), now, now] + [
            _dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        cur = self._exec(f"INSERT INTO value_proposals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def update_value_proposal(self, proposal_id: int, **fields: Any) -> None:
        bad = set(fields) - VALUE_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia návrhu prínosu: {bad}")
        vals = [_dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self._exec(f"UPDATE value_proposals SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE id = ?",
                   (*vals, utcnow(), proposal_id))

    def get_value_proposal(self, proposal_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM value_proposals WHERE id = ?", (proposal_id,))

    def value_proposals_for(self, hid: int, recipient_kind: str | None = None) -> list[sqlite3.Row]:
        if recipient_kind is None:
            return self._all("SELECT * FROM value_proposals WHERE hypothesis_id = ? ORDER BY recipient_kind, id",
                             (hid,))
        return self._all("SELECT * FROM value_proposals WHERE hypothesis_id = ? AND recipient_kind = ? ORDER BY id",
                         (hid, recipient_kind))

    def delete_value_proposals(self, hid: int, recipient_kind: str, keep_approved: bool = True) -> None:
        extra = " AND delivery_commitment_approved = 0 AND status != 'approved'" if keep_approved else ""
        self._exec(f"DELETE FROM value_proposals WHERE hypothesis_id = ? AND recipient_kind = ?{extra}",
                   (hid, recipient_kind))

    # ------------------------------------------------------------------ výsledky oslovení
    def add_outcome(self, hid: int, **fields: Any) -> int:
        bad = set(fields) - OUTCOME_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia výsledku: {bad}")
        cols = ["hypothesis_id", "recorded_at"] + list(fields)
        vals = [hid, utcnow()] + [int(v) if isinstance(v, bool) else v for v in fields.values()]
        cur = self._exec(f"INSERT INTO outreach_outcomes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def outcomes_for(self, hid: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT x.*, o.name AS org_name FROM outreach_outcomes x LEFT JOIN organizations o ON o.id = x.org_id"""
        if hid is None:
            return self._all(sql + " ORDER BY x.id")
        return self._all(sql + " WHERE x.hypothesis_id = ? ORDER BY x.id", (hid,))

    def delete_outcomes_of_person(self, person_id: int) -> int:
        return self._exec("DELETE FROM outreach_outcomes WHERE person_id = ?", (person_id,)).rowcount

    # ------------------------------------------------------------------ cloud: náklady a cache
    def reserve_cloud(self, provider: str, profile: str | None, deployment: str | None, task: str, reserve_eur: float,
                      limits: dict[str, tuple[str, float]], rate: dict[str, Any]) -> int | None:
        """Rezervácia horného odhadu ceny pred volaním – v jednej transakcii s kontrolou limitov (aj pri súbežných
        volaniach). limits: názov → (od kedy ISO, limit EUR). Vráti id rezervácie, None = limit by sa prekročil."""
        conn = self.conn
        conn.commit()  # žiadna otvorená implicitná transakcia
        conn.execute("BEGIN IMMEDIATE")
        try:
            for since, limit in limits.values():
                used = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status IN ('reserved', 'uncertain') THEN reserved_eur "
                    "ELSE COALESCE(cost_eur, 0) END), 0) FROM llm_usage WHERE created_at >= ?", (since,)).fetchone()[0]
                if used + reserve_eur > limit + 1e-9:
                    conn.execute("ROLLBACK")
                    return None
            cur = conn.execute(
                "INSERT INTO llm_usage (provider, profile, deployment, task, status, reserved_eur, rate_json, created_at) "
                "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)",
                (provider, profile, deployment, task, reserve_eur, _dumps(rate), utcnow()))
            conn.execute("COMMIT")
            return int(cur.lastrowid)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def settle_cloud(self, usage_id: int, status: str, cost_eur: float | None, prompt_tokens: int | None = None,
                     completion_tokens: int | None = None, reasoning_tokens: int | None = None,
                     note: str | None = None) -> None:
        """Vyrovnanie po odpovedi (settled), zlyhanie bez odpovede (failed, rezervácia sa uvoľní) alebo neistý výsledok
        (uncertain – napr. timeout: rezervácia ostáva započítaná)."""
        self._exec("UPDATE llm_usage SET status = ?, cost_eur = ?, prompt_tokens = ?, completion_tokens = ?, "
                   "reasoning_tokens = ?, note = ?, settled_at = ? WHERE id = ?",
                   (status, cost_eur, prompt_tokens, completion_tokens, reasoning_tokens, note, utcnow(), usage_id))

    def cloud_spend(self, since: str) -> dict[str, Any]:
        row = self._one(
            "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN cost_eur END), 0) AS settled, "
            "COALESCE(SUM(CASE WHEN status IN ('reserved', 'uncertain') THEN reserved_eur END), 0) AS open, "
            "COUNT(*) AS calls, COALESCE(SUM(CASE WHEN status = 'cached' THEN 1 END), 0) AS cached, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM llm_usage WHERE created_at >= ?", (since,))
        return dict(row)

    def record_cloud_cache(self, provider: str, deployment: str | None, task: str) -> None:
        """Odpoveď z cache: bez volania a bez nákladov (iba na prehľad v GUI)."""
        self._exec("INSERT INTO llm_usage (provider, profile, deployment, task, status, reserved_eur, cost_eur, created_at, "
                   "settled_at) VALUES (?, ?, ?, ?, 'cached', 0, 0, ?, ?)",
                   (provider, provider, deployment, task, utcnow(), utcnow()))

    def cloud_usage(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (limit,))

    def llm_cache_get(self, key: str) -> str | None:
        row = self._one("SELECT response FROM llm_cache WHERE key = ?", (key,))
        return row["response"] if row is not None else None

    def llm_cache_put(self, key: str, provider: str, deployment: str | None, task: str, response: str) -> None:
        self._exec("INSERT OR REPLACE INTO llm_cache (key, provider, deployment, task, response, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (key, provider, deployment, task, response, utcnow()))
