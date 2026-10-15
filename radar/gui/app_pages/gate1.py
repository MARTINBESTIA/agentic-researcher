from datetime import date

import streamlit as st

from radar.bindings import (
    FIELD_STATUS_LABEL, confirm_binding, current_binding, field_route, manual_binding, reject_binding, usable_datasets,
)
from radar.datasets import DatasetError
from radar.gate1 import (
    REPRO_LABELS, USER_FILE_EXT, add_dataset_url, approve_without_personal, automated_work, decide, eligible, reopen,
    revalidate_passed, review_import, upload_path,
)
from radar.gui.common import cfg, db, esc, jobs_panel, json_or, refresh, safe_url, start_job_button
from radar.outreach import add_business_days

d, c = db(), cfg()
st.caption("Brána 1 – dáta a dataset: aké polia treba, kde sú (licencia, frekvencia, kvalita), dataset, z ktorého "
           "zreprodukuješ známe číslo, a čo treba vyžiadať podľa zákona 211/2000. Beží pre hypotézy vybrané na Bránu 0. "
           f"Pravidlo prechodu: pripravenosť dát ≥ {c.gate1.auto_gate2_readiness:.2f} a každé kľúčové pole má stĺpec "
           "potvrdený tebou na aktuálnej verzii datasetu (záložka Mapovanie polí) → Brána 1 prejde automaticky (Brána 2); "
           "inak hypotéza čaká na tvoje dáta, schému alebo potvrdenie stĺpcov. Nič sa nevyraďuje; tvoje ručné "
           "rozhodnutie má prednosť.")
hyps = d.gate1_hypotheses()
if not hyps:
    st.info("Žiadna hypotéza nie je vybraná na Bránu 0. Vyber ju v Problémoch alebo akciou „Zbierať ľudí ku overeným "
            "hypotézam“.", icon=":material/info:")
    st.stop()
STATUS = {"not_started": "nezačatá", "running": "rozpracovaná", "ready": "pripravená na rozhodnutie",
          "needs_input": "čaká na tvoje dáta", "passed": "prešla", "needs_revalidation": "treba znova overiť",
          "paused": "pozastavená", "stopped": "zastavená"}
labels = {h["id"]: f"H{h['id']}: {h['title'][:80]} · {STATUS.get(h['gate1_status'], h['gate1_status'])}" for h in hyps}
hid = st.selectbox("Hypotéza", list(labels), format_func=labels.get, key="g1_h")
h = d.get_hypothesis(hid)
summary = json_or(h["gate1_json"], {})

with st.container(horizontal=True):
    st.metric("Pripravenosť dát", f"{summary.get('skóre', 0):.2f}" if summary else "–", border=True)
    st.metric("Kľúčové polia so stĺpcom", f"{summary.get('kľúčové_nájdené', 0)} / {summary.get('kľúčové', 0)}", border=True,
              help="Stĺpec v stiahnutých dátach overený kódom (existuje, pokrytie, typ, pojem).")
    st.metric("Kľúčové potvrdené tebou", f"{summary.get('kľúčové_potvrdené', 0)} / {summary.get('kľúčové', 0)}", border=True)
    st.metric("Datasety", summary.get("datasety", 0), border=True)
    st.metric("Organizácie v dátach", summary.get("organizácie_v_dátach", 0), border=True)
    st.metric("Reprodukcia čísla", REPRO_LABELS.get(
        summary.get("reprodukcia"), "–"), border=True)
running_here = next((j for j in d.active_jobs() if j["kind"] in ("gate1", "autopilot")
                     and hid in (json_or(j["progress_json"], {}).get("s_modelom") or [])), None)
if running_here is not None:
    st.info(f"Prieskum dát pre túto hypotézu práve beží (úloha #{running_here['id']}, krok "
            f"{json_or(running_here['progress_json'], {}).get('krok', '?')}) – pripravenosť sa spočíta na konci úlohy.",
            icon=":material/hourglass_top:")
elif (h["gate1_status"] or "not_started") == "not_started":
    st.caption("Prieskum dát pre túto hypotézu ešte nebežal, preto pripravenosť chýba. Beh Brány 1 stihne s modelmi "
               "~5–6 hypotéz za 120 min (karta akcie v Správcovi akcií ukáže, ktoré); ostatné idú v ďalších behoch.")
for flag in summary.get("príznaky", []):
    st.warning(flag, icon=":material/flag:")
if summary:
    st.caption("Skóre = " + " + ".join(f"{w}·{k}" for k, w in summary.get("váhy", {}).items()) + " · zložky: " + ", ".join(
        f"{k} {v}" for k, v in summary.get("zložky", {}).items()) + ". Poradie, nie pravdepodobnosť úspechu.")
if h["gate1_status"] == "needs_input":
    st.warning(f"Pripravenosť dát je pod {c.gate1.auto_gate2_readiness:.2f} – Brána 2 čaká na tvoje dáta alebo aspoň "
               "schému (záložka Tvoje dáta). Automatický prieskum je hotový a sám sa neopakuje; zdroje znova prehľadá "
               "iba „Prehľadať znova“.", icon=":material/upload:")
unmet = summary.get("podmienky_prechodu") or []
if unmet and h["gate1_status"] != "stopped":
    st.warning("Podmienky prechodu, ktoré ešte chýbajú:\n\n" + "\n".join(f"- {esc(u)}" for u in unmet),
               icon=":material/rule:")
last = next(iter(d.gate_decisions_for(hid, 1)), None)
BY = {"rule": "pravidlo", "human": "ty"}
if h["gate1_status"] == "passed" and last is not None:
    st.success(f"Brána 1 prešla ({last['decided_at'][:10]}, rozhodol: {BY.get(last['decided_by'] or '', 'pravidlo/ty – staršie')}): "
               f"{last['reason'] or ''}", icon=":material/check_circle:")
if h["gate1_status"] == "needs_revalidation":
    st.warning("Treba znova overiť: " + (last["reason"] if last is not None else "") + " Potvrď stĺpce kľúčových polí "
               "(Mapovanie polí) – keď podmienky platia, Brána 1 prejde pravidlom znova.", icon=":material/sync_problem:")
if h["gate1_status"] == "passed":
    if st.button("Preveriť prechod s aktuálnymi dátami", icon=":material/fact_check:", key=f"g1_reval_{hid}",
                 help="Historické rozhodnutie vs. aktuálne dáta: prechod pravidlom bez platných podmienok dostane "
                      "„treba znova overiť“; tvoje ručné rozhodnutie ostane s príznakom."):
        res = revalidate_passed(c, d, [hid])
        st.toast("Podmienky platia." if not res else ", ".join(f"{k}: {v}" for k, v in res.items()))
        refresh()
if eligible(h):
    todo = automated_work(c, d, hid)
    if todo:
        st.caption("Zostáva: " + ", ".join(todo) + ".")
    start_job_button("gate1", "Spustiť prieskum dát pre túto hypotézu", 90, [hid], key=f"g1_run_{hid}",
                     icon=":material/database:")
elif h["gate1_status"] in ("needs_input", "ready", "passed"):
    st.caption("Prieskum dát je vyhodnotený – automatické behy ho preskakujú.")
    if st.button("Prehľadať znova", icon=":material/restart_alt:", key=f"g1_reopen_{hid}",
                 help="Polia, ku ktorým agent nenašiel zdroj, sa znova otvoria a hypotéza bude rozpracovaná; "
                      "spusti potom prieskum dát."):
        n = reopen(d, hid)
        st.toast(f"Znova otvorené polia: {n}. Hypotéza je rozpracovaná.")
        refresh()
jobs_panel(show_last=False)

tab_f, tab_m, tab_u, tab_s, tab_d, tab_r, tab_i, tab_dec = st.tabs(
    ["Polia", "Mapovanie polí", "Tvoje dáta", "Zdroje", "Datasety", "Reprodukcia čísla", "Infozákon", "Rozhodnutie"])
FIELD_STATUS = list(FIELD_STATUS_LABEL)
with tab_f:
    fields = d.fields_for(hid)
    if not fields:
        st.caption("Polia navrhne presný model v prvom kroku prieskumu dát.")
    else:
        review = any(x["status"] == "needs_review" for x in d.datasets_for(hid))
        rows = [{"id": f["id"], "pole": f["name"], "kľúčové": bool(f["is_key"]), "stav": f["status"],
                 "význam stavu": FIELD_STATUS_LABEL.get(f["status"], f["status"]), "ďalší krok": field_route(d, f, review),
                 "dostupnosť": f["availability"] or "", "popis": f["description"] or "", "prečo": f["rationale"] or "",
                 "poznámka": f["status_note"] or ""}
                for f in fields]
        edited = st.data_editor(rows, hide_index=True, key=f"g1_fields_{hid}",
                                disabled=["id", "pole", "prečo", "význam stavu", "ďalší krok", "dostupnosť"],
                                column_config={"stav": st.column_config.SelectboxColumn("stav", options=FIELD_STATUS),
                                               "kľúčové": st.column_config.CheckboxColumn("kľúčové")},
                                alt="Polia potrebné na riešenie problému")
        st.caption("Rebrík stavov: open (hľadá sa) → partial (iba odkaz na zdroj) → available (súbor overený požiadavkou) "
                   "→ loaded (dáta načítané, stĺpec neoverený) → found (stĺpec overený kódom). schema_only = iba opis "
                   "schémy bez nameraných hodnôt; requested = vyžiadané podľa 211/2000; partner = interné dáta od "
                   "partnera; missing = nenájdené. Ručne zmenený stav kód neprepíše, prechod Bránou 1 však vždy "
                   "potrebuje stĺpec kľúčového poľa potvrdený v Mapovaní polí.")
        if st.button("Uložiť polia", icon=":material/save:", key="g1_fields_save"):
            for new, old in zip(edited, rows):
                if (new["kľúčové"], new["stav"], new["popis"], new["poznámka"]) != (old["kľúčové"], old["stav"], old["popis"],
                                                                                     old["poznámka"]):
                    d.update_field(new["id"], is_key=int(new["kľúčové"]), status=new["stav"], description=new["popis"],
                                   status_note=new["poznámka"] or None, edited_by_human=1)
            refresh()
BINDING_STATUS = {"code_ok": "overené kódom – potvrď význam", "confirmed": "potvrdené tebou",
                  "code_failed": "kontrola kódom neprešla", "not_in_data": "v dátach nie je", "rejected": "zamietnuté",
                  "stale": "stará verzia dát"}
with tab_m:
    st.caption("Pole → verzia datasetu → stĺpec alebo výraz → jednotka → význam → dôkaz. Presný model navrhne stĺpec, kód "
               "overí, že existuje v dátach v stave ok, pokrytie, typ (dátum, suma, IČO) a pojem (napr. „patent“ musí "
               "byť v stĺpci – dátum exspirácie nie je dátum patentu). Význam kľúčového poľa potvrdzuješ ty; potvrdenie "
               "sa uloží ako referenčné hodnotenie.")
    fields = d.fields_for(hid)
    usable = usable_datasets(d, hid)
    if not usable:
        st.caption("Zatiaľ niet datasetu v stave ok – mapovanie začne po stiahnutí dát (alebo schváli import v záložke "
                   "Datasety).")
    bindings = d.bindings_for(hid)
    if fields and usable:
        rows = []
        for f in fields:
            cur = current_binding(d, f["id"], usable)
            last_b = next((b for b in bindings if b["field_id"] == f["id"]), None)
            b = cur or last_b
            checks = json_or(b["checks_json"], {}) if b is not None else {}
            rows.append({"pole": f["name"], "kľúčové": bool(f["is_key"]),
                         "väzba": f"{b['table_name']}.{b['expression']}" if b is not None and b["table_name"] else "–",
                         "stav": BINDING_STATUS.get(b["validation_status"], b["validation_status"]) if b is not None else "bez návrhu",
                         "jednotka": (b["unit"] if b is not None else "") or "", "význam": (b["meaning"] if b is not None else "") or "",
                         "pokrytie": checks.get("pokrytie"), "vzorka": ", ".join(checks.get("vzorka") or [])[:120],
                         "problémy": "; ".join(checks.get("problémy") or []) or ((b["note"] or "") if b is not None else "")})
        st.dataframe(rows, hide_index=True, alt="Mapovanie polí na stĺpce datasetov",
                     column_config={"pokrytie": st.column_config.ProgressColumn("pokrytie", min_value=0, max_value=1,
                                                                                format="%.2f")})
    todo = [b for b in bindings if b["validation_status"] == "code_ok"]
    if todo:
        bid = st.selectbox("Návrh na potvrdenie", [b["id"] for b in todo], key=f"g1_bind_{hid}",
                           format_func=lambda i: next(f"{b['field_name']}{' (kľúčové)' if b['is_key'] else ''} → "
                                                      f"{b['table_name']}.{b['expression']}" for b in todo if b["id"] == i))
        b = d.get_binding(bid)
        checks = json_or(b["checks_json"], {})
        st.caption(f"Jednotka: {b['unit'] or 'chýba'} · entita: {b['entity_kind'] or 'chýba'} · granularita: "
                   f"{b['grain'] or 'chýba'} · pokrytie {checks.get('pokrytie', '–')} · navrhol {b['proposed_by'] or '?'}")
        if b["meaning"]:
            st.markdown("**Význam podľa modelu:** " + esc(b["meaning"]))
        if b["evidence"]:
            st.html(f"<blockquote>{esc(b['evidence'])}</blockquote>")
        if checks.get("vzorka"):
            st.caption("Vzorka hodnôt: " + ", ".join(esc(v) for v in checks["vzorka"]))
        with st.container(horizontal=True):
            if st.button("Potvrdiť význam stĺpca", icon=":material/verified:", key=f"g1_bind_ok_{bid}"):
                confirm_binding(d, bid)
                refresh()
            why = st.text_input("Prečo nie (nepovinné)", key=f"g1_bind_why_{bid}")
            if st.button("Zamietnuť", icon=":material/close:", key=f"g1_bind_no_{bid}"):
                reject_binding(d, bid, why)
                refresh()
    if fields and usable:
        with st.form(f"g1_manual_bind_{hid}"):
            st.caption("Poznáš správny stĺpec? Zadaj ho – kód ho overí rovnako (existencia, pokrytie, typ, pojem) a po "
                       "kontrolách je rovno potvrdený.")
            fid = st.selectbox("Pole", [f["id"] for f in fields], format_func=lambda i: next(
                f["name"] for f in fields if f["id"] == i), key=f"g1_mb_f_{hid}")
            tables = [x["table_name"] for x in usable]
            table = st.selectbox("Tabuľka", tables, key=f"g1_mb_t_{hid}")
            expr = st.text_input("Stĺpec alebo výraz (napr. \"suma_eur\" alebo \"suma\" / 1000)", key=f"g1_mb_e_{hid}")
            unit = st.text_input("Jednotka", key=f"g1_mb_u_{hid}")
            meaning = st.text_input("Význam", key=f"g1_mb_m_{hid}")
            if st.form_submit_button("Overiť a uložiť väzbu", icon=":material/link:"):
                try:
                    manual_binding(c, d, hid, fid, table, expr.strip(), unit.strip(), meaning.strip())
                except (ValueError, DatasetError) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Väzba overená a potvrdená.", icon=":material/check:")
                    refresh()
COVERAGE = {"yes": "áno", "partial": "čiastočne", "no": "nie", "chýba": "chýba (model nepovedal)"}
with tab_u:
    st.caption("Máš dáta? Nahraj súbor a opíš ho. Nemáš? Napíš aspoň schému: stĺpce, jednotky, obdobie, podrobnosť "
               "(napr. obec × mesiac) a odkiaľ by dáta boli. Sudca (27B) posúdi každé pole zo záložky Polia (áno, "
               "čiastočne, nie); vhodnosť počíta kód: kľúčové polia spolu 0,7, ostatné 0,3, áno = 1, čiastočne = 0,5. "
               f"Vhodnosť ≥ {c.gate1.user_data_threshold:.2f} → Brána 1 prejde automaticky. Samotná schéma stačí na "
               "prechod, spätný test v Bráne 2 však potrebuje skutočné dáta.")
    st.caption("Nevkladaj dáta zamestnávateľa ani osobné údaje – tabuľka so stĺpcami ako meno, e-mail či adresa sa "
               "neuloží. Nahraný súbor sa po importe do DuckDB zmaže (ostáva tabuľka, názov a SHA-256).")
    with st.form(f"g1_input_{hid}", clear_on_submit=True):
        text = st.text_area("Popis dát alebo schéma", height=160, key=f"g1_in_text_{hid}",
                            placeholder="napr. obec; IČO; rok; mesiac; výdavky_eur – mesačne 2019–2025, rozpočty 40 obcí")
        upload = st.file_uploader("Súbor s dátami (nepovinné)", type=list(USER_FILE_EXT), key=f"g1_in_file_{hid}")
        if st.form_submit_button("Uložiť a dať posúdiť", icon=":material/upload:"):
            if not text.strip():
                st.error("Napíš aspoň krátky popis alebo schému dát.")
            else:
                try:
                    if upload is not None:
                        upload_path(c, hid, 0, upload.name)  # overí príponu ešte pred uložením vstupu
                    iid = d.add_gate1_input(hid, text, upload.name if upload is not None else None)
                    if upload is not None:
                        path = upload_path(c, hid, iid, upload.name)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(upload.getbuffer())
                    st.toast(f"Vstup č. {iid} uložený – posúdi ho akcia „Brána 1: posúdiť tvoje dáta“ (aj v autopilotovi).")
                except DatasetError as exc:
                    st.error(str(exc))
    if not d.fields_for(hid):
        st.caption("Polia ešte nevznikli – posúdenie počká na prvý krok prieskumu dát.")
    else:
        import re as _re
        import unicodedata as _ud

        def _col(name: str) -> str:
            ascii_ = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
            return _re.sub(r"[^a-z0-9]+", "_", ascii_).strip("_")[:40]

        header = ";".join(_col(f["name"]) for f in d.fields_for(hid) if f["name"] != "Doplnkové dáta (pridané ručne)")
        st.download_button("Stiahnuť šablónu CSV (stĺpce = polia Brány 1)", header + "\n", file_name=f"H{hid}_sablona.csv",
                           mime="text/csv", icon=":material/download:", key=f"g1_tpl_{hid}")
    inputs = d.gate1_inputs_for(hid)
    if any(i["status"] == "pending" for i in inputs):
        start_job_button("gate1-input", "Posúdiť moje dáta teraz", 15, [hid], key=f"g1_eval_{hid}",
                         icon=":material/fact_check:")
    for inp in inputs:
        ev = json_or(inp["evaluation_json"], {})
        state = {"pending": "čaká na posúdenie", "failed": "posúdenie zlyhalo"}.get(
            inp["status"], f"vhodnosť {inp['score']:.2f}" if inp["score"] is not None else inp["status"])
        with st.expander(f"Vstup č. {inp['id']} · {inp['created_at'][:16]} · {state}"
                         + (f" · súbor {inp['file_name']}" if inp["file_name"] else "")):
            st.text(inp["text"])
            if ev.get("zhrnutie"):
                st.markdown(esc(ev["zhrnutie"]))
            if ev.get("polia"):
                st.dataframe([{"pole": r["pole"], "kľúčové": r["kľúčové"], "pokrytie": COVERAGE.get(r["pokrytie"], r["pokrytie"]),
                               "váha": r["váha"], "dôvod": r["dôvod"]} for r in ev["polia"]],
                             hide_index=True, alt="Posúdenie polí z tvojich dát")
                fids = {f["name"]: f["id"] for f in d.fields_for(hid)}
                with st.form(f"g1_fix_{inp['id']}"):
                    st.caption("Nesedí posúdenie? Oprav ho – uloží sa ako referenčné hodnotenie (gold label) na meranie presnosti.")
                    fixes = {r["pole"]: st.selectbox(r["pole"], ["yes", "partial", "no"], format_func=COVERAGE.get,
                                                     index=["yes", "partial", "no"].index(r["pokrytie"]) if r["pokrytie"] in
                                                     ("yes", "partial", "no") else 2, key=f"g1_fix_{inp['id']}_{i}")
                             for i, r in enumerate(ev["polia"])}
                    if st.form_submit_button("Uložiť ako referenčné hodnotenie", icon=":material/verified:"):
                        for r in ev["polia"]:
                            if r["pole"] in fids:
                                d.add_label("gate1_user_eval", f"gate1_input:{inp['id']}", fids[r["pole"]], fixes[r["pole"]],
                                            note=f"model: {r['pokrytie']}")
                        st.toast("Referenčné hodnotenie uložené.", icon=":material/check:")
                st.caption(f"Vhodnosť = súčet váha × pokrytie = {ev.get('skóre', 0):.2f} (prah {ev.get('prah', 0):.2f})"
                           + (f" · tabuľka {ev['dataset']} je v záložke Datasety" if ev.get("dataset") else ""))
            for note in ev.get("poznámky", []):
                st.warning(note, icon=":material/info:")
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
        for ds in [x for x in datasets if x["status"] == "needs_review"]:
            rep = json_or(ds["profile_json"], {}).get("import") or {}
            with st.expander(f"Kontrola importu: {ds['table_name']} – " + "; ".join(rep.get("problémy") or [])[:120],
                             icon=":material/rule:"):
                st.caption(f"Adaptér: {rep.get('adaptér', '?')} · načítané riadky {rep.get('riadky', '?')} · odmietnuté "
                           f"{rep.get('odmietnuté_riadky', 0)} · stĺpce {rep.get('stĺpce', '?')} · kľúče "
                           f"{', '.join(rep.get('kľúče') or []) or 'chýba'} · jednotky "
                           f"{', '.join(f'{k}: {v}' for k, v in (rep.get('jednotky') or {}).items()) or 'chýba'}")
                if rep.get("vzorka"):
                    st.dataframe(rep["vzorka"], hide_index=True, alt="Vzorka riadkov importu")
                st.caption("Dataset na kontrole sa do pripravenosti, mapovania ani experimentov neráta. Schváľ ho, iba ak "
                           "riadky sú záznamy s významom (nie formátovanie, graf ani HTML).")
                note = st.text_input("Poznámka", key=f"g1_rev_n_{ds['id']}")
                with st.container(horizontal=True):
                    if st.button("Schváliť import", icon=":material/check:", key=f"g1_rev_ok_{ds['id']}"):
                        review_import(c, d, ds["id"], True, note)
                        refresh()
                    if st.button("Zamietnuť import", icon=":material/delete:", key=f"g1_rev_no_{ds['id']}"):
                        review_import(c, d, ds["id"], False, note)
                        refresh()
        for ds in [x for x in datasets if x["status"] == "personal_data_blocked"]:
            cols = json_or(ds["profile_json"], {}).get("personal_columns") or []
            with st.expander(f"Posúdenie GDPR: {ds['table_name']} – osobné údaje v stĺpcoch {', '.join(cols)}",
                             icon=":material/shield_person:"):
                st.caption("Dataset sa neuložil, lebo obsahuje osobné údaje. Ak na jeho použitie máš účel a právny "
                           "základ, dá sa uložiť bez týchto stĺpcov (minimalizácia). Over aj zvyšné stĺpce – napr. IČO "
                           "živnostníka alebo presná adresa môžu osobu identifikovať; za posúdenie zodpovedáš ty.")
                with st.form(f"g1_gdpr_{ds['id']}"):
                    decision = st.text_area("Účel a právny základ (tvoje posúdenie)", key=f"g1_gdpr_t_{ds['id']}")
                    sure = st.checkbox("Posúdil(a) som spracovanie podľa GDPR; zvyšné stĺpce osoby neidentifikujú",
                                       key=f"g1_gdpr_c_{ds['id']}")
                    if st.form_submit_button("Uložiť bez stĺpcov s osobnými údajmi", icon=":material/save:"):
                        if not sure:
                            st.error("Najprv potvrď posúdenie.")
                        else:
                            try:
                                approve_without_personal(d, ds["id"], decision)
                            except ValueError as exc:
                                st.error(str(exc))
                            else:
                                st.toast("Zapísané – dataset sa stiahne bez osobných stĺpcov pri najbližšom "
                                         "prieskume dát.", icon=":material/check:")
                                refresh()
    with st.form(f"g1_addurl_{hid}"):
        st.caption("Pridať dataset z odkazu (CSV, XLSX, JSON, XML, ZIP; stránka https://www.nku.gov.sk/datasety = kontrolné "
                   "zistenia NKÚ s IČO). Stiahne sa pri najbližšom prieskume dát; do DuckDB sa uloží iba bez osobných údajov.")
        url = st.text_input("Odkaz", key=f"g1_url_{hid}")
        if st.form_submit_button("Pridať", icon=":material/add_link:"):
            try:
                add_dataset_url(c, d, hid, url.strip())
            except Exception as exc:  # noqa: BLE001 – zlý odkaz
                st.error(str(exc))
            else:
                st.toast("Pridané – stiahne sa v akcii „Brána 1: prieskum dát“.", icon=":material/check:")
    if datasets:
        cov = d.coverage_for(hid)
        if cov:
            st.markdown("**Organizácie v dátach** (podľa IČO) – kandidáti na Bránu 2:")
            st.caption("Stĺpec: strana zmluvy, podľa ktorej sa organizácia v dátach našla (prednostne jej vlastná – "
                       "objednávateľ, prijímateľ; inak protistrana).")
            st.dataframe([{"organizácia": r["org_name"], "IČO": r["ico"], "tabuľka": r["table_name"],
                           "stĺpec": r["column_name"], "riadky": r["rows"]}
                          for r in cov], hide_index=True, alt="Organizácie nájdené v datasetoch")
with tab_r:
    reps = d.reproductions_for(hid)
    if not reps:
        st.caption("Reprodukcia beží, keď je stiahnutý dataset a hypotéza má overené tvrdenie s číslom.")
    for r in reps:
        verdict = REPRO_LABELS.get(r["verdict"], r["verdict"])
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
            decide(d, hid, 1, decision, reason, by="human")
            refresh()
    for r in d.gate_decisions_for(hid, 1):
        st.caption(f"{r['decided_at'][:16]} · {r['decision']} · rozhodol: {BY.get(r['decided_by'] or '', 'staršie')} · "
                   f"{r['reason'] or ''}")
