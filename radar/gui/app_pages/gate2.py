import json
from pathlib import Path

import streamlit as st

from radar.gate1 import decide
from radar.datasets import DatasetError
from radar.gate2 import complete_periods, data_limits, lock, series
from radar.gui.common import cfg, db, esc, jobs_panel, json_or, refresh, start_job_button

d, c = db(), cfg()
st.caption("Brána 2 – experimenty a demo pre dvojice hypotéza × organizácia: hypotéza a metrika sa predregistrujú "
           "(uzamknú) pred testom, spätný test počíta Python na historických dátach proti naivnému odhadu, demo je na "
           "dátach konkrétnej organizácie. Stop podmienky sú príznaky; rozhoduješ ty.")
hyps = d.gate2_hypotheses()
if not hyps:
    st.info("Zatiaľ žiadna hypotéza neprešla Bránou 1. Prejde automaticky pri pripravenosti dát ≥ 0,5 alebo keď tvoje "
            "dáta (Brána 1 → Tvoje dáta) dostanú vhodnosť ≥ 0,5; inak rozhodneš na stránke Brána 1.", icon=":material/info:")
    st.stop()
labels = {h["id"]: f"H{h['id']}: {h['title'][:80]} · {h['gate2_status']}" for h in hyps}
hid = st.selectbox("Hypotéza", list(labels), format_func=labels.get, key="g2_h")
start_job_button("gate2", "Spustiť experimenty a demo pre túto hypotézu", 60, [hid], key=f"g2_run_{hid}",
                 icon=":material/science:")
jobs_panel(show_last=False)

exps = d.experiments_for(hid)
if not exps:
    st.caption("Návrhy predregistrácie vzniknú v akcii (pre organizácie, ktoré sú v nájdených dátach).")
    st.stop()
rows = []
for e in exps:
    res, flags = json_or(e["result_json"], {}), json_or(e["flags_json"], {})
    rows.append({"E": e["id"], "organizácia": e["org_name"] or "celý dataset", "stav": e["status"], "metrika": e["metric"],
                 "zlepšenie": res.get("improvement"), "príznaky": ", ".join(k for k, v in flags.items() if v),
                 "demo": bool(e["demo_path"])})
st.dataframe(rows, hide_index=True, alt="Experimenty hypotéza × organizácia",
             column_config={"zlepšenie": st.column_config.NumberColumn("zlepšenie vs. naivný", format="percent")})
eid = st.selectbox("Experiment", [r["E"] for r in rows],
                   format_func=lambda i: next(f"E{i}: {r['organizácia']} · {r['stav']}" for r in rows if r["E"] == i))
e = d.get_experiment(eid)
flags = json_or(e["flags_json"], {})
if flags.get("chyby_návrhu"):
    st.warning("Návrh odkazuje na neexistujúce stĺpce: " + "; ".join(flags["chyby_návrhu"]), icon=":material/error:")

with st.container(border=True):
    st.markdown("**Predregistrácia**")
    if e["locked_at"]:
        st.markdown(f"{esc(e['statement'])}")
        st.caption(f"metrika {e['metric']} · naivný odhad {e['baseline']} · pravidlo: {e['success_rule']} · uzamknuté "
                   f"{e['locked_at']} · hash {e['prereg_hash'][:16]}…")
        st.code(json.dumps(json_or(e["spec_json"], {}), ensure_ascii=False, indent=2), language="json")
    else:
        with st.form(f"g2_prereg_{eid}"):
            statement = st.text_area("Hypotéza (napíš vopred)", e["statement"])
            cols = st.columns(3)
            metric = cols[0].selectbox("Metrika", ["MAE", "MAPE"], index=["MAE", "MAPE"].index(e["metric"]))
            baselines = ["last_value", "mean", "seasonal_naive"]
            baseline = cols[1].selectbox("Naivný odhad", baselines, index=baselines.index(e["baseline"]))
            rule = cols[2].text_input("Pravidlo úspechu", e["success_rule"])
            spec = st.text_area("Postup (tabuľka, stĺpce, model)", json.dumps(json_or(e["spec_json"], {}), ensure_ascii=False,
                                                                           indent=2), height=200)
            if st.form_submit_button("Uložiť návrh", icon=":material/save:"):
                try:
                    d.update_experiment(eid, statement=statement, metric=metric, baseline=baseline, success_rule=rule,
                                        spec_json=json.loads(spec))
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    refresh()
        with st.expander("Náhľad časového radu – čo sa po uzamknutí otestuje", icon=":material/show_chart:"):
            spec_now = json_or(e["spec_json"], {})
            try:
                limits = data_limits(d, hid, spec_now.get("table", ""))
                data, trimmed = complete_periods(series(c, hid, spec_now), spec_now.get("period"), limits,
                                                 spec_now.get("aggregation", "sum"))
            except (DatasetError, ValueError, KeyError) as exc:
                st.caption(f"Rad sa nedá zostaviť: {exc}")
            else:
                st.caption(f"{len(data)} období · dáta od {limits.get('od') or 'začiatku'} do {limits.get('do', '?')}"
                           + (f" · vyradené neúplné: {', '.join(trimmed['vyradené_neúplné'])}"
                              if trimmed.get("vyradené_neúplné") else "")
                           + (f" · bez riadkov (0): {len(trimmed['doplnené_nuly'])}" if trimmed.get("doplnené_nuly") else "")
                           + f". Spätný test potrebuje aspoň {c.gate2.min_history + 1} období.")
                if data:
                    st.line_chart({str(t): v for t, v in data}, height=220)
        if st.button("Uzamknúť predregistráciu (potom sa už nemení)", icon=":material/lock:", key=f"g2_lock_{eid}"):
            try:
                lock(c, d, eid)
            except ValueError as exc:
                st.error(str(exc))
            else:
                refresh()

res = json_or(e["result_json"], {})
if res:
    with st.container(border=True):
        st.markdown("**Spätný test**")
        with st.container(horizontal=True):
            st.metric(f"Chyba modelu ({res['metric']})", res["model_error"], border=True)
            st.metric("Chyba naivného odhadu", res["baseline_error"], border=True)
            st.metric("Zlepšenie", f"{res['improvement']:.1%}", border=True)
            st.metric("95 % interval rozdielu", f"{res['diff_ci95'][0]} až {res['diff_ci95'][1]}", border=True)
        if res.get("model"):
            st.caption(f"model {res['model']} · sezóna {res.get('season', 1)} "
                       f"{'(bez sezónnosti)' if res.get('season', 1) == 1 else 'období'}")
        if res.get("rozsah_dát"):
            st.caption(f"Dáta od {res['rozsah_dát'].get('od') or 'začiatku'} do {res['rozsah_dát'].get('do')}"
                       + (f" · vyradené neúplné obdobia: {', '.join(res['vyradené_neúplné'])}" if res.get("vyradené_neúplné") else "")
                       + (f" · obdobia bez riadkov (0): {', '.join(res['doplnené_nuly'])}" if res.get("doplnené_nuly") else ""))
        if flags.get("model_neprekonal_naivny_odhad"):
            st.warning("Stop podmienka: model neprekonal naivný odhad.", icon=":material/flag:")
        if flags.get("malo_obdobi"):
            st.info("Málo období na spoľahlivý záver (interval je široký).", icon=":material/info:")
        st.dataframe([{"obdobie": res["periods"][f["index"]], "skutočnosť": f["actual"], "model": round(f["model"], 2),
                       "naivný": round(f["baseline"], 2)} | ({"vybraný model": f["vybraný_model"]} if "vybraný_model" in f else {})
                      for f in res["folds"]], hide_index=True, alt="Predpovede po obdobiach")
if e["demo_path"] and Path(e["demo_path"]).exists():
    html_text = Path(e["demo_path"]).read_text(encoding="utf-8")
    st.download_button("Stiahnuť demo (HTML)", html_text, file_name=Path(e["demo_path"]).name, mime="text/html",
                       icon=":material/download:")
    with st.expander("Náhľad dema"):
        st.html(html_text)

with st.container(border=True):
    st.markdown("**Spätná väzba expertov a rozhodnutie**")
    expert = json_or(e["expert_json"], {})
    with st.form(f"g2_expert_{eid}"):
        new = st.radio("Priniesol výsledok expertom niečo nové?", ["áno", "nie", "zatiaľ nevieme"],
                       index=["áno", "nie", "zatiaľ nevieme"].index(expert.get("nové", "zatiaľ nevieme")), horizontal=True)
        note = st.text_area("Poznámka (kto, čo povedal)", expert.get("poznámka", ""))
        if st.form_submit_button("Uložiť spätnú väzbu"):
            flags["experti_nic_nove"] = new == "nie"
            d.update_experiment(eid, expert_json={"nové": new, "poznámka": note}, flags_json=flags)
            refresh()
    with st.form(f"g2_decide_{eid}"):
        decision = st.selectbox("Rozhodnutie pre túto organizáciu", ["passed", "paused", "stopped"])
        reason = st.text_area("Zdôvodnenie")
        if st.form_submit_button("Uložiť rozhodnutie", icon=":material/gavel:"):
            decide(d, hid, 2, decision, reason, org_id=e["org_id"])
            refresh()
    for r in d.gate_decisions_for(hid, 2):
        st.caption(f"{r['decided_at'][:16]} · organizácia {r['org_id'] or '–'} · {r['decision']} · {r['reason'] or ''}")
