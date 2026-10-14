"""Register dôkazov: dokumenty, tvrdenia, hypotézy, kritiky, odhady trhu, poznámky, behy.

Každé tvrdenie má odkaz na konkrétny stiahnutý dokument (URL, čas stiahnutia,
hash obsahu), doslovný citát a stav overenia. Staré verzie dokumentov sa
neprepisujú – zmenený obsah sa uloží ako nový riadok, aby sa dalo spätne
zistiť, čo bolo známe v danom čase.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import domain_of, utc_days_ago, utcnow

ACTIVE_STATUSES = ("new", "watch", "interviewing")
# "candidate": nová hypotéza z objavovania, kým nemá aspoň 2 overené tvrdenia z 2 domén (R-DISC-7).
HYPOTHESIS_STATUSES = ("candidate",) + ACTIVE_STATUSES + ("rejected",)
ASSIGNABLE_STATUSES = ("candidate",) + ACTIVE_STATUSES
EXTRACTABLE_KINDS = ("seed", "rss", "links", "search", "discovery")


class EvidenceMixin:
    """Metódy registra dôkazov; spojenie a pomocné funkcie (_one, _all, _exec) dodá trieda DB."""

    conn: sqlite3.Connection

    # ---------------------------------------------------------------- documents
    def url_recently_checked(self, url: str, days: float) -> bool:
        row = self._one(
            "SELECT 1 FROM documents WHERE url = ? AND last_checked_at >= ? LIMIT 1",
            (url, utc_days_ago(days)),
        )
        return row is not None

    def upsert_document(
        self,
        url: str,
        title: str,
        source_kind: str,
        topic: str | None,
        query: str | None,
        content_hash: str,
        text: str,
        source_category: str | None = None,
    ) -> tuple[int, bool]:
        """Uloží dokument; vráti (id, je_nový). Rovnaký obsah na rovnakej URL sa neduplikuje."""
        now = utcnow()
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO documents
               (url, domain, title, source_kind, topic, query, fetched_at, last_checked_at, content_hash, text,
                source_category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, domain_of(url), title, source_kind, topic, query, now, now, content_hash, text, source_category),
        )
        if cur.rowcount:
            self.conn.commit()
            return int(cur.lastrowid), True
        row = self._one("SELECT id FROM documents WHERE url = ? AND content_hash = ?", (url, content_hash))
        self._exec("UPDATE documents SET last_checked_at = ? WHERE id = ?", (now, row["id"]))
        return int(row["id"]), False

    def documents_overview(self, kinds: list[str], query: str | None = None, domain: str | None = None,
                           only_verified: bool = False, limit: int = 500) -> list[sqlite3.Row]:
        """Dokumenty s počtom citátov (GUI Dokumenty), najnovšie najskôr."""
        if not kinds:
            return []
        where = [f"d.source_kind IN ({','.join('?' * len(kinds))})"]
        params: list = list(kinds)
        if query:
            where.append("(d.title LIKE ? OR d.url LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]
        if domain:
            where.append("d.domain LIKE ?")
            params.append(f"%{domain}%")
        having = "HAVING verified > 0" if only_verified else ""
        return self._all(
            f"""SELECT d.id, d.title, d.domain, d.url, d.source_kind, d.source_category, d.source_type, d.fetched_at,
                       d.screened, COUNT(c.id) AS claims, COALESCE(SUM(c.status = 'verified'), 0) AS verified
                FROM documents d LEFT JOIN claims c ON c.document_id = d.id
                WHERE {' AND '.join(where)} GROUP BY d.id {having} ORDER BY d.fetched_at DESC, d.id DESC LIMIT ?""",
            (*params, limit),
        )

    def claims_of_document(self, doc_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM claims WHERE document_id = ? ORDER BY id", (doc_id,))

    def get_document(self, doc_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM documents WHERE id = ?", (doc_id,))

    def documents_to_extract(self, limit: int) -> list[sqlite3.Row]:
        """Dokumenty na extrakciu; z objavovania iba tie, ktoré screen označil ako relevantné (R-DISC-6)."""
        marks = ",".join("?" * len(EXTRACTABLE_KINDS))
        return self._all(
            f"""SELECT * FROM documents WHERE extracted_at IS NULL AND source_kind IN ({marks})
                AND (source_kind != 'discovery' OR screened = 'yes') ORDER BY id LIMIT ?""",
            (*EXTRACTABLE_KINDS, limit),
        )

    def documents_to_screen(self, limit: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM documents WHERE source_kind = 'discovery' AND screened IS NULL AND extracted_at IS NULL "
            "ORDER BY id LIMIT ?",
            (limit,),
        )

    def discovery_outcomes(self) -> list[sqlite3.Row]:
        """Dokumenty z objavovania so známym výsledkom (spracované, bez neoverených tvrdení), ešte nezapočítané v banditovi."""
        return self._all(
            """SELECT d.id, d.source_category,
                      (SELECT COUNT(*) FROM claims c WHERE c.document_id = d.id AND c.status = 'verified') AS verified,
                      (SELECT COUNT(*) FROM claims c WHERE c.document_id = d.id AND c.status = 'pending') AS pending
               FROM documents d
               WHERE d.source_kind = 'discovery' AND d.bandit_counted = 0 AND d.extracted_at IS NOT NULL"""
        )

    def mark_bandit_counted(self, doc_id: int) -> None:
        self._exec("UPDATE documents SET bandit_counted = 1 WHERE id = ?", (doc_id,))

    def set_screened(self, doc_id: int, relevant: bool) -> None:
        if relevant:
            self._exec("UPDATE documents SET screened = 'yes' WHERE id = ?", (doc_id,))
        else:  # nerelevantný dokument sa už neextrahuje
            self._exec("UPDATE documents SET screened = 'no', extracted_at = ? WHERE id = ?", (utcnow(), doc_id))

    def set_extract_progress(self, doc_id: int, next_chunk: int) -> None:
        self._exec("UPDATE documents SET extract_progress = ? WHERE id = ?", (next_chunk, doc_id))

    def mark_extracted(self, doc_id: int) -> None:
        self._exec("UPDATE documents SET extracted_at = ? WHERE id = ?", (utcnow(), doc_id))

    # ------------------------------------------------------------------- claims
    def insert_claim(
        self,
        document_id: int,
        chunk_index: int,
        topic: str | None,
        problem: str,
        affected_party: str | None,
        claim_type: str | None,
        quote: str,
        numbers: list[dict[str, Any]] | None = None,
        origin: str = "extract",
        sector_hint: str | None = None,
        effective_date: str | None = None,
        details: dict[str, Any] | None = None,
        hypothesis_hint: int | None = None,
    ) -> int:
        cur = self._exec(
            """INSERT INTO claims (document_id, chunk_index, topic, problem, affected_party, claim_type,
                                   quote, numbers_json, origin, created_at, sector_hint, effective_date, details_json,
                                   hypothesis_hint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                document_id,
                chunk_index,
                topic,
                problem,
                affected_party,
                claim_type,
                quote,
                json.dumps(numbers or [], ensure_ascii=False),
                origin,
                utcnow(),
                sector_hint,
                effective_date,
                json.dumps(details, ensure_ascii=False) if details else None,
                hypothesis_hint,
            ),
        )
        return int(cur.lastrowid)

    def find_claim(self, document_id: int, quote: str) -> int | None:
        """Rovnaký citát z rovnakého dokumentu ukladáme iba raz (inak by sa dôkaz počítal dvakrát)."""
        row = self._one("SELECT id FROM claims WHERE document_id = ? AND quote = ? LIMIT 1", (document_id, quote))
        return None if row is None else int(row["id"])

    def get_claim(self, claim_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM claims WHERE id = ?", (claim_id,))

    def pending_claims(self, limit: int, ids: list[int] | None = None) -> list[sqlite3.Row]:
        if ids:
            marks = ",".join("?" * len(ids))
            return self._all(
                f"SELECT * FROM claims WHERE status = 'pending' AND id IN ({marks}) ORDER BY id", tuple(ids)
            )
        return self._all("SELECT * FROM claims WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,))

    def set_claim_verification(self, claim_id: int, status: str, score: float | None, note: str) -> None:
        self._exec(
            "UPDATE claims SET status = ?, quote_score = ?, verify_note = ?, verified_at = ? WHERE id = ?",
            (status, score, note, utcnow(), claim_id),
        )

    def set_claim_deterministic(self, claim_id: int, det_status: str, score: float, note: str) -> None:
        """Citát a čísla sú v zdroji; tvrdenie ostáva 'pending', kým ho neposúdi presný model."""
        self._exec(
            "UPDATE claims SET det_status = ?, quote_score = ?, verify_note = ? WHERE id = ?",
            (det_status, score, note, claim_id),
        )

    def set_claim_entailment(self, claim_id: int, verdict: str, low_agreement: bool = False) -> None:
        self._exec(
            "UPDATE claims SET entail_verdict = ?, low_agreement = MAX(low_agreement, ?) WHERE id = ?",
            (verdict, int(low_agreement), claim_id),
        )

    def set_claim_gate(self, claim_id: int, verdict: str, low_agreement: bool = False) -> None:
        self._exec(
            "UPDATE claims SET gate_verdict = ?, low_agreement = MAX(low_agreement, ?) WHERE id = ?",
            (verdict, int(low_agreement), claim_id),
        )

    def claims_for_deterministic(self, limit: int, ids: list[int] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM claims WHERE status = 'pending' AND det_status IS NULL"
        if ids:
            marks = ",".join("?" * len(ids))
            return self._all(sql + f" AND id IN ({marks}) ORDER BY id", tuple(ids))
        return self._all(sql + " ORDER BY id LIMIT ?", (limit,))

    def claims_for_entailment(self, limit: int, ids: list[int] | None = None) -> list[sqlite3.Row]:
        """Tvrdenia po kontrole kódom, ktoré ešte neposúdil model a nečakajú na človeka v kontrole.
        Prednosť majú rozhovory, tvrdenia agentov (test hypotéz, prieskum) a objavovania, potom tvrdenia z tém (R-ACC-6)."""
        sql = """SELECT c.* FROM claims c
                 WHERE c.status = 'pending' AND c.det_status IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM review_queue r WHERE r.task = 'entail' AND r.object_type = 'claim'
                                   AND r.object_id = c.id AND r.status = 'open')"""
        if ids:
            marks = ",".join("?" * len(ids))
            return self._all(sql + f" AND c.id IN ({marks}) ORDER BY c.id", tuple(ids))
        return self._all(
            sql + " ORDER BY CASE WHEN c.origin = 'interview' THEN 0 WHEN c.hypothesis_hint IS NOT NULL OR c.origin = 'scout' "
            "OR EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id AND d.source_kind = 'discovery') "
            "THEN 1 WHEN c.topic IS NOT NULL THEN 2 ELSE 3 END, c.id LIMIT ?",
            (limit,),
        )

    def set_claim_type(self, claim_id: int, claim_type: str) -> None:
        self._exec("UPDATE claims SET claim_type = ? WHERE id = ?", (claim_type, claim_id))

    def claims_of_type(self, claim_type: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM claims WHERE claim_type = ? ORDER BY id", (claim_type,))

    def mark_claim_unrelated(self, claim_id: int) -> None:
        """Overené tvrdenie, ktoré nepatrí k žiadnej hypotéze (napr. protidôkaz k inému problému);
        zaradenie sa už neopakuje."""
        self._exec(
            "UPDATE claims SET details_json = json_set(COALESCE(details_json, '{}'), '$.cluster', 'unrelated') WHERE id = ?",
            (claim_id,),
        )

    def mark_duplicate(self, claim_id: int, original_id: int) -> None:
        self._exec("UPDATE claims SET duplicate_of = ? WHERE id = ?", (original_id, claim_id))

    def assigned_quotes(self) -> list[sqlite3.Row]:
        """Citáty už zaradených originálnych dôkazov (na rozpoznanie prebratého textu)."""
        return self._all(
            """SELECT c.id, c.document_id, c.quote, hc.hypothesis_id FROM claims c
               JOIN hypothesis_claims hc ON hc.claim_id = c.id
               WHERE c.duplicate_of IS NULL AND c.status IN ('verified', 'partial')"""
        )

    def unassigned_evidence(self, limit: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT c.*, d.source_kind AS doc_kind, d.domain AS doc_domain FROM claims c
               JOIN documents d ON d.id = c.document_id
               LEFT JOIN hypothesis_claims hc ON hc.claim_id = c.id
               WHERE hc.claim_id IS NULL AND c.status IN ('verified', 'partial') AND c.origin NOT IN ('market', 'interview')
                 AND COALESCE(json_extract(c.details_json, '$.cluster'), '') != 'unrelated'
               ORDER BY CASE WHEN c.hypothesis_hint IS NOT NULL OR c.origin = 'scout' THEN 0 ELSE 1 END, c.id LIMIT ?""",
            (limit,),
        )

    # --------------------------------------------------------------- hypotheses
    def active_hypotheses(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        return self._all(f"SELECT * FROM hypotheses WHERE status IN ({marks}) ORDER BY id", ACTIVE_STATUSES)

    def assignable_hypotheses(self) -> list[sqlite3.Row]:
        """Aktívne hypotézy aj kandidáti: cluster_assign musí zvažovať aj kandidátov, inak by sa
        nikdy nepovýšili (R-DISC-7)."""
        marks = ",".join("?" * len(ASSIGNABLE_STATUSES))
        return self._all(f"SELECT * FROM hypotheses WHERE status IN ({marks}) ORDER BY id", ASSIGNABLE_STATUSES)

    def candidate_hypotheses(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE status = 'candidate' ORDER BY id")

    def all_hypotheses(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses ORDER BY id")

    def get_hypothesis(self, hid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM hypotheses WHERE id = ?", (hid,))

    def create_hypothesis(
        self,
        title: str,
        description: str,
        segment: str,
        status: str = "new",
        project: str | None = None,
        origin: str | None = None,
        topic: str | None = None,
        sector: str | None = None,
    ) -> int:
        now = utcnow()
        origin = origin or ("manual" if project else "topic")
        cur = self._exec(
            """INSERT INTO hypotheses (title, description, segment, status, project, created_at, updated_at,
                                       origin, topic, sector, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, description, segment, status, project, now, now, origin, topic, sector, now),
        )
        return int(cur.lastrowid)

    def set_hypothesis_fields(self, hid: int, **fields: Any) -> None:
        """Ručne upraviteľné polia hypotézy (GUI): founder_fit, payer_confirmed, gate0_status, sector."""
        allowed = {"founder_fit", "payer_confirmed", "gate0_status", "sector", "segment", "title", "description",
                   "segment_filter_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Pole {', '.join(sorted(bad))} sa nedá meniť.")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE hypotheses SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), utcnow(), hid))

    def set_people_selection(self, hid: int, enabled: bool, manual: bool = True) -> None:
        """Zapne alebo vypne hľadanie ľudí pre hypotézu. Ručná voľba má prednosť pred automatickým výberom."""
        if manual:
            self._exec(
                "UPDATE hypotheses SET people_enabled = ?, people_manual = ?, updated_at = ? WHERE id = ?",
                (int(enabled), int(enabled), utcnow(), hid),
            )
        else:
            self._exec(
                "UPDATE hypotheses SET people_enabled = ?, updated_at = ? WHERE id = ? AND people_manual IS NULL",
                (int(enabled), utcnow(), hid),
            )

    def people_enabled_hypotheses(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM hypotheses WHERE people_enabled = 1 AND status NOT IN ('rejected', 'candidate') ORDER BY id"
        )

    def project_hypothesis(self, project: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM hypotheses WHERE project = ?", (project,))

    def upsert_project_hypothesis(self, project: str, title: str, description: str, segment: str) -> tuple[int, bool]:
        """Hypotéza rozpracovaného projektu: vytvorí ju (stav 'watch') alebo zosúladí text s config.yaml.
        Stav, ktorý si nastavil ručne, sa nemení."""
        row = self.project_hypothesis(project)
        if row is None:
            return self.create_hypothesis(title, description, segment, status="watch", project=project), True
        if (row["title"], row["description"], row["segment"]) != (title, description, segment):
            self._exec(
                "UPDATE hypotheses SET title = ?, description = ?, segment = ?, updated_at = ? WHERE id = ?",
                (title, description, segment, utcnow(), row["id"]),
            )
        return int(row["id"]), False

    def hypothesis_of_claim(self, claim_id: int) -> int | None:
        row = self._one("SELECT hypothesis_id FROM hypothesis_claims WHERE claim_id = ?", (claim_id,))
        return None if row is None else int(row["hypothesis_id"])

    def move_claims(self, claim_ids: list[int], from_hid: int, to_hid: int) -> int:
        """Rozdelenie hypotézy: citáty podproblému prejdú do podhypotézy (citát patrí vždy jednej hypotéze)."""
        n = 0
        for cid in claim_ids:
            cur = self.conn.execute("UPDATE hypothesis_claims SET hypothesis_id = ? WHERE claim_id = ? AND hypothesis_id = ?",
                                    (to_hid, cid, from_hid))
            n += cur.rowcount
        self.conn.commit()
        return n

    def set_parent(self, hid: int, parent_id: int) -> None:
        self._exec("UPDATE hypotheses SET parent_id = ?, updated_at = ? WHERE id = ?", (parent_id, utcnow(), hid))

    def mark_split(self, hid: int) -> None:
        self._exec("UPDATE hypotheses SET split_at = ?, updated_at = ? WHERE id = ?", (utcnow(), utcnow(), hid))

    def children_of(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE parent_id = ? ORDER BY id", (hid,))

    def assign_claim(self, hid: int, claim_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO hypothesis_claims (hypothesis_id, claim_id) VALUES (?, ?)", (hid, claim_id)
        )
        self._exec("UPDATE hypotheses SET updated_at = ? WHERE id = ?", (utcnow(), hid))

    def set_hypothesis_status(self, hid: int, status: str) -> None:
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"Neznámy stav {status!r}, povolené: {', '.join(HYPOTHESIS_STATUSES)}")
        self._exec("UPDATE hypotheses SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), hid))

    def update_hypothesis_text(self, hid: int, title: str, description: str, segment: str) -> None:
        self._exec(
            "UPDATE hypotheses SET title = ?, description = ?, segment = ?, updated_at = ? WHERE id = ?",
            (title, description, segment, utcnow(), hid),
        )

    def merge_checked(self, a: int, b: int) -> bool:
        a, b = sorted((a, b))
        return self._one("SELECT 1 FROM merge_checks WHERE a = ? AND b = ?", (a, b)) is not None

    def record_merge_check(self, a: int, b: int, same: bool) -> None:
        a, b = sorted((a, b))
        self._exec(
            "INSERT OR REPLACE INTO merge_checks (a, b, same, checked_at) VALUES (?, ?, ?, ?)", (a, b, int(same), utcnow())
        )

    def merge_hypotheses(self, source_id: int, target_id: int, note: str | None = None, source: str = "human") -> int:
        """Presunie dôkazy a poznámky z jednej hypotézy do druhej; zdrojovú označí ako zamietnutú."""
        if source_id == target_id:
            raise ValueError("Hypotézu nemožno zlúčiť samu so sebou.")
        for hid in (source_id, target_id):
            if self.get_hypothesis(hid) is None:
                raise ValueError(f"Hypotéza {hid} neexistuje.")
        claim_ids = [r["claim_id"] for r in self._all("SELECT claim_id FROM hypothesis_claims WHERE hypothesis_id = ?", (source_id,))]
        moved = self.conn.execute(
            "UPDATE hypothesis_claims SET hypothesis_id = ? WHERE hypothesis_id = ?", (target_id, source_id)
        ).rowcount
        if claim_ids:  # aby sa dalo spätne zistiť, ktoré dôkazy prišli zo zlúčenej hypotézy
            note = (note or f"Zlúčené z H{source_id}.") + " Presunuté dôkazy: " + ", ".join(f"C{c}" for c in claim_ids) + "."
        self.conn.execute("UPDATE notes SET hypothesis_id = ? WHERE hypothesis_id = ?", (target_id, source_id))
        now = utcnow()
        self.conn.execute("UPDATE hypotheses SET status = 'rejected', updated_at = ? WHERE id = ?", (now, source_id))
        self.conn.execute("UPDATE hypotheses SET updated_at = ? WHERE id = ?", (now, target_id))
        self.conn.execute(
            "INSERT INTO notes (hypothesis_id, created_at, source, text) VALUES (?, ?, ?, ?)",
            (target_id, now, source, note or f"Zlúčené z H{source_id} ({moved} dôkazov)."),
        )
        self.conn.commit()
        return moved

    def evidence_for(self, hid: int, include_duplicates: bool = False) -> list[sqlite3.Row]:
        """Dôkazy hypotézy (overené a čiastočne overené), overené ako prvé.
        Prebratý text (duplicate_of) sa predvolene nezapočítava – nejde o nezávislý zdroj."""
        dup = "" if include_duplicates else "AND c.duplicate_of IS NULL"
        return self._all(
            f"""SELECT c.*, d.url, d.domain, d.title AS doc_title, d.fetched_at, d.source_type
               FROM hypothesis_claims hc
               JOIN claims c ON c.id = hc.claim_id
               JOIN documents d ON d.id = c.document_id
               WHERE hc.hypothesis_id = ? AND c.status IN ('verified', 'partial') {dup}
               ORDER BY CASE c.status WHEN 'verified' THEN 0 ELSE 1 END, c.id""",
            (hid,),
        )

    def duplicates_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all(
            """SELECT c.id, c.duplicate_of, d.url, d.domain FROM hypothesis_claims hc
               JOIN claims c ON c.id = hc.claim_id JOIN documents d ON d.id = c.document_id
               WHERE hc.hypothesis_id = ? AND c.duplicate_of IS NOT NULL ORDER BY c.id""",
            (hid,),
        )

    def register_rows(self) -> list[sqlite3.Row]:
        """Celý register dôkazov na export do CSV (bez zamietnutých a neoverených tvrdení)."""
        return self._all(
            """SELECT h.id AS hypothesis_id, h.title AS hypothesis, h.segment, h.status AS hypothesis_status,
                      h.project, c.id AS claim_id, c.status, c.claim_type, c.problem, c.affected_party, c.quote,
                      c.duplicate_of, c.origin, d.url, d.domain, d.fetched_at, c.verified_at, c.verify_note
               FROM hypothesis_claims hc
               JOIN hypotheses h ON h.id = hc.hypothesis_id
               JOIN claims c ON c.id = hc.claim_id
               JOIN documents d ON d.id = c.document_id
               WHERE c.status IN ('verified', 'partial')
               ORDER BY h.id, c.id"""
        )

    # ------------------------------------------------------ critiques & market
    def add_critique(self, hid: int, claim_count: int, content: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO critiques (hypothesis_id, created_at, claim_count, content_json) VALUES (?, ?, ?, ?)",
            (hid, utcnow(), claim_count, json.dumps(content, ensure_ascii=False)),
        )

    def latest_critique(self, hid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM critiques WHERE hypothesis_id = ? ORDER BY id DESC LIMIT 1", (hid,))
        if row is None:
            return None
        return {"created_at": row["created_at"], "claim_count": row["claim_count"], "content": json.loads(row["content_json"])}

    def add_market_estimate(
        self,
        hid: int,
        complete: bool,
        unit: str,
        low: float | None,
        base: float | None,
        high: float | None,
        detail: dict[str, Any],
    ) -> None:
        self._exec(
            """INSERT INTO market_estimates (hypothesis_id, created_at, complete, unit, low, base, high, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (hid, utcnow(), int(complete), unit, low, base, high, json.dumps(detail, ensure_ascii=False)),
        )

    def latest_market_estimate(self, hid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM market_estimates WHERE hypothesis_id = ? ORDER BY id DESC LIMIT 1", (hid,))
        if row is None:
            return None
        result = dict(row)
        result["detail"] = json.loads(row["detail_json"])
        return result

    # -------------------------------------------------------------------- notes
    def add_note(self, hid: int | None, text: str, source: str = "human") -> None:
        self._exec(
            "INSERT INTO notes (hypothesis_id, created_at, source, text) VALUES (?, ?, ?, ?)",
            (hid, utcnow(), source, text),
        )

    def notes_for(self, hid: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM notes WHERE hypothesis_id = ? ORDER BY id", (hid,))

    # --------------------------------------------------------- typ vydavateľa (v5)
    def untyped_domains(self, limit: int) -> list[sqlite3.Row]:
        """Domény dokumentov bez typu vydavateľa a bez záznamu v domain_types, s jedným vzorovým dokumentom."""
        return self._all(
            """SELECT d.domain, MIN(d.id) AS doc_id FROM documents d
               WHERE d.source_type IS NULL AND d.domain != ''
                 AND NOT EXISTS (SELECT 1 FROM domain_types t WHERE t.domain = d.domain)
               GROUP BY d.domain ORDER BY COUNT(*) DESC LIMIT ?""",
            (limit,),
        )

    def set_domain_type(self, domain: str, source_type: str, method: str, reason: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO domain_types (domain, source_type, method, reason, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (domain) DO UPDATE SET source_type = excluded.source_type, method = excluded.method, "
            "reason = excluded.reason, updated_at = excluded.updated_at",
            (domain, source_type, method, reason, utcnow()),
        )
        self._exec("UPDATE documents SET source_type = ? WHERE domain = ?", (source_type, domain))

    def apply_domain_types(self) -> int:
        """Nové dokumenty z už známych domén dostanú typ bez modelu."""
        return self._exec(
            """UPDATE documents SET source_type = (SELECT t.source_type FROM domain_types t WHERE t.domain = documents.domain)
               WHERE source_type IS NULL AND domain IN (SELECT domain FROM domain_types)"""
        ).rowcount

    def domain_types(self) -> list[sqlite3.Row]:
        return self._all(
            """SELECT t.*, (SELECT COUNT(*) FROM documents d WHERE d.domain = t.domain) AS documents
               FROM domain_types t ORDER BY documents DESC, t.domain"""
        )

    # --------------------------------------------------------- testovanie hypotéz (v3)
    def validation_queue(self, limit: int, retest_before: str | None = None,
                         ids: list[int] | None = None) -> list[sqlite3.Row]:
        """Hypotézy na otestovanie agentom. Najprv nikdy netestované, potom tie s testom starším než
        retest_before. Poradie: vybrané na Bránu 0, rozpracované, kandidáti, nové (viac overených
        tvrdení skôr), zamietnuté nakoniec – otestujú sa všetky, poradie len určuje, ktoré skôr."""
        if ids:
            marks = ",".join("?" * len(ids))
            return self._all(f"SELECT * FROM hypotheses WHERE id IN ({marks}) ORDER BY id", tuple(ids))
        return self._all(
            """SELECT h.*, (SELECT COUNT(*) FROM hypothesis_claims hc JOIN claims c ON c.id = hc.claim_id
                            WHERE hc.hypothesis_id = h.id AND c.status = 'verified') AS n_verified
               FROM hypotheses h
               WHERE h.validation_state IS NULL OR (h.validation_state = 'tested' AND h.validated_at < ?)
               ORDER BY CASE WHEN h.validation_state IS NULL THEN 0 ELSE 1 END, h.validation_runs, h.people_enabled DESC,
                        CASE h.status WHEN 'interviewing' THEN 0 WHEN 'watch' THEN 1 WHEN 'candidate' THEN 2
                                      WHEN 'new' THEN 3 ELSE 4 END,
                        n_verified DESC, h.validated_at, h.id
               LIMIT ?""",
            (retest_before or "", limit),
        )

    def set_validation_searched(self, hid: int, info: dict[str, Any] | None = None) -> None:
        """Agent dohľadal zdroje; verdikt sa určí, keď kód a presný model posúdia nájdené tvrdenia.
        info (výsledok agenta) sa do verdiktu prenesie cez validation_json."""
        self._exec(
            "UPDATE hypotheses SET validation_state = 'searched', validation_started_at = ?, validation_json = ?, "
            "validation_runs = validation_runs + 1 WHERE id = ?",
            (utcnow(), json.dumps(info or {}, ensure_ascii=False), hid),
        )

    def set_validation_result(self, hid: int, result: dict[str, Any]) -> None:
        self._exec(
            "UPDATE hypotheses SET validation_state = 'tested', validated_at = ?, validation_json = ? WHERE id = ?",
            (utcnow(), json.dumps(result, ensure_ascii=False), hid),
        )

    def count_validation_attempt(self, hid: int) -> None:
        """Neúspešný pokus o test (agent nič neprečítal): hypotéza ostane neotestovaná, ale vo fronte
        ide za ostatné, aby jedna problematická hypotéza neblokovala frontu."""
        self._exec("UPDATE hypotheses SET validation_runs = validation_runs + 1 WHERE id = ?", (hid,))

    def hypotheses_created_since(self, since: str) -> list[sqlite3.Row]:
        return self._all("SELECT id, title, status, origin FROM hypotheses WHERE created_at >= ? ORDER BY id", (since,))

    def hypotheses_in_validation(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE validation_state = 'searched' ORDER BY validation_started_at, id")

    def validation_claim_ids(self) -> list[int]:
        """Tvrdenia, ktoré agent našiel pri hypotézach čakajúcich na verdikt testu (stav searched)."""
        return [r["id"] for r in self._all(
            """SELECT c.id FROM claims c JOIN hypotheses h ON h.id = c.hypothesis_hint
               WHERE h.validation_state = 'searched' ORDER BY c.id"""
        )]

    def hinted_claims(self, hid: int) -> list[sqlite3.Row]:
        """Tvrdenia, ktoré agent našiel pri testovaní hypotézy (v akomkoľvek stave a zaradení)."""
        return self._all(
            """SELECT c.*, (SELECT GROUP_CONCAT(hc.hypothesis_id) FROM hypothesis_claims hc
                            WHERE hc.claim_id = c.id) AS assigned_to
               FROM claims c WHERE c.hypothesis_hint = ? ORDER BY c.id""",
            (hid,),
        )

    def validation_blocking(self, hid: int) -> int:
        """Koľko tvrdení z testu ešte čaká: na posúdenie (mimo otvorenej kontroly) alebo na zaradenie."""
        return self._one(
            """SELECT COUNT(*) AS n FROM claims c
               WHERE c.hypothesis_hint = ? AND (
                 (c.status = 'pending' AND NOT EXISTS (SELECT 1 FROM review_queue r WHERE r.object_type = 'claim'
                                                       AND r.object_id = c.id AND r.status = 'open'))
                 OR (c.status IN ('verified', 'partial') AND COALESCE(json_extract(c.details_json, '$.cluster'), '') != 'unrelated'
                     AND NOT EXISTS (SELECT 1 FROM hypothesis_claims hc WHERE hc.claim_id = c.id)))""",
            (hid,),
        )["n"]

    def validation_counts(self) -> dict[str, int]:
        rows = self._all("SELECT COALESCE(validation_state, 'none') AS s, COUNT(*) AS n FROM hypotheses GROUP BY 1")
        out = {"none": 0, "searched": 0, "tested": 0}
        out.update({r["s"]: r["n"] for r in rows})
        out["total"] = sum(r["n"] for r in rows)
        return out

    # ------------------------------------------------------------- dopyty pre správu a GUI
    def claim_types(self) -> list[str]:
        return [r[0] for r in self._all("SELECT DISTINCT claim_type FROM claims WHERE claim_type IS NOT NULL ORDER BY 1")]

    def search_claims(self, statuses: list[str], origins: list[str], types: list[str] | None = None,
                      domain: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        """Tvrdenia pre GUI (Dôkazy) s filtrami; najnovšie prvé."""
        statuses, origins = list(statuses) or ["-"], list(origins) or ["-"]
        sql = f"""SELECT c.id, c.status, c.claim_type, c.origin, c.problem, c.affected_party, c.quote, c.effective_date,
                         d.domain, d.url, d.fetched_at, c.verify_note, c.document_id, d.source_type
                  FROM claims c JOIN documents d ON d.id = c.document_id
                  WHERE c.status IN ({",".join("?" * len(statuses))}) AND c.origin IN ({",".join("?" * len(origins))})"""
        params: list = statuses + origins
        if types:
            sql += f" AND c.claim_type IN ({','.join('?' * len(types))})"
            params += list(types)
        if domain:
            sql += " AND d.domain LIKE ?"
            params.append(f"%{domain}%")
        return self._all(sql + " ORDER BY c.id DESC LIMIT ?", tuple(params) + (limit,))

    def pending_entailment_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM claims WHERE status = 'pending' AND det_status IS NOT NULL")["n"]

    def discovery_hypotheses(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE origin = 'discovery' AND status != 'rejected' ORDER BY id")

    def discovery_claims_waiting(self) -> int:
        """Tvrdenia z objavovania, ktoré ešte nie sú zaradené (čakajú na overenie alebo zaradenie)."""
        return self._one(
            """SELECT COUNT(*) AS n FROM claims c JOIN documents d ON d.id = c.document_id
               LEFT JOIN hypothesis_claims hc ON hc.claim_id = c.id
               WHERE d.source_kind = 'discovery' AND hc.claim_id IS NULL AND c.status != 'rejected'"""
        )["n"]

    def upcoming_regulations(self, date_from: str, date_to: str) -> list[sqlite3.Row]:
        return self._all(
            """SELECT c.*, d.url, d.domain FROM claims c JOIN documents d ON d.id = c.document_id
               WHERE c.claim_type = 'regulation' AND c.status IN ('verified', 'partial') AND c.effective_date IS NOT NULL
                 AND c.effective_date >= ? AND c.effective_date <= ? ORDER BY c.effective_date""",
            (date_from, date_to),
        )

    def verified_extracted_claims_with_text(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT c.*, d.text AS doc_text FROM claims c JOIN documents d ON d.id = c.document_id "
            "WHERE c.status = 'verified' AND c.origin = 'extract' ORDER BY c.id"
        )

    # --------------------------------------------------------------- embeddings
    def get_embedding(self, kind: str, ref_id: int, model: str, text_hash: str) -> bytes | None:
        row = self._one(
            "SELECT vector FROM embeddings WHERE kind = ? AND ref_id = ? AND model = ? AND text_hash = ?",
            (kind, ref_id, model, text_hash),
        )
        return None if row is None else bytes(row["vector"])

    def embeddings_of_kind(self, kind: str, model: str) -> list[sqlite3.Row]:
        """Všetky vektory jedného druhu naraz (index katalógu: desaťtisíce riadkov – po jednom by to trvalo)."""
        return self._all("SELECT ref_id, text_hash, vector FROM embeddings WHERE kind = ? AND model = ?", (kind, model))

    def put_embeddings(self, rows: list[tuple[str, int, str, str, bytes]]) -> None:
        """Dávka vektorov v jednej transakcii (zatriedenie desiatok tisíc textov – zápis po jednom trval minúty)."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (kind, ref_id, model, text_hash, vector) VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def put_embedding(self, kind: str, ref_id: int, model: str, text_hash: str, vector: bytes) -> None:
        self._exec(
            "INSERT OR REPLACE INTO embeddings (kind, ref_id, model, text_hash, vector) VALUES (?, ?, ?, ?, ?)",
            (kind, ref_id, model, text_hash, vector),
        )

    # --------------------------------------------------------------------- runs
    def last_finished(self, stage: str) -> str | None:
        """Kedy naposledy úspešne skončil daný krok (napr. predošlá správa)."""
        row = self._one(
            "SELECT finished_at FROM runs WHERE stage = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (stage,),
        )
        return None if row is None else row["finished_at"]

    def recent_runs(self, limit: int = 7) -> list[sqlite3.Row]:
        return list(reversed(self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))))

    def unassigned_count(self) -> int:
        return self._one(
            """SELECT COUNT(*) AS n FROM claims c LEFT JOIN hypothesis_claims hc ON hc.claim_id = c.id
               WHERE hc.claim_id IS NULL AND c.status IN ('verified', 'partial') AND c.origin != 'market'
                 AND COALESCE(json_extract(c.details_json, '$.cluster'), '') != 'unrelated'"""
        )["n"]

    def start_run(self, stage: str) -> int:
        cur = self._exec("INSERT INTO runs (stage, started_at) VALUES (?, ?)", (stage, utcnow()))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self._exec(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE id = ?",
            (utcnow(), json.dumps(stats, ensure_ascii=False), run_id),
        )

    def counts(self) -> dict[str, int]:
        result = {"documents": self._one("SELECT COUNT(*) AS n FROM documents")["n"]}
        for status in ("verified", "partial", "rejected", "pending"):
            result[status] = self._one(
                "SELECT COUNT(*) AS n FROM claims WHERE status = ? AND origin != 'market'", (status,)
            )["n"]
        result["duplicates"] = self._one("SELECT COUNT(*) AS n FROM claims WHERE duplicate_of IS NOT NULL")["n"]
        result["active_hypotheses"] = len(self.active_hypotheses())
        result["candidate_hypotheses"] = self._one("SELECT COUNT(*) AS n FROM hypotheses WHERE status = 'candidate'")["n"]
        return result
