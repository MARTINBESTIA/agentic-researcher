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

tab_who, tab_metrics = st.tabs(["Koho osloviť", "Metriky a rozhodnutie"])

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
        st.metric("S e-mailom", summary["s_e-mailom"], border=True)
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
    shown = [r for r in rows if r["odporúčanie"] in tiers and (kinds in (None, "všetko") or r["typ"] == kinds)]
    table = [{k: r[k] for k in ("odporúčanie", "typ", "osoba", "funkcia", "účel", "organizácia", "e-mail", "telefón",
                                "prečo", "čo_žiadať", "dôvody", "dôkaz_účelu", "stav")} | {"zdroj": safe_url(r["zdroj_kontaktu"])}
             for r in shown]
    if not rows:
        st.info("Zatiaľ nikto. Spusti hľadanie: organizácie → plán kontaktov podľa hypotézy (zákazník, držiteľ dát, "
                "expert) → ľudia a kontakty z webov → poradie.", icon=":material/info:")
    else:
        st.dataframe(table, hide_index=True, height=420, alt="Koho osloviť k hypotéze: odporúčanie, e-mail, prečo a čo žiadať",
                     column_config={"zdroj": st.column_config.LinkColumn("Zdroj kontaktu", display_text="zdroj"),
                                    "čo_žiadať": st.column_config.TextColumn("Čo žiadať", width="large"),
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
