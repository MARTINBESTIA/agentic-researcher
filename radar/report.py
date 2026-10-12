"""Krok 7 – správa v Markdowne a register dôkazov v CSV: dôkazy namiesto bodového skóre.

Sila dôkazov sa počíta deterministicky (počet overených tvrdení a rôznych domén),
nie úsudkom modelu, aby bolo jasné, na čom stojí. Prebratý text (rovnaký citát
na inom webe) sa nezapočítava.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import APIError, APITimeoutError

from .config import Config
from .db import DB
from .embed import Embedder, similar_pairs
from .extract import checked_claim_type
from .strength import NOT_SUPPORTING, STRENGTH_ORDER, evidence_strength  # noqa: F401 (evidence_strength sa importuje aj odtiaľto)
from .util import fmt_num, md_cell, md_url

log = logging.getLogger(__name__)

STATUS_LABEL = {"verified": "overené", "partial": "čiastočne", "rejected": "zamietnuté", "pending": "neoverené"}
FACTOR_LABEL = {"data": "zo zdroja", "assumption": "PREDPOKLAD", "missing": "CHÝBA ZDROJ"}


def _bullets(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return [f"**{title}:**", *[f"- {md_cell(i)}" for i in items], ""]


def _market_lines(est: dict[str, Any]) -> list[str]:
    detail = est["detail"]
    lines = ["**Hrubý odhad trhu (súčin faktorov):**", ""]
    if est["complete"]:
        lines.append(
            f"- Rozpätie {fmt_num(est['low'])} – {fmt_num(est['high'])} {est['unit']} (stred {fmt_num(est['base'])})"
        )
    else:
        lines.append("- Neúplný – aspoň jeden faktor nemá overený zdroj, preto sa výsledok nepočíta.")
    conf = detail.get("confidence")
    if conf:
        lines.append(f"- Miera istoty: **{conf['label']}** ({'; '.join(conf.get('reasons', []))})")
    lines.append(f"- Segment: {md_cell(detail.get('segment_definition'))}")
    for f in detail.get("factors", []):
        unit = "podiel 0–1" if "%" in (f.get("unit") or "") else f.get("unit")
        rng = f"{fmt_num(f.get('low'))} – {fmt_num(f.get('high'))}" if f.get("low") is not None else "?"
        line = f"  - {md_cell(f['name'])} [{unit}]: {rng} ({FACTOR_LABEL.get(f.get('status'), f.get('status'))})"
        if f.get("rationale"):
            line += f" – {md_cell(f['rationale'], 200)}"
        lines.append(line)
        for ev in f.get("evidence", [])[:3]:
            lines.append(f"    - „{md_cell(ev['quote'], 160)}“ ({md_cell(ev['unit'])}) – <{ev['url']}>")
    lines += ["- Násobením rozpätí sa neistota zväčšuje; ber to ako rádový odhad, nie ako číslo do biznis plánu.", ""]
    return lines


def _hypothesis_section(db: DB, hyp, evidence: list, label: str, n_ver: int, n_part: int, n_dom: int, since: str | None) -> list[str]:
    project = f" · **Projekt:** `{hyp['project']}`" if hyp["project"] else ""
    lines = [
        f"### H{hyp['id']}: {md_cell(hyp['title'])}",
        "",
        f"**Segment:** {md_cell(hyp['segment']) or '–'} · **Stav:** {hyp['status']}{project} · "
        f"**Sila dôkazov:** {label} (overené: {n_ver} · čiastočne: {n_part} · domény: {n_dom})",
        "",
    ]
    if hyp["description"]:
        lines += [md_cell(hyp["description"]), ""]

    critique = db.latest_critique(hyp["id"])
    content = critique["content"] if critique else {}
    if content.get("summary"):
        lines += [f"**Zhrnutie (model, len z dôkazov nižšie):** {md_cell(content['summary'])}", ""]

    if evidence:
        lines += ["| # | Tvrdenie | Citát | Zdroj | Stav |", "|---|---|---|---|---|"]
        for e in evidence[:15]:
            new = " ★" if since and (e["verified_at"] or "") > since else ""
            lines.append(
                f"| C{e['id']}{new} | {md_cell(e['problem'], 160)} | „{md_cell(e['quote'], 220)}“ | "
                f"[{md_cell(e['domain'])}]({md_url(e['url'])}), {e['fetched_at'][:10]} | "
                f"{STATUS_LABEL.get(e['status'], e['status'])}"
                f"{' · ' + NOT_SUPPORTING[e['claim_type']] if e['claim_type'] in NOT_SUPPORTING else ''} |"
            )
        if len(evidence) > 15:
            lines.append(f"\n_…a ďalších {len(evidence) - 15} tvrdení v databáze (reports/register.csv)._")
        lines.append("")
    else:
        lines += ["_Zatiaľ bez overených dôkazov._", ""]

    dups = db.duplicates_for(hyp["id"])
    if dups:
        listed = ", ".join(f"C{d['id']} ({md_cell(d['domain'])}) = C{d['duplicate_of']}" for d in dups[:8])
        lines += [f"_Prebratý text, nezapočítava sa: {listed}._", ""]

    lines += _bullets("Protiargumenty", content.get("counterarguments", []))
    lines += _bullets("Čo ešte overiť", content.get("missing_evidence", []))
    links = content.get("competitor_links", [])
    if links:
        lines.append("**Možné existujúce riešenia (výsledky vyhľadávania, over ručne):**")
        lines += [f"- [{md_cell(l['title'], 100) or l['url']}]({md_url(l['url'])})" for l in links[:8]]
        lines.append("")
    questions = content.get("interview_questions", [])
    if questions:
        lines.append("**Otázky na rozhovory:**")
        lines += [f"{i}. {md_cell(q)}" for i, q in enumerate(questions, 1)]
        lines.append("")

    estimate = db.latest_market_estimate(hyp["id"])
    if estimate:
        lines += _market_lines(estimate)

    notes = db.notes_for(hyp["id"])
    if notes:
        lines.append("**Poznámky:**")
        lines += [f"- {n['created_at'][:10]} ({n['source']}): {md_cell(n['text'], 1500)}" for n in notes[-5:]]
        lines.append("")
    return lines


def write_register(db: DB, path: Path) -> int:
    """Register dôkazov pre Excel: jeden riadok = jedno tvrdenie zaradené k hypotéze."""
    rows = db.register_rows()
    columns = [
        "hypothesis_id", "hypothesis", "segment", "hypothesis_status", "project", "claim_id", "status",
        "claim_type", "problem", "affected_party", "quote", "duplicate_of", "origin", "url", "domain",
        "fetched_at", "verified_at", "verify_note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM: Excel správne zobrazí diakritiku
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(columns)
        for r in rows:
            writer.writerow([r[c] for c in columns])
    return len(rows)


def build_report(cfg: Config, db: DB, embedder: Embedder | None = None) -> Path:
    now = datetime.now()
    counts = db.counts()
    since = db.last_finished("report")
    lines = [
        f"# Problem Radar – správa {now:%Y-%m-%d %H:%M}",
        "",
        "Toto je stolový výskum. Tvrdenia sú overené voči zdrojom, nie voči zákazníkom; "
        "o pokračovaní rozhodujú až rozhovory a ochota platiť.",
        "",
        f"Dokumenty: {counts['documents']} · tvrdenia overené / čiastočne / zamietnuté / neoverené: "
        f"{counts['verified']} / {counts['partial']} / {counts['rejected']} / {counts['pending']} · "
        f"prebratý text: {counts['duplicates']} · aktívne hypotézy: {counts['active_hypotheses']}",
        "",
        "Sila dôkazov: **silnejšia** = aspoň 4 overené tvrdenia z aspoň 3 domén, "
        "**stredná** = aspoň 2 overené z aspoň 2 domén, inak **slabá**. "
        + (f"★ = nový dôkaz od predošlej správy ({since[:10]})." if since else ""),
        "",
    ]

    lines += gate0_lines(db) + review_lines(db) + regulatory_lines(db) + discovery_lines(db)

    ranked = []
    for hyp in db.active_hypotheses():
        evidence = db.evidence_for(hyp["id"])
        label, n_ver, n_part, n_dom = evidence_strength(evidence)
        ranked.append((STRENGTH_ORDER[label], n_ver, hyp, evidence, label, n_part, n_dom))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    projects = [r for r in ranked if r[2]["project"]]
    others = [r for r in ranked if not r[2]["project"]]

    if projects:
        lines += [
            "## Rozpracované projekty",
            "",
            "Nápady, na ktorých už pracuješ (config.yaml → projects). Radar k nim zbiera dôkazy aj protiargumenty.",
            "",
        ]
        for _, n_ver, hyp, evidence, label, n_part, n_dom in projects:
            lines += _hypothesis_section(db, hyp, evidence, label, n_ver, n_part, n_dom, since)

    lines += ["## Nové hypotézy", ""]
    if not others:
        lines += ["_Zatiaľ žiadne. Spusti `python -m radar run-all`._", ""]
    for _, n_ver, hyp, evidence, label, n_part, n_dom in others:
        lines += _hypothesis_section(db, hyp, evidence, label, n_ver, n_part, n_dom, since)

    if embedder is not None:
        try:
            pairs = similar_pairs(embedder, db.active_hypotheses(), cfg.embeddings.similar_hypotheses)
        except (APIError, APITimeoutError) as exc:
            log.warning("Podobné hypotézy sa nedajú vypočítať: %s", exc)
            pairs = []
        if pairs:
            lines += [
                "## Možno rovnaké hypotézy",
                "",
                "Podľa embeddingov opisujú podobný problém, model ich však nezlúčil (alebo ide o dva projekty). "
                "Ak ide o to isté, zlúč ich: `python -m radar merge --from <menej dôkazov> --into <viac dôkazov>`.",
                "",
            ]
            lines += [f"- H{a} a H{b} (podobnosť {s:.2f})" for a, b, s in pairs[:15]]
            lines.append("")

    out_dir = Path(cfg.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = pseudonymize_report(cfg, db, "\n".join(lines) + "\n")
    path = out_dir / f"{now:%Y-%m-%d_%H%M}.md"
    path.write_text(text, encoding="utf-8")
    (out_dir / "latest.md").write_text(text, encoding="utf-8")
    write_register(db, out_dir / "register.csv")
    return path


# ======================================================================= v0.2: Brána 0 a objavovanie
VERDICT_LABEL = {"pass": "pass – podmienky splnené", "fail": "fail – problém neopisujú sami",
                 "chýba platiteľ": "chýba platiteľ", "nedostatok dát": "nedostatok dát"}


def gate0_lines(db: DB) -> list[str]:
    """Prehľad Brány 0 bez mien (osoby iba počtom; R-CRM-3, R-PRIV-8)."""
    from .crm import gate0_metrics

    hyps = db.people_enabled_hypotheses()
    lines = ["## Brána 0", ""]
    if not hyps:
        return lines + ["_Žiadna hypotéza nie je vybraná na hľadanie ľudí (python -m radar select …)._", ""]
    lines += [
        "Hypotézy vybrané na rozhovory. Brána 0 = 8–12 rozhovorov so zákazníkmi, aspoň 3 z 10 opíšu problém sami "
        "a je jasné, kto by platil. Návrh verdiktu počíta kód, rozhoduje človek. Mená a kontakty sú iba v GUI.",
        "",
        "| Hypotéza | Organizácie | Ľudia (experti) | Návrhy na kontrolu | Odoslané | Odpovede | Rozhovory | Spomenuli sami | Platiteľ | Návrh verdiktu |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for h in hyps:
        m = gate0_metrics(db, h["id"])
        links = db.links_for_hypothesis(h["id"])
        people = {l["person_id"] for l in links}
        experts = {l["person_id"] for l in links if l["is_expert"]}
        drafts = [o for o in db.outreach_list(h["id"]) if o["status"] in ("draft", "needs_edit")]
        f = m["funnel"]
        lines.append(
            f"| H{h['id']}: {md_cell(h['title'], 70)} | {len(db.orgs_for_hypothesis(h['id']))} | {len(people)} ({len(experts)}) | "
            f"{len(drafts)} | {f['odoslaní']} | {f['odpovede']} | {m['customer_interviews']}/{m['target']} | "
            f"{m['unprompted']} ({m['unprompted_share']:.0%}) | {md_cell(m['payer_confirmed']) or 'nepotvrdený'} | "
            f"{VERDICT_LABEL.get(m['verdict_proposal'], m['verdict_proposal'])} |"
        )
    due = db.follow_ups_due(datetime.now().date().isoformat())
    lines += ["", f"Follow-upy na odoslanie dnes: {len(due)}." if due else "", ""]
    return [line for line in lines]


def discovery_sort_key(db: DB, hyp) -> tuple:
    """Lexikografické poradie nových problémov bez váženého skóre (R-DISC-9): sila dôkazov,
    signál dopytu alebo regulácia s termínom, počet nezávislých domén, čerstvosť."""
    evidence = db.evidence_for(hyp["id"])
    label, n_ver, _, n_dom = evidence_strength(evidence)
    signal = any((e["claim_type"] == "demand_signal" and checked_claim_type("demand_signal", e["quote"]) == "demand_signal")
                 or (e["claim_type"] == "regulation" and e["effective_date"]) for e in evidence)
    return (STRENGTH_ORDER[label], int(signal), n_dom, hyp["first_seen_at"] or hyp["created_at"])


def discovery_lines(db: DB) -> list[str]:
    rows = db.discovery_hypotheses()
    lines = ["## Nové problémy z objavovania", ""]
    if not rows:
        waiting = db.discovery_claims_waiting()
        if waiting:
            return lines + [f"_Zatiaľ žiadne; {waiting} tvrdení z objavovania čaká na overenie a zaradenie (ďalší beh)._", ""]
        return lines + ["_Zatiaľ žiadne (objavovanie: python -m radar discover)._", ""]
    lines += [
        "Poradie: sila dôkazov → signál dopytu alebo regulácia s termínom → počet domén → čerstvosť (bez váženého skóre). "
        "Kandidát sa stane hypotézou až s 2 overenými tvrdeniami z 2 domén.",
        "",
        "| # | Problém | Segment | Odvetvie | Stav | Sila | Signál | Domény | Prvýkrát |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    ranked = sorted(rows, key=lambda h: discovery_sort_key(db, h), reverse=True)
    for h in ranked[:40]:
        strength, signal, n_dom, first = discovery_sort_key(db, h)
        label = {v: k for k, v in STRENGTH_ORDER.items()}[strength]
        lines.append(
            f"| H{h['id']} | {md_cell(h['title'], 120)} | {md_cell(h['segment'], 60)} | {md_cell(h['sector'], 40)} | "
            f"{h['status']} | {label} | {'áno' if signal else '–'} | {n_dom} | {(first or '')[:10]} |"
        )
    return lines + [""]


def regulatory_lines(db: DB) -> list[str]:
    """Regulačný radar (R-DISC-3): povinnosti s účinnosťou o 3–24 mesiacov."""
    today = datetime.now().date()
    lo, hi = today.replace(day=1).isoformat(), today.replace(year=today.year + 2).isoformat()
    rows = db.upcoming_regulations(lo, hi)
    if not rows:
        return []
    lines = ["## Regulačný radar", "", "Nové povinnosti s účinnosťou v najbližších mesiacoch (silný signál problému).", "",
             "| Od | Povinnosť | Kto | Zdroj |", "|---|---|---|---|"]
    for r in rows[:25]:
        lines.append(f"| {r['effective_date']} | {md_cell(r['problem'], 160)} | {md_cell(r['affected_party'], 60)} | "
                     f"[{md_cell(r['domain'])}]({md_url(r['url'])}) |")
    return lines + [""]


def review_lines(db: DB) -> list[str]:
    counts = db.review_counts()
    if not counts:
        return []
    return ["## Čakajúce kontroly", "", "Položky, o ktorých musí rozhodnúť človek (GUI → Kontrola):", "",
            *[f"- {task}: {n}" for task, n in sorted(counts.items())], ""]


def pseudonymize_report(cfg: Config, db: DB, text: str) -> str:
    """Mená osôb z tabuľky people sa v správe nahradia (R-PRIV-8)."""
    if not cfg.privacy.pseudonymize_reports:
        return text
    from .privacy import pseudonymize_text

    names = [p["full_name"] for p in db.all_people()]
    return pseudonymize_text(text, names) if names else text
