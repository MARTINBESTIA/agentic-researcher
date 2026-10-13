import json
from pathlib import Path

import streamlit as st

from radar.cards import FIELD_LABELS, OUTPUT_LABELS
from radar.datasets import DatasetError
from radar.exp_plan import AUTO_LOCK_KEY, TASK_LABELS, plan_quality, success_rule, validate_plan
from radar.gate1 import decide
from radar.gate2 import complete_periods, data_limits, lock, series
from radar.gate2_trace import as_markdown, explain
from radar.gui.common import cfg, db, esc, jobs_panel, json_or, refresh, start_job_button

d, c = db(), cfg()
st.caption("Brána 2 – experimenty: z problémovej karty (kto má problém a aké rozhodnutie robí) navrhne model experimenty "
           "na dátach z Brány 1, kód ich skúšobne spustí, ty ich uzamkneš (predregistrácia), spätný test počíta Python "
           "proti všetkým naivným odhadom a záver hovorí, čo výsledok znamená pre rozhodnutie. Rozhoduješ ty.")
hyps = d.gate2_hypotheses()
if not hyps:
    st.info("Zatiaľ žiadna hypotéza neprešla Bránou 1. Prejde automaticky pri pripravenosti dát ≥ 0,5 alebo keď tvoje "
            "dáta (Brána 1 → Tvoje dáta) dostanú vhodnosť ≥ 0,5; inak rozhodneš na stránke Brána 1.", icon=":material/info:")
    st.stop()


def hyp_label(h) -> str:
    tag = f" · ↳ z H{h['parent_id']}" if h["parent_id"] else (" · rozdelená" if h["split_at"] else "")
    return f"H{h['id']}: {h['title'][:80]}{tag}"


labels = {h["id"]: hyp_label(h) for h in hyps}
hid = st.selectbox("Hypotéza", list(labels), format_func=labels.get, key="g2_h")
h = d.get_hypothesis(hid)
with st.container(horizontal=True):
    start_job_button("gate2", "Karta, návrh a test experimentov", 60, [hid], key=f"g2_run_{hid}", icon=":material/science:",
                     help="Karta (ak chýba) → model navrhne experimenty → test uzamknutých → záver.")
jobs_panel(show_last=False)
with st.expander("Nastavenie: automatické uzamknutie a kvalita návrhov modelu", icon=":material/tune:"):
    auto = d.get_setting(AUTO_LOCK_KEY, {}) or {}
    on = st.toggle("Uzamknúť návrhy bez chýb a upozornení hneď po návrhu (aj v autopilotovi)", value=bool(auto.get("enabled")),
                   key="g2_autolock", help="Predregistrácia ostáva pred testom (výsledok ju nemôže ovplyvniť), ale bez "
                                           "tvojej kontroly plánu. Návrhy s upozornením (napr. prah bez citátu) čakajú na teba.")
    if on != bool(auto.get("enabled")):
        from radar.util import utcnow

        d.set_setting(AUTO_LOCK_KEY, {"enabled": on, "since": utcnow(), "by": "GUI"})
        refresh()
    q = plan_quality(d)
    if q.get("návrhy"):
        st.caption(f"Návrhy modelu spolu: {q['návrhy']} · bez chýb na prvý pokus {q.get('bez_chýb_na_prvý_pokus', 0)} · "
                   f"až po oprave {q.get('bez_chýb_po_oprave', 0)} · s chybami {q.get('s_chybami', 0)} · uzamknuté "
                   f"{q.get('uzamknuté', 0)} (bez úprav {q.get('uzamknuté_bez_úprav', 0)}, automaticky "
                   f"{q.get('uzamknuté_automaticky', 0)})")

# ======================================================================= problémová karta
VERDICT = {"supported": "✅ podložené", "partial": "◐ čiastočne", "unsupported": "✗ nepodložené", "undecided": "? neurčené"}
card_row = d.latest_card(hid)
with st.container(border=True):
    st.markdown("**Problémová karta** – komu a pri akom rozhodnutí má experiment pomôcť")
    if h["split_at"]:
        kids = d.children_of(hid)
        st.info("Hypotéza bola široká, rozdelila sa na: " + ", ".join(f"H{k['id']} {k['title'][:60]}" for k in kids)
                + ". Experimenty majú podhypotézy (vyber ich hore).", icon=":material/call_split:")
    if h["parent_id"]:
        st.caption(f"Podhypotéza H{h['parent_id']}: zdedila dáta Brány 1, citáty o tomto podprobléme sa presunuli sem.")
    if card_row is None:
        st.caption("Karta zatiaľ nie je. Vznikne v akcii „Karta, návrh a test experimentov“ alebo „Problémové karty a "
                   "rozdelenie hypotéz“ (27B, ~3–5 min).")
    else:
        card = json_or(card_row["card_json"], {})
        claims = {cl["id"]: cl for cl in d.evidence_for(hid)}
        st.caption(f"Stav: {'potvrdená' if card_row['status'] == 'confirmed' else 'návrh – skontroluj a potvrď'} · "
                   f"vytvorená {card_row['created_at'][:16]} · model {card_row['model'] or '?'}"
                   + (" · upravená človekom" if card.get("upravené_človekom") else ""))
        checks = card.get("kontrola") or {}
        st.dataframe([{"pole": label, "text": (card.get(k) or {}).get("text", ""),
                       "citáty": ", ".join(f"C{i}" for i in (card.get(k) or {}).get("claims", [])),
                       "kontrola sudcom": VERDICT.get((checks.get(k) or {}).get("verdict"), "")}
                      for k, label in FIELD_LABELS.items()], hide_index=True, alt="Problémová karta")
        missing = card.get("chýbajúce_dáta_pre_experiment") or {}
        if missing:
            st.warning("Model: s dostupnými dátami sa toto rozhodnutie otestovať nedá. Treba: " + missing.get("text", "")
                       + " – pridaj dataset (Brána 1 → Datasety → Pridať dataset z odkazu, alebo Tvoje dáta); návrh sa "
                       "zopakuje, keď pribudne.", icon=":material/database_off:")
        st.markdown(f"**Výstup, ktorý pomôže:** {esc(OUTPUT_LABELS.get(card.get('helpful_output'), card.get('helpful_output', '')))}"
                    f" – {esc(card.get('helpful_output_text', ''))}  \n**Obchodná metrika:** {esc(card.get('business_metric', ''))}"
                    f"  \n**Čo by hypotézu vyvrátilo:** {esc(card.get('falsification', ''))}")
        cited = sorted({i for k in FIELD_LABELS for i in (card.get(k) or {}).get("claims", []) if i in claims})
        if cited:
            with st.expander(f"Citované citáty ({len(cited)})"):
                for i in cited:
                    st.html(f"<p><b>C{i}</b> {esc(claims[i]['quote'])} <small>– {esc(claims[i]['domain'] or '')}</small></p>")
        with st.expander("Upraviť kartu"):
            with st.form(f"g2_card_{card_row['id']}"):
                edits = {k: st.text_area(label, (card.get(k) or {}).get("text", ""), key=f"g2_cf_{k}_{card_row['id']}")
                         for k, label in FIELD_LABELS.items()}
                metric = st.text_input("Obchodná metrika", card.get("business_metric", ""))
                if st.form_submit_button("Uložiť úpravy karty", icon=":material/save:"):
                    for k, text in edits.items():
                        card[k] = {**(card.get(k) or {}), "text": text.strip()}
                    card["business_metric"] = metric.strip()
                    card["upravené_človekom"] = True
                    d.update_card(card_row["id"], card=card)
                    refresh()
        with st.container(horizontal=True):
            if card_row["status"] != "confirmed" and st.button("Potvrdiť kartu", icon=":material/verified:",
                                                               key=f"g2_card_ok_{card_row['id']}"):
                d.update_card(card_row["id"], status="confirmed")
                refresh()
            if st.button("Zamietnuť (vznikne nová)", icon=":material/refresh:", key=f"g2_card_no_{card_row['id']}"):
                d.update_card(card_row["id"], status="rejected")
                refresh()

# ======================================================================= experimenty
if not d.datasets_for(hid, only_ok=True):
    st.warning("Hypotéza prešla Bránou 1 bez stiahnutého datasetu (iba popis alebo schéma). Experimenty počkajú, kým "
               "pribudnú dáta: Brána 1 → Tvoje dáta (nahraj súbor) alebo Datasety → Pridať dataset z odkazu.",
               icon=":material/database_off:")
exps = d.experiments_for(hid)
if not exps:
    st.caption("Experimenty navrhne model z karty v akcii „Karta, návrh a test experimentov“.")
    st.stop()
STATUS = {"draft": "návrh (neuzamknutý)", "locked": "uzamknutý, čaká na test", "done": "otestovaný", "failed": "test zlyhal",
          "invalid": "neplatný návrh (chyby aj po oprave)"}
drafts = [e for e in exps if e["status"] == "draft"]
waiting = [e for e in exps if e["status"] == "locked"]
if drafts and not waiting and not any(e["result_json"] for e in exps):
    st.warning(f"Žiadny z {len(exps)} experimentov nie je uzamknutý, preto test ani záver nebežali. Vyber experiment → "
               "skontroluj „Čomu slúži“ a plán → „Uzamknúť predregistráciu“ → spusti akciu znova.", icon=":material/lock_open:")
elif waiting:
    st.info("Uzamknuté a čakajú na test: " + ", ".join("E" + str(e["id"]) for e in waiting) + " – spusti akciu (alebo "
            "autopilota).", icon=":material/science:")


def headline(e) -> tuple[float | None, bool | None]:
    res = json_or(e["result_json"], {})
    if e["task"]:
        prim = res.get("hlavné") or {}
        return prim.get("zlepšenie_pct"), prim.get("splnené")
    return (res["improvement"] * 100 if res.get("improvement") is not None else None), None


rows = []
for e in exps:
    flags = json_or(e["flags_json"], {})
    plan = json_or(e["spec_json"], {})
    imp, met = headline(e)
    rows.append({"E": e["id"], "úloha": TASK_LABELS.get(e["task"], "pôvodná predpoveď sumy"),
                 "názov": plan.get("title") or (e["org_name"] or "celý dataset"),
                 "čomu slúži": (e["problem_link"] or "")[:90], "stav": STATUS.get(e["status"], e["status"]),
                 "zlepšenie %": imp, "pravidlo": {True: "splnené", False: "nesplnené"}.get(met, ""),
                 "príznaky": ", ".join(k.replace("_", " ") for k, v in flags.items()
                                       if v and k not in ("obdobie_upravené", "upozornenia"))})
st.dataframe(rows, hide_index=True, alt="Experimenty hypotézy",
             column_config={"zlepšenie %": st.column_config.NumberColumn("zlepšenie vs. naivný (%)", format="%.1f")})
eid = st.selectbox("Experiment", [r["E"] for r in rows],
                   format_func=lambda i: next(f"E{i}: {r['úloha']} · {r['názov'][:60]} · {r['stav']}" for r in rows if r["E"] == i))
e = d.get_experiment(eid)
flags = json_or(e["flags_json"], {})
res = json_or(e["result_json"], {})


def show_lock() -> None:
    if st.button("Uzamknúť predregistráciu (potom sa už nemení)", icon=":material/lock:", key=f"g2_lock_{eid}"):
        try:
            lock(c, d, eid)
        except ValueError as exc:
            st.error(str(exc))
        else:
            refresh()


def show_task_experiment() -> None:
    plan = json_or(e["spec_json"], {})
    task = e["task"]
    card = json_or(d.get_card(e["card_id"])["card_json"], {}) if e["card_id"] and d.get_card(e["card_id"]) else {}
    with st.container(border=True):
        st.markdown(f"**Čomu slúži** – {esc(TASK_LABELS.get(task, task))}")
        st.markdown(f"{esc(e['problem_link'] or '')}")
        if card:
            st.caption(f"Rozhodnutie z karty: {(card.get('decision') or {}).get('text', '')} · kto: "
                       f"{(card.get('who') or {}).get('text', '')}")
        st.markdown(f"**Výrok:** {esc(e['statement'])}  \n**Pravidlo úspechu:** {esc(e['success_rule'])}")
    for err in flags.get("chyby_návrhu") or []:
        st.error(err, icon=":material/error:")
    for w in flags.get("upozornenia") or []:
        st.warning(w, icon=":material/warning:")
    with st.expander("Plán: príprava dát, štítok, príznaky, metriky", expanded=not e["locked_at"]):
        facts = [("tabuľka", plan.get("table")), ("dátum riadku", plan.get("time_column")),
                 ("entita", plan.get("entity_column") or "–"), ("štítok", plan.get("label") or "–"),
                 ("hodnota", plan.get("value") or "–"), ("príznaky", ", ".join(plan.get("features") or []) or "–"),
                 ("kategória", plan.get("category") or "–"), ("obdobie", plan.get("period")),
                 ("prahy", ", ".join(f"{t:g}" for t in plan.get("thresholds") or []) or "–"),
                 ("koľko skontroluje (k)", plan.get("capacity")), ("hlavná metrika", plan.get("primary_metric")),
                 ("hlavný naivný odhad", plan.get("primary_baseline")), ("prah úspechu %", plan.get("success_threshold"))]
        st.dataframe([{"pole": k, "hodnota": str(v)} for k, v in facts], hide_index=True, alt="Plán experimentu")
        if plan.get("steps"):
            st.markdown("Príprava dát (v poradí):")
            st.dataframe([{"#": i + 1, "operácia": s.get("op"), "nový stĺpec": s.get("name", ""),
                           "podrobnosti": {k: v for k, v in s.items() if k not in ("op", "name", "why") and v not in ("", [], 0)},
                           "prečo": s.get("why", "")} for i, s in enumerate(plan["steps"])], hide_index=True,
                         alt="Kroky prípravy dát")
        dry = plan.get("skúšobný_beh") or {}
        if dry:
            st.caption("Skúšobný beh na dátach: " + ", ".join(f"{k} {v}" for k, v in dry.items() if k != "protokol"))
            if dry.get("protokol"):
                st.dataframe([{k: v for k, v in p.items()} for p in dry["protokol"]], hide_index=True,
                             alt="Protokol skúšobného behu")
        if not e["locked_at"]:
            with st.form(f"g2_plan_{eid}"):
                raw = st.text_area("Plán (JSON) – úprava pred uzamknutím",
                                   json.dumps({k: v for k, v in plan.items() if k != "skúšobný_beh"}, ensure_ascii=False,
                                              indent=2), height=260)
                if st.form_submit_button("Uložiť a skúšobne spustiť", icon=":material/play_arrow:"):
                    try:
                        new = json.loads(raw)
                        check = validate_plan(c, d, hid, new, {cl["id"]: cl["quote"] for cl in d.evidence_for(hid)})
                        new["skúšobný_beh"] = check["štatistiky"]
                        d.update_experiment(eid, spec_json=new, statement=new.get("statement", e["statement"]),
                                            metric=new.get("primary_metric", e["metric"]),
                                            baseline=new.get("primary_baseline", e["baseline"]),
                                            success_rule=success_rule(new), problem_link=new.get("problem_link"),
                                            flags_json={k: v for k, v in (("chyby_návrhu", check["chyby"]),
                                                                          ("upozornenia", check["upozornenia"])) if v})
                    except (ValueError, KeyError) as exc:
                        st.error(f"Plán sa nedá uložiť: {exc}")
                    else:
                        refresh()
    if not e["locked_at"]:
        show_lock()
    else:
        st.caption(f"Uzamknuté {e['locked_at'][:16]} · hash {(e['prereg_hash'] or '')[:16]}…")
    if flags.get("chyba_testu"):
        st.error("Test zlyhal: " + flags["chyba_testu"], icon=":material/error:")
    if not res:
        return
    prim = res.get("hlavné") or {}
    with st.container(border=True):
        st.markdown("**Výsledok spätného testu**")
        with st.container(horizontal=True):
            if prim.get("metrika"):
                st.metric(f"{prim['metrika']} – {prim.get('model')}", prim.get("model_hodnota"), border=True)
                st.metric(f"naivný odhad {prim.get('naivný')}", prim.get("naivný_hodnota"), border=True)
                st.metric("Zlepšenie", f"{prim['zlepšenie_pct']:.1f} %" if prim.get("zlepšenie_pct") is not None else "–",
                          border=True)
                st.metric("Pravidlo úspechu", "splnené" if prim.get("splnené") else "nesplnené", border=True)
            else:
                st.metric("Výsledok", "popisný (bez štítku)", border=True)
        if prim.get("ci95_rozdielu"):
            st.caption(f"95 % interval rozdielu model − naivný: {prim['ci95_rozdielu'][0]} až {prim['ci95_rozdielu'][1]}"
                       " (ak obsahuje 0, rozdiel nie je preukázaný). " + res.get("výber", ""))
        metrics = [m for m in res.get("metriky") or [] if any(m in r for r in res.get("tabuľka") or [])]
        st.markdown("Výsledková tabuľka – všetky postupy × metriky (hlavná dvojica je v predregistrácii):")
        st.dataframe([{"postup": r["postup"] + (" ← vybraný" if r["postup"] == prim.get("model") else "")
                       + (" ← hlavný naivný" if r["postup"] == prim.get("naivný") else ""), "typ": r.get("typ", ""),
                       **{m: r.get(m) for m in metrics}} for r in res.get("tabuľka") or []], hide_index=True,
                     alt="Výsledková tabuľka")
        det = res.get("detaily") or {}
        if det.get("dôležitosť_príznakov"):
            st.markdown("Dôležitosť príznakov (pokles priemernej presnosti po premiešaní):")
            st.bar_chart(det["dôležitosť_príznakov"], height=200, horizontal=True)
        if det.get("rad_súčet"):
            chart = {"skutočnosť (súčet)": det["rad_súčet"]}
            for m, vals in (det.get("predpoveď_súčet") or {}).items():
                chart[f"predpoveď {m}"] = vals
            st.line_chart({k: dict(zip(det["obdobia"], v)) for k, v in chart.items()}, height=240)
        with st.expander("Podrobnosti (obdobia učenia, validácie, testu)"):
            st.json({k: v for k, v in det.items() if k not in ("rad_súčet", "predpoveď_súčet", "obdobia")})
        if res.get("položky"):
            st.markdown(f"**{esc(res.get('položky_popis', 'Položky'))}**")
            st.dataframe(res["položky"], hide_index=True, alt="Položky na kontrolu")
        with st.expander("Protokol prípravy dát pri teste"):
            st.dataframe(res.get("protokol") or [], hide_index=True, alt="Protokol prípravy dát")
    concl = json_or(e["conclusion_json"], {})
    if concl:
        t = concl.get("text") or {}
        with st.container(border=True):
            st.markdown("**Záver pre problém**" + (" (napísal model z faktov)" if concl.get("zdroj") == "model" else
                                                  " (poskladaný kódom)"))
            for key, label in (("what_tested", "Čo sa testovalo"), ("result_for_decision", "Čo to znamená pre rozhodnutie"),
                               ("robustness", "Robustnosť"), ("limits", "Obmedzenia")):
                if t.get(key):
                    st.markdown(f"**{label}:** {esc(t[key])}")
            if t.get("interview_questions"):
                st.markdown("**Otázky do rozhovorov Brány 0:**\n" + "\n".join(f"- {esc(q)}" for q in t["interview_questions"]))
            if concl.get("poznámka"):
                st.caption(concl["poznámka"])
            with st.expander("Fakty, z ktorých záver vznikol"):
                st.text("\n".join(concl.get("fakty") or []))


def show_legacy_experiment() -> None:
    if flags.get("chyby_návrhu"):
        st.warning("Návrh odkazuje na neexistujúce stĺpce: " + "; ".join(flags["chyby_návrhu"]), icon=":material/error:")
    if flags.get("obdobie_upravené"):
        st.info("Zoskupenie upravil kód, aby rad mal dosť úplných období: " + flags["obdobie_upravené"], icon=":material/tune:")
    with st.container(border=True):
        st.markdown("**Predregistrácia** (pôvodný typ: predpoveď sumy pre organizáciu)")
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
                spec = st.text_area("Postup (tabuľka, stĺpce, model)", json.dumps(json_or(e["spec_json"], {}),
                                                                               ensure_ascii=False, indent=2), height=200)
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
            show_lock()
    if res:
        with st.container(border=True):
            st.markdown("**Spätný test**")
            with st.container(horizontal=True):
                st.metric(f"Chyba modelu ({res['metric']})", res["model_error"], border=True)
                st.metric("Chyba naivného odhadu", res["baseline_error"], border=True)
                st.metric("Zlepšenie", f"{res['improvement']:.1%}", border=True)
                st.metric("95 % interval rozdielu", f"{res['diff_ci95'][0]} až {res['diff_ci95'][1]}", border=True)
            if res.get("rozsah_dát"):
                st.caption(f"Dáta od {res['rozsah_dát'].get('od') or 'začiatku'} do {res['rozsah_dát'].get('do')}"
                           + (f" · vyradené neúplné obdobia: {', '.join(res['vyradené_neúplné'])}" if res.get("vyradené_neúplné") else ""))
            if flags.get("model_neprekonal_naivny_odhad"):
                st.warning("Stop podmienka: model neprekonal naivný odhad.", icon=":material/flag:")
            st.dataframe([{"obdobie": res["periods"][f["index"]], "skutočnosť": f["actual"], "model": round(f["model"], 2),
                           "naivný": round(f["baseline"], 2)} | ({"vybraný model": f["vybraný_model"]} if "vybraný_model" in f else {})
                          for f in res["folds"]], hide_index=True, alt="Predpovede po obdobiach")
    with st.expander("Postup krok po kroku – ako experiment vznikol a ako sa počítal", icon=":material/account_tree:",
                     expanded=bool(res)):
        try:
            trace = explain(c, d, e)
        except Exception as exc:  # noqa: BLE001 – postup je iba popis; chyba nesmie zhodiť stránku
            st.caption(f"Postup sa nedá zostaviť: {exc}")
            trace = []
        for step in trace:
            st.markdown(f"**{step['krok']}**")
            st.markdown(step["text"].replace("$", "\\$"))
            if step.get("kód"):
                st.code(step["kód"], language="sql")
            if step.get("tabuľka"):
                st.dataframe(step["tabuľka"], hide_index=True, alt=step["krok"])
        if trace:
            st.download_button("Stiahnuť postup (Markdown)", as_markdown(e, trace), file_name=f"H{hid}_E{eid}_postup.md",
                               mime="text/markdown", icon=":material/download:", key=f"g2_trace_{eid}")


if e["task"]:
    show_task_experiment()
else:
    show_legacy_experiment()

if e["demo_path"] and Path(e["demo_path"]).exists():
    html_text = Path(e["demo_path"]).read_text(encoding="utf-8")
    st.download_button("Stiahnuť správu (HTML)" if e["task"] else "Stiahnuť demo (HTML)", html_text,
                       file_name=Path(e["demo_path"]).name, mime="text/html", icon=":material/download:")
    with st.expander("Náhľad správy" if e["task"] else "Náhľad dema"):
        st.html(html_text)

with st.container(border=True):
    st.markdown("**Spätná väzba expertov a rozhodnutie**")
    expert = json_or(e["expert_json"], {})
    records = expert.get("záznamy") or ([{"kedy": "", "rozhovor": None, "pseudonym": "", "nové": expert["nové"],
                                          "poznámka": expert.get("poznámka", "")}] if expert.get("nové") else [])
    talks = {i["id"]: i for hh in {hid, h["parent_id"]} if hh for i in d.interviews_for(hh)}
    if records:
        new_n = sum(r["nové"] == "áno" for r in records)
        st.caption(f"Nové poznatky pre expertov: {new_n} z {len(records)} záznamov")
        st.dataframe([{"kedy": r.get("kedy", ""), "rozhovor": r.get("pseudonym") or "–", "nové": r["nové"],
                       "poznámka": r.get("poznámka", "")} for r in records], hide_index=True, alt="Spätná väzba expertov")
    with st.form(f"g2_expert_{eid}"):
        talk = st.selectbox("Rozhovor (Rozhovory, pseudonym – bez mien)", [None] + list(talks),
                            format_func=lambda i: "bez rozhovoru" if i is None else
                            f"{talks[i]['pseudonym']} · {talks[i]['role_label'] or talks[i]['interviewee_kind']} · {talks[i]['held_at']}")
        new = st.radio("Priniesol výsledok niečo nové?", ["áno", "nie", "zatiaľ nevieme"], horizontal=True)
        note = st.text_area("Čo povedal (bez mien a kontaktov)")
        if st.form_submit_button("Pridať záznam"):
            from datetime import date as _date

            records.append({"kedy": _date.today().isoformat(), "rozhovor": talk,
                            "pseudonym": talks[talk]["pseudonym"] if talk else "", "nové": new, "poznámka": note.strip()})
            decided = [r for r in records if r["nové"] != "zatiaľ nevieme"]
            flags["experti_nic_nove"] = bool(decided) and all(r["nové"] == "nie" for r in decided)
            d.update_experiment(eid, expert_json={"záznamy": records, "nové": new}, flags_json=flags)
            refresh()
    with st.form(f"g2_decide_{eid}"):
        decision = st.selectbox("Rozhodnutie", ["passed", "paused", "stopped"])
        reason = st.text_area("Zdôvodnenie")
        if st.form_submit_button("Uložiť rozhodnutie", icon=":material/gavel:"):
            decide(d, hid, 2, decision, reason, org_id=e["org_id"])
            refresh()
    for r in d.gate_decisions_for(hid, 2):
        st.caption(f"{r['decided_at'][:16]} · organizácia {r['org_id'] or '–'} · {r['decision']} · {r['reason'] or ''}")
