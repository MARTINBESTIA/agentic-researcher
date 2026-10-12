import streamlit as st

from radar.gui.common import cfg, db, esc, highlighted_window, refresh, safe_url
from radar.review import LABEL_TEXT, apply_label, choices_for, review_context

d, c = db(), cfg()
counts = d.review_counts()
if not counts:
    st.success("Nič nečaká na rozhodnutie.", icon=":material/done_all:")
    st.stop()
task = st.segmented_control("Úloha", sorted(counts), default=sorted(counts)[0],
                            format_func=lambda t: f"{t} ({counts[t]})", key="rv_task")
items = d.open_reviews(task, limit=200)
if not items:
    st.stop()
REASON = {"undecided": "model nerozhodol", "truncated": "orezaný výstup", "disagreement": "vzorky sa nezhodli",
          "low_agreement": "nízka zhoda (2:1)", "sample": "náhodná vzorka na eval", "invalid": "neplatný výstup"}
idx = st.number_input("Položka", min_value=1, max_value=len(items), value=1, step=1, key="rv_idx") - 1
item = items[idx]
ctx = review_context(d, item)
st.caption(f"{task} · {item['object_type']} {item['object_id']} · dôvod: {REASON.get(item['reason'], item['reason'])}")

with st.container(border=True):
    claim = ctx.get("claim")
    if claim:
        st.markdown(f"**Tvrdenie:** {esc(claim['problem'])}")
        st.caption(f"Kto: {claim['affected_party'] or '–'}")
        st.html(highlighted_window(ctx.get("doc_text", ""), claim["quote"]))
        url = safe_url(ctx.get("url"))
        if url:
            st.link_button("Otvoriť zdroj", url, icon=":material/open_in_new:")
    elif ctx.get("hypothesis"):
        st.markdown(f"**Hypotéza:** {esc(ctx['hypothesis']['title'])}")
        if ctx.get("role_map"):
            st.json(ctx["role_map"]["roles"], expanded=True)
    detail = ctx.get("detail") or {}
    if detail.get("block"):
        st.html(f"<blockquote>{esc(detail['block'])}</blockquote>")
    if detail.get("votes"):
        st.caption("Hlasy modelu: " + ", ".join(map(str, detail["votes"])))
    for j in ctx.get("judgments", [])[-3:]:
        st.caption(f"{j['model']} · verdikt {j['verdict']} · {j['latency_s'] or 0:.0f} s")

with st.form(f"label_{item['id']}"):
    choice = st.segmented_control("Správna odpoveď (gold label)", choices_for(task), format_func=lambda x: LABEL_TEXT.get(x, x))
    note = st.text_input("Poznámka")
    if st.form_submit_button("Uložiť rozhodnutie", icon=":material/gavel:"):
        if not choice:
            st.error("Vyber odpoveď.")
        else:
            result = apply_label(d, item["id"], choice, note or None)
            st.toast(f"Uložené: {result['effect']}")
            refresh()
