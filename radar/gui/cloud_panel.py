"""Cloudové modely (Azure OpenAI) v Správcovi akcií: profily, nasadenia, smerovanie rolí, rozpočet, test pripojenia.

Kľúč sa nikdy nezadáva do GUI – iba názov premennej prostredia; GUI ukáže len, či je premenná nastavená. Nasadenia sa
iba čítajú (Azure Resource Manager) alebo pridajú ručne; nič sa v Azure nevytvára ani nemení.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from radar.cloud import (
    DeploymentListError, add_manual_deployment, cloud_routes, cloud_settings, deployment_label, deployments,
    make_cloud_client, record_capability, refresh_deployments, save_cloud_settings, save_profile, secret_present,
    set_route, spend_summary, test_connection,
)
from radar.config import RoleConfig
from radar.eta import local
from radar.models import CLOUD_PAUSE_KEY, MODEL_SETTINGS_KEY, ModelRouter

USAGE_STATUS = {"reserved": "rezervované", "settled": "vyrovnané", "failed": "zlyhalo (bez nákladov)",
                "uncertain": "neisté (timeout, rezervácia ostáva)", "cached": "z cache"}
CAPS = {"text": "text", "json_schema": "JSON schéma"}


def _options(models, d) -> list[tuple[str, str, bool]]:
    opts: list[tuple[str, str, bool]] = [("local", "", False)]
    for name in models.providers:
        for dep in deployments(d, name):
            opts.append((name, dep["deployment"], bool(dep.get("reasoning"))))
    return opts


def _dep_info(d, provider: str, deployment: str) -> dict[str, Any]:
    return next((x for x in deployments(d, provider) if x["deployment"] == deployment), {"deployment": deployment})


def cloud_panel(c, d) -> None:
    if c.models is None:
        st.caption("Cloudové modely vyžadujú sekciu models v config.yaml (smerovanie úloh na roly).")
        return
    router = ModelRouter(c, d)
    models, cloud = router.models, router.cloud
    st.caption("Do cloudu idú iba úlohy z `cloud.allowed_tasks` (verejné zdroje: prehľad domény, návrh prínosu, väzby "
               "polí, posúdenie dôkazov, karty, závery experimentov, kritika). Úlohy s menami a kontaktmi (návrhy "
               "oslovení, ich kontrola, kontakty) iba s `privacy.cloud_personal_data: true` – inak čakajú. Kľúč ostáva "
               "v premennej prostredia; do databázy, configu ani logov sa nezapisuje.")
    st.caption("Azure for Students: kredit a podmienky sú určené na štúdium – komerčné použitie (napr. pilot so "
               "zákazníkom) si over v podmienkach predplatného, nepredpokladaj ho.")

    pause = d.get_setting(CLOUD_PAUSE_KEY, None)
    if pause:
        st.warning(f"Posledné prerušenie cloudu ({local(pause['kedy'])}): {pause['dôvod']} · úloha {pause['úloha']} · "
                   f"nasadenie {pause.get('nasadenie') or '–'}. Položka čaká na ďalší beh, nič sa neschválilo "
                   f"automaticky. {pause.get('správa', '')}", icon=":material/cloud_off:")

    # ---------------------------------------------------------------- rozpočet a spotreba
    spend = spend_summary(d, cloud)
    with st.container(horizontal=True):
        st.metric("Tvoj rozpočet", f"{cloud.budget_total_eur:.2f} €", border=True)
        st.metric("Odhad spotreby", f"{spend['celkom']['settled']:.4f} €", f"dnes {spend['dnes']['settled']:.4f} €",
                  delta_color="off", border=True)
        st.metric("Otvorené rezervácie", f"{spend['celkom']['open']:.4f} €", border=True)
        st.metric("Zostáva v rozpočte (odhad)", f"{spend['zostáva_v_rozpočte']:.4f} €", border=True)
    st.caption("Spotreba je vlastný odhad z tokenov a sadzieb, ktoré si zadal – nie overený zostatok ani faktúra Azure. "
               "Skutočný zostatok kreditu over v Azure portáli (Cost Management). Neisté volania (timeout) ostávajú "
               "započítané v plnej rezervácii.")

    # ---------------------------------------------------------------- profily
    providers = models.providers
    st.markdown("**Profily poskytovateľa**")
    if providers:
        st.dataframe([{"profil": n, "endpoint": p.endpoint, "premenná s kľúčom": p.api_key_env,
                       "kľúč nastavený": secret_present(p.api_key_env),
                       "resource": "/".join(x for x in (p.resource_group, p.account_name) if x) or "–",
                       "token na čítanie nasadení": secret_present(p.mgmt_token_env)} for n, p in providers.items()],
                     hide_index=True, alt="Profily cloudových poskytovateľov bez tajomstiev")
    else:
        st.caption("Zatiaľ žiadny profil. Lokálne modely fungujú ako doteraz.")
    with st.form("cloud_profile_form", border=False):
        cols = st.columns(2)
        name = cols[0].text_input("Názov profilu", value=next(iter(providers), "azure"))
        endpoint = cols[1].text_input("Endpoint", placeholder="https://<resource>.openai.azure.com/openai/v1/")
        key_env = cols[0].text_input("Premenná prostredia s kľúčom (nie kľúč)", value="AZURE_OPENAI_API_KEY")
        token_env = cols[1].text_input("Premenná s tokenom na čítanie nasadení (voliteľné)", value="AZURE_MGMT_TOKEN")
        sub = cols[0].text_input("Subscription ID (voliteľné)")
        rg = cols[1].text_input("Resource group (voliteľné)")
        acc = cols[0].text_input("Názov Azure OpenAI resource (voliteľné)")
        if st.form_submit_button("Uložiť profil", icon=":material/save:"):
            try:
                save_profile(d, name, {"endpoint": endpoint.strip(), "api_key_env": key_env.strip(),
                                       "mgmt_token_env": token_env.strip(), "subscription_id": sub.strip(),
                                       "resource_group": rg.strip(), "account_name": acc.strip()})
                st.toast("Profil uložený (bez kľúča).", icon=":material/check:")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if not providers:
        return

    # ---------------------------------------------------------------- nasadenia
    st.markdown("**Nasadenia (deployments)**")
    prof_name = st.selectbox("Profil", list(providers), key="cloud_prof")
    profile = providers[prof_name]
    deps = deployments(d, prof_name)
    if deps:
        st.dataframe([{"nasadenie": x["deployment"], "model a verzia": deployment_label(x), "sku": x.get("sku") or "",
                       "stav": x.get("stav") or "", "zdroj": x.get("zdroj") or "",
                       "reasoning": bool(x.get("reasoning")),
                       "overené": ", ".join(f"{CAPS.get(k, k)} {str(v)[:10]}" for k, v in (x.get("overené") or {}).items())
                       or "nie"} for x in deps], hide_index=True, alt="Nasadenia Azure OpenAI")
    else:
        st.caption("Žiadne nasadenie. Načítaj existujúce alebo pridaj ručne.")
    with st.container(horizontal=True):
        if st.button("Obnoviť nasadenia", icon=":material/sync:", key="cloud_refresh",
                     help="Iba čítanie zoznamu existujúcich nasadení cez Azure Resource Manager; nič nevytvára."):
            try:
                found = refresh_deployments(d, prof_name, profile)
                st.toast(f"Načítané nasadenia: {len(found)}", icon=":material/sync:")
                st.rerun()
            except DeploymentListError as exc:
                st.warning(str(exc), icon=":material/lock:")
    with st.form("cloud_manual_dep", border=False):
        cols = st.columns(4)
        dep_name = cols[0].text_input("Názov nasadenia")
        dep_model = cols[1].text_input("Model (voliteľné)")
        dep_version = cols[2].text_input("Verzia (voliteľné)")
        dep_reason = cols[3].checkbox("Reasoning model")
        if st.form_submit_button("Pridať existujúce nasadenie ručne", icon=":material/add:"):
            try:
                add_manual_deployment(d, prof_name, dep_name, dep_model, dep_version, dep_reason)
                st.toast("Nasadenie pridané – kompatibilita zatiaľ neoverená.", icon=":material/check:")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    # ---------------------------------------------------------------- test pripojenia
    if deps:
        st.markdown("**Otestovať pripojenie**")
        test_dep = st.selectbox("Nasadenie na test", [x["deployment"] for x in deps], key="cloud_test_dep",
                                format_func=lambda n: deployment_label(_dep_info(d, prof_name, n)))
        st.caption("Test pošle jednu krátku požiadavku (desiatky tokenov), započíta sa do rozpočtu a vyžaduje sadzbu "
                   "nasadenia. Text a JSON schéma sa overujú zvlášť; neúspech sa ako overený nezapíše.")
        with st.container(horizontal=True):
            for cap in ("text", "json_schema"):
                if st.button(f"Otestovať: {CAPS[cap]}", key=f"cloud_test_{cap}", icon=":material/network_check:"):
                    info = _dep_info(d, prof_name, test_dep)
                    role_cfg = RoleConfig(model=test_dep, provider=prof_name, deployment=test_dep,
                                          reasoning=bool(info.get("reasoning")), max_tokens=64, timeout_s=60)
                    client = make_cloud_client(models, cloud, d, role_cfg, 0.0, router.budget)
                    result = test_connection(client, cap)
                    record_capability(d, prof_name, test_dep, result)
                    (st.success if result["ok"] else st.error)(
                        f"{CAPS[cap]}: " + ("v poriadku" if result["ok"] else result.get("chyba", "neúspech"))
                        + (f" · {result['latencia_s']} s · tokeny {result['tokeny']}" if result.get("latencia_s") else ""))

    # ---------------------------------------------------------------- smerovanie rolí
    st.markdown("**Ktorá rola beží kde**")
    opts = _options(models, d)
    gui_routes = (d.get_setting(MODEL_SETTINGS_KEY, {}) or {}).get("routes") or {}
    with st.form("cloud_routes_form", border=False):
        picks = {}
        for role, role_cfg in models.roles.items():
            if role == "embed":
                continue
            current = (role_cfg.provider, role_cfg.deployment, role_cfg.reasoning) if role_cfg.provider != "local" \
                else ("local", "", False)
            options = opts if current in opts else opts + [current]
            picks[role] = st.selectbox(
                role, options, index=options.index(current), key=f"cloud_route_{role}",
                format_func=lambda o, r=role_cfg: f"lokálny model ({r.model})" if o[0] == "local"
                else f"{o[0]}: {deployment_label(_dep_info(d, o[0], o[1]))}")
        if st.form_submit_button("Uložiť smerovanie rolí", icon=":material/alt_route:"):
            for role, (provider, dep, reasoning) in picks.items():
                if provider == "local" and role not in gui_routes:
                    continue
                set_route(d, role, provider, dep, reasoning)
            st.toast("Smerovanie uložené – platí pre ďalšie spustené akcie.", icon=":material/check:")
            st.rerun()

    routes = cloud_routes(router)
    if routes:
        st.dataframe(routes, hide_index=True, alt="Úlohy smerované do cloudu a či ich podklady smú opustiť počítač")

    # ---------------------------------------------------------------- rozpočet a sadzby
    st.markdown("**Rozpočet a sadzby**")
    saved = cloud_settings(d)
    with st.form("cloud_budget_form", border=False):
        cols = st.columns(4)
        total = cols[0].number_input("Rozpočet celkom (€)", 0.0, 10000.0, float(cloud.budget_total_eur), 0.01,
                                     help="0 = cloudové volania vypnuté. Napr. zostatok kreditu, ktorý chceš minúť.")
        since = cols[1].text_input("Počítať od (YYYY-MM-DD)", value=cloud.budget_since)
        daily = cols[2].number_input("Denný limit (€)", 0.0, 1000.0, float(cloud.budget_daily_eur), 0.01)
        per_run = cols[3].number_input("Limit na beh (€)", 0.0, 1000.0, float(cloud.budget_run_eur), 0.01)
        all_deps = sorted({x["deployment"] for n in providers for x in deployments(d, n)} | set(cloud.rates))
        rate_rows = [{"nasadenie": dep, "vstup €/1M": cloud.rates[dep].input_per_1m if dep in cloud.rates else None,
                      "výstup €/1M": cloud.rates[dep].output_per_1m if dep in cloud.rates else None,
                      "mena": cloud.rates[dep].currency if dep in cloud.rates else "EUR",
                      "overené dňa": cloud.rates[dep].verified_at if dep in cloud.rates else ""} for dep in all_deps]
        edited = st.data_editor(rate_rows, hide_index=True, disabled=["nasadenie"], key="cloud_rates",
                                column_config={"vstup €/1M": st.column_config.NumberColumn(min_value=0.0, format="%.4f"),
                                               "výstup €/1M": st.column_config.NumberColumn(min_value=0.0, format="%.4f")})
        st.caption("Sadzby zadaj z cenníka Azure pre konkrétne nasadenie (model, verzia, región). Chýbajúca sadzba sa "
                   "nenahrádza nulou – bez nej sa nasadenie nevolá.")
        if st.form_submit_button("Uložiť rozpočet a sadzby", icon=":material/savings:"):
            rates = dict(saved.get("rates") or {})
            for row in (edited if isinstance(edited, list) else edited.to_dict("records")):
                inp, out = row.get("vstup €/1M"), row.get("výstup €/1M")
                if inp is None or out is None or inp != inp or out != out or (inp <= 0 and out <= 0):
                    rates.pop(row["nasadenie"], None)
                    continue
                rates[row["nasadenie"]] = {"input_per_1m": float(inp), "output_per_1m": float(out),
                                           "currency": row.get("mena") or "EUR", "verified_at": row.get("overené dňa") or ""}
            save_cloud_settings(d, budget_total_eur=float(total), budget_since=since.strip(), budget_daily_eur=float(daily),
                                budget_run_eur=float(per_run), rates=rates)
            st.toast("Rozpočet a sadzby uložené.", icon=":material/check:")
            st.rerun()

    usage = d.cloud_usage(20)
    if usage:
        st.dataframe([{"čas": local(u["created_at"]), "nasadenie": u["deployment"], "úloha": u["task"],
                       "stav": USAGE_STATUS.get(u["status"], u["status"]), "rezervované €": round(u["reserved_eur"], 5),
                       "odhad ceny €": None if u["cost_eur"] is None else round(u["cost_eur"], 5),
                       "tokeny vstup/výstup": f"{u['prompt_tokens'] or 0}/{u['completion_tokens'] or 0}",
                       "pozn.": u["note"] or ""} for u in usage], hide_index=True,
                     alt="Posledné cloudové volania: rezervácia, odhad ceny, tokeny")
