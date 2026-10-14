"""Benchmark rolí modelov (doctor --bench) a odhad, čo sa stihne za beh (R-ACC-5, R-ACC-6).

Pri Ollame sa časy berú z natívneho API (load_duration, prompt_eval_duration, eval_duration),
pri llama-serveri z poľa `timings`, inak z času a počtu tokenov (usage).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from .db import DB
from .models import ModelRouter

log = logging.getLogger(__name__)

# Typický počet tokenov na úlohu (vstup, výstup), kým nie sú merania v model_judgments.
TYPICAL_TOKENS: dict[str, tuple[int, int]] = {
    "triage": (1500, 80),
    "screen": (1000, 60),
    "extract": (2600, 600),
    "number_extract": (2200, 200),
    "entail": (1900, 90),
    "entail_gate": (1900, 90),
    "cluster_assign": (1300, 120),
    "merge_check": (300, 120),
    "critique": (3000, 700),
    "market_structure": (1500, 400),
    "org_mentions": (1500, 150),
    "person_mentions": (1500, 150),
    "segment_filter": (400, 200),
    "contact_assoc": (900, 150),
    "role_map": (1500, 500),
    "role_fit": (400, 80),
    "draft": (1600, 300),
    "draft_check": (1600, 150),
}
THINKING_TOKENS = 1100  # priemer nameraný pri Qwen3.6-27B (posúdenie s uvažovaním, 10. 10. 2026)

_FILLER = (
    "Obce a firmy na Slovensku riešia rôzne prevádzkové problémy, meškania dodávok, nedostatok ľudí, "
    "administratívnu záťaž a rastúce náklady. "
)


class _Ping(BaseModel):
    ok: bool
    word: str


@dataclass
class BenchResult:
    role: str
    model: str
    load_s: float | None = None
    prompt_tps: float | None = None
    gen_tps: float | None = None
    json_ok: bool | None = None
    tools_ok: bool | None = None
    note: str = ""


def _server_root(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/v1")


def _ollama(base_url: str) -> bool:
    try:
        return httpx.get(_server_root(base_url) + "/api/version", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


def bench_ollama(base_url: str, model: str, timeout: float) -> tuple[float, float, float]:
    """(načítanie s, prompt tok/s, generovanie tok/s) z natívneho API Ollamy; model sa najprv uvoľní."""
    root = _server_root(base_url)
    httpx.post(root + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=120)
    time.sleep(2)
    prompt = (_FILLER * 40)[:6000] + "\nZhrň text jednou vetou."
    r = httpx.post(
        root + "/api/chat",
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False, "think": False,
              "options": {"num_predict": 64}},
        timeout=timeout,
    ).json()
    load = (r.get("load_duration") or 0) / 1e9
    prompt_tps = r["prompt_eval_count"] / max(r["prompt_eval_duration"] / 1e9, 1e-9)
    gen_tps = r["eval_count"] / max(r["eval_duration"] / 1e9, 1e-9)
    return load, prompt_tps, gen_tps


def bench_openai(client: Any) -> tuple[float | None, float | None, float | None]:
    """Server bez natívneho API: časy z poľa timings (llama-server), inak z času a usage."""
    start = time.monotonic()
    client.chat([{"role": "user", "content": "Ahoj."}], max_tokens=1)
    load = time.monotonic() - start
    prompt = (_FILLER * 40)[:6000] + "\nZhrň text jednou vetou."
    client.chat([{"role": "user", "content": prompt}], max_tokens=64)
    info = client.last
    timings = info.timings or {}
    if timings.get("prompt_per_second") and timings.get("predicted_per_second"):
        return load, float(timings["prompt_per_second"]), float(timings["predicted_per_second"])
    if info.latency_s and info.completion_tokens:
        return load, None, info.completion_tokens / info.latency_s
    return load, None, None


def bench_role(router: ModelRouter, role: str, tools: list[dict[str, Any]] | None = None) -> BenchResult:
    role_cfg = router.role_config(role)
    result = BenchResult(role=role, model=role_cfg.model)
    base_url = role_cfg.base_url or (router.models.base_url if router.models else router.cfg.llm.base_url)
    if role == "embed":
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key="local", timeout=role_cfg.timeout_s)
        start = time.monotonic()
        client.embeddings.create(model=role_cfg.model, input=[_FILLER] * 16)
        result.note = f"16 textov za {time.monotonic() - start:.1f} s"
        return result
    try:
        if _ollama(base_url):
            llm = router.client(role)
            llm.make_exclusive()  # meria sa tak, ako pri behu: na GPU iba tento model
            result.load_s, result.prompt_tps, result.gen_tps = bench_ollama(base_url, role_cfg.model, role_cfg.timeout_s)
            swap = getattr(llm, "last_swap", None)  # make_exclusive model už načítal – čas načítania je z neho
            if isinstance(swap, dict) and swap.get("načítanie_s"):
                result.load_s = swap["načítanie_s"]
        else:
            result.load_s, result.prompt_tps, result.gen_tps = bench_openai(router.client(role))
    except Exception as exc:  # noqa: BLE001 – benchmark nesmie zhodiť doctor
        result.note = f"meranie zlyhalo: {exc}"
    client = router.client(role)
    try:
        client.chat_json([{"role": "user", "content": 'Return the JSON {"ok": true, "word": "radar"}.'}], _Ping, max_tokens=200)
        result.json_ok = True
    except Exception as exc:  # noqa: BLE001
        result.json_ok = False
        result.note += f" JSON: {exc}"
    if tools:
        try:
            msg = client.chat_tools([{"role": "user", "content": "Call the finish tool with the summary 'test'."}], tools)
            result.tools_ok = bool(getattr(msg, "tool_calls", None))
        except Exception as exc:  # noqa: BLE001
            result.tools_ok = False
            result.note += f" nástroje: {exc}"
    return result


def save_bench(db: DB, r: BenchResult) -> None:
    db.add_bench(r.role, r.model, r.load_s, r.prompt_tps, r.gen_tps, r.json_ok, r.tools_ok, r.note.strip())


# ------------------------------------------------------------------ odhad kapacity (R-ACC-6)
def seconds_per_call(db: DB, router: ModelRouter, task: str) -> float | None:
    """Odhad času jedného volania úlohy: priemer z model_judgments, inak z benchmarku a typických tokenov."""
    route = router.route(task)
    stats = db.task_latency_stats().get((task, route.model))
    if stats and stats["n"] >= 3:
        return float(stats["latency"]) * route.k
    bench = db.latest_bench().get(route.role)
    if bench is None or not bench["gen_tps"]:
        return None
    pt, ct = TYPICAL_TOKENS.get(task, (1500, 200))
    if route.thinking:
        ct += THINKING_TOKENS
    prompt_s = pt / bench["prompt_tps"] if bench["prompt_tps"] else 0.0
    return (prompt_s + ct / bench["gen_tps"]) * route.k


def capacity_plan(db: DB, router: ModelRouter, workload: dict[str, int], minutes: float) -> list[dict[str, Any]]:
    """Pre každú úlohu: počet položiek, odhad času na položku a spolu. Poradie = priorita."""
    plan = []
    for task, items in workload.items():
        per = seconds_per_call(db, router, task)
        plan.append({"task": task, "items": items, "per_item_s": per, "total_min": None if per is None else per * items / 60})
    known = sum(p["total_min"] or 0 for p in plan)
    for p in plan:
        p["fits"] = known <= minutes
    return plan
