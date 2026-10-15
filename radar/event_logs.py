"""Event log ako podporovaný typ žiadosti (dodatok 16. 10. 2026, časti 6–7).

Event log je história udalostí procesu: jeden riadok = udalosť konkrétneho prípadu (nie aktuálny stav celého prípadu).
Hodí sa najmä pre hypotézy o priebehu, opakovaní krokov, odovzdávaní a dĺžke procesov; pri hypotéze o obsahu
dokumentov, cenách alebo kvalite údajov je vhodnejšia iná vzorka – researcher vysvetlí, prečo žiada práve event log.

Žiadosť je dvojstupňová: (1) dátový slovník a malá pseudonymizovaná ukážka histórie niekoľkých celých prípadov
(20–50 riadkov je príklad, nie minimum zaručujúce analýzu), (2) po dohode väčší export pre obdobie alebo kohortu.
Text žiadosti je pevná šablóna (nie model) s podmienkami bezplatného dema a skúšky pre komerčného adresáta; verejnému
správcovi a expertovi sa protihodnota prispôsobí. Žiadosť nesľubuje „anonymné“ dáta len preto, že chýba meno.
Ak event log neexistuje, ponúkne sa alternatíva s vlastným typom dát (snapshot sa nikdy nespracuje ako história).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .db import DB
from .privacy import fold

MIN_FIELDS = [
    {"pole": "case_id", "požiadavka": "povinné", "význam": "stabilné pseudonymizované ID dohodnutého prípadu "
     "(objednávka, doklad, zákazka)"},
    {"pole": "activity", "požiadavka": "povinné", "význam": "udalosť alebo vykonaný krok, napr. „objednávka prijatá“"},
    {"pole": "timestamp", "požiadavka": "povinné pre časovú analýzu", "význam": "čas vzniku udalosti s offsetom alebo "
     "dokumentovanou zónou – nie čas exportu, zápisu ani synchronizácie"},
    {"pole": "event_id", "požiadavka": "odporúčané", "význam": "stabilné ID udalosti na kontrolu duplicít"},
    {"pole": "lifecycle", "požiadavka": "voliteľné", "význam": "start/complete, ak systém eviduje začiatok aj koniec"},
    {"pole": "activity_instance_id", "požiadavka": "voliteľné", "význam": "párovanie začiatku a konca tej istej aktivity"},
    {"pole": "role", "požiadavka": "voliteľné", "význam": "rola alebo tím – nie meno pracovníka"},
    {"pole": "sequence", "požiadavka": "voliteľné", "význam": "poradie udalostí s rovnakým časom, ak ho určuje zdroj"},
    {"pole": "source_system", "požiadavka": "voliteľné", "význam": "pôvod udalosti pri spájaní systémov"},
]
DUE_FIELD = {"pole": "due_at / planned_at", "požiadavka": "podľa hypotézy", "význam": "termín s definíciou (pôvodný "
             "prísľub alebo zmenený plán) a prípadne história jeho zmien"}
ACCOMPANYING = ["stručný dátový slovník (význam polí a hodnôt)", "definícia prípadu", "výberové obdobie",
                "spôsob výberu prípadov (všetky / iba dokončené / iba tím…)", "informácia o otvorených prípadoch",
                "význam časov (vznik udalosti vs. zápis, synchronizácia, export) a časová zóna"]
ALTERNATIVES = {"walkthrough": "prehliadka posledného konkrétneho prípadu počas rozhovoru",
                "state_schema": "opis stavov a schéma systému (aktuálny stav, nie história)",
                "aggregate_report": "existujúci agregovaný report",
                "manual_logging": "dohodnuté krátke manuálne zaznamenávanie krokov"}
REQUEST_STATUS = {"draft": "návrh", "sent": "odoslané", "received": "prijaté", "not_available": "event log nemajú",
                  "alternative": "dohodnutá alternatíva", "declined": "odmietnuté"}
FIT_RE = re.compile(r"priebeh|proces|krok|mešk|oneskor|termín|lehot|opakov|schvaľ|odovzd|čakan|dĺžk|trvan|vybav|"
                    r"workflow|urgen|eskal|reklamác|objednáv|dodáv", re.I)
DUE_RE = re.compile(r"termín|mešk|oneskor|lehot|plán|prísľub|due", re.I)
FIRST_STEP = ("dátový slovník a malá pseudonymizovaná ukážka – napríklad 20–50 riadkov histórie niekoľkých celých "
              "prípadov (nie náhodné izolované udalosti); počet je príklad na overenie formátu a významu, nie minimum, "
              "ktoré zaručí analýzu")
SECOND_STEP = ("po dohode väčší export pre konkrétne obdobie alebo kohortu; veľkosť podľa frekvencie, variability "
               "a cieľa analýzy – malá schémová vzorka nie je reprezentatívna na odhad prínosu")
DEFAULT_OUTPUT = "prehľad zaznamenaných priebehov, opakujúcich sa krokov a časových odstupov na ďalšie preverenie"


class EventLogError(ValueError):
    pass


def event_log_fit(db: DB, hid: int) -> tuple[bool, str]:
    """Je event log vhodný typ dát pre hypotézu? (Vysvetlenie, prečo žiadame práve históriu udalostí.)"""
    from .value import domain_of

    h = db.get_hypothesis(hid)
    text = f"{h['title']} {h['description'] or ''}" if h is not None else ""
    latest = db.latest_brief(domain_of(db, hid)[0])
    if latest is not None:
        b = json.loads(latest["brief_json"] or "{}")
        text += " " + " ".join(s.get("text", "") for s in b.get("process_steps") or [])
    hits = sorted({m.group(0).lower() for m in FIT_RE.finditer(text)})
    if hits:
        return True, ("hypotéza je o priebehu procesu (" + ", ".join(hits[:4]) + "): história udalostí ukáže "
                      "zaznamenané priebehy, opakovania krokov, odovzdávanie a časové odstupy")
    return False, ("hypotéza nie je zjavne o priebehu procesu – vhodnejšia môže byť iná vzorka (ukážka dokumentov, "
                   "cenník, záznamy o kvalite údajov); event log žiadaj iba s vlastným zdôvodnením")


def _org(db: DB, org_id: int | None):
    return db.get_organization(org_id) if org_id else None


def request_text(row, org, commercial: bool, policy: str | None) -> tuple[str, str]:
    """Pevná šablóna žiadosti (bez modelu). Vráti (jadro správy, predmet)."""
    case = row["case_definition"] or "prípade"
    p1 = (f"Ak váš systém uchováva históriu krokov pri {case}, na prvé overenie by postačil opis polí a malá "
          "pseudonymizovaná ukážka s identifikátorom prípadu, názvom udalosti a časom jej vzniku. Mená pracovníkov "
          "ani obsah komunikácie na tento prvý rozbor nepotrebujem.")
    if row["step"] == 2:
        p1 = (f"Ďakujem za ukážku histórie pri {case}. Ak by to bolo pre vás užitočné, ďalším krokom by bol väčší "
              "pseudonymizovaný export za dohodnuté obdobie alebo skupinu prípadov – rozsah by sme určili podľa toho, "
              "ako často sa prípady vyskytujú a čo má výstup ukázať.")
    output = row["intended_output"] or DEFAULT_OUTPUT
    p2 = (f"Z použiteľnej histórie viem pripraviť {output}. Najprv by sme overili význam údajov a to, či výstup "
          "pomáha pri vašom rozhodovaní.")
    if not commercial:
        p2 += " Na oplátku pošlem stručný prehľad zistení a nejasností v údajoch."
    q = "Viete takúto históriu z vášho systému získať, prípadne ma nasmerovať na jej správcu?"
    if row["step"] == 2:
        q = "Bolo by pre vás takéto rozšírenie zaujímavé?"
    subject = f"Ukážka histórie krokov – {case}" if row["step"] != 2 else f"Väčší export histórie – {case}"
    return "\n\n".join([p1, p2, q]), subject[:70]


def build_request(cfg: Config, db: DB, hid: int, org_id: int | None, case_definition: str = "",
                  outreach_id: int | None = None, step: int = 1, why: str = "", process_scope: str = "") -> int:
    """Žiadosť o event log s účelom, definíciou prípadu, minimálnou schémou, malým prvým krokom, prínosom
    a neznámymi. Pri nevhodnej hypotéze iba s vlastným zdôvodnením (`why`)."""
    from .domain import BLOCKS
    from .value import best_proposal, domain_of

    fit, fit_why = event_log_fit(db, hid)
    if not fit and not why.strip():
        raise EventLogError(fit_why)
    h = db.get_hypothesis(hid)
    fields = list(MIN_FIELDS)
    if DUE_RE.search(f"{h['title']} {h['description'] or ''}"):
        fields.append(DUE_FIELD)
    proposal = best_proposal(db, hid, "customer")
    p = json.loads(proposal["proposal_json"]) if proposal is not None else {}
    output = p.get("deliverable") or DEFAULT_OUTPUT
    key = domain_of(db, hid)[0]
    unknowns = ["či systém uchováva históriu krokov (nie iba aktuálny stav)", "časová zóna a presnosť časov",
                "otvorené a odrezané prípady vo výbere"]
    unknowns += [f"{q['question']} (blokuje: {BLOCKS.get(q['blocks_action'], q['blocks_action'])})"
                 for q in db.domain_questions(key, "open") if q["blocks_action"] in ("data_request", "duration_analysis")]
    return db.add_eventlog_request(
        hypothesis_id=hid, organization_id=org_id, outreach_id=outreach_id, step=step,
        process_scope=process_scope or h["title"], case_definition=case_definition.strip() or "dohodnutom prípade "
        "(napr. objednávke, doklade alebo zákazke)", fields_json=fields + [{"pole": "sprievodné informácie",
                                                                           "požiadavka": "k exportu",
                                                                           "význam": "; ".join(ACCOMPANYING)}],
        sample_scope=FIRST_STEP if step == 1 else SECOND_STEP, intended_output=output,
        why_event_log=why.strip() or fit_why, unknowns_json=unknowns, status="draft")


def request_markdown(row) -> str:
    fields = json.loads(row["fields_json"] or "[]")
    lines = [f"# Žiadosť o event log (krok {row['step']}) – {row['org_name'] or 'organizácia'}", "",
             f"**Účel:** {row['why_event_log']}", f"**Rozsah procesu:** {row['process_scope']}",
             f"**Definícia prípadu:** {row['case_definition']}", f"**Prvý krok / rozsah ukážky:** {row['sample_scope']}",
             f"**Zamýšľaný výstup:** {row['intended_output']}", "", "| pole | požiadavka | význam |", "|---|---|---|"]
    lines += [f"| {f['pole']} | {f['požiadavka']} | {f['význam']} |" for f in fields]
    lines += ["", "**Čo ešte nevieme:**"] + [f"- {u}" for u in json.loads(row["unknowns_json"] or "[]")]
    lines += ["", "Pri importe sa nič nemaže potichu: problémové riadky sa označia a report kvality povie, ktoré "
              "analýzy dáta podporia. Mená pracovníkov ani obsah komunikácie sa nežiadajú."]
    return "\n".join(lines) + "\n"


def draft_request_message(cfg: Config, db: DB, request_id: int) -> int:
    """Návrh správy so žiadosťou (oslovenie v režime data_request) pre kontakt pôvodného oslovenia alebo funkčný kontakt
    organizácie. Systém nič neodosiela – návrh ide do Oslovení na kontrolu, schválenie a ručné odoslanie."""
    from .outreach import check_draft, compose, need_confirmed, policy_text, readiness_of
    from .value import policy_version

    row = db.get_eventlog_request(request_id)
    if row is None:
        raise EventLogError("Žiadosť neexistuje.")
    orig = db.get_outreach(row["outreach_id"]) if row["outreach_id"] else None
    org = _org(db, row["organization_id"] or (orig["org_id"] if orig is not None else None))
    person = db.get_person(orig["person_id"]) if orig is not None and orig["person_id"] else None
    contact = db.get_contact(orig["contact_point_id"]) if orig is not None and orig["contact_point_id"] else None
    if contact is None and org is not None:
        emails = [c for c in db.org_contacts(org["id"]) if c["kind"] == "email"]
        contact = emails[0] if emails else None
    if contact is None:
        raise EventLogError("Chýba kontakt: žiadosť patrí k osloveniu alebo k organizácii so zverejneným e-mailom.")
    commercial = org is None or (org["sector_kind"] or "") != "public"
    policy = policy_text(cfg, "data_request", True, commercial)
    core, subject_core = request_text(row, org, commercial, policy)
    source_url = contact["source_url"] or ""
    subject, body = compose(cfg, "email", person, org["sector_kind"] if org is not None else None, core, subject_core,
                            source_url, contact["value"], "data_request", policy)
    orig_pkg = json.loads(orig["package_json"] or "{}") if orig is not None and orig["package_json"] else {}
    confirmed = need_confirmed(db, row["hypothesis_id"], person["id"] if person else None, org["id"] if org else None)
    package = {
        "režim": "data_request", "typ": orig_pkg.get("typ") or "customer",
        "prečo_tento_kontakt": orig_pkg.get("prečo_tento_kontakt") or {
            "text": "funkčný kontakt organizácie", "dôkaz": "", "zdroj": source_url, "úryvok": contact["source_snippet"] or ""},
        "potreba": {"text": row["process_scope"], "stav": "confirmed" if confirmed else "hypothesis", "podklad": []},
        "výstup": row["intended_output"], "rozhodnutie": orig_pkg.get("rozhodnutie") or "",
        "minimálny_vstup": row["sample_scope"], "čo_nevieme": "; ".join(json.loads(row["unknowns_json"] or "[]")),
        "záväzok": None, "návrh_prínosu": orig_pkg.get("návrh_prínosu"), "stav_návrhu_prínosu": orig_pkg.get(
            "stav_návrhu_prínosu") or "šablóna", "prehľad_domény": orig_pkg.get("prehľad_domény"),
        "politika": policy_version(cfg), "veta_o_podmienkach": policy, "žiadosť_event_log": request_id,
        "hlavná_otázka": {"id": None, "text": core.split("\n\n")[-1], "blokuje": "žiadosť o dáta"},
    }
    checks = check_draft(cfg, "email", subject, body, core, [], source_url, "data_request", policy,
                         f"{row['sample_scope']} {row['intended_output']}", package["potreba"]["stav"])
    checks.update(core=core, subject_core=subject_core, template="event_log_request", generic=person is None,
                  contact_kind=contact["kind"])
    readiness, reasons = readiness_of(package, checks, subject, body)
    oid = db.insert_outreach(person["id"] if person else None, row["hypothesis_id"], "email", "B", subject, body, [],
                             checks, "draft" if checks["ok"] else "needs_edit", contact["id"],
                             org_id=org["id"] if org is not None else None, mode="data_request",
                             proposal_id=package["návrh_prínosu"], package={**package, "pripravenosť": reasons},
                             readiness=readiness)
    db.update_eventlog_request(request_id, outreach_id=oid, text=body)
    return oid


def set_request_status(db: DB, request_id: int, status: str, alternative: str | None = None) -> None:
    """Stav žiadosti; „event log nemajú“ → alternatíva s vlastným typom dát (snapshot sa nespracuje ako história)."""
    if status not in REQUEST_STATUS:
        raise EventLogError(f"Neznámy stav {status!r}.")
    if alternative is not None and alternative not in ALTERNATIVES:
        raise EventLogError(f"Neznáma alternatíva {alternative!r}.")
    fields: dict[str, Any] = {"status": status}
    if alternative:
        fields.update(alternative=alternative, status="alternative")
    db.update_eventlog_request(request_id, **fields)


def next_step_without_log(row) -> list[str]:
    """Ďalšie kroky, keď event log neexistuje (dodatok 6.3)."""
    return [f"{label}" for label in ALTERNATIVES.values()]


# ======================================================================= import a kvalita (časť 7)
TARGETS = ("case_id", "activity", "timestamp", "event_id", "lifecycle", "activity_instance_id", "role", "sequence",
           "source_system", "due_at")
REQUIRED = ("case_id", "activity", "timestamp")
SYNONYMS = {
    "case_id": ["case_id", "caseid", "case", "case:concept:name", "pripad", "id_pripadu", "cislo_pripadu", "order_id",
                "objednavka", "cislo_objednavky", "ticket", "ticket_id", "doklad", "document_id", "zakazka"],
    "activity": ["activity", "concept:name", "aktivita", "udalost", "event", "event_name", "krok", "step", "action",
                 "akcia"],
    "timestamp": ["timestamp", "time:timestamp", "cas", "cas_udalosti", "event_time", "datetime", "datum_cas", "time",
                  "date", "datum"],
    "event_id": ["event_id", "eventid", "id_udalosti"],
    "lifecycle": ["lifecycle", "lifecycle:transition", "transition"],
    "activity_instance_id": ["activity_instance_id", "instance_id", "instance", "concept:instance"],
    "role": ["role", "org:role", "team", "tim", "oddelenie", "group"],
    "sequence": ["sequence", "seq", "poradie", "order"],
    "source_system": ["source_system", "system", "source", "zdroj"],
    "due_at": ["due_at", "due", "deadline", "termin", "planned_at", "plan", "datum_dodania"],
}
NOT_EVENT_TIME_RE = re.compile(r"export|sync|load|ingest|import|modif|updat|zmenen|vytvoren|created|insert|zapis", re.I)
PERSONAL_TOKENS = {"resource", "org:resource", "user", "username", "pouzivatel", "employee", "zamestnanec", "pracovnik",
                   "meno", "name", "email", "mail", "telefon", "phone", "osoba", "person", "owner", "assignee", "riesitel"}
DT_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]
START_WORDS = {"start", "begin", "zaciatok", "start_event"}
COMPLETE_WORDS = {"complete", "end", "koniec", "finish", "done"}
METRICS = {"počty": "počty prípadov a udalostí", "frekvencie_aktivít": "frekvencie aktivít",
           "varianty_a_nadväznosti": "varianty priebehu a priamo nadväzujúce kroky", "odstupy": "odstupy medzi udalosťami",
           "trvanie_prípadu": "trvanie od začiatku po koniec", "opakovanie_kroku": "opakovanie kroku",
           "čas_aktivity": "čas výkonu aktivity (start/complete)", "odchýlka_od_termínu": "odchýlka od termínu",
           "pracovný_čas": "pracovný čas (kalendár)"}
PARSER_VERSION = "eventlog-1"


def store_dir(cfg: Config) -> Path:
    base = Path(cfg.event_logs.dir)
    if not base.is_absolute():
        base = Path(cfg.db_path).resolve().parent / base.name
    base.mkdir(parents=True, exist_ok=True)
    return base


def org_store(cfg: Config, org_id: int) -> Path:
    """Oddelený priestor organizácie: vlastný DuckDB súbor (rovnaké case_id v dvoch organizáciách sa nikdy nezlúči)."""
    return store_dir(cfg) / f"org_{int(org_id)}.duckdb"


def _connect(cfg: Config, org_id: int, read_only: bool = False):
    import duckdb

    path = org_store(cfg, org_id)
    if read_only and not path.exists():
        raise EventLogError("Organizácia nemá importované dáta.")
    return duckdb.connect(str(path), read_only=read_only)


def _col_key(name: str) -> str:
    return fold(name).replace(" ", "_")


def propose_mapping(columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Návrh mapovania stĺpcov podľa názvov (deterministicky); potvrdí alebo upraví človek. Stĺpec, ktorý vyzerá ako
    čas zápisu/exportu, sa ako čas udalosti nenavrhne; stĺpec s menami pracovníkov sa nepoužije."""
    notes: list[str] = []
    mapping: dict[str, str] = {}
    used: set[str] = set()
    keys = {c: _col_key(c) for c in columns}
    personal = {c for c, key in keys.items() if key in PERSONAL_TOKENS or set(re.split(r"[_:]", key)) & PERSONAL_TOKENS}
    for target in TARGETS:
        best, score = None, 0
        for col, key in keys.items():
            if col in used or (col in personal and target != "activity"):
                continue
            for syn in SYNONYMS[target]:
                s = 3 if key == syn else (2 if syn in key.split("_") or key.endswith(syn) else 0)
                if target == "timestamp" and s and NOT_EVENT_TIME_RE.search(key):
                    notes.append(f"stĺpec „{col}“ vyzerá ako čas zápisu, zmeny alebo exportu – nenavrhnutý ako čas udalosti")
                    s = 0
                if s > score:
                    best, score = col, s
        if best is not None:
            mapping[target] = best
            used.add(best)
    for col, key in keys.items():
        if col != mapping.get("timestamp") and NOT_EVENT_TIME_RE.search(key) and re.search(
                r"(^|_)(at|ts)$|time|date|cas|datum", key) and not any(col in n for n in notes):
            notes.append(f"stĺpec „{col}“ vyzerá ako čas zápisu, zmeny alebo exportu – nie je čas vzniku udalosti")
        if col not in used and col in personal:
            notes.append(f"stĺpec „{col}“ môže obsahovať mená alebo kontakty – do analýzy sa nepoužije")
    for t in REQUIRED:
        if t not in mapping:
            notes.append(f"chýba stĺpec pre „{t}“ – doplň mapovanie ručne")
    return mapping, notes


def import_file(cfg: Config, db: DB, org_id: int, path: str | Path, hid: int | None = None,
                request_id: int | None = None, file_name: str | None = None) -> tuple[int, bool]:
    """Import CSV/Parquet do priestoru organizácie. Idempotentný: rovnaký súbor (SHA-256) pre rovnakú organizáciu
    vráti existujúci import. Vráti (id importu, či je nový). Pôvodný súbor sa uchová podľa hashu."""
    path = Path(path)
    if path.stat().st_size > cfg.event_logs.max_file_mb * 1024 * 1024:
        raise EventLogError(f"Súbor je väčší ako {cfg.event_logs.max_file_mb} MB (event_logs.max_file_mb).")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = db.eventlog_import_by_hash(org_id, digest)
    if existing is not None:
        return int(existing["id"]), False
    suffix = path.suffix.lower()
    fmt = "parquet" if suffix in (".parquet", ".pq") else "csv" if suffix in (".csv", ".txt", ".tsv") else None
    if fmt is None:
        raise EventLogError("Podporované sú CSV a Parquet (XES zatiaľ nie).")
    iid = db.add_eventlog_import(org_id, digest, request_id=request_id, hypothesis_id=hid,
                                 file_name=file_name or path.name, file_format=fmt, status="uploaded")
    raw_path = path
    if cfg.event_logs.keep_raw:
        raw_dir = store_dir(cfg) / f"org_{int(org_id)}_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{digest}{suffix}"
        if not raw_path.exists():
            shutil.copyfile(path, raw_path)
    con = _connect(cfg, org_id)
    try:
        src = str(raw_path).replace("'", "''")
        if fmt == "csv":
            con.execute(f"CREATE OR REPLACE TABLE raw_{iid} AS SELECT * FROM read_csv('{src}', all_varchar = true, "
                        "header = true)")
        else:
            con.execute(f"CREATE OR REPLACE TABLE raw_{iid} AS SELECT * FROM read_parquet('{src}')")
        cols = [(r[0], r[1]) for r in con.execute(f"DESCRIBE raw_{iid}").fetchall()]
        n = con.execute(f"SELECT COUNT(*) FROM raw_{iid}").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 – nečitateľný súbor: import zlyhal s dôvodom, nič sa nemaže
        db.update_eventlog_import(iid, status="failed", quality_json={"chyba": f"{type(exc).__name__}: {str(exc)[:300]}"})
        raise EventLogError(f"Súbor sa nedá načítať: {type(exc).__name__}") from exc
    finally:
        con.close()
    mapping, notes = propose_mapping([c for c, _ in cols])
    db.update_eventlog_import(iid, columns_json=[{"stĺpec": c, "typ": t} for c, t in cols], mapping_json={
        "návrh": mapping, "poznámky": notes}, rows_total=n,
        transformations_json=[f"načítané ako {fmt} (všetky hodnoty ako text), poradie riadkov zo súboru"])
    return iid, True


# ----------------------------------------------------------------------- čas
def parse_ts(raw: str | None) -> tuple[datetime | None, str | None]:
    """(čas, presnosť day/minute/second/subsecond); bez hádania formátov deň/mesiac s lomkou."""
    s = (raw or "").strip()
    if not s:
        return None, None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in DT_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}/\d{2}/\d{2}", s):
        precision = "day"
    elif dt.microsecond:
        precision = "subsecond"
    elif re.search(r"\d{1,2}:\d{2}:\d{2}", s):
        precision = "second"
    else:
        precision = "minute"
    return dt, precision


def _localize(dt: datetime, zone: ZoneInfo) -> tuple[datetime | None, str]:
    """Naivný miestny čas → UTC; neexistujúci (jarný posun) a nejednoznačný (jesenný posun) čas sa označí."""
    a, b = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
    ua, ub = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    ok_a = ua.astimezone(zone).replace(tzinfo=None) == dt
    ok_b = ub.astimezone(zone).replace(tzinfo=None) == dt
    if not ok_a and not ok_b:
        return None, "nonexistent"  # v medzere jarného posunu (zoneinfo dá rôzne offsety aj tu)
    if ok_a and ok_b and ua != ub:
        return None, "ambiguous"  # hodina sa pri jesennom posune opakuje
    return (ua if ok_a else ub), "ok"


def _dst_window(dt: datetime) -> bool:
    """Naivný čas v hodine prechodu letného času (posledná nedeľa marca a októbra, 2:00–3:00) – pri neznámej zóne."""
    if dt.month not in (3, 10) or dt.weekday() != 6 or dt.day < 25 or not (2 <= dt.hour < 3):
        return False
    return True


# ----------------------------------------------------------------------- kontroly
def confirm_mapping(cfg: Config, db: DB, import_id: int, mapping: dict[str, str], *, timezone_policy: str = "",
                    case_definition: str = "", period: tuple[str, str] | None = None, selection: str = "",
                    end_activities: list[str] | None = None, start_activities: list[str] | None = None,
                    due_definition: str = "", data_kind: str | None = None) -> dict[str, Any]:
    """Potvrdené mapovanie (človek) → normalizované udalosti a report kvality. Nič sa nemaže: problémové riadky sa
    označia, report uvedie počty, podiely, rozhodnutie a dostupnosť jednotlivých metrík."""
    imp = db.get_eventlog_import(import_id)
    if imp is None:
        raise EventLogError("Import neexistuje.")
    cols = {c["stĺpec"] for c in json.loads(imp["columns_json"] or "[]")}
    bad = {t: c for t, c in mapping.items() if c and (t not in TARGETS or c not in cols)}
    if bad:
        raise EventLogError(f"Neplatné mapovanie: {bad}")
    mapping = {t: c for t, c in mapping.items() if c}
    missing = [t for t in ("case_id", "activity") if t not in mapping]
    if missing:
        raise EventLogError("Mapovanie musí mať stĺpec pre: " + ", ".join(missing))
    version = (imp["mapping_version"] or 0) + 1
    report = _check(cfg, db, imp, mapping, timezone_policy or cfg.event_logs.default_timezone, period, selection,
                    end_activities or [], start_activities or [], due_definition, data_kind, version)
    db.update_eventlog_import(
        import_id, mapping_json={"potvrdené": mapping, "návrh": json.loads(imp["mapping_json"] or "{}").get("návrh"),
                                 "poznámky": json.loads(imp["mapping_json"] or "{}").get("poznámky", []),
                                 "koncové_aktivity": end_activities or [], "začiatočné_aktivity": start_activities or [],
                                 "výber": selection, "termín": due_definition},
        mapping_version=version, mapping_confirmed=1, timezone_policy=report["časová_zóna"]["politika"],
        case_definition=case_definition or None, period=json.dumps(report["obdobie"], ensure_ascii=False),
        data_kind=report["druh_dát"], quality_json=report, availability_json=report["dostupnosť"],
        transformations_json=json.loads(imp["transformations_json"] or "[]") + report["transformácie"],
        retained_rows=report["použiteľné_riadky"], quarantined_rows=report["vylúčené_riadky"], status="checked")
    return report


def _check(cfg: Config, db: DB, imp, mapping: dict[str, str], tz_policy: str, period, selection: str,
           end_acts: list[str], start_acts: list[str], due_definition: str, data_kind: str | None,
           version: int) -> dict[str, Any]:
    iid, org_id = imp["id"], imp["organization_id"]
    con = _connect(cfg, org_id)
    try:
        sel = ", ".join(f'CAST("{mapping[t]}" AS VARCHAR)' if t in mapping else "NULL" for t in TARGETS)
        rows = con.execute(f"SELECT rowid + 1, {sel} FROM raw_{iid} ORDER BY rowid").fetchall()
    finally:
        con.close()
    n = len(rows)
    zone = None
    transformations: list[str] = []
    if tz_policy and tz_policy not in ("offset", "unknown"):
        try:
            zone = ZoneInfo(tz_policy)
        except (ZoneInfoNotFoundError, ValueError):
            raise EventLogError(f"Neznáma časová zóna {tz_policy!r} (napr. Europe/Bratislava).")
    ev: list[dict[str, Any]] = []
    precision: Counter = Counter()
    with_offset = without_offset = 0
    dst_naive = 0
    for r in rows:
        row_no, case, act, ts_raw, eid, life, inst, role, seq, src, due = r
        e: dict[str, Any] = {"_row": row_no, "case_id": (case or "").strip() or None, "activity": (act or "").strip() or None,
                             "ts_raw": ts_raw, "event_id": (eid or "").strip() or None,
                             "lifecycle": (life or "").strip().lower() or None, "instance_id": (inst or "").strip() or None,
                             "role": (role or "").strip() or None, "source_system": (src or "").strip() or None,
                             "due_raw": due, "issues": []}
        try:
            e["sequence"] = float(seq) if seq not in (None, "") else None
        except ValueError:
            e["sequence"] = None
            e["issues"].append("poradie nie je číslo")
        if e["case_id"] is None:
            e["issues"].append("chýba ID prípadu")
        if e["activity"] is None:
            e["issues"].append("chýba aktivita")
        dt, prec = parse_ts(ts_raw) if "timestamp" in mapping else (None, None)
        e["precision"] = prec
        if "timestamp" not in mapping:
            e["ts_status"] = "missing"
        elif not (ts_raw or "").strip():
            e["ts_status"] = "missing"
            e["issues"].append("chýba čas")
        elif dt is None:
            e["ts_status"] = "unparsed"
            e["issues"].append("neplatný formát času")
        else:
            precision[prec] += 1
            if dt.tzinfo is not None:
                with_offset += 1
                e["ts"], e["ts_kind"], e["ts_status"] = dt.astimezone(timezone.utc), "utc", "ok"
            else:
                without_offset += 1
                if zone is not None:
                    utc, status = _localize(dt, zone)
                    e["ts"], e["ts_kind"], e["ts_status"] = utc, "utc", status
                    if status == "ambiguous":
                        e["issues"].append("nejednoznačný čas (prechod na zimný čas)")
                    elif status == "nonexistent":
                        e["issues"].append("neexistujúci čas (prechod na letný čas)")
                else:
                    e["ts"], e["ts_kind"], e["ts_status"] = dt, "local", "ok"
                    if _dst_window(dt):
                        dst_naive += 1
        due_dt, _ = parse_ts(due) if "due_at" in mapping else (None, None)
        if due_dt is not None and due_dt.tzinfo is None and zone is not None:
            due_dt, _ = _localize(due_dt, zone)
        elif due_dt is not None and due_dt.tzinfo is not None:
            due_dt = due_dt.astimezone(timezone.utc)
        e["due"] = due_dt
        ev.append(e)
    # zmiešané časy s offsetom a bez neho pri neznámej zóne sa nedajú porovnať
    if with_offset and without_offset and zone is None:
        for e in ev:
            if e.get("ts_kind") == "local":
                e["ts_status"] = "no_tz"
                e["issues"].append("čas bez zóny medzi časmi s offsetom")
        transformations.append("časy bez zóny vylúčené z časových metrík (iné riadky majú offset, zóna neznáma)")
    if with_offset:
        transformations.append("časy s offsetom prevedené na UTC")
    if without_offset and zone is not None:
        transformations.append(f"časy bez zóny prevedené zo zóny {tz_policy} na UTC")
    policy = ("offset v dátach" if with_offset and not without_offset else
              f"zóna {tz_policy}" if zone is not None else "neznáma")
    # podozrenie: čas exportu/synchronizácie namiesto vzniku udalosti
    ts_counter = Counter(e["ts"] for e in ev if e.get("ts_status") == "ok")
    export_suspect = False
    if ts_counter:
        top_ts, top_n = ts_counter.most_common(1)[0]
        cases_top = {e["case_id"] for e in ev if e.get("ts") == top_ts}
        if top_n / max(1, len(ev)) > 0.5 and len(cases_top) > 5:
            export_suspect = True
    ts_name = mapping.get("timestamp", "")
    name_suspect = bool(ts_name and NOT_EVENT_TIME_RE.search(_col_key(ts_name)))
    # duplicity
    dup_eid = 0
    if "event_id" in mapping:
        seen: set[str] = set()
        for e in ev:
            if e["event_id"] is None:
                continue
            if e["event_id"] in seen:
                e["issues"].append("duplicitné event_id")
                e["dup"] = True
                dup_eid += 1
            seen.add(e["event_id"])
    identical = 0
    if "event_id" not in mapping:
        keyc = Counter((e["case_id"], e["activity"], (e["ts_raw"] or "").strip(), e["lifecycle"], e["instance_id"],
                        e["role"]) for e in ev)
        for e in ev:
            k = (e["case_id"], e["activity"], (e["ts_raw"] or "").strip(), e["lifecycle"], e["instance_id"], e["role"])
            if keyc[k] > 1:
                e["issues"].append("identický riadok (ponechaný – môže byť legitímne opakovanie)")
                identical += 1
    for e in ev:
        e["usable_count"] = e["case_id"] is not None and e["activity"] is not None and not e.get("dup")
        e["usable_time"] = e["usable_count"] and e.get("ts_status") == "ok" and not export_suspect
    # nekonzistentné ID prípadu (líšia sa iba veľkosťou písmen či medzerami)
    variants: dict[str, set[str]] = defaultdict(set)
    for e in ev:
        if e["case_id"]:
            variants[re.sub(r"\s+", "", e["case_id"].lower())].add(e["case_id"])
    inconsistent_ids = [sorted(v) for v in variants.values() if len(v) > 1]
    # prípady: poradie, zhodné časy, hranice, odrezanie
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ev:
        if e["usable_count"]:
            by_case[e["case_id"]].append(e)
    has_seq = "sequence" in mapping
    tie_cases = no_time_cases = 0
    cases: list[dict[str, Any]] = []
    all_ts = [e["ts"] for e in ev if e["usable_time"]]
    comparable = all_ts and len({type(t.tzinfo) for t in all_ts}) == 1
    p_start = p_end = None
    if period and period[0] and period[1]:
        p_start, p_end = parse_ts(period[0])[0], parse_ts(period[1])[0]
        if p_start and p_end and all_ts and all_ts[0].tzinfo is not None:
            p_start = p_start.replace(tzinfo=timezone.utc) if p_start.tzinfo is None else p_start
            p_end = p_end.replace(tzinfo=timezone.utc) if p_end.tzinfo is None else p_end
    if all_ts and comparable:
        p_start = p_start or min(all_ts)
        p_end = p_end or max(all_ts)
    window = timedelta(days=cfg.event_logs.truncation_window_days)
    end_set, start_set = set(end_acts), set(start_acts)
    for cid, items in by_case.items():
        timed = all(x["usable_time"] for x in items)
        if not timed:
            no_time_cases += 1
        ordered = timed and comparable
        if ordered:
            keyf = (lambda x: (x["ts"], x["sequence"] if x["sequence"] is not None else float("inf"))) if has_seq else \
                (lambda x: x["ts"])
            items.sort(key=keyf)
            for a, b in zip(items, items[1:]):
                same = a["ts"] == b["ts"] or (a["precision"] == "day" or b["precision"] == "day") and \
                    a["ts"].date() == b["ts"].date()
                if same and not (has_seq and a["sequence"] is not None and b["sequence"] is not None
                                 and a["sequence"] != b["sequence"]):
                    ordered = False
                    break
            if not ordered:
                tie_cases += 1
        first, last = (items[0], items[-1]) if ordered else (None, None)
        c = {"case_id": cid, "n_events": len(items), "ordered": ordered,
             "first_ts": first["ts"] if first else None, "last_ts": last["ts"] if last else None,
             "first_activity": first["activity"] if first else None, "last_activity": last["activity"] if last else None,
             "open": (last["activity"] not in end_set) if (ordered and end_set) else None,
             "left_truncated": None, "right_truncated": None}
        if ordered and p_start is not None and p_end is not None:
            starts_ok = first["activity"] in start_set if start_set else None
            c["left_truncated"] = bool(first["ts"] - p_start <= window) and starts_ok is not True
            c["right_truncated"] = bool(p_end - last["ts"] <= window) and c["open"] is not False
        cases.append(c)
    n_cases = len(cases)
    ordered_cases = sum(1 for c in cases if c["ordered"])
    single = sum(1 for c in cases if c["n_events"] == 1)
    snapshot_like = n_cases >= 10 and single / n_cases >= 0.9
    kind = data_kind or ("snapshot" if snapshot_like else "event_log")
    # štart/complete párovanie
    life_pairs = life_amb = life_no_start = life_no_end = life_neg = 0
    if "lifecycle" in mapping:
        for items in by_case.values():
            seq_items = sorted([x for x in items if x["usable_time"]], key=lambda x: x["ts"])
            open_: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
            for x in seq_items:
                lc = (x["lifecycle"] or "").lower()
                k = (x["activity"], x["instance_id"])
                if lc in START_WORDS:
                    if open_[k] and x["instance_id"] is None:
                        life_amb += 1
                        x["issues"].append("nejednoznačná inštancia (viac otvorených štartov)")
                        open_[k][-1]["issues"].append("nejednoznačná inštancia (viac otvorených štartov)")
                        open_[k][-1]["ambiguous"] = x["ambiguous"] = True
                    open_[k].append(x)
                elif lc in COMPLETE_WORDS:
                    if not open_[k]:
                        life_no_start += 1
                        x["issues"].append("complete bez štartu")
                        continue
                    s = open_[k].pop(0)
                    if s.get("ambiguous") or x.get("ambiguous"):
                        continue
                    if x["ts"] < s["ts"]:
                        life_neg += 1
                        x["issues"].append("záporné trvanie (complete pred štartom)")
                        continue
                    life_pairs += 1
                    x["pair_start_row"] = s["_row"]
            for k, left in open_.items():
                for s in left:
                    life_no_end += 1
                    s["issues"].append("štart bez complete")
    # premenovania a zmiešanie procesov, logovanie v čase
    act_raw = Counter(e["activity"] for e in ev if e["activity"])
    folded: dict[str, set[str]] = defaultdict(set)
    for a in act_raw:
        folded[re.sub(r"\s+", " ", fold(a))].add(a)
    renamed = [sorted(v) for v in folded.values() if len(v) > 1]
    many_unique = len(act_raw) > 20 and len(act_raw) / max(1, sum(act_raw.values())) > 0.3
    systems = sorted({e["source_system"] for e in ev if e["source_system"]})
    appear: list[str] = []
    gaps: list[str] = []
    if all_ts and comparable and p_start is not None and p_end is not None and p_end > p_start:
        span = p_end - p_start
        per_act: dict[str, list[datetime]] = defaultdict(list)
        for e in ev:
            if e["usable_time"]:
                per_act[e["activity"]].append(e["ts"])
        for a, ts in per_act.items():
            if len(ts) >= 5 and (min(ts) - p_start > span * 0.25 or p_end - max(ts) > span * 0.25):
                appear.append(a)
        months = Counter((t.year, t.month) for t in all_ts)
        y, m = p_start.year, p_start.month
        while (y, m) <= (p_end.year, p_end.month):
            if (y, m) not in months:
                gaps.append(f"{y}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    # report
    checks: list[dict[str, Any]] = []

    def add(name: str, count: int, decision: str, unit: str = "riadky", total: int | None = None, examples=None):
        total = n if unit == "riadky" else (total if total is not None else n_cases)
        checks.append({"kontrola": name, "počet": count, "jednotka": unit,
                       "podiel": round(count / total, 4) if total else 0.0, "rozhodnutie": decision,
                       "príklady_riadkov": examples or []})

    def rows_with(text: str) -> list[int]:
        return [e["_row"] for e in ev if any(text in i for i in e["issues"])]

    for label, text, decision in (
            ("chýba ID prípadu", "chýba ID prípadu", "vylúčené zo všetkých metrík prípadov"),
            ("chýba aktivita", "chýba aktivita", "vylúčené z frekvencií, variantov a časových metrík"),
            ("chýba čas", "chýba čas", "ponechané pre početnosti, vylúčené z časových metrík"),
            ("neplatný formát času", "neplatný formát času", "ponechané pre početnosti, vylúčené z časových metrík"),
            ("nejednoznačný čas (prechod na zimný čas)", "nejednoznačný čas", "vylúčené z časových metrík"),
            ("neexistujúci čas (prechod na letný čas)", "neexistujúci čas", "vylúčené z časových metrík"),
            ("čas bez zóny medzi časmi s offsetom", "čas bez zóny", "vylúčené z časových metrík"),
            ("duplicitné event_id", "duplicitné event_id", "ďalší výskyt vylúčený z počtov (prvý ostáva)"),
            ("identické riadky bez event_id", "identický riadok", "iba označené – nezmazané (môže ísť o legitímne opakovanie)"),
            ("nejednoznačná inštancia start/complete", "nejednoznačná inštancia", "nepárované – žiadny vymyslený čas aktivity"),
            ("complete bez štartu", "complete bez štartu", "nepárované"),
            ("štart bez complete", "štart bez complete", "nepárované (otvorená aktivita)"),
            ("záporné trvanie (complete pred štartom)", "záporné trvanie", "nepárované, na preverenie"),
            ("poradie nie je číslo", "poradie nie je číslo", "poradie riadku sa nepoužije")):
        found = rows_with(text)
        if found:
            add(label, len(found), decision, examples=found[:5])
    if inconsistent_ids:
        add("ID prípadu sa líši iba veľkosťou písmen alebo medzerami", sum(len(v) for v in inconsistent_ids),
            "nezlúčené – over, či ide o ten istý prípad", "prípady")
    if tie_cases:
        add("zhodné časy bez určeného poradia", tie_cases, "prípad sa nepoužije na varianty a nadväznosti (poradie "
            "sa nevynucuje abecedou ani poradím importu)", "prípady")
    if no_time_cases:
        add("prípad s udalosťou bez platného času", no_time_cases, "prípad sa nepoužije na časové metriky a poradie",
            "prípady")
    trunc_l = sum(1 for c in cases if c["left_truncated"])
    trunc_r = sum(1 for c in cases if c["right_truncated"])
    open_n = sum(1 for c in cases if c["open"])
    if trunc_l:
        add("prípad možno odrezaný na začiatku obdobia", trunc_l, "trvanie sa uvedie zvlášť", "prípady")
    if trunc_r:
        add("prípad možno odrezaný na konci obdobia alebo otvorený", trunc_r, "trvanie sa uvedie zvlášť", "prípady")
    if open_n:
        add("otvorený prípad (posledná udalosť nie je koncová)", open_n, "trvanie sa uvedie zvlášť", "prípady")
    warnings: list[str] = []
    if export_suspect:
        warnings.append("viac ako polovica riadkov má rovnaký čas – môže ísť o čas exportu alebo synchronizácie, nie "
                        "vzniku udalosti; časové metriky sa nepočítajú")
    if name_suspect:
        warnings.append(f"stĺpec času „{ts_name}“ má názov ako čas zápisu, zmeny alebo exportu – over jeho význam")
    if zone is None and without_offset and not with_offset:
        warnings.append("časová zóna neznáma – odstupy cez prechod letného času môžu byť skreslené o hodinu"
                        + (f" ({dst_naive} časov v hodine prechodu)" if dst_naive else ""))
    if precision.get("day"):
        warnings.append(f"{precision['day']} časov má presnosť iba na deň – poradie v rámci dňa nie je známe")
    if renamed:
        warnings.append("názvy aktivít, ktoré sa líšia iba písmenami či diakritikou (možné premenovanie): "
                        + "; ".join(" / ".join(v) for v in renamed[:5]))
    if many_unique:
        warnings.append("veľa jedinečných názvov aktivít – možno voľný text alebo zmiešané procesy")
    if len(systems) > 1:
        warnings.append("udalosti z viacerých systémov: " + ", ".join(systems[:5]) + " – over, či ide o jeden proces "
                        "a rovnakú úroveň prípadu")
    if appear:
        warnings.append("aktivity, ktoré sa objavia alebo zaniknú počas obdobia (zmena logovania alebo názvov?): "
                        + ", ".join(appear[:5]))
    if gaps:
        warnings.append("mesiace bez udalostí: " + ", ".join(gaps[:6]))
    if snapshot_like:
        warnings.append(f"{single} z {n_cases} prípadov má jediný riadok – vyzerá to ako snapshot (aktuálny stav), "
                        "nie história; snapshot sa nespracuje ako sekvencia udalostí")
    if selection:
        warnings.append(f"výber prípadov: {selection}" + (" – trvania budú skreslené (chýbajú nedokončené prípady)"
                                                        if re.search(r"dokon|ukon|complet|finish", selection, re.I) else ""))
    elif end_set and n_cases > 20 and open_n == 0:
        warnings.append("všetky prípady končia koncovou aktivitou – možno výber iba dokončených prípadov")
    usable = sum(1 for e in ev if e["usable_count"])
    usable_time = sum(1 for e in ev if e["usable_time"])
    availability = _availability(kind, mapping, usable, usable_time, n, n_cases, ordered_cases, export_suspect,
                                 end_set, life_pairs, life_amb + life_no_start + life_no_end + life_neg,
                                 due_definition, zone is None and without_offset > 0 and not with_offset)
    report = {
        "verzia_parsera": PARSER_VERSION, "verzia_mapovania": version, "riadky": n, "prípady": n_cases,
        "použiteľné_riadky": usable, "riadky_s_platným_časom": usable_time, "vylúčené_riadky": n - usable,
        "usporiadané_prípady": ordered_cases, "druh_dát": kind,
        "časová_zóna": {"politika": policy, "s_offsetom": with_offset, "bez_offsetu": without_offset},
        "presnosť_času": dict(precision), "obdobie": {"od": p_start.isoformat() if p_start else None,
                                                      "do": p_end.isoformat() if p_end else None},
        "kontroly": checks, "upozornenia": warnings, "dostupnosť": availability, "transformácie": transformations,
        "start_complete": {"páry": life_pairs, "nejednoznačné": life_amb, "complete_bez_štartu": life_no_start,
                           "štart_bez_complete": life_no_end, "záporné": life_neg} if "lifecycle" in mapping else None,
    }
    _store_events(cfg, org_id, iid, ev, cases)
    return report


def _availability(kind: str, mapping: dict[str, str], usable: int, usable_time: int, n: int, n_cases: int,
                  ordered_cases: int, export_suspect: bool, end_set: set[str], pairs: int, bad_pairs: int,
                  due_definition: str, unknown_zone: bool) -> dict[str, dict[str, Any]]:
    """Ktoré metriky dáta podporia – po metrikách (žiadne univerzálne „80 % platných riadkov = všetko povolené“)."""
    out: dict[str, dict[str, Any]] = {}

    def put(metric: str, state: str, reason: str, coverage: float | None = None):
        out[metric] = {"stav": state, "dôvod": reason, "pokrytie": round(coverage, 4) if coverage is not None else None}

    if usable == 0:
        for m in METRICS:
            put(m, "nie", "žiadne riadky s ID prípadu a aktivitou")
        return out
    put("počty", "áno", f"{usable} z {n} riadkov má ID prípadu aj aktivitu", usable / max(1, n))
    put("frekvencie_aktivít", "áno", "evidované činnosti, nie celý pracovný deň", usable / max(1, n))
    if kind == "snapshot":
        for m in ("varianty_a_nadväznosti", "odstupy", "trvanie_prípadu", "opakovanie_kroku", "čas_aktivity",
                  "odchýlka_od_termínu"):
            put(m, "nie", "snapshot – aktuálny stav, nie história udalostí")
        put("pracovný_čas", "nie", "chýba dohodnutý pracovný kalendár")
        return out
    cov = ordered_cases / max(1, n_cases)
    time_reason = ""
    if "timestamp" not in mapping:
        time_reason = "chýba stĺpec času"
    elif export_suspect:
        time_reason = "podozrenie, že čas je čas exportu/synchronizácie"
    elif usable_time == 0:
        time_reason = "žiadny riadok s platným časom"
    if time_reason:
        for m in ("varianty_a_nadväznosti", "odstupy", "trvanie_prípadu", "čas_aktivity", "odchýlka_od_termínu"):
            put(m, "nie", time_reason)
        put("opakovanie_kroku", "čiastočne", "iba počet opakovaní aktivity v prípade (bez poradia)",
            usable / max(1, n))
    else:
        state = "áno" if cov == 1 else ("čiastočne" if cov > 0 else "nie")
        excluded = usable - usable_time
        reason = (f"iba jednoznačne usporiadané prípady ({ordered_cases} z {n_cases})"
                  + (f"; {excluded} riadkov bez platného času podporí iba početnosti" if excluded else ""))
        put("varianty_a_nadväznosti", state, reason + " – pozorované sekvencie, nie kauzalita", cov)
        put("odstupy", state, reason + " – uplynutý čas, nie automaticky čakanie ani aktívna práca"
            + ("; zóna neznáma (možné skreslenie o hodinu pri prechode času)" if unknown_zone else ""), cov)
        if end_set:
            put("trvanie_prípadu", state, "od začiatku po koniec; otvorené a odrezané prípady zvlášť", cov)
        else:
            put("trvanie_prípadu", "čiastočne" if cov else "nie", "hranice prípadu nedefinované – iba od prvej po "
                "poslednú zaznamenanú udalosť; otvorené a odrezané prípady sa nedajú spoľahlivo oddeliť", cov)
        put("opakovanie_kroku", state, "kandidát na opakovanú prácu, môže byť aj legitímny cyklus", cov)
        if "lifecycle" not in mapping:
            put("čas_aktivity", "nie", "bez stĺpca start/complete sa čas výkonu aktivity nedá merať")
        elif pairs == 0:
            put("čas_aktivity", "nie", "žiadny jednoznačne spárovaný start/complete")
        else:
            put("čas_aktivity", "áno" if bad_pairs == 0 else "čiastočne",
                f"{pairs} jednoznačných párov" + (f"; {bad_pairs} nespárovaných alebo nejednoznačných" if bad_pairs
                                                  else "") + " – pozor na súbehy")
        if "due_at" not in mapping:
            put("odchýlka_od_termínu", "nie", "chýba stĺpec termínu")
        elif not due_definition or due_definition == "neznámy":
            put("odchýlka_od_termínu", "čiastočne", "nevieme, či termín je pôvodný prísľub alebo zmenený plán")
        else:
            put("odchýlka_od_termínu", "áno", f"termín = {due_definition}")
    put("pracovný_čas", "nie", "chýba dohodnutý pracovný kalendár (kalendárny a pracovný čas sú odlišné metriky)")
    return out


def _iso(v: datetime | None) -> str:
    return v.replace(tzinfo=None).isoformat(timespec="microseconds") if v is not None else ""


def _store_events(cfg: Config, org_id: int, iid: int, ev: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    """Normalizované udalosti a prípady do DuckDB organizácie (cez dočasné CSV – rýchle aj pri veľkých súboroch)."""
    tmpdir = Path(tempfile.mkdtemp(dir=store_dir(cfg)))
    try:
        ev_path, case_path = tmpdir / "events.csv", tmpdir / "cases.csv"
        with ev_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["_row", "case_id", "activity", "ts", "ts_kind", "ts_status", "precision", "event_id",
                        "lifecycle", "instance_id", "role", "sequence", "source_system", "due_at", "issues",
                        "usable_count", "usable_time", "pair_start_row"])
            for e in ev:
                w.writerow([e["_row"], e["case_id"] or "", e["activity"] or "", _iso(e.get("ts")), e.get("ts_kind") or "",
                            e.get("ts_status") or "", e.get("precision") or "", e["event_id"] or "", e["lifecycle"] or "",
                            e["instance_id"] or "", e["role"] or "", "" if e["sequence"] is None else e["sequence"],
                            e["source_system"] or "", _iso(e.get("due")), " | ".join(e["issues"]),
                            int(e["usable_count"]), int(e["usable_time"]), e.get("pair_start_row") or ""])
        with case_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["case_id", "n_events", "ordered", "first_ts", "last_ts", "first_activity", "last_activity",
                        "open", "left_truncated", "right_truncated"])
            for c in cases:
                w.writerow([c["case_id"], c["n_events"], int(c["ordered"]), _iso(c["first_ts"]), _iso(c["last_ts"]),
                            c["first_activity"] or "", c["last_activity"] or "",
                            "" if c["open"] is None else int(c["open"]),
                            "" if c["left_truncated"] is None else int(c["left_truncated"]),
                            "" if c["right_truncated"] is None else int(c["right_truncated"])])
        con = _connect(cfg, org_id)
        try:
            ev_types = ("{'_row': 'BIGINT', 'case_id': 'VARCHAR', 'activity': 'VARCHAR', 'ts': 'TIMESTAMP', "
                        "'ts_kind': 'VARCHAR', 'ts_status': 'VARCHAR', 'precision': 'VARCHAR', 'event_id': 'VARCHAR', "
                        "'lifecycle': 'VARCHAR', 'instance_id': 'VARCHAR', 'role': 'VARCHAR', 'sequence': 'DOUBLE', "
                        "'source_system': 'VARCHAR', 'due_at': 'TIMESTAMP', 'issues': 'VARCHAR', 'usable_count': 'BOOLEAN', "
                        "'usable_time': 'BOOLEAN', 'pair_start_row': 'BIGINT'}")
            case_types = ("{'case_id': 'VARCHAR', 'n_events': 'BIGINT', 'ordered': 'BOOLEAN', 'first_ts': 'TIMESTAMP', "
                          "'last_ts': 'TIMESTAMP', 'first_activity': 'VARCHAR', 'last_activity': 'VARCHAR', "
                          "'open': 'BOOLEAN', 'left_truncated': 'BOOLEAN', 'right_truncated': 'BOOLEAN'}")
            evp, csp = str(ev_path).replace("'", "''"), str(case_path).replace("'", "''")
            con.execute(f"CREATE OR REPLACE TABLE events_{iid} AS SELECT * FROM read_csv('{evp}', header = true, "
                        f"columns = {ev_types}, nullstr = '', quote = '\"', escape = '\"')")
            con.execute(f"CREATE OR REPLACE TABLE cases_{iid} AS SELECT * FROM read_csv('{csp}', header = true, "
                        f"columns = {case_types}, nullstr = '', quote = '\"', escape = '\"')")
        finally:
            con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def quality_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Riadky reportu kvality pre GUI: kontrola, počet, podiel, rozhodnutie."""
    return [{"kontrola": c["kontrola"], "počet": c["počet"], "jednotka": c["jednotka"], "podiel": f"{c['podiel']:.1%}",
             "rozhodnutie": c["rozhodnutie"], "príklady (riadky súboru)": ", ".join(map(str, c["príklady_riadkov"]))}
            for c in report.get("kontroly") or []]


def distinct_values(cfg: Config, imp, column: str, limit: int = 60) -> list[str]:
    """Najčastejšie hodnoty stĺpca surového importu (napr. aktivity na výber začiatočných a koncových)."""
    if not column:
        return []
    con = _connect(cfg, imp["organization_id"], read_only=True)
    try:
        rows = con.execute(f'SELECT CAST("{column}" AS VARCHAR) AS v, COUNT(*) AS n FROM raw_{imp["id"]} '
                           f"WHERE v IS NOT NULL GROUP BY v ORDER BY n DESC, v LIMIT {int(limit)}").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def delete_import(cfg: Config, db: DB, import_id: int) -> None:
    """Výmaz importu: tabuľky v priestore organizácie, pôvodný súbor (ak ho iný import nepoužíva) aj metadáta."""
    imp = db.get_eventlog_import(import_id)
    if imp is None:
        return
    con = _connect(cfg, imp["organization_id"])
    try:
        for t in ("raw", "events", "cases"):
            con.execute(f"DROP TABLE IF EXISTS {t}_{int(import_id)}")
    finally:
        con.close()
    raw_dir = store_dir(cfg) / f"org_{int(imp['organization_id'])}_raw"
    for f in raw_dir.glob(f"{imp['file_hash']}.*") if raw_dir.exists() else []:
        f.unlink(missing_ok=True)
    db._exec("DELETE FROM eventlog_imports WHERE id = ?", (import_id,))
