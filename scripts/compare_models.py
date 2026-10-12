"""Porovnanie modelov na posúdení citátov (entail): posledných N rozhodnutí referenčného modelu znova posúdi iný model.

Zhoda nie je presnosť (referencia môže byť tiež chybná) – presnosť merá iba eval s gold labelmi z Kontroly. Veľký rozdiel
je však varovanie pred prepnutím modelu. Výsledky sa do DB neukladajú (priamy klient, bez záznamu rozhodnutí).

Použitie:  python scripts/compare_models.py --reference radar-qwen3.6-27b --candidate radar-qwen3.5-9b --n 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.cli import App, default_config_path  # noqa: E402
from radar.config import load_config  # noqa: E402
from radar.verify import Entailment, claim_context, entail_messages, quote_score  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default="radar-qwen3.6-27b")
    ap.add_argument("--candidate", default="radar-qwen3.5-9b")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--out", default=None, help="JSON s výsledkom (voliteľné)")
    args = ap.parse_args()
    cfg = load_config(default_config_path())
    app = App(cfg)
    db = app.db
    rows = db._all(
        """SELECT j.object_id AS claim_id, j.verdict, j.created_at FROM model_judgments j
           WHERE j.task = 'entail' AND j.model = ? AND j.verdict IN ('supported', 'partial', 'unsupported')
           ORDER BY j.id DESC LIMIT ?""", (args.reference, args.n))
    client = app.router.client("fast")
    client.cfg.model = args.candidate  # priamy klient: rozhodnutia sa neukladajú do DB
    pairs, t0 = [], time.time()
    for r in rows:
        claim = db.get_claim(r["claim_id"])
        doc = db.get_document(claim["document_id"]) if claim is not None else None
        if doc is None:
            continue
        context = claim_context(cfg, doc["text"], claim["chunk_index"])
        if quote_score(claim["quote"], context) < cfg.pipeline.quote_match_threshold and len(doc["text"]) <= 60_000:
            context = doc["text"]
        try:
            res = client.chat_json(entail_messages(cfg, claim, context), Entailment, max_tokens=400)
            got = res.verdict
        except Exception as exc:  # noqa: BLE001
            got = f"chyba: {type(exc).__name__}"
        pairs.append((r["verdict"], got))
    agree = sum(1 for a, b in pairs if a == b)
    matrix = Counter(f"{a} → {b}" for a, b in pairs)
    result = {"referencia": args.reference, "kandidát": args.candidate, "posúdení": len(pairs), "zhoda": agree,
              "zhoda_podiel": round(agree / len(pairs), 3) if pairs else None,
              "prísnejší_kandidát": sum(1 for a, b in pairs if a == "supported" and b in ("partial", "unsupported")),
              "benevolentnejší_kandidát": sum(1 for a, b in pairs if a in ("partial", "unsupported") and b == "supported"),
              "matica": dict(matrix.most_common()), "sekúnd": round(time.time() - t0)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
