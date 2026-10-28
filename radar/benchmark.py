"""Benchmark v reálnom svete (M40, audit 26. 10. 2026).

Kohorta „systém“: najviac 10 príležitostí zo systému (celý reťazec v Prehľade príležitosti). Kontrolná kohorta
„ručne“: najviac 10 problémov, ktoré človek vyberie sám z čerstvých oznámení o obstarávaní (TED, M37) bez agentov –
problém, kontakty aj správy pripraví ručne. Obe kohorty sa porovnajú v obchodných stavoch (M35), odpovediach,
rozhovoroch a čase prípravy. Ciele: aspoň 20 odoslaných oslovení a 5 rozhovorov spolu.
Rozhodovacie pravidlo (DECISIONS 28. 10. 2026): ak systém neprekoná ručnú kohortu v obchodných stavoch, ďalší míľnik
o „lepšom uvažovaní“ (modely, agenti, Brána 2) sa nerobí. Odosiela vždy človek; bez modelu.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from .commercial import STAGES, wtp_state
from .config import Config
from .db import DB
from .privacy import Suppression

COHORTS = {"system": "systém (celý reťazec)", "manual": "ručne z TED (bez agentov)"}
COHORT_SIZE = 10
TARGET_SENT, TARGET_INTERVIEWS = 20, 5
# Porovnanie: najprv vyšší obchodný stav (počet organizácií), potom potvrdený problém – každá zložka sa ukáže.
COMPARE = ("zaplatené", "záujem o kúpu", "rozpočet", "platiteľ", "potvrdený problém")
STAGE_KEY = {"buyer_identified": "platiteľ", "budget_verified": "rozpočet", "purchase_intent": "záujem o kúpu",
             "payment_received": "zaplatené"}


class BenchmarkError(ValueError):
    pass


def add_member(db: DB, hid: int, cohort: str, source_url: str | None = None, note: str = "") -> int:
    if cohort not in COHORTS:
        raise BenchmarkError(f"Neznáma kohorta {cohort!r}.")
    hyp = db.get_hypothesis(hid)
    if hyp is None:
        raise BenchmarkError(f"Hypotéza {hid} neexistuje.")
    if db.benchmark_member(hid) is not None:
        raise BenchmarkError(f"H{hid} už je v benchmarku.")
    if len(db.benchmark_members(cohort)) >= COHORT_SIZE:
        raise BenchmarkError(f"Kohorta „{COHORTS[cohort]}“ už má {COHORT_SIZE} problémov.")
    if cohort == "system" and hyp["origin"] == "manual":
        raise BenchmarkError("Ručne zadaný problém nepatrí do kohorty systému.")
    if cohort == "manual" and hyp["origin"] != "manual":
        raise BenchmarkError("Do ručnej kohorty patrí iba problém zadaný ručne (z oznámenia o obstarávaní).")
    return db.add_benchmark_member(hid, cohort, (source_url or "").strip() or None, note.strip() or None)


def add_manual_problem(db: DB, title: str, segment: str, description: str, ted_url: str, note: str = "") -> int:
    """Ručný problém z oznámenia o obstarávaní: hypotéza s pôvodom „manual“, Brána 0 zapnutá (kontakty pridáš ručne)."""
    if len(title.strip()) < 10:
        raise BenchmarkError("Opíš problém aspoň jednou vetou (názov ≥ 10 znakov).")
    if not ted_url.strip().startswith(("http://", "https://")):
        raise BenchmarkError("Zadaj odkaz na oznámenie o obstarávaní (napr. TED).")
    if len(db.benchmark_members("manual")) >= COHORT_SIZE:
        raise BenchmarkError(f"Ručná kohorta už má {COHORT_SIZE} problémov.")
    hid = db.create_hypothesis(title.strip(), description.strip(), segment.strip(), origin="manual")
    db.set_people_selection(hid, True)
    add_member(db, hid, "manual", ted_url, note)
    return hid


def set_prep_minutes(db: DB, hid: int, minutes: float) -> None:
    if db.benchmark_member(hid) is None:
        raise BenchmarkError(f"H{hid} nie je v benchmarku.")
    if minutes < 0:
        raise BenchmarkError("Čas prípravy nemôže byť záporný.")
    db.update_benchmark_member(hid, prep_minutes=float(minutes))


def record_manual_outreach(cfg: Config, db: DB, hid: int, contact_point_id: int, subject: str, note: str = "") -> int:
    """Ručne napísané oslovenie: záznam, aby pošta (M41) spárovala odoslanie a odpovede a metriky ho započítali.
    Text píše a odosiela človek; systém ho neskladá ani neposiela. Kontrola zoznamu potlačenia platí."""
    cp = db.get_contact(contact_point_id)
    if cp is None or cp["kind"] != "email":
        raise BenchmarkError("Vyber e-mailový kontakt.")
    if Suppression(cfg, db).is_suppressed(email=cp["value"]):
        raise BenchmarkError("Kontakt je na zozname potlačenia – neoslovuj ho.")
    if not subject.strip():
        raise BenchmarkError("Zadaj predmet správy (podľa neho sa nájde vo vlákne).")
    from .outreach import SEND_STATES, send_state

    # Rovnaké pravidlá ako pri schválení návrhu systému: stav kontaktu, vylúčená organizácia, neoverené obstarávanie
    # a denný limit (kontrola konzistentnosti 28. 10. 2026).
    row = {"status": "draft", "person_id": cp["person_id"], "org_id": cp["org_id"], "hypothesis_id": hid,
           "contact_point_id": contact_point_id, "package_json": None, "checks_json": "{}"}
    state, reasons = send_state(cfg, db, row)
    if state in ("suppressed", "procurement_unverified"):
        raise BenchmarkError(f"Nedá sa zapísať ({SEND_STATES[state]}): " + "; ".join(reasons))
    now = datetime.now(timezone.utc)
    if db.approvals_on(now.date().isoformat()) >= cfg.outreach.daily_cap:
        raise BenchmarkError(f"Denný limit {cfg.outreach.daily_cap} schválených oslovení je vyčerpaný.")
    body = note.strip() or "(text napísaný ručne mimo systému)"
    oid = db.insert_outreach(cp["person_id"], hid, "email", "manual", subject.strip(), body, [],
                             {"napísané_ručne": True}, "approved", contact_point_id, org_id=cp["org_id"])
    db.update_outreach(oid, approved_at=now.isoformat(timespec="seconds"))
    return oid


def _days(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return round((datetime.fromisoformat(b[:10]) - datetime.fromisoformat(a[:10])).days, 1)
    except ValueError:
        return None


def cohort_report(db: DB) -> dict[str, Any]:
    """Porovnanie kohort so zložkami; verdikt až pri dosiahnutí cieľov (20 oslovení, 5 rozhovorov)."""
    outreach, interviews = db.outreach_by_hypothesis(), db.customer_interviews_by_hypothesis()
    outcomes = db.outcomes_by_hypothesis()
    rows, members = {}, []
    for cohort, label in COHORTS.items():
        agg: dict[str, Any] = {"kohorta": label, "problémy": 0, "oslovené problémy": 0, "odoslané správy": 0,
                               "oslovené organizácie": 0, "odpovede": 0, "rozhovory": 0, "potvrdený problém": 0,
                               **{k: 0 for k in STAGE_KEY.values()}}
        prep, lead = [], []
        for m in db.benchmark_members(cohort):
            hid = m["hypothesis_id"]
            o, out = outreach.get(hid, {}), outcomes.get(hid, {})
            orgs = wtp_state(db, hid)["organizácie"]
            stage_orgs = {key: sum(1 for s in orgs.values() if s in STAGES and STAGES.index(s) >= STAGES.index(stage))
                          for stage, key in STAGE_KEY.items()}
            first = o.get("first_sent")
            item = {"H": hid, "kohorta": label, "problém": m["title"], "zdroj": m["source_url"],
                    "príprava (min)": m["prep_minutes"], "odoslané": o.get("sent") or 0,
                    "odpovede": max(o.get("replied") or 0, out.get("replies") or 0), "rozhovory": interviews.get(hid, 0),
                    "potvrdený problém": out.get("confirmed") or 0, **stage_orgs,
                    "dni do 1. oslovenia": _days(m["added_at"], first)}
            members.append(item)
            agg["problémy"] += 1
            agg["oslovené problémy"] += int(item["odoslané"] > 0)
            agg["odoslané správy"] += item["odoslané"]
            agg["oslovené organizácie"] += o.get("sent_orgs") or 0
            for k in ("odpovede", "rozhovory", "potvrdený problém", *STAGE_KEY.values()):
                agg[k] += item[k]
            if m["prep_minutes"] is not None:
                prep.append(m["prep_minutes"])
            if item["dni do 1. oslovenia"] is not None:
                lead.append(item["dni do 1. oslovenia"])
        agg["príprava (min, medián)"] = median(prep) if prep else None
        agg["dni do 1. oslovenia (medián)"] = median(lead) if lead else None
        rows[cohort] = agg
    sent = sum(r["odoslané správy"] for r in rows.values())
    talks = sum(r["rozhovory"] for r in rows.values())
    sys_key = tuple(rows["system"][k] for k in COMPARE)
    man_key = tuple(rows["manual"][k] for k in COMPARE)
    if sent < TARGET_SENT or talks < TARGET_INTERVIEWS:
        verdict = (f"málo dát: {sent} z {TARGET_SENT} odoslaných oslovení, {talks} z {TARGET_INTERVIEWS} rozhovorov "
                   "– porovnanie zatiaľ nič nehovorí")
    elif sys_key > man_key:
        verdict = "systém vedie v obchodných stavoch – ďalšie zlepšovanie uvažovania má zmysel"
    elif sys_key < man_key:
        verdict = ("ručná kohorta vedie – podľa pravidla sa míľnik o „lepšom uvažovaní“ (modely, agenti, Brána 2) "
                   "nerobí; zlepšuj výber zákazníkov a oslovenia")
    else:
        verdict = "nerozhodné – systém ručnú kohortu neprekonal; míľnik o „lepšom uvažovaní“ sa nerobí"
    return {"kohorty": list(rows.values()), "členovia": members, "porovnanie": list(COMPARE), "odoslané": sent,
            "rozhovory": talks, "verdikt": verdict}
