"""Stav hypotézy na samostatných osiach a postup k zákazníkovi (audit 14. 10. 2026: overený citát nie je overený trh).

Osi sa nesčítavajú do jedného skóre (invariant 7) – každá má stav a zložky:
- literatúra: overené tvrdenia z nezávislých domén (bez dodávateľov a protidôkazov) – podklad na výber rozhovorov,
- zákazník: rozhovory so zákazníkmi a podiel, keď problém spomenuli nevyžiadane (Brána 0),
- dáta: Brána 1 – kľúčové polia so stĺpcom potvrdeným tebou na aktuálnej verzii dát,
- ochota platiť: platiteľ potvrdený v rozhovoroch alebo v obchodnom stave.
Obchodný stav zapisuje človek (GUI Problémy → Obchodný stav): preukázaný problém → vlastník procesu → vzorka dát →
nameraná úspora → pilot → platiteľ; každý krok s poznámkou (dôkaz) a dátumom. Metriky postupu merajú cestu
k zákazníkovi; počet hypotéz a modelových rozhodnutí je iba prevádzková metrika.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .db import DB
from .strength import evidence_strength
from .util import utcnow

BUSINESS_STEPS = ("preukázaný_problém", "vlastník_procesu", "vzorka", "nameraná_úspora", "pilot", "platiteľ")
BUSINESS_LABELS = {"preukázaný_problém": "Preukázaný problém (u zákazníka, nie iba v článkoch)",
                   "vlastník_procesu": "Identifikovaný vlastník procesu", "vzorka": "Získaná vzorka dát",
                   "nameraná_úspora": "Nameraná úspora", "pilot": "Dohodnutý pilot", "platiteľ": "Potvrdený platiteľ"}
AXES = ("literatúra", "zákazník", "dáta", "ochota_platiť")


def business_state(db: DB, hid: int) -> dict[str, Any]:
    hyp = db.get_hypothesis(hid)
    try:
        state = json.loads(hyp["business_json"] or "{}") if hyp is not None else {}
    except ValueError:
        state = {}
    return {k: state.get(k) or {"hotové": False} for k in BUSINESS_STEPS}


def set_business_step(db: DB, hid: int, step: str, done: bool, note: str, by: str = "človek") -> dict[str, Any]:
    """Krok obchodného stavu zapíše človek; hotový krok potrebuje dôkaz (poznámku: kto, čo, kedy)."""
    if step not in BUSINESS_STEPS:
        raise ValueError(f"Neznámy krok {step!r}")
    note = (note or "").strip()
    if done and len(note) < 5:
        raise ValueError("Hotový krok potrebuje dôkaz – napíš, kto a čo (napr. „vedúca účtovníctva, rozhovor 12. 10.“).")
    state = business_state(db, hid)
    state[step] = {"hotové": bool(done), "dôkaz": note[:500], "kedy": utcnow(), "kto": by}
    db.set_hypothesis_fields(hid, business_json=json.dumps(state, ensure_ascii=False))
    return state


def axes(cfg: Config, db: DB, hid: int) -> dict[str, dict[str, Any]]:
    """Štyri samostatné osi so stavom a zložkami (kód, bez modelu)."""
    from .bindings import key_conditions
    from .crm import gate0_metrics

    hyp = db.get_hypothesis(hid)
    label, verified, partial, domains = evidence_strength(db.evidence_for(hid))
    m = gate0_metrics(db, hid)
    business = business_state(db, hid)
    g1 = (hyp["gate1_status"] if hyp is not None else None) or "not_started"
    keys = [f for f in db.fields_for(hid) if f["is_key"]]
    unmet = key_conditions(db, hid) if keys else []
    customer = ("chýba" if not m["customer_interviews"] else
                "potvrdený" if m["verdict_proposal"] in ("pass", "chýba platiteľ") else
                "nepotvrdený" if m["verdict_proposal"] == "fail" else "rozpracované")
    if business["preukázaný_problém"].get("hotové") and customer != "nepotvrdený":
        customer = "potvrdený"
    data = ("realizovateľné" if g1 == "passed" and not unmet else
            "treba znova overiť" if g1 == "needs_revalidation" or (g1 == "passed" and unmet) else
            "nezačaté" if g1 == "not_started" else "čaká na dáta")
    payer = m["payer_confirmed"] or (business["platiteľ"].get("dôkaz") if business["platiteľ"].get("hotové") else None)
    commitments = m["commitments"]
    return {
        "literatúra": {"stav": label, "zložky": {"overené": verified, "čiastočné": partial, "nezávislé_domény": domains},
                       "pozn": "výber rozhovorov, nie dôkaz trhu"},
        "zákazník": {"stav": customer, "zložky": {"rozhovory": m["customer_interviews"], "cieľ": m["target"],
                                                  "nevyžiadane": m["unprompted"], "podiel": m["unprompted_share"]}},
        "dáta": {"stav": data, "zložky": {"Brána 1": g1, "kľúčové_polia": len(keys), "nesplnené": len(unmet)}},
        "ochota_platiť": {"stav": "potvrdená" if payer else ("náznak" if commitments.get("pilot") else "chýba"),
                          "zložky": {"platiteľ": payer or "chýba", "záväzky": commitments}},
    }


def customer_progress(cfg: Config, db: DB, ids: list[int] | None = None) -> dict[str, Any]:
    """Postup k zákazníkovi pre hypotézy vybrané na Bránu 0: relevantné kontakty → oslovení → odpovede →
    rozhovory → vzorky → piloty → nameraná úspora → ochota platiť. Prevádzkové metriky zvlášť."""
    from .crm import gate0_metrics
    from .shortlist import contact_shortlist

    hyps = [db.get_hypothesis(i) for i in ids] if ids else list(db.people_enabled_hypotheses())
    rows = []
    total: dict[str, int] = {}
    for h in [x for x in hyps if x is not None]:
        hid = h["id"]
        m = gate0_metrics(db, hid)
        business = business_state(db, hid)
        relevant = [r for r in contact_shortlist(cfg, db, hid) if r["odporúčanie"] in ("oslov najprv", "oslov")]
        samples = sum(1 for i in db.gate1_inputs_for(hid) if i["file_name"]) + int(bool(business["vzorka"].get("hotové")))
        row = {"hypotéza": hid, "názov": h["title"], "relevantné_kontakty": len(relevant),
               "oslovení": m["funnel"]["odoslaní"], "odpovede": m["funnel"]["odpovede"], "rozhovory": m["customer_interviews"],
               "vzorky": samples, "piloty": int(bool(business["pilot"].get("hotové"))) + m["commitments"].get("pilot", 0),
               "nameraná_úspora": int(bool(business["nameraná_úspora"].get("hotové"))),
               "ochota_platiť": int(bool(m["payer_confirmed"] or business["platiteľ"].get("hotové")))}
        rows.append(row)
        for k, v in row.items():
            if isinstance(v, int) and k != "hypotéza":
                total[k] = total.get(k, 0) + v
    ops = {"hypotézy": db._one("SELECT COUNT(*) AS n FROM hypotheses WHERE status != 'rejected'")["n"],
           "modelové_rozhodnutia": db._one("SELECT COUNT(*) AS n FROM model_judgments")["n"]}
    return {"hypotézy": rows, "spolu": total, "prevádzkové": ops}
