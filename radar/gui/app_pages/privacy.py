import streamlit as st

from radar.crm import export
from radar.gui.common import cfg, db
from radar.privacy import forget, purge, sender_placeholders

d, c = db(), cfg()
p = c.privacy
with st.container(horizontal=True):
    st.metric("LIA potvrdená", "áno" if p.lia_completed else "nie", border=True)
    st.metric("Profil odosielateľa", "doplnený" if not sender_placeholders(c) else "placeholdery", border=True)
    st.metric("Potlačené identifikátory", sum(d.suppression_counts().values()), border=True)
st.caption(f"Retencia: neoslovení {p.retention_days_uncontacted} dní · po poslednej interakcii {p.retention_days_after_interaction} dní · "
           f"odmietnutí {p.retention_days_declined} dní · exporty {p.retention_days_exports} dní. Purge beží aj na konci run-all.")
st.dataframe([d.gate0_counts()], hide_index=True, alt="Počty osobných a súvisiacich záznamov")

st.subheader("Výmaz osoby (námietka alebo žiadosť o výmaz)")
with st.form("forget_form", clear_on_submit=True):
    email = st.text_input("E-mail")
    phone = st.text_input("Telefón")
    reason = st.segmented_control("Dôvod", ["objection", "erasure", "manual"], default="erasure",
                                  format_func=lambda r: {"objection": "námietka", "erasure": "výmaz", "manual": "ručne"}[r])
    confirm = st.checkbox("Potvrdzujem nevratný výmaz")
    if st.form_submit_button("Vymazať a pridať do potlačenia", icon=":material/delete_forever:"):
        if not confirm or not (email or phone):
            st.error("Zadaj e-mail alebo telefón a potvrď výmaz.")
        else:
            result = forget(c, d, email=email or None, phone=phone or None, reason=reason or "erasure")
            st.success(f"Vymazané osoby: {result['people_removed']}, kontakty: {result['contacts_removed']}.")

st.subheader("Retencia")
if st.button("Spustiť purge teraz", icon=":material/auto_delete:"):
    st.write(purge(c, d))

st.subheader("Export")
what = st.segmented_control("Čo", ["hypotheses", "people", "outreach", "interviews"], default="hypotheses")
fmt = st.segmented_control("Formát", ["csv", "xlsx"], default="xlsx")
personal = st.checkbox("Vrátane osobných údajov (mená, kontakty)")
if personal:
    st.warning("Export s osobnými údajmi neposielaj ďalej; po 30 dňoch ho zmaže purge.", icon=":material/warning:")
if st.button("Vytvoriť export", icon=":material/download:") and what and fmt:
    path = export(c, d, what, fmt, personal)
    st.download_button("Stiahnuť " + path.name, path.read_bytes(), file_name=path.name, icon=":material/file_download:")
