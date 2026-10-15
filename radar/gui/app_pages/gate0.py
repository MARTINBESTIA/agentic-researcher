from urllib.parse import quote

import streamlit as st

from radar.contact_scout import KIND_LABEL, target_queries, target_roles
from radar.crm import DECISION_STATUS, gate0_metrics, record_decision
from radar.gui.common import cfg, db, esc, json_or, pick_hypothesis, refresh, safe_url, start_job_button
from radar.shortlist import TIERS, contact_shortlist, shortlist_summary

d, c = db(), cfg()
hid = pick_hypothesis(d, "g0_h")
if hid is None:
    st.stop()
h = d.get_hypothesis(hid)

tab_who, tab_value, tab_metrics = st.tabs(["Koho osloviť", "Doména a prínos", "Metriky a rozhodnutie"])

with tab_who:
    if not c.privacy.lia_completed:
        st.warning("Hľadanie ľudí a kontaktov je zablokované, kým nepotvrdíš LIA (privacy.lia_completed).",
                   icon=":material/lock:")
    rows = contact_shortlist(c, d, hid)
    summary = shortlist_summary(rows)
    with st.container(horizontal=True):
        st.metric("Oslov najprv", summary["odporúčanie"].get("oslov najprv", 0), border=True,
                  help="Vlastný pracovný e-mail, silná väzba na problém a funkcia sedí.")
        st.metric("Oslov", summary["odporúčanie"].get("oslov", 0), border=True,
                  help="E-mail osoby alebo organizácie (pri generickom požiadaj o nasmerovanie).")
        st.metric("Doplň kontakt", summary["odporúčanie"].get("doplň kontakt", 0), border=True,
                  help="Iba telefón, skrytý e-mail (otvor stránku ručne) alebo LinkedIn.")
        st.metric("S e-mailom", summary["s_e-mailom"], border=True, help="Unikátne e-maily (spoločný sa ráta raz), bez uzavretých.")
        st.metric("Fronta prvého oslovenia", summary["fronta_prvého_oslovenia"], border=True,
                  help=f"{summary['fronta_organizácie']} organizácií, {summary['fronta_kanály']} kanálov. Bez oslovených "
                       "a odmietnutých (v ktorejkoľvek hypotéze), jedna osoba na organizáciu, organizácia oslovená "
                       f"za posledných {c.outreach.org_cooldown_days} dní čaká.")
    st.caption("Odporúčanie je pravidlo, nie hodnotenie človeka: dôvody sú v stĺpci „dôvody“. Typy: **zákazník** "
               "(rozhovor o poslednom prípade problému), **držiteľ dát** (schéma a vzorka), **expert** (posúdenie, "
               "odporúčanie). Účel (vlastník procesu, používateľ, správca dát, schvaľovateľ rozpočtu, expert) má každý "
               "svoj dôkaz; politici a členovia predstavenstiev nie sú vlastníci procesu. Kontakty sú iba zverejnené "
               "organizáciou alebo v úradnom zverejnení, so zdrojom a úryvkom. Nič sa neodosiela.")
    with st.container(horizontal=True):
        kinds = st.segmented_control("Typ", ["všetko", "zákazník", "držiteľ dát", "expert"], default="všetko",
                                     key="g0_kind")
        tiers = st.multiselect("Odporúčanie", list(TIERS), default=[t for t in TIERS if t != "neoslovovať"],
                               key="g0_tiers")
        only_queue = st.toggle("Iba fronta prvého oslovenia", key="g0_queue",
                               help="Noví kontakty bez oslovenia v žiadnej hypotéze; oslovení sú v stave „čaká na "
                                    "odpoveď“ alebo „nadviazanie“.")
    shown = [r for r in rows if r["odporúčanie"] in tiers and (kinds in (None, "všetko") or r["typ"] == kinds)
             and (not only_queue or r.get("prvé_oslovenie"))]
    table = [{k: r[k] for k in ("odporúčanie", "stav_komunikácie", "ďalšia_akcia", "typ", "osoba", "funkcia", "účel",
                                "organizácia", "e-mail", "telefón", "prečo", "čo_dostanú", "potrebný_vstup", "čo_nevieme",
                                "dôvody", "dôkaz_účelu")}
             | {"zdroj": safe_url(r["zdroj_kontaktu"])} for r in shown]
    if not rows:
        st.info("Zatiaľ nikto. Spusti hľadanie: organizácie → plán kontaktov podľa hypotézy (zákazník, držiteľ dát, "
                "expert) → ľudia a kontakty z webov → poradie.", icon=":material/info:")
    else:
        st.dataframe(table, hide_index=True, height=420, alt="Koho osloviť k hypotéze: odporúčanie, e-mail, prečo a čo žiadať",
                     column_config={"zdroj": st.column_config.LinkColumn("Zdroj kontaktu", display_text="zdroj"),
                                    "potrebný_vstup": st.column_config.TextColumn("Potrebný vstup", width="large"),
                                    "dôvody": st.column_config.TextColumn("Dôvody", width="medium")})
    start_job_button("people", "Hľadať ďalšie kontakty k tejto hypotéze", 90, [hid], key=f"g0_search_{hid}",
                     icon=":material/person_search:", disabled=not c.privacy.lia_completed,
                     help="Organizácie → plán kontaktov a dopyty → ľudia a kontakty z webov → poradie → kontakty k osobám.")

    people_rows = [r for r in shown if r["person_id"]]
    if people_rows:
        pid = st.selectbox("Detail", [r["person_id"] for r in people_rows], key="g0_detail",
                           format_func=lambda i: next(f"{r['osoba']} – {r['organizácia']} ({r['odporúčanie']})"
                                                      for r in people_rows if r["person_id"] == i))
        row = next(r for r in people_rows if r["person_id"] == pid)
        with st.container(border=True):
            st.markdown(f"**{esc(row['osoba'])}** · {esc(row['funkcia'] or 'funkcia neuvedená')} · {esc(row['organizácia'])}")
            st.markdown(f"**Čo žiadať:** {esc(row['čo_žiadať'])}")
            if row["cieľ"]:
                st.caption(f"Cieľ plánu: {row['cieľ']}")
            if row["úryvok"]:
                st.html(f"<small>Úryvok zo zdroja: {esc(row['úryvok'])}</small>")
            if row["účel"]:
                st.caption(f"Účel: {row['účel']} · dôkaz: {row['dôkaz_účelu']}")
            with st.container(horizontal=True):
                if st.button("Účel sedí", icon=":material/thumb_up:", key=f"g0_role_ok_{pid}",
                             help="Referenčné hodnotenie (meranie presnosti výberu kontaktov)."):
                    d.add_label("contact_role", "person", pid, "correct", note=f"H{hid}: {row['účel']}")
                    st.toast("Uložené ako referenčné hodnotenie.", icon=":material/check:")
                if st.button("Účel nesedí", icon=":material/thumb_down:", key=f"g0_role_no_{pid}"):
                    d.add_label("contact_role", "person", pid, "incorrect", note=f"H{hid}: {row['účel']}")
                    st.toast("Uložené ako referenčné hodnotenie.", icon=":material/check:")
            with st.container(horizontal=True):
                if row["e-mail"]:
                    st.link_button(f"Napísať na {row['e-mail']}", "mailto:" + quote(row["e-mail"], safe="@."),
                                   icon=":material/mail:", help="Otvorí tvoj poštový program; odosielaš ty.")
                if st.button("Chcem osloviť", icon=":material/check:", key="g0_ok"):
                    d.update_person(pid, status="approved")
                    refresh()
                if st.button("Vylúčiť", icon=":material/block:", key="g0_ex"):
                    d.update_person(pid, status="excluded")
                    refresh()

    with st.expander("Plán kontaktov (koho a ako hľadať)"):
        targets = d.contact_targets_for(hid)
        info = json_or(h["contact_search_json"], {})
        if info:
            st.caption(f"Posledné hľadanie: {(h['contacts_searched_at'] or '')[:16].replace('T', ' ')} UTC · "
                       f"kolá {info.get('kolá')} · dopyty {info.get('dopyty')} · organizácie {info.get('organizácie')} · "
                       f"kontaktovateľní {info.get('kontaktovateľní')}")
        if not targets:
            st.caption("Plán navrhne rýchly model pri ďalšom hľadaní (ciele: zákazník, držiteľ dát, expert; dopyty "
                       "a čo žiadať). Weby organizácií overí kód cez IČO.")
        else:
            st.dataframe([{"typ": KIND_LABEL.get(t["kind"], t["kind"]), "kto": t["who"],
                           "funkcie": ", ".join(target_roles(t)),
                           "dopyty": "; ".join(q["q"] + (" ✓" if q.get("done_at") else "") for q in target_queries(t)),
                           "organizácie": sum(q.get("orgs") or 0 for q in target_queries(t)),
                           "čo žiadať": t["ask"] or "", "kolo": t["round"]} for t in targets],
                         hide_index=True, alt="Ciele plánu kontaktov s dopytmi")
            confirm = st.checkbox("Zahodiť plán a navrhnúť nový", key="g0_replan_ok",
                                  help="Nájdené organizácie a ľudia ostanú; nový plán vznikne pri ďalšom hľadaní.")
            if st.button("Navrhnúť plán znova", disabled=not confirm, key="g0_replan", icon=":material/refresh:"):
                d.delete_contact_targets(hid)
                refresh()

    with st.expander("Pridať kontakt zo stránky organizácie"):
        st.caption("Napr. kontakt odporúčaný mimo systému. Stránka sa stiahne a kód overí, že meno aj e-mail či telefón sú "
                   "na nej v tom istom bloku – čo na stránke nie je, sa neuloží. Bez mena sa uložia generické kontakty "
                   "organizácie (info@, podateľňa). LinkedIn a sociálne siete sa nesťahujú.")
        with st.form(f"g0_add_{hid}", clear_on_submit=False):
            url = st.text_input("Stránka (URL)", placeholder="https://www.organizacia.sk/kontakty")
            name = st.text_input("Meno osoby (voliteľné)")
            kind = st.segmented_control("Typ", ["zákazník", "držiteľ dát", "expert"], default="zákazník")
            role = st.text_input("Funkcia (uloží sa, iba ak je na stránke)")
            org_name = st.text_input("Organizácia (ak na stránke nie je IČO)")
            ask = st.text_input("Čo žiadať", placeholder="napr. schéma a 20 anonymizovaných riadkov")
            if st.form_submit_button("Pridať", icon=":material/person_add:", disabled=not c.privacy.lia_completed):
                from radar.contact_scout import add_contact_from_url
                from radar.fetch import Fetcher
                from radar.registers import RPOClient

                kinds_map = {"zákazník": "customer", "držiteľ dát": "data_holder", "expert": "expert"}
                try:
                    with st.spinner("Sťahujem stránku a overujem meno a kontakt…"):
                        out = add_contact_from_url(
                            c, d, Fetcher(c.fetch, blocked_domains=c.blocked_domains()), hid, url, name,
                            kinds_map.get(kind or "zákazník", "customer"), role, org_name, ask,
                            RPOClient(d, c.people.registry_delay_s, c.people.registry_cache_days, c.fetch.user_agent))
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"Uložené: {out['osoba'] or 'organizácia'} ({out['organizácia']}); kontakty: "
                               f"{', '.join(out['kontakty']) or 'žiadne na stránke'}. Poradie a väzbu doplní krok "
                               "people-rank (akcia „Zbierať ľudí“).")

with tab_value:
    from radar.value import KIND_TEXT, approve_commitment, brief_inputs, brief_markdown, recipient_kinds

    key, dom_title, members, *_, digest = brief_inputs(d, hid)
    st.caption(f"Doména: **{esc(dom_title)}** ({len(members)} hypotéz). Prehľad na ~5 minút čítania je spoločný pre "
               "kontakty domény; tvrdenia sú s citátmi, čo zdroje nehovoria, je medzi neznámymi. Nová verzia vznikne iba "
               "pri zmene podkladov (tvrdenia, karty, odpovede kontaktov); staršie ostávajú.")
    from radar.domain import BLOCKS, CLAIM_STATUS, PARTS, STATUS_LABEL, TARGETS, request_research, set_priority, \
        sync_priorities

    domains = sync_priorities(c, d)
    state = next((r for r in domains if r["domain_key"] == key), None)
    with st.container(border=True):
        cov = json_or(state["coverage_json"], {}) if state is not None else {}
        st.markdown(f"**Doménové podklady:** {STATUS_LABEL.get(state['status'], state['status']) if state else 'nepožiadané'}"
                    + (f" · {esc(state['stop_reason'] or '')}" if state and state["stop_reason"] else "")
                    + (f" · ďalej: {esc(state['next_step'] or '')}" if state and state["next_step"] else ""))
        if cov:
            st.caption("Pokrytie: " + " · ".join(f"{PARTS.get(k, k)}: {v}" for k, v in cov.items()))
        st.caption("Neblokujúce obohatenie: rozhovor stačí s overeným dôvodom výberu kontaktu, zrozumiteľnou hypotézou "
                   "a jednou otázkou. „Pripravené“ neznamená potvrdený problém firmy a nerozhoduje o bránach.")
        with st.container(horizontal=True, vertical_alignment="center"):
            on = bool(state["priority"]) if state else False
            if st.toggle("Prioritná doména (výskum skôr)", value=on, key=f"g0_prio_{key}",
                         help=f"Prvá dávka: {c.domain_research.initial_priority_domain_count} domény podľa sily dôkazov; "
                              "poradie výpočtu, nie právo osloviť kontakt.") != on:
                set_priority(d, key, dom_title, not on)
                refresh()
            if st.button("Obnoviť doménové podklady", key=f"g0_research_{key}", icon=":material/travel_explore:",
                         help="Nový pokus aj pri rovnakej verzii podkladov (inak sa ten istý vstup neskúša znova)."):
                request_research(d, key, dom_title)
                st.toast("Doména zaradená – pokus prebehne v akcii „Pripraviť návrhy oslovení“.", icon=":material/check:")
                refresh()
    stats = json_or(state["stats_json"], {}) if state is not None else {}
    if stats:
        st.caption("Posledný pokus: " + " · ".join(f"{k} {v}" for k, v in stats.items()
                                                    if k in ("dopyty", "dokumenty", "úryvky", "esco", "sekundy")))
    with st.expander("Zdroje domény a roly → povolania (ESCO)", icon=":material/library_books:"):
        esco = d.esco_last_import()
        st.caption(f"ESCO index: verzia {esco['version']}, jazyk {esco['lang']}, {json_or(esco['counts_json'], {})} "
                   f"({esco['license']})" if esco else "ESCO index nie je načítaný: stiahni balík CSV zo stránky ESCO "
                   "(Download) a spusti python -m radar esco-import --dir <priečinok>. Bez neho sa použije iba web.")
        for m in d.role_mappings(key):
            cands = json_or(m["candidates_json"], [])
            if not cands:
                st.caption(f"Rola „{esc(m['role_label'])}“: bez kandidáta v ESCO.")
                continue
            opts = [None] + [x["uri"] for x in cands]
            labels = {x["uri"]: f"{x['label']} ({x['score']} – {x['basis']})" for x in cands}
            pick = st.selectbox(f"Rola „{m['role_label']}“" + (f" – vybral(a) {m['chosen_by']}" if m["chosen_by"] else
                                                                " – nejednoznačné, vyber kandidáta"),
                                opts, index=opts.index(m["chosen_uri"]) if m["chosen_uri"] in opts else 0,
                                format_func=lambda u, lab=labels: "nevybrané (ostanú kandidáti)" if u is None else lab[u],
                                key=f"g0_role_{m['id']}")
            if pick != m["chosen_uri"]:
                d.choose_role_mapping(m["id"], pick)
                refresh()
        dom_ev = d.domain_evidence(key)
        if dom_ev:
            st.dataframe([{"id": f"D{e['id']}", "druh": e["source_kind"], "rozsah": e["scope"], "zdroj": e["source_url"]
                           or e["local_reference"], "verzia": e["source_version"] or "", "úryvok": e["excerpt"][:300],
                           "stiahnuté": (e["retrieved_at"] or "")[:10]} for e in dom_ev], hide_index=True,
                         alt="Doménové podklady: druh, rozsah platnosti, zdroj, úryvok")
    with st.expander(f"Všetky domény Brány 0 ({len(domains)})", icon=":material/list:"):
        st.dataframe([{"doména": r["title"], "priorita": ("áno" if r["priority"] else "nie") + (
            " (ručne)" if (r["priority_source"] or "").startswith("manual") else ""),
                       "stav": STATUS_LABEL.get(r["status"], r["status"]), "dôvod": r["stop_reason"] or "",
                       "hypotézy": ", ".join(f"H{x}" for x in r["hypotézy"])} for r in domains],
                     hide_index=True, alt="Domény Brány 0, priorita a stav podkladov")
    history = d.brief_history(key)
    start_job_button("drafts", "Pripraviť prehľad, návrh prínosu a návrhy oslovení", 60, [hid], key=f"g0_value_{hid}",
                     icon=":material/menu_book:")
    if not history:
        st.info("Prehľad domény ešte nie je – vznikne v akcii „Pripraviť návrhy oslovení“ (krok value).",
                icon=":material/info:")
    else:
        ver = st.selectbox("Verzia prehľadu", [b["id"] for b in history], key=f"g0_brief_{hid}",
                           format_func=lambda i: next(f"v{b['version']} · {b['created_at'][:10]} · {b['status']} · "
                                                      f"{b['reason'] or ''}" for b in history if b["id"] == i))
        brief = d.get_brief(ver)
        if brief["inputs_hash"] != digest and brief["id"] == history[0]["id"]:
            st.warning("Podklady sa od tejto verzie zmenili – nová verzia vznikne pri ďalšom kroku value.",
                       icon=":material/update:")
        checks = json_or(brief["checks_json"], {})
        rev = checks.get("hodnotiteľ") or {}
        if brief["status"] != "ok":
            st.warning({"needs_review": "Prehľad má výhrady – skontroluj:", "pending_review": "Čaká na hodnotiteľa:"}
                       .get(brief["status"], brief["status"]) + " " + "; ".join(
                checks.get("problémy", []) + [f"{p['sentence']} ({p['issue']})" for p in rev.get("problems") or []]),
                icon=":material/rule:")
        md = brief_markdown(brief, d)
        with st.container(border=True, height=420):
            st.markdown(md.replace("](", "]\\("))
        st.download_button("Stiahnuť prehľad (Markdown)", md, file_name=f"prehlad_{key.replace(':', '_')}_v{brief['version']}.md",
                           mime="text/markdown", icon=":material/download:", key=f"g0_brief_dl_{ver}")
    claims = d.domain_claims(key)
    if claims:
        with st.expander(f"Tvrdenia domény so stavom a rozsahom ({len(claims)})", icon=":material/fact_check:"):
            st.dataframe([{"časť": PARTS.get(x["part"], x["part"]), "tvrdenie": x["text"],
                           "stav": CLAIM_STATUS.get(x["status"], x["status"]), "platí pre": x["applicability"] or "",
                           "zdroje": ", ".join([f"[{i}]" for i in json_or(x["evidence_json"], {}).get("claims", [])]
                                               + [f"[D{i}]" for i in json_or(x["evidence_json"], {}).get("evidence", [])])}
                          for x in claims], hide_index=True, alt="Tvrdenia domény: stav, rozsah platnosti, zdroje")
    questions = [q for q in d.domain_questions(key) if q["status"] != "superseded"]
    if questions:
        with st.expander(f"Otázky domény ({sum(q['status'] == 'open' for q in questions)} otvorených)",
                         icon=":material/help:"):
            st.dataframe([{"otázka": q["question"], "komu": TARGETS.get(q["target_role"], q["target_role"]),
                           "blokuje": BLOCKS.get(q["blocks_action"], q["blocks_action"]),
                           "stav": {"open": "otvorená", "answered": "zodpovedaná"}.get(q["status"], q["status"]),
                           "odpoveď": q["answer"] or "", "zdroj odpovede": q["answer_source"] or ""}
                          for q in questions], hide_index=True, alt="Otázky domény: komu, čo blokujú, odpovede")
    st.markdown("**Návrh prínosu podľa typu adresáta** – hypotéza potreby → výstup → podporené rozhodnutie → minimálny vstup")
    st.caption("Potreba je hypotéza, kým ju nepotvrdí odpoveď kontaktu. Bez meraní žiadne číselné úspory; konkrétny "
               "rozsah a termín sľúbi návrh až po tvojom potvrdení záväzku.")
    VSTATUS = {"checked": "skontrolované", "needs_edit": "na úpravu", "pending_review": "čaká na hodnotiteľa",
               "approved": "záväzok potvrdený"}
    for kind in recipient_kinds(c, d, hid):
        props = d.value_proposals_for(hid, kind)
        st.markdown(f"*{KIND_LABEL.get(kind, kind)}* – {KIND_TEXT[kind]}")
        if not props:
            st.caption("Zatiaľ bez návrhu (krok value).")
            continue
        st.dataframe([{"id": p["id"], "stav": VSTATUS.get(p["status"], p["status"]), "potreba": p["need_status"],
                       **{k: json_or(p["proposal_json"], {}).get(k, "") for k in (
                           "need_statement", "deliverable", "decision_supported", "minimum_input", "limitations")},
                       "výhrady": "; ".join(json_or(p["checks_json"], {}).get("kód") or [])}
                      for p in props], hide_index=True, alt=f"Návrhy prínosu – {kind}",
                     column_config={"need_statement": "hypotéza potreby", "deliverable": "čo dostanú",
                                    "decision_supported": "pomôže rozhodnúť", "minimum_input": "potrebný vstup",
                                    "limitations": "čo ešte nevieme"})
        with st.form(f"g0_commit_{hid}_{kind}"):
            pid_ = st.selectbox("Návrh", [p["id"] for p in props], key=f"g0_commit_p_{hid}_{kind}")
            scope = st.text_input("Záväzok, ktorý vieš dodať (rozsah, termín, kapacita)", key=f"g0_commit_s_{hid}_{kind}")
            if st.form_submit_button("Potvrdiť záväzok", icon=":material/handshake:"):
                try:
                    approve_commitment(d, pid_, scope)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    refresh()

with tab_metrics:
    m = gate0_metrics(d, hid)
    with st.container(horizontal=True):
        st.metric("Rozhovory so zákazníkmi", f"{m['customer_interviews']} / {m['target']}", border=True)
        st.metric("Spomenuli sami", f"{m['unprompted']} ({m['unprompted_share']:.0%})", help="Prah: aspoň 3 z 10.", border=True)
        st.metric("Platiteľ", m["payer_confirmed"] or "nepotvrdený", border=True)
        st.metric("Návrh verdiktu", m["verdict_proposal"], border=True)
    rate = "–" if m["response_rate"] is None else f"{m['response_rate']:.0%}"
    st.caption(f"Experti (nepočítajú sa): {m['expert_interviews']} · záväzky: {m['commitments'] or 'žiadne'} · "
               f"miera odpovedí: {rate}")
    funnel = [{"krok": k, "počet": v} for k, v in m["funnel"].items()]
    st.bar_chart(funnel, x="krok", y="počet", horizontal=True, sort=False, alt="Lievik od kandidátov po rozhovory")

    with st.form("payer"):
        payer = st.text_input("Kto platí (potvrdené z rozhovorov)", value=h["payer_confirmed"] or "")
        if st.form_submit_button("Uložiť platiteľa"):
            d.set_hypothesis_fields(hid, payer_confirmed=payer.strip() or None)
            refresh()

    st.subheader("Rozhodnutie")
    st.caption("Rozhoduje človek; uloží sa so snímkou metrík.")
    with st.form("decision", clear_on_submit=True):
        decision = st.segmented_control("Rozhodnutie", list(DECISION_STATUS),
                                        format_func=lambda x: {"continue": "pokračovať", "pivot": "pivot", "stop": "ukončiť",
                                                               "pause": "pozastaviť"}[x])
        rationale = st.text_area("Odôvodnenie")
        if st.form_submit_button("Zapísať rozhodnutie"):
            try:
                record_decision(d, hid, decision or "", rationale)
                refresh()
            except ValueError as exc:
                st.error(str(exc))
    rows_d = [{"dátum": r["created_at"][:10], "rozhodnutie": r["decision"], "odôvodnenie": r["rationale"],
               "verdikt v tom čase": json_or(r["metrics_json"], {}).get("verdict_proposal")} for r in d.decisions_for(hid)]
    st.dataframe(rows_d, hide_index=True, alt="Doterajšie rozhodnutia k hypotéze")
