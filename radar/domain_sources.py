"""Zdroje doménových podkladov (dodatok 16. 10. 2026, časť 3): lokálny ESCO index, fulltext, kandidáti povolaní
a ohraničený doménový výskum cez existujúci SearXNG a fetcher.

- **ESCO** (európska klasifikácia povolaní a zručností): balík CSV sa stiahne ručne zo stránky ESCO a importuje
  príkazom `esco-import`; zachovajú sa stabilné URI konceptov, jazyk, verzia a väzby povolanie → zručnosť. Opis
  povolania má rozsah `occupation` – nikdy nie je faktom o práci konkrétnej firmy.
- **Fulltext** nad názvom, alternatívnymi názvami a opisom (FTS5, inak LIKE nad normalizovaným textom); slovenské
  tvary sa skracujú na kmeň. Sémantické vyhľadávanie je vypnuté (`semantic_search_enabled`), kým sa nezmerajú chyby
  fulltextu. Skóre podobnosti nie je pravdepodobnosť pravdivosti.
- **Rola → povolanie:** kandidáti s dôvodom zhody; jediný jednoznačný kandidát sa vyberie pravidlom, inak ostanú
  kandidáti na výber človekom (žiadne falošne presné mapovanie).
- **Web:** dopyty k procesu, dnešnému riešeniu, existujúcim nástrojom, významu dát a možnému dôvodu, prečo nový
  nástroj nepomôže; z prečítaných stránok sa vyberú doslovné úryvky (výsledok vyhľadávača sám nie je podklad).
  Úryvky s e-mailom alebo telefónom sa neukladajú. Limity: dopyty, dokumenty, úryvky, čas; výpadok zdroja = pokračuje
  sa s tým, čo je (označené v štatistike), nikdy nekonečný retry.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .config import Config
from .db import DB
from .privacy import fold, mask_text
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

ESCO_LICENSE = "ESCO © Európska únia – podmienky opätovného použitia podľa stránky ESCO (over pri stiahnutí balíka)"
STOP = {"pre", "and", "the", "pri", "ako", "alebo", "ich", "jej", "jeho", "ktore", "ktory", "ktora", "ktori", "nie",
        "sa", "na", "od", "do", "za", "zo", "so", "vo", "of", "in", "for", "with", "aby", "kde", "kto", "co", "tento",
        "tieto", "toto", "vasom", "vas", "nas", "sme", "ste", "byt", "moze", "maju", "ma", "su", "je"}
VERSION_RE = re.compile(r"v?(\d+\.\d+(?:\.\d+)?)")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", fold(text or "")).strip()


def stems(text: str, min_len: int = 3) -> list[str]:
    """Kmene slov na fulltext (slovenské tvary: „nákupcu“ → „nakup“)."""
    out = []
    for w in normalize(text).split():
        if len(w) < min_len or w in STOP or w.isdigit():
            continue
        out.append(w[: max(5, len(w) - 3)] if len(w) > 5 else w)
    return list(dict.fromkeys(out))


# ======================================================================= ESCO import
def _files(directory: Path, lang: str | None) -> tuple[str, dict[str, Path]]:
    found: dict[str, Path] = {}
    for p in directory.rglob("*.csv"):
        name = p.name.lower()
        m = re.match(r"(occupations|skills|occupationskillrelations)_?([a-z]{2})?\.csv$", name)
        if not m:
            continue
        kind, file_lang = m.group(1), m.group(2)
        if lang and file_lang and file_lang != lang:
            continue
        found[kind] = p
        lang = lang or file_lang
    if "occupations" not in found:
        raise FileNotFoundError(f"V {directory} nie je súbor occupations_<jazyk>.csv z ESCO balíka (formát CSV).")
    return lang or "sk", found


def _read(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


def import_esco(db: DB, directory: str | Path, lang: str | None = None, version: str | None = None,
                license_note: str = ESCO_LICENSE) -> dict[str, Any]:
    """Import ESCO balíka (CSV) do lokálneho indexu. Idempotentný: rovnaký balík = rovnaký stav (jazyk sa nahradí)."""
    directory = Path(directory)
    lang, files = _files(directory, lang)
    if not version:
        m = VERSION_RE.search(directory.name) or VERSION_RE.search(str(directory))
        version = f"v{m.group(1)}" if m else "neznáma"
    concepts: list[dict[str, Any]] = []
    for kind, key in (("occupation", "occupations"), ("skill", "skills")):
        if key not in files:
            continue
        for row in _read(files[key]):
            uri = (row.get("conceptUri") or "").strip()
            label = (row.get("preferredLabel") or "").strip()
            if not uri or not label:
                continue
            alt = " | ".join(x.strip() for x in (row.get("altLabels") or "").splitlines() if x.strip())
            desc = (row.get("description") or row.get("definition") or "").strip()
            concepts.append({"uri": uri, "lang": lang, "kind": kind, "pref_label": label, "alt_labels": alt or None,
                             "description": desc or None, "isco_group": (row.get("iscoGroup") or "").strip() or None,
                             "code": (row.get("code") or "").strip() or None,
                             "norm": normalize(f"{label} {alt} {desc}"), "version": version})
    relations = []
    if "occupationskillrelations" in files:
        for row in _read(files["occupationskillrelations"]):
            occ, skill = (row.get("occupationUri") or "").strip(), (row.get("skillUri") or "").strip()
            if occ and skill:
                relations.append((occ, skill, (row.get("relationType") or "optional").strip().lower()))
    counts = db.esco_replace(version, lang, concepts, relations,
                             {"license": license_note, "source": str(directory),
                              "files": sorted(p.name for p in files.values())})
    return {"jazyk": lang, "verzia": version, **counts}


def search_esco(cfg: Config, db: DB, query: str, kind: str | None = "occupation", limit: int = 10) -> list[dict[str, Any]]:
    """Kandidáti z lokálneho ESCO podľa fulltextu, zoradení podľa zhody názvu (rapidfuzz) – skóre je zhoda textu,
    nie pravdepodobnosť správnosti."""
    terms = stems(query)
    rows = list(db.esco_candidates(normalize(query), terms, kind, limit * 3))
    q = normalize(query)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        labels = [r["pref_label"]] + [x for x in (r["alt_labels"] or "").split(" | ") if x]
        best = max(labels, key=lambda lab: fuzz.token_sort_ratio(q, normalize(lab)))
        score = fuzz.token_sort_ratio(q, normalize(best))  # slová navyše znižujú zhodu („nákupca“ ≠ „nákupca energií“)
        basis = "názov" if best == r["pref_label"] else f"alternatívny názov „{best}“"
        if score < 60:
            score, basis = max(score, 40), "fulltext v opise"
        prev = out.get(r["uri"])
        if prev is None or score > prev["score"] or (score == prev["score"] and r["lang"] == cfg.domain_research.esco_language):
            out[r["uri"]] = {"uri": r["uri"], "label": r["pref_label"], "lang": r["lang"], "score": int(score),
                             "basis": basis, "version": r["version"], "description": r["description"] or ""}
    return sorted(out.values(), key=lambda x: (-x["score"], x["label"]))[:limit]


def map_roles(cfg: Config, db: DB, key: str, roles: list[str]) -> list[dict[str, Any]]:
    """Rola → kandidátne povolania. Pravidlo vyberie iba jednoznačnú zhodu (≥ 95 a ďalší kandidát < 85); ručný výber
    človeka sa neprepisuje."""
    if db.esco_last_import() is None:
        return []
    existing = {m["role_label"]: m for m in db.role_mappings(key)}
    out = []
    for role in dict.fromkeys(r.strip() for r in roles if r and r.strip()):
        cands = search_esco(cfg, db, role, "occupation", cfg.domain_research.max_role_candidates)
        fields: dict[str, Any] = {"mapping_basis": "fulltext + zhoda názvu",
                                  "source_version": cands[0]["version"] if cands else None}
        prev = existing.get(role)
        if prev is not None and prev["chosen_by"] == "človek":
            pass
        elif cands and cands[0]["score"] >= 95 and (len(cands) == 1 or cands[1]["score"] < 85):
            fields.update(chosen_uri=cands[0]["uri"], chosen_by="pravidlo")
        else:
            fields.update(chosen_uri=None, chosen_by=None)  # nejednoznačné: kandidáti ostanú na výber
        mid = db.upsert_role_mapping(key, role, cands, **fields)
        out.append({"id": mid, "rola": role, "kandidáti": cands, **fields})
    return out


def esco_evidence(cfg: Config, db: DB, key: str) -> int:
    """Doménové podklady z vybraných povolaní (rozsah occupation): opis a hlavné zručnosti."""
    added = 0
    for m in db.role_mappings(key):
        if not m["chosen_uri"]:
            continue
        concept = db.esco_concept(m["chosen_uri"], cfg.domain_research.esco_language) or db.esco_concept(m["chosen_uri"])
        if concept is None:
            continue
        skills = [s["pref_label"] for s in db.esco_related(concept["uri"], concept["lang"], 10)]
        excerpt = (concept["description"] or "")[:700]
        if skills:
            excerpt += (" " if excerpt else "") + "Hlavné zručnosti a znalosti: " + "; ".join(skills) + "."
        if not excerpt.strip():
            continue
        before = len(db.domain_evidence(key))
        db.add_domain_evidence(key, "esco", excerpt, hashlib.sha256(f"{concept['uri']}|{excerpt}".encode()).hexdigest(),
                               "occupation", source_url=concept["uri"], local_reference=f"esco:{concept['uri']}",
                               title=f"ESCO: {concept['pref_label']} (rola „{m['role_label']}“)",
                               source_locator=concept["uri"], source_version=concept["version"],
                               language=concept["lang"], jurisdiction="EÚ")
        added += len(db.domain_evidence(key)) - before
    return added


# ======================================================================= web
def queries_for(title: str, segment: str, roles: list[str], limit: int) -> list[tuple[str, str]]:
    """Dopyty podľa účelu: proces, dnešné riešenie, existujúce nástroje, význam dát, prečo nástroj nepomôže, rola."""
    base = " ".join(title.split()[:8])
    seg = " ".join((segment or "").split()[:5])
    out = [("process", f"{base} postup kroky"), ("current", f"{seg} {base} ako to riešia dnes".strip()),
           ("solutions", f"{base} softvér riešenie"), ("data", f"{base} metodika definícia údajov"),
           ("alternatives", f"{base} príčiny problémov")]
    out += [("roles", f"{r} náplň práce {seg}".strip()) for r in roles[:2]]
    return out[:max(0, limit)]


def _windows(text: str, size: int = 600) -> list[str]:
    """Doslovné okná textu (presné podreťazce stránky): odsek, dlhý odsek rozdelený na hraniciach viet. Odseky
    s e-mailom alebo telefónom sa vynechajú (osobné údaje sa do podkladov neukladajú)."""
    out = []
    for part in re.split(r"\n\s*\n|\n", text):
        part = part.strip()
        if not part or mask_text(part) != part:
            continue
        if len(part) <= size:
            out.append(part)
            continue
        start = 0
        ends = [m.end() for m in re.finditer(r"[.!?](\s|$)", part)] + [len(part)]
        last = start
        for e in ends:
            if e - start > size and last > start:
                out.append(part[start:last].strip())
                start = last
            last = e
        if start < len(part):
            out.append(part[start:][:size].strip())
    return [w for w in out if len(w) >= 80]


def select_chunks(text: str, terms: list[str], per_doc: int = 3) -> list[tuple[int, str]]:
    """Doslovné úryvky stránky s najväčším pokrytím pojmov domény; bez e-mailov a telefónov."""
    scored = []
    for w in _windows(text):
        if mask_text(w) != w:  # e-mail alebo telefón – osobné údaje sa do podkladov neukladajú
            continue
        nw = normalize(w)
        score = sum(1 for t in terms if t in nw)
        if score >= 2:
            scored.append((score, w))
    scored.sort(key=lambda x: -x[0])
    return scored[:per_doc]


def web_evidence(cfg: Config, db: DB, key: str, title: str, segment: str, roles: list[str], searx, fetcher,
                 stop_at: float, stats: dict[str, Any]) -> int:
    dr = cfg.domain_research
    terms = stems(f"{title} {segment} {' '.join(roles)}")
    seen = {e["source_url"] for e in db.domain_evidence(key) if e["source_kind"] == "web"}
    candidates: list[tuple[int, str, Any, str]] = []
    for purpose, q in queries_for(title, segment, roles, dr.max_queries_per_domain):
        if time.monotonic() >= stop_at:
            stats["ukončenie"] = "časový limit"
            break
        try:
            results = searx.search(q)
        except Exception as exc:  # noqa: BLE001 – výpadok vyhľadávača: pokračuje sa s tým, čo je
            stats.setdefault("výpadky", []).append(f"vyhľadávanie: {type(exc).__name__}")
            break
        stats["dopyty"] = stats.get("dopyty", 0) + 1
        for r in results:
            if stats.get("dokumenty", 0) >= dr.max_documents_per_domain or time.monotonic() >= stop_at:
                break
            if r.url in seen:
                continue
            seen.add(r.url)
            try:
                page = fetcher.fetch(r.url)
            except Exception as exc:  # noqa: BLE001
                stats.setdefault("výpadky", []).append(f"stiahnutie: {type(exc).__name__}")
                continue
            if page is None:
                continue
            stats["dokumenty"] = stats.get("dokumenty", 0) + 1
            for score, chunk in select_chunks(page.text, terms):
                candidates.append((score, purpose, page, chunk))
    candidates.sort(key=lambda x: -x[0])
    added = 0
    for score, purpose, page, chunk in candidates[: dr.max_evidence_chunks]:
        host = re.sub(r"^https?://(www\.)?", "", page.final_url or page.url).split("/")[0]
        before = len(db.domain_evidence(key))
        db.add_domain_evidence(key, "web", chunk, hashlib.sha256(chunk.encode()).hexdigest(), "sector",
                               source_url=page.final_url or page.url, title=page.title[:200],
                               source_locator=f"účel: {purpose}", source_version=page.content_hash[:16],
                               language="sk", jurisdiction="SK" if host.endswith(".sk") else None)
        added += len(db.domain_evidence(key)) - before
    stats["úryvky"] = stats.get("úryvky", 0) + added
    return added


# ======================================================================= ohraničený pokus
def research_domain(cfg: Config, db: DB, llm: Any, hid: int, searx=None, fetcher=None,
                    deadline: Deadline | None = None, explicit: bool = False) -> dict[str, Any]:
    """Jeden ohraničený pokus o doménové podklady (ESCO, web) a nová verzia prehľadu. Pokus sa pre rovnakú verziu
    vstupu neopakuje (iba nové podklady alebo výslovná žiadosť). Neúplný výsledok → partial / needs_research s dôvodom;
    oslovenia pokračujú s označenými otázkami."""
    import json

    from .domain import STATUS_LABEL, coverage, finish_attempt
    from .value import brief_inputs, domain_of, ensure_brief

    dr = cfg.domain_research
    key, title, ids = domain_of(db, hid)
    pre = brief_inputs(db, hid, dr.max_evidence_chunks)[-1]
    state = db.research_get(key)
    prev = json.loads(state["stats_json"] or "{}") if state is not None and state["stats_json"] else {}
    queued = state is not None and state["status"] == "queued"
    if not dr.enabled:
        return {"preskočené": "domain_research.enabled = false"}
    if (prev.get("vstup") == pre and prev.get("pokusy", 0) >= dr.max_attempts_per_input_version and not explicit
            and not (queued and state["attempts"] == 0)):
        return {"preskočené": "rovnaká verzia vstupu už mala pokus"}
    started = time.monotonic()
    budget = dr.max_wall_seconds_per_pass
    if deadline is not None:
        budget = min(budget, deadline.remaining_minutes() * 60)
    stop_at = started + budget
    stats: dict[str, Any] = {"vstup": pre, "pokusy": (prev.get("pokusy", 0) if prev.get("vstup") == pre else 0) + 1,
                             "začiatok": utcnow()}
    db.research_upsert(key, title, status="researching", started_at=utcnow(), stats_json=stats)
    h = db.get_hypothesis(hid)
    roles = sorted({r for x in ids for t in db.contact_targets_for(x) for r in json.loads(t["roles_json"] or "[]")})
    latest = db.latest_brief(key)
    if latest is not None:
        b = json.loads(latest["brief_json"] or "{}")
        roles += [v.get("text") for v in (b.get("roles") or {}).values() if v.get("text")]
    try:
        stats["mapovanie_rolí"] = len(map_roles(cfg, db, key, roles[:8]))
        stats["esco"] = esco_evidence(cfg, db, key)
    except Exception as exc:  # noqa: BLE001 – ESCO je voliteľný zdroj
        stats.setdefault("výpadky", []).append(f"ESCO: {type(exc).__name__}")
    if searx is not None and fetcher is not None:
        web_evidence(cfg, db, key, title, h["segment"] if h is not None else "", roles[:3], searx, fetcher, stop_at, stats)
    else:
        stats.setdefault("výpadky", []).append("web: vyhľadávanie nie je k dispozícii")
    brief_id = ensure_brief(cfg, db, llm, hid, reason="doménový výskum", force=explicit)
    stats["sekundy"] = round(time.monotonic() - started, 1)
    stats["prehľad"] = brief_id
    cov, status = coverage(db, key)
    after = db.research_get(key)
    if after is not None and after["status"] == "needs_research" and after["stop_reason"]:
        status, reason = "needs_research", after["stop_reason"]
    elif brief_id is None:
        status, reason = "needs_research", "žiadne využiteľné podklady"
    else:
        missing = [k for k, v in cov.items() if v == "chýba"]
        reason = "pokrytie všetkých častí" if status == "ready" else (
            "chýba: " + ", ".join(missing) if missing else "časti iba ako hypotézy s otázkami")
    if stats.get("výpadky"):
        reason += " · výpadky: " + "; ".join(stats["výpadky"][:3])
    if stats.get("ukončenie"):
        reason += f" · {stats['ukončenie']}"
    finish_attempt(db, key, title, status, reason,
                   "použiť v príprave oslovení" if status == "ready" else
                   "pokračovať s označenými otázkami; nový pokus pri nových podkladoch alebo na tvoju žiadosť", cov, stats)
    return {"doména": key, "stav": STATUS_LABEL.get(status, status), **{k: v for k, v in stats.items() if k != "vstup"}}
