import streamlit as st

from radar.gui.common import cfg, db, esc, safe_url
from radar.publishers import TYPE_LABELS

d, c = db(), cfg()

KINDS = {"agent": "prečítal agent (test, prieskum)", "search": "vyhľadávanie k témam", "discovery": "objavovanie",
         "rss": "RSS", "gate1": "Brána 1 (zdroje dát)", "links": "odkazy", "seed": "zadané", "market": "trh"}
SCREEN = {"yes": "relevantný", "no": "nerelevantný", None: ""}

st.caption("Dokument = stiahnutý text (webová stránka, PDF, článok z RSS, stránka prečítaná agentom, metadáta z katalógu), "
           "z ktorého sa vyťahujú doslovné citáty. Citát kontroluje kód (je doslova v texte) a model (podporuje tvrdenie). "
           "Dokumenty Brány 1 slúžia na hľadanie dát, nie dôkazov.")
with st.container(horizontal=True):
    kinds = st.pills("Druh", list(KINDS), selection_mode="multi", default=list(KINDS), format_func=KINDS.get, key="doc_kind")
with st.container(horizontal=True):
    query = st.text_input("Hľadať v názve alebo URL", key="doc_q", placeholder="napr. eurofondy")
    domain = st.text_input("Doména obsahuje", key="doc_domain", placeholder="napr. nku.gov.sk")
    only_claims = st.toggle("Iba s overeným citátom", key="doc_only")

rows = d.documents_overview(kinds or [], query or None, domain or None, only_claims, limit=500)
table = [{"id": r["id"], "názov": r["title"] or "", "doména": r["domain"], "druh": KINDS.get(r["source_kind"], r["source_kind"]),
          "kategória": r["source_category"] or "", "vydavateľ": TYPE_LABELS.get(r["source_type"], ""),
          "posúdenie": SCREEN.get(r["screened"], ""), "citáty": r["claims"], "overené": r["verified"],
          "stiahnuté": (r["fetched_at"] or "")[:10], "url": safe_url(r["url"])} for r in rows]
st.caption(f"{len(table)} dokumentov (najviac 500 najnovších podľa filtra).")
st.dataframe(table, hide_index=True, height=420, alt="Stiahnuté dokumenty s počtom citátov",
             column_config={"id": st.column_config.NumberColumn("D", format="D%d"),
                            "url": st.column_config.LinkColumn("Zdroj", display_text="otvoriť")})

if table:
    doc_id = st.selectbox("Detail dokumentu", [r["id"] for r in rows], key="doc_detail",
                          format_func=lambda i: f"D{i}: {next((r['title'] or r['url']) for r in rows if r['id'] == i)[:100]}")
    doc = d.get_document(doc_id)
    claims = d.claims_of_document(doc_id)
    st.markdown(f"**{esc(doc['title'] or '')}** · {esc(doc['domain'] or '')} · stiahnuté {esc((doc['fetched_at'] or '')[:10])}")
    if claims:
        st.dataframe([{"C": cl["id"], "stav": cl["status"], "typ": cl["claim_type"], "tvrdenie": cl["problem"],
                       "citát": cl["quote"]} for cl in claims], hide_index=True, alt="Citáty z dokumentu",
                     column_config={"C": st.column_config.NumberColumn("C", format="C%d")})
    else:
        st.caption("Z dokumentu sa zatiaľ nevytiahol žiadny citát.")
    with st.expander("Text dokumentu (prvých 6 000 znakov)"):
        st.text((doc["text"] or "")[:6000])
