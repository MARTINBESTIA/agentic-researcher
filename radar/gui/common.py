"""Spoločné pomôcky GUI: konfigurácia, databáza, bezpečné zobrazenie textu zo zdrojov.

Text zo zdrojov je nedôveryhodný (R-GUI-4): pred zobrazením cez HTML sa vždy escapuje
(html.escape) a zvýraznenie citátu sa vkladá až potom. Ako odkazy sa zobrazujú iba URL s http/https.
Zápis ide iba cez metódy DB vrstvy (R-GUI-3).
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any

import streamlit as st
from rapidfuzz import fuzz

from radar.config import Config, load_config
from radar.db import DB
from radar.strength import evidence_strength

ROOT = Path(__file__).resolve().parent.parent.parent


def config_path() -> Path:
    return Path(os.environ.get("RADAR_CONFIG") or ROOT / "config.yaml")


@st.cache_resource
def get_config(path: str) -> Config:
    return load_config(path)


@st.cache_resource
def get_db(db_path: str) -> DB:
    return DB(db_path)


def cfg() -> Config:
    return get_config(str(config_path()))


def db() -> DB:
    return get_db(cfg().db_path)


def refresh() -> None:
    """Po zápise: zahoď cache výpočtov a prekresli stránku."""
    st.cache_data.clear()
    st.rerun()


def safe_url(url: str | None) -> str | None:
    """Odkaz iba pre http/https (nie javascript:, data:, file: …)."""
    if url and url.strip().lower().startswith(("http://", "https://")):
        return url.strip()
    return None


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def highlighted_window(text: str, quote: str, radius: int = 600) -> str:
    """HTML úsek zdroja okolo citátu: najprv escape celého textu, potom <mark> okolo citátu."""
    text = text or ""
    quote = quote or ""
    if not text:
        return f"<blockquote>{esc(quote)}</blockquote>"
    align = fuzz.partial_ratio_alignment(quote.lower()[:400], text.lower())
    start, end = align.dest_start, align.dest_end
    if align.score < 80:
        return f"<blockquote>{esc(quote)}</blockquote><p><em>Citát sa v zobrazenom texte nenašiel presne.</em></p>"
    a, b = max(0, start - radius), min(len(text), end + radius)
    before, match, after = esc(text[a:start]), esc(text[start:end]), esc(text[end:b])
    prefix = "… " if a > 0 else ""
    suffix = " …" if b < len(text) else ""
    return (f"<div style='white-space: pre-wrap; line-height: 1.5'>{prefix}{before}"
            f"<mark>{match}</mark>{after}{suffix}</div>")


@st.cache_data(ttl=120, max_entries=4)
def hypothesis_table(db_path: str) -> list[dict[str, Any]]:
    """Hypotézy so silou dôkazov (zložky, nie jedno číslo)."""
    d = get_db(db_path)
    rows = []
    theme_names = {t["id"]: t["name"] for t in d.themes()}
    for h in d.all_hypotheses():
        label, n_ver, n_part, n_dom = evidence_strength(d.evidence_for(h["id"]))
        test = json_or(h["validation_json"], {}) if h["validation_state"] == "tested" else {}
        incomplete = (test.get("agent") or {}).get("ok") is False
        rows.append({
            "id": h["id"], "hypotéza": h["title"], "segment": h["segment"], "téma": theme_names.get(h["theme_id"], ""),
            "stav": h["status"], "pôvod": h["origin"],
            "sila": label, "overené": n_ver, "čiastočne": n_part, "domény": n_dom,
            "test": test.get("verdikt") or VALIDATION_STATE.get(h["validation_state"], "neotestovaná"),
            "test neúplný": incomplete,
            "protidôkazy": test.get("protidôkazy"), "testované": (h["validated_at"] or "")[:10],
            "ľudia": bool(h["people_enabled"]), "brána 0": h["gate0_status"], "projekt": h["project"] or "",
            "zhoda so zakladateľom": h["founder_fit"] or "", "odvetvie": h["sector"] or "",
        })
    return rows


VALIDATION_STATE = {None: "neotestovaná", "searched": "čaká na overenie", "tested": "otestovaná"}


def theme_table(db_path: str, hyp_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Témy so súhrnom: sila dôkazov z tvrdení všetkých hypotéz témy (každé tvrdenie raz) a verdikty testov."""
    d = get_db(db_path)
    by_id = {r["id"]: r for r in hyp_rows}
    out = []
    for t in d.themes():
        members = [by_id[h["id"]] for h in d.theme_members(t["id"]) if h["id"] in by_id]
        evidence = {e["id"]: e for h in members for e in d.evidence_for(h["id"])}
        label, n_ver, n_part, n_dom = evidence_strength(list(evidence.values()))
        tests = [m["test"] for m in members]
        out.append({
            "id": t["id"], "téma": t["name"], "hypotéz": len(members), "sila": label, "overené": n_ver, "domény": n_dom,
            "podložené": tests.count("podložená"), "sporné": tests.count("sporná"),
            "neotestované": sum(1 for x in tests if x in ("neotestovaná", "čaká na overenie")),
            "brána 0": sum(1 for m in members if m["ľudia"]),
            "názov": {"model": "model", "medoid": "z hypotézy", "manual": "ručne"}.get(t["name_method"], t["name_method"]),
        })
    return out


# ---------------------------------------------------------------- akcie na pozadí (Správca akcií)
JOB_STATUS = {"queued": "v poradí", "starting": "štartuje", "running": "beží", "done": "hotovo",
              "failed": "skončila s chybou", "stopped": "zastavená", "interrupted": "prerušená", "cancelled": "zrušená"}


def job_label(kind: str) -> str:
    from radar.workflows import WORKFLOWS

    return WORKFLOWS.get(kind, kind)


def _start(kind: str, minutes: float, ids: list[int] | None, mode: str) -> None:
    from radar.jobs import JobBusy, start_job

    try:
        job_id = start_job(cfg(), db(), config_path(), kind, minutes, ids, mode=mode)
    except JobBusy as exc:
        st.warning(str(exc), icon=":material/hourglass:")
        return
    verb = "zaradená do poradia" if db().get_job(job_id)["status"] == "queued" else "spustená"
    st.toast(f"Úloha #{job_id} {verb}: {job_label(kind)}", icon=":material/rocket_launch:")
    st.rerun()


def start_job_button(kind: str, label: str, minutes: float, ids: list[int] | None = None, key: str = "",
                     icon: str = ":material/play_arrow:", help: str | None = None, disabled: bool = False) -> None:
    """Spustí akciu ako samostatný proces (GUI ostane responzívne). Ak niečo beží: zaradiť do poradia,
    alebo bežiacu zastaviť a spustiť túto (na GPU sa zmestí iba jeden veľký model)."""
    from radar.jobs import refresh_jobs

    d = db()
    refresh_jobs(d)
    key = key or f"job_{kind}"
    active = d.active_jobs()
    if not active:
        if st.button(label, key=key, icon=icon, type="primary", disabled=disabled, help=help):
            _start(kind, minutes, ids, "start")
        return
    with st.container(horizontal=True):
        if st.button(f"{label} – do poradia", key=key + "_q", icon=":material/queue:", disabled=disabled,
                     help=f"Spustí sa, keď skončí bežiaca úloha #{active[0]['id']} ({job_label(active[0]['kind'])})."):
            _start(kind, minutes, ids, "queue")
        if st.button("Zastaviť bežiacu a spustiť", key=key + "_r", icon=":material/swap_horiz:", disabled=disabled,
                     help=f"Zastaví úlohu #{active[0]['id']} ({job_label(active[0]['kind'])}); rozpracovaný stav "
                          "ostane v databáze a dá sa dokončiť neskôr."):
            _start(kind, minutes, ids, "replace")


def _job_summary(job) -> str:
    stats = json_or(job["stats_json"], {})
    parts = []
    created = stats.get("nové_hypotézy") or []
    if job["kind"] in ("scout", "evidence", "topics", "run-all"):
        parts.append(f"nové hypotézy: {len(created)}")
    test = stats.get("testovanie") or {}
    if job["kind"] == "validate" and test:
        parts.append(f"otestované {test.get('tested', 0)} z {test.get('total', 0)}")
    errors = [s["krok"] for s in stats.get("stages", []) if "chyba" in s]
    if errors:
        parts.append("chyby v krokoch: " + ", ".join(errors))
    skipped = [s["krok"] for s in stats.get("stages", []) if "preskočené" in s]
    if skipped:
        parts.append(f"nestihnuté kroky ({len(skipped)}): " + ", ".join(skipped[:8]) + ("…" if len(skipped) > 8 else ""))
    if stats.get("spánok_min"):
        parts.append(f"počítač počas behu spal {stats['spánok_min']} min")
    return " · ".join(parts)


def _stop(job_id: int | None = None, everything: bool = False) -> None:
    from radar.jobs import start_next, stop_all, stop_job

    d = db()
    if everything:
        stop_all(d)
    else:
        stop_job(d, job_id)
        start_next(cfg(), d)  # poradie pokračuje ďalšou úlohou
    st.rerun()


def _render_job(job, live: bool) -> None:
    from radar.jobs import log_tail

    progress = json_or(job["progress_json"], {})
    head = f"#{job['id']} {job_label(job['kind'])} – {JOB_STATUS.get(job['status'], job['status'])}"
    with st.container(border=True):
        st.markdown(f"**{esc(head)}**")
        bits = [f"krok: {progress['krok']}"] if progress.get("krok") and live else []
        if progress.get("dávka"):
            bits.append(f"dávka {progress['dávka']}: {', '.join(progress.get('hypotézy') or [])}")
        if live:
            from datetime import datetime, timezone

            from radar.eta import local, schedule

            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds() / 60
            limit = json_or(job["args_json"], {}).get("minutes")
            bits.append(f"beží od {local(job['started_at'])} ({elapsed:.0f} min" + (f" z limitu {limit:g} min)" if limit else ")"))
            plan = next((s for s in schedule(db()) if s["job"]["id"] == job["id"]), None)
            if plan:
                bits.append(f"odhad konca ~{local(plan['koniec_odhad'])}"
                            + (f" (najneskôr {local(plan['koniec_najneskôr'])})" if plan["koniec_najneskôr"] else ""))
        summary = _job_summary(job)
        if summary:
            bits.append(summary)
        if bits:
            st.caption(" · ".join(bits))
        tail = log_tail(job["log_path"], 12 if live else 6)
        if tail:
            st.code(tail, language=None, wrap_lines=True)
        if live:
            queued = db().queued_jobs()
            with st.container(horizontal=True):
                if st.button("Zastaviť úlohu" + (" (poradie pokračuje)" if queued else ""), key=f"stop_{job['id']}",
                             icon=":material/stop:"):
                    _stop(job["id"])
                if queued and st.button("Zastaviť všetko vrátane poradia", key=f"stopall_{job['id']}",
                                        icon=":material/cancel:"):
                    _stop(everything=True)


@st.fragment(run_every="5s")
def _live_jobs() -> None:
    from radar.jobs import refresh_jobs

    d = db()
    refresh_jobs(d)
    active = d.active_jobs()
    if not active:
        st.cache_data.clear()
        st.rerun()  # úloha skončila: prekresli celú stránku (nové hypotézy, počty, ďalšia z poradia)
    for job in active:
        _render_job(job, live=True)


def queue_panel() -> None:
    """Úlohy čakajúce v poradí (spustia sa po skončení bežiacej)."""
    from radar.jobs import start_next

    d = db()
    queued = d.queued_jobs()
    if not queued:
        return
    from radar.eta import local, schedule

    plan = {s["job"]["id"]: s for s in schedule(d)}
    last = max((s["koniec_odhad"] for s in plan.values()), default=None)
    st.markdown(f"**V poradí ({len(queued)})**" + (f" · všetko hotové ~{local(last)}" if last else ""))
    for i, job in enumerate(queued, 1):
        args = json_or(job["args_json"], {})
        ids = ", ".join(f"H{x}" for x in args.get("hypotheses") or [])
        when = plan.get(job["id"])
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"{i}. {esc(job_label(job['kind']))} · limit {args.get('minutes', '?')} min"
                        + (f" · ~{local(when['začiatok'])} – {local(when['koniec_odhad'])}" if when else "")
                        + (f" · {ids}" if ids else ""))
            if st.button("Zrušiť", key=f"cancel_{job['id']}", icon=":material/close:"):
                _stop(job["id"])
    if not d.active_jobs() and st.button("Spustiť ďalšiu z poradia", key="queue_next", icon=":material/play_arrow:",
                                         type="primary"):
        start_next(cfg(), d)
        st.rerun()


def jobs_panel(show_last: bool = True) -> None:
    """Stav bežiacej úlohy (obnovuje sa každých 5 s) alebo výsledok poslednej úlohy, a poradie."""
    from radar.jobs import refresh_jobs

    d = db()
    refresh_jobs(d)
    if d.active_jobs():
        _live_jobs()
    elif show_last:
        recent = [j for j in d.recent_jobs(5) if j["status"] not in ("queued", "cancelled")][:1]
        if recent:
            _render_job(recent[0], live=False)
    queue_panel()


def _hypothesis_choices(kind: str, mode: str, key: str) -> list[int] | None:
    """Výber hypotéz pre akciu: overené (podložené po teste) alebo vybrané na Bránu 0."""
    d = db()
    if mode == "selected":
        hyps = {h["id"]: h["title"] for h in d.people_enabled_hypotheses()}
        if not hyps:
            st.caption("Žiadna hypotéza nie je vybraná na Bránu 0 – najprv „Zbierať ľudí ku overeným hypotézam“.")
            return []
        return st.multiselect("Hypotézy", list(hyps), default=list(hyps), key=key,
                              format_func=lambda i: f"H{i}: {hyps[i][:70]}")
    if mode in ("gate0", "gate1_passed"):
        hyps = d.gate1_hypotheses() if mode == "gate0" else d.gate2_hypotheses()
        if not hyps:
            st.caption("Žiadna hypotéza nie je vybraná na Bránu 0." if mode == "gate0" else
                       "Zatiaľ žiadna hypotéza neprešla Bránou 1 (pripravenosť ≥ 0,5, tvoje dáta ≥ 0,5 alebo tvoje "
                       "rozhodnutie na stránke Brána 1).")
            return []
        labels = {h["id"]: f"H{h['id']}: {h['title'][:70]} · {h['gate1_status' if mode == 'gate0' else 'gate2_status']}"
                  for h in hyps}
        todo = [h["id"] for h in hyps if (h["gate1_status"] in ("not_started", "running", "needs_input") if mode == "gate0"
                                          else h["gate2_status"] in ("not_started", "running"))]
        st.caption("Brána 1 beží súbežne s rozhovormi Brány 0." if mode == "gate0" else
                   "Dvojice hypotéza × organizácia vzniknú z organizácií, ktoré sú v nájdených dátach.")
        default = todo[:3]
        if mode == "gate0":  # predvolene tie, pri ktorých má beh prácu (úloha #75: H1–H3 boli prehľadané, nové nie)
            from radar.gate1 import plan_run
            from radar.workflows import ACTIONS

            plan = plan_run(cfg(), d, [h["id"] for h in hyps], ACTIONS[kind].minutes)
            default = list(dict.fromkeys(plan["s_modelom"] + plan["na_kontrolu"])) or default
        return st.multiselect("Hypotézy", list(labels), default=default, key=key, format_func=labels.get)
    rows = hypothesis_table(cfg().db_path)
    order = {"silnejšia": 2, "stredná": 1, "slabá": 0}
    verified = [r for r in rows if r["test"] == "podložená"]
    note = "overené testom (podložené)"
    if not verified:  # ešte nič neotestované: aspoň hypotézy so silnými dôkazmi
        verified = [r for r in rows if r["sila"] in ("silnejšia", "stredná") and r["stav"] != "rejected"]
        note = "zatiaľ nič nie je otestované – ponúkam hypotézy so strednou a silnejšou silou dôkazov"
    verified.sort(key=lambda r: (order[r["sila"]], r["overené"], r["domény"]), reverse=True)
    with_people = d.hypotheses_with_people()
    labels = {r["id"]: f"H{r['id']}: {r['hypotéza'][:70]}" + (" · ľudia už sú" if r["id"] in with_people else "")
              for r in verified}
    default = [r["id"] for r in verified if r["id"] not in with_people][: cfg().people.auto_select_top]
    st.caption(f"{len(verified)} hypotéz – {note}. Predvolene {len(default)} najsilnejšie bez ľudí; osobné údaje sa "
               "zbierajú iba k hypotézam, ktoré tu zvolíš (Brána 0).")
    return st.multiselect("Hypotézy", list(labels), default=default, key=key, format_func=labels.get)


def action_card(kind: str, key_prefix: str = "act", extra: str | None = None) -> None:
    """Karta jednej akcie: čo robí, aké dáta pribudnú, modely, limit, výber hypotéz a tlačidlá."""
    from radar.workflows import ACTIONS

    a = ACTIONS[kind]
    c = cfg()
    with st.container(border=True):
        st.markdown(f"**{esc(a.label)}**")
        st.caption(f"{a.what}  \n**Pribudne:** {a.data} · **Modely:** {a.models}" + (f"  \n{extra}" if extra else ""))
        blocked = a.needs_lia and not c.privacy.lia_completed
        if blocked:
            st.warning("Zablokované, kým nepotvrdíš LIA (privacy.lia_completed).", icon=":material/lock:")
        from radar.eta import typical_minutes

        typical = typical_minutes(db(), kind)
        if typical is not None:
            st.caption(f"Zvyčajne trvá ~{typical:g} min (medián posledných behov; skončí aj pred limitom, keď nemá prácu).")
        ids = _hypothesis_choices(kind, a.hypotheses, f"{key_prefix}_{kind}_h") if a.hypotheses else None
        minutes = st.number_input("Limit (min)", 5, 1440, a.minutes, step=15, key=f"{key_prefix}_{kind}_min")
        if kind == "gate1" and ids:
            from radar.gate1 import plan_run

            plan = plan_run(c, db(), ids, minutes)
            names = ", ".join(f"H{h}" for h in plan["s_modelom"]) or "žiadna (všetky polia sú hľadané nedávno)"
            st.caption(f"**V tomto behu s modelmi:** {names} (odhad ~{plan['odhad_min']} min)"
                       + (f" · na ďalší beh: {len(plan['nabudúce'])}" if plan["nabudúce"] else "")
                       + (f" · už vyhodnotené, preskočia sa: {', '.join(f'H{h}' for h in plan['vyhodnotené'])} "
                          "(znova: Brána 1 → Prehľadať znova)" if plan["vyhodnotené"] else "")
                       + f". Kontrola zdrojov a sťahovanie dát (vyhradených {c.gate1.download_reserve_minutes:g} min) "
                       "prebehnú pre všetky zvolené"
                       + (": nájdené zdroje čakajú pri " + ", ".join(f"H{h}" for h in plan["na_kontrolu"])
                          if plan["na_kontrolu"] else "") + ".")
        start_job_button(kind, a.label, minutes, ids, key=f"{key_prefix}_{kind}_go",
                         disabled=blocked or (a.hypotheses is not None and not ids))


def hypothesis_options(d: DB, only_people: bool = False) -> dict[int, str]:
    hyps = d.people_enabled_hypotheses() if only_people else d.active_hypotheses()
    return {h["id"]: f"H{h['id']}: {h['title'][:90]}" for h in hyps}


def pick_hypothesis(d: DB, key: str, only_people: bool = True) -> int | None:
    options = hypothesis_options(d, only_people)
    if not options:
        st.info("Žiadna hypotéza nie je vybraná na hľadanie ľudí. Zapni ju na stránke Problémy alebo príkazom "
                "`python -m radar select --hypothesis ID --people on`.", icon=":material/info:")
        return None
    return st.selectbox("Hypotéza", list(options), format_func=options.get, key=key)


def json_or(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
