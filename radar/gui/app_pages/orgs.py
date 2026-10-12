import json

import streamlit as st

from radar.gui.common import cfg, db, json_or, pick_hypothesis, refresh, safe_url
from radar.registers import RPO_ATTRIBUTION

d, c = db(), cfg()
hid = pick_hypothesis(d, "org_h")
if hid is None:
    st.stop()

orgs = d.orgs_for_hypothesis(hid, include_excluded=True)
RELATION = {"mentioned_with_problem": "v dôkaze", "quoted_person": "citovaná osoba", "procured_related": "obstarávanie",
            "segment_match": "segment", "manual": "ručne"}
selected = {}
for l in d.links_for_hypothesis(hid):
    if l["priority"] is not None and l["org_id"] is not None:
        selected[l["org_id"]] = selected.get(l["org_id"], 0) + 1
rows = []
for o in orgs:
    fit = json_or(o["fit_json"], {})
    org_contacts = d.org_contacts(o["id"])
    company = next((ct["value"] for ct in org_contacts if ct["kind"] == "profile_url"), None)
    rows.append({
        "id": o["id"], "organizácia": o["name"], "IČO": o["ico"] or "", "zdroj zaradenia": RELATION.get(o["relation"], o["relation"]),
        "ľudia": selected.get(o["id"], 0),
        "kontakt org.": next((ct["value"] for ct in org_contacts if ct["kind"] == "email"), ""),
        "LinkedIn firmy": safe_url(company),
        "web": safe_url(o["website"]), "web overený": bool(o["website_verified"]), "overenie webu": o["website_evidence"] or "",
        "odvetvie (SK NACE)": o["sk_nace"] or "", "veľkosť": o["size_category"] or "", "kraj": o["region"] or "",
        "zhoda": ", ".join(f"{k}: {v}" for k, v in fit.items()),
        "obstarávanie": "⚠ prebieha" if o["procurement_active"] else "",
        "vylúčiť": bool(o["excluded"]),
    })
st.caption(f"{len(rows)} organizácií, pre ktoré môže byť problém dôležitý (dôkaz, obstarávanie, segment). Zložky zhody "
           "so segmentom sa ukazujú zvlášť, nie jedným číslom; „ľudia“ = vybraní ľudia z tejto organizácie.")
edited = st.data_editor(
    rows, hide_index=True, key="org_editor", disabled=[k for k in (rows[0] if rows else {}) if k != "vylúčiť"],
    column_config={"web": st.column_config.LinkColumn("Web"), "vylúčiť": st.column_config.CheckboxColumn("Vylúčiť"),
                   "LinkedIn firmy": st.column_config.LinkColumn("LinkedIn firmy", display_text="stránka")},
    alt="Organizácie k hypotéze so zdrojom zaradenia a overením webu",
)
changes = [(r["id"], r["vylúčiť"]) for r, old in zip(edited, rows) if r["vylúčiť"] != old["vylúčiť"]]
if changes and st.button("Uložiť vylúčenia", icon=":material/save:"):
    for org_id, excluded in changes:
        d.update_organization(org_id, excluded=int(excluded))
    refresh()
if any(o["procurement_active"] for o in orgs):
    st.warning("Organizácie s príznakom obstarávania môžu práve obstarávať súvisiacu vec: komunikuj iba oficiálnou cestou "
               "(prípravné trhové konzultácie, vysvetľovanie súťažných podkladov). Návrhy oslovení sa pre ne nevytvárajú.",
               icon=":material/gavel:")
st.caption(RPO_ATTRIBUTION + ". Trvalé vylúčenie (napr. konflikt záujmov): outreach.exclude_orgs v config.yaml.")

h = d.get_hypothesis(hid)
with st.expander("Filter segmentu pre registre (upraviteľný)"):
    current = json_or(h["segment_filter_json"], {})
    text = st.text_area("JSON", json.dumps(current, ensure_ascii=False, indent=2), height=220, key="seg_json")
    if st.button("Uložiť filter", key="seg_save"):
        try:
            json.loads(text)
        except ValueError as exc:
            st.error(f"Neplatný JSON: {exc}")
        else:
            d.set_hypothesis_fields(hid, segment_filter_json=text)
            refresh()
