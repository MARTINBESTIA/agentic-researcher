import streamlit as st

from radar.event_logs import (
    ALTERNATIVES, REQUEST_STATUS, EventLogError, build_request, draft_request_message, event_log_fit, request_markdown,
    set_request_status,
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
