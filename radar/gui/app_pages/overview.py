import json

import streamlit as st

from radar.eta import local
from radar.crm import gate0_metrics
from radar.gui.common import action_card, cfg, db, jobs_panel
from radar.jobs import refresh_jobs
from radar.privacy import sender_placeholders

d, c = db(), cfg()


def metrics() -> None:
    counts, g, v = d.counts(), d.gate0_counts(), d.validation_counts()
    with st.container(horizontal=True):
        st.metric("Dokumenty", counts["documents"], border=True,
                  help="Stiahnuté texty (stránky, PDF, RSS, stránky prečítané agentmi), z ktorých sa vyťahujú citáty. "
                       "Zoznam a detail: stránka Dokumenty.")
        st.metric("Overené tvrdenia", counts["verified"], border=True)
        st.metric("Aktívne hypotézy", counts["active_hypotheses"], border=True)
        st.metric("Kandidáti z objavovania", counts["candidate_hypotheses"], border=True)
        st.metric("Otestované hypotézy", f"{v['tested']} / {v['total']}", border=True,
                  delta=f"{v['searched']} čaká na overenie" if v["searched"] else None, delta_color="off")
        st.metric("Ľudia", g["people"], border=True)
        st.metric("Návrhy na kontrolu", g["drafts"], border=True)


refresh_jobs(d)
if d.active_jobs():  # počas úlohy sa čísla obnovujú samé (iba tento blok, nie celá stránka)
    st.fragment(run_every="20s")(metrics)()
    st.caption("Čísla sa počas bežiacej úlohy obnovujú každých 20 s. Otestovaná je hypotéza až po posúdení "
               "nájdených citátov presným modelom (po dávke 5 hypotéz).")
else:
    metrics()
v = d.validation_counts()

prog = None
try:
    from radar.progress import customer_progress

    prog = customer_progress(c, d)
except Exception:  # noqa: BLE001 – prehľad sa zobrazí aj bez metrík postupu
    prog = None
if prog and prog["hypotézy"]:
    st.subheader("Postup k zákazníkovi")
    tot = prog["spolu"]
    with st.container(horizontal=True):
        for k, label in (("relevantné_kontakty", "Relevantné kontakty"), ("oslovení", "Oslovení"), ("odpovede", "Odpovede"),
                         ("rozhovory", "Rozhovory"), ("vzorky", "Vzorky dát"), ("piloty", "Piloty"),
                         ("nameraná_úspora", "Nameraná úspora"), ("ochota_platiť", "Ochota platiť")):
            st.metric(label, tot.get(k, 0), border=True)
    st.caption("Merajú cestu k zákazníkovi pre hypotézy na Bráne 0. Prevádzkové: "
               f"{prog['prevádzkové']['hypotézy']} hypotéz, {prog['prevádzkové']['modelové_rozhodnutia']} modelových "
               "rozhodnutí – to nie je postup.")

st.subheader("Akcie")
cols = st.columns(3)
extras = {"validate": f"Otestované {v['tested']} z {v['total']}, čaká na overenie {v['searched']}, "
                      f"neotestované {v['none']} (~2 min na hypotézu)."}
for col, kind in zip(cols, ("scout", "validate", "people")):
    with col:
        action_card(kind, "ov", extras.get(kind))
jobs_panel()
st.page_link("app_pages/actions.py", label="Všetky akcie, poradie a stav GPU: Správca akcií", icon=":material/memory:")
w = c.workflows
st.caption("Akcie bežia na pozadí aj po zatvorení prehliadača; nič neodosielajú. Nočný run-all robí to isté v menšom "
           f"({w.scout_sessions_per_run} sedenia prieskumu, {w.validate_per_run} hypotéz za noc).")

if not c.privacy.lia_completed:
    st.warning("Hľadanie ľudí a návrhy oslovení sú zablokované, kým nepotvrdíš posúdenie oprávneného záujmu "
               "(privacy.lia_completed v config.yaml).", icon=":material/lock:")
missing = sender_placeholders(c)
if missing:
    st.info("Schvaľovanie oslovení je zablokované: v config.yaml doplň profil odosielateľa (" + ", ".join(missing) + ").",
            icon=":material/badge:")

left, right = st.columns(2)
with left:
    st.subheader("Brána 0")
    rows = []
    for h in d.people_enabled_hypotheses():
        m = gate0_metrics(d, h["id"])
        rows.append({"hypotéza": f"H{h['id']}: {h['title'][:70]}", "rozhovory": f"{m['customer_interviews']}/{m['target']}",
                     "spomenuli sami": m["unprompted"], "návrh verdiktu": m["verdict_proposal"], "stav": h["gate0_status"]})
    if rows:
        st.dataframe(rows, hide_index=True, alt="Stav Brány 0 pre vybrané hypotézy")
    else:
        st.caption("Žiadna hypotéza nie je vybraná na hľadanie ľudí.")
with right:
    st.subheader("Čakajúce kontroly")
    reviews = d.review_counts()
    if reviews:
        st.dataframe([{"úloha": t, "položky": n} for t, n in sorted(reviews.items())], hide_index=True,
                     alt="Počet položiek na rozhodnutie človekom podľa úlohy")
    else:
        st.caption("Nič nečaká na rozhodnutie.")

st.subheader("Posledné behy")
runs = []
for r in reversed(d.recent_runs(15)):
    stats = json.loads(r["stats_json"] or "{}")
    stats.pop("report", None)
    runs.append({"krok": r["stage"], "začiatok": local(r["started_at"]) if r["started_at"] else "",
                 "koniec": local(r["finished_at"]) if r["finished_at"] else "beží / prerušené",
                 "výsledok": ", ".join(f"{k} {v}" for k, v in stats.items())[:200]})
st.dataframe(runs, hide_index=True, alt="Posledné kroky behu a ich výsledky")
