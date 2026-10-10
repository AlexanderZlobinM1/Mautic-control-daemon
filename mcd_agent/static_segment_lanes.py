"""Persisted static priority and super-priority segment lanes.

The ordinary segment cycle must never promote work into a static lane. This
module keeps the two operator-owned MCC lists separate while giving their
dependency closures one shared, deduplicated execution plan per instance.
"""

from __future__ import annotations

from hashlib import sha1
from typing import Any, Iterable, Mapping

from mcd_agent.segment_dependencies import (
    dependency_expanded_segment_plan,
    dependency_segment_closure,
    mautic7_terminal_segment_plan,
)


SCHEMA = "mcd-static-segment-lanes-v1"
SUPER_PRIORITY = "super_priority"
PRIORITY = "priority"
LANES = (SUPER_PRIORITY, PRIORITY)


def normalize_ids(values: Iterable[object] | None) -> list[int]:
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


def state_key(root: str) -> str:
    digest = sha1(str(root or "").encode("utf-8")).hexdigest()[:24]
    return f"segment_static_lanes:{digest}"


def _empty_lane(items: Iterable[object] | None = None) -> dict[str, object]:
    return {
        "items": normalize_ids(items),
        "claimed": [],
        "running": [],
        "done": [],
        "errors": [],
        "error_notes": {},
    }


def _normalize_lane(raw: object, planned: Iterable[object] | None = None) -> dict[str, object]:
    source = raw if isinstance(raw, Mapping) else {}
    items = normalize_ids([*(source.get("items") or []), *(planned or [])])
    item_set = set(items)
    done = [sid for sid in normalize_ids(source.get("done")) if sid in item_set]
    errors = [sid for sid in normalize_ids(source.get("errors")) if sid in item_set and sid not in done]
    claimed = [
        sid
        for sid in normalize_ids(source.get("claimed"))
        if sid in item_set and sid not in done and sid not in errors
    ]
    running = [
        sid
        for sid in normalize_ids(source.get("running"))
        if sid in item_set and sid not in done and sid not in errors
    ]
    claimed = normalize_ids([*claimed, *running])
    notes = source.get("error_notes")
    return {
        "items": items,
        "claimed": claimed,
        "running": running,
        "done": done,
        "errors": errors,
        "error_notes": dict(notes) if isinstance(notes, Mapping) else {},
    }


def _lane_complete(lane: Mapping[str, object]) -> bool:
    items = set(normalize_ids(lane.get("items")))
    finished = set(normalize_ids(lane.get("done"))) | set(normalize_ids(lane.get("errors")))
    active = set(normalize_ids(lane.get("claimed"))) | set(normalize_ids(lane.get("running")))
    return not items or items <= finished and not active


def _all_complete(state: Mapping[str, object]) -> bool:
    lanes = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    return all(_lane_complete(lanes.get(lane, {})) for lane in LANES)


def build_lane_plans(
    *,
    super_priority_ids: Iterable[object] | None,
    priority_ids: Iterable[object] | None,
    parents_by_child: Mapping[int, set[int]] | None,
    children_by_parent: Mapping[int, set[int]] | None,
    mautic_major: int,
) -> dict[str, list[int]]:
    """Build mutually exclusive static lane plans.

    Priority is the legacy whitelist lane. Super-priority owns any shared
    dependency closure first. For Mautic <=6 the plan is topological and
    explicit. For Mautic 7 only native terminal roots are retained; internal
    parents are covered by Mautic's native terminal semantics and are never
    separately scheduled by MCD.
    """

    super_ids = normalize_ids(super_priority_ids)
    super_set = set(super_ids)
    priority_ids_normalized = [sid for sid in normalize_ids(priority_ids) if sid not in super_set]
    parents = {int(key): set(normalize_ids(value)) for key, value in (parents_by_child or {}).items()}
    children = {int(key): set(normalize_ids(value)) for key, value in (children_by_parent or {}).items()}

    if int(mautic_major or 0) >= 7:
        super_plan, _ = mautic7_terminal_segment_plan(super_ids, children)
        priority_plan, _ = mautic7_terminal_segment_plan(priority_ids_normalized, children)
        super_plan = normalize_ids(super_plan)
        super_set = set(super_plan)
        return {
            SUPER_PRIORITY: super_plan,
            PRIORITY: [sid for sid in normalize_ids(priority_plan) if sid not in super_set],
        }

    seeds = [*super_ids, *priority_ids_normalized]
    full_plan = dependency_expanded_segment_plan(seeds, parents)
    super_closure = set(super_ids) | dependency_segment_closure(set(super_ids), parents)
    priority_closure = set(priority_ids_normalized) | dependency_segment_closure(set(priority_ids_normalized), parents)
    return {
        SUPER_PRIORITY: [sid for sid in full_plan if sid in super_closure],
        PRIORITY: [sid for sid in full_plan if sid in priority_closure and sid not in super_closure],
    }


def reconcile_state(
    raw: Mapping[str, Any] | None,
    *,
    plans: Mapping[str, Iterable[object]],
    now_ts: float,
) -> tuple[dict[str, object], dict[str, list[int]], bool]:
    """Persist a lane snapshot without reordering an active queue."""

    source = raw if isinstance(raw, Mapping) else {}
    old_lanes = source.get("lanes") if isinstance(source.get("lanes"), Mapping) else {}
    complete = bool(old_lanes) and _all_complete({"lanes": old_lanes})
    started_new = not old_lanes or complete
    added: dict[str, list[int]] = {lane: [] for lane in LANES}
    lanes: dict[str, dict[str, object]] = {}
    for lane_name in LANES:
        planned = normalize_ids(plans.get(lane_name, []))
        if started_new:
            lanes[lane_name] = _empty_lane(planned)
            added[lane_name] = list(planned)
            continue
        lane = _normalize_lane(old_lanes.get(lane_name), [])
        existing = set(normalize_ids(lane.get("items")))
        additions = [sid for sid in planned if sid not in existing]
        lane["items"] = [*normalize_ids(lane.get("items")), *additions]
        lanes[lane_name] = lane
        added[lane_name] = additions
    state = {
        "schema": SCHEMA,
        "status": "active" if any(lane.get("items") for lane in lanes.values()) else "terminal",
        "lanes": lanes,
        "updated_at": float(now_ts),
    }
    if all(_lane_complete(lane) for lane in lanes.values()):
        state["status"] = "terminal"
    return state, added, started_new


def claim_item(
    raw: Mapping[str, Any] | None,
    *,
    lane: str,
    entity_id: int,
    blocked_ids: Iterable[object] | None = None,
    globally_running_ids: Iterable[object] | None = None,
    now_ts: float,
) -> tuple[dict[str, object], bool]:
    """Atomically claim one item while respecting the shared closure lock."""

    state = dict(raw or {})
    lanes_raw = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {name: _normalize_lane(lanes_raw.get(name), []) for name in LANES}
    if lane not in lanes:
        return state, False
    current = lanes[lane]
    sid = int(entity_id)
    blocked = set(normalize_ids(blocked_ids))
    global_running = set(normalize_ids(globally_running_ids))
    finished = set(normalize_ids(current.get("done"))) | set(normalize_ids(current.get("errors")))
    claimed = set(normalize_ids(current.get("claimed")))
    running = set(normalize_ids(current.get("running"))) | global_running
    ok = (
        sid in set(normalize_ids(current.get("items")))
        and sid not in blocked
        and sid not in finished
        and sid not in claimed
        and sid not in running
    )
    if not ok:
        state["lanes"] = lanes
        return state, False
    current["claimed"] = normalize_ids([*current.get("claimed", []), sid])
    lanes[lane] = current
    state["lanes"] = lanes
    state["status"] = "active"
    state["updated_at"] = float(now_ts)
    return state, True


def mark_running(raw: Mapping[str, Any] | None, *, lane: str, entity_id: int, now_ts: float) -> dict[str, object]:
    state = dict(raw or {})
    lanes_raw = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {name: _normalize_lane(lanes_raw.get(name), []) for name in LANES}
    current = lanes.get(lane)
    if current is None:
        return state
    sid = int(entity_id)
    if sid in set(normalize_ids(current.get("items"))):
        current["claimed"] = normalize_ids([*current.get("claimed", []), sid])
        current["running"] = normalize_ids([*current.get("running", []), sid])
        lanes[lane] = current
        state["lanes"] = lanes
        state["status"] = "active"
        state["updated_at"] = float(now_ts)
    return state


def release_claim(raw: Mapping[str, Any] | None, *, lane: str, entity_id: int, now_ts: float) -> dict[str, object]:
    state = dict(raw or {})
    lanes_raw = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {name: _normalize_lane(lanes_raw.get(name), []) for name in LANES}
    current = lanes.get(lane)
    if current is None:
        return state
    sid = int(entity_id)
    current["claimed"] = [x for x in normalize_ids(current.get("claimed")) if x != sid]
    current["running"] = [x for x in normalize_ids(current.get("running")) if x != sid]
    lanes[lane] = current
    state["lanes"] = lanes
    state["status"] = "terminal" if all(_lane_complete(item) for item in lanes.values()) else "active"
    state["updated_at"] = float(now_ts)
    return state


def mark_terminal(
    raw: Mapping[str, Any] | None,
    *,
    lane: str,
    entity_id: int,
    success: bool,
    note: str | None = None,
    now_ts: float,
) -> dict[str, object]:
    state = dict(raw or {})
    lanes_raw = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {name: _normalize_lane(lanes_raw.get(name), []) for name in LANES}
    current = lanes.get(lane)
    if current is None:
        return state
    sid = int(entity_id)
    current["claimed"] = [x for x in normalize_ids(current.get("claimed")) if x != sid]
    current["running"] = [x for x in normalize_ids(current.get("running")) if x != sid]
    if success:
        current["done"] = normalize_ids([*current.get("done", []), sid])
        current["errors"] = [x for x in normalize_ids(current.get("errors")) if x != sid]
    else:
        current["errors"] = normalize_ids([*current.get("errors", []), sid])
        if note:
            notes = dict(current.get("error_notes") or {})
            notes[str(sid)] = str(note)[:500]
            current["error_notes"] = notes
    lanes[lane] = current
    state["lanes"] = lanes
    state["status"] = "terminal" if all(_lane_complete(item) for item in lanes.values()) else "active"
    state["updated_at"] = float(now_ts)
    return state


def pending_items(raw: Mapping[str, Any] | None, lane: str) -> list[int]:
    source = raw if isinstance(raw, Mapping) else {}
    lanes = source.get("lanes") if isinstance(source.get("lanes"), Mapping) else {}
    current = _normalize_lane(lanes.get(lane), [])
    finished = set(normalize_ids(current.get("done"))) | set(normalize_ids(current.get("errors")))
    active = set(normalize_ids(current.get("claimed"))) | set(normalize_ids(current.get("running")))
    return [sid for sid in normalize_ids(current.get("items")) if sid not in finished and sid not in active]
