"""Agregátor Brány 0: organizácie, ľudia, kontakty, väzby, mapy rolí, oslovenia, rozhovory,
rozhodnutia a zoznam potlačenia. Osobné údaje sú iba v tabuľkách people, contact_points,
outreach a interviews (CLAUDE.md, Súkromie)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow

ORG_FIELDS = {
    "name", "ico", "legal_form", "sk_nace", "size_category", "sector_kind", "region", "municipality", "website",
    "website_verified", "website_evidence", "source", "source_url", "excluded", "register_json",
}
PERSON_FIELDS = {
    "full_name", "titles", "role_title", "role_category", "gender_hint", "source_url", "source_snippet", "source_kind",
    "is_expert", "status", "note", "last_interaction_at", "purge_after", "org_id",
}
OUTREACH_FIELDS = {
    "subject", "body", "facts_json", "checks_json", "status", "approved_at", "sent_at", "follow_up_due",
    "follow_up_count", "contact_point_id", "mode", "proposal_id", "package_json", "readiness", "message_version",
    "card_id", "card_version",
}
PERSON_STATUSES = ("candidate", "approved", "contacted", "replied", "interviewed", "declined", "excluded")
OUTREACH_STATUSES = (
    "draft", "needs_edit", "approved", "sent", "replied", "no_reply", "bounced", "declined", "objected",
    "scheduled", "interviewed", "no_show", "blocked",
)
# Poradie dôvodov zaradenia organizácie (R-ORG-5): nižšie číslo = vyššia priorita. data_holder (drží dáta k hypotéze),
# contact_search (zákazník z hľadania podľa hypotézy) a expert_org (zväz, výskum, watchdog) pribudli v schéme v10.
RELATION_RANK = {"mentioned_with_problem": 0, "quoted_person": 1, "uses_solution": 2, "procured_related": 2,
                 "data_holder": 3, "contact_search": 4, "segment_match": 5, "expert_org": 6, "manual": 7}
CONTACT_TARGET_KINDS = ("customer", "data_holder", "expert")


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class Gate0Mixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------ organizácie
    def find_organization(self, ico: str | None = None, name_key: str | None = None) -> sqlite3.Row | None:
        if ico:
            row = self._one("SELECT * FROM organizations WHERE ico = ?", (ico,))
            if row is not None:
                return row
        if name_key:
            return self._one("SELECT * FROM organizations WHERE name_key = ? ORDER BY ico IS NULL, id LIMIT 1", (name_key,))
        return None

    def upsert_organization(self, name: str, name_key: str, source: str, ico: str | None = None, **fields: Any) -> tuple[int, bool]:
        """Vráti (id, nová). Deduplikácia podľa IČO, inak podľa normalizovaného názvu.
        Existujúce hodnoty sa neprepisujú prázdnymi; IČO sa doplní, ak chýbalo."""
        bad = set(fields) - ORG_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia organizácie: {bad}")
        row = self.find_organization(ico, name_key)
        now = utcnow()
        if row is None:
            cols = {"name": name, "name_key": name_key, "ico": ico, "source": source, "created_at": now, "updated_at": now, **fields}
            marks = ", ".join("?" * len(cols))
            cur = self._exec(f"INSERT INTO organizations ({', '.join(cols)}) VALUES ({marks})", tuple(cols.values()))
            return int(cur.lastrowid), True
        updates = {k: v for k, v in {"ico": ico, **fields}.items() if v not in (None, "") and row[k] in (None, "", 0)}
        if updates:
            self.update_organization(int(row["id"]), **updates)
        return int(row["id"]), False

    def update_organization(self, org_id: int, **fields: Any) -> None:
        bad = set(fields) - ORG_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia organizácie: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE organizations SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), utcnow(), org_id))

    def find_organization_by_website(self, url: str) -> sqlite3.Row | None:
        """Organizácia s rovnakým webom (bez www a koncovej lomky) – aby sa web nesťahoval znova."""
        key = url.lower().replace("://www.", "://").rstrip("/")
        for row in self._all("SELECT * FROM organizations WHERE website IS NOT NULL AND website_verified = 1"):
            if (row["website"] or "").lower().replace("://www.", "://").rstrip("/") == key:
                return row
        return None

    def get_organization(self, org_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM organizations WHERE id = ?", (org_id,))

    def all_organizations(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM organizations ORDER BY id")

    def link_org_hypothesis(
        self,
        org_id: int,
        hid: int,
        relation: str,
        evidence_claim_id: int | None = None,
        evidence_url: str | None = None,
        fit: dict[str, Any] | None = None,
        target_id: int | None = None,
    ) -> bool:
        """Zaradí organizáciu k hypotéze; pri opakovaní ponechá silnejší dôvod (R-ORG-5). Vráti True, ak je nová."""
        row = self._one("SELECT * FROM org_hypotheses WHERE org_id = ? AND hypothesis_id = ?", (org_id, hid))
        if row is None:
            self._exec(
                """INSERT INTO org_hypotheses (org_id, hypothesis_id, relation, evidence_claim_id, evidence_url, fit_json,
                                               target_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (org_id, hid, relation, evidence_claim_id, evidence_url, _dumps(fit), target_id, utcnow()),
            )
            return True
        if RELATION_RANK.get(relation, 9) < RELATION_RANK.get(row["relation"], 9):
            self._exec(
                """UPDATE org_hypotheses SET relation = ?, evidence_claim_id = ?, evidence_url = ?,
                          target_id = COALESCE(?, target_id) WHERE org_id = ? AND hypothesis_id = ?""",
                (relation, evidence_claim_id, evidence_url, target_id, org_id, hid),
            )
        if fit is not None:
            self._exec("UPDATE org_hypotheses SET fit_json = ? WHERE org_id = ? AND hypothesis_id = ?", (_dumps(fit), org_id, hid))
        return False

    def set_org_hypothesis(self, org_id: int, hid: int, **fields: Any) -> None:
        allowed = {"status", "procurement_active", "procurement_note", "fit_json", "procurement_status",
                   "procurement_subject", "procurement_date", "procurement_url", "procurement_checked_at",
                   "segment_fit_json", "relation", "people_version"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Neznáme polia: {bad}")
        for key in ("fit_json", "segment_fit_json"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = _dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE org_hypotheses SET {sets} WHERE org_id = ? AND hypothesis_id = ?", (*fields.values(), org_id, hid))

    def orgs_for_hypothesis(self, hid: int, include_excluded: bool = False) -> list[sqlite3.Row]:
        """Organizácie hypotézy v poradí priority: dôvod zaradenia, overený web, id."""
        excl = "" if include_excluded else "AND o.excluded = 0 AND oh.status != 'excluded'"
        rank = " ".join(f"WHEN '{k}' THEN {v}" for k, v in RELATION_RANK.items())
        return self._all(
            f"""SELECT o.*, oh.relation, oh.evidence_claim_id, oh.evidence_url, oh.fit_json, oh.status AS oh_status,
                       oh.procurement_active, oh.procurement_note, oh.hypothesis_id, oh.target_id,
                       oh.procurement_status, oh.procurement_subject, oh.procurement_date, oh.procurement_url,
                       oh.segment_fit_json, oh.people_version AS oh_people_version
                FROM org_hypotheses oh JOIN organizations o ON o.id = oh.org_id
                WHERE oh.hypothesis_id = ? {excl}
                ORDER BY CASE oh.relation {rank} ELSE 9 END, o.website_verified DESC, o.id""",
            (hid,),
        )

    def org_hypothesis(self, org_id: int, hid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM org_hypotheses WHERE org_id = ? AND hypothesis_id = ?", (org_id, hid))

    def add_contact_page(self, org_id: int, url: str, content_hash: str | None, status: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO contact_pages (org_id, url, fetched_at, content_hash, status) VALUES (?, ?, ?, ?, ?)",
            (org_id, url, utcnow(), content_hash, status),
        )

    def contact_pages_for(self, org_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM contact_pages WHERE org_id = ? ORDER BY id", (org_id,))

    # ------------------------------------------------------------------- ľudia
    def find_person(self, org_id: int | None, name_key: str) -> sqlite3.Row | None:
        if org_id is None:
            return self._one("SELECT * FROM people WHERE org_id IS NULL AND name_key = ?", (name_key,))
        return self._one("SELECT * FROM people WHERE org_id = ? AND name_key = ?", (org_id, name_key))

    def insert_person(
        self,
        org_id: int | None,
        full_name: str,
        name_key: str,
        source_url: str,
        source_snippet: str,
        hypothesis_id: int,
        **fields: Any,
    ) -> int:
        bad = set(fields) - PERSON_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia osoby: {bad}")
        now = utcnow()
        cols = {
            "org_id": org_id, "full_name": full_name, "name_key": name_key, "source_url": source_url,
            "source_snippet": source_snippet[:300], "collected_for_hypothesis_id": hypothesis_id,
            "created_at": now, "updated_at": now, **fields,
        }
        marks = ", ".join("?" * len(cols))
        cur = self._exec(f"INSERT INTO people ({', '.join(cols)}) VALUES ({marks})", tuple(cols.values()))
        return int(cur.lastrowid)

    def update_person(self, person_id: int, **fields: Any) -> None:
        bad = set(fields) - PERSON_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia osoby: {bad}")
        if "source_snippet" in fields:
            fields["source_snippet"] = (fields["source_snippet"] or "")[:300]
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE people SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), utcnow(), person_id))

    def get_person(self, person_id: int) -> sqlite3.Row | None:
        return self._one(
            """SELECT p.*, o.name AS org_name, o.ico AS org_ico, o.website AS org_website, o.sector_kind AS org_sector_kind
               FROM people p LEFT JOIN organizations o ON o.id = p.org_id WHERE p.id = ?""",
            (person_id,),
        )

    def all_people(self) -> list[sqlite3.Row]:
        return self._all(
            """SELECT p.*, o.name AS org_name FROM people p LEFT JOIN organizations o ON o.id = p.org_id ORDER BY p.id"""
        )

    def people_of_org(self, org_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM people WHERE org_id = ? ORDER BY id", (org_id,))

    def unranked_people(self, hid: int) -> list[sqlite3.Row]:
        """Ľudia zozbieraní pre hypotézu, ktorí ešte nemajú väzbu na ňu (poradie pre hypotézu ešte nebežalo)."""
        return self._all(
            """SELECT p.*, o.name AS org_name FROM people p LEFT JOIN organizations o ON o.id = p.org_id
               WHERE p.collected_for_hypothesis_id = ? AND p.status != 'excluded'
                 AND NOT EXISTS (SELECT 1 FROM person_problem_links l WHERE l.person_id = p.id AND l.hypothesis_id = ?)
               ORDER BY p.id""",
            (hid, hid),
        )

    def delete_person(self, person_id: int) -> None:
        """Zmaže osobu; kontakty, väzby a oslovenia zmaže CASCADE, rozhovor ostane pseudonymizovaný. Obchodné dôkazy
        (M35) a ekonomika u zákazníka (M38) ostanú ako fakt o organizácii, bez väzby na osobu a bez textu „kto a kedy“ –
        pri forget, retencii aj výbere ľudí rovnako."""
        self.scrub_commercial_evidence_of_person(person_id)
        self.scrub_buyer_factors_of_person(person_id)
        self._exec("DELETE FROM people WHERE id = ?", (person_id,))

    def people_by_contact(self, kind: str, value: str) -> list[int]:
        rows = self._all(
            "SELECT DISTINCT person_id FROM contact_points WHERE kind = ? AND value = ? AND person_id IS NOT NULL", (kind, value)
        )
        return [int(r["person_id"]) for r in rows]

    def delete_contact_values(self, kind: str, value: str) -> int:
        return self._exec("DELETE FROM contact_points WHERE kind = ? AND value = ?", (kind, value)).rowcount

    # --------------------------------------------------------------- kontakty
    def add_contact_point(
        self,
        person_id: int | None,
        org_id: int | None,
        kind: str,
        value: str,
        source_url: str,
        source_snippet: str,
        extraction: str,
        is_generic: bool = False,
        checks: dict[str, Any] | None = None,
    ) -> int | None:
        cur = self._exec(
            """INSERT OR IGNORE INTO contact_points (person_id, org_id, kind, value, is_generic, source_url, source_snippet,
                                                     extraction, checks_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (person_id, org_id, kind, value, int(is_generic), source_url, source_snippet[:300], extraction, _dumps(checks), utcnow()),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def contacts_for_person(self, person_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM contact_points WHERE person_id = ? ORDER BY kind, id", (person_id,))

    def org_contacts(self, org_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM contact_points WHERE org_id = ? AND person_id IS NULL ORDER BY kind, id", (org_id,))

    def org_all_contacts(self, org_id: int) -> list[sqlite3.Row]:
        """Funkčné kontakty organizácie aj kontakty jej ľudí."""
        return self._all("SELECT * FROM contact_points WHERE org_id = ? ORDER BY kind, id", (org_id,))

    def hypotheses_with_people(self) -> set[int]:
        """Hypotézy, ku ktorým už sú vybraní ľudia (väzba s prioritou)."""
        return {r["hypothesis_id"] for r in self._all(
            "SELECT DISTINCT hypothesis_id FROM person_problem_links WHERE priority IS NOT NULL")}

    def delete_contact_point(self, contact_id: int) -> None:
        """Ručné odstránenie nesprávneho kontaktu (napr. profil LinkedIn inej osoby s rovnakým menom)."""
        self._exec("DELETE FROM contact_points WHERE id = ?", (contact_id,))

    def get_contact(self, contact_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM contact_points WHERE id = ?", (contact_id,))

    # ----------------------------------------------------------------- väzby
    def upsert_link(
        self,
        person_id: int,
        hid: int,
        link_type: str,
        evidence_claim_id: int | None = None,
        evidence_url: str | None = None,
        rationale: str | None = None,
        verdict: str | None = None,
    ) -> int:
        self._exec(
            """INSERT INTO person_problem_links (person_id, hypothesis_id, link_type, evidence_claim_id, evidence_url,
                                                 rationale, verdict, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (person_id, hypothesis_id, link_type) DO UPDATE SET
                   evidence_claim_id = COALESCE(excluded.evidence_claim_id, evidence_claim_id),
                   evidence_url = COALESCE(excluded.evidence_url, evidence_url),
                   rationale = COALESCE(excluded.rationale, rationale),
                   verdict = COALESCE(excluded.verdict, verdict)""",
            (person_id, hid, link_type, evidence_claim_id, evidence_url, rationale, verdict, utcnow()),
        )
        row = self._one(
            "SELECT id FROM person_problem_links WHERE person_id = ? AND hypothesis_id = ? AND link_type = ?",
            (person_id, hid, link_type),
        )
        return int(row["id"])

    def prune_links(self, person_id: int, hid: int, keep_types: set[str]) -> int:
        """Zmaže väzby osoby k hypotéze, ktoré pri novom poradí už neplatia (napr. „expert“ po zmene pravidiel)."""
        rows = self._all("SELECT id, link_type FROM person_problem_links WHERE person_id = ? AND hypothesis_id = ?", (person_id, hid))
        stale = [r["id"] for r in rows if r["link_type"] not in keep_types]
        for link_id in stale:
            self.conn.execute("DELETE FROM person_problem_links WHERE id = ?", (link_id,))
        self.conn.commit()
        return len(stale)

    def set_link_priority(self, link_id: int, priority: float, components: dict[str, Any]) -> None:
        self._exec(
            "UPDATE person_problem_links SET priority = ?, priority_json = ? WHERE id = ?", (priority, _dumps(components), link_id)
        )

    def set_link_verdict(self, link_id: int, verdict: str, rationale: str | None = None) -> None:
        self._exec(
            "UPDATE person_problem_links SET verdict = ?, rationale = COALESCE(?, rationale) WHERE id = ?",
            (verdict, rationale, link_id),
        )

    def links_for_hypothesis(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT l.*, p.full_name, p.titles, p.role_title, p.role_category, p.gender_hint, p.status AS person_status,
                      p.is_expert, p.org_id, p.source_url AS person_source_url, p.source_snippet AS person_snippet,
                      o.name AS org_name, o.website AS org_website
               FROM person_problem_links l
               JOIN people p ON p.id = l.person_id
               LEFT JOIN organizations o ON o.id = p.org_id
               WHERE l.hypothesis_id = ?
               ORDER BY l.priority IS NULL, l.priority DESC, l.id""",
            (hid,),
        )

    def links_for_person(self, person_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM person_problem_links WHERE person_id = ? ORDER BY priority DESC", (person_id,))

    def unlink_person(self, person_id: int, hid: int) -> None:
        self._exec("DELETE FROM person_problem_links WHERE person_id = ? AND hypothesis_id = ?", (person_id, hid))

    def interviews_of_person(self, person_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM interviews WHERE person_id = ? ORDER BY id", (person_id,))

    def person_email_contacts(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT c.*, p.full_name FROM contact_points c JOIN people p ON p.id = c.person_id WHERE c.kind = 'email'"
        )

    # ------------------------------------------------------------- retencia (R-PRIV-4)
    def uncontacted_people_before(self, cutoff: str) -> list[int]:
        """Kandidáti bez komunikácie a bez odoslaného oslovenia, vytvorení pred cutoff."""
        rows = self._all(
            """SELECT p.id FROM people p
               WHERE p.status IN ('candidate', 'approved', 'excluded') AND p.last_interaction_at IS NULL
                 AND p.created_at < ?
                 AND NOT EXISTS (SELECT 1 FROM outreach x WHERE x.person_id = p.id AND x.sent_at IS NOT NULL)""",
            (cutoff,),
        )
        return [int(r["id"]) for r in rows]

    def declined_people(self) -> list[sqlite3.Row]:
        return self._all("SELECT id, purge_after, updated_at FROM people WHERE status = 'declined'")

    def people_inactive_since(self, cutoff: str) -> list[int]:
        rows = self._all("SELECT id FROM people WHERE last_interaction_at IS NOT NULL AND last_interaction_at < ?", (cutoff,))
        return [int(r["id"]) for r in rows]

    def delete_org_contacts_before(self, cutoff: str) -> int:
        return self._exec("DELETE FROM contact_points WHERE person_id IS NULL AND created_at < ?", (cutoff,)).rowcount

    # ------------------------------------------------------- plán kontaktov (v10)
    def add_contact_target(self, hid: int, kind: str, who: str, roles: list[str], queries: list[str], ask: str,
                           why: str, round_: int = 1) -> int:
        if kind not in CONTACT_TARGET_KINDS:
            raise ValueError(f"Neznámy typ cieľa {kind!r}")
        now = utcnow()
        cur = self._exec(
            """INSERT INTO contact_targets (hypothesis_id, kind, who, roles_json, queries_json, ask, why, round,
                                            created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hid, kind, who, _dumps(roles), _dumps([{"q": q, "done_at": None} for q in queries]), ask, why, round_, now, now),
        )
        return int(cur.lastrowid)

    def contact_targets_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM contact_targets WHERE hypothesis_id = ? ORDER BY id", (hid,))

    def get_contact_target(self, target_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM contact_targets WHERE id = ?", (target_id,))

    def update_contact_target(self, target_id: int, **fields: Any) -> None:
        allowed = {"kind", "who", "roles_json", "queries_json", "ask", "why", "edited_by_human"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Neznáme polia cieľa: {bad}")
        for key in ("roles_json", "queries_json"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = _dumps(fields[key])
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE contact_targets SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), utcnow(), target_id))

    def delete_contact_targets(self, hid: int) -> None:
        """Plán kontaktov znova (GUI): organizácie nájdené cez starý plán ostanú, iba bez odkazu na cieľ."""
        self._exec("UPDATE org_hypotheses SET target_id = NULL WHERE hypothesis_id = ?", (hid,))
        self._exec("DELETE FROM contact_targets WHERE hypothesis_id = ?", (hid,))

    def set_contacts_searched(self, hid: int, summary: dict[str, Any]) -> None:
        self._exec("UPDATE hypotheses SET contacts_searched_at = ?, contact_search_json = ? WHERE id = ?",
                   (utcnow(), _dumps(summary), hid))

    def orgs_waiting_for_people(self, hid: int) -> int:
        """Organizácie hypotézy, z ktorých krok people ešte nezbieral ľudí."""
        row = self._one("""SELECT COUNT(*) AS n FROM org_hypotheses oh JOIN organizations o ON o.id = oh.org_id
                           WHERE oh.hypothesis_id = ? AND oh.status IN ('candidate', 'website_checked') AND o.excluded = 0""",
                        (hid,))
        return int(row["n"]) if row else 0

    def org_hypotheses_by_relation(self, hid: int) -> dict[str, int]:
        rows = self._all("SELECT relation, COUNT(*) AS n FROM org_hypotheses WHERE hypothesis_id = ? GROUP BY relation", (hid,))
        return {r["relation"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------- mapy rolí
    def get_role_map(self, hid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM role_maps WHERE hypothesis_id = ?", (hid,))
        if row is None:
            return None
        return {"roles": json.loads(row["roles_json"]), "edited_by_human": bool(row["edited_by_human"]),
                "low_agreement": bool(row["low_agreement"]), "updated_at": row["updated_at"]}

    def set_role_map(self, hid: int, roles: list[dict[str, Any]], edited_by_human: bool = False, low_agreement: bool = False) -> None:
        self._exec(
            """INSERT INTO role_maps (hypothesis_id, roles_json, edited_by_human, low_agreement, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (hypothesis_id) DO UPDATE SET roles_json = excluded.roles_json,
                   edited_by_human = excluded.edited_by_human, low_agreement = excluded.low_agreement,
                   updated_at = excluded.updated_at""",
            (hid, _dumps(roles), int(edited_by_human), int(low_agreement), utcnow()),
        )

    def get_role_fit(self, hid: int, title_key: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM role_fit_cache WHERE hypothesis_id = ? AND title_key = ?", (hid, title_key))

    def put_role_fit(self, hid: int, title_key: str, score: float, category: str | None, rationale: str | None) -> None:
        self._exec(
            """INSERT OR REPLACE INTO role_fit_cache (hypothesis_id, title_key, score, category, rationale, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hid, title_key, score, category, rationale, utcnow()),
        )

    # -------------------------------------------------------------- oslovenia
    def insert_outreach(
        self,
        person_id: int | None,
        hid: int,
        channel: str,
        variant: str,
        subject: str | None,
        body: str,
        facts: list[dict[str, Any]],
        checks: dict[str, Any],
        status: str,
        contact_point_id: int | None = None,
        *,
        org_id: int | None = None,
        mode: str = "discovery",
        proposal_id: int | None = None,
        package: dict[str, Any] | None = None,
        readiness: str | None = None,
        card_id: int | None = None,
        card_version: int | None = None,
    ) -> int:
        """Oslovenie osoby, alebo organizácie cez funkčný kontakt (person_id None, org_id povinné)."""
        if person_id is None and org_id is None:
            raise ValueError("Oslovenie potrebuje osobu alebo organizáciu.")
        if org_id is None and person_id is not None:
            row = self._one("SELECT org_id FROM people WHERE id = ?", (person_id,))
            org_id = row["org_id"] if row is not None else None
        now = utcnow()
        cur = self._exec(
            """INSERT INTO outreach (person_id, org_id, hypothesis_id, contact_point_id, channel, variant, subject, body,
                                     facts_json, checks_json, status, created_at, updated_at, mode, proposal_id,
                                     package_json, readiness, card_id, card_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (person_id, org_id, hid, contact_point_id, channel, variant, subject, body, _dumps(facts), _dumps(checks),
             status, now, now, mode, proposal_id, _dumps(package) if package is not None else None, readiness,
             card_id, card_version),
        )
        return int(cur.lastrowid)

    def update_outreach(self, outreach_id: int, **fields: Any) -> None:
        bad = set(fields) - OUTREACH_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia oslovenia: {bad}")
        if "status" in fields and fields["status"] not in OUTREACH_STATUSES:
            raise ValueError(f"Neznámy stav oslovenia {fields['status']!r}")
        for key in ("facts_json", "checks_json", "package_json"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = _dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE outreach SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), utcnow(), outreach_id))

    def get_outreach(self, outreach_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM outreach WHERE id = ?", (outreach_id,))

    def delete_unsent_draft(self, outreach_id: int) -> int:
        """Neodoslaný návrh (draft, needs_edit) na nahradenie novším – odoslané a schválené sa nemažú."""
        return self._exec("DELETE FROM outreach WHERE id = ? AND sent_at IS NULL AND approved_at IS NULL "
                          "AND status IN ('draft', 'needs_edit')", (outreach_id,)).rowcount

    def delete_unsent_outreach_for_contacts(self, contact_ids: list[int]) -> int:
        """Neodoslané návrhy (draft, needs_edit, blocked) na kontakty, ktoré sa odstraňujú (zlý zdroj kontaktu)."""
        if not contact_ids:
            return 0
        marks = ",".join("?" * len(contact_ids))
        return self._exec(f"DELETE FROM outreach WHERE contact_point_id IN ({marks}) AND status IN "
                          "('draft', 'needs_edit', 'blocked') AND sent_at IS NULL", tuple(contact_ids)).rowcount

    def outreach_for_person(self, person_id: int, hid: int | None = None) -> list[sqlite3.Row]:
        if hid is None:
            return self._all("SELECT * FROM outreach WHERE person_id = ? ORDER BY id", (person_id,))
        return self._all("SELECT * FROM outreach WHERE person_id = ? AND hypothesis_id = ? ORDER BY id", (person_id, hid))

    def delete_procurement_placeholders(self, org_id: int, hid: int) -> int:
        """Zástupné riadky „návrh sa nevytvoril – obstarávanie“ (bez obsahu) po zmene stavu obstarávania."""
        return self._exec("""DELETE FROM outreach WHERE org_id = ? AND hypothesis_id = ? AND status = 'blocked'
                             AND sent_at IS NULL AND subject IS NULL AND body LIKE 'Návrh sa nevytvoril: organizácia možno%'""",
                          (org_id, hid)).rowcount

    def outreach_for_org(self, org_id: int, hid: int | None = None) -> list[sqlite3.Row]:
        """Oslovenia organizácie cez funkčný kontakt (bez osoby)."""
        if hid is None:
            return self._all("SELECT * FROM outreach WHERE org_id = ? AND person_id IS NULL ORDER BY id", (org_id,))
        return self._all("SELECT * FROM outreach WHERE org_id = ? AND person_id IS NULL AND hypothesis_id = ? ORDER BY id",
                         (org_id, hid))

    def outreach_list(self, hid: int | None = None, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = """SELECT x.*, p.full_name, p.role_title, p.titles, p.gender_hint, o.name AS org_name,
                        c.value AS contact_value, c.kind AS contact_kind
                 FROM outreach x LEFT JOIN people p ON p.id = x.person_id
                 LEFT JOIN organizations o ON o.id = COALESCE(x.org_id, p.org_id)
                 LEFT JOIN contact_points c ON c.id = x.contact_point_id WHERE 1 = 1"""
        params: tuple = ()
        if hid is not None:
            sql += " AND x.hypothesis_id = ?"
            params += (hid,)
        if statuses:
            sql += f" AND x.status IN ({','.join('?' * len(statuses))})"
            params += tuple(statuses)
        return self._all(sql + " ORDER BY x.hypothesis_id, x.person_id, x.channel, x.variant", params)

    def outreach_history(self) -> list[sqlite3.Row]:
        """Všetky oslovenia s osobou, organizáciou a hodnotou kanála – stav komunikácie naprieč hypotézami."""
        return self._all(
            """SELECT x.id, x.person_id, x.hypothesis_id, x.status, x.channel, x.sent_at, x.approved_at, x.follow_up_due,
                      x.created_at, x.mode, COALESCE(x.org_id, p.org_id) AS org_id, lower(c.value) AS contact_value
               FROM outreach x LEFT JOIN people p ON p.id = x.person_id
               LEFT JOIN contact_points c ON c.id = x.contact_point_id ORDER BY x.id""")

    def approvals_on(self, day: str) -> int:
        """Počet schválení na odoslanie v daný deň (YYYY-MM-DD, UTC)."""
        return self._one("SELECT COUNT(*) AS n FROM outreach WHERE substr(approved_at, 1, 10) = ?", (day,))["n"]

    def follow_ups_due(self, day: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM outreach WHERE status = 'sent' AND follow_up_due IS NOT NULL AND follow_up_due <= ? ORDER BY follow_up_due",
            (day,),
        )

    # -------------------------------------------------------------- rozhovory
    def next_pseudonym(self) -> str:
        row = self._one("SELECT COUNT(*) AS n FROM interviews")
        return f"R{row['n'] + 1}"

    def insert_interview(self, **fields: Any) -> int:
        allowed = {
            "person_id", "pseudonym", "hypothesis_id", "interviewee_kind", "role_label", "org_type", "held_at",
            "last_occurrence", "current_workaround", "cost_impact", "decision_and_payer", "tried_before",
            "mentioned_unprompted", "commitment", "notes", "document_id",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Neznáme polia rozhovoru: {bad}")
        fields["created_at"] = utcnow()
        marks = ", ".join("?" * len(fields))
        cur = self._exec(f"INSERT INTO interviews ({', '.join(fields)}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def set_interview_document(self, interview_id: int, document_id: int) -> None:
        self._exec("UPDATE interviews SET document_id = ? WHERE id = ?", (document_id, interview_id))

    def interviews_for(self, hid: int | None = None) -> list[sqlite3.Row]:
        if hid is None:
            return self._all("SELECT * FROM interviews ORDER BY held_at, id")
        return self._all("SELECT * FROM interviews WHERE hypothesis_id = ? ORDER BY held_at, id", (hid,))

    def anonymize_interviews_of(self, person_id: int) -> None:
        """Pri výmaze osoby: rozhovor ostane iba s pseudonymom (osobné polia sa nemenia, citáty ostanú)."""
        self._exec("UPDATE interviews SET person_id = NULL WHERE person_id = ?", (person_id,))

    # ------------------------------------------------------------ rozhodnutia
    def add_decision(self, hid: int, decision: str, rationale: str, metrics: dict[str, Any]) -> int:
        cur = self._exec(
            "INSERT INTO decisions (hypothesis_id, decision, rationale, metrics_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (hid, decision, rationale, _dumps(metrics), utcnow()),
        )
        return int(cur.lastrowid)

    def decisions_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM decisions WHERE hypothesis_id = ? ORDER BY id", (hid,))

    # --------------------------------------------------------------- potlačenie
    def add_suppression(self, hash_: str, kind: str, reason: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO suppression (hash, kind, reason, created_at) VALUES (?, ?, ?, ?)",
            (hash_, kind, reason, utcnow()),
        )

    def suppressed(self, hashes: list[str]) -> bool:
        if not hashes:
            return False
        marks = ",".join("?" * len(hashes))
        return self._one(f"SELECT 1 FROM suppression WHERE hash IN ({marks}) LIMIT 1", tuple(hashes)) is not None

    def suppression_counts(self) -> dict[str, int]:
        return {r["kind"]: r["n"] for r in self._all("SELECT kind, COUNT(*) AS n FROM suppression GROUP BY kind")}

    # ------------------------------------------------------------------ prehľad
    def gate0_counts(self) -> dict[str, int]:
        def n(sql: str) -> int:
            return self._one(sql)["n"]

        return {
            "organizations": n("SELECT COUNT(*) AS n FROM organizations"),
            "people": n("SELECT COUNT(*) AS n FROM people"),
            "contacts": n("SELECT COUNT(*) AS n FROM contact_points"),
            "drafts": n("SELECT COUNT(*) AS n FROM outreach WHERE status IN ('draft', 'needs_edit')"),
            "approved": n("SELECT COUNT(*) AS n FROM outreach WHERE status = 'approved'"),
            "sent": n("SELECT COUNT(*) AS n FROM outreach WHERE sent_at IS NOT NULL"),
            "interviews": n("SELECT COUNT(*) AS n FROM interviews"),
        }
