"""Úlohy na pozadí pre GUI (Správca akcií): tlačidlo spustí `python -m radar workflow …` ako samostatný proces.

- naraz beží najviac jedna úloha (na 8 GB VRAM sa zmestí iba jeden veľký model); ďalšie sa dajú
  zaradiť do poradia (stav „queued“) – po skončení úlohy sa spustí ďalšia, alebo bežiacu zastaviť a hneď
  spustiť inú,
- proces beží vo vlastnej skupine procesov: prekreslenie alebo zastavenie GUI ho neukončí,
- stav, priebeh a výsledok sú v tabuľke jobs, výstup v logs/jobs/*.log (maskované e-maily a telefóny),
- nič sa neodosiela: workflowy iba čítajú verejné zdroje a zapisujú do lokálnej databázy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config
from .db import DB
from .privacy import mask_text
from .util import utcnow

ROOT = Path(__file__).resolve().parent.parent
STILL_ACTIVE = 259  # GetExitCodeProcess vo Windows: proces ešte beží


class JobBusy(RuntimeError):
    """Iná úloha ešte beží."""


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":  # os.kill(pid, 0) by vo Windows poslal Ctrl+C
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def job_command(config_path: str | Path, kind: str, minutes: float, job_id: int,
                hypothesis_ids: list[int] | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "radar", "--config", str(config_path), "workflow", kind,
           "--minutes", f"{minutes:g}", "--job", str(job_id)]
    if hypothesis_ids:
        cmd += ["--hypotheses", ",".join(str(i) for i in hypothesis_ids)]
    return cmd


def refresh_jobs(db: DB, alive: Callable[[int | None], bool] = pid_alive) -> None:
    """Úloha, ktorej proces už nebeží a nezapísala výsledok, je prerušená (napr. reštart počítača)."""
    for job in db.active_jobs():
        if job["pid"] and not alive(job["pid"]):
            db.update_job(job["id"], status="interrupted", finished_at=utcnow())
        elif not job["pid"] and job["started_at"] < _minutes_ago(2):
            db.update_job(job["id"], status="interrupted", finished_at=utcnow())


def _minutes_ago(minutes: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _spawn(cfg: Config, db: DB, job_id: int, popen: Callable[..., Any]) -> int:
    """Spustí proces pre existujúci riadok úlohy (nový alebo z poradia)."""
    job = db.get_job(job_id)
    args = json.loads(job["args_json"] or "{}")
    log_dir = cfg.path(cfg.workflows.jobs_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job-{job_id}-{job['kind']}.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = 0
    if os.name == "nt":  # bez okna konzoly a vo vlastnej skupine (Ctrl+C v termináli GUI ju nezastaví)
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = job_command(args.get("config") or Path(cfg.config_dir) / "config.yaml", job["kind"], args.get("minutes", 60),
                      job_id, args.get("hypotheses") or None)
    with open(log_path, "ab") as out:
        proc = popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                     creationflags=flags)
    db.update_job(job_id, pid=proc.pid, status="running", log_path=str(log_path))
    return job_id


def start_job(cfg: Config, db: DB, config_path: str | Path, kind: str, minutes: float,
              hypothesis_ids: list[int] | None = None, popen: Callable[..., Any] = subprocess.Popen,
              alive: Callable[[int | None], bool] = pid_alive, mode: str = "start",
              run: Callable[..., Any] = subprocess.run) -> int:
    """mode: start (iba ak nič nebeží), queue (zaradí do poradia, ak niečo beží), replace (zastaví bežiacu a spustí)."""
    if mode not in ("start", "queue", "replace"):
        raise ValueError(f"Neznámy režim {mode!r}")
    refresh_jobs(db, alive)
    running = db.active_jobs()
    if running and mode == "replace":
        for job in running:
            stop_job(db, job["id"], run=run)
        running = []
    args = {"minutes": minutes, "hypotheses": hypothesis_ids or [], "config": str(config_path)}
    if running:
        if mode == "queue":
            return db.create_job(kind, args, status="queued")
        raise JobBusy(f"Už beží úloha #{running[0]['id']} ({running[0]['kind']}). Zaraď ju do poradia, "
                      "alebo bežiacu zastav.")
    return _spawn(cfg, db, db.create_job(kind, args), popen)


def start_next(cfg: Config, db: DB, popen: Callable[..., Any] = subprocess.Popen,
               alive: Callable[[int | None], bool] = pid_alive) -> int | None:
    """Ak nič nebeží, spustí najstaršiu úlohu z poradia."""
    refresh_jobs(db, alive)
    if db.active_jobs():
        return None
    queued = db.queued_jobs()
    return _spawn(cfg, db, queued[0]["id"], popen) if queued else None


def queue_stalled(db: DB, alive: Callable[[int | None], bool] = pid_alive) -> bool:
    """Poradie stojí: nič nebeží, ale v poradí niečo čaká (ďalšiu úlohu spúšťa až koniec bežiacej, takže po
    reštarte alebo prerušení počítača sa poradie samo nepohne)."""
    refresh_jobs(db, alive)
    return not db.active_jobs() and bool(db.queued_jobs())


def interrupted_jobs(db: DB, limit: int = 25) -> list:
    """Prerušené úlohy, po ktorých už rovnaká akcia nebežala (kandidáti na „Spustiť znova“)."""
    out, later_done = [], set()
    for job in db.recent_jobs(limit):  # od najnovšej
        if job["status"] == "interrupted" and job["kind"] not in later_done:
            out.append(job)
        later_done.add(job["kind"])  # novšia úloha tej istej akcie (aj zastavená) → staršiu neponúkať
    return out


def restart_job(cfg: Config, db: DB, config_path: str | Path, job_id: int, popen: Callable[..., Any] = subprocess.Popen,
                alive: Callable[[int | None], bool] = pid_alive) -> int:
    """Rovnaká akcia s rovnakým limitom a hypotézami ako prerušená úloha (ak niečo beží, zaradí sa do poradia).
    Rozpracovaný stav je v databáze, takže akcia pokračuje tam, kde prestala (nie od začiatku)."""
    job = db.get_job(job_id)
    if job is None:
        raise ValueError("Úloha neexistuje.")
    args = json.loads(job["args_json"] or "{}")
    return start_job(cfg, db, config_path, job["kind"], args.get("minutes") or 60, args.get("hypotheses") or None,
                     popen=popen, alive=alive, mode="queue")


def stop_job(db: DB, job_id: int, run: Callable[..., Any] = subprocess.run) -> bool:
    job = db.get_job(job_id)
    if job is None:
        return False
    if job["status"] == "queued":
        db.update_job(job_id, status="cancelled", finished_at=utcnow())
        return True
    if job["status"] not in ("starting", "running"):
        return False
    if job["pid"]:
        if os.name == "nt":
            run(["taskkill", "/PID", str(job["pid"]), "/T", "/F"], capture_output=True, check=False)
        else:
            import signal

            try:
                os.kill(int(job["pid"]), signal.SIGTERM)
            except ProcessLookupError:
                pass
    db.update_job(job_id, status="stopped", finished_at=utcnow())
    return True


def stop_all(db: DB, run: Callable[..., Any] = subprocess.run) -> int:
    """Zastaví bežiacu úlohu a zruší celé poradie."""
    jobs = list(db.active_jobs()) + list(db.queued_jobs())
    return sum(stop_job(db, job["id"], run=run) for job in jobs)


def log_tail(path: str | None, lines: int = 40) -> str:
    """Koniec výstupu úlohy na zobrazenie v GUI (pre istotu ešte raz maskovaný)."""
    if not path or not Path(path).exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        text = fh.read().decode("utf-8", errors="replace")
    return mask_text("\n".join(text.splitlines()[-lines:]))


# ---------------------------------------------------------------- stav GPU (Ollama)
def _ollama_root(cfg: Config) -> str:
    base = cfg.models.base_url if cfg.models is not None else cfg.llm.base_url
    return base.rstrip("/").removesuffix("/v1")


def gpu_status(cfg: Config, get: Callable[..., Any] = httpx.get) -> list[dict[str, Any]] | None:
    """Modely načítané v Ollame (aj z iných aplikácií, napr. astra). None = server nedostupný alebo nie je Ollama."""
    try:
        models = get(_ollama_root(cfg) + "/api/ps", timeout=3).json().get("models", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    out = []
    for m in models:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        out.append({"model": m.get("name") or m.get("model"), "vram_gb": round(vram / 1e9, 1),
                    "na_gpu": f"{vram / size:.0%}" if size else "?", "do": (m.get("expires_at") or "")[11:16]})
    return out


def unload_gpu(cfg: Config, get: Callable[..., Any] = httpx.get, post: Callable[..., Any] = httpx.post) -> list[str]:
    """Uvoľní z VRAM všetky modely Ollamy (keep_alive 0); embedding na CPU ostane."""
    unloaded = []
    for m in gpu_status(cfg, get) or []:
        if not m["vram_gb"]:
            continue
        try:
            post(_ollama_root(cfg) + "/api/generate", json={"model": m["model"], "keep_alive": 0}, timeout=60)
            unloaded.append(m["model"])
        except httpx.HTTPError:
            continue
    return unloaded


def ollama_models(cfg: Config, get: Callable[..., Any] = httpx.get) -> list[dict[str, Any]] | None:
    """Modely nainštalované v Ollame (na výber v GUI). None = server nedostupný."""
    try:
        models = get(_ollama_root(cfg) + "/api/tags", timeout=3).json().get("models", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    out = []
    for m in models:
        details = m.get("details") or {}
        out.append({"model": m.get("name") or m.get("model"), "gb": round((m.get("size") or 0) / 1e9, 1),
                    "parametre": details.get("parameter_size", ""), "kvantizácia": details.get("quantization_level", "")})
    return sorted(out, key=lambda m: m["model"] or "")
