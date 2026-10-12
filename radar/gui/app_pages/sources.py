import streamlit as st

from radar.discover import MATRIX_ARM, load_sources
from radar.gui.common import cfg, db, safe_url
from radar.util import domain_of

d, c = db(), cfg()
sources = load_sources(c)
enabled = [s for s in sources if s.verified]

st.caption("Zdroje, z ktorých objavovanie hľadá nové problémy mimo tvojich tém (súbor sources.yaml). Kategórie si delia "
           "rozpočet dokumentov podľa výťažnosti (úspech = dokument s overeným tvrdením); zo zdrojov s vyhľadávaním sa za beh "
           "použijú najviac 3 na kategóriu v rotácii, RSS vždy. Zdroj zapneš alebo vypneš v sources.yaml (verified).")
with st.container(horizontal=True):
    st.metric("Povolené zdroje", len(enabled), border=True)
    st.metric("Cez RSS (bez vyhľadávača)", sum(1 for s in enabled if s.method == "rss"), border=True)
    st.metric("Kategórie", len({s.category for s in enabled}), border=True)
    st.metric("Vypnuté", len(sources) - len(enabled), border=True)

yield_rows = d.discovery_yield()
by_cat: dict[str, dict] = {}
for r in yield_rows:
    row = by_cat.setdefault(r["category"], {"dokumenty": 0, "relevantné": 0, "overené tvrdenia": 0})
    row["dokumenty"] += r["documents"]
    row["relevantné"] += r["relevant"]
    row["overené tvrdenia"] += r["verified_claims"]
arms = d.arm_stats()
cats = sorted({s.category for s in enabled} | set(by_cat) | {MATRIX_ARM})
st.subheader("Výťažnosť podľa kategórie")
st.dataframe([{
    "kategória": "matica dopytov (vzor bolesti × odvetvie)" if cat == MATRIX_ARM else cat,
    "zdrojov": sum(1 for s in enabled if s.category == cat),
    **by_cat.get(cat, {"dokumenty": 0, "relevantné": 0, "overené tvrdenia": 0}),
    "úspešnosť (bandit)": (round(arms[cat]["successes"] / arms[cat]["tries"], 2)
                           if cat in arms and arms[cat]["tries"] else None),
} for cat in cats], hide_index=True, alt="Dokumenty a overené tvrdenia z objavovania podľa kategórie zdrojov",
    column_config={"úspešnosť (bandit)": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
st.caption("Dokumenty = stiahnuté z objavovania; relevantné = prešli posúdením začiatku (screen); overené tvrdenia = citáty "
           "overené kódom a modelom. Kategórie bez dokumentov ešte nedostali rozpočet alebo sú nové.")

st.subheader("Zdroje")
per_domain: dict[str, tuple[int, int]] = {}
for r in yield_rows:
    docs, ver = per_domain.get(r["domain"], (0, 0))
    per_domain[r["domain"]] = (docs + r["documents"], ver + r["verified_claims"])
cat_filter = st.multiselect("Kategória", sorted({s.category for s in sources}), key="src_cat")
rows = []
for s in sources:
    if cat_filter and s.category not in cat_filter:
        continue
    host = domain_of(s.url)
    docs, ver = per_domain.get(host, (0, 0))
    rows.append({"kategória": s.category, "zdroj": s.name, "spôsob": "RSS" if s.method == "rss" else s.method,
                 "povolený": s.verified, "dokumenty": docs, "overené tvrdenia": ver, "web": safe_url(s.url)})
rows.sort(key=lambda r: (not r["povolený"], -r["overené tvrdenia"], -r["dokumenty"], r["kategória"]))
st.dataframe(rows, hide_index=True, height=520, alt="Katalóg zdrojov objavovania s výťažnosťou",
             column_config={"web": st.column_config.LinkColumn("Web"),
                            "povolený": st.column_config.CheckboxColumn("Povolený")})
st.caption("Počty pri zdroji sa počítajú podľa domény dokumentov z objavovania. Zdroj, ktorý po niekoľkých behoch nemá "
           "žiadne overené tvrdenie, je kandidát na vypnutie.")
