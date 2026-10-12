"""Témy nad hypotézami: zhluky podobných hypotéz, aby 400+ úzkych hypotéz (napr. 18 o e-faktúrach) dalo prehľad.

- Nedeštruktívne: hypotézy sa nezlučujú (to robí consolidate pri takmer rovnakých problémoch), iba dostanú
  theme_id. Samostatné hypotézy ostanú bez témy; zamietnuté sa do tém nezaraďujú.
- Zhluky počíta kód: priemerná väzba (average linkage) nad kosínusovou podobnosťou uložených embeddingov s prahom
  a najväčšou veľkosťou témy (themes.threshold, themes.max_size).
- Názov dá rýchly model iba zo zoznamu názvov hypotéz; pri chybe alebo nad limitom názvov na beh je názvom
  najtypickejšia hypotéza (medoid). Téma si medzi behmi ponechá ID a názov, ak sa jej členovia zmenili málo
  (Jaccard ≥ themes.keep_jaccard); ručný názov z GUI sa neprepíše.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Sequence

import numpy as np
from pydantic import BaseModel

from .config import Config
from .db import DB
from .models import as_router
from .prompts import THEME_NAME_SYSTEM
from .util import Deadline

log = logging.getLogger(__name__)


class ThemeName(BaseModel):
    name: str
    summary: str = ""


def average_linkage(sim: np.ndarray, threshold: float, max_size: int) -> list[list[int]]:
    """Zhlukovanie zdola nahor: vždy sa spoja dva najpodobnejšie zhluky (priemer podobností všetkých dvojíc),
    kým je podobnosť aspoň `threshold`; spojenie nad `max_size` sa preskočí. Vracia zoznamy indexov."""
    n = len(sim)
    s = np.array(sim, dtype=np.float64, copy=True)
    np.fill_diagonal(s, -np.inf)
    members = {i: [i] for i in range(n)}
    while len(members) > 1:
        k = int(np.argmax(s))
        a, b = divmod(k, n)
        if not np.isfinite(s[a, b]) or s[a, b] < threshold:
            break
        if len(members[a]) + len(members[b]) > max_size:
            s[a, b] = s[b, a] = -np.inf
            continue
        na, nb = len(members[a]), len(members[b])
        row = (s[a] * na + s[b] * nb) / (na + nb)  # priemerná väzba: vážený priemer riadkov
        row[np.isneginf(s[a]) | np.isneginf(s[b])] = -np.inf  # zakázané spojenia (veľkosť) ostanú zakázané
        s[a], s[:, a] = row, row
        s[a, a] = -np.inf
        s[b], s[:, b] = -np.inf, -np.inf
        members[a] += members.pop(b)
    return [sorted(m) for m in members.values()]


def medoid(sim: np.ndarray, idx: Sequence[int]) -> int:
    """Člen s najvyššou priemernou podobnosťou k ostatným (najtypickejšia hypotéza)."""
    sub = sim[np.ix_(idx, idx)]
    return int(idx[int(np.argmax(sub.sum(axis=1)))])


def jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def match_existing(new: list[set[int]], old: list[tuple[int, set[int]]], min_jaccard: float) -> dict[int, int]:
    """Index nového zhluku → ID starej témy (každá stará téma najviac raz, najlepšie zhody najskôr)."""
    pairs = sorted(((jaccard(n, o), i, tid) for i, n in enumerate(new) for tid, o in old), reverse=True)
    out: dict[int, int] = {}
    used: set[int] = set()
    for score, i, tid in pairs:
        if score < min_jaccard:
            break
        if i not in out and tid not in used:
            out[i] = tid
            used.add(tid)
    return out


def name_theme(cfg: Config, router, titles: list[str], theme_ref: int | None) -> ThemeName | None:
    listing = "\n".join(f"- {t}" for t in titles[:15])
    res = router.run_task(
        "theme_name",
        [{"role": "system", "content": THEME_NAME_SYSTEM.format(lang=cfg.pipeline.output_language)},
         {"role": "user", "content": f"<untrusted_document>\n{listing}\n</untrusted_document>"}],
        ThemeName, object_type="theme", object_id=theme_ref, max_tokens=300,
    )
    if not res.ok or not res.value.name.strip():
        return None
    return ThemeName(name=res.value.name.strip()[:120], summary=res.value.summary.strip()[:400])


def run_themes(cfg: Config, db: DB, llm: Any, embedder, deadline: Deadline) -> dict:
    stats: Counter = Counter()
    if embedder is None:
        return {"skipped": "embeddingy vypnuté"}
    t = cfg.themes
    hyps = [h for h in db.all_hypotheses() if h["status"] != "rejected"]
    if len(hyps) < t.min_size:
        return {"hypotheses": len(hyps)}
    matrix = embedder.hypothesis_matrix(hyps)  # uložené vektory; chýbajúce dopočíta embedding model
    sim = matrix @ matrix.T
    clusters = [c for c in average_linkage(sim, t.threshold, t.max_size) if len(c) >= t.min_size]
    clusters.sort(key=len, reverse=True)
    ids = [h["id"] for h in hyps]
    title = {h["id"]: h["title"] for h in hyps}
    new_sets = [{ids[i] for i in c} for c in clusters]
    old = {th["id"]: th for th in db.themes()}
    matched = match_existing(new_sets, [(tid, set(json.loads(th["members_json"]))) for tid, th in old.items()],
                             t.keep_jaccard)
    router = as_router(llm, cfg, db)
    out: list[dict[str, Any]] = []
    for i, (c, members) in enumerate(zip(clusters, new_sets)):
        center = ids[medoid(sim, c)]
        prev = old.get(matched.get(i, -1))
        theme = {"id": prev["id"] if prev else None, "members": members, "medoid_id": center}
        # Členovia od najtypickejšieho: model aj GUI vidia najprv jadro témy.
        ordered = [ids[j] for j in sorted(c, key=lambda j: -sim[j, c].mean())]
        if prev is not None and prev["name_method"] in ("manual", "model"):
            theme.update(name=prev["name"], summary=prev["summary"], name_method=prev["name_method"])
            stats["kept"] += 1
        else:
            named = None
            if stats["named"] < t.max_names_per_run and not deadline.expired():
                try:
                    named = name_theme(cfg, router, [title[m] for m in ordered], prev["id"] if prev else None)
                except Exception as exc:  # noqa: BLE001 – názov je iba pomôcka; použije sa medoid
                    log.warning("Názov témy zlyhal: %s", exc)
            if named is not None:
                theme.update(name=named.name, summary=named.summary, name_method="model")
                stats["named"] += 1
            else:
                theme.update(name=title[center], summary=None, name_method="medoid")
                stats["medoid_names"] += 1
        out.append(theme)
    db.save_themes(out)
    stats.update(themes=len(out), hypotheses_in_themes=sum(len(m) for m in new_sets), hypotheses=len(hyps),
                 new=sum(1 for o in out if o["id"] is None))
    return dict(stats)
