"""Ekonomika u konkrétneho zákazníka (M38, audit 26. 10. 2026).

Organizácia × hypotéza: objem prípadov, minúty na prípad, cena práce, dosiahnuteľná úspora, náklad na zavedenie
a na kontrolu. Každý faktor má pôvod – pozorované (`observed`: citát z dokumentu overený kódom alebo dáta organizácie),
uviedol zákazník (`customer_reported`: kto a kedy, väzba na výsledok oslovenia alebo rozhovor), odhad (`inferred`:
ako vznikol) alebo nevieme (`unknown`) – a zdroj. Rozpätie ročnej hodnoty, čistý prínos a návratnosť počíta Python
intervalovou aritmetikou; model sa nepoužíva. Neznámy faktor = otázka do rozhovoru (minulosť, nie „kúpili by ste“).
Úspora z webu dodávateľa sa nepoužije. Pravidlo: veľký trh nič neposúva – „pripravené na ponuku“ až s aspoň jedným
nákladovým údajom dnešného postupu (objem, čas, cena práce) pozorovaným alebo od zákazníka.
"""

from __future__ import annotations

from typing import Any

from .db import DB
from .verify import quote_score

PROVENANCE = {"observed": "pozorované", "customer_reported": "uviedol zákazník", "inferred": "odhad",
              "unknown": "nevieme"}
RANK = {"observed": 3, "customer_reported": 2, "inferred": 1, "unknown": 0}
FACTORS: dict[str, dict[str, Any]] = {
    "cases_per_year": {"label": "objem prípadov za rok", "unit": "prípady/rok", "max": None,
                       "question": "Koľko takých prípadov ste riešili za posledný mesiac? Je to bežný mesiac?"},
    "minutes_per_case": {"label": "práca na jeden prípad", "unit": "min", "max": None,
                         "question": "Ako dlho vám trval posledný taký prípad od začiatku po koniec – kto na ňom robil?"},
    "hourly_cost": {"label": "cena hodiny práce (vrátane odvodov)", "unit": "€/h", "max": None,
                    "question": "Kto to u vás robí a koľko približne stojí hodina jeho práce (mzda s odvodmi, "
                                "alebo sadzba externistu)?"},
    "saving_share": {"label": "dosiahnuteľná úspora času", "unit": "podiel 0–1", "max": 1.0,
                     "question": "Ktorú časť tej práce by ste pri poslednom prípade naozaj nemuseli robiť? Čo by "
                                 "ostalo na človeku (kontrola, výnimky)?"},
    "implementation_cost": {"label": "náklad na zavedenie (jednorazovo)", "unit": "€", "max": None,
                            "question": "Čo vás stálo zavedenie posledného nástroja – peniaze aj čas ľudí?"},
    "control_cost": {"label": "náklad na kontrolu a prevádzku (ročne)", "unit": "€/rok", "max": None,
                     "question": "Koľko času by týždenne zabrala kontrola výstupu a riešenie výnimiek – a kto by to robil?"},
}
COST_FACTORS = ("cases_per_year", "minutes_per_case", "hourly_cost")  # náklad dnešného postupu
SAVING_USE = {"cost_cut": "zníži výdavok (nadčasy, externista, brigádnik)",
              "capacity": "viac práce tým istým tímom (viac obchodov, rýchlejšia odpoveď)",
              "time_only": "iba čas – peniaze sa neušetria", "unknown": "nevieme"}
USE_QUESTION = ("Čo by ste robili s ušetreným časom? Ušetrí to reálny výdavok (nadčasy, externistu), prinesie viac "
                "obchodov, alebo ostane len voľnejší deň?")


class EconomicsError(ValueError):
    pass


def validate(db: DB, hid: int, org_id: int, factor: str, provenance: str, low: float | None = None,
             high: float | None = None, *, evidence: str = "", quote: str = "", document_id: int | None = None,
             outcome_id: int | None = None, interview_id: int | None = None, use: str | None = None) -> dict[str, Any]:
    """Kontrola zápisu kódom; vráti polia na uloženie. Pri chybe EconomicsError s dôvodom."""
    if factor != "saving_use" and factor not in FACTORS:
        raise EconomicsError(f"Neznámy faktor {factor!r}.")
    if provenance not in PROVENANCE:
        raise EconomicsError(f"Neznámy pôvod {provenance!r}.")
    fields: dict[str, Any] = {"factor": factor, "provenance": provenance, "evidence": evidence.strip() or None}
    if factor == "saving_use":
        if use not in SAVING_USE:
            raise EconomicsError("Vyber, ako by zákazník úsporu využil.")
        fields.update(value_text=use, unit="", low=None, high=None)
    elif provenance == "unknown":
        fields.update(low=None, high=None, unit=FACTORS[factor]["unit"])
    else:
        if low is None:
            raise EconomicsError("Zadaj hodnotu (pri rozpätí od–do).")
        high = low if high is None else high
        if low < 0 or high < low:
            raise EconomicsError("Rozpätie musí byť nezáporné a „od“ nesmie byť väčšie ako „do“.")
        top = FACTORS[factor]["max"]
        if top is not None and high > top:
            raise EconomicsError(f"{FACTORS[factor]['label']}: najviac {top}.")
        fields.update(low=float(low), high=float(high), unit=FACTORS[factor]["unit"])
    if provenance == "observed":
        doc = db.get_document(document_id) if document_id else None
        if doc is None:
            raise EconomicsError("Pozorovaný údaj potrebuje dokument (URL alebo ID) s doslovným citátom.")
        if (doc["source_type"] or "") == "vendor":
            raise EconomicsError("Údaj z webu dodávateľa sa nepoužije (marketing vlastného produktu).")
        if not quote.strip() or quote_score(quote, doc["text"]) < 100:
            raise EconomicsError("Citát sa v dokumente doslova nenašiel – skopíruj presný úryvok.")
        if factor != "saving_use" and not any(ch.isdigit() for ch in quote):
            raise EconomicsError("Citát pri číselnom údaji musí obsahovať číslo.")
        fields.update(document_id=document_id, quote=quote.strip(), source_kind="document")
    elif provenance == "customer_reported":
        if outcome_id is None and interview_id is None and len(evidence.strip()) < 10:
            raise EconomicsError("Uveď, kto a kedy to povedal (aspoň 10 znakov), alebo vyber výsledok oslovenia či rozhovor.")
        if outcome_id is not None and not any(o["id"] == outcome_id and o["org_id"] == org_id for o in db.outcomes_for(hid)):
            raise EconomicsError("Výsledok oslovenia nepatrí k tejto organizácii a hypotéze.")
        if interview_id is not None and not any(i["id"] == interview_id for i in db.interviews_for(hid)):
            raise EconomicsError("Rozhovor nepatrí k tejto hypotéze.")
        fields.update(outcome_id=outcome_id, interview_id=interview_id,
                      source_kind="outcome" if outcome_id else "interview" if interview_id else "manual")
    elif provenance == "inferred":
        if len(evidence.strip()) < 10:
            raise EconomicsError("Pri odhade opíš, ako vznikol (aspoň 10 znakov).")
        fields["source_kind"] = "inference"
    return fields


def record(db: DB, hid: int, org_id: int, factor: str, provenance: str, low: float | None = None,
           high: float | None = None, by: str = "human", person_id: int | None = None, **kw: Any) -> int:
    fields = validate(db, hid, org_id, factor, provenance, low, high, **kw)
    return db.add_buyer_factor(hid, org_id, recorded_by=by, person_id=person_id, **fields)


def current_factors(db: DB, hid: int, org_id: int) -> dict[str, Any]:
    """Platná hodnota každého faktora: najlepší pôvod (pozorované > zákazník > odhad > nevieme), pri zhode najnovšia."""
    best: dict[str, Any] = {}
    for r in db.buyer_factors(hid, org_id):
        cur = best.get(r["factor"])
        if cur is None or (RANK[r["provenance"]], r["id"]) > (RANK[cur["provenance"]], cur["id"]):
            best[r["factor"]] = r
    return best


def _rng(row) -> tuple[float, float] | None:
    if row is None or row["provenance"] == "unknown" or row["low"] is None:
        return None
    return float(row["low"]), float(row["high"] if row["high"] is not None else row["low"])


def _fmt(r: tuple[float, float] | None, unit: str = "€", digits: int = 0) -> str:
    if r is None:
        return "chýba"
    lo, hi = (round(x, digits) for x in r)
    num = (lambda x: f"{x:,.{digits}f}".replace(",", " "))
    return f"{num(lo)} {unit}" if lo == hi else f"{num(lo)} – {num(hi)} {unit}"


def economics(db: DB, hid: int, org_id: int) -> dict[str, Any]:
    """Rozpätia (Python), chýbajúce faktory s otázkami a pripravenosť na ponuku."""
    cur = current_factors(db, hid, org_id)
    r = {k: _rng(cur.get(k)) for k in FACTORS}
    out: dict[str, Any] = {"faktory": cur, "chýba": [k for k in FACTORS if r[k] is None]}
    hours = labor = value = net = payback = None
    if r["cases_per_year"] and r["minutes_per_case"]:
        hours = (r["cases_per_year"][0] * r["minutes_per_case"][0] / 60, r["cases_per_year"][1] * r["minutes_per_case"][1] / 60)
    if hours and r["hourly_cost"]:
        labor = (hours[0] * r["hourly_cost"][0], hours[1] * r["hourly_cost"][1])
    if labor and r["saving_share"]:
        value = (labor[0] * r["saving_share"][0], labor[1] * r["saving_share"][1])
    if value and r["control_cost"]:
        net = (value[0] - r["control_cost"][1], value[1] - r["control_cost"][0])
    if net and r["implementation_cost"]:
        if net[0] > 0:
            payback = (r["implementation_cost"][0] / (net[1] / 12), r["implementation_cost"][1] / (net[0] / 12))
        else:
            payback = None
            out["návratnosť_poznámka"] = "pri dolnom odhade sa zavedenie nevráti (čistý prínos ≤ 0)"
    out.update({"hodiny_ročne": hours, "náklad_dnes": labor, "ročná_hodnota": value, "čistý_prínos": net,
                "návratnosť_mesiace": payback,
                "text": {"hodiny_ročne": _fmt(hours, "h", 0), "náklad_dnes": _fmt(labor), "ročná_hodnota": _fmt(value),
                         "čistý_prínos": _fmt(net), "návratnosť_mesiace": _fmt(payback, "mes.", 1)}})
    use = cur.get("saving_use")
    out["využitie_úspory"] = SAVING_USE.get(use["value_text"]) if use is not None else None
    cost_evidence = [k for k in COST_FACTORS if cur.get(k) is not None and cur[k]["provenance"] in ("observed", "customer_reported")]
    out["nákladový_údaj"] = cost_evidence
    questions = [FACTORS[k]["question"] for k in FACTORS
                 if cur.get(k) is None or cur[k]["provenance"] in ("unknown", "inferred")]
    if use is None or use["value_text"] in ("unknown", None):
        questions.append(USE_QUESTION)
    out["otázky"] = questions
    out["pripravené_na_ponuku"] = bool(cost_evidence)
    out["dôvod"] = ("nákladový údaj od zákazníka alebo pozorovaný: " + ", ".join(FACTORS[k]["label"] for k in cost_evidence)
                    if cost_evidence else
                    "chýba nákladový údaj dnešného postupu (objem, čas alebo cena práce) – pozorovaný alebo od zákazníka; "
                    "veľký trh ani odhad nestačí")
    if use is not None and use["value_text"] == "time_only":
        out["varovanie"] = "zákazník ušetrený čas nepremení na peniaze – hodnota v € je iba teoretická"
    return out


def factor_rows(db: DB, hid: int, org_id: int) -> list[dict[str, Any]]:
    """Riadky pre GUI: platné faktory s pôvodom a zdrojom (história ostáva v DB)."""
    cur = current_factors(db, hid, org_id)
    rows = []
    for key, meta in [*FACTORS.items(), ("saving_use", {"label": "využitie úspory", "unit": ""})]:
        r = cur.get(key)
        if key == "saving_use":
            value = SAVING_USE.get(r["value_text"]) if r is not None else "chýba"
        else:
            value = _fmt(_rng(r), meta["unit"], 2) if r is not None and r["provenance"] != "unknown" else "chýba"
        rows.append({"faktor": meta["label"], "hodnota": value,
                     "pôvod": PROVENANCE.get(r["provenance"], "") if r is not None else "",
                     "zdroj": (r["quote"] or r["evidence"] or "") if r is not None else "",
                     "id": r["id"] if r is not None else None})
    return rows


def economics_summary(db: DB, hid: int) -> dict[str, Any]:
    """Pre Prehľad príležitosti: koľko organizácií má nákladový údaj a rozpätia ich ročnej hodnoty."""
    orgs = db.orgs_with_buyer_factors(hid)
    ready, values = [], []
    for org_id in orgs:
        e = economics(db, hid, org_id)
        if e["pripravené_na_ponuku"]:
            ready.append(org_id)
        if e["ročná_hodnota"]:
            values.append(e["text"]["ročná_hodnota"])
    return {"organizácie": len(orgs), "s_nákladovým_údajom": len(ready), "ročné_hodnoty": values}
