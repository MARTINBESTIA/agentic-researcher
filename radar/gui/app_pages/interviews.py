from datetime import date

import streamlit as st

from radar.crm import COMMITMENTS, record_interview
from radar.gui.common import cfg, db, pick_hypothesis

d, c = db(), cfg()
hid = pick_hypothesis(d, "int_h")
if hid is None:
    st.stop()

st.caption("Otázky podľa The Mom Test: kedy sa to stalo naposledy, ako to riešite dnes, čo to stojí, čo ste skúšali, "
           "kto rozhoduje a z akého rozpočtu. Nepýtať sa „kúpili by ste…“. Do poznámok nepíš kontaktné údaje.")
people = {l["person_id"]: f"{l['full_name']} ({l['org_name'] or ''})" for l in d.links_for_hypothesis(hid)}
with st.form("interview_form", clear_on_submit=True):
    cols = st.columns(3)
    held = cols[0].date_input("Dátum", value=date.today())
    kind = cols[1].segmented_control("Typ", ["customer", "expert"], default="customer",
                                     format_func=lambda k: {"customer": "zákazník", "expert": "expert"}[k])
    person = cols[2].selectbox("Osoba (nepovinné)", [None] + list(people), format_func=lambda p: "–" if p is None else people[p])
    role = cols[0].text_input("Funkcia respondenta", placeholder="napr. vedúci nákupu")
    org_type = cols[1].text_input("Typ organizácie", placeholder="napr. strojárska firma 50–100 ľudí")
    unprompted = cols[2].segmented_control("Spomenul problém sám?", ["áno", "nie"])
    last = st.text_area("Kedy sa problém stal naposledy a čo sa stalo")
    workaround = st.text_area("Ako to riešia dnes")
    cost = st.text_input("Čo ich to stojí (čas, peniaze, chyby)")
    payer = st.text_input("Kto rozhoduje a kto by platil")
    tried = st.text_input("Čo už skúšali")
    commitment = st.selectbox("Ďalší krok / záväzok", COMMITMENTS,
                              format_func=lambda x: {"intro": "predstavenie kolegu", "follow_up": "ďalšie stretnutie",
                                                     "data": "dáta", "pilot": "pilot", "none": "nič"}[x])
    notes = st.text_area("Poznámky (doslovné citáty sem)", height=180)
    quotes = st.text_area("Citáty ako dôkazy (jeden na riadok, musia byť doslova v poznámkach)")
    if st.form_submit_button("Uložiť rozhovor", icon=":material/save:"):
        result = record_interview(
            c, d, hid, kind or "customer", held.isoformat(), notes,
            quotes=[q for q in quotes.splitlines() if q.strip()], person_id=person,
            role_label=role or None, org_type=org_type or None, last_occurrence=last or None,
            current_workaround=workaround or None, cost_impact=cost or None, decision_and_payer=payer or None,
            tried_before=tried or None, mentioned_unprompted={"áno": 1, "nie": 0}.get(unprompted), commitment=commitment,
        )
        st.success(f"Uložené ako {result['pseudonym']}: citáty overené {result.get('verified', 0)}, "
                   f"zamietnuté {result.get('rejected', 0)} (nie sú doslova v poznámkach).")

rows = [{"pseudonym": i["pseudonym"], "typ": i["interviewee_kind"], "funkcia": i["role_label"], "organizácia": i["org_type"],
         "dátum": i["held_at"], "spomenul sám": {1: "áno", 0: "nie"}.get(i["mentioned_unprompted"], "–"),
         "záväzok": i["commitment"], "platiteľ": i["decision_and_payer"]} for i in d.interviews_for(hid)]
st.subheader("Rozhovory")
st.dataframe(rows, hide_index=True, alt="Rozhovory k hypotéze (pseudonymizované)")
