import streamlit as st

from radar.event_logs import (
    ALTERNATIVES, METRICS, REQUEST_STATUS, TARGETS, EventLogError, build_request, confirm_mapping, distinct_values,
    draft_request_message, event_log_fit, import_file, quality_rows, request_markdown, set_request_status, store_dir,
)
from radar.gui.common import cfg, db, esc, json_or, pick_hypothesis, refresh

d, c = db(), cfg()
st.caption("Event log = história udalostí procesu (jeden riadok = udalosť konkrétneho prípadu, nie aktuálny stav). "
           "Žiadosť je dvojstupňová: najprv slovník a malá ukážka niekoľkých celých prípadov, až po dohode väčší export. "
           "Systém nič neodosiela; dáta organizácie ostávajú lokálne v jej oddelenom priestore.")
hid = pick_hypothesis(d, "el_h")
if hid is None:
    st.stop()
tab_req, tab_import, tab_report = st.tabs(["Žiadosti", "Import a kvalita", "Analýza"])

with tab_req:
    fit, why = event_log_fit(d, hid)
    (st.info if fit else st.warning)(("Vhodné: " if fit else "Pozor: ") + why, icon=":material/timeline:")
    orgs = {o["org_id"]: o["org_name"] for o in d.outreach_list(hid) if o["org_id"]}
    with st.form("el_new", border=True):
        st.markdown("**Nová žiadosť**")
        org_id = st.selectbox("Organizácia", list(orgs) or [None], format_func=lambda i: orgs.get(i, "–"), key="el_org")
        case = st.text_input("Definícia prípadu (v 6. páde: „pri …“)", placeholder="objednávke, doklade, zákazke",
                             key="el_case")
        step = st.radio("Krok", [1, 2], horizontal=True, key="el_step",
                        format_func=lambda s: "1 – slovník a malá ukážka" if s == 1 else "2 – väčší export po dohode")
        reason = st.text_input("Vlastné zdôvodnenie (povinné, ak hypotéza nie je o priebehu)", key="el_why")
        if st.form_submit_button("Pripraviť žiadosť", icon=":material/note_add:"):
            outreach = next((o["id"] for o in d.outreach_list(hid) if o["org_id"] == org_id and o["status"] in (
                "replied", "scheduled", "interviewed", "sent")), None)
            try:
                build_request(c, d, hid, org_id, case, outreach_id=outreach, step=step, why=reason)
                refresh()
            except EventLogError as exc:
                st.error(str(exc))
    for r in d.eventlog_requests(hid):
        title = (f"#{r['id']} · {r['org_name'] or 'organizácia'} · krok {r['step']} · "
                 f"{REQUEST_STATUS.get(r['status'], r['status'])}"
                 + (f" · {ALTERNATIVES[r['alternative']]}" if r["alternative"] else ""))
        with st.expander(title, expanded=r["status"] == "draft"):
            st.markdown(esc(request_markdown(r)).replace("](", "]\\("))  # odkazy z textu modelu sa nevykreslia
            with st.container(horizontal=True):
                if not r["outreach_id"] or d.get_outreach(r["outreach_id"]) is None or \
                        d.get_outreach(r["outreach_id"])["mode"] != "data_request":
                    if st.button("Pripraviť správu (do Oslovení)", key=f"el_msg_{r['id']}", icon=":material/mail:"):
                        try:
                            draft_request_message(c, d, r["id"])
                            st.toast("Návrh správy je v Osloveniach – skontroluj, schváľ a pošli ručne.",
                                     icon=":material/check:")
                            refresh()
                        except EventLogError as exc:
                            st.error(str(exc))
                else:
                    st.caption(f"Správa: oslovenie #{r['outreach_id']} (stav {d.get_outreach(r['outreach_id'])['status']})")
            cols = st.columns(2)
            new_status = cols[0].selectbox("Stav", list(REQUEST_STATUS), index=list(REQUEST_STATUS).index(r["status"]),
                                           format_func=REQUEST_STATUS.get, key=f"el_st_{r['id']}")
            alt = cols[1].selectbox("Ak event log nemajú: alternatíva", [None, *ALTERNATIVES],
                                    index=([None, *ALTERNATIVES].index(r["alternative"]) if r["alternative"] else 0),
                                    format_func=lambda a: "–" if a is None else ALTERNATIVES[a], key=f"el_alt_{r['id']}")
            if (new_status != r["status"] or alt != r["alternative"]) and st.button("Uložiť stav", key=f"el_save_{r['id']}"):
                set_request_status(d, r["id"], new_status, alt)
                refresh()
            if r["status"] in ("not_available", "alternative"):
                st.caption("Aktuálny snapshot sa nespracuje ako história: import ho označí typom „snapshot“ a časové "
                           "analýzy z neho nevzniknú.")
            st.caption("Neznáme: " + "; ".join(json_or(r["unknowns_json"], [])))

AV_ICON = {"áno": "áno", "čiastočne": "čiastočne", "nie": "nie"}
with tab_import:
    st.caption("CSV alebo Parquet. Rovnaký súbor sa pre organizáciu nezdvojí (SHA-256); rovnaké ID prípadu v dvoch "
               "organizáciách sa nezlúči (každá má vlastný priestor). Mapovanie navrhne systém, potvrdíš ho ty.")
    all_orgs = {o["org_id"]: o["org_name"] for o in d.outreach_list(hid) if o["org_id"]}
    with st.form("el_upload", border=True):
        up_org = st.selectbox("Organizácia", list(all_orgs) or [None], format_func=lambda i: all_orgs.get(i, "–"),
                              key="el_up_org")
        upload = st.file_uploader("Export event logu", type=["csv", "parquet"], key="el_file")
        req_ids = [r["id"] for r in d.eventlog_requests(hid) if r["organization_id"] == up_org]
        if st.form_submit_button("Importovať", icon=":material/upload:") and upload is not None and up_org:
            tmp = store_dir(c) / f"upload_{upload.name}"
            tmp.write_bytes(upload.getvalue())
            try:
                iid, new = import_file(c, d, up_org, tmp, hid=hid, request_id=req_ids[0] if req_ids else None,
                                       file_name=upload.name)
                st.toast("Importované – potvrď mapovanie." if new else "Tento súbor už je importovaný.",
                         icon=":material/check:")
                if req_ids:
                    set_request_status(d, req_ids[0], "received")
            except EventLogError as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)
    imports = d.eventlog_imports(hid=hid)
    if not imports:
        st.caption("Zatiaľ žiadny import.")
    for imp in imports:
        rep = json_or(imp["quality_json"], {})
        with st.expander(f"#{imp['id']} · {imp['org_name']} · {imp['file_name']} · {imp['rows_total'] or 0} riadkov · "
                         f"{imp['status']} · {imp['data_kind']}", expanded=imp["status"] != "checked"):
            cols = [x["stĺpec"] for x in json_or(imp["columns_json"], [])]
            mp = json_or(imp["mapping_json"], {})
            current = mp.get("potvrdené") or mp.get("návrh") or {}
            for n in mp.get("poznámky") or []:
                st.caption(f"⚠ {esc(n)}")
            with st.form(f"el_map_{imp['id']}", border=False):
                st.markdown("**Mapovanie stĺpcov** (case_id, activity a timestamp sú základ; meno pracovníka nie)")
                grid = st.columns(5)
                picks = {}
                for i, t in enumerate(TARGETS):
                    opts = [""] + cols
                    picks[t] = grid[i % 5].selectbox(t, opts, index=opts.index(current[t]) if current.get(t) in opts else 0,
                                                     key=f"el_m_{imp['id']}_{t}")
                acts = distinct_values(c, imp, picks.get("activity") or current.get("activity") or "")
                g2 = st.columns(3)
                tz = g2[0].selectbox("Časová zóna časov bez offsetu", ["", "offset", "Europe/Bratislava", "UTC"],
                                     format_func=lambda z: {"": "neznáma", "offset": "časy majú offset"}.get(z, z),
                                     key=f"el_tz_{imp['id']}")
                case_def = g2[1].text_input("Definícia prípadu", imp["case_definition"] or "", key=f"el_cd_{imp['id']}")
                selection = g2[2].text_input("Výber prípadov (všetky / iba dokončené / tím…)", mp.get("výber") or "",
                                             key=f"el_sel_{imp['id']}")
                g3 = st.columns(4)
                starts = g3[0].multiselect("Začiatočné aktivity", acts, default=[a for a in mp.get("začiatočné_aktivity")
                                                                                  or [] if a in acts],
                                           key=f"el_sa_{imp['id']}")
                ends = g3[1].multiselect("Koncové aktivity", acts, default=[a for a in mp.get("koncové_aktivity") or []
                                                                           if a in acts], key=f"el_ea_{imp['id']}")
                p_from = g3[2].text_input("Obdobie od (YYYY-MM-DD)", key=f"el_pf_{imp['id']}")
                p_to = g3[3].text_input("Obdobie do", key=f"el_pt_{imp['id']}")
                g4 = st.columns(2)
                due_def = g4[0].selectbox("Termín (due_at) znamená", ["", "pôvodný prísľub", "zmenený plán", "neznámy"],
                                          format_func=lambda v: v or "–", key=f"el_due_{imp['id']}")
                kind = g4[1].selectbox("Druh dát", [None, "event_log", "snapshot"], key=f"el_kind_{imp['id']}",
                                       format_func=lambda k: {None: "zistiť automaticky", "event_log": "história udalostí",
                                                              "snapshot": "snapshot (aktuálny stav)"}[k])
                if st.form_submit_button("Potvrdiť mapovanie a skontrolovať kvalitu", icon=":material/rule:"):
                    try:
                        confirm_mapping(c, d, imp["id"], picks, timezone_policy=tz, case_definition=case_def,
                                        period=(p_from, p_to) if p_from and p_to else None, selection=selection,
                                        end_activities=ends, start_activities=starts, due_definition=due_def,
                                        data_kind=kind)
                        refresh()
                    except EventLogError as exc:
                        st.error(str(exc))
            if rep.get("kontroly") is not None:
                st.markdown(f"**Report kvality** (mapovanie v{imp['mapping_version']}, parser {rep.get('verzia_parsera')}) – "
                            f"{rep['riadky']} riadkov, {rep['prípady']} prípadov, použiteľné {rep['použiteľné_riadky']}, "
                            f"s platným časom {rep['riadky_s_platným_časom']}, usporiadané prípady "
                            f"{rep['usporiadané_prípady']}; časová zóna: {rep['časová_zóna']['politika']}")
                rows = quality_rows(rep)
                if rows:
                    st.dataframe(rows, hide_index=True, alt="Kontroly kvality: počet, podiel, rozhodnutie")
                for w in rep.get("upozornenia") or []:
                    st.warning(esc(w), icon=":material/report:")
                st.dataframe([{"metrika": METRICS[m], "dostupná": AV_ICON.get(v["stav"], v["stav"]), "dôvod": v["dôvod"],
                               "pokrytie": f"{v['pokrytie']:.0%}" if v.get("pokrytie") is not None else ""}
                              for m, v in (rep.get("dostupnosť") or {}).items()], hide_index=True,
                             alt="Ktoré analýzy dáta podporia a prečo")
                st.caption("Nič sa nezmazalo: problémové riadky sú označené. Transformácie: "
                           + "; ".join(json_or(imp["transformations_json"], [])))
            if st.button("Zmazať import (tabuľky aj pôvodný súbor)", key=f"el_del_{imp['id']}",
                         icon=":material/delete:", help="Napr. ak export obsahuje údaje, ktoré nemali byť poslané."):
                from radar.event_logs import delete_import

                delete_import(c, d, imp["id"])
                refresh()
