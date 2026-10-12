"""Kontrola (review queue): rozhodnutie človeka sa uloží ako gold label a premietne do objektu.

Človek smie tvrdenie aj overiť (na rozdiel od modelu, ktorý ho smie iba zamietnuť alebo zoslabiť).
"""

from __future__ import annotations

import json
from typing import Any

from .db import DB

# Možnosti pre gold label podľa úlohy (GUI ich ponúkne ako tlačidlá); "unsure" = neviem.
LABEL_CHOICES: dict[str, list[str]] = {
    "entail": ["supported", "partial", "unsupported", "unsure"],
    "entail_gate": ["supported", "partial", "unsupported", "unsure"],
    "draft_check": ["ok", "problems", "unsure"],
    "role_fit": ["exact", "related", "general", "none", "unsure"],
    "contact_assoc": ["correct", "incorrect", "unsure"],
    "extract": ["correct", "incorrect", "unsure"],
    "cluster_assign": ["correct", "incorrect", "unsure"],
    "role_map": ["correct", "incorrect", "unsure"],
}
LABEL_TEXT = {
    "supported": "vyplýva", "partial": "čiastočne", "unsupported": "nevyplýva", "unsure": "neviem",
    "ok": "v poriadku", "problems": "má problém", "exact": "presná rola", "related": "príbuzná", "general": "vedenie",
    "none": "nesúvisí", "correct": "správne", "incorrect": "nesprávne",
}


def choices_for(task: str) -> list[str]:
    return LABEL_CHOICES.get(task, ["correct", "incorrect", "unsure"])


def apply_label(db: DB, review_id: int, label: str, note: str | None = None) -> dict[str, Any]:
    """Uloží gold label, uzavrie položku a premietne rozhodnutie do objektu (ak to dáva zmysel)."""
    from .verify import apply_entailment

    review = db.get_review(review_id)
    if review is None:
        raise ValueError("Položka kontroly neexistuje.")
    task, otype, oid = review["task"], review["object_type"], int(review["object_id"])
    if label not in choices_for(task):
        raise ValueError(f"Neplatný label {label!r} pre úlohu {task}.")
    db.add_label(task, otype, oid, label, note)
    effect = "label"
    if label != "unsure":
        if task == "entail" and otype == "claim":
            claim = db.get_claim(oid)
            if claim is not None and claim["status"] == "pending":
                status = apply_entailment(db, claim, label, "Rozhodol človek." + (f" {note}" if note else ""))
                effect = f"tvrdenie {status}"
        elif task == "entail_gate" and otype == "claim":
            db.set_claim_gate(oid, label)
            effect = "fakt pre oslovenia " + label
        elif task == "role_map" and otype == "hypothesis" and label == "correct":
            rm = db.get_role_map(oid)
            if rm is not None:
                db.set_role_map(oid, rm["roles"], edited_by_human=True, low_agreement=False)
                effect = "mapa rolí potvrdená"
    db.close_review(review_id)
    return {"task": task, "object": f"{otype} {oid}", "label": label, "effect": effect}


def review_context(db: DB, review) -> dict[str, Any]:
    """Údaje na zobrazenie položky v GUI (text zo zdroja je nedôveryhodný – GUI ho escapuje)."""
    detail = json.loads(review["detail_json"] or "{}")
    out: dict[str, Any] = {"detail": detail}
    if review["object_type"] == "claim":
        claim = db.get_claim(review["object_id"])
        if claim is not None:
            doc = db.get_document(claim["document_id"])
            out.update(claim=dict(claim), url=doc["url"] if doc else None, doc_text=doc["text"] if doc else "")
    elif review["object_type"] == "hypothesis":
        hyp = db.get_hypothesis(review["object_id"])
        out["hypothesis"] = dict(hyp) if hyp else None
        out["role_map"] = db.get_role_map(review["object_id"])
    judgments = db.judgments_for(review["task"], review["object_type"], review["object_id"])
    out["judgments"] = [dict(j) for j in judgments[-5:]]
    return out
