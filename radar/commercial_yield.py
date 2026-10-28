"""Zdroj → obchodný výsledok (M36, audit 26. 10. 2026; zatiaľ iba správa).

Objavovanie (Thompson sampling v `discover.py`) odmeňuje dokument s overeným tvrdením – výskumnú výťažnosť, nie obchodnú.
Táto správa kladie vedľa seba dva lieviky pre každú stratégiu a kategóriu zdroja:
- výskumný: dokumenty → dokumenty s overeným tvrdením → overené tvrdenia,
- obchodný: hypotézy → prešli testom → Brána 0 → oslovené → odpovede → rozhovory → potvrdený problém → stupne
  ochoty platiť (M35).
Pôvod hypotézy je dokument jej najstaršieho priradeného tvrdenia (jedna hypotéza = jeden zdroj, súčty sedia); riadok
TED (zákazka posúdená ako zhodná, M37) je vlastnosť hypotézy, nie jej pôvod – ráta sa navyše.
Bandit sa nemení, kým nie je aspoň MIN_SENT odoslaných oslovení v MIN_CATEGORIES kategóriách: predajné výsledky sú
oneskorené, riedke a závisia od toho, koho oslovíš (DECISIONS 28. 10. 2026). Bez modelu.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .commercial import STAGES, wtp_state
from .config import Config
from .db import DB

MIN_SENT, MIN_CATEGORIES = 30, 3
STRATEGY_LABEL = {
    "scout": "scout (agent hľadá problémy)", "matrix": "matica dopytov", "catalog": "katalóg zdrojov",
    "topics": "témy (zber)", "agent": "agent (overovanie hypotéz)", "manual": "ručne", "none": "bez tvrdení",
}
TED_ROW = "TED – zhodná zákazka (navyše)"
RESEARCH = ("dokumenty", "s overeným tvrdením", "overené tvrdenia")
COMMERCIAL = ("hypotézy", "test prešiel", "Brána 0", "oslovené", "odoslané správy", "odpovede", "rozhovory",
              "potvrdený problém")
STAGE_COLUMN = {"buyer_identified": "platiteľ", "budget_verified": "rozpočet", "purchase_intent": "záujem o kúpu",
                "payment_received": "zaplatené"}
COLUMNS = RESEARCH + COMMERCIAL + tuple(STAGE_COLUMN.values())


def source_of(source_kind: str | None, category: str | None, origin: str | None = None) -> tuple[str, str]:
    """(stratégia, kategória) dokumentu alebo pôvodu hypotézy."""
    if origin == "manual" or source_kind == "interview":
        return "manual", "ručne"
    if not source_kind:
        return "none", "bez tvrdení"
    if source_kind == "discovery":
        if category == "scout":
            return "scout", "scout"
        if category == "query_matrix":
            return "matrix", "matica dopytov"
        return "catalog", category or "iné"
    if source_kind == "rss":
        return "catalog", category or "rss"
    if source_kind == "agent":
        return "agent", "agent"
    return "topics", f"témy – {source_kind}"


def _hypothesis_flags(db: DB, h, verdicts: set[str], outreach: dict, interviews: dict, outcomes: dict,
                      wtp_hids: set[int]) -> Counter:
    hid = h["id"]
    try:
        verdict = json.loads(h["validation_json"] or "{}").get("verdikt")
    except ValueError:
        verdict = None
    o, out = outreach.get(hid, {}), outcomes.get(hid, {})
    flags = Counter({
        "hypotézy": 1,
        "test prešiel": int(verdict in verdicts),
        "Brána 0": int(bool(h["people_enabled"])),
        "oslovené": int((o.get("sent") or 0) > 0),
        "odoslané správy": int(o.get("sent") or 0),
        "odpovede": int((o.get("replied") or 0) > 0 or (out.get("replies") or 0) > 0),
        "rozhovory": int(interviews.get(hid, 0) > 0),
        "potvrdený problém": int((out.get("confirmed") or 0) > 0),
    })
    if hid in wtp_hids:
        top = wtp_state(db, hid)["stupeň"]
        if top in STAGES:
            for stage in STAGES[: STAGES.index(top) + 1]:
                flags[STAGE_COLUMN[stage]] = 1
    return flags


def commercial_yield(cfg: Config, db: DB) -> dict[str, Any]:
    """Výskumný a obchodný lievik podľa stratégie a kategórie zdroja + stav pravidla pre bandit."""
    verdicts = set(cfg.people.auto_select_verdicts) or {"podložená"}
    outreach, interviews = db.outreach_by_hypothesis(), db.customer_interviews_by_hypothesis()
    outcomes, ted = db.outcomes_by_hypothesis(), db.ted_hypotheses()
    wtp_hids = set(db.hypotheses_with_commercial_evidence())
    by_strategy: dict[str, Counter] = defaultdict(Counter)
    by_category: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in db.research_yield():
        strategy, category = source_of(r["source_kind"], r["category"] or None)
        for key, col in zip(RESEARCH, ("documents", "with_verified", "verified_claims")):
            by_strategy[strategy][key] += r[col] or 0
            by_category[(strategy, category)][key] += r[col] or 0
    total, ted_row = Counter(), Counter()
    for h in db.hypothesis_origins():
        strategy, category = source_of(h["source_kind"], h["source_category"], h["origin"])
        flags = _hypothesis_flags(db, h, verdicts, outreach, interviews, outcomes, wtp_hids)
        by_strategy[strategy].update(flags)
        by_category[(strategy, category)].update(flags)
        total.update(flags)
        if h["id"] in ted:
            ted_row.update(flags)
    for c in by_strategy.values():
        total.update({k: c[k] for k in RESEARCH})

    def row(label: str, c: Counter, **extra: Any) -> dict[str, Any]:
        return {"zdroj": label, **extra, **{k: int(c.get(k, 0)) for k in COLUMNS}}

    strategies = [row(STRATEGY_LABEL.get(s, s), c) for s, c in sorted(by_strategy.items(), key=lambda x: -x[1]["hypotézy"])]
    if ted_row:
        strategies.append(row(TED_ROW, ted_row))
    categories = [row(cat, c, stratégia=STRATEGY_LABEL.get(s, s))
                  for (s, cat), c in sorted(by_category.items(), key=lambda x: (-x[1]["oslovené"], -x[1]["hypotézy"]))]
    sent_by_cat = {cat: c["odoslané správy"] for (s, cat), c in by_category.items() if c["odoslané správy"]}
    sent = sum(sent_by_cat.values())
    ready = sent >= MIN_SENT and len(sent_by_cat) >= MIN_CATEGORIES
    bandit = {
        "odoslané": sent, "kategórie_s_oslovením": len(sent_by_cat), "pripravené": ready,
        "text": (f"Obchodná odmena pre objavovanie sa dá zvážiť: {sent} odoslaných oslovení v {len(sent_by_cat)} kategóriách."
                 if ready else
                 f"Bandit ostáva na výskumnej výťažnosti: {sent} z {MIN_SENT} odoslaných oslovení, "
                 f"{len(sent_by_cat)} z {MIN_CATEGORIES} kategórií s oslovením (predajné výsledky sú oneskorené a riedke)."),
    }
    return {"stratégie": strategies, "kategórie": categories, "spolu": row("spolu", total), "bandit": bandit}


def yield_markdown(report: dict[str, Any]) -> str:
    cols = ["zdroj", *COLUMNS]
    lines = ["# Zdroj → obchodný výsledok (M36)", "",
             "Výskumný lievik (dokumenty → overené tvrdenia) a obchodný lievik (hypotézy → … → ochota platiť) podľa "
             "stratégie. Pôvod hypotézy = dokument jej najstaršieho tvrdenia; TED sa ráta navyše.", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in [*report["stratégie"], report["spolu"]]:
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    lines += ["", f"**Bandit:** {report['bandit']['text']}", ""]
    return "\n".join(lines)
