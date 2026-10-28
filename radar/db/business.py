"""SQL pre obchodné vyhodnotenie (audit 26. 10. 2026): pôvod hypotéz a lieviky podľa zdroja (M36) a kohorty benchmarku
(M40). Iba agregáty a väzby na hypotézy – osobné údaje ostávajú v tabuľkách ľudí, kontaktov, oslovení a rozhovorov."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

REPLIED_STATUSES = ("replied", "scheduled", "interviewed", "declined", "no_show")
BENCHMARK_FIELDS = {"source_url", "note", "prep_minutes"}
ECONOMICS_FIELDS = {"factor", "low", "high", "unit", "value_text", "provenance", "source_kind", "document_id", "quote",
                    "outcome_id", "interview_id", "evidence"}


class BusinessMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------- M36: zdroj → obchodný výsledok
    def research_yield(self) -> list[sqlite3.Row]:
        """Výskumný lievik: dokumenty podľa druhu a kategórie zdroja (bez podkladov Brány 1), s overeným tvrdením."""
        return self._all(
            """SELECT d.source_kind, COALESCE(d.source_category, '') AS category, COUNT(*) AS documents,
                      SUM(CASE WHEN v.n IS NULL THEN 0 ELSE 1 END) AS with_verified,
                      COALESCE(SUM(v.n), 0) AS verified_claims
               FROM documents d
               LEFT JOIN (SELECT document_id, COUNT(*) AS n FROM claims WHERE status = 'verified'
                          GROUP BY document_id) v ON v.document_id = d.id
               WHERE d.source_kind != 'gate1'
               GROUP BY d.source_kind, COALESCE(d.source_category, '')"""
        )

    def hypothesis_origins(self) -> list[sqlite3.Row]:
        """Pôvod hypotézy = dokument jej najstaršieho priradeného tvrdenia (najnižšie ID tvrdenia)."""
        return self._all(
            """SELECT h.id, h.origin, h.status, h.people_enabled, h.validation_json, d.source_kind, d.source_category
               FROM hypotheses h
               LEFT JOIN (SELECT hypothesis_id, MIN(claim_id) AS claim_id FROM hypothesis_claims
                          GROUP BY hypothesis_id) f ON f.hypothesis_id = h.id
               LEFT JOIN claims c ON c.id = f.claim_id
               LEFT JOIN documents d ON d.id = c.document_id
               ORDER BY h.id"""
        )

    def outreach_by_hypothesis(self) -> dict[int, dict[str, Any]]:
        """Odoslané správy, oslovené a odpovedajúce organizácie (osoba bez organizácie sa ráta zvlášť) a prvé odoslanie."""
        marks = ", ".join("?" * len(REPLIED_STATUSES))
        rows = self._all(
            f"""SELECT hypothesis_id, SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
                       COUNT(DISTINCT CASE WHEN sent_at IS NOT NULL THEN COALESCE(org_id, -person_id) END) AS sent_orgs,
                       COUNT(DISTINCT CASE WHEN status IN ({marks}) THEN COALESCE(org_id, -person_id) END) AS replied,
                       MIN(sent_at) AS first_sent
                FROM outreach GROUP BY hypothesis_id""",
            REPLIED_STATUSES,
        )
        return {r["hypothesis_id"]: dict(r) for r in rows}

    def customer_interviews_by_hypothesis(self) -> dict[int, int]:
        rows = self._all("SELECT hypothesis_id, COUNT(*) AS n FROM interviews WHERE interviewee_kind = 'customer' "
                         "GROUP BY hypothesis_id")
        return {r["hypothesis_id"]: r["n"] for r in rows}

    def outcomes_by_hypothesis(self) -> dict[int, dict[str, int]]:
        """Odpovede a potvrdený problém z výsledkov oslovení (unikátne organizácie)."""
        rows = self._all(
            """SELECT hypothesis_id,
                      COUNT(DISTINCT CASE WHEN reply IS NOT NULL AND reply != 'none'
                                          THEN COALESCE(org_id, -person_id) END) AS replies,
                      COUNT(DISTINCT CASE WHEN problem_relevance = 'confirmed'
                                          THEN COALESCE(org_id, -person_id) END) AS confirmed
               FROM outreach_outcomes GROUP BY hypothesis_id"""
        )
        return {r["hypothesis_id"]: dict(r) for r in rows}

    def hypotheses_with_commercial_evidence(self) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT hypothesis_id FROM commercial_evidence WHERE status = 'active'")]

    def ted_hypotheses(self) -> set[int]:
        """Hypotézy so zákazkou z TED, ktorú model posúdil ako zhodnú alebo čiastočne zhodnú (M37)."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT hypothesis_id FROM procurement_matches WHERE verdict IN ('yes', 'partial')")}

    # ------------------------------------------------------------- M40: kohorty benchmarku
    def add_benchmark_member(self, hid: int, cohort: str, source_url: str | None = None, note: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO benchmark_members (hypothesis_id, cohort, source_url, note, added_at) VALUES (?, ?, ?, ?, ?)",
            (hid, cohort, source_url, note, utcnow()),
        )
        return int(cur.lastrowid)

    def benchmark_members(self, cohort: str | None = None) -> list[sqlite3.Row]:
        where, params = ("WHERE b.cohort = ?", (cohort,)) if cohort else ("", ())
        return self._all(
            f"""SELECT b.*, h.title, h.origin, h.status AS hypothesis_status
                FROM benchmark_members b JOIN hypotheses h ON h.id = b.hypothesis_id {where}
                ORDER BY b.cohort, b.id""",
            params,
        )

    def benchmark_member(self, hid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM benchmark_members WHERE hypothesis_id = ?", (hid,))

    def update_benchmark_member(self, hid: int, **fields: Any) -> None:
        bad = set(fields) - BENCHMARK_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia: {bad}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE benchmark_members SET {sets} WHERE hypothesis_id = ?", (*fields.values(), hid))

    def remove_benchmark_member(self, hid: int) -> None:
        self._exec("DELETE FROM benchmark_members WHERE hypothesis_id = ?", (hid,))

    # ------------------------------------------------------------- M38: ekonomika u zákazníka
    def add_buyer_factor(self, hid: int, org_id: int, recorded_by: str = "human", person_id: int | None = None,
                         **fields: Any) -> int:
        bad = set(fields) - ECONOMICS_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia: {bad}")
        now = utcnow()
        cols = ["hypothesis_id", "org_id", "person_id", "recorded_by", "created_at", "updated_at", *fields]
        cur = self._exec(f"INSERT INTO buyer_economics ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         (hid, org_id, person_id, recorded_by, now, now, *fields.values()))
        return int(cur.lastrowid)

    def buyer_factors(self, hid: int, org_id: int, include_withdrawn: bool = False) -> list[sqlite3.Row]:
        extra = "" if include_withdrawn else "AND status = 'active'"
        return self._all(f"SELECT * FROM buyer_economics WHERE hypothesis_id = ? AND org_id = ? {extra} ORDER BY id",
                         (hid, org_id))

    def withdraw_buyer_factor(self, factor_id: int) -> None:
        self._exec("UPDATE buyer_economics SET status = 'withdrawn', updated_at = ? WHERE id = ?", (utcnow(), factor_id))

    def orgs_with_buyer_factors(self, hid: int) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT org_id FROM buyer_economics WHERE hypothesis_id = ? AND status = 'active' ORDER BY org_id",
            (hid,))]

    def document_by_url(self, url: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM documents WHERE url = ? ORDER BY fetched_at DESC LIMIT 1", (url.strip(),))

    # ------------------------------------------------------------- M39: alternatívy a medzera
    def add_alternative_assessment(self, hid: int, inputs_hash: str, sources: list, alternatives: list, proposal: str,
                                   reason: str | None) -> int:
        cur = self._exec(
            """INSERT INTO alternative_assessments (hypothesis_id, inputs_hash, sources_json, alternatives_json, proposal,
                                                    proposal_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (hid, inputs_hash, json.dumps(sources, ensure_ascii=False), json.dumps(alternatives, ensure_ascii=False),
             proposal, reason, utcnow()),
        )
        return int(cur.lastrowid)

    def latest_alternative_assessment(self, hid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM alternative_assessments WHERE hypothesis_id = ? ORDER BY id DESC LIMIT 1", (hid,))

    def get_alternative_assessment(self, assessment_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM alternative_assessments WHERE id = ?", (assessment_id,))

    def decide_alternative_assessment(self, assessment_id: int, decision: str, note: str | None) -> None:
        self._exec("UPDATE alternative_assessments SET decision = ?, decision_note = ?, decided_at = ? WHERE id = ?",
                   (decision, note, utcnow(), assessment_id))

    def set_gap_status(self, hid: int, status: str | None, note: str | None) -> None:
        self._exec("UPDATE hypotheses SET gap_status = ?, gap_note = ?, gap_decided_at = ?, updated_at = ? WHERE id = ?",
                   (status, note, utcnow(), utcnow(), hid))

    def gap_closed_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM hypotheses WHERE gap_status = 'no_defensible_gap'")}

    def scrub_buyer_factors_of_person(self, person_id: int) -> int:
        """Forget: údaj o organizácii ostane, väzba na osobu a text „kto a kedy“ nie."""
        cur = self._exec("UPDATE buyer_economics SET person_id = NULL, evidence = '(osoba vymazaná)', updated_at = ? "
                         "WHERE person_id = ?", (utcnow(), person_id))
        return cur.rowcount
