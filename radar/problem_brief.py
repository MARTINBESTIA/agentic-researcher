"""Prehľad príležitosti – jedna strana na problém pre človeka, ktorý aplikáciu vidí prvýkrát (21. 10. 2026).

Skladá iba existujúce údaje (bez volania modelu): odkazy na overené zdroje, čo problém znamená (prehľad domény),
navrhnutý produkt ako hypotéza riešenia (problémová karta, návrhy prínosu, karty organizácií, konkurencia, kritika,
trh), čo je podložené dátami a čo nie (osi stavu, Brána 1, experimenty Brány 2), koho a s čím osloviť (Koho osloviť,
hlavná a vyvracajúce otázky, návrhy oslovení) a čo chýba s presnou akciou, ktorá to doplní. Nič nie je potvrdené
zákazníkom, kým to neukáže jeho odpoveď. Osoby sú predvolene pseudonymizované.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB

STEP_ACTION = {
    "zdroje": "chýba nezávislý dôkaz problému (iba dodávatelia alebo cudzí kontext) – Problémy → hypotéza → „Otestovať "
              "teraz“ (test hypotézy hľadá aj nezávislé zdroje), hlavne však rozhovor so zákazníkom",
    "doména": "Brána 0 → Doména a prínos → „Pripraviť prehľad, návrh prínosu a návrhy oslovení“ (krok value)",
    "produkt": "Správca akcií → „Problémové karty a rozdelenie hypotéz“ (karta s výstupom) a krok value (návrh prínosu)",
    "kontakty": "Problémy → „Hľadať firmy a ľudí k tomuto problému“ (akcia people) – kto a s akým kontaktom",
    "oslovenia": "Správca akcií → „Pripraviť návrhy oslovení“ – návrhy na tvoju kontrolu, odosielaš ty",
    "dáta": "Správca akcií → „Brána 1: prieskum dát“ – zdroje dát a ich pripravenosť",
    "karty": "Brána 0 → Doména a prínos → Karta organizácie – ponuka pre konkrétnu organizáciu",
    "ochota platiť": "pri rozhovore (nie v prvom e-maile) over stupne v časti 6 a zapíš ich s dôkazom do výsledku "
                     "oslovenia (GUI Oslovenia → Zapísať výsledok → Ochota platiť) – potvrdená je až záujem o kúpu za "
                     "konkrétnych podmienok alebo platba",
}


def _cut(text: Any, n: int = 220) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cell(text: Any, n: int = 160) -> str:
    return _cut(text, n).replace("|", "/")


def completeness(cfg: Config, db: DB, hid: int) -> dict[str, Any]:
    """Čo k problému existuje (počty) a čo chýba – pre prehľad aj tabuľku všetkých problémov."""
    from .commercial import wtp_state
    from .shortlist import contact_shortlist
    from .strength import evidence_strength
    from .value import domain_of

    hyp = db.get_hypothesis(hid)
    label, verified, _, domains = evidence_strength(db.evidence_for(hid))
    brief = db.latest_brief(domain_of(db, hid)[0])
    card = db.latest_card(hid)
    proposals = [p for p in db.value_proposals_for(hid) if p["status"] in ("checked", "approved", "pending_review")]
    try:
        org_cards = db.org_cards_for(hid)
    except Exception:  # noqa: BLE001 – staršia schéma
        org_cards = []
    rows = contact_shortlist(cfg, db, hid) if hyp is not None and hyp["people_enabled"] else []
    reachable = [r for r in rows if r["odporúčanie"] in ("oslov najprv", "oslov")]
    drafts = [o for o in db.outreach_list(hid) if o["status"] in ("draft", "needs_edit", "approved")]
    done_exps = [e for e in db.experiments_for(hid) if e["status"] == "done"]
    # zdroje = nezávislý dôkaz problému (nie dodávatelia ani cudzí kontext) – rovnako ako v časti 2 prehľadu
    have = {"zdroje": independent_problem_evidence(cfg, db, hid) > 0, "doména": brief is not None,
            "produkt": card is not None or bool(proposals),
            "kontakty": bool(reachable), "oslovenia": bool(drafts),
            "ochota platiť": wtp_state(db, hid)["potvrdená"],
            "dáta": (hyp["gate1_status"] if hyp is not None else None) not in (None, "not_started"),
            "karty": bool(org_cards)}
    all_verified = sum(1 for e in db.evidence_for(hid) if e["status"] == "verified")
    return {"sila": label, "overené": verified, "overené_všetky": all_verified, "domény": domains, "prehľad": brief,
            "karta": card,
            "návrhy": proposals, "karty_org": org_cards, "riadky": rows, "osloviteľní": reachable, "návrhy_oslovení": drafts,
            "experimenty": done_exps, "má": have, "chýba": [k for k in ("zdroje", "doména", "produkt", "kontakty",
                                                                         "oslovenia", "ochota platiť", "dáta")
                                                              if not have[k]],
            "skóre": sum(have[k] for k in ("zdroje", "doména", "produkt", "kontakty", "oslovenia"))}


def _sources(cfg: Config, db: DB, hid: int) -> dict[str, list]:
    """Všetky overené citáty hypotézy rozdelené podľa pôvodu a významu (nezávislý dôkaz problému / riešenia
    a dodávatelia / protiargumenty / iba kontext) – na počty aj na zobrazenie."""
    from .relevance import assess_target, select_buckets
    from .value import domain_target

    claims = [e for e in db.evidence_for(hid) if e["status"] == "verified"]
    pairs = assess_target(cfg, db, domain_target(db, hid), claims)
    return select_buckets(pairs, limit=500, per_doc=500)


def independent_problem_evidence(cfg: Config, db: DB, hid: int) -> int:
    b = _sources(cfg, db, hid)
    return len(b.get("process_problem") or []) + len(b.get("organization") or [])


def problem_brief(cfg: Config, db: DB, hid: int, with_names: bool = False) -> str:
    """Prehľad príležitosti ako Markdown (GUI, export, príkaz problem-brief)."""
    from .cards import FIELD_LABELS, OUTPUT_LABELS
    from .commercial import QUESTIONS as WTP_QUESTIONS
    from .commercial import STAGE_LABEL, org_stage_label
    from .dossier import Scrubber
    from .domain import STATUS_LABEL
    from .exp_tasks import verdict
    from .progress import axes
    from .relevance import MEANINGS, PUBLISHER_LABEL
    from .value import KIND_TEXT, domain_of

    hyp = db.get_hypothesis(hid)
    if hyp is None:
        raise ValueError(f"Hypotéza H{hid} neexistuje.")
    c = completeness(cfg, db, hid)
    key, dom_title, members = domain_of(db, hid)
    card = json.loads(c["karta"]["card_json"] or "{}") if c["karta"] is not None else {}
    buckets = _sources(cfg, db, hid)
    n_problem = len(buckets.get("process_problem") or []) + len(buckets.get("organization") or [])
    n_solution = len(buckets.get("solutions") or [])
    n_counter = len(buckets.get("counter") or [])
    n_context = len(buckets.get("context") or [])
    out = [f"# Prehľad príležitosti H{hid}: {hyp['title']}", "",
           f"Segment: **{hyp['segment'] or '–'}** · doména: {dom_title} ({len(members)} hypotéz) · test hypotézy: "
           f"{(json.loads(hyp['validation_json'] or '{}') or {}).get('verdikt', hyp['validation_state'] or 'neotestovaná')}"
           f" · Brána 1: {hyp['gate1_status'] or 'nezačatá'}", "",
           "> Návrh na overenie, nie overený obchod. Citáty sú overené kódom (doslova v zdroji); potrebu konkrétnej "
           "organizácie potvrdí iba jej odpoveď.", "",
           f"**Stav:** nezávislý dôkaz problému {n_problem} · dodávatelia a riešenia {n_solution} · popis domény "
           f"{'áno' if c['má']['doména'] else 'nie'} · navrhnutý produkt {'áno' if c['má']['produkt'] else 'nie'} · "
           f"kontakty na oslovenie {len(c['osloviteľní'])} · návrhy oslovení {len(c['návrhy_oslovení'])} · "
           f"karty organizácií {len(c['karty_org'])}", ""]

    # 1 problém
    out += ["## 1. Problém jednou vetou", ""]
    if card:
        out += [f"- **{FIELD_LABELS[k]}:** {_cut((card.get(k) or {}).get('text'), 400) or '–'}"
                for k in ("problem", "who", "current_practice", "cost_of_error")]
    else:
        out.append(f"- {_cut(hyp['description'] or hyp['title'], 500)}")
    out.append("")

    # 2 zdroje
    from .relevance import BUCKETS

    out += [f"## 2. Odkazy na zdroje (overené citáty: dôkaz problému {n_problem}, dodávatelia a riešenia {n_solution}, "
            f"protiargumenty {n_counter}, iba kontext {n_context})", ""]
    if not n_problem:
        out += ["> ⚠ **Nezávislý dôkaz problému chýba.** Citáty o probléme sú od dodávateľov riešení (marketing – ich "
                "tvrdenia o úsporách nie sú dôkaz) alebo z iného kontextu. Problém zatiaľ tvrdia hlavne tí, čo predávajú "
                "riešenie – rozhovor so zákazníkom je o to dôležitejší.", ""]
    show = {"process_problem": 6, "organization": 4, "solutions": 5, "counter": 3}
    for name in ("process_problem", "organization", "solutions", "counter"):
        items = (buckets.get(name) or [])[: show[name]]
        if not items:
            continue
        more = len(buckets.get(name) or []) - len(items)
        out += [f"**{BUCKETS[name].capitalize()}**" + (f" (ďalších {more} v GUI → Problémy → Dôkazy)" if more > 0 else ""), ""]
        for e, a in items:
            out.append(f"- „{_cut(e['quote'], 260)}“ — [{e['domain']}]({e['url']}) "
                       f"({PUBLISHER_LABEL.get(a['publisher_type'], a['publisher_type'] or 'zdroj')}, "
                       f"{MEANINGS.get(a['meaning'], a['meaning'])}, stiahnuté {str(e['fetched_at'] or '')[:10]}) [C{e['id']}]")
        out.append("")
    if n_context:
        out += [f"_{n_context} citátov je iba kontext (iný program alebo typ aktéra) – nie sú dôkazom procesu._", ""]
    if not c["overené"]:
        out += ["- zatiaľ žiadny overený citát", ""]

    # 3 doména
    out += ["## 3. Čo ten problém znamená (doména)", ""]
    brief_row = c["prehľad"]
    if brief_row is None:
        out += [f"- Prehľad domény ešte nevznikol → {STEP_ACTION['doména']}.", ""]
    else:
        b = json.loads(brief_row["brief_json"] or "{}")
        state = db.research_get(key) if hasattr(db, "research_get") else None
        out.append(f"_Prehľad v{brief_row['version']} z {brief_row['created_at'][:10]}, stav {brief_row['status']}"
                   + (f", podklady: {STATUS_LABEL.get(state['status'], state['status'])}" if state is not None else "")
                   + ". Všeobecný pre odvetvie – nie fakt o konkrétnej organizácii._")
        out.append("")
        steps = [s.get("text") for s in b.get("process_steps") or [] if s.get("text")]
        if steps:
            out += ["**Ako proces prebieha:**", ""] + [f"{i}. {_cut(t, 260)}" for i, t in enumerate(steps, 1)] + [""]
        terms = [t for t in b.get("terms") or [] if t.get("term")]
        if terms:
            out += ["**Pojmy:**", ""] + [f"- **{t['term']}** – {_cut(t.get('explanation'), 220)}" for t in terms] + [""]
        roles = b.get("roles") or {}
        role_lines = [f"- {label}: {_cut((roles.get(k) or {}).get('text'), 200)}" for k, label in
                      (("user", "kto s problémom pracuje"), ("data_steward", "kto spravuje dáta"),
                       ("decision_maker", "kto rozhoduje o rozpočte")) if (roles.get(k) or {}).get("text")]
        if role_lines:
            out += ["**Roly:**", ""] + role_lines + [""]
        for k, label in (("decision", "Rozhodnutie, ktoré má výstup podporiť"), ("current_practice", "Dnes to robia takto")):
            if (b.get(k) or {}).get("text"):
                out += [f"**{label}:** {_cut(b[k]['text'], 400)}", ""]
        pitfalls = [d for d in b.get("data_needed") or [] if d.get("field")]
        if pitfalls:
            out += ["**Dáta a úskalia ich významu:**", ""] + [
                f"- {d['field']}: {_cut(d.get('meaning_pitfall'), 200)}" for d in pitfalls] + [""]
        if b.get("unknowns"):
            out += ["**Čo zdroje nehovoria (neznáme):**", ""] + [f"- {_cut(u, 220)}" for u in b["unknowns"][:5]] + [""]

    # 4 produkt
    out += ["## 4. Navrhnutý produkt (hypotéza riešenia – na overenie)", ""]
    if not card and not c["návrhy"] and not c["karty_org"]:
        out.append(f"- Zatiaľ bez návrhu produktu → {STEP_ACTION['produkt']}.")
    if card:
        out += [f"- **Výstup, ktorý pomôže:** {OUTPUT_LABELS.get(card.get('helpful_output'), card.get('helpful_output'))} – "
                f"{_cut(card.get('helpful_output_text'), 400)}",
                f"- **Rozhodnutie, ktoré podporí:** {_cut((card.get('decision') or {}).get('text'), 300) or '–'}",
                f"- **Obchodná metrika:** {_cut(card.get('business_metric'), 300) or '–'}",
                f"- **Čo by nápad vyvrátilo:** {_cut(card.get('falsification'), 300) or '–'}"]
    if c["návrhy"]:
        best: dict[str, Any] = {}
        for p in c["návrhy"]:
            best.setdefault(p["recipient_kind"], p)
        out += ["", "**Malý dodateľný výstup podľa adresáta** (návrh prínosu – potreba je hypotéza):", ""]
        for kind, p in best.items():
            d = json.loads(p["proposal_json"] or "{}")
            out.append(f"- *{KIND_TEXT.get(kind, kind).split(' (')[0]}* ({p['status']}): {_cut(d.get('deliverable'), 260)} · "
                       f"pomôže rozhodnúť: {_cut(d.get('decision_supported'), 160)} · minimálny vstup: "
                       f"{_cut(d.get('minimum_input'), 160)} · obmedzenia: {_cut(d.get('limitations'), 160)}")
    for row in c["karty_org"][:3]:
        oc = json.loads(row["card_json"] or "{}")
        opts = "; ".join(f"{o.get('title')} – {_cut(o.get('mechanism'), 120)}" for o in oc.get("options") or [])
        out.append(f"- *karta organizácie {row['org_name']} v{row['version']}* ({row['status']}): ponuka "
                   f"„{_cut((oc.get('offer') or {}).get('deliverable'), 200)}“; možnosti: {opts or '–'}")
    sols = [s.get("text") for s in (json.loads(brief_row["brief_json"] or "{}").get("existing_solutions") or [])
            if s.get("text")] if brief_row is not None else []
    vendor = [e for e, a in (buckets.get("solutions") or [])[:4]]
    if sols or vendor:
        out += ["", "**Konkurencia a existujúce riešenia** (prečo to možno už vyriešené je):", ""]
        out += [f"- {_cut(s, 240)}" for s in sols[:4]]
        out += [f"- „{_cut(e['quote'], 200)}“ — [{e['domain']}]({e['url']})" for e in vendor[:4]]
    crit = db.latest_critique(hid)
    if crit:
        counter = (crit["content"] or {}).get("counterarguments") or []
        if counter:
            out += ["", "**Prečo to nemusí fungovať (kritika):**", ""] + [f"- {_cut(x, 240)}" for x in counter[:4]]
    market = db.latest_market_estimate(hid)
    if market:
        rng = (f"{market['low']:,.0f} – {market['high']:,.0f} {market['unit']}".replace(",", " ") if market["complete"]
               else "neúplný (niektorý faktor nemá overený zdroj)")
        out += ["", f"**Hrubý odhad trhu:** {rng} – rádový odhad, nie číslo do biznis plánu."]
    out.append("")

    # 5 dáta
    ax = axes(cfg, db, hid)
    out += ["## 5. Čo je podložené dátami a čo nie", "", "| oblasť | stav | z čoho |", "|---|---|---|"]
    out += [f"| problém v zdrojoch | {n_problem} nezávislých citátov (pôvodná sila: {ax['literatúra']['stav']}) | "
            f"{n_solution} citátov od dodávateľov sa nerátajú – výber rozhovorov, nie dôkaz trhu |",
            f"| zákazník | {ax['zákazník']['stav']} | rozhovory {ax['zákazník']['zložky']['rozhovory']} z cieľa "
            f"{ax['zákazník']['zložky']['cieľ']} |",
            f"| dáta (Brána 1) | {ax['dáta']['stav']} | kľúčové polia {ax['dáta']['zložky']['kľúčové_polia']}, "
            f"datasety {len(db.datasets_for(hid, only_ok=True))} |",
            f"| ochota platiť | {ax['ochota_platiť']['stav']} | {_cell(ax['ochota_platiť']['zložky']['dôkazy'])}; "
            f"platiteľ: {_cell(ax['ochota_platiť']['zložky']['platiteľ'], 80)} – potvrdená je až záujem o kúpu alebo platba |"]
    from .procurement import signals

    ps = signals(cfg, db, hid)
    if ps.get("zákazky"):
        rep = ps["opakovaní_kupujúci"]
        out.append(f"| obstarávanie (TED) | {ps['zákazky']} zákaziek u {ps['kupujúci']} kupujúcich, ktoré podľa posúdenia "
                   f"riešia problém; opakovane {len(rep)} | otvorené teraz {len(ps['otvorené_teraz'])}, plánované "
                   f"{len(ps['plánované'])} (Problémy → Obstarávanie, over pri každej zákazke) |")
    else:
        out.append("| obstarávanie (TED) | " + ("žiadna zákazka nad limitmi EÚ, ktorá by riešila problém"
                                               if ps.get("porovnané") else "neporovnané")
                   + " | akcia „Obstarávanie z TED“ |")
    for e in c["experimenty"][:4]:
        v = verdict(json.loads(e["result_json"] or "{}"), json.loads(e["flags_json"] or "{}"))
        out.append(f"| experiment E{e['id']} (Brána 2) | {v['verdikt']} | {_cell(e['statement'], 140)} |")
    out.append("")

    # 6 koho osloviť
    out += ["## 6. Koho osloviť a s čím", ""]
    rows = sorted(c["osloviteľní"], key=lambda r: (not r.get("prvé_oslovenie"), r["odporúčanie"] != "oslov najprv",
                                                   -(r["priorita"] or 0)))
    if not rows:
        out += [f"- Zatiaľ nikto s kontaktom → {STEP_ACTION['kontakty']}.", ""]
    else:
        out += ["Postup: najprv si prečítaj časť 3, potom osloviť v tomto poradí (jedna osoba na organizáciu, odosielaš "
                "ručne). Cieľ prvého kontaktu: overiť, či problém u nich nastal – nie predávať.", "",
                "| # | komu | organizácia | prečo práve oni | s čím (čo žiadať) | kanál | stav |", "|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows[:6], 1):
            who = (r["osoba"] if with_names else (f"Osoba P{r['person_id']}" if r["person_id"] else "funkčný kontakt"))
            channel = ", ".join(x for x, ok in (("e-mail", r["e-mail"]), ("telefón", r["telefón"])) if ok) or "–"
            warn = " ⚠ obstarávanie neoverené" if r.get("obstarávanie") == "neoverené" else ""
            out.append(f"| {i} | {r['typ']}: {_cell(r['funkcia'] or r['účel'] or '', 60)} {who} | {_cell(r['organizácia'], 50)} | "
                       f"{_cell(r['prečo'] or r['účel'], 120)} | {_cell(r['čo_žiadať'], 140)} | {channel} | "
                       f"{r['stav_komunikácie']}; ochota platiť: {org_stage_label(db, hid, r.get('org_id'))}{warn} |")
        rest = Counter(r["odporúčanie"] for r in c["riadky"])
        out += ["", f"Ďalej: doplň kontakt {rest.get('doplň kontakt', 0)}, preveriť rolu {rest.get('preveriť rolu', 0)}, "
                    f"neoslovovať {rest.get('neoslovovať', 0)} (dôvody v Brána 0 → Koho osloviť).", ""]
    questions = []
    if c["karty_org"]:
        mq = (json.loads(c["karty_org"][0]["card_json"] or "{}").get("main_question") or {}).get("text")
        if mq:
            questions.append(("hlavná otázka z karty organizácie", mq))
    try:
        from .domain import main_question

        main = main_question(db, key, "customer")
        if main is not None:
            questions.append(("hlavná otázka domény", main["question"]))
    except Exception:  # noqa: BLE001
        pass
    if brief_row is not None:
        for q in json.loads(brief_row["brief_json"] or "{}").get("falsifying_questions") or []:
            questions.append(({"problem_exists": "vzniká problém?", "already_solved": "nie je už vyriešený?",
                               "data_enables": "pomôžu dáta?"}.get(q.get("aspect"), "otázka"), q.get("question")))
    seen_q: set[str] = set()
    questions = [(lb, q) for lb, q in questions if q and not (q.strip().lower() in seen_q or seen_q.add(q.strip().lower()))]
    if questions:
        out += ["**S čím (otázky na posledný konkrétny prípad):**", ""] + [
            f"- *{label}:* {_cut(q, 260)}" for label, q in questions[:6]] + [""]
    out += ["**Ako overiť ochotu platiť** (pri rozhovore, nie v prvom e-maile; nepýtaj sa „kúpili by ste…“ – pýtaj sa "
            "na dnešné náklady a konkrétny ďalší krok; každý stupeň zapíš s dôkazom do výsledku oslovenia):", ""] + [
        f"{i}. *{STAGE_LABEL[st]}:* {q}" for i, (st, q) in enumerate(WTP_QUESTIONS, 1)] + [""]
    if c["návrhy_oslovení"]:
        states = Counter(o["readiness"] or o["status"] for o in c["návrhy_oslovení"])
        out += [f"Návrhy oslovení: {', '.join(f'{k} {v}' for k, v in states.items())} → GUI Oslovenia (odosielaš ty).", ""]

    # 7 chýba
    out += ["## 7. Čo chýba a ďalší krok", ""]
    if c["chýba"]:
        out += [f"- **{k}:** {STEP_ACTION[k]}" for k in c["chýba"]]
    else:
        out.append("- Reťazec je úplný. Ďalej: odoslať prvé oslovenia, zapísať odpovede (GUI Oslovenia → výsledok) a podľa "
                   "nich spresniť kartu organizácie.")
    if not c["má"]["karty"] and c["osloviteľní"]:
        out.append(f"- **ponuka pre konkrétnu organizáciu:** {STEP_ACTION['karty']}")
    out.append("")
    text = "\n".join(out) + "\n"
    if not with_names:
        text = Scrubber([p["full_name"] for p in db._all("SELECT full_name FROM people")]).text(text)
    return text


def problems_index(cfg: Config, db: DB, ids: list[int] | None = None) -> str:
    """Tabuľka všetkých problémov Brány 0: čo je hotové, čo chýba – zoradené od najúplnejších."""
    hyps = [db.get_hypothesis(i) for i in ids] if ids else list(db.people_enabled_hypotheses())
    rows = []
    for h in [x for x in hyps if x is not None]:
        c = completeness(cfg, db, h["id"])
        rows.append((c["skóre"], c["overené"], h, c))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]["id"]))
    out = ["# Prehľad problémov Brány 0", "",
           "Od najúplnejších: zdroje → popis domény → navrhnutý produkt → kontakty → návrhy oslovení. Detail každého "
           "problému je v súbore `H<číslo>.md` (python -m radar problem-brief --all). Stĺpec „nezávislé“ = citáty o "
           "probléme mimo dodávateľov riešení a mimo cudzieho kontextu; 0 znamená, že problém zatiaľ tvrdia hlavne predajcovia.",
           "", "| H | problém | nezávislé / všetky citáty | doména | produkt | kontakty | oslovenia | Brána 1 | chýba |",
           "|---|---|---|---|---|---|---|---|---|"]
    for score, _, h, c in rows:
        out.append(f"| [H{h['id']}](H{h['id']}.md) | {_cell(h['title'], 90)} | "
                   f"{independent_problem_evidence(cfg, db, h['id'])} / {c['overené_všetky']} | "
                   f"{'áno' if c['má']['doména'] else '–'} | {'karta' if c['karta'] is not None else ''}"
                   f"{' + návrh' if c['návrhy'] else ''}{'' if c['má']['produkt'] else '–'} | {len(c['osloviteľní'])} | "
                   f"{len(c['návrhy_oslovení'])} | {h['gate1_status'] or 'nezačatá'} | {', '.join(c['chýba']) or '–'} |")
    return "\n".join(out) + "\n"


def write_briefs(cfg: Config, db: DB, out_dir: Path, ids: list[int] | None = None, with_names: bool = False) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    hyps = ids or [h["id"] for h in db.people_enabled_hypotheses()]
    paths = []
    for hid in hyps:
        p = out_dir / f"H{hid}.md"
        p.write_text(problem_brief(cfg, db, hid, with_names), encoding="utf-8")
        paths.append(p)
    idx = out_dir / "INDEX.md"
    idx.write_text(problems_index(cfg, db, hyps), encoding="utf-8")
    return [idx] + paths
