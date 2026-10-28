"""Brána 0 → Doména a prínos → Karta organizácie (zadanie 17. 10. 2026, časť GUI).

Výber organizácie a roly, päť odpovedí, tri pripravenosti, ďalší krok; dôkazy, medzery a technické údaje
v rozbaľovacích sekciách. Akcie: vytvorenie/obnova karty (úloha na pozadí – žiadne volanie modelu pri prekreslení),
doplnenie medzery, ručná oprava, výber skúšky, priradenie experimentu, schválenie verzie, zápis odpovede a sústredený
režim (najviac `org_cards.focus_max_orgs` organizácie).
"""

from __future__ import annotations

import json

import streamlit as st

from radar.gui.common import esc, json_or, refresh, start_job_button, willingness_inputs


def render(c, d, hid: int) -> None:
    from radar.opportunity import (CARD_STATUS, DECISIONS, INTERVENTIONS, RESEARCH_STATUS, ROLE_LABEL, ROLES, SOURCES,
                                   approve_card, card_answers, card_markdown, current_card, edit_card, focus_settings,
                                   link_experiment, refresh_readiness, request_card, select_trial, set_focus)
    from radar.outcomes import DECLINE_REASONS, FLAGS, RELEVANCE, REPLIES, STRUCTURED, OutcomeError, record_outcome
    from radar.relevance import assessment_rows
    from radar.shortlist import contact_shortlist

    st.markdown("**Karta organizácie** – organizácia × hypotéza × pracovný prípad × rola adresáta")
    st.caption("Karta dedí prehľad domény, ale všeobecné tvrdenie o odvetví ostáva pre organizáciu hypotézou. Každý "
               "výrok má stav poznania (zo zdroja / uviedol kontakt / pozorované v dátach / hypotéza / návrh / nevieme). "
               "Kontrola modelom, pokrytie podkladov a pripravenosť na akciu sú zobrazené oddelene. Nič sa neodosiela.")
    rows = contact_shortlist(c, d, hid)
    orgs: dict[int, dict] = {}
    for r in rows:
        if r["org_id"] and r["org_id"] not in orgs:
            orgs[r["org_id"]] = {"label": f"{r['organizácia']} · {r['typ']} · {r['odporúčanie']}",
                                 "role": r["skupina"] if r["skupina"] in ROLES else "customer"}
    for card in d.org_cards_for(hid):
        orgs.setdefault(card["organization_id"], {"label": f"{card['org_name']} · karta", "role": card["recipient_role"]})
    focus = focus_settings(d)
    with st.expander(f"Sústredený režim prípravy oslovení ({'zapnutý' if focus.get('enabled') else 'vypnutý'})",
                     icon=":material/center_focus_strong:"):
        st.caption(f"Najviac {c.org_cards.focus_max_orgs} organizácie naraz. Autopilot potom najprv dopĺňa ich karty, "
                   "medzery a návrhy oslovení; nový všeobecný zber iba zo zvyšného času. Hypotézy ani plán sa nemažú.")
        mine = [it["org_id"] for it in focus.get("items") or [] if it["hypothesis_id"] == hid]
        others = [it for it in focus.get("items") or [] if it["hypothesis_id"] != hid]
        with st.form(f"g0_focus_{hid}"):
            on = st.toggle("Zapnúť sústredený režim", value=bool(focus.get("enabled")))
            pick = st.multiselect("Organizácie tejto hypotézy", list(orgs), default=[o for o in mine if o in orgs],
                                  format_func=lambda o: orgs[o]["label"])
            if others:
                st.caption("Ďalšie v zozname (iné hypotézy): " + ", ".join(f"org {o['org_id']} (H{o['hypothesis_id']})"
                                                                           for o in others))
            if st.form_submit_button("Uložiť", icon=":material/save:"):
                try:
                    set_focus(c, d, on, others + [{"org_id": o, "hypothesis_id": hid, "role": orgs[o]["role"]} for o in pick])
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    refresh()
    if not orgs:
        st.info("Zatiaľ žiadna organizácia v „Koho osloviť“.", icon=":material/info:")
        return
    org_id = st.selectbox("Organizácia", list(orgs), format_func=lambda o: orgs[o]["label"], key=f"g0_card_org_{hid}")
    role = st.segmented_control("Rola adresáta", list(ROLES), default=orgs[org_id]["role"], format_func=ROLE_LABEL.get,
                                key=f"g0_card_role_{hid}_{org_id}") or orgs[org_id]["role"]
    cur = current_card(d, org_id, hid, role)
    with st.container(horizontal=True):
        if st.button("Vytvoriť / obnoviť kartu", icon=":material/note_add:", key=f"g0_card_req_{hid}_{org_id}_{role}",
                     help="Zaradí kartu na spracovanie; spusti potom akciu nižšie (úloha na pozadí, dá sa zrušiť). Nová "
                          "verzia vznikne iba pri zmene vstupov – pri zastaranej karte aj pri rovnakých."):
            request_card(d, org_id, hid, role, force=bool(cur is not None and cur["stale_reason"]))
            st.toast("Karta je vyžiadaná – spusti akciu „Karty organizácií a ponuky“.", icon=":material/check:")
        start_job_button("org_cards", "Spustiť akciu Karty organizácií", 30, [hid], key=f"g0_card_job_{hid}",
                         icon=":material/play_arrow:")
    if cur is None:
        st.info("Pre túto organizáciu a rolu ešte nie je karta. Oslovenie bez karty dostane iba situačnú otázku, nie "
                "personalizovanú ponuku.", icon=":material/info:")
        return
    history = d.card_history(org_id, hid, cur["process_key"], role)
    ver = st.selectbox("Verzia karty", [r["id"] for r in history], key=f"g0_card_ver_{cur['id']}",
                       format_func=lambda i: next(f"v{r['version']} · {r['created_at'][:10]} · "
                                                  f"{CARD_STATUS.get(r['status'], r['status'])} · {r['author']}"
                                                  for r in history if r["id"] == i))
    row = d.get_org_card(ver)
    card = json.loads(row["card_json"] or "{}")
    card["_selected_option"] = row["selected_option"]
    ready = json_or(row["readiness_json"], {})
    if row["stale_reason"]:
        st.warning(f"Karta je zastaraná: {row['stale_reason']}. Obnoví sa iba na tvoju žiadosť (tlačidlo vyššie).",
                   icon=":material/update:")
    if row["conflict_json"]:
        st.error("Rozpor: " + str(json_or(row["conflict_json"], {}).get("text", "")), icon=":material/report:")
    ans = card_answers(card, row["org_name"], row["recipient_role"])
    with st.container(border=True):
        st.markdown(f"1. **Komu pomáham:** {esc(ans['komu'])}\n2. **V akom úkone:** {esc(ans['úkon'])}\n"
                    f"3. **Čo sa zmení:** {esc(ans['zmena'])}\n4. **Minimálny vstup:** {esc(ans['vstup'])}\n"
                    f"5. **Ako overíme prínos:** {esc(ans['overenie'])}")
    cols = st.columns(4)
    for col, (k, label) in zip(cols, (("rozhovor", "Na rozhovor"), ("skúška", "Na návrh skúšky"),
                                      ("prínos", "Na tvrdenie o prínose"), ("ponuka", "Na ponuku"))):
        r = ready.get(k) or {}
        with col.container(border=True):
            st.markdown(f"**{label}:** {'áno' if r.get('pripravené') else 'nie'}")
            for x in r.get("dôvody") or []:
                st.caption(x)
            for x in r.get("chýba") or []:
                st.caption("chýba: " + x)
            if k == "skúška":
                st.caption(f"súhlas: {'áno' if r.get('súhlas') else 'nie'} · dáta: {'áno' if r.get('dáta') else 'nie'}")
    if ready.get("ďalší_krok"):
        st.info("Ďalší krok: " + ready["ďalší_krok"], icon=":material/arrow_forward:")
    checks = json_or(row["checks_json"], {})
    rev = checks.get("hodnotiteľ") or {}
    st.caption(f"Kontrola: kód {len(checks.get('problémy') or [])} výhrad, zoslabené výroky "
               f"{len(checks.get('zoslabené') or [])} · hodnotiteľ – opora: {(rev.get('support') or {}).get('verdict', '-')}, "
               f"použiteľnosť: {(rev.get('usability') or {}).get('verdict', '-')} · stav "
               f"{CARD_STATUS.get(row['status'], row['status'])}")
    md = card_markdown(d, row)
    with st.expander("Celá karta", icon=":material/description:"):
        with st.container(height=480):
            st.markdown(md.replace("](", "]\\("))
        st.download_button("Stiahnuť kartu (Markdown)", md, file_name=f"karta_org{org_id}_H{hid}_{row['process_key']}_"
                           f"v{row['version']}.md", mime="text/markdown", icon=":material/download:",
                           key=f"g0_card_dl_{row['id']}")
    with st.expander("Hodnotenie karty a kontroly", icon=":material/rule:"):
        st.dataframe(json_or(row["evaluation_json"], []), hide_index=True, alt="Hodnotenie položiek karty",
                     column_order=("položka", "stav", "poznámka"))
        for p in (checks.get("problémy") or []) + (checks.get("zoslabené") or []):
            st.caption("• " + p)
        for part in ("support", "usability"):
            for p in (rev.get(part) or {}).get("problems") or []:
                st.caption(f"• hodnotiteľ ({'opora' if part == 'support' else 'použiteľnosť'}): „{p.get('sentence')}“ – "
                           f"{p.get('issue')}")
    with st.expander("Dôkazy a ich relevantnosť pre túto organizáciu", icon=":material/fact_check:"):
        st.dataframe(assessment_rows(d, f"card:{org_id}:{hid}"), hide_index=True,
                     alt="Posúdenie dôkazov voči organizácii: význam, relevantnosť, stav potreby")
    tasks = d.research_tasks_for(row["id"])
    with st.expander(f"Medzery a cielený výskum ({sum(t['status'] == 'open' for t in tasks)} otvorených)",
                     icon=":material/travel_explore:"):
        st.caption("Verejné zdroje: agenda, služby, kompetencie, postupy, funkcie nástrojov. Interné časy, skutočné "
                   "používanie a dôvod zdržania idú na kontakt alebo dáta. Verejné mlčanie nie je dôkaz, že niečo neexistuje.")
        if tasks:
            st.dataframe([{"id": t["id"], "otázka": t["question"], "zmení": DECISIONS.get(t["decision_changed"]),
                           "zdroj": SOURCES.get(t["best_source"]), "stav": RESEARCH_STATUS.get(t["status"], t["status"]),
                           "odpoveď": json_or(t["result_json"], {}).get("answer", ""), "ďalej": t["next_step"] or ""}
                          for t in tasks], hide_index=True, alt="Výskumné úlohy karty")
            with st.form(f"g0_gap_{row['id']}"):
                tid = st.selectbox("Medzera", [t["id"] for t in tasks],
                                   format_func=lambda i: next(t["question"][:90] for t in tasks if t["id"] == i))
                answer = st.text_area("Odpoveď (napr. z rozhovoru) – prázdne = znova hľadať vo verejných zdrojoch")
                src = st.text_input("Zdroj odpovede (napr. „rozhovor 20. 10.“ alebo URL)")
                if st.form_submit_button("Uložiť / znova hľadať", icon=":material/save:"):
                    if answer.strip():
                        d.update_research_task(tid, status="found", result_json={"answer": answer.strip(), "zdroj":
                                                                                 src.strip() or "človek", "človek": True},
                                               next_step="použiť v ďalšej verzii karty")
                        d.mark_org_cards_stale("doplnená medzera (človek)", card_ids=[row["id"]])
                    else:
                        d.update_research_task(tid, status="open", attempts=0, next_step="znova hľadať")
                        request_card(d, org_id, hid, role)
                    refresh()
    options = card.get("options") or []
    if options:
        with st.expander("Možnosti riešenia a výber skúšky", icon=":material/science:"):
            for i, o in enumerate(options):
                st.markdown(f"{i + 1}. **{esc(o.get('title'))}** ({INTERVENTIONS.get(o.get('type'), o.get('type'))}) – "
                            f"{esc(o.get('mechanism'))}")
            pick = st.selectbox("Vybraná skúška", list(range(len(options))), index=row["selected_option"] or 0,
                                format_func=lambda i: options[i].get("title") or f"možnosť {i + 1}",
                                key=f"g0_trial_{row['id']}")
            if st.button("Vybrať túto skúšku", key=f"g0_trial_ok_{row['id']}", icon=":material/check:"):
                select_trial(c, d, row["id"], pick)
                refresh()
    exps = [x for x in d.experiments_for(hid) if x["org_id"] == org_id and x["result_json"]]
    if exps:
        with st.expander("Priradiť výsledok experimentu k úkonu karty", icon=":material/link:"):
            st.caption("Tvrdenie o prínose vyžaduje porovnanie s dnešným alebo jednoduchým postupom na dátach tejto "
                       "organizácie, správny význam dát a zhodu s úkonom karty. Syntetické dáta nestačia.")
            eid = st.selectbox("Experiment", [x["id"] for x in exps], format_func=lambda i: next(
                f"E{x['id']}: {x['statement'][:80]}" for x in exps if x["id"] == i), key=f"g0_exp_{row['id']}")
            if st.button("Priradiť", key=f"g0_exp_ok_{row['id']}"):
                link_experiment(d, eid, row["id"])
                refresh_readiness(c, d, row["id"])
                refresh()
    with st.expander("Ekonomika u zákazníka", icon=":material/payments:"):
        _economics(c, d, hid, org_id, cur["id"])
    with st.expander("Zapísať odpoveď organizácie", icon=":material/forum:"):
        st.caption("Odpoveď sa viaže na túto organizáciu a verziu karty; iným organizáciám ani odvetviu potrebu nemení. "
                   "„Bez odpovede“ nie je vyvrátenie. Výpoveď kontaktu nie je meraný výsledok.")
        with st.form(f"g0_reply_{row['id']}", clear_on_submit=True):
            reply = st.selectbox("Odpoveď", list(REPLIES), format_func=REPLIES.get)
            rel = st.selectbox("Problém", [None, *RELEVANCE], format_func=lambda x: "nezistené" if x is None else RELEVANCE[x])
            decline = st.selectbox("Dôvod nezáujmu", [None, *DECLINE_REASONS],
                                   format_func=lambda x: "–" if x is None else DECLINE_REASONS[x])
            structured = {k: st.text_input(label) for k, label in STRUCTURED.items()}
            flags = {k: st.checkbox(label) for k, label in FLAGS.items()}
            note = st.text_area("Poznámka (bez mien a kontaktov)")
            willingness = willingness_inputs(f"g0_reply_{row['id']}")
            contradicts = st.checkbox("Odpoveď odporuje karte")
            if st.form_submit_button("Zapísať", icon=":material/save:"):
                try:
                    record_outcome(c, d, hid=hid, org_id=org_id, reply=reply, problem_relevance=rel, note=note,
                                   card_id=row["id"], decline_reason=decline, structured=structured,
                                   contradicts=contradicts, willingness=willingness, **flags)
                except OutcomeError as exc:
                    st.error(str(exc))
                else:
                    refresh()
    with st.expander("Ručná oprava a schválenie", icon=":material/edit:"):
        st.caption("Oprava vytvorí novú verziu s autorom „človek“; pôvodná ostáva v histórii. Schválenie sa na novú "
                   "verziu neprenáša a nový rozpor po schválení ostane viditeľný.")
        with st.form(f"g0_edit_{row['id']}"):
            raw = st.text_area("Karta (JSON)", json.dumps({k: v for k, v in card.items() if not k.startswith("_")},
                                                         ensure_ascii=False, indent=1), height=300)
            note = st.text_input("Čo si opravil")
            if st.form_submit_button("Uložiť ako novú verziu", icon=":material/save:"):
                try:
                    edit_card(c, d, row["id"], json.loads(raw), note)
                except (ValueError, json.JSONDecodeError) as exc:
                    st.error(f"Neplatná karta: {exc}")
                else:
                    refresh()
        if st.button("Schváliť túto verziu", key=f"g0_card_ok_{row['id']}", icon=":material/verified:",
                     disabled=bool(row["approved_at"])):
            approve_card(d, row["id"])
            refresh()
        if row["approved_at"]:
            st.caption(f"Schválené {row['approved_at'][:16]}")
    with st.expander("Technické údaje", icon=":material/settings:"):
        st.json({"id": row["id"], "verzia": row["version"], "pracovný_prípad": row["process_key"],
                 "schéma": row["schema_version"], "prompt": row["prompt_version"], "politika": row["policy_version"],
                 "poskytovateľ": row["provider"], "model": row["model"], "hash_vstupov": row["inputs_hash"],
                 "prehľad_domény": row["brief_id"], "autor": row["author"]}, expanded=False)


def _economics(c, d, hid: int, org_id: int, card_id: int) -> None:
    """Faktory s pôvodom a zdrojom; rozpätia počíta Python (radar.economics), model sa nepoužíva."""
    from radar.economics import FACTORS, PROVENANCE, SAVING_USE, EconomicsError, economics, factor_rows, record
    from radar.opportunity import refresh_readiness

    e = economics(d, hid, org_id)
    st.caption("Objem prípadov, čas na prípad a cena práce dnes; úspora a náklady riešenia. Každý údaj s pôvodom: "
               "pozorované (citát z dokumentu – overí ho kód), uviedol zákazník (kto a kedy), odhad (ako vznikol) "
               "alebo nevieme. Úspora z webu dodávateľa sa nepoužije. Ponuka až s nákladovým údajom zákazníka.")
    with st.container(horizontal=True):
        st.metric("Hodiny ročne dnes", e["text"]["hodiny_ročne"], border=True)
        st.metric("Náklad dnes ročne", e["text"]["náklad_dnes"], border=True)
        st.metric("Ročná hodnota úspory", e["text"]["ročná_hodnota"], border=True)
        st.metric("Čistý prínos ročne", e["text"]["čistý_prínos"], border=True)
        st.metric("Návratnosť", e["text"]["návratnosť_mesiace"], border=True)
    (st.success if e["pripravené_na_ponuku"] else st.warning)(e["dôvod"], icon=":material/payments:")
    if e.get("varovanie"):
        st.warning(e["varovanie"], icon=":material/warning:")
    if e.get("návratnosť_poznámka"):
        st.caption(e["návratnosť_poznámka"])
    rows = factor_rows(d, hid, org_id)
    st.dataframe([{k: v for k, v in r.items() if k != "id"} for r in rows], hide_index=True,
                 alt="Faktory ekonomiky u zákazníka s pôvodom a zdrojom")
    if e["otázky"]:
        st.markdown("**Otázky do rozhovoru (neznáme alebo iba odhadnuté):**\n" + "\n".join(f"- {q}" for q in e["otázky"]))
    keys = [*FACTORS, "saving_use"]
    labels = {**{k: v["label"] for k, v in FACTORS.items()}, "saving_use": "využitie úspory"}
    with st.form(f"g0_econ_{hid}_{org_id}", clear_on_submit=True):
        factor = st.selectbox("Faktor", keys, format_func=labels.get)
        prov = st.segmented_control("Pôvod", list(PROVENANCE), format_func=PROVENANCE.get, default="customer_reported")
        col_lo, col_hi, col_use = st.columns(3)
        low = col_lo.number_input("Hodnota (od)", min_value=0.0, value=None, step=1.0)
        high = col_hi.number_input("do (pri rozpätí)", min_value=0.0, value=None, step=1.0)
        use = col_use.selectbox("Využitie úspory", list(SAVING_USE), format_func=SAVING_USE.get, index=None)
        evidence = st.text_input("Kto a kedy to povedal / ako vznikol odhad")
        doc_ref = st.text_input("Dokument (URL alebo ID) – pri pozorovanom údaji")
        quote = st.text_area("Doslovný citát z dokumentu", height=70)
        outcomes = {o["id"]: f"{o['recorded_at'][:10]} – {o['reply']}" for o in d.outcomes_for(hid) if o["org_id"] == org_id}
        outcome = st.selectbox("Výsledok oslovenia (voliteľné)", list(outcomes), format_func=outcomes.get, index=None)
        if st.form_submit_button("Zapísať faktor"):
            doc_id = None
            if doc_ref.strip():
                doc = d.get_document(int(doc_ref)) if doc_ref.strip().isdigit() else d.document_by_url(doc_ref)
                doc_id = doc["id"] if doc is not None else -1
            try:
                record(d, hid, org_id, factor, prov or "", low, high, evidence=evidence, quote=quote,
                       document_id=doc_id if doc_id != -1 else None, outcome_id=outcome, use=use)
                refresh_readiness(c, d, card_id)
                st.rerun()
            except EconomicsError as exc:
                st.error(str(exc) if doc_id != -1 else "Dokument sa v databáze nenašiel – " + str(exc))
    active = [r for r in rows if r["id"]]
    if active:
        drop = st.selectbox("Stiahnuť údaj (ostane v histórii)", [r["id"] for r in active], index=None,
                            format_func=lambda i: next(f"{r['faktor']}: {r['hodnota']} ({r['pôvod']})" for r in active
                                                       if r["id"] == i), key=f"g0_econ_drop_{hid}_{org_id}")
        if drop and st.button("Stiahnuť", key=f"g0_econ_drop_btn_{hid}_{org_id}"):
            d.withdraw_buyer_factor(drop)
            refresh_readiness(c, d, card_id)
            st.rerun()

