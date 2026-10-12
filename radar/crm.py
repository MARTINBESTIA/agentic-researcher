"""Brána 0: rozhovory, citáty z nich ako dôkazy, metriky, návrh verdiktu a rozhodnutia (R-CRM),
exporty (R-GUI-6) a prístup k údajom o osobe (čl. 15 GDPR).

Brána 0 = kontrolný bod pred stavaním produktu: 8–12 rozhovorov so zákazníkmi, aspoň 3 z 10
opíšu problém sami (bez navádzania) a je jasné, kto by platil. Experti sa do metrík nepočítajú.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB
from .privacy import mask_text, person_pseudonym, pseudonymize_text
from .registers import RPO_ATTRIBUTION
from .util import PERSONAL_RE, utcnow
from .verify import quote_score

TARGET_INTERVIEWS = (8, 12)
UNPROMPTED_THRESHOLD = 0.3  # aspoň 3 z 10
DECISION_STATUS = {"continue": "passed", "stop": "failed", "pause": "paused", "pivot": "in_progress"}
COMMITMENTS = ("intro", "follow_up", "data", "pilot", "none")


def record_interview(cfg: Config, db: DB, hypothesis_id: int, interviewee_kind: str, held_at: str,
                     notes: str, quotes: list[str] | None = None, person_id: int | None = None, **fields: Any) -> dict[str, Any]:
    """Uloží rozhovor; citáty z neho sa stanú tvrdeniami (origin interview) na pseudo-dokumente
    s poznámkami. Citát musí prejsť rovnakou kontrolou kódom voči poznámkam (R-CRM-2)."""
    if interviewee_kind not in ("customer", "expert"):
        raise ValueError("Typ respondenta je customer alebo expert.")
    pseudonym = db.next_pseudonym()
    clean_notes = mask_text(notes or "")  # e-maily a telefóny do dôkazov nepatria
    iid = db.insert_interview(person_id=person_id, pseudonym=pseudonym, hypothesis_id=hypothesis_id,
                              interviewee_kind=interviewee_kind, held_at=held_at, notes=clean_notes, **fields)
    url = f"interview://{iid}"
    digest = hashlib.sha256(clean_notes.encode("utf-8")).hexdigest()
    doc_id, _ = db.upsert_document(url, f"Rozhovor {pseudonym}", "interview", None, None, digest, clean_notes or "-")
    db.set_interview_document(iid, doc_id)
    db.mark_extracted(doc_id)
    stats: Counter = Counter()
    for quote in quotes or []:
        quote = quote.strip()
        if not quote:
            continue
        cid = db.insert_claim(doc_id, 0, None, quote[:300], f"{fields.get('role_label') or 'respondent'} ({pseudonym})",
                              "pain", quote, origin="interview")
        score = quote_score(quote, clean_notes)
        if PERSONAL_RE.search(quote):
            db.set_claim_verification(cid, "rejected", score, "Citát obsahuje kontaktný údaj.")
            stats["rejected"] += 1
        elif score < cfg.pipeline.quote_match_threshold:
            db.set_claim_verification(cid, "rejected", score, "Citát nie je v poznámkach z rozhovoru.")
            stats["rejected"] += 1
        else:
            db.set_claim_verification(cid, "verified", score, f"Citát z rozhovoru {pseudonym} nájdený v poznámkach.")
            db.assign_claim(hypothesis_id, cid)
            stats["verified"] += 1
    if person_id is not None:
        db.update_person(person_id, status="interviewed", last_interaction_at=utcnow())
    hyp = db.get_hypothesis(hypothesis_id)
    if hyp is not None and hyp["gate0_status"] == "not_started":
        db.set_hypothesis_fields(hypothesis_id, gate0_status="in_progress")
    return {"interview_id": iid, "pseudonym": pseudonym, **stats}


def gate0_metrics(db: DB, hypothesis_id: int) -> dict[str, Any]:
    """Metriky Brány 0 (R-CRM-4). Experti sa nepočítajú (R-CRM-6)."""
    hyp = db.get_hypothesis(hypothesis_id)
    interviews = db.interviews_for(hypothesis_id)
    customers = [i for i in interviews if i["interviewee_kind"] == "customer"]
    experts = [i for i in interviews if i["interviewee_kind"] == "expert"]
    unprompted = sum(1 for i in customers if i["mentioned_unprompted"] == 1)
    share = unprompted / len(customers) if customers else 0.0
    commitments = Counter(i["commitment"] for i in customers if i["commitment"] and i["commitment"] != "none")
    links = [l for l in db.links_for_hypothesis(hypothesis_id) if not l["is_expert"]]
    outreach = db.outreach_list(hypothesis_id)
    people_with = lambda statuses: len({o["person_id"] for o in outreach if o["status"] in statuses})  # noqa: E731
    sent_people = len({o["person_id"] for o in outreach if o["sent_at"]})
    replied = people_with(("replied", "scheduled", "interviewed", "declined", "no_show"))
    funnel = {
        "kandidáti": len({l["person_id"] for l in links}),
        "schválení": people_with(("approved", "sent", "replied", "no_reply", "bounced", "declined", "scheduled", "interviewed", "no_show")),
        "odoslaní": sent_people,
        "odpovede": replied,
        "dohodnuté": people_with(("scheduled", "interviewed", "no_show")),
        "rozhovory": len(customers),
    }
    payer = hyp["payer_confirmed"] if hyp is not None else None
    if len(customers) < TARGET_INTERVIEWS[0]:
        verdict = "nedostatok dát"
    elif share < UNPROMPTED_THRESHOLD:
        verdict = "fail"
    elif not payer:
        verdict = "chýba platiteľ"
    else:
        verdict = "pass"
    return {
        "customer_interviews": len(customers),
        "target": f"{TARGET_INTERVIEWS[0]}–{TARGET_INTERVIEWS[1]}",
        "expert_interviews": len(experts),
        "unprompted": unprompted,
        "unprompted_share": round(share, 3),
        "unprompted_threshold": UNPROMPTED_THRESHOLD,
        "payer_confirmed": payer,
        "commitments": dict(commitments),
        "funnel": funnel,
        "response_rate": round(replied / sent_people, 3) if sent_people else None,
        "verdict_proposal": verdict,
    }


def record_decision(db: DB, hypothesis_id: int, decision: str, rationale: str) -> int:
    """Rozhodnutie robí človek (R-CRM-5): uloží sa so snímkou metrík a nastaví gate0_status."""
    if decision not in DECISION_STATUS:
        raise ValueError(f"Rozhodnutie je jedno z: {', '.join(DECISION_STATUS)}")
    if not rationale.strip():
        raise ValueError("Rozhodnutie potrebuje odôvodnenie.")
    did = db.add_decision(hypothesis_id, decision, rationale.strip(), gate0_metrics(db, hypothesis_id))
    db.set_hypothesis_fields(hypothesis_id, gate0_status=DECISION_STATUS[decision])
    return did


# ======================================================================= exporty
def _person_label(person) -> str:
    role = person["role_title"] or "funkcia neuvedená"
    org_type = person["org_sector_kind"] if "org_sector_kind" in person.keys() and person["org_sector_kind"] else "organizácia"
    return f"{person_pseudonym(person['id'])} ({role}, {org_type})"


def export_rows(db: DB, what: str, include_personal: bool) -> tuple[list[str], list[list[Any]]]:
    if what == "hypotheses":
        cols = ["id", "title", "segment", "status", "origin", "people_enabled", "gate0_status", "verdict_proposal",
                "customer_interviews", "founder_fit", "payer_confirmed"]
        rows = []
        for h in db.all_hypotheses():
            m = gate0_metrics(db, h["id"]) if h["people_enabled"] else {}
            rows.append([h["id"], h["title"], h["segment"], h["status"], h["origin"], h["people_enabled"], h["gate0_status"],
                         m.get("verdict_proposal", ""), m.get("customer_interviews", ""), h["founder_fit"], h["payer_confirmed"]])
        return cols, rows
    if what == "people":
        cols = ["person", "role", "organization", "hypothesis", "link", "priority", "status", "contacts", "source_url", "zdroj"]
        rows = []
        for h in db.people_enabled_hypotheses():
            for l in db.links_for_hypothesis(h["id"]):
                person = db.get_person(l["person_id"])
                contacts = db.contacts_for_person(l["person_id"])
                name = person["full_name"] if include_personal else _person_label(person)
                contact_text = "; ".join(f"{c['kind']}: {c['value']}" for c in contacts) if include_personal else f"{len(contacts)} kontaktov"
                src = person["source_url"] if include_personal else "(pseudonymizované)"
                rows.append([name, person["role_title"], person["org_name"], h["id"], l["link_type"], l["priority"],
                             person["status"], contact_text, src, RPO_ATTRIBUTION if person["source_kind"] == "rpo" else ""])
        return cols, rows
    if what == "outreach":
        cols = ["id", "person", "hypothesis", "channel", "variant", "status", "subject", "body", "approved_at", "sent_at", "follow_up_due"]
        rows = []
        for o in db.outreach_list():
            person = db.get_person(o["person_id"])
            if include_personal:
                rows.append([o["id"], o["full_name"], o["hypothesis_id"], o["channel"], o["variant"], o["status"], o["subject"],
                             o["body"], o["approved_at"], o["sent_at"], o["follow_up_due"]])
            else:
                names = [o["full_name"], split_name(o["full_name"])]
                rows.append([o["id"], _person_label(person), o["hypothesis_id"], o["channel"], o["variant"], o["status"],
                             pseudonymize_text(o["subject"] or "", names), pseudonymize_text(o["body"] or "", names),
                             o["approved_at"], o["sent_at"], o["follow_up_due"]])
        return cols, rows
    if what == "interviews":
        cols = ["pseudonym", "hypothesis", "kind", "role", "org_type", "held_at", "last_occurrence", "current_workaround",
                "cost_impact", "decision_and_payer", "tried_before", "mentioned_unprompted", "commitment", "notes"]
        rows = []
        names = [n for p in db.all_people() for n in (p["full_name"], split_name(p["full_name"]))]
        for i in db.interviews_for():
            notes = i["notes"] if include_personal else pseudonymize_text(i["notes"] or "", names)
            rows.append([i["pseudonym"], i["hypothesis_id"], i["interviewee_kind"], i["role_label"], i["org_type"], i["held_at"],
                         i["last_occurrence"], i["current_workaround"], i["cost_impact"], i["decision_and_payer"],
                         i["tried_before"], i["mentioned_unprompted"], i["commitment"], notes])
        return cols, rows
    raise ValueError("Export: hypotheses, people, outreach alebo interviews.")


def split_name(full_name: str) -> str:
    parts = (full_name or "").split()
    return parts[-1] if parts else ""


def export(cfg: Config, db: DB, what: str, fmt: str = "csv", include_personal: bool = False) -> Path:
    """Export do exports/ (gitignored, maže ho purge po 30 dňoch). Predvolene pseudonymizovaný."""
    cols, rows = export_rows(db, what, include_personal)
    out = Path(cfg.exports_dir)
    out.mkdir(parents=True, exist_ok=True)
    suffix = "osobne" if include_personal else "pseudonym"
    path = out / f"{what}_{datetime.now():%Y-%m-%d_%H%M}_{suffix}.{fmt}"
    if fmt == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(cols)
            writer.writerows(rows)
    elif fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = what
        ws.append(cols)
        for r in rows:
            ws.append([v if isinstance(v, (int, float)) or v is None else str(v) for v in r])
        if what == "people":
            ws.append([])
            ws.append([RPO_ATTRIBUTION])
        wb.save(path)
    else:
        raise ValueError("Formát je csv alebo xlsx.")
    return path


def export_person(cfg: Config, db: DB, person_id: int) -> Path:
    """Všetky údaje o osobe (žiadosť o prístup, čl. 15 GDPR) do JSON v exports/."""
    person = db.get_person(person_id)
    if person is None:
        raise ValueError(f"Osoba {person_id} neexistuje.")
    data = {
        "osoba": dict(person),
        "kontakty": [dict(c) for c in db.contacts_for_person(person_id)],
        "väzby": [dict(l) for l in db.links_for_person(person_id)],
        "oslovenia": [dict(o) for o in db.outreach_for_person(person_id)],
        "rozhovory": [dict(i) for i in db.interviews_of_person(person_id)],
        "poznámka": "Údaje zo zverejnených zdrojov; právny základ oprávnený záujem (čl. 6 ods. 1 písm. f GDPR).",
    }
    out = Path(cfg.exports_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"osoba_{person_id}_{datetime.now():%Y-%m-%d_%H%M}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
