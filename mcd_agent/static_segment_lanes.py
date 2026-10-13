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
ORPHAN_GRACE_SEC = 60
_LIVE_EVIDENCE_KEYS = (
    "active",
    "db",
    "db_process",
    "executor_active",
    "live",
    "process",
    "runtime_confirmed",
    "taskstore_active",
)


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
        "attempts": {},
    }


def _timestamp(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_active_meta(raw: object) -> dict[str, dict[str, dict[str, float]]]:
    source = raw if isinstance(raw, Mapping) else {}
    normalized: dict[str, dict[str, dict[str, float]]] = {}
    for lane_name in LANES:
        lane_raw = source.get(lane_name)
        if not isinstance(lane_raw, Mapping):
            normalized[lane_name] = {}
            continue
        lane_meta: dict[str, dict[str, float]] = {}
        for raw_sid, raw_meta in lane_raw.items():
            try:
                sid = int(raw_sid)
            except (TypeError, ValueError):
                continue
            if sid <= 0 or not isinstance(raw_meta, Mapping):
                continue
            meta: dict[str, float] = {}
            for field in ("claimed_at", "running_at", "last_independent_at", "orphan_since"):
                value = _timestamp(raw_meta.get(field))
                if value is not None:
                    meta[field] = value
            if meta:
                lane_meta[str(sid)] = meta
        normalized[lane_name] = lane_meta
    return normalized


def _evidence_is_live(evidence: object) -> bool:
    if isinstance(evidence, Mapping):
        return any(bool(evidence.get(key)) for key in _LIVE_EVIDENCE_KEYS)
    return bool(evidence) if isinstance(evidence, bool) else False


def _evidence_timestamp(evidence: object, *fields: str) -> float | None:
    if not isinstance(evidence, Mapping):
        return None
    for field in fields:
        value = _timestamp(evidence.get(field))
        if value is not None:
            return value
    return None


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
    raw_attempts = source.get("attempts")
    attempts: dict[str, int] = {}
    if isinstance(raw_attempts, Mapping):
        for raw_sid, raw_count in raw_attempts.items():
            try:
                sid = int(raw_sid)
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                continue
            if sid in item_set and count > 0:
                attempts[str(sid)] = count
    return {
        "items": items,
        "claimed": claimed,
        "running": running,
        "done": done,
        "errors": errors,
        "error_notes": dict(notes) if isinstance(notes, Mapping) else {},
        "attempts": attempts,
    }


def _lane_complete(lane: Mapping[str, object]) -> bool:
    items = set(normalize_ids(lane.get("items")))
    finished = set(normalize_ids(lane.get("done"))) | set(normalize_ids(lane.get("errors")))
    active = set(normalize_ids(lane.get("claimed"))) | set(normalize_ids(lane.get("running")))
    return not items or items <= finished and not active


def _normalize_successful_coverage(
    raw: object,
    *,
    plans: Mapping[str, Iterable[object]],
) -> dict[str, list[int]]:
    source = raw if isinstance(raw, Mapping) else {}
    coverage: dict[str, list[int]] = {}
    for lane_name in LANES:
        planned = set(normalize_ids(plans.get(lane_name, [])))
        lane_raw = source.get(lane_name) if isinstance(source, Mapping) else []
        coverage[lane_name] = [sid for sid in normalize_ids(lane_raw) if sid in planned]
    return coverage


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
    explicit. For Mautic 7 the persisted lane owns the complete dependency
    closure so ordinary/other static lanes cannot admit an inherited parent.
    Native terminal roots are selected later when the closure is turned into
    a command plan; internal parents remain durable ownership members but are
    never separately scheduled by MCD.
    """

    super_ids = normalize_ids(super_priority_ids)
    super_set = set(super_ids)
    priority_ids_normalized = [sid for sid in normalize_ids(priority_ids) if sid not in super_set]
    parents = {int(key): set(normalize_ids(value)) for key, value in (parents_by_child or {}).items()}
    children = {int(key): set(normalize_ids(value)) for key, value in (children_by_parent or {}).items()}

    if int(mautic_major or 0) >= 7:
        super_roots, _ = mautic7_terminal_segment_plan(super_ids, children)
        priority_roots, _ = mautic7_terminal_segment_plan(priority_ids_normalized, children)
        # Mautic 7 executes native terminal roots, but static ownership still
        # follows the full ancestor closure. Keeping these as two concepts
        # prevents an inherited parent (for example 57 -> 59) from leaking
        # back into the ordinary ring while preserving one native worker per
        # closure.
        super_plan = normalize_ids(dependency_expanded_segment_plan(super_roots, parents))
        priority_plan = normalize_ids(dependency_expanded_segment_plan(priority_roots, parents))
        super_set = set(super_plan)
        return {
            SUPER_PRIORITY: super_plan,
            PRIORITY: [sid for sid in priority_plan if sid not in super_set],
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
    active_evidence: Mapping[tuple[str, int], Mapping[str, object] | bool] | None = None,
    orphan_grace_sec: int = ORPHAN_GRACE_SEC,
) -> tuple[dict[str, object], dict[str, list[int]], bool]:
    """Persist a lane snapshot without reordering an active queue.

    Persisted ``claimed``/``running`` values are durable coordination state,
    not proof that a process still exists.  A live executor, TaskStore row, or
    independent process/DB probe may adopt the item.  Without such evidence we
    retain the claim for one bounded grace window, then release it in place so
    the same persisted ring can claim it again.  Marker-only observations may
    anchor that grace window, but never renew it.
    """

    source = raw if isinstance(raw, Mapping) else {}
    old_lanes = source.get("lanes") if isinstance(source.get("lanes"), Mapping) else {}
    # Each static lane owns its own cadence.  A completed super-priority
    # cycle must be able to roll over while the weaker priority lane is still
    # waiting on shared closure ownership (and vice versa).  Coupling the
    # rollover to an all-lanes-complete check turns a budget wait in one lane
    # into a stop-the-world barrier for the other lane.
    started_new_by_lane = {
        lane_name: not old_lanes or _lane_complete(old_lanes.get(lane_name, {}))
        for lane_name in LANES
    }
    started_new = not old_lanes or any(
        started_new_by_lane[lane_name] and normalize_ids(plans.get(lane_name, []))
        for lane_name in LANES
    )
    added: dict[str, list[int]] = {lane: [] for lane in LANES}
    lanes: dict[str, dict[str, object]] = {}
    active_meta = _normalize_active_meta(source.get("active_meta"))
    evidence = active_evidence if isinstance(active_evidence, Mapping) else {}
    grace = max(1, int(orphan_grace_sec or ORPHAN_GRACE_SEC))
    for lane_name in LANES:
        planned = normalize_ids(plans.get(lane_name, []))
        if started_new_by_lane[lane_name]:
            lanes[lane_name] = _empty_lane(planned)
            active_meta[lane_name] = {}
            added[lane_name] = list(planned)
            continue
        lane = _normalize_lane(old_lanes.get(lane_name), [])
        existing = set(normalize_ids(lane.get("items")))
        additions = [sid for sid in planned if sid not in existing]
        lane["items"] = [*normalize_ids(lane.get("items")), *additions]

        lane_meta: dict[str, dict[str, float]] = {}
        for sid in normalize_ids([*lane.get("claimed", []), *lane.get("running", [])]):
            key = (lane_name, int(sid))
            item_evidence = evidence.get(key)
            prior = dict(active_meta.get(lane_name, {}).get(str(sid), {}))
            if _evidence_is_live(item_evidence):
                independent_at = _evidence_timestamp(
                    item_evidence,
                    "independent_at",
                    "last_independent_at",
                    "observed_at",
                )
                prior.setdefault("claimed_at", float(now_ts))
                prior.setdefault("running_at", float(now_ts))
                prior["last_independent_at"] = float(independent_at if independent_at is not None else now_ts)
                prior.pop("orphan_since", None)
                lane_meta[str(sid)] = prior
                continue

            orphan_since = _timestamp(prior.get("orphan_since"))
            if orphan_since is None:
                orphan_since = (
                    _timestamp(prior.get("last_independent_at"))
                    or _timestamp(prior.get("running_at"))
                    or _timestamp(prior.get("claimed_at"))
                    or _evidence_timestamp(item_evidence, "last_independent_at", "started_at")
                    or float(now_ts)
                )
                prior["orphan_since"] = float(orphan_since)
            if float(now_ts) - float(orphan_since) >= grace:
                lane["claimed"] = [value for value in normalize_ids(lane.get("claimed")) if value != sid]
                lane["running"] = [value for value in normalize_ids(lane.get("running")) if value != sid]
                continue
            lane_meta[str(sid)] = prior

        active_meta[lane_name] = lane_meta
        lanes[lane_name] = lane
        added[lane_name] = additions
    state = {
        "schema": SCHEMA,
        "status": "active" if any(lane.get("items") for lane in lanes.values()) else "terminal",
        "lanes": lanes,
        "active_meta": active_meta,
        "successful_coverage": _normalize_successful_coverage(
            source.get("successful_coverage"),
            plans=plans,
        ),
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
    active_meta = _normalize_active_meta(state.get("active_meta"))
    lane_meta = active_meta[lane]
    item_meta = dict(lane_meta.get(str(sid), {}))
    item_meta.setdefault("claimed_at", float(now_ts))
    item_meta.pop("orphan_since", None)
    lane_meta[str(sid)] = item_meta
    active_meta[lane] = lane_meta
    lanes[lane] = current
    state["lanes"] = lanes
    state["active_meta"] = active_meta
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
        active_meta = _normalize_active_meta(state.get("active_meta"))
        lane_meta = active_meta[lane]
        item_meta = dict(lane_meta.get(str(sid), {}))
        item_meta.setdefault("claimed_at", float(now_ts))
        item_meta.setdefault("running_at", float(now_ts))
        item_meta.pop("orphan_since", None)
        lane_meta[str(sid)] = item_meta
        active_meta[lane] = lane_meta
        lanes[lane] = current
        state["lanes"] = lanes
        state["active_meta"] = active_meta
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
    active_meta = _normalize_active_meta(state.get("active_meta"))
    active_meta[lane].pop(str(sid), None)
    lanes[lane] = current
    state["lanes"] = lanes
    state["active_meta"] = active_meta
    state["status"] = "terminal" if all(_lane_complete(item) for item in lanes.values()) else "active"
    state["updated_at"] = float(now_ts)
    return state


def record_attempt(
    raw: Mapping[str, Any] | None,
    *,
    lane: str,
    entity_id: int,
    now_ts: float,
) -> dict[str, object]:
    """Record one real Mautic start without changing claim/terminal state."""

    state = dict(raw or {})
    lanes_raw = state.get("lanes") if isinstance(state.get("lanes"), Mapping) else {}
    lanes = {name: _normalize_lane(lanes_raw.get(name), []) for name in LANES}
    current = lanes.get(lane)
    if current is None:
        return state
    sid = int(entity_id)
    if sid not in set(normalize_ids(current.get("items"))):
        return state
    attempts = dict(current.get("attempts") or {})
    attempts[str(sid)] = max(0, int(attempts.get(str(sid), 0) or 0)) + 1
    current["attempts"] = attempts
    lanes[lane] = current
    state["lanes"] = lanes
    state["status"] = "active"
    state["updated_at"] = float(now_ts)
    return state


def attempt_count(raw: Mapping[str, Any] | None, *, lane: str, entity_id: int) -> int:
    source = raw if isinstance(raw, Mapping) else {}
    lanes = source.get("lanes") if isinstance(source.get("lanes"), Mapping) else {}
    lane_payload = lanes.get(lane) if isinstance(lanes, Mapping) else {}
    attempts = lane_payload.get("attempts") if isinstance(lane_payload, Mapping) else {}
    try:
        return max(0, int((attempts or {}).get(str(int(entity_id)), 0) or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


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
    active_meta = _normalize_active_meta(state.get("active_meta"))
    active_meta[lane].pop(str(sid), None)
    if success:
        current["done"] = normalize_ids([*current.get("done", []), sid])
        current["errors"] = [x for x in normalize_ids(current.get("errors")) if x != sid]
        successful_coverage = _normalize_successful_coverage(
            state.get("successful_coverage"),
            plans={name: lane.get("items", []) for name, lane in lanes.items()},
        )
        successful_coverage[lane] = normalize_ids([*successful_coverage.get(lane, []), sid])
        state["successful_coverage"] = successful_coverage
    else:
        current["errors"] = normalize_ids([*current.get("errors", []), sid])
        if note:
            notes = dict(current.get("error_notes") or {})
            notes[str(sid)] = str(note)[:500]
            current["error_notes"] = notes
    lanes[lane] = current
    state["lanes"] = lanes
    state["active_meta"] = active_meta
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
