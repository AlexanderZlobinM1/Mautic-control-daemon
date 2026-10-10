"""Canonical, bounded evidence for segment admission and static lanes.

The scheduler persists the authoritative ordinary-cycle and static-lane
payloads in ``TaskStore.runtime_sync``.  This module turns those payloads into
one monitor-safe receipt without exposing the source fingerprints or allowing
malformed runtime state to leak into the MCC monitor channel.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA = "mcd-segment-admission-receipt-v1"
ORDINARY_SCHEMA = "mcd-segment-cycle-v1"
STATIC_SCHEMA = "mcd-static-segment-lanes-v1"
LANES = ("super_priority", "priority")


def _ids(values: Iterable[object] | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in values or ():
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return float(default)


def _integer(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return int(default)


def _queue(items: list[int], *occupied: Iterable[object] | None) -> list[int]:
    occupied_ids: set[int] = set()
    for values in occupied:
        occupied_ids.update(_ids(values))
    return [sid for sid in items if sid not in occupied_ids]


def _ordinary_receipt(raw: Mapping[str, Any] | None) -> dict[str, object]:
    state = raw if isinstance(raw, Mapping) else {}
    items = _ids(state.get("items"))
    claimed = _ids(state.get("claimed"))
    running = _ids(state.get("running"))
    done = _ids(state.get("done"))
    errors = _ids(state.get("errors"))
    return {
        "schema": ORDINARY_SCHEMA,
        "cycle_id": str(state.get("cycle_id") or ""),
        "status": str(state.get("status") or ("active" if items else "terminal")).strip().lower(),
        "items": items,
        "queued": _queue(items, claimed, running, done, errors),
        "claimed": claimed,
        "running": running,
        "done": done,
        "errors": errors,
        "error_notes": {
            str(key): str(value)[:500]
            for key, value in (state.get("error_notes") or {}).items()
            if str(key).strip() and str(value).strip()
        } if isinstance(state.get("error_notes"), Mapping) else {},
        "total": len(items),
        "baseline_ready": bool(state.get("baseline_ready")),
        "baseline_generation": _integer(state.get("baseline_generation")),
        "observed_generation": _integer(state.get("observed_generation")),
        "updated_at": _number(state.get("updated_at")),
    }


def _lane_receipt(raw: object) -> dict[str, object]:
    state = raw if isinstance(raw, Mapping) else {}
    items = _ids(state.get("items"))
    claimed = _ids(state.get("claimed"))
    running = _ids(state.get("running"))
    done = _ids(state.get("done"))
    errors = _ids(state.get("errors"))
    return {
        "items": items,
        "queued": _queue(items, claimed, running, done, errors),
        "claimed": claimed,
        "running": running,
        "done": done,
        "errors": errors,
        "total": len(items),
    }


def _static_receipt(raw: Mapping[str, Any] | None) -> dict[str, object]:
    state = raw if isinstance(raw, Mapping) else {}
    raw_lanes = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {lane: _lane_receipt(raw_lanes.get(lane)) for lane in LANES}
    items = [sid for lane in LANES for sid in lanes[lane]["items"]]
    return {
        "schema": STATIC_SCHEMA,
        "status": str(state.get("status") or ("active" if items else "terminal")).strip().lower(),
        "lanes": lanes,
        "total": len(items),
        "updated_at": _number(state.get("updated_at")),
    }


def build_segment_admission_receipt(
    ordinary_state: Mapping[str, Any] | None,
    static_state: Mapping[str, Any] | None,
    *,
    now_ts: float,
    admission_rejections: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Build the bounded receipt exposed through scheduler monitor state."""

    ordinary = _ordinary_receipt(ordinary_state)
    static = _static_receipt(static_state)
    rejections: dict[str, object] = {}
    raw_rejections = admission_rejections.get("entries") if isinstance(admission_rejections, Mapping) else {}
    if isinstance(raw_rejections, Mapping):
        ordered = sorted(
            raw_rejections.items(),
            key=lambda item: _number(
                item[1].get("updated_at") if isinstance(item[1], Mapping) else 0.0
            ),
            reverse=True,
        )
        for raw_id, raw_entry in ordered[:200]:
            if not isinstance(raw_entry, Mapping):
                continue
            try:
                entity_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if entity_id <= 0:
                continue
            rejections[str(entity_id)] = {
                "entity_id": entity_id,
                "lane": str(raw_entry.get("lane") or "ordinary"),
                "reason": str(raw_entry.get("reason") or "unknown")[:120],
                "count": _integer(raw_entry.get("count"), 1),
                "updated_at": _number(raw_entry.get("updated_at")),
            }
    return {
        "schema": SCHEMA,
        "updated_at": float(now_ts),
        "ordinary": ordinary,
        "static": static,
        "admission_rejections": rejections,
    }


def validate_segment_admission_receipt(
    raw: Mapping[str, Any] | None,
    *,
    now_ts: float | None = None,
    max_age_sec: float | None = None,
) -> list[str]:
    """Return explicit evidence failures for a monitor receipt.

    This validator is intentionally strict about schema, totals, queue
    partitioning, and static-lane ownership.  It is used by tests and by
    rollout evidence checks; it does not mutate or repair persisted state.
    """

    errors: list[str] = []
    if not isinstance(raw, Mapping):
        return ["receipt_not_mapping"]
    if raw.get("schema") != SCHEMA:
        errors.append("receipt_schema")
    updated_at = _number(raw.get("updated_at"))
    if updated_at <= 0:
        errors.append("receipt_updated_at")
    if now_ts is not None and max_age_sec is not None:
        age = float(now_ts) - updated_at
        if age < 0 or age > max(0.0, float(max_age_sec)):
            errors.append("receipt_stale")

    ordinary = raw.get("ordinary")
    if not isinstance(ordinary, Mapping):
        errors.append("ordinary_missing")
    else:
        if ordinary.get("schema") != ORDINARY_SCHEMA:
            errors.append("ordinary_schema")
        items = _ids(ordinary.get("items"))
        categories = {
            "queued": _ids(ordinary.get("queued")),
            "claimed": _ids(ordinary.get("claimed")),
            "running": _ids(ordinary.get("running")),
            "done": _ids(ordinary.get("done")),
            "errors": _ids(ordinary.get("errors")),
        }
        if ordinary.get("total") != len(items):
            errors.append("ordinary_total")
        item_set = set(items)
        for name, values in categories.items():
            if not set(values) <= item_set:
                errors.append(f"ordinary_{name}_outside_items")
        if set(categories["queued"]) & (
            set(categories["claimed"])
            | set(categories["running"])
            | set(categories["done"])
            | set(categories["errors"])
        ):
            errors.append("ordinary_queue_overlap")
        if len(set(items)) != len(items):
            errors.append("ordinary_duplicate_items")

    static = raw.get("static")
    if not isinstance(static, Mapping):
        errors.append("static_missing")
    else:
        if static.get("schema") != STATIC_SCHEMA:
            errors.append("static_schema")
        raw_lanes = static.get("lanes")
        if not isinstance(raw_lanes, Mapping):
            errors.append("static_lanes_missing")
            raw_lanes = {}
        all_items: list[int] = []
        for lane in LANES:
            lane_payload = raw_lanes.get(lane)
            if not isinstance(lane_payload, Mapping):
                errors.append(f"static_{lane}_missing")
                continue
            items = _ids(lane_payload.get("items"))
            all_items.extend(items)
            if lane_payload.get("total") != len(items):
                errors.append(f"static_{lane}_total")
            item_set = set(items)
            for name in ("queued", "claimed", "running", "done", "errors"):
                values = _ids(lane_payload.get(name))
                if not set(values) <= item_set:
                    errors.append(f"static_{lane}_{name}_outside_items")
            if set(_ids(lane_payload.get("queued"))) & (
                set(_ids(lane_payload.get("claimed")))
                | set(_ids(lane_payload.get("running")))
                | set(_ids(lane_payload.get("done")))
                | set(_ids(lane_payload.get("errors")))
            ):
                errors.append(f"static_{lane}_queue_overlap")
        if len(all_items) != len(set(all_items)):
            errors.append("static_lane_ownership_overlap")
        if static.get("total") != len(all_items):
            errors.append("static_total")
    rejections = raw.get("admission_rejections")
    if rejections is not None and not isinstance(rejections, Mapping):
        errors.append("admission_rejections_mapping")
    return errors


__all__ = [
    "SCHEMA",
    "build_segment_admission_receipt",
    "validate_segment_admission_receipt",
]
