"""SQL pre doménový výskum (dodatok 16. 10. 2026): stav podkladov domény, podklady, tvrdenia so stavom a rozsahom,
otázky s blokovanou akciou a históriou odpovedí, mapovanie rolí na povolania a lokálny ESCO index."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _history(old: str | None, entry: dict[str, Any]) -> str:
    items = json.loads(old or "[]")
    items.append({"kedy": utcnow(), **entry})
    return _dumps(items[-50:])


RESEARCH_FIELDS = {"title", "priority", "priority_source", "status", "input_hash", "attempts", "coverage_json",
                   "stop_reason", "next_step", "stats_json", "requested_at", "started_at", "finished_at"}
CLAIM_FIELDS = {"text", "evidence_json", "status", "scope", "applicability", "organization_id", "reasoning_note",
                "brief_id", "active", "part"}
QUESTION_FIELDS = {"question", "norm", "kind", "part", "aspect", "unknown_claim_id", "target_role", "blocks_action", "priority",
                   "status", "answer", "answer_source", "answered_at", "outcome_id", "brief_id"}


class DomainMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ stav výskumu domény
    def research_get(self, key: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_research WHERE domain_key = ?", (key,))

    def research_list(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM domain_research ORDER BY priority DESC, domain_key")

    def research_upsert(self, key: str, title: str, **fields: Any) -> None:
        bad = set(fields) - RESEARCH_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia stavu domény: {bad}")
        for k in ("coverage_json", "stats_json"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = _dumps(fields[k])
        if self.research_get(key) is None:
            cols = ["domain_key", "title", "updated_at"] + list(fields)
            self._exec(f"INSERT INTO domain_research ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [key, title, utcnow(), *fields.values()])
            return
        sets = ", ".join(f"{k} = ?" for k in ["title", "updated_at", *fields])
        self._exec(f"UPDATE domain_research SET {sets} WHERE domain_key = ?", [title, utcnow(), *fields.values(), key])

    # ------------------------------------------------------------------ podklady domény
    def add_domain_evidence(self, key: str, source_kind: str, excerpt: str, content_hash: str, scope: str,
                            **fields: Any) -> int:
        """Podklad domény (rovnaký obsah sa v doméne uloží raz). Vráti id nového alebo existujúceho."""
        row = self._one("SELECT id FROM domain_evidence WHERE domain_key = ? AND content_hash = ?", (key, content_hash))
        if row is not None:
            return int(row["id"])
        data = {"domain_key": key, "source_kind": source_kind, "excerpt": excerpt, "content_hash": content_hash,
                "scope": scope, "created_at": utcnow(), **fields}
        data["retrieved_at"] = data.get("retrieved_at") or utcnow()
        cur = self._exec(f"INSERT INTO domain_evidence ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                         list(data.values()))
        return int(cur.lastrowid)

    def domain_evidence(self, key: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM domain_evidence WHERE domain_key = ? ORDER BY id", (key,))

    def get_domain_evidence(self, evidence_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_evidence WHERE id = ?", (evidence_id,))

    # ------------------------------------------------------------------ tvrdenia domény
    def add_domain_claim(self, key: str, part: str, text: str, status: str, evidence: dict[str, list[int]],
                         **fields: Any) -> int:
        bad = set(fields) - CLAIM_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia tvrdenia: {bad}")
        cols = ["domain_key", "part", "text", "status", "evidence_json", "history_json", "created_at", "updated_at"] + list(fields)
        vals = [key, part, text, status, _dumps(evidence), _history(None, {"stav": status, "dôvod": "vznik"}), utcnow(),
                utcnow(), *fields.values()]
        cur = self._exec(f"INSERT INTO domain_claims ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def domain_claims(self, key: str, active: bool | None = True) -> list[sqlite3.Row]:
        if active is None:
            return self._all("SELECT * FROM domain_claims WHERE domain_key = ? ORDER BY id", (key,))
        return self._all("SELECT * FROM domain_claims WHERE domain_key = ? AND active = ? ORDER BY id", (key, int(active)))

    def get_domain_claim(self, claim_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_claims WHERE id = ?", (claim_id,))

    def update_domain_claim(self, claim_id: int, reason: str = "", **fields: Any) -> None:
        bad = set(fields) - CLAIM_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia tvrdenia: {bad}")
        row = self.get_domain_claim(claim_id)
        if row is None:
            return
        if "evidence_json" in fields and not isinstance(fields["evidence_json"], str):
            fields["evidence_json"] = _dumps(fields["evidence_json"])
        entry = {("stav" if k == "status" else k): v for k, v in fields.items() if k in ("status", "text", "active")}
        fields["history_json"] = _history(row["history_json"], {**entry, "dôvod": reason}) if entry or reason \
            else row["history_json"]
        sets = ", ".join(f"{k} = ?" for k in [*fields, "updated_at"])
        self._exec(f"UPDATE domain_claims SET {sets} WHERE id = ?", [*fields.values(), utcnow(), claim_id])

    def deactivate_brief_claims(self, key: str) -> None:
        """Nová verzia prehľadu: tvrdenia z predošlej verzie (zdroj, hypotéza) sa nahradia; výpovede kontaktov
        a pozorovania v dátach ostávajú."""
        for row in self._all("SELECT id FROM domain_claims WHERE domain_key = ? AND active = 1 AND status IN "
                             "('source_fact', 'hypothesis') AND brief_id IS NOT NULL", (key,)):
            self.update_domain_claim(row["id"], reason="nová verzia prehľadu", active=0)

    # ------------------------------------------------------------------ otázky domény
    def add_domain_question(self, key: str, question: str, norm: str, kind: str, **fields: Any) -> int:
        bad = set(fields) - QUESTION_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia otázky: {bad}")
        cols = ["domain_key", "question", "norm", "kind", "history_json", "created_at", "updated_at"] + list(fields)
        vals = [key, question, norm, kind, _history(None, {"stav": "open", "dôvod": "vznik"}), utcnow(), utcnow(),
                *fields.values()]
        cur = self._exec(f"INSERT INTO domain_questions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def domain_questions(self, key: str, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return self._all("SELECT * FROM domain_questions WHERE domain_key = ? ORDER BY priority, id", (key,))
        return self._all("SELECT * FROM domain_questions WHERE domain_key = ? AND status = ? ORDER BY priority, id",
                         (key, status))

    def get_domain_question(self, question_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM domain_questions WHERE id = ?", (question_id,))

    def update_domain_question(self, question_id: int, reason: str = "", **fields: Any) -> None:
        bad = set(fields) - QUESTION_FIELDS
        if bad:
            raise ValueError(f"Neznáme polia otázky: {bad}")
        row = self.get_domain_question(question_id)
        if row is None:
            return
        entry = {("stav" if k == "status" else k): v for k, v in fields.items()
                 if k in ("status", "answer", "answer_source", "question")}
        fields["history_json"] = _history(row["history_json"], {**entry, "dôvod": reason}) if entry or reason \
            else row["history_json"]
        sets = ", ".join(f"{k} = ?" for k in [*fields, "updated_at"])
        self._exec(f"UPDATE domain_questions SET {sets} WHERE id = ?", [*fields.values(), utcnow(), question_id])

    # ------------------------------------------------------------------ rola → povolanie
    def upsert_role_mapping(self, key: str, role_label: str, candidates: list[dict[str, Any]], **fields: Any) -> int:
        row = self._one("SELECT id FROM role_mappings WHERE domain_key = ? AND role_label = ?", (key, role_label))
        allowed = {"chosen_uri", "chosen_by", "mapping_basis", "source_version"}
        if set(fields) - allowed:
            raise ValueError(f"Neznáme polia mapovania: {set(fields) - allowed}")
        if row is None:
            cols = ["domain_key", "role_label", "candidates_json", "created_at", "updated_at"] + list(fields)
            cur = self._exec(f"INSERT INTO role_mappings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [key, role_label, _dumps(candidates), utcnow(), utcnow(), *fields.values()])
            return int(cur.lastrowid)
        sets = ", ".join(f"{k} = ?" for k in ["candidates_json", "updated_at", *fields])
        self._exec(f"UPDATE role_mappings SET {sets} WHERE id = ?", [_dumps(candidates), utcnow(), *fields.values(), row["id"]])
        return int(row["id"])

    def role_mappings(self, key: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM role_mappings WHERE domain_key = ? ORDER BY role_label", (key,))

    def choose_role_mapping(self, mapping_id: int, uri: str | None, by: str = "človek") -> None:
        self._exec("UPDATE role_mappings SET chosen_uri = ?, chosen_by = ?, updated_at = ? WHERE id = ?",
                   (uri, by if uri else None, utcnow(), mapping_id))

    # ------------------------------------------------------------------ ESCO
    def esco_has_fts(self) -> bool:
        return self._one("SELECT name FROM sqlite_master WHERE name = 'esco_fts'") is not None

    def esco_replace(self, version: str, lang: str, concepts: list[dict[str, Any]], relations: list[tuple[str, str, str]],
                     meta: dict[str, Any]) -> dict[str, int]:
        """Nahradí ESCO koncepty daného jazyka (import je idempotentný: rovnaký balík = rovnaký stav)."""
        conn = self.conn
        conn.execute("DELETE FROM esco_concepts WHERE lang = ?", (lang,))
        conn.executemany(
            "INSERT OR REPLACE INTO esco_concepts (uri, lang, kind, pref_label, alt_labels, description, isco_group, code, "
            "norm, version) VALUES (:uri, :lang, :kind, :pref_label, :alt_labels, :description, :isco_group, :code, :norm, "
            ":version)", concepts)
        if relations:
            conn.execute("DELETE FROM esco_relations")
            conn.executemany("INSERT OR REPLACE INTO esco_relations (occupation_uri, skill_uri, relation, version) "
                             "VALUES (?, ?, ?, ?)", [(*r, version) for r in relations])
        if self.esco_has_fts():
            conn.execute("DELETE FROM esco_fts WHERE lang = ?", (lang,))
            conn.executemany("INSERT INTO esco_fts (uri, lang, norm) VALUES (?, ?, ?)",
                             [(c["uri"], c["lang"], c["norm"]) for c in concepts])
        counts = {"koncepty": len(concepts), "väzby": len(relations)}
        conn.execute("INSERT INTO esco_imports (version, lang, license, source, files_json, counts_json, imported_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (version, lang, meta.get("license"), meta.get("source"),
                                                      _dumps(meta.get("files") or []), _dumps(counts), utcnow()))
        conn.commit()
        return counts

    def esco_last_import(self, lang: str | None = None) -> sqlite3.Row | None:
        if lang:
            return self._one("SELECT * FROM esco_imports WHERE lang = ? ORDER BY id DESC LIMIT 1", (lang,))
        return self._one("SELECT * FROM esco_imports ORDER BY id DESC LIMIT 1")

    def esco_concept(self, uri: str, lang: str | None = None) -> sqlite3.Row | None:
        if lang:
            return self._one("SELECT * FROM esco_concepts WHERE uri = ? AND lang = ?", (uri, lang))
        return self._one("SELECT * FROM esco_concepts WHERE uri = ? ORDER BY lang = 'sk' DESC LIMIT 1", (uri,))

    def esco_candidates(self, norm_query: str, terms: list[str], kind: str | None, limit: int) -> list[sqlite3.Row]:
        """Kandidáti z fulltextu (FTS5, inak LIKE nad normalizovaným textom)."""
        kind_sql = " AND c.kind = ?" if kind else ""
        if self.esco_has_fts() and terms:
            match = " OR ".join(f'"{t}"*' for t in terms)
            params: list[Any] = [match] + ([kind] if kind else []) + [limit]
            try:
                return self._all("SELECT c.*, bm25(esco_fts) AS rank FROM esco_fts JOIN esco_concepts c "
                                 "ON c.uri = esco_fts.uri AND c.lang = esco_fts.lang WHERE esco_fts MATCH ?"
                                 f"{kind_sql} ORDER BY rank LIMIT ?", params)
            except sqlite3.OperationalError:
                pass
        if not terms:
            return []
        where = " OR ".join("c.norm LIKE ?" for _ in terms)
        params = [f"%{t}%" for t in terms] + ([kind] if kind else []) + [limit * 5]
        return self._all(f"SELECT c.*, 0 AS rank FROM esco_concepts c WHERE ({where}){kind_sql} LIMIT ?", params)

    def esco_related(self, occupation_uri: str, lang: str, limit: int = 15) -> list[sqlite3.Row]:
        return self._all("SELECT c.*, r.relation FROM esco_relations r JOIN esco_concepts c ON c.uri = r.skill_uri "
                         "AND c.lang = ? WHERE r.occupation_uri = ? ORDER BY r.relation = 'essential' DESC, c.pref_label "
                         "LIMIT ?", (lang, occupation_uri, limit))
