"""Beh Brány 0 pre vybrané hypotézy po krokoch (organizácie → ľudia → poradie → návrhy) s časovým limitom.

Použitie (z priečinka projektu):
  .venv\\Scripts\\python scripts\\gate0_run.py --minutes 300 [--skip-orgs 4] [--hypotheses 1 4 11]
Bez --hypotheses berie hypotézy s people_enabled. Každý krok sa zapíše do tabuľky runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.cli import App, setup_logging  # noqa: E402
from radar.commands import stage_drafts, stage_orgs, stage_people, stage_people_rank, stage_prepare_next  # noqa: E402
from radar.config import load_config  # noqa: E402
from radar.util import Deadline  # noqa: E402

log = logging.getLogger("radar")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=300)
    parser.add_argument("--hypotheses", type=int, nargs="*")
    parser.add_argument("--skip-orgs", type=int, nargs="*", default=[], help="Hypotézy, ktoré už majú organizácie.")
    parser.add_argument("--no-drafts", action="store_true")
    parser.add_argument("--recollect", type=int, nargs="*", default=[],
                        help="Hypotézy, pre ktoré sa ľudia zozbierajú z webov organizácií znova.")
    parser.add_argument("--refresh-drafts", action="store_true",
                        help="Neupravené návrhy znova zložiť a skontrolovať (bez nového generovania).")
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    setup_logging(cfg.logs_dir, False)
    app = App(cfg)
    ids = args.hypotheses or [h["id"] for h in app.db.people_enabled_hypotheses()]
    for hid in args.recollect:
        for org in app.db.orgs_for_hypothesis(hid):
            app.db.set_org_hypothesis(org["id"], hid, status="website_checked")
    if args.refresh_drafts:
        from collections import Counter

        from radar.outreach import refresh_draft

        refreshed = Counter(refresh_draft(cfg, app.db, app.router, o["id"]) for o in app.db.outreach_list()
                            if o["status"] in ("draft", "needs_edit"))
        log.info("Prepočítané návrhy: %s", dict(refreshed))
    deadline = Deadline(args.minutes)
    # Presný model najprv pripraví filtre segmentu a mapy rolí (raz na hypotézu), potom rýchly model
    # a web (organizácie, ľudia), nakoniec znova presný (poradie, návrhy) – dve výmeny modelu.
    org_ids = [i for i in ids if i not in args.skip_orgs]
    steps = [("prepare-next", lambda dl: stage_prepare_next(app, dl))]
    if org_ids:  # prázdny zoznam by v run_orgs znamenal „všetky vybrané hypotézy“
        steps.append(("orgs", lambda dl: stage_orgs(app, dl, org_ids)))
    steps += [("people", lambda dl: stage_people(app, dl, ids)),
             ("people-rank", lambda dl: stage_people_rank(app, dl, ids))]
    if not args.no_drafts:
        steps.append(("drafts", lambda dl: stage_drafts(app, dl, ids)))
    shares = {"prepare-next": 0.3, "orgs": 0.35, "people": 0.4, "people-rank": 0.6, "drafts": 1.0}
    for name, fn in steps:
        if deadline.expired():
            break
        run_id = app.db.start_run(name)
        log.info("=== %s (hypotézy %s) ===", name, ids)
        try:
            stats = fn(deadline.sub(deadline.remaining_minutes() * shares[name]))
        except Exception:
            log.exception("Krok %s zlyhal", name)
            stats = {"error": True}
        app.db.finish_run(run_id, stats)
        log.info("%s hotovo: %s", name, json.dumps(stats, ensure_ascii=False))
    app.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
