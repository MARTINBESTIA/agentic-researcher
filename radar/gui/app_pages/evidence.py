import streamlit as st

from radar.gui.common import cfg, db, highlighted_window, safe_url
from radar.publishers import TYPE_LABELS

d, c = db(), cfg()

with st.container(horizontal=True):
    statuses = st.pills("Stav", ["verified", "partial", "pending", "rejected"], selection_mode="multi",
                        default=["verified", "partial"], key="ev_status")
    origins = st.pills("Pôvod", ["extract", "agent", "validation", "scout", "market", "interview"], selection_mode="multi",
                       default=["extract", "agent", "validation", "scout", "interview"], key="ev_origin",
                       help="validation = test hypotézy, scout = prieskum nových problémov")
types = d.claim_types()
claim_types = st.multiselect("Typ tvrdenia", types, key="ev_types")
domain = st.text_input("Doména obsahuje", key="ev_domain")

rows = d.search_claims(statuses or [], origins or [], claim_types, domain or None, limit=500)
table = [{"id": r["id"], "stav": r["status"], "typ": r["claim_type"], "pôvod": r["origin"], "tvrdenie": r["problem"],
          "kto": r["affected_party"], "citát": r["quote"], "doména": r["domain"], "vydavateľ": TYPE_LABELS.get(r["source_type"], "–"),
          "stiahnuté": r["fetched_at"][:10],
          "zdroj": safe_url(r["url"])} for r in rows]
st.caption(f"{len(table)} tvrdení (najviac 500 najnovších).")
st.dataframe(table, hide_index=True, alt="Tvrdenia s citátmi a zdrojmi", height=420,
             column_config={"zdroj": st.column_config.LinkColumn("Zdroj"), "id": st.column_config.NumberColumn("C", format="C%d")})

if table:
    cid = st.selectbox("Citát v kontexte zdroja", [r["id"] for r in rows], format_func=lambda i: f"C{i}", key="ev_detail")
    r = next(x for x in rows if x["id"] == cid)
    doc = d.get_document(r["document_id"])
    st.html(highlighted_window(doc["text"] if doc else "", r["quote"]))
    st.caption(r["verify_note"] or "")
st.caption("Vydavateľ: úrad, médiá, výskum, združenie / NGO, dodávateľ, iné. Citáty dodávateľov (marketing vlastného "
           "produktu) sa nepočítajú do sily dôkazov hypotézy.")
