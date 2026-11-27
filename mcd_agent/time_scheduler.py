"""Durable wall-clock scheduling for independent recurring MCD tasks.

The scheduler owns *when* a recurring task is due.  Execution admission still
belongs to MCD's existing task supervisor, which owns process identity, slots,
receipts and recovery.  Keeping those concerns separate prevents a long
running command from moving the next due time of an unrelated task.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping


STATE_SCHEMA = "mcd-wall-clock-schedule-v1"
STATE_PREFIX = "wall_clock_schedule:"
MIN_INTERVAL_SEC = 1
ANCHOR_QUANTUM_SEC = 60


@dataclass(frozen=True)
class TimeTaskSpec:
    """One independently scheduled recurring task."""

    key: str
    interval_sec: int
    enabled: bool
    dispatch: Callable[[float, Mapping[str, Any]], Mapping[str, Any]]


def state_key(task_key: str) -> str:
    digest = hashlib.sha256(str(task_key).encode("utf-8", errors="replace")).hexdigest()
    return f"{STATE_PREFIX}{digest}"


def wall_clock_anchor(now_ts: float, *, quantum_sec: int = ANCHOR_QUANTUM_SEC) -> float:
    """Return the current wall-clock quantum used as an enable anchor."""

    quantum = max(1, int(quantum_sec or ANCHOR_QUANTUM_SEC))
    return float(math.floor(float(now_ts) / quantum) * quantum)


def interval_from_schedule(
    schedule: Mapping[str, Any] | None,
    values: Mapping[str, Any] | None = None,
) -> int:
    schedule = schedule if isinstance(schedule, Mapping) else {}
    values = values if isinstance(values, Mapping) else {}
    field = str(schedule.get("interval_field", "") or "")
    try:
        raw = values.get(field, 60) if field else schedule.get("interval_sec", 60)
        return max(MIN_INTERVAL_SEC, int(raw or 60))
    except (TypeError, ValueError):
        return 60


def _new_state(*, task_key: str, interval_sec: int, now_ts: float) -> dict[str, Any]:
    anchor = wall_clock_anchor(now_ts)
    return {
        "schema": STATE_SCHEMA,
        "task_key": str(task_key),
        "enabled": True,
        "interval_sec": int(interval_sec),
        "anchor_at": anchor,
        "last_due": None,
        "last_dispatch_at": None,
        "next_due": anchor,
        "dispatch_count": 0,
        "deferred_count": 0,
        "missed_count": 0,
        "last_receipt": {},
        "updated_at": float(now_ts),
    }


def reconcile_state(
    raw: Mapping[str, Any] | None,
    *,
    task_key: str,
    interval_sec: int,
    enabled: bool,
    now_ts: float,
) -> dict[str, Any]:
    """Reconcile durable state without deriving due time from completion.

    A disable→enable transition and an interval change deliberately create a
    new anchor.  A restart and normal re-read preserve the existing anchor and
    next_due values.
    """

    interval = max(MIN_INTERVAL_SEC, int(interval_sec or MIN_INTERVAL_SEC))
    previous = dict(raw) if isinstance(raw, Mapping) else {}
    valid = previous.get("schema") == STATE_SCHEMA and previous.get("task_key") == str(task_key)
    was_enabled = bool(previous.get("enabled", False)) if valid else False
    previous_interval = int(previous.get("interval_sec", 0) or 0) if valid else 0
    if not enabled:
        if not valid:
            previous = _new_state(task_key=task_key, interval_sec=interval, now_ts=now_ts)
        previous.update(
            {
                "schema": STATE_SCHEMA,
                "task_key": str(task_key),
                "enabled": False,
                "interval_sec": interval,
                "next_due": None,
            }
        )
        return previous
    if not valid or not was_enabled or previous_interval != interval:
        return _new_state(task_key=task_key, interval_sec=interval, now_ts=now_ts)
    previous["enabled"] = True
    previous["interval_sec"] = interval
    previous.setdefault("last_receipt", {})
    previous.setdefault("dispatch_count", 0)
    previous.setdefault("deferred_count", 0)
    previous.setdefault("missed_count", 0)
    return previous


def is_due(state: Mapping[str, Any], now_ts: float, *, tolerance_sec: float = 0.25) -> bool:
    if not bool(state.get("enabled", False)):
        return False
    next_due = state.get("next_due")
    try:
        return next_due is not None and float(now_ts) + float(tolerance_sec) >= float(next_due)
    except (TypeError, ValueError):
        return True


def record_deferred(
    state: Mapping[str, Any],
    *,
    due_ts: float,
    now_ts: float,
    reason: str,
) -> dict[str, Any]:
    next_state = dict(state)
    next_state["deferred_count"] = int(next_state.get("deferred_count", 0) or 0) + 1
    next_state["last_receipt"] = {
        "event": "deferred",
        "status": "deferred",
        "reason": str(reason or "admission_unavailable"),
        "due_at": float(due_ts),
        "observed_at": float(now_ts),
    }
    next_state["updated_at"] = float(now_ts)
    return next_state


def record_terminal(
    state: Mapping[str, Any],
    *,
    due_ts: float,
    now_ts: float,
    status: str,
    reason: str = "",
) -> dict[str, Any]:
    next_state = dict(state)
    interval = max(MIN_INTERVAL_SEC, int(next_state.get("interval_sec", 60) or 60))
    anchor = float(next_state.get("anchor_at") or wall_clock_anchor(now_ts))
    due = float(due_ts)
    periods = max(1, int(math.floor(max(0.0, float(now_ts) - due) / interval)) + 1)
    next_due = due + float(periods * interval)
    next_state["last_due"] = due
    next_state["last_dispatch_at"] = float(now_ts)
    next_state["next_due"] = max(next_due, anchor)
    next_state["dispatch_count"] = int(next_state.get("dispatch_count", 0) or 0) + 1
    next_state["missed_count"] = int(next_state.get("missed_count", 0) or 0) + max(0, periods - 1)
    next_state["last_receipt"] = {
        "event": "dispatch",
        "status": str(status or "skipped"),
        "reason": str(reason or ""),
        "due_at": due,
        "dispatched_at": float(now_ts),
        "next_due": next_state["next_due"],
        "missed_periods": max(0, periods - 1),
    }
    next_state["updated_at"] = float(now_ts)
    return next_state


class WallClockDispatcher:
    """Run independent due decisions against a durable TaskStore-like API."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def tick(self, specs: list[TimeTaskSpec], *, now_ts: float | None = None) -> list[dict[str, Any]]:
        now = float(time.time() if now_ts is None else now_ts)
        outcomes: list[dict[str, Any]] = []
        for spec in specs:
            interval = max(MIN_INTERVAL_SEC, int(spec.interval_sec or MIN_INTERVAL_SEC))
            key = state_key(spec.key)
            raw = self.store.get_runtime_sync(key) or {}
            state = reconcile_state(
                raw,
                task_key=spec.key,
                interval_sec=interval,
                enabled=bool(spec.enabled),
                now_ts=now,
            )
            if not spec.enabled:
                if state != raw:
                    self.store.put_runtime_sync(key, state)
                continue
            if not is_due(state, now):
                if state != raw:
                    self.store.put_runtime_sync(key, state)
                continue
            due_ts = float(state.get("next_due") or now)
            try:
                result = dict(spec.dispatch(due_ts, state) or {})
            except Exception as exc:  # the task receipt must survive callback failures
                result = {"status": "deferred", "reason": f"dispatch_exception:{exc}"}
            status = str(result.get("status") or "deferred").strip().lower()
            if status in {"deferred", "busy", "missed"}:
                next_state = record_deferred(
                    state,
                    due_ts=due_ts,
                    now_ts=now,
                    reason=str(result.get("reason") or "admission_unavailable"),
                )
            else:
                next_state = record_terminal(
                    state,
                    due_ts=due_ts,
                    now_ts=now,
                    status=status,
                    reason=str(result.get("reason") or ""),
                )
            self.store.put_runtime_sync(key, next_state)
            outcomes.append({"key": spec.key, **dict(next_state.get("last_receipt") or {})})
        return outcomes
