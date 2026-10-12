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


def _timestamp(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_active_meta(raw: object) -> dict[str, dict[str, object]]:
    """Normalize restart-safe metadata for ordinary claimed items.

    The metadata is deliberately small.  It records when a claim was made,
    the last independent liveness observation, and the cycle identity of the
    worker that produced that observation.  It is not a second queue: the
    durable ``claimed``/``running`` lists remain the source of truth.
    """

    source = raw if isinstance(raw, Mapping) else {}
    normalized: dict[str, dict[str, object]] = {}
    for raw_sid, raw_meta in source.items():
        try:
            sid = int(raw_sid)
        except (TypeError, ValueError):
            continue
        if sid <= 0 or not isinstance(raw_meta, Mapping):
            continue
        meta: dict[str, object] = {}
        for field in (
            "claimed_at",
            "running_at",
            "last_independent_at",
            "orphan_since",
            "started_at",
        ):
            value = _timestamp(raw_meta.get(field))
            if value is not None:
                meta[field] = value
        cycle_id = str(raw_meta.get("cycle_id") or "").strip()
        if cycle_id:
            meta["cycle_id"] = cycle_id
        if meta:
            normalized[str(sid)] = meta
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


def _evidence_generation_mismatch(
    evidence: object,
    *,
    cycle_id: str,
    admitted_at: float,
) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    observed_cycle_id = str(evidence.get("cycle_id") or "").strip()
    if observed_cycle_id and cycle_id and observed_cycle_id != cycle_id:
        return True
    started_at = _evidence_timestamp(evidence, "started_at", "admitted_at")
    return bool(
        started_at is not None
        and admitted_at > 0
        and started_at < admitted_at
    )


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
    include_fresh_remaining: bool = False,
    groups: dict[str, list[int]] | None = None,
) -> list[int]:
    """Build the deterministic ordinary snapshot.

    The two static MCC lanes are excluded by ``static_priority_ids``. The
    remaining due candidates are grouped as never-built, changed, then stale;
    currently-fresh published rows are discovery-only and are not admitted in
    lazy mode. A full published snapshot can set ``include_fresh_remaining``
    to append those remaining rows after the due groups, preserving the same
    deterministic observed order. This keeps full and lazy planning on one
    classifier instead of maintaining two subtly different orderings.
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
    ordered_due = sorted(
        due_candidates,
        key=lambda sid: (int(category(sid) or 0), observed_position[sid]),
    )
    if not include_fresh_remaining:
        if groups is not None:
            groups["important"] = list(ordered_due)
            groups["remaining"] = []
        return ordered_due
    due_set = set(ordered_due)
    # Full scans intentionally reconcile every published definition. Fresh
    # rows are appended only after never-built/changed/stale work, retaining
    # the category priority while ensuring a mixed full snapshot cannot drop
    # the fresh tail.
    fresh_remaining = [sid for sid in candidates if sid not in due_set]
    if groups is not None:
        groups["important"] = list(ordered_due)
        groups["remaining"] = list(fresh_remaining)
    return [*ordered_due, *fresh_remaining]


def active_cycle_follow_up_rows(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    admitted_at: object,
) -> list[Mapping[str, Any]]:
    """Keep only safe admissions discovered while an ordinary cycle runs.

    A segment created after the cycle admission belongs in that cycle's FIFO
    follow-up. A pre-existing never-built segment is also safe to add because
    it was not represented by the completed baseline. A built segment whose
    ``date_modified`` changed after admission is deliberately excluded: it is
    planned by the next terminal snapshot, so an in-flight cycle keeps its
    deterministic order and does not repeatedly chase one edited segment.
    """

    cutoff = _timestamp_seconds(admitted_at)
    result: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        sid = _row_id(row)
        if sid <= 0 or sid in seen:
            continue
        built = _row_timestamp(row, "last_built_date", "last_built", "built_ts")
        created = _row_timestamp(row, "date_added", "created_ts")
        if built is None or (cutoff is not None and created is not None and created >= cutoff):
            result.append(row)
            seen.add(sid)
    return result


def _cycle_id(now_ts: float, items: list[int]) -> str:
    raw = f"{int(float(now_ts) * 1000)}:{','.join(str(x) for x in items)}"
    return sha1(raw.encode("utf-8")).hexdigest()[:20]


def _plan_mode_value(lazy_mode: bool | None) -> str:
    if lazy_mode is True:
        return "lazy"
    if lazy_mode is False:
        return "full"
    return ""


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
    important_ids: Iterable[object] | None = None,
    remaining_ids: Iterable[object] | None = None,
    lazy_mode: bool | None = None,
) -> dict[str, object]:
    cycle_snapshot = _normalize_baseline(cycle_baseline)
    observed = _normalize_baseline(
        observed_snapshot if observed_snapshot is not None else cycle_snapshot
    )
    item_set = set(items)
    important = [sid for sid in normalize_ids(important_ids) if sid in item_set]
    remaining = [
        sid
        for sid in normalize_ids(remaining_ids)
        if sid in item_set and sid not in set(important)
    ]
    if important_ids is None and remaining_ids is None:
        # State written by older callers remains safe: before a planner has
        # supplied explicit tier metadata, every admitted item is important.
        important = list(items)
    elif not important and not remaining and items:
        important = list(items)
    # A malformed or partial planner result must never make an item vanish
    # from the durable tier projection.
    unclassified = [sid for sid in items if sid not in set(important) and sid not in set(remaining)]
    important.extend(unclassified)
    plan_mode = _plan_mode_value(lazy_mode)
    if not plan_mode and remaining:
        # A pre-1.5.139 caller has no explicit mode argument, but a durable
        # fresh tail is unambiguously a full-mode admission.
        plan_mode = "full"
    return {
        "schema": SCHEMA,
        "cycle_id": _cycle_id(now_ts, items),
        "status": "active" if items else "terminal",
        "items": list(items),
        "follow_up": [],
        "important_items": normalize_ids(important),
        "remaining_items": normalize_ids(remaining),
        "plan_mode": plan_mode,
        "importance_classified": True,
        "claimed": [],
        "running": [],
        "active_meta": {},
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
    follow_up = [sid for sid in normalize_ids(source.get("follow_up")) if sid not in set(items)]
    item_set = set(items) | set(follow_up)
    importance_classified = "important_items" in source or "remaining_items" in source
    important_items = [sid for sid in normalize_ids(source.get("important_items")) if sid in item_set]
    remaining_items = [
        sid
        for sid in normalize_ids(source.get("remaining_items"))
        if sid in item_set and sid not in set(important_items)
    ]
    if not importance_classified and item_set:
        # Legacy state has no tier metadata. Treat its pending projection as
        # important until the next planner persists an explicit split.
        important_items = [*items, *follow_up]
        remaining_items = []
    plan_mode = str(source.get("plan_mode") or "").strip().lower()
    if plan_mode not in {"lazy", "full"}:
        plan_mode = "full" if remaining_items else ""
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
    active_meta = _normalize_active_meta(source.get("active_meta"))
    active_ids = set(claimed) | set(running)
    active_meta = {
        str(sid): dict(meta)
        for sid, meta in active_meta.items()
        if int(sid) in active_ids
    }
    return {
        "schema": SCHEMA,
        "cycle_id": str(source.get("cycle_id") or ""),
        "status": status,
        "items": items,
        "follow_up": follow_up,
        "important_items": normalize_ids(important_items),
        "remaining_items": normalize_ids(remaining_items),
        "plan_mode": plan_mode,
        "importance_classified": bool(importance_classified),
        "claimed": claimed,
        "running": running,
        "active_meta": active_meta,
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
    items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    active = set(normalize_ids(state.get("claimed"))) | set(normalize_ids(state.get("running")))
    if items and items <= finished and not active:
        state["status"] = "terminal"
    elif not items:
        state["status"] = "terminal"
    else:
        state["status"] = "active"


def reconcile_active_claims(
    raw: Mapping[str, Any] | None,
    *,
    active_evidence: Mapping[object, object] | None = None,
    now_ts: float,
    orphan_grace_sec: int = ORPHAN_GRACE_SEC,
) -> tuple[dict[str, object], list[int]]:
    """Reconcile ordinary claims without reusing a prior cycle's terminal.

    A persisted claim is coordination state, not proof that a worker still
    exists.  Current TaskStore/executor/process evidence keeps it adopted.  A
    marker from another cycle, or a terminal marker from before this cycle was
    admitted, releases the claim in place but never marks the item done.  A
    claim with no independent evidence gets one bounded grace window so a
    restart can adopt a worker that is between scheduler observations.
    """

    state = normalize_state(raw)
    evidence = active_evidence if isinstance(active_evidence, Mapping) else {}
    current_cycle_id = str(state.get("cycle_id") or "").strip()
    admitted_at = float(state.get("admitted_at") or 0.0)
    grace = max(1, int(orphan_grace_sec or ORPHAN_GRACE_SEC))
    active_meta = _normalize_active_meta(state.get("active_meta"))
    released: list[int] = []
    active_ids = normalize_ids([*state.get("claimed", []), *state.get("running", [])])
    claimed = set(normalize_ids(state.get("claimed")))
    running = set(normalize_ids(state.get("running")))
    for sid in active_ids:
        item_evidence = evidence.get(sid, evidence.get(str(sid)))
        prior = dict(active_meta.get(str(sid), {}))
        generation_mismatch = _evidence_generation_mismatch(
            item_evidence,
            cycle_id=current_cycle_id,
            admitted_at=admitted_at,
        )
        status = (
            str(item_evidence.get("status") or "").strip().lower()
            if isinstance(item_evidence, Mapping)
            else ""
        )
        if _evidence_is_live(item_evidence) and not generation_mismatch:
            independent_at = _evidence_timestamp(
                item_evidence,
                "independent_at",
                "last_independent_at",
                "observed_at",
            )
            prior.setdefault("claimed_at", float(now_ts))
            if sid in running:
                prior.setdefault("running_at", float(now_ts))
            prior["last_independent_at"] = float(
                independent_at
                if independent_at is not None
                else prior.get("last_independent_at") or now_ts
            )
            observed_cycle_id = str(
                item_evidence.get("cycle_id") or current_cycle_id
            ).strip()
            if observed_cycle_id:
                prior["cycle_id"] = observed_cycle_id
            started_at = _evidence_timestamp(item_evidence, "started_at")
            if started_at is not None:
                prior["started_at"] = started_at
            prior.pop("orphan_since", None)
            active_meta[str(sid)] = prior
            continue

        # A terminal marker is an acknowledgement that the old executor is
        # finished, not evidence that this cycle's item succeeded.  Release
        # it so the current cycle can claim and execute it again.
        if generation_mismatch or status in {"terminal", "done", "error", "lost"}:
            released.append(sid)
            claimed.discard(sid)
            running.discard(sid)
            active_meta.pop(str(sid), None)
            continue

        orphan_since = _timestamp(prior.get("orphan_since"))
        if orphan_since is None:
            orphan_since = (
                _timestamp(prior.get("last_independent_at"))
                or _timestamp(prior.get("running_at"))
                or _timestamp(prior.get("claimed_at"))
                or _evidence_timestamp(item_evidence, "started_at")
                or float(now_ts)
            )
            prior["orphan_since"] = float(orphan_since)
        if float(now_ts) - float(orphan_since) >= grace:
            released.append(sid)
            claimed.discard(sid)
            running.discard(sid)
            active_meta.pop(str(sid), None)
        else:
            active_meta[str(sid)] = prior

    state["claimed"] = normalize_ids(claimed)
    state["running"] = normalize_ids(running)
    state["active_meta"] = {
        str(sid): dict(meta)
        for sid, meta in active_meta.items()
        if sid in {str(value) for value in active_ids}
        and int(sid) in set(state["claimed"]) | set(state["running"])
    }
    state["updated_at"] = float(now_ts)
    _mark_terminal_if_complete(state)
    return state, released


def cycle_item_tier(raw: Mapping[str, Any] | None, entity_id: object) -> str | None:
    """Return the durable tier for one admitted ordinary item."""

    state = normalize_state(raw)
    try:
        sid = int(entity_id)
    except (TypeError, ValueError):
        return None
    if sid in set(normalize_ids(state.get("important_items"))):
        return "important"
    if sid in set(normalize_ids(state.get("remaining_items"))):
        return "remaining"
    items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
    return "important" if sid in items else None


def pending_important_ids(raw: Mapping[str, Any] | None) -> set[int]:
    """Return ready, unassigned important IDs, excluding claimed/running work."""

    state = normalize_state(raw)
    important = set(normalize_ids(state.get("important_items")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    active = set(normalize_ids(state.get("claimed"))) | set(normalize_ids(state.get("running")))
    return important - finished - active


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
    all_items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
    if not all_items and not errors:
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
    if not errors and (not all_items or done):
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

    items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
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
    item_ids = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
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
    excluded_ids: Iterable[object] | None = None,
    now_ts: float,
    source_snapshot: Mapping[object, object] | None = None,
    important_ids: Iterable[object] | None = None,
    remaining_ids: Iterable[object] | None = None,
    lazy_mode: bool | None = None,
) -> tuple[dict[str, object], list[int], bool]:
    """Reconcile planning observations without resetting an active cycle.

    Returns ``(state, added_ids, started_new_cycle)``. Active cycles never
    reorder existing work. New IDs discovered after admission enter the
    durable follow-up FIFO; the frozen ``items`` order remains unchanged.
    Claimed, running, and terminal items retain their positions.
    When ``lazy_mode`` changes while a cycle is active, the policy change is
    held for that root's next cycle: no fresh rows from the new mode are
    appended to ``follow_up``. Already-admitted important/remaining work is
    still dispatchable from the persisted cursor.
    """

    excluded = set(normalize_ids(excluded_ids))
    planned = [sid for sid in normalize_ids(planned_ids) if sid not in excluded]
    never = {sid for sid in normalize_ids(never_built) if sid not in excluded}
    planned_set = set(planned)
    supplied_important = [sid for sid in normalize_ids(important_ids) if sid in planned_set]
    supplied_remaining = [
        sid
        for sid in normalize_ids(remaining_ids)
        if sid in planned_set and sid not in set(supplied_important)
    ]
    explicit_tiers = important_ids is not None or remaining_ids is not None
    running = set(normalize_ids(running_ids))
    current = normalize_state(raw)
    current_items = normalize_ids(current.get("items"))
    current_follow_up = normalize_ids(current.get("follow_up"))
    current_status = str(current.get("status") or "active")
    finished = set(normalize_ids(current.get("done"))) | set(normalize_ids(current.get("errors")))
    active_claims = set(normalize_ids(current.get("claimed"))) | set(normalize_ids(current.get("running")))
    all_current_items = set(current_items) | set(current_follow_up)
    cycle_complete = bool(all_current_items) and all_current_items <= finished and not active_claims
    started_new = not all_current_items or current_status == "terminal" or cycle_complete
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
            important_ids=supplied_important if explicit_tiers else None,
            remaining_ids=supplied_remaining if explicit_tiers else None,
            lazy_mode=lazy_mode,
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
        existing = set(current_items) | set(current_follow_up)
        candidate_order = normalize_ids([*planned, *normalize_ids(never)])
        requested_mode = _plan_mode_value(lazy_mode)
        current_mode = str(state.get("plan_mode") or "").strip().lower()
        if current_mode not in {"lazy", "full"}:
            current_mode = "full" if normalize_ids(state.get("remaining_items")) else ""
        mode_changed = bool(requested_mode and current_mode and requested_mode != current_mode)
        # A UID toggle is a next-cycle policy change.  Do not turn fresh
        # rows from the new mode into follow-up work in the frozen active
        # cycle: the dispatcher must finish the already-admitted cursor, and
        # the next root-local cycle is the first one owned by the new mode.
        additions = [] if mode_changed else [
            sid for sid in candidate_order if sid not in existing and sid not in running
        ]
        if current_mode:
            state["plan_mode"] = current_mode
        elif requested_mode:
            state["plan_mode"] = requested_mode
        # The ordinary snapshot is frozen for the life of the active cycle.
        # New IDs discovered by a later read are durable follow-up FIFO work,
        # so a late segment cannot rewrite the frozen order.
        state["items"] = list(current_items)
        state["follow_up"] = normalize_ids([*current_follow_up, *additions])
        if explicit_tiers:
            existing_items = set(current_items) | set(current_follow_up)
            current_important = set(normalize_ids(state.get("important_items")))
            current_remaining = set(normalize_ids(state.get("remaining_items")))
            if not bool(state.get("importance_classified")):
                current_important = set(existing_items)
                current_remaining = set()
            current_important.update(sid for sid in additions if sid in set(supplied_important))
            current_remaining.update(sid for sid in additions if sid in set(supplied_remaining))
            classified_additions = set(supplied_important) | set(supplied_remaining)
            current_important.update(sid for sid in additions if sid not in classified_additions)
            current_remaining.difference_update(current_important)
            state["important_items"] = normalize_ids(current_important)
            state["remaining_items"] = normalize_ids(current_remaining)
            state["importance_classified"] = True
        else:
            # Callers that have not yet supplied planner tiers still retain
            # the historical follow-up precedence: a newly discovered ID is
            # important until a planner explicitly classifies it as fresh
            # remaining work.
            state["important_items"] = normalize_ids(
                [*state.get("important_items", []), *additions]
            )
        added = additions

    state_items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
    state["running"] = [sid for sid in normalize_ids(running) if sid in state_items]
    running_set = set(normalize_ids(state.get("running")))
    state["claimed"] = [
        sid
        for sid in normalize_ids(state.get("claimed"))
        if sid in running_set and sid not in set(normalize_ids(state.get("done"))) and sid not in set(normalize_ids(state.get("errors")))
    ]
    state["claimed"] = normalize_ids([*state.get("claimed", []), *state["running"]])
    active_ids = set(state["claimed"]) | set(state["running"])
    state["active_meta"] = {
        str(sid): dict(meta)
        for sid, meta in _normalize_active_meta(state.get("active_meta")).items()
        if int(sid) in active_ids
    }
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
    items = set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up")))
    blocked = set(normalize_ids(blocked_ids))
    running = set(normalize_ids([*normalize_ids(state.get("running")), *normalize_ids(running_ids)]))
    eligible = set(normalize_ids(eligible_ids)) if eligible_ids is not None else items
    claimed = set(normalize_ids(state.get("claimed")))
    finished = set(normalize_ids(state.get("done"))) | set(normalize_ids(state.get("errors")))
    ok = sid in items and sid in eligible and sid not in blocked and sid not in finished and sid not in claimed and sid not in running
    if ok:
        state["claimed"] = [*normalize_ids(state.get("claimed")), sid]
        active_meta = _normalize_active_meta(state.get("active_meta"))
        item_meta = dict(active_meta.get(str(sid), {}))
        item_meta.setdefault("claimed_at", float(now_ts))
        item_meta.pop("orphan_since", None)
        active_meta[str(sid)] = item_meta
        state["active_meta"] = active_meta
        state["updated_at"] = float(now_ts)
    return state, ok


def mark_running(raw: Mapping[str, Any] | None, *, entity_id: int, now_ts: float) -> dict[str, object]:
    state = normalize_state(raw)
    sid = int(entity_id)
    if sid in set(normalize_ids(state.get("items"))) | set(normalize_ids(state.get("follow_up"))):
        state["claimed"] = normalize_ids([*state.get("claimed", []), sid])
        state["running"] = normalize_ids([*state.get("running", []), sid])
        active_meta = _normalize_active_meta(state.get("active_meta"))
        item_meta = dict(active_meta.get(str(sid), {}))
        item_meta.setdefault("claimed_at", float(now_ts))
        item_meta.setdefault("running_at", float(now_ts))
        item_meta.setdefault("last_independent_at", float(now_ts))
        item_meta.pop("orphan_since", None)
        active_meta[str(sid)] = item_meta
        state["active_meta"] = active_meta
        state["updated_at"] = float(now_ts)
        state["status"] = "active"
    return state


def release_claim(raw: Mapping[str, Any] | None, *, entity_id: int, now_ts: float) -> dict[str, object]:
    state = normalize_state(raw)
    previous_status = str(state.get("status") or "active")
    sid = int(entity_id)
    state["claimed"] = [x for x in normalize_ids(state.get("claimed")) if x != sid]
    state["running"] = [x for x in normalize_ids(state.get("running")) if x != sid]
    active_meta = _normalize_active_meta(state.get("active_meta"))
    active_meta.pop(str(sid), None)
    state["active_meta"] = active_meta
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
    active_meta = _normalize_active_meta(state.get("active_meta"))
    active_meta.pop(str(sid), None)
    state["active_meta"] = active_meta
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
