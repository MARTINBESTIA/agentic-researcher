"""Brána 1 – dáta a dataset (beží pre hypotézy vybrané na Bránu 0, súbežne s rozhovormi).

Kroky (v poradí podľa modelov):
1. gate1-fields (presný model): zoznam polí potrebných na riešenie problému, kľúčové polia.
2. gate1-sources (agent): ku každému poľu zdroj – názov, URL, prístup, licencia, frekvencia; licenciu
   a frekvenciu kód prijme, iba ak sú doslova na prečítanej stránke.
3. gate1-check (kód): dostupnosť zdroja, odkazy na súbory (CSV/XLSX/JSON), kvalita so zložkami, stav poľa.
4. gate1-datasets (kód): stiahnutie, import do DuckDB, profil, pokrytie organizácií podľa IČO.
5. gate1-repro (presný model + DuckDB): zreprodukovať známe číslo z overeného tvrdenia (NKÚ, revízia
   výdavkov, výročná správa); model navrhne SQL, čísla počíta DuckDB, porovnáva kód.
6. gate1-finish (kód): návrhy žiadostí podľa zákona 211/2000 pre kľúčové polia bez otvoreného zdroja,
   pripravenosť dát (skóre so zložkami) a príznaky. Nič sa automaticky nevyraďuje ani neodosiela.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from .agent import ResearchAgent
from .config import Config
from .critic import evidence_block
from .datasets import (
    FILE_LINK_RE, DatasetError, coverage, download, download_paginated, drop_table, file_format, hyp_dir, import_table,
    profile_table, run_query, schema_text, wide_to_long,
)
from .db import DB
from .models import as_router
from .prompts import GATE1_FIELDS_SYSTEM, GATE1_REPRO_SYSTEM, GATE1_SOURCE_EXTRACT_SYSTEM, GATE1_SOURCES_SYSTEM
from .util import Deadline, utcnow
from .verify import normalize

log = logging.getLogger(__name__)

ACCESS = ("open", "registration", "request_211", "partner", "paid", "unknown")
ACCESS_SCORE = {"open": 1.0, "registration": 0.7, "request_211": 0.4, "partner": 0.3, "paid": 0.2, "unknown": 0.3}
OPEN_LICENSE_RE = re.compile(r"cc[- ]?by|cc0|creative commons|otvoren\w* licenc|open data|odbl|public domain|voľne použi", re.I)
MACHINE_FORMATS = ("csv", "xlsx", "json", "xml", "api", "parquet", "sparql")
REPRO_SCORE = {"match": 1.0, "close": 0.7, "mismatch": 0.2, "failed": 0.0}


# ======================================================================= 1. polia
class DataField(BaseModel):
    name: str
    description: str = ""
    is_key: bool = False
    rationale: str = ""
    source_hints: list[str] = Field(default_factory=list)


class DataFields(BaseModel):
    fields: list[DataField] = Field(default_factory=list)


def define_fields(cfg: Config, db: DB, llm: Any, hyp) -> int:
    router = as_router(llm, cfg, db)
    evidence = evidence_block(db.evidence_for(hyp["id"]), 8) or "(no evidence)"
    res = router.run_task(
        "gate1_fields",
        [{"role": "system", "content": GATE1_FIELDS_SYSTEM.format(max_fields=cfg.gate1.max_fields,
                                                                  lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"PROBLEM: {hyp['title']}\nSEGMENT: {hyp['segment'] or '?'}\n"
                                     f"DESCRIPTION: {hyp['description'] or ''}\n\n<untrusted_document>\n{evidence}\n"
                                     "</untrusted_document>"}],
        DataFields, object_type="hypothesis", object_id=hyp["id"], max_tokens=3500,  # 8 polí po slovensky ~2 000 tokenov
    )
    if not res.ok:
        return 0
    n = 0
    for f in res.value.fields[: cfg.gate1.max_fields]:
        name = " ".join(f.name.split())[:80]
        if not name:
            continue
        rationale = f.rationale.strip() + (" Kde hľadať: " + "; ".join(f.source_hints[:3]) if f.source_hints else "")
        db.upsert_field(hyp["id"], name, f.description.strip(), f.is_key, rationale.strip())
        n += 1
    if n:
        db.start_gate1(hyp["id"])
    return n


def run_fields(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        if deadline.expired():
            break
        if db.fields_for(hyp["id"]):
            continue
        stats["fields"] += define_fields(cfg, db, llm, hyp)
        stats["hypotheses"] += 1
    return dict(stats)


def _hypotheses(cfg: Config, db: DB, ids: list[int] | None):
    if ids:
        return [h for h in (db.get_hypothesis(i) for i in ids) if h is not None]
    return db.gate1_hypotheses()[: cfg.gate1.max_hypotheses_per_run]


# ======================================================================= 2. zdroje (agent)
SOURCE_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "web_search", "description": "Search the web. Returns a JSON list of results (title, url, snippet).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_page", "description": "Download a web page or document and return one part of its text (0 = first part).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "part": {"type": "integer", "default": 0}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "record_source", "description": "Save one data source for one field (only for a page you have read).",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string", "description": "Exact field name from the list"},
            "name": {"type": "string", "description": "Name of the dataset or register"},
            "url": {"type": "string", "description": "Dataset, API or catalog page URL"},
            "page_url": {"type": "string", "description": "The page you read that describes the source"},
            "publisher": {"type": "string"},
            "access": {"type": "string", "enum": list(ACCESS)},
            "license": {"type": "string", "description": "As stated on the page, else empty"},
            "update_frequency": {"type": "string", "description": "As stated on the page, else empty"},
            "formats": {"type": "string", "description": "e.g. CSV, XLSX, API"},
            "evidence_quote": {"type": "string", "description": "Verbatim quote from page_url, max 300 characters"}},
            "required": ["field", "name", "url", "page_url", "access", "evidence_quote"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "End with a short summary.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


class SourceAgent(ResearchAgent):
    """Agent zdrojov dát: rovnaké nástroje na čítanie, namiesto dôkazu ukladá zdroj poľa."""

    def __init__(self, cfg: Config, db: DB, llm: Any, searx, fetcher, hyp, fields: list):
        super().__init__(cfg, db, llm, searx, fetcher, topic=f"Brána 1: {hyp['title']}", hypothesis_id=hyp["id"],
                         task_name="gate1_sources", claim_origin="gate1", doc_kind="gate1")
        self.tools = SOURCE_TOOLS
        self.hyp = hyp
        self.fields = {f["name"]: f for f in fields}
        self.saved: list[int] = []

    def _page_text(self, url: str) -> str | None:
        if url not in self.pages:
            return None
        return "\n".join(self.pages[url][1])

    def match_field(self, value: str) -> str | None:
        """Názov poľa od modelu: často skopíruje celý riadok „názov: popis“ – porovná sa časť pred dvojbodkou."""
        name = re.split(r"\s*[:–—]\s+", str(value or "").strip().strip('"'), maxsplit=1)[0]
        if not name or not self.fields:
            return None
        match = process.extractOne(name, list(self.fields), scorer=fuzz.token_set_ratio, score_cutoff=88)
        return match[0] if match else None

    def record_source(self, args: dict[str, Any]) -> str:
        field_name = self.match_field(args.get("field", ""))
        match = (field_name,) if field_name else None
        if match is None:
            return "Error: unknown field. Use one of: " + "; ".join(self.fields)
        field = self.fields[match[0]]
        page_url = str(args.get("page_url") or args.get("url") or "")
        text = self._page_text(page_url)
        if text is None:
            return "Error: read page_url with read_page first."
        url = str(args.get("url") or page_url).strip()
        if not url.startswith(("http://", "https://")):
            return "Error: url must start with http:// or https://."
        quote = str(args.get("evidence_quote", "")).strip()[:300]
        body = normalize(text)
        quote_ok = quote_on_page(quote, body)
        checks: dict[str, Any] = {"citát_na_stránke": quote_ok}
        license_ = str(args.get("license") or "").strip() or None
        freq = str(args.get("update_frequency") or "").strip() or None
        # Licenciu a frekvenciu prijme kód, iba ak sú doslova na prečítanej stránke (nič sa nehádá).
        if license_ and normalize(license_) not in body:
            checks["licencia_neoverená"] = license_
            license_ = None
        if freq and normalize(freq) not in body:
            checks["frekvencia_neoverená"] = freq
            freq = None
        access = str(args.get("access") or "unknown")
        sid = self.db.add_source(
            field["id"], self.hyp["id"], str(args.get("name") or url)[:200], url, "agent",
            publisher=str(args.get("publisher") or "")[:200] or None, access=access if access in ACCESS else "unknown",
            license=license_, update_frequency=freq, formats=str(args.get("formats") or "")[:100] or None,
            evidence_url=page_url, evidence_snippet=quote if quote_ok else None, checks=checks,
        )
        if sid is None:
            return "This source is already saved for this field."
        self.saved.append(sid)
        return f"Saved source S{sid} for field '{field['name']}'" + ("" if quote_ok else " (quote not found on the page)")

    def dispatch(self, name: str, raw_args: str | None) -> str:
        if name == "record_source":
            try:
                args = json.loads(raw_args or "{}")
            except ValueError:
                return "Error: tool arguments are not a valid JSON object."
            return self.record_source(args if isinstance(args, dict) else {})
        return super().dispatch(name, raw_args)


class ExtractedSource(BaseModel):
    field: str
    name: str = ""
    url: str = ""
    access: str = "unknown"
    license: str = ""
    update_frequency: str = ""
    formats: str = ""
    evidence_quote: str = ""


class ExtractedSources(BaseModel):
    sources: list[ExtractedSource] = Field(default_factory=list)


def sources_from_read_pages(cfg: Config, db: DB, llm: Any, agent: SourceAgent, max_pages: int = 6) -> int:
    """Rýchly model pri nástrojoch často iba číta (reálny beh: 22 krokov, 5 prečítaných stránok ITMS, 0 uložených
    zdrojov). Presný model preto z každej prečítanej stránky vyberie zdroje v štruktúrovanom JSON; overenie je
    rovnaké ako pri nástroji record_source (citát, licencia a frekvencia musia byť na stránke)."""
    router = as_router(llm, cfg, db)
    listing = "\n".join(f'- "{name}" – {f["description"] or ""}' for name, f in agent.fields.items())
    saved = 0
    for url, (doc_id, chunks) in list(agent.pages.items())[:max_pages]:
        res = router.run_task(
            "gate1_source_extract",
            [{"role": "system", "content": GATE1_SOURCE_EXTRACT_SYSTEM},
             {"role": "user", "content": f"DATA FIELDS:\n{listing}\n\nPAGE URL: {url}\n<untrusted_document>\n"
                                         f"{chunks[0][:5000] if chunks else ''}\n</untrusted_document>"}],
            ExtractedSources, object_type="document", object_id=doc_id, max_tokens=1200,
        )
        if not res.ok:
            continue
        for s in res.value.sources[:6]:
            reply = agent.record_source({**s.model_dump(), "url": s.url or url, "page_url": url})
            saved += reply.startswith("Saved")
    return saved


def quote_on_page(quote: str, body: str) -> bool:
    """Doslovný úryvok; model ho niekedy skráti „…“ – vtedy musí byť doslova každá časť (≥ 15 znakov)."""
    parts = [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", quote or "") if len(x.strip()) >= 15]
    if not parts or max(len(x) for x in parts) < 25:
        return False
    return all(fuzz.partial_ratio(normalize(x), body) >= 90 for x in parts)


def catalog_text(cfg: Config) -> str:
    return "\n".join(f"- {c.name} ({c.publisher}): {c.url} – {c.topics}" for c in cfg.gate1.catalog)


def run_sources(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        if deadline.expired():
            break
        fields = db.fields_for(hyp["id"])
        have = Counter(s["field_id"] for s in db.sources_for(hyp["id"]) if s["status"] != "rejected")
        todo = [f for f in fields if have[f["id"]] < 1 and f["status"] in ("open", "missing")]
        if not todo:
            continue
        steps = min(24, cfg.gate1.source_agent_steps + 2 * len(todo))
        system = GATE1_SOURCES_SYSTEM.format(today=date.today().isoformat(), catalog=catalog_text(cfg), max_steps=steps,
                                             lang=cfg.pipeline.output_language)
        listing = "\n".join(f"- {f['name']}{' [KEY]' if f['is_key'] else ''}: {f['description'] or ''}" for f in todo)
        task = (f"Problem: {hyp['title']} (segment: {hyp['segment'] or '?'}).\nFind sources for these fields:\n{listing}\n"
                "Start with the known portals; search in Slovak first.")
        agent = SourceAgent(cfg, db, llm, searx, fetcher, hyp, todo)
        try:
            agent.run(task, steps, system=system, deadline=deadline)
        except Exception as exc:  # noqa: BLE001 – zlyhanie modelu nezastaví beh (invariant 5)
            from openai import APIConnectionError

            if isinstance(exc, APIConnectionError):
                raise
            log.warning("Brána 1: agent zdrojov H%s zlyhal: %s", hyp["id"], exc)
            stats["errors"] += 1
        if not agent.saved and agent.pages:
            stats["sources_from_pages"] += sources_from_read_pages(cfg, db, llm, agent)
        stats["sources"] += len(agent.saved)
        stats["hypotheses"] += 1
        for f in todo:  # agent pole skúsil a nič nenašiel → „chýba“ (nie vyradenie)
            if not any(s["field_id"] == f["id"] for s in db.sources_for(hyp["id"])):
                db.update_field(f["id"], status="missing", status_note="agent nenašiel zdroj")
    return dict(stats)


# ======================================================================= 3. kontrola zdrojov (kód)
def source_quality(src, checks: dict[str, Any]) -> dict[str, Any]:
    """Kvalita zdroja so zložkami (0–1); chýbajúci údaj je „chýba“, nie odhad."""
    formats = (src["formats"] or "").lower() + " " + " ".join(checks.get("súbory_formáty", []))
    machine = 1.0 if any(f in formats for f in MACHINE_FORMATS) or checks.get("súbory") else 0.0
    lic = src["license"] or ""
    license_score = 1.0 if OPEN_LICENSE_RE.search(lic) else (0.5 if lic else 0.0)
    comp = {
        "strojovo_čitateľné": machine,
        "licencia": license_score,
        "frekvencia_známa": 1.0 if src["update_frequency"] else 0.0,
        "dostupné": 1.0 if checks.get("http_ok") else 0.0,
        "prístup": ACCESS_SCORE.get(src["access"], 0.3),
    }
    comp["skóre"] = round(sum(comp.values()) / len(comp), 2)
    return comp


API_SPEC_PATHS = ("swagger.json", "openapi.json", "v1/swagger.json", "v2/swagger.json", "v3/api-docs", "v2/api-docs",
                  "api/swagger.json", "api/openapi.json")
LIMIT_PARAMS = ("limit", "pageSize", "page_size", "size", "per_page", "rows")
CURSOR_PARAMS = ("minId", "min_id", "since_id", "sinceId", "afterId", "after_id", "startId")
OFFSET_PARAMS = ("offset", "skip", "start")
PAGE_PARAMS = ("page", "pageNumber", "page_number")


def paging_of(params: set[str]) -> dict[str, Any] | None:
    """Ako endpoint stránkuje (z mien parametrov v popise API); None = nevieme (stiahne sa jedna odpoveď)."""
    limit = next((p for p in LIMIT_PARAMS if p in params), None)
    cursor = next((p for p in CURSOR_PARAMS if p in params), None)
    offset = next((p for p in OFFSET_PARAMS if p in params), None)
    page = next((p for p in PAGE_PARAMS if p in params), None)
    if not (cursor or offset or page):
        return {"limit": limit} if limit else None
    return {"limit": limit, "cursor": cursor, "offset": offset, "page": page}


def api_endpoints(spec: dict[str, Any], base_url: str, topic: str, max_endpoints: int = 3,
                  paging: dict[str, Any] | None = None) -> list[str]:
    """GET endpointy bez parametrov v ceste z popisu OpenAPI/Swagger, najpodobnejšie téme poľa.
    Ak endpoint pozná limit, pridá sa limit 1000; spôsob stránkovania sa zapíše do `paging` (URL → parametre)."""
    from urllib.parse import urljoin, urlparse

    if spec.get("servers"):
        root = urljoin(base_url, spec["servers"][0].get("url", "/"))
    else:
        parsed = urlparse(base_url)
        host = spec.get("host") or parsed.netloc
        root = f"{(spec.get('schemes') or [parsed.scheme])[0]}://{host}{spec.get('basePath', '')}"
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        get = (ops or {}).get("get") if isinstance(ops, dict) else None
        if not get or "{" in path:
            continue
        params = {p.get("name") for p in get.get("parameters", []) if isinstance(p, dict)}
        if any(p.get("required") and p.get("in") == "query" for p in get.get("parameters", []) if isinstance(p, dict)):
            continue  # povinný parameter nevieme bezpečne vyplniť
        url = root.rstrip("/") + "/" + path.lstrip("/")
        limit = next((p for p in LIMIT_PARAMS if p in params), None)
        if limit:
            url += f"?{limit}=1000"
        if paging is not None and paging_of(params):
            paging[url] = paging_of(params)
        words = " ".join(re.split(r"[/_\-]", path)) + " " + str(get.get("summary") or "")
        out.append((fuzz.token_set_ratio(words.lower(), topic.lower()), url))
    out.sort(key=lambda x: -x[0])
    chosen = [u for _, u in out[:max_endpoints]]
    if paging is not None:
        for u in list(paging):
            if u not in chosen:
                del paging[u]
    return chosen


def discover_api(fetcher, page_url: str, topic: str, paging: dict[str, Any] | None = None) -> list[str]:
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(page_url)
    roots = {f"{parsed.scheme}://{parsed.netloc}/", page_url if page_url.endswith("/") else page_url + "/"}
    for root in roots:
        for path in API_SPEC_PATHS:
            spec = fetcher.fetch_json(urljoin(root, path)) if hasattr(fetcher, "fetch_json") else None
            if isinstance(spec, dict) and spec.get("paths"):
                return api_endpoints(spec, urljoin(root, path), topic, paging=paging)
    return []


def check_source(fetcher, src, topic: str = "") -> dict[str, Any]:
    checks = json.loads(src["checks_json"] or "{}")
    url = src["url"]
    if file_format(url):
        checks.update(http_ok=True, súbory=[url], súbory_formáty=[file_format(url)])
        return checks
    page = fetcher.fetch_html(url)
    checks["http_ok"] = page.status == "ok"
    checks["http_stav"] = page.status
    if page.status == "ok":
        from .contacts import extract_contacts

        links = [href for href, _ in extract_contacts(page.html, page.final_url or url).links if FILE_LINK_RE.search(href)]
        links = list(dict.fromkeys(links))[:10]
        formats = sorted({file_format(u) or "?" for u in links})
        low = page.html.lower()
        if not links and ("swagger" in low or "openapi" in low or "api" in (src["formats"] or "").lower()):
            paging: dict[str, Any] = {}
            endpoints = discover_api(fetcher, page.final_url or url, topic, paging)  # API s popisom OpenAPI/Swagger
            if endpoints:
                checks["api_endpointy"] = endpoints
                checks["stránkovanie"] = paging
                links, formats = endpoints, ["json"]
        checks["súbory"] = links
        checks["súbory_formáty"] = formats
    return checks


def run_check(cfg: Config, db: DB, fetcher, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        for src in db.sources_for(hyp["id"]):
            if deadline.expired():
                return dict(stats)
            old = json.loads(src["checks_json"] or "{}")
            # znova skontrolovať aj API zistené pred stránkovaním (12. 10. 2026), inak sa stiahne iba prvá strana
            if src["status"] == "rejected" or (old.get("http_stav") and not (old.get("api_endpointy")
                                                                            and "stránkovanie" not in old)):
                continue
            field = db.get_field(src["field_id"])
            checks = check_source(fetcher, src, f"{field['name']} {field['description'] or ''}" if field else "")
            db.update_source(src["id"], checks_json=checks, quality_json=source_quality(src, checks),
                             status="verified" if checks.get("http_ok") else "candidate")
            stats["checked"] += 1
        _update_field_statuses(db, hyp["id"])
    return dict(stats)


def _update_field_statuses(db: DB, hid: int) -> None:
    by_field: dict[int, list] = {}
    for s in db.sources_for(hid):
        if s["status"] != "rejected":
            by_field.setdefault(s["field_id"], []).append(s)
    for f in db.fields_for(hid):
        if f["edited_by_human"] or f["status"] in ("requested", "partner"):
            continue
        srcs = by_field.get(f["id"], [])
        if not srcs:
            continue
        good = [s for s in srcs if s["status"] == "verified" and s["access"] in ("open", "registration")
                and json.loads(s["quality_json"] or "{}").get("strojovo_čitateľné")]
        status = "found" if good else "partial"
        if status != f["status"]:
            db.update_field(f["id"], status=status, status_note=None)
        if status == "found":
            db.delete_draft_requests(f["id"])  # otvorený zdroj → žiadosť podľa 211/2000 netreba


# ======================================================================= 4. datasety (kód)
def run_datasets(cfg: Config, db: DB, fetcher, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    limit_total = cfg.gate1.max_total_gb * 1024 ** 3
    orgs = [(o["id"], o["ico"]) for o in db.organizations_with_ico()]
    for hyp in _hypotheses(cfg, db, ids):
        hid = hyp["id"]
        for src in db.sources_for(hid):
            if src["status"] != "verified":
                continue
            checks = json.loads(src["checks_json"] or "{}")
            paging = checks.get("stránkovanie") or {}
            existing = {d["url"]: d for d in db.datasets_for(hid)}
            for url in (checks.get("súbory") or [])[: cfg.gate1.max_files_per_source]:
                if deadline.expired():
                    return dict(stats)
                old = existing.get(url)
                if old is not None and not (paging.get(url) and not (old["note"] or "").startswith("API:")):
                    continue  # už stiahnuté (jednostránkové API sa stiahne znova, keď vieme stránkovať)
                if db.datasets_total_bytes() >= limit_total:
                    stats["total_limit"] += 1
                    return dict(stats)
                stats.update(_one_dataset(cfg, db, fetcher, hid, src, url, orgs, paging.get(url)))
    return dict(stats)


def _one_dataset(cfg: Config, db: DB, fetcher, hid: int, src, url: str, orgs, paging: dict | None = None) -> Counter:
    stats: Counter = Counter()
    dataset_id = db.add_dataset(hid, src["id"], url, "pending", status="error", note="sťahuje sa")
    table = f"ds{dataset_id}"
    try:
        if paging and paging.get("limit") and any(paging.get(k) for k in ("cursor", "offset", "page")):
            dl = download_paginated(cfg, fetcher, url, hyp_dir(cfg, hid), fetcher.blocked, paging)
            fmt = "json"
        else:
            dl = download(cfg, fetcher, url, hyp_dir(cfg, hid), fetcher.blocked)
            fmt = file_format(dl.final_url, dl.content_type) or "bin"
        import_table(cfg, hid, table, dl.path, fmt)
        dl.path.unlink(missing_ok=True)  # v DuckDB je tabuľka; ostáva URL, čas a SHA-256 súboru
        prof = profile_table(cfg, hid, table)
    except DatasetError as exc:
        db.add_dataset(hid, src["id"], url, table, status="too_large" if "limit" in str(exc) else "error",
                       note=str(exc)[:300])
        stats["dataset_errors"] += 1
        return stats
    if prof["personal_columns"]:
        drop_table(cfg, hid, table)
        db.add_dataset(hid, src["id"], url, table, format=fmt, size_bytes=dl.size, sha256=dl.sha256,
                       rows=prof["rows"], columns_json=[c["name"] for c in prof["columns"]], profile_json=prof,
                       personal_data=1, status="personal_data_blocked",
                       note="obsahuje osobné údaje (" + ", ".join(prof["personal_columns"][:5]) + ") – tabuľka sa "
                            "neuložila; pred použitím posúdenie podľa GDPR")
        stats["personal_blocked"] += 1
        return stats
    if prof.get("period_columns"):
        prof["dlhý_tvar"] = wide_to_long(cfg, hid, table)
        stats["long_tables"] += 1 if (prof["dlhý_tvar"] or {}).get("tabuľka") else 0
    db.add_dataset(hid, src["id"], url, table, format=fmt, size_bytes=dl.size, sha256=dl.sha256, rows=prof["rows"],
                   columns_json=[c["name"] for c in prof["columns"]], profile_json=prof, status="ok", note=dl.note)
    for org_id, col, n in coverage(cfg, hid, table, prof["ico_columns"], orgs):
        db.set_coverage(dataset_id, org_id, col, n)
        stats["coverage"] += 1
    stats["datasets"] += 1
    return stats


# ======================================================================= 5. reprodukcia známeho čísla
class ReproSpec(BaseModel):
    sql: str = ""
    explanation: str = ""
    unit: str = ""


SCALES = (1.0, 1e3, 1e6, 1e9, 1e-3, 1e-6, 100.0, 0.01)


def compare(result: float, target: float, cfg: Config) -> tuple[str, float, float]:
    """(verdikt, mierka, relatívna odchýlka): mierka pre jednotky ako „mil. €“ vs „€“ (zapíše sa do poznámky)."""
    if target == 0:
        err, scale = (0.0 if result == 0 else 1.0), 1.0
    else:
        scale, err = min(((s, abs(result / s - target) / abs(target)) for s in SCALES), key=lambda x: x[1])
    verdict = "match" if err <= cfg.gate1.repro_match else ("close" if err <= cfg.gate1.repro_close else "mismatch")
    return verdict, scale, err


def run_repro(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        hid = hyp["id"]
        tables = [d["table_name"] for d in db.datasets_for(hid, only_ok=True)]
        if not tables:
            continue
        done = {r["claim_id"] for r in db.reproductions_for(hid)}
        targets = [c for c in db.number_claims(hid, 20) if c["id"] not in done][: cfg.gate1.max_repro_per_hypothesis]
        schema = schema_text(cfg, hid, tables)
        for claim in targets:
            if deadline.expired():
                return dict(stats)
            numbers = json.loads(claim["numbers_json"] or "[]")
            if not numbers:
                continue
            fact = numbers[0]
            res = router.run_task(
                "gate1_repro",
                [{"role": "system", "content": GATE1_REPRO_SYSTEM.format(lang=cfg.pipeline.output_language)},
                 {"role": "user", "content": f"TARGET NUMBER: {fact.get('value')} {fact.get('unit', '')} – "
                                             f"{fact.get('meaning', '')}\nQUOTE: {claim['quote']}\nSOURCE: {claim['url']}\n\n"
                                             f"<untrusted_document>\n{schema}\n</untrusted_document>"}],
                ReproSpec, object_type="claim", object_id=claim["id"], max_tokens=800,
            )
            base = {"target_value": fact.get("value"), "target_unit": fact.get("unit", ""), "target_text": claim["quote"],
                    "dataset_ids": ",".join(tables)}
            if not res.ok:
                stats["llm_failures"] += 1
                continue
            spec = res.value
            if not spec.sql.strip():
                db.add_reproduction(hid, claim["id"], "failed", explanation=spec.explanation, note="model: z dostupných "
                                    "tabuliek sa číslo nedá vypočítať", **base)
                stats["failed"] += 1
                continue
            try:
                rows = run_query(cfg, hid, spec.sql)
                value = float(rows[0][0])
            except (DatasetError, IndexError, TypeError, ValueError) as exc:
                db.add_reproduction(hid, claim["id"], "failed", sql=spec.sql, explanation=spec.explanation,
                                    note=f"dotaz: {str(exc)[:200]}", **base)
                stats["failed"] += 1
                continue
            verdict, scale, err = compare(value, float(fact.get("value") or 0), cfg)
            db.add_reproduction(hid, claim["id"], verdict, sql=spec.sql, explanation=spec.explanation, result_value=value,
                                scale=scale, rel_error=round(err, 4),
                                note=None if scale == 1 else f"výsledok v inej mierke (×{scale:g})", **base)
            stats[verdict] += 1
    return dict(stats)


# ======================================================================= 6. infozákon, pripravenosť, príznaky
INFO_REQUEST_BODY = """{authority}

Vec: Žiadosť o sprístupnenie informácií podľa zákona č. 211/2000 Z. z. o slobodnom prístupe k informáciám

Podľa § 14 zákona č. 211/2000 Z. z. o slobodnom prístupe k informáciám žiadam o sprístupnenie týchto informácií:

{field}: {description}

Informácie prosím sprístupnite v štruktúrovanej elektronickej podobe (napr. CSV alebo XLSX), ak je to možné,
za obdobie {{OBDOBIE}}, elektronicky na adresu {email}.

Žiadateľ: {name}, {{ADRESA_ŽIADATEĽA}}
Dátum: {{DÁTUM}}
"""


def draft_info_requests(cfg: Config, db: DB, hid: int) -> int:
    """Návrh žiadosti pre kľúčové polia bez otvoreného zdroja (iba text; posiela používateľ, nič sa neodosiela)."""
    existing = {r["field_id"] for r in db.info_requests_for(hid)}
    sources = db.sources_for(hid)
    n = 0
    for f in db.fields_for(hid):
        if not f["is_key"] or f["status"] in ("found", "requested", "partner") or f["id"] in existing:
            continue
        publisher = next((s["publisher"] for s in sources if s["field_id"] == f["id"] and s["publisher"]), None)
        authority = publisher or "{POVINNÁ_OSOBA}"
        sender = cfg.outreach.sender
        body = INFO_REQUEST_BODY.format(authority=authority, field=f["name"], description=f["description"] or "",
                                        email=sender.email, name=sender.name)
        db.add_info_request(hid, f["id"], authority, "Žiadosť o sprístupnenie informácií podľa zákona č. 211/2000 Z. z.", body)
        n += 1
    return n


def readiness(cfg: Config, db: DB, hid: int, now: datetime | None = None) -> dict[str, Any]:
    """Pripravenosť dát so zložkami (0–1) a príznaky; nič sa nevyraďuje."""
    now = now or datetime.now(timezone.utc)
    fields = db.fields_for(hid)
    sources = db.sources_for(hid)
    keys = [f for f in fields if f["is_key"]]
    found = [f for f in fields if f["status"] == "found"]
    keys_found = [f for f in keys if f["status"] == "found"]
    best_quality = []
    for f in fields:
        q = [json.loads(s["quality_json"] or "{}").get("skóre", 0) for s in sources if s["field_id"] == f["id"]
             and s["status"] != "rejected"]
        best_quality.append(max(q) if q else 0.0)
    repro = [r["verdict"] for r in db.reproductions_for(hid)]
    best_repro = max(repro, key=lambda v: REPRO_SCORE.get(v, 0)) if repro else None
    datasets = db.datasets_for(hid)
    ok = [d for d in datasets if d["status"] == "ok"]
    comp = {
        "pokrytie_kľúčových": round(len(keys_found) / len(keys), 2) if keys else 0.0,
        "pokrytie_polí": round(len(found) / len(fields), 2) if fields else 0.0,
        "kvalita_zdrojov": round(sum(best_quality) / len(best_quality), 2) if best_quality else 0.0,
        "reprodukcia": REPRO_SCORE.get(best_repro, 0.0) if best_repro else 0.0,
        "dataset_stiahnutý": 1.0 if ok else 0.0,
    }
    weights = {"pokrytie_kľúčových": 0.35, "pokrytie_polí": 0.15, "kvalita_zdrojov": 0.2, "reprodukcia": 0.2,
               "dataset_stiahnutý": 0.1}
    score = round(sum(comp[k] * w for k, w in weights.items()), 2)
    flags = []
    if fields and not keys_found:
        flags.append("dáta nenájdené (žiadne kľúčové pole)")
    hyp = db.get_hypothesis(hid)
    started = hyp["gate1_started_at"] if hyp else None
    if started and keys and len(keys_found) < len(keys):
        if datetime.fromisoformat(started) + timedelta(weeks=cfg.gate1.key_field_weeks) < now:
            flags.append(f"kľúčové pole nezískané do {cfg.gate1.key_field_weeks} týždňov")
    if any(d["status"] == "personal_data_blocked" for d in datasets):
        flags.append("dataset s osobnými údajmi – posúdenie GDPR")
    if repro and best_repro == "mismatch":
        flags.append("známe číslo sa nepodarilo zreprodukovať")
    orgs = {c["org_id"] for c in db.coverage_for(hid)}
    return {"skóre": score, "zložky": comp, "váhy": weights, "príznaky": flags, "polia": len(fields),
            "kľúčové": len(keys), "kľúčové_nájdené": len(keys_found), "datasety": len(ok),
            "organizácie_v_dátach": len(orgs), "reprodukcia": best_repro, "vypočítané": utcnow()}


def run_finish(cfg: Config, db: DB, deadline: Deadline, ids: list[int] | None) -> dict:
    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        if not db.fields_for(hyp["id"]):
            continue
        stats["info_requests"] += draft_info_requests(cfg, db, hyp["id"])
        summary = readiness(cfg, db, hyp["id"])
        db.set_gate1_summary(hyp["id"], summary)
        if hyp["gate1_status"] in ("not_started", "running") and summary["zložky"]["pokrytie_kľúčových"] == 1.0 \
                and summary["reprodukcia"] in ("match", "close"):
            db.set_gate_status(hyp["id"], 1, "ready")  # pripravené na tvoje rozhodnutie (nie automaticky „prešla“)
        stats["hypotheses"] += 1
    return dict(stats)


def decide(db: DB, hid: int, gate: int, decision: str, reason: str, org_id: int | None = None) -> None:
    """Rozhodnutie brány zapisuje človek; uloží sa s aktuálnymi metrikami."""
    if decision not in ("passed", "paused", "stopped"):
        raise ValueError("Rozhodnutie: passed, paused alebo stopped")
    hyp = db.get_hypothesis(hid)
    metrics = json.loads(hyp["gate1_json"] or "{}") if gate == 1 else {}
    db.add_gate_decision(gate, hid, decision, reason, metrics, org_id)
    if org_id is None:
        db.set_gate_status(hid, gate, decision)
