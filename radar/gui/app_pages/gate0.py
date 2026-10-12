import streamlit as st

from radar.crm import DECISION_STATUS, gate0_metrics, record_decision
from radar.gui.common import cfg, db, json_or, pick_hypothesis, refresh

d, c = db(), cfg()
hid = pick_hypothesis(d, "g0_h")
if hid is None:
    st.stop()
h = d.get_hypothesis(hid)
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
rows = [{"dátum": r["created_at"][:10], "rozhodnutie": r["decision"], "odôvodnenie": r["rationale"],
         "verdikt v tom čase": json_or(r["metrics_json"], {}).get("verdict_proposal")} for r in d.decisions_for(hid)]
st.dataframe(rows, hide_index=True, alt="Doterajšie rozhodnutia k hypotéze")
