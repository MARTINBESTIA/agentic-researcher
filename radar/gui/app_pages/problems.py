from pathlib import Path

import streamlit as st

from radar import dossier
from radar.db import HYPOTHESIS_STATUSES
from radar.gui.common import (
    cfg, db, esc, highlighted_window, hypothesis_table, jobs_panel, json_or, refresh, safe_url, start_job_button,
    theme_table,
)
from radar.publishers import TYPE_LABELS
from radar.strength import evidence_strength

d, c = db(), cfg()
rows = hypothesis_table(c.db_path)

view = st.segmented_control("Zobrazenie", ["Hypotézy", "Témy"], default="Hypotézy", key="pr_view")
if view == "Témy":
    themes = theme_table(c.db_path, rows)
    if not themes:
        st.info("Témy ešte nie sú zostavené. Spusti akciu „Zoskupiť hypotézy do tém“ (Správca akcií) alebo počkaj na "
                "celý beh.", icon=":material/info:")
        start_job_button("themes", "Zoskupiť hypotézy do tém", 15, key="pr_themes_start", icon=":material/category:")
        st.stop()
    in_themes = sum(t["hypotéz"] for t in themes)
    st.caption(f"{len(themes)} tém pokrýva {in_themes} z {len(rows)} hypotéz (ostatné sú samostatné alebo zamietnuté). "
               "Sila témy počíta každé overené tvrdenie jej hypotéz raz; hypotézy sa nezlučujú.")
    themes.sort(key=lambda t: ({"silnejšia": 2, "stredná": 1, "slabá": 0}[t["sila"]], t["overené"], t["hypotéz"]),
                reverse=True)
    st.dataframe(themes, hide_index=True, alt="Témy so súhrnom dôkazov", height=360,
                 column_config={"id": st.column_config.NumberColumn("T", format="T%d")})
    tid = st.selectbox("Detail témy", [t["id"] for t in themes], key="pr_theme",
                       format_func=lambda i: f"T{i}: {next(t['téma'] for t in themes if t['id'] == i)[:100]}")
    theme = d.get_theme(tid)
    if theme["summary"]:
        st.markdown(esc(theme["summary"]))
    member_ids = {h["id"] for h in d.theme_members(tid)}
    members = [r for r in rows if r["id"] in member_ids]
    st.dataframe(members, hide_index=True, alt="Hypotézy témy",
                 column_config={"id": st.column_config.NumberColumn("H", format="H%d")})
    untested = [r["id"] for r in members if r["test"] == "neotestovaná"]
    rank = {"silnejšia": 2, "stredná": 1, "slabá": 0}
    strongest = [r["id"] for r in sorted(members, key=lambda r: (rank[r["sila"]], r["overené"], r["domény"]), reverse=True)
                 if r["stav"] != "rejected"][:3]
    with st.container(horizontal=True):
        start_job_button("validate", f"Otestovať neotestované hypotézy témy ({len(untested)})", 10 + 5 * len(untested),
                         untested, key=f"th_val_{tid}", icon=":material/fact_check:", disabled=not untested)
        if c.privacy.lia_completed:
            start_job_button("people", "Zbierať ľudí k téme (3 najsilnejšie hypotézy)", 120, strongest, key=f"th_ppl_{tid}",
                             icon=":material/group_add:",
                             help="Zapne na Bránu 0 najviac 3 najsilnejšie hypotézy témy (" +
                                  ", ".join(f"H{i}" for i in strongest) + ") a spustí organizácie → ľudia a kontakty "
                                  "→ priorita → LinkedIn. Nič sa neodosiela.")
    with st.container(border=True):  # Brána 0 a 1 za celú tému (hypotézy sa nezlučujú, súčty sú bez duplicít)
        st.markdown("**Brána 0 a 1 pre celú tému**")
        live = [r["id"] for r in members if r["stav"] != "rejected"]
        selected = {h["id"] for h in d.people_enabled_hypotheses()} & set(live)
        people = {l["person_id"] for h in live for l in d.links_for_hypothesis(h)}
        outreach = [o for h in live for o in d.outreach_list(h)]
        interviews = [i for h in live for i in d.interviews_for(h)]
        with st.container(horizontal=True):
            st.metric("Vybrané na Bránu 0", f"{len(selected)} / {len(live)}", border=True)
            st.metric("Ľudia s väzbou", len(people), border=True)
            st.metric("Oslovenia odoslané", sum(1 for o in outreach if o["status"] in ("sent", "replied", "scheduled")),
                      border=True)
            st.metric("Rozhovory", len(interviews), border=True)
        g1 = [h["id"] for h in d.theme_members(tid) if h["id"] in selected and (h["gate1_status"] or "not_started")
              in ("not_started", "running")]
        with st.container(horizontal=True):
            if c.privacy.lia_completed and len(selected) < len(live):
                if st.button(f"Vybrať celú tému na Bránu 0 ({len(live) - len(selected)} ďalších hypotéz)",
                             icon=":material/playlist_add_check:", key=f"th_sel_{tid}",
                             help="Zapne hľadanie ľudí pre všetky nezamietnuté hypotézy témy (osobné údaje sa zbierajú "
                                  "iba k vybraným hypotézam). Zber spustíš akciou „Zbierať ľudí“."):
                    for h in live:
                        if h not in selected:
                            d.set_people_selection(h, True)
                    refresh()
            start_job_button("gate1", f"Prieskum dát pre tému ({len(g1)} nezačatých/rozpracovaných)", 120, g1,
                             key=f"th_g1_{tid}", icon=":material/database:", disabled=not g1)
    jobs_panel(show_last=False)
    with st.form(f"pr_theme_rename_{tid}"):
        name = st.text_input("Názov témy", theme["name"])
        if st.form_submit_button("Premenovať (ručný názov sa pri ďalšom zostavení zachová)", icon=":material/edit:"):
            try:
                d.rename_theme(tid, name)
            except ValueError as exc:
                st.error(str(exc))
            else:
                refresh()
    st.stop()

# Predvolene všetky hypotézy (aj slabé a zamietnuté); filtre iba zužujú.
TESTS = ["podložená", "sporná", "slabá", "nepodložená", "čaká na overenie", "neotestovaná"]
with st.container(horizontal=True):
    origins = st.pills("Pôvod", ["topic", "discovery", "manual", "interview"], selection_mode="multi",
                       default=["topic", "discovery", "manual", "interview"], key="pr_origin")
    statuses = st.pills("Stav", list(HYPOTHESIS_STATUSES), selection_mode="multi",
                        default=list(HYPOTHESIS_STATUSES), key="pr_status")
    strengths = st.pills("Sila dôkazov", ["silnejšia", "stredná", "slabá"], selection_mode="multi",
                         default=["silnejšia", "stredná", "slabá"], key="pr_strength")
    tests = st.pills("Test", TESTS, selection_mode="multi", default=TESTS, key="pr_test")
theme_names = sorted({r["téma"] for r in rows if r["téma"]})
with st.container(horizontal=True):
    query = st.text_input("Hľadať v názve", key="pr_q", placeholder="napr. obstarávanie")
    theme_pick = st.selectbox("Téma", ["všetky", "bez témy"] + theme_names, key="pr_theme_filter")
order = {"silnejšia": 2, "stredná": 1, "slabá": 0}
shown = [r for r in rows if r["pôvod"] in (origins or []) and r["stav"] in (statuses or []) and r["sila"] in (strengths or [])
         and r["test"] in (tests or []) and (not query or query.lower() in (r["hypotéza"] or "").lower())
         and (theme_pick == "všetky" or r["téma"] == ("" if theme_pick == "bez témy" else theme_pick))]
shown.sort(key=lambda r: (order[r["sila"]], r["overené"], r["domény"]), reverse=True)
n_tested = sum(1 for r in rows if r["test"] not in ("neotestovaná", "čaká na overenie"))
st.caption(f"{len(shown)} z {len(rows)} hypotéz · otestované {n_tested} z {len(rows)}. Sila: silnejšia = ≥ 4 overené "
           "z ≥ 3 domén, stredná = ≥ 2 z 2. Test: verdikt kódu po tom, čo agent dohľadal zdroje za aj proti.")
st.dataframe(shown, hide_index=True, alt="Hypotézy so silou dôkazov a výsledkom testu", height=360,
             column_config={"id": st.column_config.NumberColumn("H", format="H%d"),
                            "protidôkazy": st.column_config.NumberColumn("protidôkazy", format="%d")})

if not shown:
    st.stop()
hid = st.selectbox("Detail hypotézy", [r["id"] for r in shown],
                   format_func=lambda i: f"H{i}: {next(r['hypotéza'] for r in shown if r['id'] == i)[:100]}", key="pr_detail")
h = d.get_hypothesis(hid)
evidence = d.evidence_for(hid)
label, n_ver, n_part, n_dom = evidence_strength(evidence)

with st.container(border=True):
    st.markdown(f"**{esc(h['title'])}**")
    st.caption(f"Segment: {h['segment'] or '–'} · pôvod: {h['origin']} · odvetvie: {h['sector'] or '–'} · "
               f"sila: {label} (overené {n_ver}, čiastočne {n_part}, domény {n_dom})")
    if h["description"]:
        st.text(h["description"])
    with st.form(f"hyp_edit_{hid}"):
        cols = st.columns(4)
        people_on = cols[0].toggle("Hľadať ľudí (Brána 0)", value=bool(h["people_enabled"]))
        status = cols[1].selectbox("Stav", HYPOTHESIS_STATUSES, index=HYPOTHESIS_STATUSES.index(h["status"]))
        fit_options = ["", "high", "medium", "low", "unknown"]
        founder = cols[2].selectbox("Zhoda so zakladateľom", fit_options, index=fit_options.index(h["founder_fit"] or ""),
                                    format_func=lambda v: {"": "–", "high": "vysoká", "medium": "stredná", "low": "nízka",
                                                           "unknown": "neviem"}[v])
        note = cols[3].text_input("Poznámka")
        if st.form_submit_button("Uložiť", icon=":material/save:"):
            if bool(h["people_enabled"]) != people_on:
                d.set_people_selection(hid, people_on)
            if status != h["status"]:
                d.set_hypothesis_status(hid, status)
            if (founder or None) != h["founder_fit"]:
                d.set_hypothesis_fields(hid, founder_fit=founder or None)
            if note.strip():
                d.add_note(hid, note.strip(), source="human")
            refresh()

with st.container(border=True):
    test = json_or(h["validation_json"], {}) if h["validation_state"] == "tested" else {}
    if test:
        z = test.get("z_testu") or {}
        st.markdown(f"**Test: {esc(test.get('verdikt'))}** ({esc(test.get('pravidlo', ''))}) · {h['validated_at'][:10]}")
        agent = test.get("agent") or {}
        if agent.get("ok") is False:
            st.warning(f"Test je neúplný – agent: {agent.get('chyba', 'zlyhal')}. Verdikt je iba z doterajších dôkazov; "
                       "použi „Otestovať teraz“.", icon=":material/warning:")
        st.caption(f"Overené {test.get('overené')} z {test.get('domény')} domén, protidôkazy {test.get('protidôkazy')}, "
                   f"existujúce riešenia {test.get('existujúce_riešenia')}, od dodávateľov {test.get('od_dodávateľov', 0)} "
                   f"(nepočítajú sa) · agent našiel {z.get('nájdené', 0)} citátov: "
                   f"{z.get('priradené_sem', 0)} k tejto hypotéze, {z.get('priradené_inam', 0)} inam, "
                   f"{z.get('zamietnuté', 0)} zamietnutých kontrolou citátu.")
    elif h["validation_state"] == "searched":
        st.markdown("**Test: čaká na overenie** – agent dohľadal zdroje, citáty ešte posúdi kód a presný model.")
    else:
        st.markdown("**Test: neotestovaná**")
    with st.container(horizontal=True):
        start_job_button("validate", "Otestovať teraz", 25, [hid], key=f"val_{hid}", icon=":material/fact_check:")
        if c.privacy.lia_completed:
            start_job_button("people", "Hľadať firmy a ľudí k tomuto problému", 90, [hid], key=f"ppl_{hid}",
                             icon=":material/group_add:",
                             help="Zapne hypotézu na Bránu 0 a spustí organizácie → ľudia a kontakty → priorita → "
                                  "LinkedIn (nič sa neodosiela; návrhy oslovení sú samostatná akcia).")
    jobs_panel(show_last=False)

tab_brief, tab_ev, tab_axes, tab_crit, tab_notes, tab_export = st.tabs(
    ["Prehľad príležitosti", "Dôkazy", "Osi a obchodný stav", "Kritika a trh", "Poznámky", "Export všetkého"])
with tab_brief:
    from radar.problem_brief import problem_brief

    st.caption("Jedna strana na problém: odkazy na overené zdroje, čo problém znamená, navrhnutý produkt (hypotéza "
               "riešenia), čo je podložené dátami, koho a s čím osloviť a čo chýba. Skladá sa z uložených údajov – "
               "model sa nevolá. Osoby sú pseudonymizované.")
    brief_md = problem_brief(c, d, hid)
    with st.container(border=True, height=640):
        st.markdown(brief_md.replace("](", "]\\("))
    st.download_button("Stiahnuť prehľad (Markdown)", brief_md, file_name=f"prehlad_prilezitosti_H{hid}.md",
                       mime="text/markdown", icon=":material/download:", key=f"pb_dl_{hid}")
with tab_axes:
    from radar.progress import BUSINESS_LABELS, BUSINESS_STEPS, axes, business_state, set_business_step

    st.caption("Samostatné osi (nesčítavajú sa): overený citát nie je overený trh. Literatúra slúži na výber rozhovorov; "
               "zákazník, dáta a ochota platiť sa overujú rozhovormi, Bránou 1 a pilotom.")
    ax = axes(c, d, hid)
    with st.container(horizontal=True):
        for name, label in (("literatúra", "Literatúra"), ("zákazník", "Zákazník"), ("dáta", "Dáta"),
                            ("ochota_platiť", "Ochota platiť")):
            st.metric(label, ax[name]["stav"], border=True,
                      help=", ".join(f"{k}: {v}" for k, v in ax[name]["zložky"].items()))
    state = business_state(d, hid)
    st.markdown("**Obchodný stav** (zapisuješ ty, každý krok s dôkazom):")
    st.dataframe([{"krok": BUSINESS_LABELS[k], "hotové": bool(state[k].get("hotové")), "dôkaz": state[k].get("dôkaz", ""),
                   "kedy": (state[k].get("kedy") or "")[:10]} for k in BUSINESS_STEPS], hide_index=True,
                 alt="Obchodný stav hypotézy")
    with st.form(f"pr_business_{hid}"):
        step = st.selectbox("Krok", list(BUSINESS_STEPS), format_func=BUSINESS_LABELS.get, key=f"pr_bs_step_{hid}")
        done = st.checkbox("Hotové", key=f"pr_bs_done_{hid}")
        proof = st.text_input("Dôkaz (kto, čo, kedy)", key=f"pr_bs_note_{hid}")
        if st.form_submit_button("Uložiť krok", icon=":material/flag:"):
            try:
                set_business_step(d, hid, step, done, proof)
            except ValueError as exc:
                st.error(str(exc))
            else:
                refresh()
with tab_ev:
    for e in evidence[:40]:
        vyd = TYPE_LABELS.get(e["source_type"], "") if "source_type" in e.keys() else ""
        with st.expander(f"C{e['id']} · {e['status']} · {e['domain']}{' (' + vyd + ')' if vyd else ''} · {e['problem'][:100]}"):
            doc = d.get_document(e["document_id"])
            st.html(highlighted_window(doc["text"] if doc else "", e["quote"]))
            url = safe_url(e["url"])
            if url:
                st.link_button("Otvoriť zdroj", url, icon=":material/open_in_new:")
            st.caption(f"Stiahnuté {e['fetched_at'][:10]} · {e['verify_note'] or ''}")
with tab_crit:
    crit = d.latest_critique(hid)
    if crit:
        content = crit["content"]
        st.markdown("**Zhrnutie (model, len z dôkazov):**")
        st.text(content.get("summary", ""))
        for title, key in (("Protiargumenty", "counterarguments"), ("Čo ešte overiť", "missing_evidence"),
                           ("Otázky na rozhovory", "interview_questions")):
            if content.get(key):
                st.markdown(f"**{title}:**")
                for item in content[key]:
                    st.text(f"• {item}")
    else:
        st.caption("Kritika zatiaľ nie je.")
    est = d.latest_market_estimate(hid)
    if est:
        st.markdown("**Hrubý odhad trhu**")
        st.json(est["detail"], expanded=False)
with tab_notes:
    for n in d.notes_for(hid)[-20:]:
        st.text(f"{n['created_at'][:10]} ({n['source']}): {n['text'][:2000]}")
with tab_export:
    st.caption("Dosiér: všetko, čo systém k hypotéze zozbieral, v jednom ZIP – súhrn (README), tabuľky (CSV a Excel), "
               "dôkazy s textami zdrojov, organizácie, ľudia a kontakty, Brány 1 a 2, volania modelov a dáta. "
               "Ukladá sa do exports/ (mimo gitu, po 30 dňoch ho zmaže purge).")
    scope = dossier.scope_of(d, hid, True)
    related = False
    if len(scope) > 1:
        related = st.toggle("Aj súvisiace hypotézy: " + ", ".join(f"H{h} ({r})" for h, r in scope[1:]), value=True,
                            key=f"dos_rel_{hid}", help="Ľudia a organizácie rozdelenej hypotézy ostávajú pri rodičovi.")
    hids = [h for h, _ in (scope if related else scope[:1])]
    sets = [ds for h in hids for ds in d.datasets_for(h, only_ok=True)]
    rows_total = sum(ds["rows"] or 0 for ds in {ds["table_name"] + str(ds["url"]): ds for ds in sets}.values())
    data_mode = st.segmented_control("Dáta Brány 1", list(dossier.DATA_MODES), default="full", key=f"dos_data_{hid}",
                                     format_func=dossier.DATA_LABELS.get) or "full"
    st.caption(f"Tabuľky: {len(sets)}, spolu ~{rows_total:,} riadkov".replace(",", " ") if sets
               else "K hypotéze nie sú stiahnuté dáta.")
    personal = st.checkbox("S menami a osobnými kontaktmi (e-mail, telefón, LinkedIn, zdroj s úryvkom)", value=False,
                           key=f"dos_pers_{hid}",
                           help="Predvolene je dosiér pseudonymizovaný: osoby ako „Osoba P<id>“, bez osobných kontaktov.")
    if personal:
        st.warning("Dosiér bude obsahovať osobné údaje. Neposielaj ho ďalej a po použití ho zmaž; export sa zapíše do "
                   "poznámok hypotézy.", icon=":material/shield_person:")
    if st.button("Vytvoriť dosiér", icon=":material/folder_zip:", key=f"dos_go_{hid}"):
        with st.status("Vytváram dosiér…", expanded=True) as box:
            try:
                path = dossier.write(c, d, hid, personal=personal, data_mode=data_mode, include_related=related,
                                     progress=box.write)
                st.session_state[f"dos_path_{hid}"] = str(path)
                box.update(label="Dosiér je hotový", state="complete")
            except Exception as exc:  # noqa: BLE001 – chyba exportu nesmie zhodiť stránku
                box.update(label=f"Dosiér sa nepodarilo vytvoriť: {exc}", state="error")
    done = st.session_state.get(f"dos_path_{hid}")
    if done and Path(done).exists():
        size = Path(done).stat().st_size
        st.success(f"`{done}` · {size / 1e6:.1f} MB", icon=":material/check_circle:")
        st.download_button("Stiahnuť ZIP", data=lambda: Path(done).read_bytes(), file_name=Path(done).name,
                           mime="application/zip", icon=":material/download:", key=f"dos_dl_{hid}", on_click="ignore")
