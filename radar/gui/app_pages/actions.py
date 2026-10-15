import streamlit as st

from radar.eta import local
from radar.gui.common import (
    JOB_STATUS, action_card, cfg, config_path, db, job_label, jobs_panel, json_or, start_job_button,
)
from radar.jobs import (
    gpu_status, interrupted_jobs, ollama_models, queue_stalled, refresh_jobs, restart_job, start_next, unload_gpu,
)
from radar.models import MODEL_SETTINGS_KEY, PROFILES
from radar.workflows import ACTIONS

ACTIONS_INFO = ACTIONS["autopilot"]


def autopilot_status(d) -> dict | None:
    """Plán posledného kola, ak autopilot ešte beží (v poradí alebo práve beží niektorá jeho úloha)."""
    import json

    for j in d.recent_jobs(40):
        if j["kind"] == "autopilot" and j["status"] in ("done", "running"):
            plan = (json.loads(j["progress_json"] or "{}") or {}).get("autopilot")
            alive = any(q["kind"] == "autopilot" for q in d.queued_jobs()) or d.active_jobs()
            return plan if plan and alive else None
    return None

d, c = db(), cfg()
refresh_jobs(d)
active, queued = d.active_jobs(), d.queued_jobs()
gpu = gpu_status(c)

st.caption("Všetky akcie, ktoré zbierajú alebo spracúvajú dáta lokálnymi modelmi, na jednom mieste. Na 8 GB VRAM sa "
           "zmestí iba jeden veľký model, preto naraz beží jedna akcia; ďalšie zaraď do poradia, alebo bežiacu zastav "
           "a spusti inú. Rozpracovaný stav ostáva v databáze. Nič sa neodosiela.")

with st.container(horizontal=True):
    st.metric("Beží", job_label(active[0]["kind"]) if active else "nič", border=True)
    st.metric("V poradí", len(queued), border=True)
    if gpu is None:
        st.metric("GPU (Ollama)", "nedostupné", border=True)
    else:
        loaded = [m for m in gpu if m["vram_gb"]]
        st.metric("Model v GPU", ", ".join(m["model"].removesuffix(":latest") for m in loaded) or "žiadny",
                  f"{sum(m['vram_gb'] for m in loaded):.1f} GB VRAM" if loaded else None, delta_color="off", border=True)

swaps = d.get_setting("model_swaps", []) or []
if swaps:
    by_model: dict[str, list[float]] = {}
    for s in swaps:
        by_model.setdefault(s["model"], []).append(s["spolu_s"])
    st.caption("Výmena modelu v GPU (medián posledných načítaní): " + ", ".join(
        f"{m.removeprefix('radar-')} {sorted(v)[len(v) // 2]:.0f} s ({len(v)}×)" for m, v in by_model.items())
        + ". Každá výmena 9B ↔ 27B ↔ 35B stojí tento čas – kroky akcií sú zoradené tak, aby ich bolo čo najmenej.")
if gpu:
    with st.expander("Načítané modely (aj z iných aplikácií, napr. astra)"):
        st.dataframe(gpu, hide_index=True, alt="Modely načítané v Ollame",
                     column_config={"vram_gb": st.column_config.NumberColumn("VRAM (GB)", format="%.1f"),
                                    "na_gpu": "na GPU", "do": "uvoľní sa o"})
        if st.button("Uvoľniť GPU", icon=":material/memory:", disabled=bool(active),
                     help="Uvoľní z VRAM všetky modely Ollamy (napr. po inej aplikácii). Počas akcie nie je dostupné."):
            st.toast("Uvoľnené: " + (", ".join(unload_gpu(c)) or "nič"), icon=":material/memory:")
            st.rerun()

ROLE_HELP = {"fast": "rýchly: extrakcia, triedenie, kontakty", "accurate": "presný: mapy rolí, návrhy oslovení, "
             "kritika, Brány 1 a 2", "judge": "sudca: posúdenie dôkazov, zaradenie do hypotéz, zlúčenie",
             "agent": "agent s nástrojmi: prieskum, test hypotéz, zdroje Brány 1", "embed": "embeddingy (CPU)",
             "reviewer": "hodnotiteľ: kontrola prehľadov domén, návrhov prínosu a oslovení"}
with st.expander("Modely – výber pre ďalšie spustené akcie", icon=":material/tune:"):
    installed = ollama_models(c)
    chosen = d.get_setting(MODEL_SETTINGS_KEY, {}) or {}
    if installed is None or c.models is None:
        st.caption("Ollama nie je dostupná alebo config.yaml nemá sekciu models – platí config.yaml.")
    else:
        names = [m["model"] for m in installed]
        bench = d.bench_by_model()
        rows = []
        for m in installed:
            b = bench.get(m["model"]) or bench.get(m["model"].removesuffix(":latest"))
            rows.append({**m, "generovanie tok/s": round(b["gen_tps"], 1) if b and b["gen_tps"] else None,
                         "prompt tok/s": round(b["prompt_tps"]) if b and b["prompt_tps"] else None,
                         "JSON": bool(b["json_ok"]) if b and b["json_ok"] is not None else None,
                         "nástroje": bool(b["tools_ok"]) if b and b["tools_ok"] is not None else None})
        st.dataframe(rows, hide_index=True, alt="Nainštalované modely a posledné meranie rýchlosti")
        with st.form("models_form", border=False):
            picks = {}
            for role, role_cfg in c.models.roles.items():
                current = (chosen.get("roles") or {}).get(role) or role_cfg.model
                options = names if current in names or f"{current}:latest" in names else [current] + names
                index = options.index(current) if current in options else options.index(f"{current}:latest")
                picks[role] = st.selectbox(f"{role} – {ROLE_HELP.get(role, '')}", options, index=index, key=f"model_{role}")
            profile = st.selectbox("Profil (fast = všetko rýchly model, accurate = všetko presný)", list(PROFILES),
                                   index=list(PROFILES).index(chosen.get("profile") or c.models.profile))
            if st.form_submit_button("Uložiť výber modelov", icon=":material/save:"):
                changed = {r: m for r, m in picks.items() if m not in (c.models.roles[r].model,
                                                                       f"{c.models.roles[r].model}:latest")}
                d.set_setting(MODEL_SETTINGS_KEY, {"roles": changed, "profile": profile})
                st.toast("Výber modelov uložený – platí pre ďalšie spustené akcie.", icon=":material/check:")
                st.session_state["models_changed"] = True
                st.rerun()
        if st.session_state.get("models_changed"):
            st.info("Modely sa zmenili. Odporúčam zmerať ich rýchlosť a schopnosti (JSON, nástroje); presnosť porovná "
                    "eval na referenčných hodnoteniach (Kontrola → gold labely).", icon=":material/speed:")
            start_job_button("bench", "Zmerať rýchlosť vybraných modelov", 20, key="am_bench_after_change",
                             icon=":material/speed:")
            from radar.evals import MIN_LABELS, eval_ready

            ready = eval_ready(d)
            if ready:
                start_job_button("eval", "Porovnať presnosť (eval: " + ", ".join(ready) + ")", 60,
                                 key="am_eval_after_change", icon=":material/rule:")
            else:
                labels = d.label_counts()
                st.caption("Eval presnosti čaká na gold labely: " + ", ".join(
                    f"{t} {labels.get(t, 0)}/{MIN_LABELS}" for t in ("entail", "cluster_assign", "role_fit", "draft_check"))
                    + " – pridáš ich na stránke Eval → „Pridať na označenie“ a označíš v Kontrole.")
        if chosen:
            st.caption("Aktívny výber z GUI (prepisuje config.yaml): " + ", ".join(
                f"{r} = {m}" for r, m in (chosen.get("roles") or {}).items()) + f" · profil {chosen.get('profile')}")
            if st.button("Obnoviť podľa config.yaml", icon=":material/restart_alt:"):
                d.delete_setting(MODEL_SETTINGS_KEY)
                st.rerun()
        st.caption("Na 8 GB VRAM sa zmestí jeden veľký model; väčšie modely (napr. 27B, MoE 35B) bežia čiastočne na CPU. "
                   "Bežiaca akcia si ponechá modely, s ktorými začala. Rýchlosť zmeria akcia „Zmerať rýchlosť modelov“.")

with st.container(border=True):
    st.markdown("**Nechať bežať všetko (autopilot)**")
    st.caption(ACTIONS_INFO.what)
    plan = autopilot_status(d)
    if plan:
        st.info(f"Autopilot beží do {local(plan['do'])} · kolo {plan['kolo']} · naplánované: "
                + (", ".join(f"{p['akcia']} ({p['minúty']} min)" for p in plan["naplánované"]) or "nič")
                + ((" · bez práce: " + ", ".join(k for k, v in plan["preskočené"].items() if v == "bez práce"))
                   if plan.get("preskočené") else ""), icon=":material/autorenew:")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        hours = st.number_input("Ako dlho (hodiny)", 1, 72, 8, key="am_auto_hours")
        start_job_button("autopilot", ACTIONS_INFO.label, hours * 60, key="am_auto_go", icon=":material/autorenew:")

FUNNEL = ACTIONS["funnel"]
with st.container(border=True):
    from radar.funnel import STEP_LABEL, waiting_for_you

    st.markdown(f"**{FUNNEL.label} (donekonečna)**")
    st.caption(FUNNEL.what)
    funnel_rows = d.funnel_log(12)
    if any(j["kind"] == "funnel" for j in list(active) + list(queued)):
        st.info("Lievik beží" + (f" · posledný krok: {STEP_LABEL.get(funnel_rows[0]['step'], funnel_rows[0]['step'])}"
                                 + (f" H{funnel_rows[0]['hypothesis_id']}" if funnel_rows[0]["hypothesis_id"] else "")
                                 if funnel_rows else ""), icon=":material/filter_alt:")
    if funnel_rows:
        st.dataframe([{"čas": local(r["started_at"]), "hypotéza": f"H{r['hypothesis_id']}" if r["hypothesis_id"] else "–",
                       "krok": STEP_LABEL.get(r["step"], r["step"]), "výsledok": r["outcome"] or "beží…",
                       "ďalej": STEP_LABEL.get(r["next_step"] or "", r["reason"] or ""),
                       "chyba": "áno" if r["failed"] else ""} for r in funnel_rows],
                     hide_index=True, alt="Posledné kroky lievika: hypotéza, krok, výsledok a čo ďalej")
    waiting = waiting_for_you(d)
    if waiting:
        st.warning(f"Čaká na teba {len(waiting)} hypotéz (lievik ich preskakuje, kým nerozhodneš alebo nevložíš dáta).",
                   icon=":material/person_alert:")
        st.dataframe(waiting, hide_index=True, alt="Hypotézy, pri ktorých lievik čaká na tvoje dáta alebo rozhodnutie")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        f_hours = st.number_input("Ako dlho (hodiny, 0 = donekonečna)", 0, 720, 0, key="am_funnel_hours")
        start_job_button("funnel", FUNNEL.label, f_hours * 60, key="am_funnel_go", icon=":material/filter_alt:",
                         help="Jedna úloha = jeden krok jednej hypotézy; ďalšia sa zaradí sama. Zastavenie: „Zastaviť "
                              "úlohu“ pri bežiacom kroku alebo „Zastaviť všetko vrátane poradia“.")

st.subheader("Bežiaca akcia a poradie")
if queue_stalled(d):
    st.warning(f"Poradie stojí: nič nebeží a {len(queued)} akcií čaká (napr. po reštarte alebo uspatí počítača "
               "sa ďalšia akcia sama nespustí).", icon=":material/pause_circle:")
    if st.button("Pokračovať v poradí", icon=":material/play_arrow:", key="am_resume_queue"):
        job_id = start_next(c, d)
        st.toast(f"Spustená úloha #{job_id} z poradia.", icon=":material/rocket_launch:")
        st.rerun()
for j in interrupted_jobs(d)[:3]:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"Prerušená #{j['id']} · {job_label(j['kind'])} · {local(j['started_at'])}. "
                   "Rozpracovaný stav je v databáze – akcia pokračuje tam, kde prestala.")
        if st.button("Spustiť znova", icon=":material/replay:", key=f"am_restart_{j['id']}",
                     help="Rovnaký limit a hypotézy; ak niečo beží, zaradí sa do poradia."):
            new_id = restart_job(c, d, config_path(), j["id"])
            st.toast(f"Úloha #{new_id}: {job_label(j['kind'])}", icon=":material/replay:")
            st.rerun()
if not active and not queued:
    st.caption("Nič nebeží.")
jobs_panel(show_last=False)

v = d.validation_counts()
extras = {
    "validate": f"Neotestované {v['none']}, čaká na overenie {v['searched']}, otestované {v['tested']} z {v['total']} "
                f"(~2 min na hypotézu, celá fronta ~{v['none'] * 2 / 60:.0f} h).",
    "evidence": f"Čaká na posúdenie {d.pending_entailment_count()} tvrdení, na zaradenie {d.unassigned_count()}.",
    "gate1-input": f"Tvoje vstupy na posúdenie: {len(d.pending_gate1_inputs())} (vkladáš ich na stránke Brána 1 → Tvoje dáta).",
    "catalog": "Index: {datasety} datasetov, aktualizovaný {kedy}.".format(
        datasety=d.catalog_stats()["datasety"], kedy=(d.catalog_stats()["aktualizované"] or "nikdy")[:10]),
}

st.subheader("Zbieranie dát")
cols = st.columns(3)
for col, kind in zip(cols, ("scout", "validate", "people")):
    with col:
        action_card(kind, "am", extras.get(kind))

st.subheader("Brány 1 a 2")
cols = st.columns(4)
for col, kind in zip(cols, ("gate1", "gate1-input", "cards", "gate2")):
    with col:
        action_card(kind, "am", extras.get(kind))

st.subheader("Ďalšie akcie")
cols = st.columns(3)
for i, kind in enumerate(("contacts", "drafts", "evidence", "themes", "topics", "critique", "catalog", "bench", "eval",
                          "run-all")):
    with cols[i % 3]:
        action_card(kind, "am", extras.get(kind))

st.subheader("História")
rows = []
for j in d.recent_jobs(25):
    args = json_or(j["args_json"], {})
    rows.append({"#": j["id"], "akcia": job_label(j["kind"]), "stav": JOB_STATUS.get(j["status"], j["status"]),
                 "začiatok": local(j["started_at"]) if j["started_at"] else "",
                 "koniec": local(j["finished_at"]) if j["finished_at"] else "", "limit (min)": args.get("minutes"),
                 "hypotézy": ", ".join(f"H{x}" for x in args.get("hypotheses") or [])})
st.dataframe(rows, hide_index=True, alt="História akcií", column_config={"#": st.column_config.NumberColumn(format="%d")})
st.caption("Časy sú v miestnom čase. Výstup každej akcie: logs/jobs/job-<číslo>-<akcia>.log (e-maily a telefóny maskované).")
