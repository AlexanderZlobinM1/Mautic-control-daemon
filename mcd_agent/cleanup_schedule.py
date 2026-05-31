from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable


def cron_field_matches(field: str, value: int) -> bool:
    raw = str(field or "").strip()
    if raw == "*":
        return True
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = max(1, int(step_s))
            except Exception:
                return False
        if base == "*":
            return value % step == 0
        if "-" in base:
            try:
                start, end = [int(x) for x in base.split("-", 1)]
            except Exception:
                continue
            if start <= value <= end and (value - start) % step == 0:
                return True
            continue
        try:
            if int(base) == value:
                return True
        except Exception:
            continue
    return False


def cron_expr_due(expr: str, now_dt: datetime) -> bool:
    parts = str(expr or "").strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    cron_dow = (now_dt.weekday() + 1) % 7
    return (
        cron_field_matches(minute, now_dt.minute)
        and cron_field_matches(hour, now_dt.hour)
        and cron_field_matches(dom, now_dt.day)
        and cron_field_matches(month, now_dt.month)
        and cron_field_matches(dow, cron_dow)
    )


def cron_expr_window_due(expr: str, now_dt: datetime, window_min: int) -> bool:
    return bool(cron_expr_window_key(expr, now_dt, window_min))


def parse_hhmm(value: object, default: str) -> tuple[int, int]:
    raw = str(value or default).strip()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not m:
        raw = str(default).strip()
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not m:
        return 22, 0
    return int(m.group(1)), int(m.group(2))


def window_minutes(start: object, end: object) -> int:
    sh, sm = parse_hhmm(start, "22:00")
    eh, em = parse_hhmm(end, "09:00")
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if end_min <= start_min:
        end_min += 1440
    return max(1, min(1440, end_min - start_min))


def in_daily_window(now_local: datetime, quiet_hour: int, quiet_window_min: int) -> bool:
    start_hour = max(0, min(23, int(quiet_hour)))
    window_min = max(1, int(quiet_window_min))
    start_today = now_local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_today = start_today + timedelta(minutes=window_min)
    if start_today <= now_local < end_today:
        return True
    if end_today.date() != start_today.date():
        start_prev = start_today - timedelta(days=1)
        end_prev = start_prev + timedelta(minutes=window_min)
        if start_prev <= now_local < end_prev:
            return True
    return False


def in_hhmm_window(now_local: datetime, start: object, end: object) -> bool:
    sh, sm = parse_hhmm(start, "22:00")
    window_min = window_minutes(start, end)
    start_today = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_today = start_today + timedelta(minutes=window_min)
    if start_today <= now_local < end_today:
        return True
    if end_today.date() != start_today.date():
        start_prev = start_today - timedelta(days=1)
        end_prev = start_prev + timedelta(minutes=window_min)
        if start_prev <= now_local < end_prev:
            return True
    return False


def hhmm_window_key(now_local: datetime, start: object, end: object) -> str:
    sh, sm = parse_hhmm(start, "22:00")
    window_min = window_minutes(start, end)
    start_today = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if start_today <= now_local < start_today + timedelta(minutes=window_min):
        return start_today.isoformat(timespec="minutes")
    start_prev = start_today - timedelta(days=1)
    return start_prev.isoformat(timespec="minutes")


def cron_expr_window_key(expr: str, now_dt: datetime, window_min: int) -> str:
    window = max(1, min(1440, int(window_min or 1)))
    current = now_dt.replace(second=0, microsecond=0)
    for offset_min in range(window):
        candidate = current - timedelta(minutes=offset_min)
        if cron_expr_due(expr, candidate):
            return candidate.isoformat(timespec="minutes")
    return ""


def cleanup_session_key(
    *,
    schedule_type: str,
    now_local: datetime,
    now_epoch: float,
    interval_sec: int,
    cron_expr: str,
    window_min: int,
    window_start: str,
    window_end: str,
) -> tuple[bool, str]:
    """Return whether cleanup is allowed now and the current drain-session key."""
    mode = str(schedule_type or "interval").strip().lower()
    if mode in {"nightly", "nightly_window", "window"}:
        if not in_hhmm_window(now_local, window_start, window_end):
            return False, ""
        return True, hhmm_window_key(now_local, window_start, window_end)
    if mode in {"cron", "cron_window"}:
        expr = str(cron_expr or "").strip()
        if not expr:
            return False, ""
        key = cron_expr_window_key(expr, now_local.replace(second=0, microsecond=0), window_min)
        return bool(key), key
    interval = max(60, int(interval_sec or 60))
    return True, f"interval:{int(float(now_epoch) // interval)}"


def select_fair_cleanup_task(task_names: Iterable[str], previous_index: int) -> tuple[str, int]:
    """Select the next due cleanup task using a stable round-robin cursor."""
    names = [str(name) for name in task_names if str(name)]
    if not names:
        return "", previous_index
    if len(names) == 1:
        return names[0], previous_index
    next_index = (int(previous_index or 0) + 1) % len(names)
    return names[next_index], next_index
