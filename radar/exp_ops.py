"""Operácie nad dátami pre experimenty Brány 2 – univerzálne: model ich vyberie a naplní podľa hypotézy a profilu
dát, kód ich vykoná v DuckDB a zapíše protokol (čo, prečo, koľko riadkov).

Bezpečnosť a správnosť:
- spojenie iba na čítanie bez prístupu k súborom a sieti; pracuje sa v dočasných tabuľkách (nič sa neuloží),
- výrazy od modelu sú iba výrazy (bez `;`, komentárov a príkazov), názvy stĺpcov kontroluje kód,
- história (`prior_agg`) končí deň pred dátumom riadku, budúcnosť (`future_agg`, `linked_agg`) je označená –
  príznak z budúcnosti kód odmietne (únik budúcich dát), štítok z budúcnosti „dozrieva“ (riadky bez celého
  budúceho okna sa vyradia).

Operácie:
- filter (expr) – ponechá riadky,
- derive (name, expr) – nový stĺpec z toho istého riadku,
- text_category (name, column, categories) – zatriedenie textu do kategórií (embeddingy; vzorku skontroluje model),
- link_records (name, column, pattern, target_column, keys) – väzba riadku na skorší „rodičovský“ riadok podľa
  odkazu v texte (napr. dodatok → zmluva podľa čísla), pri zhode kľúčov (napr. ten istý objednávateľ),
- linked_agg (name, link, agg, column, days, expr) – súhrn naviazaných riadkov do `days` dní po rodičovi,
- prior_agg (name, keys, agg, column, days, expr) – súhrn skorších riadkov rovnakých kľúčov (0 = celá história),
- future_agg (name, keys, agg, column, days, expr) – súhrn neskorších riadkov do `days` dní (štítok).
- lookup (name, table, keys, target_column, match, other_time, days, agg, other_value, expr) – súhrn riadkov INEJ
  tabuľky hypotézy so zhodným kľúčom (napr. IČO objednávateľa = IČO subjektu kontroly NKÚ): days > 0 = do N dní
  po dátume riadku (štítok), days < 0 = N dní pred (príznak), 0 = kedykoľvek (iba štítok – čas nie je ohraničený).

Dostupnosť štítka (audit 14. 10. 2026): udalosť z inej tabuľky (napr. zistenie NKÚ) je známa až po zverejnení, nie
v deň udalosti. `lookup` ako štítok preto potrebuje `available_column` (dátum zverejnenia alebo dostupnosti v druhej
tabuľke – oneskorenie spočíta kód ako 95. percentil) alebo `available_lag_days` (predpoklad človeka). Štítok je
„dozretý“ až po okne udalosti + oneskorení dostupnosti; bez toho je čas dostupnosti neznámy a experiment neplatný.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import date, timedelta
from typing import Any, Callable, Literal

import duckdb
import numpy as np
from pydantic import BaseModel, Field

from .config import Config
from .datasets import DatasetError, connect_ro, duck_path

log = logging.getLogger(__name__)

OPS = ("filter", "derive", "text_category", "link_records", "linked_agg", "prior_agg", "future_agg", "lookup")
# hodnota vždy ako číslo: áno/nie (BOOLEAN) → 1/0 – reálny beh E10: avg(BOOLEAN) DuckDB odmietol
AGGS = {"count": "SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)",
        "sum": "SUM(CASE WHEN {cond} THEN TRY_CAST({col} AS DOUBLE) END)",
        "avg": "AVG(CASE WHEN {cond} THEN TRY_CAST({col} AS DOUBLE) END)",
        "max": "MAX(CASE WHEN {cond} THEN TRY_CAST({col} AS DOUBLE) END)"}
FORBIDDEN_RE = re.compile(r";|--|/\*|\*/|\b(attach|detach|copy|export|import|install|load|pragma|create|drop|alter|"
                          r"insert|update|delete|call|set|read_\w+|glob|httpfs|getenv)\b", re.I)
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
TABLE_RE = re.compile(r"^ds\d+(_long)?$")


class Step(BaseModel):
    op: Literal["filter", "derive", "text_category", "link_records", "linked_agg", "prior_agg", "future_agg", "lookup"]
    name: str = ""
    expr: str = ""
    column: str = ""
    categories: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    pattern: str = ""
    target_column: str = ""
    link: str = ""
    agg: Literal["count", "sum", "avg", "max"] = "count"
    days: int = 0
    table: str = ""  # lookup: iná tabuľka hypotézy
    match: Literal["exact", "ico"] = "exact"
    other_time: str = ""
    other_value: str = ""
    available_column: str = ""  # lookup: dátum zverejnenia/dostupnosti udalosti v druhej tabuľke
    available_lag_days: int | None = None  # lookup: predpoklad oneskorenia zverejnenia (dni) – zapíše sa do protokolu
    why: str = ""


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def check_expr(expr: str) -> str:
    expr = (expr or "").strip()
    if not expr:
        raise DatasetError("prázdny výraz")
    if FORBIDDEN_RE.search(expr):
        raise DatasetError(f"nepovolený výraz: {expr[:80]!r} (iba výraz nad stĺpcami, bez príkazov)")
    return expr


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class Workspace:
    """Dočasné tabuľky w0, w1, … nad tabuľkou datasetu; protokol krokov a pôvod stĺpcov z budúcnosti."""

    def __init__(self, cfg: Config, hid: int, table: str, time_column: str, limits: dict[str, str] | None = None,
                 categorize: Callable[[list[str], list[str]], list[tuple[str, float]]] | None = None,
                 check_categories: Callable[[list[tuple[str, str]], list[str]], float | None] | None = None):
        if not TABLE_RE.match(table or ""):
            raise DatasetError(f"neznáma tabuľka {table!r}")
        path = duck_path(cfg, hid)
        if not path.exists():
            raise DatasetError("hypotéza nemá žiadny dataset")
        self.cfg = cfg
        self.con = connect_ro(path, {"enable_external_access": False})
        self.n = 0
        self.protocol: list[dict[str, Any]] = []
        self.future: dict[str, int] = {}  # stĺpec → dni budúcnosti, z ktorých vznikol
        # stĺpec štítka z inej tabuľky → dostupnosť: {"oneskorenie_dní", "spôsob", "tabuľka", "kľúč", "neohraničený"}
        self.availability: dict[str, dict[str, Any]] = {}
        self.categorize, self.check_categories = categorize, check_categories
        cols = [r[0] for r in self.con.execute(f"DESCRIBE {table}").fetchall()]
        if time_column not in cols:
            raise DatasetError(f"stĺpec času {time_column!r} nie je v tabuľke {table}")
        ts = f"TRY_CAST({q(time_column)} AS TIMESTAMP)"
        lo = (limits or {}).get("od") or "1990-01-01"
        hi = (limits or {}).get("do") or (date.today() + timedelta(days=366)).isoformat()
        total = self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        self.con.execute(f"CREATE TEMP TABLE w0 AS SELECT ROW_NUMBER() OVER () AS _rid, CAST({ts} AS DATE) AS _t, * "
                         f"FROM {table} WHERE {ts} IS NOT NULL AND CAST({ts} AS DATE) BETWEEN DATE {_lit(lo)} AND DATE {_lit(hi)}")
        kept = self.rows()
        self.limits = {"od": lo, "do": hi}
        self.protocol.append({"krok": "základ", "op": "base", "popis": f"tabuľka {table}, dátum zo stĺpca {time_column}, "
                              f"iba dni {lo} – {hi}", "riadky_pred": total, "riadky_po": kept})

    # -------------------------------------------------------------- pomocné
    @property
    def table(self) -> str:
        return f"w{self.n}"

    def rows(self) -> int:
        return int(self.con.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])

    def columns(self) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE {self.table}").fetchall()]

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        return self.con.execute(sql, params or []).fetchall()

    def close(self) -> None:
        self.con.close()

    def _col(self, name: str) -> str:
        if name not in self.columns():
            raise DatasetError(f"stĺpec {name!r} neexistuje (dostupné: {', '.join(c for c in self.columns() if not c.startswith('_'))[:400]})")
        return q(name)

    def _new_name(self, name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise DatasetError(f"neplatný názov nového stĺpca {name!r} (malé písmená, číslice, _)")
        if name in self.columns():
            raise DatasetError(f"stĺpec {name!r} už existuje")
        return q(name)

    def _next(self, select_sql: str) -> None:
        try:
            self.con.execute(f"CREATE TEMP TABLE w{self.n + 1} AS {select_sql}")
        except duckdb.Error as exc:
            raise DatasetError(f"krok zlyhal v DuckDB: {str(exc)[:300]}") from exc
        self.con.execute(f"DROP TABLE IF EXISTS w{self.n}")
        self.n += 1

    def _uses_future(self, expr: str) -> int:
        """Najdlhšie budúce okno stĺpcov, ktoré výraz používa (0 = žiadne)."""
        return max([d for c, d in self.future.items() if re.search(rf"\b{re.escape(c)}\b", expr)] or [0])

    def _frame_cond(self, step: Step) -> str:
        return f"({check_expr(step.expr)})" if step.expr.strip() else "TRUE"

    def _agg(self, step: Step) -> str:
        col = self._col(step.column) if step.agg != "count" else "NULL"
        return AGGS[step.agg].format(cond=self._frame_cond(step), col=col)

    # -------------------------------------------------------------- operácie
    def apply(self, step: Step) -> dict[str, Any]:
        before = self.rows()
        entry: dict[str, Any] = {"krok": step.name or step.op, "op": step.op, "prečo": step.why, "riadky_pred": before}
        if step.op == "filter":
            expr = check_expr(step.expr)
            self._next(f"SELECT * FROM {self.table} WHERE ({expr})")
            entry["popis"] = f"ponechané riadky: {expr}"
        elif step.op == "derive":
            name, expr = self._new_name(step.name), check_expr(step.expr)
            self._next(f"SELECT *, ({expr}) AS {name} FROM {self.table}")
            if self._uses_future(expr):
                self.future[step.name] = self._uses_future(expr)
            used = [c for c in self.availability if re.search(rf"\b{re.escape(c)}\b", expr)]
            if used:  # odvodený štítok dedí dostupnosť (najhoršiu) zo stĺpcov, z ktorých vznikol
                worst = max(used, key=lambda c: (self.availability[c]["spôsob"] == "neznámy",
                                                 self.availability[c].get("oneskorenie_dní") or 0))
                self.availability[step.name] = dict(self.availability[worst])
            entry["popis"] = f"{step.name} = {expr}"
        elif step.op == "text_category":
            entry |= self._text_category(step)
        elif step.op == "link_records":
            entry |= self._link(step)
        elif step.op == "linked_agg":
            entry |= self._linked_agg(step)
        elif step.op in ("prior_agg", "future_agg"):
            entry |= self._window(step)
        elif step.op == "lookup":
            entry |= self._lookup(step)
        entry["riadky_po"] = self.rows()
        if step.name and step.op not in ("filter",):
            entry |= self.column_stats(step.name)
        self.protocol.append(entry)
        return entry

    def column_stats(self, name: str) -> dict[str, Any]:
        c = q(name)
        n, nulls, distinct = self.con.execute(
            f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {c} IS NULL), approx_count_distinct({c}) FROM {self.table}").fetchone()
        out: dict[str, Any] = {"prázdne": round(nulls / n, 3) if n else None, "rôznych_hodnôt": int(distinct)}
        try:
            mean, nonzero = self.con.execute(
                f"SELECT AVG(TRY_CAST({c} AS DOUBLE)), AVG(CASE WHEN TRY_CAST({c} AS DOUBLE) != 0 THEN 1.0 ELSE 0 END) "
                f"FROM {self.table}").fetchone()
            if mean is not None:
                out |= {"priemer": round(float(mean), 4), "podiel_nenulových": round(float(nonzero), 4)}
        except duckdb.Error:
            pass
        return out

    def _text_category(self, step: Step) -> dict[str, Any]:
        name, col = self._new_name(step.name), self._col(step.column)
        cats = [c.strip() for c in step.categories if c.strip()][:20]
        if len(cats) < 2:
            raise DatasetError("text_category potrebuje aspoň 2 kategórie")
        limit = self.cfg.experiments.max_category_texts
        texts = [r[0] for r in self.con.execute(
            f"SELECT CAST({col} AS VARCHAR) AS t FROM {self.table} WHERE {col} IS NOT NULL GROUP BY 1 "
            f"ORDER BY COUNT(*) DESC LIMIT {int(limit)}").fetchall()]
        if self.categorize is None:  # skúšobný beh: kategórie sa nepočítajú (embeddingy trvajú minúty)
            mapping: list[tuple[str, str, float]] = []
            note = "skúšobný beh – zatriedenie sa vykoná až pri teste"
        else:
            mapping = [(t, c, s) for t, (c, s) in zip(texts, self.categorize(texts, cats))]
            note = f"zatriedené {len(mapping)} rôznych textov (najčastejšie; ostatné „nezaradené“)"
        self.con.execute("CREATE OR REPLACE TEMP TABLE _cat (t VARCHAR, cat VARCHAR, sim DOUBLE)")
        if mapping:
            self.con.executemany("INSERT INTO _cat VALUES (?, ?, ?)", mapping)
        self._next(f"SELECT w.*, COALESCE(m.cat, 'nezaradené') AS {name} FROM {self.table} w "
                   f"LEFT JOIN _cat m ON CAST(w.{col} AS VARCHAR) = m.t")
        out: dict[str, Any] = {"popis": f"{step.name}: kategórie {', '.join(cats)} podľa {step.column} ({note})"}
        if mapping and self.check_categories is not None:
            sample = random.Random(0).sample(mapping, min(len(mapping), self.cfg.experiments.category_check_sample))
            acc = self.check_categories([(t, c) for t, c, _ in sample], cats)
            if acc is not None:
                out["presnosť_vzorky"] = round(acc, 3)
        dist = self.con.execute(f"SELECT {name}, COUNT(*) FROM {self.table} GROUP BY 1 ORDER BY 2 DESC LIMIT 12").fetchall()
        out["rozdelenie"] = {str(k): int(v) for k, v in dist}
        return out

    def _link(self, step: Step) -> dict[str, Any]:
        name, col, target = self._new_name(step.name), self._col(step.column), self._col(step.target_column)
        keys = [self._col(k) for k in step.keys]
        pattern = (step.pattern or "").strip()
        if not pattern or "(" not in pattern:
            raise DatasetError("link_records potrebuje regulárny výraz so skupinou ( ), ktorá vyberie odkaz")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DatasetError(f"neplatný regulárny výraz: {exc}") from exc
        norm = "lower(regexp_replace(trim(CAST({x} AS VARCHAR)), '\\s+', '', 'g'))"
        join_keys = "".join(f" AND c.{k} = p.{k}" for k in keys)
        self._next(
            f"""SELECT w.*, l.parent AS {name} FROM {self.table} w LEFT JOIN (
                  SELECT c._rid AS child, arg_max(p._rid, p._t) AS parent
                  FROM (SELECT * FROM {self.table} WHERE regexp_matches(CAST({col} AS VARCHAR), {_lit(pattern)})) c
                  JOIN {self.table} p ON {norm.format(x='p.' + target)} =
                       {norm.format(x=f"regexp_extract(CAST(c.{col} AS VARCHAR), {_lit(pattern)}, 1)")}
                       {join_keys} AND p._t <= c._t AND p._rid != c._rid
                  GROUP BY c._rid) l ON l.child = w._rid""")
        linked = self.con.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {name} IS NOT NULL").fetchone()[0]
        matched = self.con.execute(f"SELECT COUNT(*) FROM {self.table} WHERE regexp_matches(CAST({col} AS VARCHAR), "
                                   f"{_lit(pattern)})").fetchone()[0]
        return {"popis": f"{step.name}: riadok s odkazom v {step.column} → skorší riadok s rovnakým {step.target_column}"
                         + (f" a {', '.join(step.keys)}" if step.keys else ""),
                "riadkov_s_odkazom": int(matched), "naviazaných": int(linked)}

    def _linked_agg(self, step: Step) -> dict[str, Any]:
        name, link = self._new_name(step.name), self._col(step.link)
        if step.days <= 0:
            raise DatasetError("linked_agg potrebuje days > 0 (okno po rodičovskom riadku)")
        val = f"TRY_CAST({self._col(step.column)} AS DOUBLE)" if step.agg != "count" else "NULL"
        cond = self._frame_cond(step)
        agg = {"count": "SUM(CASE WHEN c.ok THEN 1 ELSE 0 END)", "sum": "SUM(CASE WHEN c.ok THEN c.v END)",
               "avg": "AVG(CASE WHEN c.ok THEN c.v END)", "max": "MAX(CASE WHEN c.ok THEN c.v END)"}[step.agg]
        zero = "0" if step.agg in ("count", "sum") else "NULL"
        self._next(
            f"""SELECT w.*, COALESCE(a.v, {zero}) AS {name} FROM {self.table} w LEFT JOIN (
                  SELECT p._rid AS pid, {agg} AS v
                  FROM (SELECT {link} AS pid, _t AS ct, {val} AS v, {cond} AS ok FROM {self.table} WHERE {link} IS NOT NULL) c
                  JOIN (SELECT _rid, _t FROM {self.table}) p ON p._rid = c.pid
                  WHERE c.ct >= p._t AND c.ct <= p._t + INTERVAL {int(step.days)} DAY
                  GROUP BY p._rid) a ON a.pid = w._rid""")
        self.future[step.name] = int(step.days)
        return {"popis": f"{step.name} = {step.agg}({step.column or 'riadky'}) naviazaných cez {step.link} do {step.days} dní"
                         + (f", podmienka {step.expr}" if step.expr.strip() else "") + " – budúcnosť (štítok)"}

    def _window(self, step: Step) -> dict[str, Any]:
        name = self._new_name(step.name)
        part = ("PARTITION BY " + ", ".join(self._col(k) for k in step.keys)) if step.keys else ""
        d = int(step.days)
        if step.op == "prior_agg":
            frame = (f"RANGE BETWEEN INTERVAL {d} DAY PRECEDING AND INTERVAL 1 DAY PRECEDING" if d > 0 else
                     "RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 DAY PRECEDING")
        else:
            if d <= 0:
                raise DatasetError("future_agg potrebuje days > 0")
            frame = f"RANGE BETWEEN INTERVAL 1 DAY FOLLOWING AND INTERVAL {d} DAY FOLLOWING"
            self.future[step.name] = d
        zero = "0" if step.agg in ("count", "sum") else "NULL"
        self._next(f"SELECT *, COALESCE({self._agg(step)} OVER ({part} ORDER BY _t {frame}), {zero}) AS {name} "
                   f"FROM {self.table}")
        when = ("celá história" if d == 0 else f"{d} dní") if step.op == "prior_agg" else f"nasledujúcich {d} dní"
        return {"popis": f"{step.name} = {step.agg}({step.column or 'riadky'}) rovnakých {', '.join(step.keys) or 'všetkých'} "
                         f"({'skôr' if step.op == 'prior_agg' else 'neskôr'}: {when}, bez toho istého dňa)"
                         + (f", podmienka {step.expr}" if step.expr.strip() else "")
                         + (" – budúcnosť (štítok)" if step.op == "future_agg" else "")}

    def _lookup(self, step: Step) -> dict[str, Any]:
        from .datasets import ico_sql

        name = self._new_name(step.name)
        other = (step.table or "").strip()
        if not TABLE_RE.match(other):
            raise DatasetError(f"lookup: neznáma tabuľka {other!r}")
        ocols = [r[0] for r in self.con.execute(f"DESCRIBE {other}").fetchall()] if self.con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [other]).fetchone()[0] else []
        if not ocols:
            raise DatasetError(f"lookup: tabuľka {other} neexistuje")
        if len(step.keys) != 1:
            raise DatasetError("lookup potrebuje práve jeden kľúč (stĺpec riadku)")
        local = self._col(step.keys[0])
        for c in [step.target_column] + ([step.other_time] if step.other_time else []) + (
                [step.other_value] if step.agg != "count" else []):
            if c not in ocols:
                raise DatasetError(f"lookup: stĺpec {c!r} nie je v tabuľke {other} ({', '.join(ocols)[:300]})")
        key = (lambda x: ico_sql(x)) if step.match == "ico" else (lambda x: f"lower(trim(CAST({x} AS VARCHAR)))")
        ot = f"CAST(TRY_CAST({q(step.other_time)} AS TIMESTAMP) AS DATE)" if step.other_time else "NULL"
        val = f"TRY_CAST({q(step.other_value)} AS DOUBLE)" if step.agg != "count" else "NULL"
        d = int(step.days)
        if d and not step.other_time:
            raise DatasetError("lookup s oknom dní potrebuje other_time (dátum v druhej tabuľke)")
        when = ("" if d == 0 else f" AND o.ot > r._t AND o.ot <= r._t + INTERVAL {d} DAY" if d > 0 else
                f" AND o.ot >= r._t - INTERVAL {-d} DAY AND o.ot < r._t")
        agg = {"count": "SUM(CASE WHEN o.ok THEN 1 ELSE 0 END)", "sum": "SUM(CASE WHEN o.ok THEN o.v END)",
               "avg": "AVG(CASE WHEN o.ok THEN o.v END)", "max": "MAX(CASE WHEN o.ok THEN o.v END)"}[step.agg]
        zero = "0" if step.agg in ("count", "sum") else "NULL"
        cond = self._frame_cond(step)
        self._next(
            f"""SELECT w.*, COALESCE(a.v, {zero}) AS {name} FROM {self.table} w LEFT JOIN (
                  SELECT r._rid AS rid, {agg} AS v
                  FROM (SELECT _rid, _t, {key(local)} AS k FROM {self.table}) r
                  JOIN (SELECT {key(q(step.target_column))} AS k, {ot} AS ot, {val} AS v, {cond} AS ok FROM {other}) o
                    ON o.k = r.k{when}
                  GROUP BY r._rid) a ON a.rid = w._rid""")
        if d >= 0:  # budúcnosť alebo neohraničený čas: iba štítok
            self.future[step.name] = max(d, 1)
            self.availability[step.name] = self._availability(step, other, ocols)
        hit = self.con.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {name} IS NOT NULL AND {name} != 0").fetchone()[0]
        when_txt = ("kedykoľvek (čas neohraničený – iba štítok)" if d == 0 else f"do {d} dní po dátume riadku (štítok)"
                    if d > 0 else f"{-d} dní pred dátumom riadku (príznak)")
        return {"popis": f"{step.name} = {step.agg} riadkov {other} so zhodným {step.keys[0]} = {step.target_column}"
                         f" ({'IČO' if step.match == 'ico' else 'presná zhoda'}), {when_txt}"
                         + (f", podmienka {step.expr}" if step.expr.strip() else ""), "riadkov_so_zhodou": int(hit),
                **({"dostupnosť": self.availability[step.name]} if step.name in self.availability else {})}

    def _availability(self, step: Step, other: str, ocols: list[str]) -> dict[str, Any]:
        """Kedy je udalosť z druhej tabuľky známa: oneskorenie zverejnenia z dát (95. percentil) a/alebo predpoklad."""
        info: dict[str, Any] = {"tabuľka": other, "kľúč": step.keys[0] if step.keys else "", "match": step.match,
                                "neohraničený": int(step.days) == 0, "oneskorenie_dní": None, "spôsob": "neznámy"}
        lag = 0
        if step.available_column:
            if step.available_column not in ocols:
                raise DatasetError(f"lookup: stĺpec dostupnosti {step.available_column!r} nie je v tabuľke {other}")
            if not step.other_time:
                raise DatasetError("lookup: available_column potrebuje other_time (dátum udalosti)")
            av = f"CAST(TRY_CAST({q(step.available_column)} AS TIMESTAMP) AS DATE)"
            ev = f"CAST(TRY_CAST({q(step.other_time)} AS TIMESTAMP) AS DATE)"
            p95, n = self.con.execute(f"SELECT quantile_cont(date_diff('day', {ev}, {av}), 0.95), COUNT(*) FROM {other} "
                                      f"WHERE {ev} IS NOT NULL AND {av} IS NOT NULL").fetchone()
            if not n:
                raise DatasetError(f"lookup: stĺpec dostupnosti {step.available_column!r} nemá dátumy")
            lag = max(0, int(np.ceil(p95 or 0)))
            info["spôsob"] = f"z dát ({step.available_column}, 95. percentil oneskorenia)"
        if step.available_lag_days is not None:
            if step.available_lag_days < 0:
                raise DatasetError("lookup: available_lag_days nesmie byť záporné")
            lag += int(step.available_lag_days)
            info["spôsob"] = (info["spôsob"] + " + " if step.available_column else "") + \
                f"predpoklad {int(step.available_lag_days)} dní (over pri uzamknutí)"
        if step.available_column or step.available_lag_days is not None:
            info["oneskorenie_dní"] = lag
        return info

    # -------------------------------------------------------------- štítky z budúcnosti
    def label_columns(self, label: str) -> list[str]:
        """Stĺpce štítka z inej tabuľky (lookup), z ktorých štítok vznikol – pre kontrolu dostupnosti a jednotky."""
        return [c for c in self.availability if c == label or re.search(rf"\b{re.escape(c)}\b", label)]

    def label_horizon(self, label: str) -> int:
        """Dni od dátumu riadku, kým je štítok známy: budúce okno + oneskorenie dostupnosti (zverejnenia)."""
        days = max(self.future.get(label, 0), self._uses_future(label))
        lags = [self.availability[c].get("oneskorenie_dní") or 0 for c in self.label_columns(label)]
        return days + max(lags or [0])

    def matured(self, label: str) -> int:
        """Riadky, ktorých budúce okno štítka (+ oneskorenie zverejnenia) ešte neuplynulo, sa vyradia (štítok by bol
        neúplný). Vráti dni do dozretia štítka."""
        days = self.label_horizon(label)
        if not days:
            return 0
        cutoff = date.fromisoformat(self.limits["do"]) - timedelta(days=days)
        before = self.rows()
        self._next(f"SELECT * FROM {self.table} WHERE _t <= DATE {_lit(cutoff.isoformat())}")
        self.protocol.append({"krok": "dozretie štítka", "op": "mature", "popis": f"iba riadky do {cutoff} (štítok {label} "
                              f"potrebuje {days} dní budúcnosti)", "riadky_pred": before, "riadky_po": self.rows()})
        return days


def label_concentration(ws: Workspace, label: str, ent: str, top: int = 5) -> dict[str, Any] | None:
    """Na koľkých entitách je štítok v porovnaní s náhodným rozložením rovnakého počtu štítkov (každá entita
    s n riadkami má štítok aspoň raz s pravdepodobnosťou 1 − (1 − p)^n). Oveľa menej entít = experiment meria hlavne,
    KTORÉ entity štítok majú (vlastnosť entity), nie ktoré položky. Reálny beh E6: zistenie NKÚ „u dodávateľa“ malo
    206 z 88 759 dodávateľov – ministerstvá, kraje, mestá a univerzity (NKÚ kontroluje iba verejné subjekty)."""
    cols = ws.columns()
    if not ent or not label or ent not in cols or label not in cols:
        return None
    base = (f"WITH e AS (SELECT CAST({q(ent)} AS VARCHAR) AS e, COUNT(*) AS n, "
            f"SUM(CASE WHEN TRY_CAST({q(label)} AS DOUBLE) > 0 THEN 1 ELSE 0 END) AS p "
            f"FROM {ws.table} WHERE {q(ent)} IS NOT NULL GROUP BY 1)")
    n_ent, pos_ent, pos, expected = ws.query(
        f"{base}, r AS (SELECT SUM(p)::DOUBLE / SUM(n) AS r FROM e) "
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE p > 0), SUM(p), SUM(1 - POW(1 - (SELECT r FROM r), n)) FROM e")[0]
    if not n_ent or not pos:
        return None
    tops = ws.query(f"{base} SELECT e, p FROM e WHERE p > 0 ORDER BY p DESC, e LIMIT {int(top)}")
    top10 = ws.query(f"{base} SELECT SUM(p) FROM (SELECT p FROM e ORDER BY p DESC LIMIT 10)")[0][0]
    expected = float(expected or 0)
    return {"entity": int(n_ent), "entity_so_štítkom": int(pos_ent), "očakávané_entity": round(expected),
            "pomer_k_náhodnému": round(pos_ent / expected, 3) if expected else None,
            "top10_podiel_štítkov": round(float(top10 or 0) / float(pos), 3),
            "najčastejšie": [{"entita": str(v), "štítkov": int(c)} for v, c in tops],
            "sústredený": bool(expected >= 10 and pos_ent <= 0.2 * expected)}


def concentration_text(conc: dict[str, Any], ent: str) -> str:
    top = ", ".join(f"{t['entita']} ({t['štítkov']})" for t in (conc.get("najčastejšie") or [])[:3])
    return (f"Štítok má iba {conc['entity_so_štítkom']} z {conc['entity']} entít ({ent}); pri náhodnom rozložení "
            f"rovnakého počtu štítkov by ich bolo približne {conc['očakávané_entity']}. Experiment tak meria hlavne, "
            "KTORÉ entity štítok majú (vlastnosť entity), nie ktoré položky – over, či tieto entity patria do cieľovej "
            f"skupiny rozhodnutia (najčastejšie: {top}).")

