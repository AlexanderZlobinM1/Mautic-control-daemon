"""Persisted ordinary segment-cycle admission.

The scheduler owns the storage transaction; this module keeps the cycle
state transition rules pure and deterministic so they can be exercised
without a live Mautic database or process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1
from typing import Any, Iterable, Mapping


SCHEMA = "mcd-segment-cycle-v1"
BASELINE_MAX_ENTRIES = 4096
MANUAL_ADMISSIONS_MAX = 256


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


def cycle_key(root: str) -> str:
    return f"segment_cycle:{sha1(str(root or '').encode('utf-8')).hexdigest()}"


def never_built_ids(rows: Iterable[Mapping[str, Any]] | None) -> list[int]:
    """Return published definitions whose build marker is absent."""

    out: list[int] = []
    for row in rows or ():
        try:
            sid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            continue
        marker = row.get("last_built_date")
        if marker is None:
            marker = row.get("last_built")
        if marker is None:
            marker = row.get("built_ts")
        if marker in (None, "", 0, "0"):
            out.append(sid)
    return normalize_ids(out)


def _timestamp_seconds(value: object) -> float | None:
    """Normalize DB timestamp values without making ordering locale-dependent."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None:
            return numeric if numeric > 0 else None
        raw = str(value).strip()
        if not raw or raw == "0":
            return None
        normalized = raw.replace("T", " ")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _row_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _row_timestamp(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _timestamp_seconds(row.get(key))
        if value is not None:
            return value
    return None


def _normalize_baseline(
    raw: Mapping[object, object] | None,
    *,
    max_entries: int = BASELINE_MAX_ENTRIES,
) -> dict[str, dict[str, float | None]]:
    """Keep only the bounded source fingerprint needed for admission.

    The baseline is deliberately not a copy of the segment definition.  The
    modified and built timestamps are sufficient to distinguish a changed
    definition, a fresh successful build, and a stale build while keeping
    persisted runtime state bounded and restart-safe.
    """

    if not isinstance(raw, Mapping):
        return {}
    normalized: dict[str, dict[str, float | None]] = {}
    for raw_sid, raw_record in raw.items():
        try:
            sid = int(raw_sid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if sid <= 0:
            continue
        if isinstance(raw_record, Mapping):
            modified = _timestamp_seconds(
                raw_record.get("modified_ts", raw_record.get("modified"))
            )
            built = _timestamp_seconds(
                raw_record.get("built_ts", raw_record.get("built"))
            )
        else:
            # Accept the old compact form as a modified timestamp so a
            # partially upgraded runtime state remains safe and monotonic.
            modified = _timestamp_seconds(raw_record)
            built = None
        normalized[str(sid)] = {"modified_ts": modified, "built_ts": built}
    limit = max(1, int(max_entries or BASELINE_MAX_ENTRIES))
    return {
        sid: normalized[sid]
        for sid in sorted(normalized, key=lambda value: int(value))[:limit]
    }


def source_baseline(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    max_entries: int = BASELINE_MAX_ENTRIES,
) -> dict[str, dict[str, float | None]]:
    """Build a bounded fingerprint snapshot from published definitions."""

    snapshot: dict[str, dict[str, float | None]] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        sid = _row_id(row)
        if sid <= 0 or str(sid) in snapshot:
            continue
        snapshot[str(sid)] = {
            "modified_ts": _row_timestamp(row, "date_modified", "modified_ts"),
            "built_ts": _row_timestamp(row, "last_built_date", "last_built", "built_ts"),
        }
    return _normalize_baseline(snapshot, max_entries=max_entries)


def _normalize_manual_admissions(raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, Mapping):
        return {}
    entries: list[tuple[str, dict[str, object]]] = []
    for raw_request_id, raw_entry in raw.items():
        try:
            request_id = int(raw_request_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if request_id <= 0 or not isinstance(raw_entry, Mapping):
            continue
        status = str(raw_entry.get("status") or "claimed").strip().lower()
        entries.append(
            (
                str(request_id),
                {
                    "request_id": request_id,
                    "lane": str(raw_entry.get("lane") or "ordinary"),
                    "entity_ids": normalize_ids(raw_entry.get("entity_ids")),
                    "claimed_ids": normalize_ids(raw_entry.get("claimed_ids")),
                    "observed_snapshot": _normalize_baseline(raw_entry.get("observed_snapshot")),
                    "status": status,
                    "applied": bool(raw_entry.get("applied")),
                    "updated_at": float(raw_entry.get("updated_at") or 0.0),
                },
            )
        )
    # Keep the most recent bounded receipts in insertion order. Completed
    # receipts remain useful for restart reconciliation and provenance.
    return dict(entries[-MANUAL_ADMISSIONS_MAX:])


def ordinary_snapshot_order(
    rows: Iterable[Mapping[str, Any]] | None,
    observed_ids: Iterable[object] | None,
    *,
    static_priority_ids: Iterable[object] | None = None,
    baseline_snapshot: Mapping[object, object] | None = None,
    retry_ids: Iterable[object] | None = None,
    stale_after_sec: int = 24 * 60 * 60,
    now_ts: float,
) -> list[int]:
    """Build the deterministic ordinary snapshot.

    The two static MCC lanes are excluded by ``static_priority_ids``. The
    remaining due candidates are grouped as never-built, changed, then stale;
    currently-fresh published rows are discovery-only and are not admitted.
    When ``baseline_snapshot`` is supplied, changed means newer than the
    previous fully completed ordinary-cycle snapshot rather than merely newer
    than the row's last build marker. ``retry_ids`` keeps terminal failures
    eligible even when their source fingerprint did not change.
    Within each group the first observed order is preserved; weights are
    deliberately not consulted.
    """

    row_by_id: dict[int, Mapping[str, Any]] = {}
    row_order_never_built: list[int] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        sid = _row_id(row)
        if sid <= 0 or sid in row_by_id:
            continue
        row_by_id[sid] = row
        if _row_timestamp(row, "last_built_date", "last_built", "built_ts") is None:
            row_order_never_built.append(sid)

    excluded = set(normalize_ids(static_priority_ids))
    observed = normalize_ids(observed_ids)
    retry = set(normalize_ids(retry_ids))
    baseline = (
        _normalize_baseline(baseline_snapshot)
        if baseline_snapshot is not None
        else None
    )
    # A full read of definitions is the authoritative discovery path for a
    # never-built segment that did not appear in the due query. Do not add
    # other non-due rows from that read.
    retry_candidates = [sid for sid in retry if sid in row_by_id]
    candidates = normalize_ids([*observed, *row_order_never_built, *retry_candidates])
    candidates = [sid for sid in candidates if sid not in excluded]
    observed_position = {sid: index for index, sid in enumerate(candidates)}
    stale_limit = max(0, int(stale_after_sec or 0))

    def category(sid: int) -> int | None:
        row = row_by_id.get(sid, {})
        built = _row_timestamp(row, "last_built_date", "last_built", "built_ts")
        if built is None:
            return 0  # never-built
        if sid in retry:
            return 1  # retry a terminal error before stale work
        modified = _row_timestamp(row, "date_modified", "modified_ts")
        if baseline is None:
            changed = modified is not None and modified > built
        else:
            previous = baseline.get(str(sid))
            if previous is None:
                changed = True
            else:
                previous_modified = _timestamp_seconds(previous.get("modified_ts"))
                changed = modified is not None and (
                    previous_modified is None or modified > previous_modified
                )
        if changed:
            return 1  # changed
        if stale_limit and float(now_ts) - built >= stale_limit:
            return 2  # stale
        # A bounded full scan is also a discovery read; a fresh segment is not
        # ordinary work merely because it was observed.
        return None

    due_candidates = [sid for sid in candidates if category(sid) is not None]
    return sorted(due_candidates, key=lambda sid: (int(category(sid) or 0), observed_position[sid]))


def _cycle_id(now_ts: float, items: list[int]) -> str:
    raw = f"{int(float(now_ts) * 1000)}:{','.join(str(x) for x in items)}"
    return sha1(raw.encode("utf-8")).hexdigest()[:20]


def _new_cycle(
    items: list[int],
    now_ts: float,
    *,
    baseline: Mapping[object, object] | None = None,
    baseline_ready: bool = False,
    baseline_generation: int = 0,
    cycle_baseline: Mapping[object, object] | None = None,
    observed_snapshot: Mapping[object, object] | None = None,
    observed_generation: int = 0,
) -> dict[str, object]:
    cycle_snapshot = _normalize_baseline(cycle_baseline)
    observed = _normalize_baseline(
        observed_snapshot if observed_snapshot is not None else cycle_snapshot
    )
    return {
        "schema": SCHEMA,
        "cycle_id": _cycle_id(now_ts, items),
        "status": "active" if items else "terminal",
        "items": list(items),
        "claimed": [],
        "running": [],
        "done": [],
        "errors": [],
        "error_notes": {},
        "baseline": _normalize_baseline(baseline),
        "applied_state": _normalize_baseline(baseline),
        "baseline_ready": bool(baseline_ready),
        "baseline_generation": max(0, int(baseline_generation or 0)),
        "applied_generation": max(0, int(baseline_generation or 0)),
        "cycle_baseline": cycle_snapshot,
        "observed_snapshot": observed,
        "observed_generation": max(0, int(observed_generation or 0)),
        "admitted_at": float(now_ts),
        "updated_at": float(now_ts),
    }


def normalize_state(raw: Mapping[str, Any] | None) -> dict[str, object]:
    source = dict(raw or {})
    items = normalize_ids(source.get("items"))
    item_set = set(items)
    done = [x for x in normalize_ids(source.get("done")) if x in item_set]
    errors = [x for x in normalize_ids(source.get("errors")) if x in item_set and x not in done]
    claimed = [x for x in normalize_ids(source.get("claimed")) if x in item_set and x not in done and x not in errors]
    running = [x for x in normalize_ids(source.get("running")) if x in item_set and x not in done and x not in errors]
    # A running item is necessarily claimed. Keep order deterministic.
    claimed = normalize_ids([*claimed, *running])
    status = str(source.get("status") or "active").strip().lower()
    if status not in {"active", "terminal"}:
        status = "active"
    baseline = _normalize_baseline(source.get("baseline"))
    applied_state = _normalize_baseline(source.get("applied_state", baseline))
    cycle_baseline = _normalize_baseline(source.get("cycle_baseline"))
    observed_snapshot = _normalize_baseline(
        source.get("observed_snapshot", source.get("observed"))
    )
    if not observed_snapshot:
        observed_snapshot = dict(cycle_baseline)
    baseline_ready = bool(source.get("baseline_ready", bool(baseline)))
    return {
        "schema": SCHEMA,
        "cycle_id": str(source.get("cycle_id") or ""),
        "status": status,
        "items": items,
        "claimed": claimed,
        "running": running,
        "done": done,
        "errors": errors,
        "error_notes": dict(source.get("error_notes") or {}) if isinstance(source.get("error_notes"), dict) else {},
        "baseline": baseline,
        "applied_state": applied_state,
        "baseline_ready": baseline_ready,
        "baseline_generation": max(0, int(source.get("baseline_generation") or 0)),
        "applied_generation": max(
            0,
            int(source.get("applied_generation", source.get("baseline_generation")) or 0),
        ),
        "cycle_baseline": cycle_baseline,
        "observed_snapshot": observed_snapshot,
        "observed_generation": max(0, int(source.get("observed_generation") or 0)),
        "manual_admissions": _normalize_manual_admissions(source.get("manual_admissions")),
        "manual_retry_ids": normalize_ids(source.get("manual_retry_ids")),
        "admitted_at": float(source.get("admitted_at") or 0.0),
        "updated_at": float(source.get("updated_at") or 0.0),
    }


def _mark_terminal_if_complete(state: dict[str, object]) -> None:
    items = set(normalize_ids(state.get("items")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    active = set(normalize_ids(state.get("claimed"))) | set(normalize_ids(state.get("running")))
    if items and items <= finished and not active:
        state["status"] = "terminal"
    elif not items:
        state["status"] = "terminal"
    else:
        state["status"] = "active"


def _observe_source(
    state: dict[str, object],
    source_snapshot: Mapping[object, object] | None,
) -> None:
    if source_snapshot is None:
        return
    observed = _normalize_baseline(source_snapshot)
    previous = _normalize_baseline(state.get("observed_snapshot"))
    state["observed_snapshot"] = observed
    if observed != previous:
        state["observed_generation"] = max(
            0, int(state.get("observed_generation") or 0)
        ) + 1


def _commit_terminal_baseline(state: dict[str, object]) -> None:
    """Apply the latest observation only for work that did not terminal-fail.

    This is intentionally called only on the active -> terminal transition.
    A partial cycle may observe new timestamps, but it cannot make them the
    completed-cycle baseline. Terminal errors retain the old per-ID value (or
    no value), so the next cycle can retry them even if the source is fresh.
    """

    if str(state.get("status") or "") != "terminal":
        return
    previous = _normalize_baseline(state.get("baseline"))
    observed = _normalize_baseline(
        state.get("observed_snapshot") or state.get("cycle_baseline")
    )
    errors = {
        str(sid)
        for sid in (
            set(normalize_ids(state.get("errors")))
            | set(normalize_ids(state.get("manual_retry_ids")))
        )
    }
    done = set(normalize_ids(state.get("done")))
    if not state.get("items") and not errors:
        # An empty terminal cycle still establishes that the full read-only
        # snapshot was observed, preventing fresh rows from being rediscovered
        # on every scheduler tick.
        applied = observed
    else:
        applied = dict(previous)
        for sid, record in observed.items():
            if sid not in errors:
                applied[sid] = dict(record)
        # A successful item whose row disappeared from the latest read must
        # not erase prior history.
    state["baseline"] = _normalize_baseline(applied)
    state["applied_state"] = _normalize_baseline(applied)
    state["baseline_ready"] = True
    if not errors and (not state.get("items") or done):
        state["baseline_generation"] = max(
            0, int(state.get("baseline_generation") or 0)
        ) + 1
    elif done:
        # A mixed success/error terminal cycle advances the applied generation
        # for successes while the failed IDs remain behind.
        state["baseline_generation"] = max(
            0, int(state.get("baseline_generation") or 0)
        ) + 1
    state["applied_generation"] = max(
        int(state.get("applied_generation") or 0),
        int(state.get("baseline_generation") or 0),
    )


def manual_admission_info(
    raw: Mapping[str, Any] | None,
    *,
    request_id: int,
) -> dict[str, object] | None:
    state = normalize_state(raw)
    entry = (state.get("manual_admissions") or {}).get(str(int(request_id)))
    return dict(entry) if isinstance(entry, Mapping) else None


def manual_claim(
    raw: Mapping[str, Any] | None,
    *,
    request_id: int,
    entity_ids: Iterable[object] | None,
    lane: str,
    observed_snapshot: Mapping[object, object] | None,
    now_ts: float,
) -> tuple[dict[str, object], bool, str]:
    """Claim a manual target/closure in the same ordinary state machine."""

    state = normalize_state(raw)
    rid = int(request_id)
    ids = normalize_ids(entity_ids)
    if rid <= 0 or not ids:
        return state, False, "invalid_manual_admission"
    admissions = dict(state.get("manual_admissions") or {})
    previous = admissions.get(str(rid))
    if isinstance(previous, Mapping) and str(previous.get("status") or "").startswith("claimed"):
        return state, False, "manual_already_claimed"

    items = set(normalize_ids(state.get("items")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    active = set(normalize_ids(state.get("claimed"))) | set(normalize_ids(state.get("running")))
    if set(ids) & finished:
        return state, False, "manual_overlap_terminal"
    if set(ids) & active:
        return state, False, "manual_overlap_active"

    claimed_ids = [sid for sid in ids if sid in items]
    state["claimed"] = normalize_ids([*state.get("claimed", []), *claimed_ids])
    if claimed_ids:
        state["status"] = "active"
    admissions[str(rid)] = {
        "request_id": rid,
        "lane": str(lane or "ordinary"),
        "entity_ids": ids,
        "claimed_ids": claimed_ids,
        "observed_snapshot": _normalize_baseline(observed_snapshot),
        "status": "claimed",
        "applied": False,
        "updated_at": float(now_ts),
    }
    state["manual_admissions"] = _normalize_manual_admissions(admissions)
    state["manual_retry_ids"] = [
        sid for sid in normalize_ids(state.get("manual_retry_ids")) if sid not in set(ids)
    ]
    state["updated_at"] = float(now_ts)
    return state, True, "ordinary_pending" if claimed_ids else "outside_cycle"


def manual_release(
    raw: Mapping[str, Any] | None,
    *,
    request_id: int,
    now_ts: float,
) -> dict[str, object]:
    state = normalize_state(raw)
    rid = int(request_id)
    admissions = dict(state.get("manual_admissions") or {})
    entry = admissions.get(str(rid))
    if not isinstance(entry, Mapping):
        return state
    claimed_ids = set(normalize_ids(entry.get("claimed_ids")))
    state["claimed"] = [sid for sid in normalize_ids(state.get("claimed")) if sid not in claimed_ids]
    state["running"] = [sid for sid in normalize_ids(state.get("running")) if sid not in claimed_ids]
    released = dict(entry)
    released["status"] = "released"
    released["updated_at"] = float(now_ts)
    admissions[str(rid)] = released
    state["manual_admissions"] = _normalize_manual_admissions(admissions)
    state["updated_at"] = float(now_ts)
    _mark_terminal_if_complete(state)
    return state


def manual_terminal(
    raw: Mapping[str, Any] | None,
    *,
    request_id: int,
    success: bool,
    note: str | None = None,
    observed_snapshot: Mapping[object, object] | None = None,
    now_ts: float,
) -> dict[str, object]:
    """Reconcile a persisted manual admission on process terminal."""

    state = normalize_state(raw)
    rid = int(request_id)
    admissions = dict(state.get("manual_admissions") or {})
    raw_entry = admissions.get(str(rid))
    if not isinstance(raw_entry, Mapping):
        return state
    entry = dict(raw_entry)
    ids = normalize_ids(entry.get("entity_ids"))
    claimed_ids = set(normalize_ids(entry.get("claimed_ids")))
    item_ids = set(normalize_ids(state.get("items")))
    previous_status = str(state.get("status") or "active")
    observed = _normalize_baseline(
        observed_snapshot if observed_snapshot is not None else entry.get("observed_snapshot")
    )
    if observed_snapshot is not None:
        entry["observed_snapshot"] = observed

    state["claimed"] = [sid for sid in normalize_ids(state.get("claimed")) if sid not in claimed_ids]
    state["running"] = [sid for sid in normalize_ids(state.get("running")) if sid not in claimed_ids]
    if success:
        state["done"] = normalize_ids([*state.get("done", []), *sorted(claimed_ids)])
        state["errors"] = [sid for sid in normalize_ids(state.get("errors")) if sid not in claimed_ids]
        outside_ids = set(ids) - item_ids
        baseline = _normalize_baseline(state.get("baseline"))
        applied_outside = 0
        for sid in sorted(outside_ids):
            record = observed.get(str(sid))
            if record is None:
                continue
            baseline[str(sid)] = dict(record)
            applied_outside += 1
        if applied_outside:
            state["baseline"] = _normalize_baseline(baseline)
            state["baseline_ready"] = True
            if not item_ids:
                state["baseline_generation"] = max(
                    0, int(state.get("baseline_generation") or 0)
                ) + 1
        state["manual_retry_ids"] = [
            sid for sid in normalize_ids(state.get("manual_retry_ids")) if sid not in set(ids)
        ]
        entry["status"] = "terminal_success"
        entry["applied"] = True
    else:
        state["errors"] = normalize_ids([*state.get("errors", []), *sorted(claimed_ids)])
        state["manual_retry_ids"] = normalize_ids([*state.get("manual_retry_ids", []), *ids])
        if note and claimed_ids:
            notes = dict(state.get("error_notes") or {})
            for sid in claimed_ids:
                notes[str(sid)] = str(note)[:500]
            state["error_notes"] = notes
        entry["status"] = "terminal_error"
        entry["applied"] = False
    entry["updated_at"] = float(now_ts)
    admissions[str(rid)] = entry
    state["manual_admissions"] = _normalize_manual_admissions(admissions)
    state["updated_at"] = float(now_ts)
    _mark_terminal_if_complete(state)
    if previous_status != "terminal" and str(state.get("status") or "") == "terminal":
        _commit_terminal_baseline(state)
    return state


def sync_cycle(
    raw: Mapping[str, Any] | None,
    *,
    planned_ids: Iterable[object] | None,
    never_built: Iterable[object] | None,
    running_ids: Iterable[object] | None,
    now_ts: float,
    source_snapshot: Mapping[object, object] | None = None,
) -> tuple[dict[str, object], list[int], bool]:
    """Reconcile planning observations without resetting an active cycle.

    Returns ``(state, added_ids, started_new_cycle)``. Active cycles never
    reorder existing work. New ordinary IDs append in first-seen order; new
    never-built IDs are inserted after existing pending never-built items and
    before pending changed/stale items. Claimed, running, and terminal items
    retain their positions.
    """

    planned = normalize_ids(planned_ids)
    never = set(normalize_ids(never_built))
    running = set(normalize_ids(running_ids))
    current = normalize_state(raw)
    current_items = normalize_ids(current.get("items"))
    current_status = str(current.get("status") or "active")
    finished = set(normalize_ids(current.get("done"))) | set(normalize_ids(current.get("errors")))
    active_claims = set(normalize_ids(current.get("claimed"))) | set(normalize_ids(current.get("running")))
    cycle_complete = bool(current_items) and set(current_items) <= finished and not active_claims
    started_new = not current_items or current_status == "terminal" or cycle_complete
    if started_new:
        current_observed = _normalize_baseline(current.get("observed_snapshot"))
        cycle_snapshot = (
            _normalize_baseline(source_snapshot)
            if source_snapshot is not None
            else current_observed or _normalize_baseline(current.get("baseline"))
        )
        state = _new_cycle(
            planned,
            now_ts,
            baseline=current.get("baseline"),
            baseline_ready=bool(current.get("baseline_ready")),
            baseline_generation=int(current.get("baseline_generation") or 0),
            cycle_baseline=cycle_snapshot,
            observed_snapshot=cycle_snapshot,
            observed_generation=max(0, int(current.get("observed_generation") or 0)) + 1,
        )
        added = list(planned)
        _mark_terminal_if_complete(state)
        if str(state.get("status") or "") == "terminal":
            _commit_terminal_baseline(state)
    else:
        state = current
        state["status"] = "active"
        if not state.get("cycle_baseline") and source_snapshot is not None:
            state["cycle_baseline"] = _normalize_baseline(source_snapshot)
        _observe_source(state, source_snapshot)
        existing = set(current_items)
        candidate_order = normalize_ids([*planned, *normalize_ids(never)])
        additions = [sid for sid in candidate_order if sid not in existing]
        new_never = [sid for sid in additions if sid in never]
        ordinary_additions = [sid for sid in additions if sid not in never]
        if new_never:
            claimed_now = set(normalize_ids(current.get("claimed")))
            running_now = set(normalize_ids(current.get("running")))
            pending_non_never_index = next(
                (
                    index
                    for index, sid in enumerate(current_items)
                    if sid not in never
                    and sid not in set(normalize_ids(current.get("done")))
                    and sid not in set(normalize_ids(current.get("errors")))
                    and sid not in claimed_now
                    and sid not in running_now
                    and sid not in running
                ),
                None,
            )
            insertion_index = (
                len(current_items)
                if pending_non_never_index is None
                else pending_non_never_index
            )
            # The insertion point is immediately before the first pending
            # changed/stale item; a valid ordinary snapshot already has all
            # pending never-built items before that point.
            state["items"] = [
                *current_items[:insertion_index],
                *new_never,
                *current_items[insertion_index:],
                *ordinary_additions,
            ]
        else:
            state["items"] = [*current_items, *ordinary_additions]
        added = additions

    state_items = set(normalize_ids(state.get("items")))
    state["running"] = [sid for sid in normalize_ids(running) if sid in state_items]
    running_set = set(normalize_ids(state.get("running")))
    state["claimed"] = [
        sid
        for sid in normalize_ids(state.get("claimed"))
        if sid in running_set and sid not in set(normalize_ids(state.get("done"))) and sid not in set(normalize_ids(state.get("errors")))
    ]
    state["claimed"] = normalize_ids([*state.get("claimed", []), *state["running"]])
    state["updated_at"] = float(now_ts)
    previous_status = str(state.get("status") or "active")
    _mark_terminal_if_complete(state)
    if previous_status != "terminal" and str(state.get("status") or "") == "terminal":
        _commit_terminal_baseline(state)
    return state, added, started_new


def claim_item(
    raw: Mapping[str, Any] | None,
    *,
    entity_id: int,
    eligible_ids: Iterable[object] | None = None,
    blocked_ids: Iterable[object] | None = None,
    running_ids: Iterable[object] | None = None,
    now_ts: float,
) -> tuple[dict[str, object], bool]:
    state = normalize_state(raw)
    sid = int(entity_id)
    if str(state.get("status") or "active") != "active":
        return state, False
    items = set(normalize_ids(state.get("items")))
    blocked = set(normalize_ids(blocked_ids))
    running = set(normalize_ids([*normalize_ids(state.get("running")), *normalize_ids(running_ids)]))
    eligible = set(normalize_ids(eligible_ids)) if eligible_ids is not None else items
    claimed = set(normalize_ids(state.get("claimed")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    ok = sid in items and sid in eligible and sid not in blocked and sid not in finished and sid not in claimed and sid not in running
    if ok:
        state["claimed"] = [*normalize_ids(state.get("claimed")), sid]
        state["updated_at"] = float(now_ts)
    return state, ok


def mark_running(raw: Mapping[str, Any] | None, *, entity_id: int, now_ts: float) -> dict[str, object]:
    state = normalize_state(raw)
    sid = int(entity_id)
    if sid in set(normalize_ids(state.get("items"))):
        state["claimed"] = normalize_ids([*state.get("claimed", []), sid])
        state["running"] = normalize_ids([*state.get("running", []), sid])
        state["updated_at"] = float(now_ts)
        state["status"] = "active"
    return state


def release_claim(raw: Mapping[str, Any] | None, *, entity_id: int, now_ts: float) -> dict[str, object]:
    state = normalize_state(raw)
    previous_status = str(state.get("status") or "active")
    sid = int(entity_id)
    state["claimed"] = [x for x in normalize_ids(state.get("claimed")) if x != sid]
    state["running"] = [x for x in normalize_ids(state.get("running")) if x != sid]
    state["updated_at"] = float(now_ts)
    _mark_terminal_if_complete(state)
    if previous_status != "terminal" and str(state.get("status") or "") == "terminal":
        _commit_terminal_baseline(state)
    return state


def mark_terminal(
    raw: Mapping[str, Any] | None,
    *,
    entity_id: int,
    success: bool,
    note: str | None = None,
    now_ts: float,
) -> dict[str, object]:
    state = normalize_state(raw)
    previous_status = str(state.get("status") or "active")
    sid = int(entity_id)
    state["claimed"] = [x for x in normalize_ids(state.get("claimed")) if x != sid]
    state["running"] = [x for x in normalize_ids(state.get("running")) if x != sid]
    state["done"] = [x for x in normalize_ids([*state.get("done", []), sid]) if success or x != sid]
    if success:
        state["errors"] = [x for x in normalize_ids(state.get("errors")) if x != sid]
    else:
        state["errors"] = normalize_ids([*state.get("errors", []), sid])
        if note:
            notes = dict(state.get("error_notes") or {})
            notes[str(sid)] = str(note)[:500]
            state["error_notes"] = notes
    state["updated_at"] = float(now_ts)
    _mark_terminal_if_complete(state)
    if previous_status != "terminal" and str(state.get("status") or "") == "terminal":
        _commit_terminal_baseline(state)
    return state
