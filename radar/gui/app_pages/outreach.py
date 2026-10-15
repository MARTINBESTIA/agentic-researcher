from datetime import date, datetime, timezone

import streamlit as st

from radar.gui.common import cfg, db, esc, json_or, pick_hypothesis, refresh
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
STATUS = ["draft", "needs_edit", "approved", "sent", "replied", "scheduled", "interviewed", "no_reply", "declined",
          "bounced", "no_show", "blocked"]
shown = st.pills("Stav", STATUS, selection_mode="multi", default=["draft", "needs_edit", "approved", "sent", "blocked"], key="out_st")
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
