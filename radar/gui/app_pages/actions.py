import streamlit as st

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

if gpu:
    with st.expander("Načítané modely (aj z iných aplikácií, napr. astra)"):
        st.dataframe(gpu, hide_index=True, alt="Modely načítané v Ollame",
                     column_config={"vram_gb": st.column_config.NumberColumn("VRAM (GB)", format="%.1f"),
                                    "na_gpu": "na GPU", "do": "uvoľní sa o"})
        if st.button("Uvoľniť GPU", icon=":material/memory:", disabled=bool(active),
                     help="Uvoľní z VRAM všetky modely Ollamy (napr. po inej aplikácii). Počas akcie nie je dostupné."):
            st.toast("Uvoľnené: " + (", ".join(unload_gpu(c)) or "nič"), icon=":material/memory:")
            st.rerun()

ROLE_HELP = {"fast": "rýchly: extrakcia, triedenie, kontakty", "accurate": "presný: posúdenie dôkazov, zaradenie, "
             "ľudia, návrhy, Brány 1 a 2", "agent": "agent s nástrojmi: prieskum, test hypotéz, zdroje Brány 1",
             "embed": "embeddingy (CPU)"}
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
                st.rerun()
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
        st.info(f"Autopilot beží do {plan['do'].replace('T', ' ')} UTC · kolo {plan['kolo']} · naplánované: "
                + (", ".join(f"{p['akcia']} ({p['minúty']} min)" for p in plan["naplánované"]) or "nič")
                + ((" · bez práce: " + ", ".join(k for k, v in plan["preskočené"].items() if v == "bez práce"))
                   if plan.get("preskočené") else ""), icon=":material/autorenew:")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        hours = st.number_input("Ako dlho (hodiny)", 1, 72, 8, key="am_auto_hours")
        start_job_button("autopilot", ACTIONS_INFO.label, hours * 60, key="am_auto_go", icon=":material/autorenew:")

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
        st.caption(f"Prerušená #{j['id']} · {job_label(j['kind'])} · {(j['started_at'] or '')[:16].replace('T', ' ')} UTC. "
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
}

st.subheader("Zbieranie dát")
cols = st.columns(3)
for col, kind in zip(cols, ("scout", "validate", "people")):
    with col:
        action_card(kind, "am", extras.get(kind))

st.subheader("Brány 1 a 2")
cols = st.columns(3)
for col, kind in zip(cols, ("gate1", "gate2")):
    with col:
        action_card(kind, "am", extras.get(kind))

st.subheader("Ďalšie akcie")
cols = st.columns(3)
for i, kind in enumerate(("contacts", "drafts", "evidence", "themes", "topics", "critique", "bench", "run-all")):
    with cols[i % 3]:
        action_card(kind, "am", extras.get(kind))

st.subheader("História")
rows = []
for j in d.recent_jobs(25):
    args = json_or(j["args_json"], {})
    rows.append({"#": j["id"], "akcia": job_label(j["kind"]), "stav": JOB_STATUS.get(j["status"], j["status"]),
                 "začiatok": (j["started_at"] or "")[:16].replace("T", " "),
                 "koniec": (j["finished_at"] or "")[:16].replace("T", " "), "limit (min)": args.get("minutes"),
                 "hypotézy": ", ".join(f"H{x}" for x in args.get("hypotheses") or [])})
st.dataframe(rows, hide_index=True, alt="História akcií", column_config={"#": st.column_config.NumberColumn(format="%d")})
st.caption("Časy sú v UTC. Výstup každej akcie: logs/jobs/job-<číslo>-<akcia>.log (e-maily a telefóny maskované).")
