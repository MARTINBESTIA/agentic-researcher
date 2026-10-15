from datetime import date, datetime, timezone

import streamlit as st

from radar.gui.common import cfg, db, esc, json_or, pick_hypothesis, refresh
from radar.outcomes import FLAGS, RELEVANCE, REPLIES, OutcomeError, outcome_metrics, record_outcome
from radar.outreach import (
    MODE_LABEL, OutreachError, approve, follow_up_text, mailto_link, mark_follow_up_sent, mark_sent, recheck, set_status,
)
from radar.privacy import sender_placeholders

d, c = db(), cfg()
st.caption("Systém nič neodosiela. Návrh skontroluj, schváľ, skopíruj alebo otvor v pošte a pošli sám. Každý návrh "
           "má reťazec prečo tento kontakt → predpokladaná potreba → čo môže dostať → čo minimálne potrebuješ; potreba je "
           "hypotéza, kým ju kontakt nepotvrdí.")
today = datetime.now(timezone.utc).date().isoformat()
with st.container(horizontal=True):
    st.metric("Schválené dnes", f"{d.approvals_on(today)}/{c.outreach.daily_cap}", border=True)
    st.metric("Follow-upy na dnes", len(d.follow_ups_due(date.today().isoformat())), border=True)
missing = sender_placeholders(c)
if missing:
    st.info("Schvaľovanie je zablokované, kým profil odosielateľa obsahuje placeholdery: " + ", ".join(missing),
            icon=":material/badge:")

hid = pick_hypothesis(d, "out_h")
if hid is None:
    st.stop()
metrics = outcome_metrics(d, [hid])
if metrics["oslovené"]:
    with st.expander(f"Učenie z odpovedí – po organizáciách (oslovené: {metrics['oslovené']})", icon=":material/school:"):
        st.dataframe(metrics["riadky"], hide_index=True, alt="Postup po unikátnych organizáciách")
        if metrics["režimy"]:
            st.dataframe([{**m, "režim": MODE_LABEL.get(m["režim"], m["režim"])} for m in metrics["režimy"]],
                         hide_index=True, alt="Oslovené organizácie a relevantné odpovede podľa režimu oslovenia")
        st.caption(f"Príprava na kontakt (medián): {metrics['príprava_min_medián'] or '–'} min (n = {metrics['príprava_n']}) · "
                   f"dodanie výstupu (medián): {metrics['dodanie_dni_medián'] or '–'} dní (n = {metrics['dodanie_n']}). "
                   "Počíta sa po organizáciách, nie po počte správ.")
STATUS = ["draft", "needs_edit", "approved", "sent", "replied", "scheduled", "interviewed", "no_reply", "declined",
          "bounced", "no_show", "blocked"]
shown = st.pills("Stav", STATUS, selection_mode="multi", default=["draft", "needs_edit", "approved", "sent", "replied",
                                                                  "scheduled", "blocked"], key="out_st")
items = [o for o in d.outreach_list(hid) if o["status"] in (shown or [])]
if not items:
    st.caption("Žiadne oslovenia v zvolených stavoch (návrhy vytvorí krok drafts).")
    st.stop()

READY = {"ready": "pripravené", "needs_input": "rozpracované"}
for o in items:
    checks = json_or(o["checks_json"], {})
    pkg = json_or(o["package_json"], {})
    who = o["full_name"] or "funkčný kontakt organizácie"
    title = (f"{who} · {o['org_name'] or ''} · {MODE_LABEL.get(o['mode'] if pkg else None, o['mode'])} · {o['channel']} "
             f"{o['variant']} · {o['status']}" + (f" · {READY.get(o['readiness'], o['readiness'])}" if o["readiness"] else ""))
    with st.expander(title, expanded=o["status"] in ("draft", "needs_edit")):
        if o["status"] == "blocked":
            st.warning(o["body"], icon=":material/gavel:")
            continue
        if pkg:
            why, need = pkg.get("prečo_tento_kontakt") or {}, pkg.get("potreba") or {}
            st.dataframe([
                {"krok": "Prečo tento kontakt", "obsah": f"{why.get('text', '')} – {why.get('dôkaz', '')}",
                 "zdroj": why.get("zdroj") or "chýba"},
                {"krok": "Predpokladaná potreba", "obsah": need.get("text", ""),
                 "zdroj": {"hypothesis": "hypotéza", "confirmed": "potvrdené kontaktom"}.get(need.get("stav"), need.get("stav"))},
                {"krok": "Čo môže dostať", "obsah": pkg.get("výstup") or "– (iba otázka na skúsenosť)",
                 "zdroj": f"pomôže: {pkg.get('rozhodnutie') or '–'}"},
                {"krok": "Čo minimálne potrebujem", "obsah": pkg.get("minimálny_vstup") or "", "zdroj": ""},
                {"krok": "Čo ešte nevieme", "obsah": pkg.get("čo_nevieme") or "", "zdroj": ""},
            ], hide_index=True, alt="Reťazec dôvodov návrhu oslovenia")
            if pkg.get("záväzok"):
                st.caption(f"Schválený záväzok: {pkg['záväzok']}")
            for r in pkg.get("pripravenosť") or []:
                st.warning(r, icon=":material/edit_note:")
        problems = list(checks.get("problems", []))
        model = checks.get("model") or {}
        for p in model.get("problems", []):
            problems.append(f"Model: „{p.get('sentence', '')}“ – {p.get('issue', '')}")
        if problems:
            st.error("\n".join(f"- {esc(p)}" for p in problems), icon=":material/error:")
        elif checks.get("model_note"):
            st.info(checks["model_note"])
        if checks.get("contact_kind") == "hidden_contact":
            st.info("E-mail je na stránke skrytý: otvor zdrojovú stránku a adresu doplň ručne.", icon=":material/visibility_off:")
        if checks.get("generic"):
            st.caption("Ide na generickú adresu organizácie – správa prosí o nasmerovanie na správneho človeka.")
        key = f"o{o['id']}"
        subject = st.text_input("Predmet", o["subject"] or "", key=key + "s") if o["channel"] == "email" else None
        body = st.text_area("Text", o["body"], height=320, key=key + "b")
        st.caption(f"Slová bez pätičky: {checks.get('words', '?')} · znaky: {len(body)} · fakty: "
                   + "; ".join(f"{f['id']}: {f['text'][:80]}" for f in json_or(o["facts_json"], [])))
        with st.container(horizontal=True):
            if st.button("Uložiť a skontrolovať", key=key + "chk", icon=":material/spellcheck:"):
                recheck(c, d, o["id"], body, subject)
                refresh()
            if st.button("Schváliť na odoslanie", key=key + "ok", icon=":material/task_alt:", disabled=o["status"] != "draft"):
                try:
                    approve(c, d, o["id"])
                    refresh()
                except OutreachError as exc:
                    st.error(str(exc))
            if o["status"] in ("approved", "sent") and o["contact_kind"] == "email" and o["channel"] == "email":
                link = mailto_link(o["contact_value"], o["subject"] or "", o["body"], c.outreach.mailto_max_chars)
                if link:
                    st.link_button("Otvoriť v pošte", link, icon=":material/forward_to_inbox:")
                else:
                    st.caption("Text je na mailto: príliš dlhý – skopíruj ho.")
        if o["status"] in ("approved", "sent"):
            st.code(o["body"], language=None, wrap_lines=True)
            if o["contact_value"]:
                st.caption(f"Adresát: {o['contact_value']}")
        if o["status"] == "approved":
            sent_day = st.date_input("Dátum odoslania", value=date.today(), key=key + "d")
            if st.button("Označiť ako odoslané (ručne)", key=key + "sent", icon=":material/send:"):
                mark_sent(c, d, o["id"], sent_day)
                refresh()
        if o["status"] == "sent":
            st.caption(f"Odoslané {o['sent_at']} · follow-up: {o['follow_up_due'] or 'už nie'}")
            if o["follow_up_due"] and o["follow_up_due"] <= date.today().isoformat():
                fs, fb = follow_up_text(o)
                st.code(f"{fs}\n\n{fb}", language=None, wrap_lines=True)
                if st.button("Follow-up odoslaný", key=key + "fu"):
                    mark_follow_up_sent(c, d, o["id"])
                    refresh()
            new_status = st.segmented_control("Výsledok", ["replied", "scheduled", "interviewed", "no_reply", "declined",
                                                           "bounced", "objected"], key=key + "res",
                                              format_func=lambda s: {"replied": "odpovedal", "scheduled": "dohodnuté",
                                                                     "interviewed": "rozhovor", "no_reply": "bez odpovede",
                                                                     "declined": "odmietol", "bounced": "nedoručené",
                                                                     "objected": "námietka (vymazať)"}[s])
            if new_status and st.button("Uložiť výsledok", key=key + "rs"):
                set_status(c, d, o["id"], new_status)
                refresh()
        if o["status"] in ("sent", "replied", "scheduled", "interviewed", "no_reply", "declined", "no_show"):
            past = d.outcomes_of_outreach(o["id"])
            if past:
                st.dataframe([{"kedy": r["recorded_at"][:10], "odpoveď": REPLIES.get(r["reply"], r["reply"]),
                               "problém": RELEVANCE.get(r["problem_relevance"] or "", "–"),
                               "ďalej": ", ".join(lbl for f, lbl in FLAGS.items() if r[f]) or "–",
                               "ďalší krok": r["next_action"] or ""} for r in past], hide_index=True,
                             alt="Zapísané výsledky tohto oslovenia")
            with st.form(key + "outcome", border=True):
                st.markdown("**Zapísať výsledok** (učenie z odpovede)")
                cols = st.columns(2)
                reply = cols[0].selectbox("Odpoveď", list(REPLIES), format_func=REPLIES.get, key=key + "rep")
                relevance = cols[1].selectbox("Problém", ["", *RELEVANCE], key=key + "rel",
                                              format_func=lambda v: RELEVANCE.get(v, "nevieme"))
                flag_cols = st.columns(len(FLAGS))
                flags = {f: flag_cols[i].checkbox(lbl, key=key + f) for i, (f, lbl) in enumerate(FLAGS.items())}
                rejection = st.text_input("Dôvod odmietnutia (ak odmietol)", key=key + "rr")
                next_step = st.text_input("Ďalší krok", key=key + "nx")
                note = st.text_area("Poznámka (čo potvrdil alebo vyvrátil – bez citlivých údajov)", key=key + "nt", height=80)
                cols = st.columns(2)
                prep = cols[0].number_input("Príprava na kontakt (min)", min_value=0.0, value=None, key=key + "pm")
                delivery = cols[1].number_input("Dodanie výstupu (dni)", min_value=0.0, value=None, key=key + "dd")
                if st.form_submit_button("Uložiť výsledok oslovenia", icon=":material/fact_check:"):
                    try:
                        res = record_outcome(c, d, outreach_id=o["id"], reply=reply, problem_relevance=relevance or None,
                                             rejection_reason=rejection, next_action=next_step, note=note,
                                             prep_minutes=prep, delivery_days=delivery, **flags)
                        msg = "Výsledok uložený"
                        if res.get("prehľad_domény"):
                            msg += " · prehľad domény sa aktualizuje pri najbližšom kroku value"
                        st.toast(msg, icon=":material/check:")
                        refresh()
                    except OutcomeError as exc:
                        st.error(str(exc))
