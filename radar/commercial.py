"""Obchodné stavy a ochota platiť (M35, 26. 10. 2026; audit: „kto platí“ nie je „ochota platiť“).

Štyri pozitívne stupne (každý s dôkazom kto, čo, kedy – zapisuje človek, systém nič nevyvodzuje z textu):
- buyer_identified – vieme, kto rozhoduje o rozpočte (platiteľ),
- budget_verified – existuje rozpočet na takýto výdavok (dnešné náklady, položka, projekt),
- purchase_intent – kupujúci prejavil konkrétny záujem za uvedených podmienok (čo, rozsah, cena alebo rozpätie, termín),
- payment_received – peniaze prišli (suma, mena, jednorazovo / mesačne / ročne, hodnota zmluvy).
Plus negatívny dôkaz `no_willingness` (nemajú rozpočet, nezaplatia) – aj to je výsledok.
„Ochota platiť potvrdená“ = záujem o kúpu alebo platba. Platiteľ a rozpočet sú kroky k nej, nie potvrdenie.
Výsledok oslovenia s potvrdeným problémom musí mať zapísané aj posúdenie ochoty platiť (aspoň „nepýtali sme sa“) –
pokyn používateľa 26. 10. 2026. Fakty o organizácii nie sú osobné údaje; väzba na osobu je voliteľná a `forget` ju zmaže.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .db import DB
from .privacy import mask_text

STAGES = ("buyer_identified", "budget_verified", "purchase_intent", "payment_received")
NEGATIVE = "no_willingness"
STAGE_LABEL = {"buyer_identified": "platiteľ identifikovaný (kto rozhoduje o rozpočte)",
               "budget_verified": "rozpočet overený (na takýto výdavok existuje rozpočet)",
               "purchase_intent": "záujem o kúpu za konkrétnych podmienok",
               "payment_received": "zaplatené",
               NEGATIVE: "nezaplatia / nemajú rozpočet"}
# posúdenie pri výsledku oslovenia: stupne + „nepýtali sme sa“ a „nejasné“ (bez záznamu obchodného dôkazu)
ASSESSMENTS = {"not_asked": "nepýtali sme sa (zatiaľ)", "unclear": "pýtali sme sa – nejasné", **STAGE_LABEL}
WTP_CONFIRMED = {"purchase_intent", "payment_received"}
AXIS_LABEL = {None: "chýba", "buyer_identified": "platiteľ známy", "budget_verified": "rozpočet overený",
              "purchase_intent": "potvrdená – záujem o kúpu", "payment_received": "potvrdená – zaplatené"}
BILLING = {"one_off": "jednorazovo", "monthly": "mesačne", "annual": "ročne"}
# Otázky podľa The Mom Test: minulosť a dnešné náklady, nie „kúpili by ste…“ (pri rozhovore, nie v prvom e-maile)
QUESTIONS = [
    ("buyer_identified", "Kto u vás schvaľuje výdavok na takúto vec a z akého rozpočtu sa platí dnešné riešenie?"),
    ("budget_verified", "Koľko vás to dnes stojí (ľudia, externá služba, licencie) a je na to vyhradená položka alebo "
                        "projekt?"),
    ("purchase_intent", "Ak skúška splní dohodnutú metriku, aký bude ďalší krok – cenová ponuka, objednávka, schválenie "
                        "rozpočtu? Kto ho urobí, kedy a v akom rozsahu ceny?"),
    ("payment_received", "Platba: zmluva alebo faktúra – suma, mena, jednorazovo / mesačne / ročne."),
]


class CommercialError(ValueError):
    pass


def validate(w: dict[str, Any], org_id: int | None) -> dict[str, Any]:
    """Skontroluje zápis ochoty platiť; vráti polia pre commercial_evidence (bez hypotézy). Chýbajúce = chyba
    s presným pokynom, čo doplniť (nič sa nedopĺňa samo)."""
    stage = (w or {}).get("stage")
    if stage not in (*STAGES, NEGATIVE):
        raise CommercialError(f"Neznámy stupeň ochoty platiť {stage!r}.")
    evidence = " ".join(str(w.get("evidence") or "").split())
    if len(evidence) < 10:
        raise CommercialError("Ochota platiť potrebuje dôkaz – kto, čo a kedy (napr. „vedúca ekonomického, rozhovor 3. 11.“).")
    out: dict[str, Any] = {"stage": stage, "evidence": mask_text(evidence)[:1000]}
    if stage in ("purchase_intent", "payment_received") and not org_id:
        raise CommercialError("Záujem o kúpu a platba sa viažu na konkrétnu organizáciu.")
    conditions = " ".join(str(w.get("conditions") or "").split())
    if stage == "purchase_intent":
        if len(conditions) < 10:
            raise CommercialError("Záujem o kúpu potrebuje podmienky: čo by kúpili, v akom rozsahu, za akú cenu alebo "
                                  "rozpätie a kedy.")
    if conditions:
        out["conditions"] = mask_text(conditions)[:1000]
    amount, contract = w.get("amount"), w.get("contract_value")
    for value, label in ((amount, "suma"), (contract, "hodnota zmluvy")):
        if value is not None and float(value) <= 0:
            raise CommercialError(f"{label} musí byť kladná.")
    billing = w.get("billing") or None
    if billing is not None and billing not in BILLING:
        raise CommercialError(f"Neznámy spôsob platby {billing!r}.")
    currency = str(w.get("currency") or "").strip().upper() or None
    if stage == "payment_received":
        if amount is None or not currency or len(currency) != 3 or billing is None:
            raise CommercialError("Platba potrebuje sumu, menu (napr. EUR) a spôsob: jednorazovo, mesačne alebo ročne.")
    if amount is not None:
        out.update(amount=float(amount), currency=currency or "EUR")
    if billing:
        out["billing"] = billing
    if contract is not None:
        out["contract_value"] = float(contract)
    return out


def record(db: DB, hid: int, willingness: dict[str, Any], org_id: int | None = None, person_id: int | None = None,
           outcome_id: int | None = None, interview_id: int | None = None, by: str = "human") -> int:
    """Zapíše obchodný dôkaz (overí ho `validate`)."""
    fields = validate(willingness, org_id)
    return db.add_commercial_evidence(hid, org_id=org_id, person_id=person_id, outcome_id=outcome_id,
                                      interview_id=interview_id, recorded_by=by, **fields)


def _top(rows) -> str | None:
    stages = [r["stage"] for r in rows if r["stage"] in STAGES]
    return max(stages, key=STAGES.index) if stages else None


def wtp_state(db: DB, hid: int) -> dict[str, Any]:
    """Ochota platiť hypotézy: najvyšší stupeň s dôkazom, stupeň po organizáciách, sumy (iba EUR) a negatívne."""
    rows = db.commercial_evidence_for(hid)
    by_org: dict[Any, list] = {}
    for r in rows:
        by_org.setdefault(r["org_id"], []).append(r)
    orgs = {k: _top(v) for k, v in by_org.items() if k is not None}
    top = _top(rows)
    eur = lambda stage: round(sum(r["amount"] for r in rows if r["stage"] == stage and r["amount"]  # noqa: E731
                                  and (r["currency"] or "EUR") == "EUR"), 2)
    return {"stupeň": top, "stav": AXIS_LABEL[top], "potvrdená": top in WTP_CONFIRMED,
            "záznamy": len(rows), "organizácie": orgs,
            "potvrdené_organizácie": sum(1 for s in orgs.values() if s in WTP_CONFIRMED),
            "odmietli": sum(1 for r in rows if r["stage"] == NEGATIVE),
            "záujem_eur": eur("purchase_intent"), "zaplatené_eur": eur("payment_received"),
            "po_stupňoch": dict(Counter(r["stage"] for r in rows))}


def org_stage_label(db: DB, hid: int, org_id: int | None) -> str:
    """Stupeň ochoty platiť jednej organizácie (pre tabuľky kontaktov)."""
    if not org_id:
        return "–"
    rows = db.commercial_evidence_for(hid, org_id)
    top = _top(rows)
    if top is None:
        return "odmietli" if any(r["stage"] == NEGATIVE for r in rows) else "nezistená"
    return AXIS_LABEL[top]


def summary_text(state: dict[str, Any]) -> str:
    parts = [f"záznamy {state['záznamy']}", f"organizácie s potvrdenou ochotou {state['potvrdené_organizácie']}"]
    if state["záujem_eur"]:
        parts.append(f"záujem {state['záujem_eur']:,.0f} €".replace(",", " "))
    if state["zaplatené_eur"]:
        parts.append(f"zaplatené {state['zaplatené_eur']:,.0f} €".replace(",", " "))
    if state["odmietli"]:
        parts.append(f"odmietli {state['odmietli']}")
    return ", ".join(parts)
