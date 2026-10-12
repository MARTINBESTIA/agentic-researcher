from datetime import date

import streamlit as st

from radar.gate1 import decide
from radar.gui.common import cfg, db, esc, jobs_panel, json_or, refresh, safe_url, start_job_button
from radar.outreach import add_business_days

d, c = db(), cfg()
st.caption("Brána 1 – dáta a dataset: aké polia treba, kde sú (licencia, frekvencia, kvalita), dataset, z ktorého "
           "zreprodukuješ známe číslo, a čo treba vyžiadať podľa zákona 211/2000. Beží pre hypotézy vybrané na Bránu 0. "
           "Nič sa automaticky nevyraďuje – chýbajúce dáta sú príznak; rozhodnutie je tvoje.")
hyps = d.gate1_hypotheses()
if not hyps:
    st.info("Žiadna hypotéza nie je vybraná na Bránu 0. Vyber ju v Problémoch alebo akciou „Zbierať ľudí ku overeným "
            "hypotézam“.", icon=":material/info:")
    st.stop()
STATUS = {"not_started": "nezačatá", "running": "prebieha", "ready": "pripravená na rozhodnutie", "passed": "prešla",
          "paused": "pozastavená", "stopped": "zastavená"}
labels = {h["id"]: f"H{h['id']}: {h['title'][:80]} · {STATUS.get(h['gate1_status'], h['gate1_status'])}" for h in hyps}
hid = st.selectbox("Hypotéza", list(labels), format_func=labels.get, key="g1_h")
h = d.get_hypothesis(hid)
summary = json_or(h["gate1_json"], {})

with st.container(horizontal=True):
    st.metric("Pripravenosť dát", f"{summary.get('skóre', 0):.2f}" if summary else "–", border=True)
    st.metric("Kľúčové polia nájdené", f"{summary.get('kľúčové_nájdené', 0)} / {summary.get('kľúčové', 0)}", border=True)
    st.metric("Datasety", summary.get("datasety", 0), border=True)
    st.metric("Organizácie v dátach", summary.get("organizácie_v_dátach", 0), border=True)
    st.metric("Reprodukcia čísla", {"match": "zhoda", "close": "blízko", "mismatch": "nesedí", "failed": "nevyšla"}.get(
        summary.get("reprodukcia"), "–"), border=True)
for flag in summary.get("príznaky", []):
    st.warning(flag, icon=":material/flag:")
if summary:
    st.caption("Skóre = " + " + ".join(f"{w}·{k}" for k, w in summary.get("váhy", {}).items()) + " · zložky: " + ", ".join(
        f"{k} {v}" for k, v in summary.get("zložky", {}).items()) + ". Poradie, nie pravdepodobnosť úspechu.")
start_job_button("gate1", "Spustiť prieskum dát pre túto hypotézu", 90, [hid], key=f"g1_run_{hid}",
                 icon=":material/database:")
jobs_panel(show_last=False)

tab_f, tab_s, tab_d, tab_r, tab_i, tab_dec = st.tabs(["Polia", "Zdroje", "Datasety", "Reprodukcia čísla", "Infozákon",
                                                      "Rozhodnutie"])
FIELD_STATUS = ["open", "found", "partial", "requested", "partner", "missing"]
with tab_f:
    fields = d.fields_for(hid)
    if not fields:
        st.caption("Polia navrhne presný model v prvom kroku prieskumu dát.")
    else:
        rows = [{"id": f["id"], "pole": f["name"], "kľúčové": bool(f["is_key"]), "stav": f["status"],
                 "popis": f["description"] or "", "prečo": f["rationale"] or "", "poznámka": f["status_note"] or ""}
                for f in fields]
        edited = st.data_editor(rows, hide_index=True, key=f"g1_fields_{hid}", disabled=["id", "pole", "prečo"],
                                column_config={"stav": st.column_config.SelectboxColumn("stav", options=FIELD_STATUS),
                                               "kľúčové": st.column_config.CheckboxColumn("kľúčové")},
                                alt="Polia potrebné na riešenie problému")
        st.caption("Stavy: open = hľadá sa, found = otvorený strojovo čitateľný zdroj, partial = zdroj bez otvoreného "
                   "prístupu, requested = vyžiadané podľa 211/2000, partner = cez partnera, missing = nenájdené.")
        if st.button("Uložiť polia", icon=":material/save:", key="g1_fields_save"):
            for new, old in zip(edited, rows):
                if (new["kľúčové"], new["stav"], new["popis"], new["poznámka"]) != (old["kľúčové"], old["stav"], old["popis"],
                                                                                     old["poznámka"]):
                    d.update_field(new["id"], is_key=int(new["kľúčové"]), status=new["stav"], description=new["popis"],
                                   status_note=new["poznámka"] or None, edited_by_human=1)
            refresh()
with tab_s:
    sources = d.sources_for(hid)
    if not sources:
        st.caption("Zdroje hľadá agent v druhom kroku; licenciu a frekvenciu uloží iba z prečítanej stránky.")
    else:
        rows = []
        for s in sources:
            q = json_or(s["quality_json"], {})
            rows.append({"id": s["id"], "pole": s["field_name"], "zdroj": s["name"], "url": safe_url(s["url"]),
                         "prístup": s["access"], "licencia": s["license"] or "chýba", "frekvencia": s["update_frequency"] or "chýba",
                         "formáty": s["formats"] or "", "kvalita": q.get("skóre"), "stav": s["status"], "pôvod": s["origin"]})
        st.dataframe(rows, hide_index=True, alt="Zdroje dát k poliam",
                     column_config={"url": st.column_config.LinkColumn("URL"),
                                    "kvalita": st.column_config.ProgressColumn("kvalita", min_value=0, max_value=1, format="%.2f")})
        sid = st.selectbox("Detail zdroja", [r["id"] for r in rows],
                           format_func=lambda i: next(f"S{i}: {r['pole']} – {r['zdroj']}" for r in rows if r["id"] == i))
        src = d.get_source(sid)
        q = json_or(src["quality_json"], {})
        st.caption("Kvalita (zložky): " + ", ".join(f"{k} {v}" for k, v in q.items()) if q else "Kvalita sa ešte nepočítala.")
        if src["evidence_snippet"]:
            st.html(f"<blockquote>{esc(src['evidence_snippet'])}</blockquote>")
        ev = safe_url(src["evidence_url"])
        if ev:
            st.caption(f"Úryvok zo stránky: {ev}")
        checks = json_or(src["checks_json"], {})
        if checks.get("súbory"):
            st.caption("Súbory na stránke: " + ", ".join(checks["súbory"][:5]))
        with st.container(horizontal=True):
            if st.button("Overené ručne", icon=":material/verified:", key=f"g1_src_ok_{sid}"):
                d.update_source(sid, status="verified")
                refresh()
            if st.button("Zamietnuť zdroj", icon=":material/close:", key=f"g1_src_no_{sid}"):
                d.update_source(sid, status="rejected")
                refresh()
with tab_d:
    datasets = d.datasets_for(hid)
    if not datasets:
        st.caption("Datasety sa stiahnu zo zdrojov s odkazmi na CSV/XLSX/JSON (limit 2 GB na súbor, 50 GB spolu).")
    else:
        rows = []
        for ds in datasets:
            prof = json_or(ds["profile_json"], {})
            long = prof.get("dlhý_tvar") or {}
            rows.append({"tabuľka": ds["table_name"] + (f" (+ {long['tabuľka']})" if long.get("tabuľka") else ""), "url": safe_url(ds["url"]), "riadky": ds["rows"],
                         "stĺpce": len(json_or(ds["columns_json"], [])), "MB": round((ds["size_bytes"] or 0) / 1e6, 1),
                         "časový rozsah": "; ".join(f"{k}: {v[0]}–{v[1]}" for k, v in prof.get("time_span", {}).items()),
                         "stav": ds["status"], "poznámka": ds["note"] or ""})
        st.dataframe(rows, hide_index=True, alt="Stiahnuté datasety",
                     column_config={"url": st.column_config.LinkColumn("URL")})
        cov = d.coverage_for(hid)
        if cov:
            st.markdown("**Organizácie v dátach** (podľa IČO) – kandidáti na Bránu 2:")
            st.dataframe([{"organizácia": r["org_name"], "IČO": r["ico"], "tabuľka": r["table_name"], "riadky": r["rows"]}
                          for r in cov], hide_index=True, alt="Organizácie nájdené v datasetoch")
with tab_r:
    reps = d.reproductions_for(hid)
    if not reps:
        st.caption("Reprodukcia beží, keď je stiahnutý dataset a hypotéza má overené tvrdenie s číslom.")
    for r in reps:
        verdict = {"match": "zhoda", "close": "blízko", "mismatch": "nesedí", "failed": "nevyšla"}[r["verdict"]]
        with st.expander(f"{verdict} · cieľ {r['target_value']} {r['target_unit'] or ''} · výsledok "
                         f"{'' if r['result_value'] is None else round(r['result_value'], 3)}"):
            st.html(f"<blockquote>{esc(r['target_text'])}</blockquote>")
            if r["sql"]:
                st.code(r["sql"], language="sql")
            st.caption(" · ".join(x for x in (r["explanation"] or "", r["note"] or "",
                                              f"odchýlka {r['rel_error']:.1%}" if r["rel_error"] is not None else "") if x))
with tab_i:
    st.caption("Návrhy žiadostí podľa § 14 zákona č. 211/2000 Z. z. pre kľúčové polia bez otvoreného zdroja. Systém nič "
               "neodosiela – skopíruj, doplň údaje v {…} a pošli sám. Povinná osoba má lehotu 8 pracovných dní.")
    for r in d.info_requests_for(hid):
        with st.container(border=True):
            st.markdown(f"**{esc(r['field_name'] or 'pole')}** → {esc(r['authority'])} · stav: {r['status']}"
                        + (f" · odoslané {r['sent_at']} · lehota {r['due_at']}" if r["sent_at"] else ""))
            st.code(r["body"], language=None, wrap_lines=True)
            with st.container(horizontal=True):
                sent = st.date_input("Dátum odoslania", value=date.today(), key=f"g1_sent_{r['id']}")
                if st.button("Označiť ako odoslané", key=f"g1_req_sent_{r['id']}", icon=":material/outgoing_mail:"):
                    due = add_business_days(sent, 8, c.outreach.holidays)
                    d.update_info_request(r["id"], status="sent", sent_at=sent.isoformat(), due_at=due.isoformat())
                    if r["field_id"]:
                        d.update_field(r["field_id"], status="requested", edited_by_human=1)
                    refresh()
                for status, label in (("answered", "Odpovedali"), ("refused", "Zamietli"), ("partner", "Cez partnera")):
                    if st.button(label, key=f"g1_req_{status}_{r['id']}"):
                        d.update_info_request(r["id"], status=status, answered_at=date.today().isoformat())
                        refresh()
with tab_dec:
    with st.form(f"g1_decide_{hid}"):
        decision = st.selectbox("Rozhodnutie", ["passed", "paused", "stopped"],
                                format_func={"passed": "Brána 1 prešla (dáta sú)", "paused": "pozastaviť",
                                             "stopped": "zastaviť (dáta nezískané)"}.get)
        reason = st.text_area("Zdôvodnenie")
        if st.form_submit_button("Uložiť rozhodnutie", icon=":material/gavel:"):
            decide(d, hid, 1, decision, reason)
            refresh()
    for r in d.gate_decisions_for(hid, 1):
        st.caption(f"{r['decided_at'][:16]} · {r['decision']} · {r['reason'] or ''}")
