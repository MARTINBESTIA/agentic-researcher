import streamlit as st

from radar.evals import EVAL_TASKS, MIN_LABELS, eval_ready, sample_for_review
from radar.gui.common import cfg, db, json_or, start_job_button

d, c = db(), cfg()
labels = d.label_counts()
st.caption(f"Eval porovná profily A–D (rýchly/presný model, s uvažovaním a bez) na položkách s gold labelom. "
           f"Pred záverom treba aspoň {MIN_LABELS} labelov na úlohu.")
st.dataframe([{"úloha": t, "labely": labels.get(t, 0), "stav": "dosť dát" if labels.get(t, 0) >= MIN_LABELS else "nedostatok dát"}
              for t in EVAL_TASKS], hide_index=True, alt="Počet gold labelov podľa úlohy")
with st.container(horizontal=True):
    task = st.selectbox("Úloha", EVAL_TASKS, key="ev_task")
    n = st.number_input("Koľko položiek", 5, 100, 20, step=5, key="ev_n")
    if st.button("Pridať na označenie (Kontrola)", icon=":material/playlist_add:", key="ev_sample",
                 help="Vyváži vzorku podľa verdiktu modelu; označíš ju na stránke Kontrola – každá označená položka "
                      "je gold label."):
        st.toast(f"Do Kontroly pridaných: {sample_for_review(d, task, int(n))}", icon=":material/check:")
ready = eval_ready(d)
start_job_button("eval", "Spustiť eval presnosti (" + (", ".join(ready) or f"zatiaľ žiadna úloha s {MIN_LABELS} labelmi")
                 + ")", 60, key="ev_run", icon=":material/rule:", disabled=not ready)
st.code("python -m radar eval --task entail --sample 30      # vzorka do Kontroly na označenie\n"
        "python -m radar eval --task entail --synthetic 20   # prípady s nastrčenou chybou\n"
        "python -m radar eval --task entail --profile all", language="bash")
rows = []
for r in d.eval_results():
    m = json_or(r["metrics_json"], {})
    b = m.get("binary") or {}
    lo, hi = m.get("accuracy_ci", (0, 0))
    rows.append({"čas": r["run_at"][:16].replace("T", " "), "úloha": r["task"], "profil": r["profile"], "model": r["model"],
                 "uvažovanie": bool(r["thinking"]), "n": r["n"], "správne": m.get("accuracy"),
                 "interval 95 %": f"{lo:.0%}–{hi:.0%}", "presnosť": b.get("precision"), "úplnosť": b.get("recall"),
                 "F1": b.get("f1"), "s/položku": m.get("avg_s"), "tokeny/položku": m.get("avg_tokens")})
st.subheader("Výsledky")
st.dataframe(rows, hide_index=True, alt="Výsledky evalu podľa profilu",
             column_config={k: st.column_config.NumberColumn(format="%.2f") for k in ("správne", "presnosť", "úplnosť", "F1", "s/položku")})
st.subheader("Volania modelov")
st.dataframe([dict(r) for r in d.judgment_counts()], hide_index=True, alt="Počet volaní a priemerný čas podľa úlohy a modelu")
