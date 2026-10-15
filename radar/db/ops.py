"""Prevádzkové tabuľky: provenance volaní modelov, kontrola (review queue), gold labely,
benchmark, štatistiky objavovania, cache registrov a eval."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utc_days_ago, utcnow


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


class OpsMixin:
    conn: sqlite3.Connection

    # ------------------------------------------------------------ model_judgments
    def add_judgment(
        self,
        task: str,
        role: str,
        model: str | None,
        profile: str | None,
        prompt_version: str,
        params: dict[str, Any] | None,
        output: Any,
        verdict: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_s: float | None,
        truncated: bool,
        object_type: str | None = None,
        object_id: int | None = None,
    ) -> int:
        cur = self._exec(
            """INSERT INTO model_judgments (task, object_type, object_id, role, model, profile, prompt_version,
                   params_json, output_json, verdict, prompt_tokens, completion_tokens, latency_s, truncated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task, object_type, object_id, role, model, profile, prompt_version, _json(params), _json(output),
                verdict, prompt_tokens, completion_tokens, latency_s, int(truncated), utcnow(),
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------- lievik (v11)
    def start_funnel_step(self, hid: int | None, step: str, signature: str | None, job_id: int | None) -> int:
        cur = self._exec(
            "INSERT INTO funnel_log (hypothesis_id, step, signature, job_id, started_at) VALUES (?, ?, ?, ?, ?)",
            (hid, step, signature, job_id, utcnow()),
        )
        return int(cur.lastrowid)

    def finish_funnel_step(self, row_id: int, outcome: str, next_step: str | None, reason: str, failed: bool,
                           detail: dict[str, Any] | None = None) -> None:
        self._exec(
            """UPDATE funnel_log SET outcome = ?, next_step = ?, reason = ?, failed = ?, detail_json = ?, finished_at = ?
               WHERE id = ?""",
            (outcome, next_step, reason, int(failed), _json(detail or {}), utcnow(), row_id),
        )

    def funnel_log(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._all(
            """SELECT f.*, h.title FROM funnel_log f LEFT JOIN hypotheses h ON h.id = f.hypothesis_id
               ORDER BY f.id DESC LIMIT ?""", (limit,))

    def funnel_last(self) -> sqlite3.Row | None:
        return self._one("SELECT * FROM funnel_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")

    def funnel_attempts(self, hid: int, step: str, signature: str) -> int:
        """Koľkokrát už krok bežal pri presne tomto stave hypotézy (bez zmeny = zaseknutá)."""
        return int(self._one("SELECT COUNT(*) AS n FROM funnel_log WHERE hypothesis_id = ? AND step = ? AND signature = ?",
                             (hid, step, signature))["n"])

    def funnel_runs(self, hid: int, step: str) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM funnel_log WHERE hypothesis_id = ? AND step = ?", (hid, step))["n"])

    def funnel_steps_since_find(self, step: str = "test") -> int:
        """Počet krokov `step` od posledného hľadania nových hypotéz (find)."""
        last = self._one("SELECT MAX(id) AS id FROM funnel_log WHERE step = 'find'")["id"] or 0
        return int(self._one("SELECT COUNT(*) AS n FROM funnel_log WHERE step = ? AND id > ?", (step, last))["n"])

    def funnel_recent_failures(self) -> int:
        """Zlyhané kroky za sebou (od posledného úspešného)."""
        n = 0
        for r in self._all("SELECT failed FROM funnel_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 20"):
            if not r["failed"]:
                break
            n += 1
        return n

    def cached_output(self, task: str, object_type: str, object_id: int) -> str | None:
        """Výstup posledného úspešného volania úlohy pre objekt (JSON) – opakovaný beh nevolá model znova."""
        row = self._one(
            """SELECT output_json FROM model_judgments WHERE task = ? AND object_type = ? AND object_id = ?
                 AND output_json IS NOT NULL AND output_json != 'null' AND verdict NOT IN ('undecided', 'timeout')
               ORDER BY id DESC LIMIT 1""",
            (task, object_type, object_id),
        )
        return row["output_json"] if row else None

    def judgments_for(self, task: str, object_type: str, object_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM model_judgments WHERE task = ? AND object_type = ? AND object_id = ? ORDER BY id",
            (task, object_type, object_id),
        )

    def task_latency_stats(self) -> dict[tuple[str, str], dict[str, float]]:
        """Priemerný čas a tokeny na volanie podľa (úloha, model) – na plánovanie behu (R-ACC-6)."""
        rows = self._all(
            """SELECT task, model, COUNT(*) AS n, AVG(latency_s) AS latency, AVG(prompt_tokens) AS pt,
                      AVG(completion_tokens) AS ct
               FROM model_judgments WHERE latency_s IS NOT NULL GROUP BY task, model"""
        )
        return {(r["task"], r["model"] or ""): dict(n=r["n"], latency=r["latency"] or 0, pt=r["pt"] or 0, ct=r["ct"] or 0) for r in rows}

    def judgment_counts(self) -> list[sqlite3.Row]:
        return self._all(
            """SELECT task, role, model, COUNT(*) AS n, SUM(truncated) AS truncated, AVG(latency_s) AS latency
               FROM model_judgments GROUP BY task, role, model ORDER BY task"""
        )

    def sample_judgments(self, task: str, verdict: str | None, limit: int, seed: int = 0) -> list[sqlite3.Row]:
        """Náhodná vzorka posúdení (stratifikovaná podľa verdiktu volajúcim) bez existujúceho labelu."""
        sql = """SELECT j.* FROM model_judgments j
                 WHERE j.task = ? AND j.object_type IS NOT NULL AND j.object_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.task = j.task AND l.object_type = j.object_type
                                   AND l.object_id = j.object_id)"""
        params: tuple = (task,)
        if verdict is not None:
            sql += " AND j.verdict = ?"
            params += (verdict,)
        sql += " GROUP BY j.object_type, j.object_id ORDER BY (j.id * 2654435761 + ?) % 4294967296 LIMIT ?"
        return self._all(sql, params + (seed, limit))

    def judgment_verdicts(self, task: str) -> list[str]:
        return [r[0] for r in self._all(
            "SELECT DISTINCT verdict FROM model_judgments WHERE task = ? AND verdict IS NOT NULL", (task,))]

    # --------------------------------------------------------------- review queue
    def enqueue_review(
        self, task: str, object_type: str, object_id: int, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Položka na rozhodnutie človekom. Uzavretá položka sa pri novom probléme znovu otvorí."""
        self._exec(
            """INSERT INTO review_queue (task, object_type, object_id, reason, detail_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'open', ?)
               ON CONFLICT (task, object_type, object_id)
               DO UPDATE SET reason = excluded.reason, detail_json = excluded.detail_json, status = 'open',
                             created_at = excluded.created_at""",
            (task, object_type, object_id, reason, _json(detail), utcnow()),
        )

    def open_reviews(self, task: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if task:
            return self._all(
                "SELECT * FROM review_queue WHERE status = 'open' AND task = ? ORDER BY id LIMIT ?", (task, limit)
            )
        return self._all("SELECT * FROM review_queue WHERE status = 'open' ORDER BY id LIMIT ?", (limit,))

    def review_counts(self) -> dict[str, int]:
        return {
            r["task"]: r["n"]
            for r in self._all("SELECT task, COUNT(*) AS n FROM review_queue WHERE status = 'open' GROUP BY task")
        }

    def is_in_review(self, task: str, object_type: str, object_id: int) -> bool:
        return (
            self._one(
                "SELECT 1 FROM review_queue WHERE task = ? AND object_type = ? AND object_id = ? AND status = 'open'",
                (task, object_type, object_id),
            )
            is not None
        )

    def close_review(self, review_id: int) -> None:
        self._exec("UPDATE review_queue SET status = 'done' WHERE id = ?", (review_id,))

    def get_review(self, review_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM review_queue WHERE id = ?", (review_id,))

    # --------------------------------------------------------------------- labels
    def add_label(self, task: str, object_type: str, object_id: int, label: str, note: str | None = None) -> None:
        self._exec(
            """INSERT INTO labels (task, object_type, object_id, label, note, created_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (task, object_type, object_id) DO UPDATE SET label = excluded.label, note = excluded.note,
                                                                       created_at = excluded.created_at""",
            (task, object_type, object_id, label, note, utcnow()),
        )

    def labels_for(self, task: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM labels WHERE task = ? ORDER BY id", (task,))

    def label_counts(self) -> dict[str, int]:
        return {r["task"]: r["n"] for r in self._all("SELECT task, COUNT(*) AS n FROM labels GROUP BY task")}

    # ---------------------------------------------------------------------- bench
    def add_bench(
        self,
        role: str,
        model: str | None,
        load_s: float | None,
        prompt_tps: float | None,
        gen_tps: float | None,
        json_ok: bool | None,
        tools_ok: bool | None,
        note: str = "",
    ) -> None:
        self._exec(
            """INSERT INTO bench (role, model, load_s, prompt_tps, gen_tps, json_ok, tools_ok, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                role, model, load_s, prompt_tps, gen_tps,
                None if json_ok is None else int(json_ok), None if tools_ok is None else int(tools_ok), note, utcnow(),
            ),
        )

    def latest_bench(self) -> dict[str, sqlite3.Row]:
        rows = self._all("SELECT * FROM bench ORDER BY id")
        return {r["role"]: r for r in rows}

    # ---------------------------------------------------------------- query_stats
    def discovery_yield(self) -> list[sqlite3.Row]:
        """Dokumenty z objavovania podľa kategórie zdroja a domény: počet, relevantné (screen) a overené tvrdenia."""
        return self._all(
            """SELECT d.source_category AS category, d.domain AS domain, COUNT(DISTINCT d.id) AS documents,
                      COUNT(DISTINCT CASE WHEN d.screened = 'yes' THEN d.id END) AS relevant,
                      COUNT(DISTINCT CASE WHEN c.status = 'verified' THEN c.id END) AS verified_claims
               FROM documents d LEFT JOIN claims c ON c.document_id = d.id
               WHERE d.source_kind = 'discovery' AND d.source_category IS NOT NULL
               GROUP BY d.source_category, d.domain"""
        )

    def arm_stats(self) -> dict[str, sqlite3.Row]:
        return {r["arm"]: r for r in self._all("SELECT * FROM query_stats")}

    def record_arm(self, arm: str, tries: int = 0, successes: int = 0, cursor: str | None = None) -> None:
        self._exec(
            """INSERT INTO query_stats (arm, tries, successes, last_used_at, cursor) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (arm) DO UPDATE SET tries = tries + excluded.tries, successes = successes + excluded.successes,
                   last_used_at = excluded.last_used_at, cursor = COALESCE(excluded.cursor, cursor)""",
            (arm, tries, successes, utcnow(), cursor),
        )

    def get_cursor(self, arm: str) -> str | None:
        row = self._one("SELECT cursor FROM query_stats WHERE arm = ?", (arm,))
        return None if row is None else row["cursor"]

    # ------------------------------------------------------------- register cache
    def cache_get(self, url: str, max_age_days: float) -> str | None:
        row = self._one(
            "SELECT body FROM register_cache WHERE url = ? AND fetched_at >= ?", (url, utc_days_ago(max_age_days))
        )
        return None if row is None else row["body"]

    def cache_put(self, url: str, body: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO register_cache (url, body, fetched_at) VALUES (?, ?, ?)", (url, body, utcnow())
        )

    # ------------------------------------------------------------------------ eval
    def add_eval_case(self, task: str, payload: dict[str, Any], source: str = "synthetic") -> int:
        cur = self._exec(
            "INSERT INTO eval_cases (task, payload_json, source, created_at) VALUES (?, ?, ?, ?)",
            (task, _json(payload), source, utcnow()),
        )
        return int(cur.lastrowid)

    def get_eval_case(self, case_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM eval_cases WHERE id = ?", (case_id,))

    def eval_cases_for(self, task: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM eval_cases WHERE task = ? ORDER BY id", (task,))

    def add_eval_result(
        self, task: str, profile: str, model: str | None, thinking: bool, n: int, metrics: dict[str, Any]
    ) -> None:
        self._exec(
            """INSERT INTO eval_results (run_at, task, profile, model, thinking, n, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (utcnow(), task, profile, model, int(thinking), n, _json(metrics)),
        )

    def eval_results(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM eval_results ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- úlohy z GUI (v3)
    JOB_FIELDS = ("pid", "status", "log_path", "progress_json", "stats_json", "finished_at", "started_at")

    def create_job(self, kind: str, args: dict[str, Any], status: str = "starting") -> int:
        """status: starting (spúšťa sa hneď) alebo queued (čaká v poradí; started_at = čas zaradenia)."""
        cur = self._exec(
            "INSERT INTO jobs (kind, args_json, status, started_at) VALUES (?, ?, ?, ?)",
            (kind, json.dumps(args, ensure_ascii=False), status, utcnow()),
        )
        return int(cur.lastrowid)

    def queued_jobs(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id")

    def update_job(self, job_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(self.JOB_FIELDS)
        if unknown:
            raise ValueError(f"Neznáme polia úlohy: {sorted(unknown)}")
        for key in ("progress_json", "stats_json"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        if fields.get("status") == "running":  # úloha z poradia: čas štartu = skutočný štart
            fields["started_at"] = utcnow()
            sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def recent_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))

    def active_jobs(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM jobs WHERE status IN ('starting', 'running') ORDER BY id")

    def bench_by_model(self) -> dict[str, sqlite3.Row]:
        """Posledné meranie pre každý model (výber modelov v GUI)."""
        return {r["model"]: r for r in self._all("SELECT * FROM bench WHERE model IS NOT NULL ORDER BY id")}
