import streamlit as st

from radar.gui.common import cfg, db, esc, json_or, pick_hypothesis, refresh, safe_url
from radar.people import add_manual_profile
from radar.privacy import forget
from radar.shortlist import contact_shortlist

d, c = db(), cfg()
if not c.privacy.lia_completed:
    st.warning("Hľadanie ľudí je zablokované, kým nepotvrdíš LIA (privacy.lia_completed).", icon=":material/lock:")

LINK = {"quoted_about_problem": "verejne o probléme", "role_owner": "vlastník procesu", "org_affected": "vedenie dotknutej org.",
        "procurement_contact": "kontakt v obstarávaní", "expert": "expert", "data_holder": "drží dáta"}
RELATION = {"mentioned_with_problem": "v dôkaze", "quoted_person": "citovaná osoba", "procured_related": "obstarávanie",
            "segment_match": "segment", "manual": "ručne", "contact_search": "hľadanie podľa hypotézy",
            "data_holder": "drží dáta", "expert_org": "expertná org."}
KIND = {"email": "e-mail", "phone": "telefón", "profile_url": "LinkedIn (over ručne, nesťahuje sa)",
        "hidden_contact": "skrytý e-mail – otvor stránku ručne", "contact_form": "formulár"}
SOURCE = {"page_link": "odkaz na webe organizácie", "search_result": "výsledok vyhľadávania", "manual": "zadané ručne",
          "catalog_contact_point": "kontaktný bod datasetu (Národný katalóg)", "manual_page": "stránka zadaná ručne"}


def person_rows(hids: list[int]) -> list[dict]:
    """Jeden riadok na osobu a problém: firma, väzba, značky (overiteľné fakty) a kontakty so zdrojom."""
    orgs: dict = {}
    rows = []
    for hid in hids:
        org_links = {o["id"]: o for o in d.orgs_for_hypothesis(hid)}
        advice = {r["person_id"]: r for r in contact_shortlist(c, d, hid) if r["person_id"]}
        # Aj ľudia bez priority (poradie pre hypotézu ešte nebežalo): inak by zozbieraní ľudia v GUI chýbali
        # (akcia #20: 39 osôb bez väzby, kým nebeží people-rank s mapou rolí ~10 min na hypotézu).
        pending = [{"person_id": p["id"], "org_id": p["org_id"], "priority": None, "priority_json": None,
                    "link_type": "pending", "titles": p["titles"], "full_name": p["full_name"],
                    "role_title": p["role_title"], "org_name": p["org_name"], "is_expert": p["is_expert"],
                    "person_source_url": p["source_url"], "person_status": p["status"]}
                   for p in d.unranked_people(hid)]
        for l in list(d.links_for_hypothesis(hid)) + pending:
            if l["org_id"] not in orgs and l["org_id"] is not None:
                orgs[l["org_id"]] = d.get_organization(l["org_id"])
            org, org_link = orgs.get(l["org_id"]), org_links.get(l["org_id"])
            comp = json_or(l["priority_json"], {})
            contacts = d.contacts_for_person(l["person_id"])
            emails = [ct["value"] for ct in contacts if ct["kind"] == "email"]
            phones = [ct["value"] for ct in contacts if ct["kind"] == "phone"]
            profiles = [ct["value"] for ct in contacts if ct["kind"] == "profile_url"]
            generic = [ct["value"] for ct in d.org_contacts(l["org_id"]) if ct["kind"] == "email"] if l["org_id"] else []
            marks = []
            if l["priority"] is None:
                marks.append("čaká na poradie")
            if l["link_type"] == "quoted_about_problem":
                marks.append("citovaný v dôkaze")
            if (comp.get("role_fit") or 0) >= 0.5:
                marks.append("rola sedí")
            if org is not None and org["website_verified"]:
                marks.append("web overený")
            if org is not None and org["ico"]:
                marks.append("IČO v RPO")
            if org_link is not None and org_link["relation"] in RELATION:
                marks.append("firma: " + RELATION[org_link["relation"]])
            if emails or phones:
                marks.append("zverejnený kontakt")
            if profiles:
                marks.append("LinkedIn")
            if l["is_expert"]:
                marks.append("expert")
            if org_link is not None and org_link["procurement_active"]:
                marks.append("⚠ obstarávanie")
            adv = advice.get(l["person_id"]) or {}
            rows.append({
                "problém": f"H{hid}", "odporúčanie": adv.get("odporúčanie", ""), "typ": adv.get("typ", ""),
                "osoba": f"{l['titles'] + ' ' if l['titles'] else ''}{l['full_name']}",
                "funkcia": l["role_title"] or "", "organizácia": l["org_name"] or "", "IČO": (org["ico"] if org else "") or "",
                "väzba": "ešte nezoradený" if l["link_type"] == "pending" else LINK.get(l["link_type"], l["link_type"]),
                "priorita": l["priority"], "značky": ", ".join(marks),
                "e-mail": "; ".join(emails) or (f"org.: {generic[0]}" if generic else ""), "telefón": "; ".join(phones),
                "LinkedIn": safe_url(profiles[0]) if profiles else None, "zdroj": safe_url(l["person_source_url"]),
                "čo žiadať": adv.get("čo_žiadať", ""), "stav": l["person_status"], "id": l["person_id"],
            })
    rows.sort(key=lambda r: -(r["priorita"] or 0))
    return rows


tab_all, tab_one = st.tabs(["Všetky problémy", "Detail problému"])

with tab_all:
    hyps = {h["id"]: h["title"] for h in d.people_enabled_hypotheses()}
    if not hyps:
        st.info("Žiadna hypotéza nie je vybraná na hľadanie ľudí. Na stránke Problémy klikni pri hypotéze "
                "„Hľadať firmy a ľudí k tomuto problému“.", icon=":material/info:")
    else:
        chosen = st.multiselect("Problémy", list(hyps), default=list(hyps), format_func=lambda i: f"H{i}: {hyps[i][:80]}",
                                key="ppl_all_h")
        rows = person_rows(chosen)
        query = st.text_input("Hľadať", key="ppl_all_q", placeholder="meno, firma, funkcia, značka…")
        if query:
            rows = [r for r in rows if query.lower() in " ".join(str(v) for v in r.values()).lower()]
        n_li = sum(1 for r in rows if r["LinkedIn"])
        n_wait = sum(1 for r in rows if r["priorita"] is None)
        st.caption(f"{len(rows)} osôb · {len({r['organizácia'] for r in rows})} organizácií · LinkedIn {n_li} · "
                   f"čaká na poradie {n_wait} (zoradí ich krok people-rank; nevybraní sa potom vymažú – minimalizácia). "
                   "Značky sú overiteľné fakty (zdroj osoby, web a IČO firmy, zverejnený kontakt), nie hodnotenie človeka. "
                   "Priorita určuje iba poradie kontroly: 0,35·role_fit + 0,25·väzba + 0,20·kanál + 0,20·zhoda_org.")
        st.dataframe(
            rows, hide_index=True, height=520, alt="Ľudia a firmy ku všetkým vybraným problémom so značkami a kontaktmi",
            column_config={
                "priorita": st.column_config.ProgressColumn("Priorita", min_value=0, max_value=1, format="%.2f"),
                "LinkedIn": st.column_config.LinkColumn("LinkedIn (over ručne)", display_text="profil"),
                "zdroj": st.column_config.LinkColumn("Zdroj osoby", display_text="zdroj"),
                "id": st.column_config.NumberColumn("ID", format="%d"),
            },
        )

with tab_one:
    hid = pick_hypothesis(d, "ppl_h")
    if hid is None:
        st.stop()
    role_map = d.get_role_map(hid)
    with st.expander("Mapa rolí (kto vlastní proces s problémom)", expanded=False):
        if role_map is None:
            st.caption("Mapu rolí vytvorí presný model pri kroku people-rank.")
        else:
            if role_map["low_agreement"]:
                st.warning("Vzorky modelu sa nezhodli – skontroluj mapu rolí.", icon=":material/warning:")
            roles = [{"kategória": r["category"], "príklady funkcií": ", ".join(r.get("examples", [])),
                      "prečo": r.get("rationale", "")} for r in role_map["roles"]]
            edited = st.data_editor(roles, num_rows="dynamic", hide_index=True, key="role_editor",
                                    alt="Kategórie rolí, ktoré vlastnia proces s problémom")
            if st.button("Uložiť mapu rolí", key="role_save"):
                new = [{"category": r["kategória"], "examples": [x.strip() for x in (r["príklady funkcií"] or "").split(",") if x.strip()],
                        "rationale": r["prečo"] or ""} for r in edited if r.get("kategória")]
                d.set_role_map(hid, new, edited_by_human=True)
                refresh()

    # Jeden riadok na osobu: hlavná väzba (s prioritou); ďalšie väzby sú v detaile osoby.
    links = [l for l in d.links_for_hypothesis(hid) if l["priority"] is not None]
    rows = []
    for l in links:
        comp = json_or(l["priority_json"], {})
        contacts = d.contacts_for_person(l["person_id"])
        profile = next((ct["value"] for ct in contacts if ct["kind"] == "profile_url"), None)
        rows.append({
            "id": l["person_id"], "osoba": f"{l['titles'] + ' ' if l['titles'] else ''}{l['full_name']}", "funkcia": l["role_title"] or "",
            "organizácia": l["org_name"] or "", "väzba": LINK.get(l["link_type"], l["link_type"]),
            "priorita": l["priority"], "role_fit": comp.get("role_fit"), "väzba_sila": comp.get("väzba"),
            "kanál": comp.get("kanál_popis", ""), "zhoda_org": comp.get("zhoda_org"), "expert": bool(l["is_expert"]),
            "kontakty": len(contacts), "LinkedIn": safe_url(profile), "stav": l["person_status"],
        })
    st.caption("Priorita je kľúč na zoradenie, nie hodnotenie človeka: 0,35·role_fit + 0,25·väzba + 0,20·kanál + 0,20·zhoda_org "
               "(zložky v stĺpcoch). Experti sa do metrík Brány 0 nepočítajú.")
    st.dataframe(rows, hide_index=True, alt="Ľudia k hypotéze s väzbou a zložkami priority", height=380,
                 column_config={"priorita": st.column_config.ProgressColumn("Priorita", min_value=0, max_value=1, format="%.2f"),
                                "LinkedIn": st.column_config.LinkColumn("LinkedIn", display_text="profil")})
    if not rows:
        st.stop()

    pid = st.selectbox("Detail osoby", [r["id"] for r in rows],
                       format_func=lambda i: next(r["osoba"] + " – " + r["organizácia"] for r in rows if r["id"] == i), key="ppl_detail")
    person = d.get_person(pid)
    with st.container(border=True):
        st.markdown(f"**{esc(person['full_name'])}** · {esc(person['role_title'] or 'funkcia neuvedená')} · {esc(person['org_name'] or '')}")
        st.caption(f"Zdroj osoby: {person['source_kind']} · oslovenie z textu zdroja: {person['gender_hint'] or 'neznáme (Dobrý deň,)'}")
        st.html(f"<blockquote>{esc(person['source_snippet'])}</blockquote>")
        url = safe_url(person["source_url"])
        if url:
            st.link_button("Zdroj osoby", url, icon=":material/open_in_new:")
        st.markdown("**Kontakty (iba zverejnené, so zdrojom a úryvkom):**")
        for ct in d.contacts_for_person(pid):
            label = KIND.get(ct["kind"], ct["kind"])
            cols = st.columns([5, 1])
            with cols[0]:
                if ct["kind"] == "profile_url" and safe_url(ct["value"]):
                    st.html(f"<div>• {esc(label)}: <a href='{esc(safe_url(ct['value']))}' target='_blank' rel='noopener'>"
                            f"{esc(ct['value'])}</a> · {esc(SOURCE.get(ct['extraction'], ct['extraction']))}</div>")
                else:
                    st.markdown(f"- {label}: `{esc(ct['value'])}`")
                st.html(f"<small>Úryvok: {esc(ct['source_snippet'])}</small>")
                src = safe_url(ct["source_url"])
                if src:
                    st.caption(src)
            if ct["kind"] == "profile_url":
                with cols[1]:
                    if st.button("Odstrániť", key=f"ct_del_{ct['id']}", icon=":material/delete:",
                                 help="Nesprávny profil (napr. iná osoba s rovnakým menom)."):
                        d.delete_contact_point(ct["id"])
                        refresh()
        with st.form(f"li_{pid}", border=False):
            li = st.text_input("Pridať LinkedIn ručne", placeholder="https://www.linkedin.com/in/…",
                               help="Uloží sa iba URL profilu (kanonická, bez parametrov). Stránka sa nesťahuje.")
            if st.form_submit_button("Uložiť LinkedIn", icon=":material/link:") and li.strip():
                try:
                    add_manual_profile(c, d, pid, li)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    refresh()
        for l in d.links_for_person(pid):
            if l["hypothesis_id"] == hid:
                st.caption(f"Väzba: {LINK.get(l['link_type'], l['link_type'])} – {l['rationale'] or ''}"
                           + (f" (dôkaz C{l['evidence_claim_id']})" if l["evidence_claim_id"] else ""))
        with st.container(horizontal=True):
            if st.button("Chcem osloviť", icon=":material/check:", key="ppl_ok"):
                d.update_person(pid, status="approved")
                refresh()
            if st.button("Vylúčiť", icon=":material/block:", key="ppl_ex"):
                d.update_person(pid, status="excluded")
                refresh()
            confirm = st.checkbox("Potvrdzujem výmaz", key="ppl_forget_ok")
            if st.button("Vymazať a nezbierať znova (forget)", icon=":material/delete:", disabled=not confirm, key="ppl_forget"):
                forget(c, d, person_id=pid, reason="manual")
                refresh()
