"""Odhady časov pre dashboard: kedy skončí bežiaca úloha, kedy začnú a skončia úlohy v poradí, typické trvanie
akcie. Úlohy často skončia skôr ako limit (napr. akcia bez práce) – odhad je limit × medián skutočného pomeru
trvanie / limit z histórie rovnakej akcie (najviac 1). Časy sa zobrazujú v miestnom čase."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def local(text_or_dt: str | datetime | None, now: datetime | None = None) -> str:
    """Miestny čas „HH:MM“, iný deň „d. m. HH:MM“."""
    t = _ts(text_or_dt) if isinstance(text_or_dt, str) or text_or_dt is None else text_or_dt
    if t is None:
        return "–"
    lt = t.astimezone()
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    return lt.strftime("%H:%M") if lt.date() == today else f"{lt.day}. {lt.month}. {lt.strftime('%H:%M')}"


def history(db, kind: str, limit: int = 12) -> list[tuple[float, float | None]]:
    """(skutočné minúty, limit v minútach) posledných dokončených úloh danej akcie."""
    rows = db._all("SELECT started_at, finished_at, args_json FROM jobs WHERE kind = ? AND status = 'done' "
                   "AND started_at IS NOT NULL AND finished_at IS NOT NULL ORDER BY id DESC LIMIT ?", (kind, limit))
    out = []
    for r in rows:
        mins = (_ts(r["finished_at"]) - _ts(r["started_at"])).total_seconds() / 60
        lim = (json.loads(r["args_json"] or "{}") or {}).get("minutes")
        out.append((mins, float(lim) if lim else None))
    return out


def typical_minutes(db, kind: str) -> float | None:
    hist = [m for m, _ in history(db, kind) if m >= 0.2]
    return round(median(hist), 1) if hist else None


def expected_minutes(db, kind: str, limit: float | None) -> float:
    """Očakávané trvanie úlohy s daným limitom podľa histórie (bez histórie = limit)."""
    ratios = [min(1.0, m / lim) for m, lim in history(db, kind) if lim]
    if limit is None:
        return typical_minutes(db, kind) or 30.0
    return float(limit) * (median(ratios) if ratios else 1.0)


def schedule(db, now: datetime | None = None) -> list[dict[str, Any]]:
    """Bežiace a čakajúce úlohy s odhadom začiatku a konca (UTC datetime)."""
    now = now or datetime.now(timezone.utc)
    out, cursor = [], now
    for j in db.active_jobs():
        lim = (json.loads(j["args_json"] or "{}") or {}).get("minutes")
        start = _ts(j["started_at"]) or now
        latest = start + timedelta(minutes=float(lim)) if lim else None
        exp = start + timedelta(minutes=expected_minutes(db, j["kind"], lim))
        if exp < now + timedelta(minutes=1):  # beží dlhšie ako zvyčajne: aspoň minúta, najviac limit
            exp = min(latest or now + timedelta(minutes=5), now + timedelta(minutes=5)) if latest and latest > now else \
                now + timedelta(minutes=1)
        out.append({"job": j, "začiatok": start, "koniec_odhad": exp, "koniec_najneskôr": latest})
        cursor = max(cursor, exp)
    for j in db.queued_jobs():
        lim = (json.loads(j["args_json"] or "{}") or {}).get("minutes")
        exp = expected_minutes(db, j["kind"], lim)
        out.append({"job": j, "začiatok": cursor, "koniec_odhad": cursor + timedelta(minutes=exp),
                    "koniec_najneskôr": None})
        cursor += timedelta(minutes=exp)
    return out
