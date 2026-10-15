"""Väzba požiadavky na dáta (Brána 1): pole → verzia datasetu → stĺpec alebo výraz → jednotka → význam → dôkaz.

Audit 14. 10. 2026: pole sa označilo „nájdené“, keď existoval strojovo čitateľný otvorený zdroj – bez väzby na reálny
stĺpec a bez kontroly významu (H18: register liekov ako „schválená cena“ aj „expirácia patentu“). Teraz:

1. presný model navrhne väzbu (tabuľka, stĺpec alebo výraz, jednotka, entita, granularita, význam, dôkaz) alebo povie,
   že pole v dátach nie je (úloha field_binding),
2. kód overí: tabuľka je stiahnutý dataset v stave „ok“, výraz sa dá vyhodnotiť, pokrytie (podiel neprázdnych hodnôt),
   typ vzorky podľa druhu poľa (dátum, suma/počet, IČO), časové pokrytie a vzorka hodnôt; pojmová poistka: pole
   o patente, cene, odpovedi, komunikácii či pôvodnom pláne musí mať tento pojem v stĺpci – podobnosť slov nestačí,
3. význam kľúčového poľa potvrdí človek (GUI Brána 1 → Mapovanie polí); potvrdenie je referenčné hodnotenie (label).

Stavy poľa (rebrík): open → partial (odkaz na zdroj) → available (súbor overený požiadavkou) → loaded (dataset „ok“)
→ found (väzba overená kódom); schema_only = iba opis schémy (bez nameraných hodnôt); partner / requested / missing.
Brána 1 prejde pravidlom iba vtedy, keď má každé kľúčové pole väzbu potvrdenú človekom na aktuálnej verzii datasetu.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel

from .config import Config
from .datasets import PARSER_VERSION, DatasetError, run_query, schema_text
from .db import DB
from .models import as_router
from .names import fold
from .prompts import FIELD_BINDING_SYSTEM
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

GOOD = ("code_ok", "confirmed")
FIELD_LEVEL = {"open": 0, "missing": 0, "partial": 1, "schema_only": 1, "available": 2, "loaded": 3, "found": 4}
FIELD_STATUS_LABEL = {
    "open": "hľadá sa", "missing": "nenájdené", "partial": "iba odkaz na zdroj", "schema_only": "iba opis schémy (bez dát)",
    "available": "súbor dostupný (význam neoverený)", "loaded": "dáta načítané (bez overeného stĺpca)",
    "found": "stĺpec v dátach overený kódom", "requested": "vyžiadané (211/2000)", "partner": "od partnera (interné dáta)",
}
# Pojmy, ktoré musí mať stĺpec, keď ich má pole (bez diakritiky): (pojem, výskyt v poli, opora v stĺpci alebo výraze).
CONCEPTS: list[tuple[str, str, str]] = [
    ("patent", r"patent|patentov", r"patent|spc|ochran"),
    ("cena", r"\bcen[ay]?\b|\bceny\b|price|uhrad", r"cen|price|uhrad|suma|eur|amount|hodnot|plat"),
    ("odpoveď", r"odpove|response|reakci", r"odpov|response|reakc|reply"),
    ("komunikácia", r"komunik", r"komunik|sprav|email|ticket|podnet|request|kontakt"),
    ("pôvodný plán", r"povodn|slubeny|slubene|original", r"povodn|plan|slub|original|baseline|promis"),
    ("indikácia", r"indikac", r"indik"),
    ("úrok", r"\buro[kc]", r"uro[kc]|interest"),
]
DATE_FIELD_RE = re.compile(r"datum|termin|lehot|splatnost|expir|zaciat|koniec|ukoncen|deadline|kedy", re.I)
NUMBER_FIELD_RE = re.compile(r"cena|suma|vydav|naklad|rozpoc|uhrad|eur|hodnot|dlh|prijem|pocet|mnozstv|objem|uspor|cas\b|minut",
                             re.I)
ICO_FIELD_RE = re.compile(r"\bico\b", re.I)


class FieldBindingOut(BaseModel):
    in_data: bool = False
    table: str = ""
    expression: str = ""
    unit: str = ""
    entity_kind: str = ""
    grain: str = ""
    meaning: str = ""
    evidence: str = ""
    missing: str = ""


def usable_datasets(db: DB, hid: int) -> list:
    """Datasety v stave „ok“ bez duplikátov distribúcií (tá istá kocka v inom jazyku či formáte sa ráta raz)."""
    out = []
    for d in db.datasets_for(hid, only_ok=True):
        rep = (json.loads(d["profile_json"] or "{}") or {}).get("import") or {}
        if not rep.get("duplikát_distribúcie"):
            out.append(d)
    return out


def inputs_fingerprint(datasets) -> str:
    """Odtlačok vstupov návrhu: tabuľky, verzie súborov a verzia parsera – nový návrh až pri zmene."""
    parts = sorted(f"{d['table_name']}:{d['sha256'] or ''}" for d in datasets) + [PARSER_VERSION]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _tables(datasets) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for d in datasets:
        out[d["table_name"]] = d
        long = ((json.loads(d["profile_json"] or "{}") or {}).get("dlhý_tvar") or {}).get("tabuľka")
        if long:
            out[long] = d
    return out


def _columns_in(expression: str) -> list[str]:
    quoted = re.findall(r'"([^"]+)"', expression or "")
    return quoted or re.findall(r"[A-Za-z_À-ſ][\wÀ-ſ]*", expression or "")


def concept_problems(field_text: str, expression: str, meaning: str = "") -> list[str]:
    """Pojmová poistka: pojem z poľa musí byť v stĺpci alebo výraze, nie iba v opise od modelu."""
    hay_field = fold(field_text)
    hay_col = fold(" ".join(_columns_in(expression)))
    out = []
    for name, trigger, support in CONCEPTS:
        if re.search(trigger, hay_field) and not re.search(support, hay_col):
            out.append(f"pojem „{name}“ z poľa nie je v stĺpci {expression} – podobnosť slov nestačí")
    return out


def verify_binding(cfg: Config, db: DB, hid: int, field, out: FieldBindingOut, datasets) -> tuple[str, dict[str, Any]]:
    """Kontroly kódom; vráti (code_ok | code_failed, kontroly). Čísla aj vzorku počíta DuckDB (iba na čítanie)."""
    from .exp_ops import check_expr

    tables = _tables(datasets)
    checks: dict[str, Any] = {"problémy": []}
    problems = checks["problémy"]
    table = (out.table or "").strip()
    if table not in tables:
        problems.append(f"tabuľka {table or '?'} nie je stiahnutý dataset v stave ok")
        return "code_failed", checks
    try:
        expr = check_expr(out.expression)
    except DatasetError as exc:
        problems.append(str(exc))
        return "code_failed", checks
    field_text = f"{field['name']} {field['description'] or ''}"
    problems += concept_problems(field_text, expr, out.meaning)
    try:
        total, nonnull = run_query(cfg, hid, f"SELECT COUNT(*), COUNT({expr}) FROM {table}")[0]
        sample = [r[0] for r in run_query(cfg, hid, f"SELECT DISTINCT {expr} FROM {table} WHERE {expr} IS NOT NULL LIMIT 5")]
    except DatasetError as exc:
        problems.append(f"výraz sa nedá vyhodnotiť: {str(exc)[:160]}")
        return "code_failed", checks
    coverage = (nonnull / total) if total else 0.0
    checks.update(riadky=total, pokrytie=round(coverage, 3), vzorka=[str(v)[:60] for v in sample])
    if coverage < cfg.gate1.binding_min_coverage:
        problems.append(f"pokrytie {coverage:.0%} je pod {cfg.gate1.binding_min_coverage:.0%}")
    kind = ("dátum" if DATE_FIELD_RE.search(fold(field_text)) else "ičo" if ICO_FIELD_RE.search(fold(field_text))
            else "číslo" if NUMBER_FIELD_RE.search(fold(field_text)) else None)
    if kind and nonnull:
        test = {"dátum": f"COALESCE(TRY_CAST({expr} AS DATE), TRY_CAST(TRY_STRPTIME(CAST({expr} AS VARCHAR), '%d.%m.%Y') AS DATE))",
                "číslo": f"TRY_CAST({expr} AS DOUBLE)",
                "ičo": f"CASE WHEN regexp_matches(CAST({expr} AS VARCHAR), '^\\s*\\d{{6,8}}\\s*$') THEN 1 END"}[kind]
        try:
            ok = run_query(cfg, hid, f"SELECT COUNT({test}) FROM (SELECT * FROM {table} WHERE {expr} IS NOT NULL LIMIT 20000)")[0][0]
            checked = min(nonnull, 20000)
            share = ok / checked if checked else 0.0
            checks["typ"] = {"očakávaný": kind, "zhoda": round(share, 3)}
            if share < 0.8:
                problems.append(f"hodnoty nie sú {kind} ({share:.0%} zhoda)")
        except DatasetError as exc:
            problems.append(f"kontrola typu zlyhala: {str(exc)[:120]}")
    ds = tables[table]
    prof = json.loads(ds["profile_json"] or "{}") or {}
    if prof.get("time_span"):
        checks["časové_pokrytie"] = prof["time_span"]
    checks["jednotka_v_dátach"] = ((prof.get("import") or {}).get("jednotky") or {})
    return ("code_failed" if problems else "code_ok"), checks


def binding_messages(cfg: Config, hyp, field, schema: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": FIELD_BINDING_SYSTEM.format(lang=cfg.pipeline.output_language)},
            {"role": "user", "content": f"PROBLEM: {hyp['title']}\nFIELD: {field['name']}{' [KEY]' if field['is_key'] else ''}"
                                        f"\nFIELD DESCRIPTION: {field['description'] or ''}\n\n<untrusted_document>\n"
                                        f"{schema[:9000]}\n</untrusted_document>"}]


def propose_bindings(cfg: Config, db: DB, llm: Any, hyp, deadline: Deadline) -> Counter:
    """Návrh väzieb pre polia hypotézy (kľúčové najprv), ktoré pre aktuálne dáta ešte návrh nemajú."""
    stats: Counter = Counter()
    hid = hyp["id"]
    datasets = usable_datasets(db, hid)
    if not datasets:
        return stats
    fp = inputs_fingerprint(datasets)
    tables = [d["table_name"] for d in datasets]
    refresh_stale(cfg, db, hid, datasets, fp)
    schema = None
    router = None
    n = 0
    for f in db.fields_for(hid):
        if f["status"] in ("requested", "partner") or deadline.expired():
            continue
        current = [b for b in db.bindings_for_field(f["id"]) if b["inputs_fingerprint"] == fp]
        if current:
            continue  # pre tieto dáta už návrh je (opakuje sa až pri novom datasete alebo verzii parsera)
        if n >= cfg.gate1.max_bindings_per_run:
            stats["later"] += 1
            continue
        schema = schema or schema_text(cfg, hid, tables)
        router = router or as_router(llm, cfg, db)
        res = router.run_task("field_binding", binding_messages(cfg, hyp, f, schema), FieldBindingOut,
                              object_type="field", object_id=f["id"], max_tokens=900)
        n += 1
        if not res.ok:
            stats["llm_failures"] += 1
            continue
        out = res.value
        common = {"proposed_by": f"model:{res.model}" if getattr(res, "model", None) else "model",
                  "inputs_fingerprint": fp, "unit": out.unit[:40], "entity_kind": out.entity_kind[:60],
                  "grain": out.grain[:80], "meaning": out.meaning[:300], "evidence": out.evidence[:300]}
        if not out.in_data:
            db.add_binding(f["id"], hid, "not_in_data", note=(out.missing or "model: v dátach nie je")[:300], **common)
            stats["not_in_data"] += 1
            continue
        status, checks = verify_binding(cfg, db, hid, f, out, datasets)
        ds = _tables(datasets).get(out.table.strip())
        db.add_binding(f["id"], hid, status, dataset_id=ds["id"] if ds is not None else None,
                       dataset_sha=ds["sha256"] if ds is not None else None, table_name=out.table.strip()[:40],
                       expression=out.expression.strip()[:300], checks_json=checks, **common)
        stats[status] += 1
    return stats


def refresh_stale(cfg: Config, db: DB, hid: int, datasets, fp: str) -> int:
    """Väzba na staršiu verziu datasetu: kód ju overí znova na nových dátach. Potvrdenie človeka sa prenesie, iba keď
    kontroly prejdú (rovnaký stĺpec a význam); inak „stale“ a pole čaká na nový návrh a potvrdenie."""
    tables = _tables(datasets)
    n = 0
    for b in db.bindings_for(hid):
        if b["validation_status"] not in GOOD or b["inputs_fingerprint"] == fp:
            continue
        ds = tables.get(b["table_name"])
        if ds is not None and (ds["sha256"] or "") == (b["dataset_sha"] or ""):
            db.update_binding(b["id"], inputs_fingerprint=fp)  # tá istá verzia tabuľky, pribudol iný dataset
            continue
        field = db.get_field(b["field_id"])
        out = FieldBindingOut(in_data=True, table=b["table_name"] or "", expression=b["expression"] or "",
                              meaning=b["meaning"] or "")
        status, checks = verify_binding(cfg, db, hid, field, out, datasets) if ds is not None else ("code_failed", {
            "problémy": ["dataset väzby už nie je v stave ok"]})
        db.update_binding(b["id"], validation_status="stale")
        new_status = b["validation_status"] if status == "code_ok" else "code_failed"
        db.add_binding(b["field_id"], hid, new_status, dataset_id=ds["id"] if ds is not None else None,
                       dataset_sha=ds["sha256"] if ds is not None else None, table_name=b["table_name"],
                       expression=b["expression"], unit=b["unit"], entity_kind=b["entity_kind"], grain=b["grain"],
                       meaning=b["meaning"], evidence=b["evidence"], checks_json=checks, proposed_by=b["proposed_by"],
                       confirmed_by=b["confirmed_by"] if new_status == "confirmed" else None, inputs_fingerprint=fp,
                       note="znova overené na novej verzii dát")
        n += 1
    return n


def run_bindings(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None) -> dict:
    from .gate1 import _hypotheses, _update_field_statuses

    stats: Counter = Counter()
    for hyp in _hypotheses(cfg, db, ids):
        if deadline.expired():
            break
        stats.update(propose_bindings(cfg, db, llm, hyp, deadline))
        _update_field_statuses(db, hyp["id"])
    return dict(stats)


# ======================================================================= človek
def confirm_binding(db: DB, binding_id: int, by: str = "človek") -> None:
    """Človek potvrdil význam stĺpca (gold label field_binding = confirmed)."""
    from .gate1 import _update_field_statuses

    b = db.get_binding(binding_id)
    if b is None or b["validation_status"] not in ("code_ok", "confirmed"):
        raise ValueError("Potvrdiť sa dá iba väzba, ktorú overil kód (stĺpec existuje, pokrytie a typ sedia).")
    db.update_binding(binding_id, validation_status="confirmed", confirmed_by=by, validated_at=utcnow())
    db.add_label("field_binding", "binding", binding_id, "confirmed", note=f"{b['table_name']}.{b['expression']}")
    _update_field_statuses(db, b["hypothesis_id"])


def reject_binding(db: DB, binding_id: int, note: str = "") -> None:
    from .gate1 import _update_field_statuses

    b = db.get_binding(binding_id)
    if b is None:
        raise ValueError("Väzba neexistuje.")
    db.update_binding(binding_id, validation_status="rejected", confirmed_by="človek", validated_at=utcnow(),
                      note=(note or b["note"] or "")[:300] or None)
    db.add_label("field_binding", "binding", binding_id, "rejected", note=note or None)
    _update_field_statuses(db, b["hypothesis_id"])


def manual_binding(cfg: Config, db: DB, hid: int, field_id: int, table: str, expression: str, unit: str = "",
                   meaning: str = "") -> int:
    """Väzbu zadal človek (stĺpec, ktorý pozná); kód ju overí rovnako – po kontrolách je rovno potvrdená."""
    from .gate1 import _update_field_statuses

    field = db.get_field(field_id)
    datasets = usable_datasets(db, hid)
    out = FieldBindingOut(in_data=True, table=table, expression=expression, unit=unit, meaning=meaning)
    status, checks = verify_binding(cfg, db, hid, field, out, datasets)
    ds = _tables(datasets).get(table)
    bid = db.add_binding(field_id, hid, "confirmed" if status == "code_ok" else "code_failed",
                         dataset_id=ds["id"] if ds is not None else None, dataset_sha=ds["sha256"] if ds is not None else None,
                         table_name=table, expression=expression, unit=unit, meaning=meaning, checks_json=checks,
                         proposed_by="človek", confirmed_by="človek" if status == "code_ok" else None,
                         inputs_fingerprint=inputs_fingerprint(datasets))
    _update_field_statuses(db, hid)
    if status != "code_ok":
        raise ValueError("Kontrola kódom neprešla: " + "; ".join(checks.get("problémy") or []))
    return bid


# ======================================================================= podmienky prechodu
def current_binding(db: DB, field_id: int, datasets) -> Any | None:
    """Najlepšia platná väzba poľa na aktuálnu verziu datasetu: potvrdená pred overenou kódom."""
    tables = _tables(datasets)
    best = None
    for b in db.bindings_for_field(field_id):
        ds = tables.get(b["table_name"] or "")
        if b["validation_status"] not in GOOD or ds is None or (ds["sha256"] or "") != (b["dataset_sha"] or ""):
            continue
        if best is None or (b["validation_status"] == "confirmed" and best["validation_status"] != "confirmed"):
            best = b
    return best


def key_conditions(db: DB, hid: int) -> list[str]:
    """Nesplnené podmienky prechodu Bránou 1: každé kľúčové pole musí mať stĺpec potvrdený človekom na aktuálnej
    verzii datasetu „ok“. Vysoké celkové skóre chýbajúce kľúčové pole nenahradí (audit, H18 a H58)."""
    datasets = usable_datasets(db, hid)
    out = []
    for f in db.fields_for(hid):
        if not f["is_key"]:
            continue
        b = current_binding(db, f["id"], datasets)
        if b is not None and b["validation_status"] == "confirmed":
            continue
        name = f["name"]
        if b is not None:
            out.append(f"„{name}“: potvrď stĺpec {b['table_name']}.{b['expression']} (Brána 1 → Mapovanie polí)")
        elif f["status"] == "partner":
            out.append(f"„{name}“: interné dáta – vyžiadať od partnera (vzorka)")
        elif f["status"] == "schema_only":
            out.append(f"„{name}“: iba opis schémy, bez nameraných hodnôt")
        elif f["status"] == "requested":
            out.append(f"„{name}“: vyžiadané podľa 211/2000 – čaká na odpoveď")
        else:
            out.append(f"„{name}“: v dátach bez overeného stĺpca")
    return out


def data_fingerprint(db: DB, hid: int) -> str:
    """Verzia dát hypotézy pre rozhodnutie brány: polia (kľúčové), platné väzby a verzie datasetov."""
    fields = sorted(f"{f['name']}:{f['is_key']}" for f in db.fields_for(hid))
    datasets = usable_datasets(db, hid)
    bindings = sorted(f"{b['field_id']}:{b['table_name']}:{b['expression']}:{b['validation_status']}:{b['dataset_sha']}"
                      for b in db.bindings_for(hid) if b["validation_status"] in GOOD)
    ds = sorted(f"{d['table_name']}:{d['sha256'] or ''}" for d in datasets)
    return hashlib.sha1(json.dumps([fields, bindings, ds]).encode()).hexdigest()[:16]


def field_route(db: DB, f, datasets_review: bool) -> str:
    """Ďalší krok pre pole podľa toho, čo chýba (smerovanie: katalóg/API, parser, mapovanie, partner, rozhovor)."""
    status = f["status"]
    if status == "partner":
        return "vyžiadať od partnera (Brána 0 → Koho osloviť: držiteľ dát)"
    if status == "requested":
        return "počkať na odpoveď (211/2000)"
    if datasets_review and status in ("available", "loaded", "partial"):
        return "skontrolovať import (dataset na kontrole)"
    if status == "loaded":
        return "mapovanie na stĺpec (model navrhne, ty potvrdíš)"
    if status == "found":
        return "potvrdiť stĺpec" if f["is_key"] else "hotovo"
    if status == "available":
        return "stiahnuť dataset"
    if status == "schema_only":
        return "získať vzorku dát (schéma nestačí na meranie)"
    if status == "missing":
        return "partner alebo žiadosť 211/2000; overiť v rozhovore, či dáta existujú"
    return "hľadať v katalógu a API"
