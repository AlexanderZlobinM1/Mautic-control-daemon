from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from typing import Any

_SERIALIZED_LEADLIST_FIELD_RE = re.compile(
    r's:\d+:"field";s:\d+:"leadlist"',
    re.IGNORECASE,
)
_SERIALIZED_FILTER_KEY_RE = re.compile(
    r's:\d+:"filter";',
    re.IGNORECASE,
)
_SERIALIZED_NUMERIC_STRING_RE = re.compile(r's:\d+:"(\d+)"')
_SERIALIZED_INT_RE = re.compile(r"i:(\d+);")


def extract_leadlist_filter_segment_ids(filters: object) -> set[int]:
    raw = str(filters or "")
    if "leadlist" not in raw:
        return set()
    out: set[int] = set()
    for match in _SERIALIZED_LEADLIST_FIELD_RE.finditer(raw):
        chunk = raw[match.start() : match.start() + 1800]
        next_field = chunk.find('s:5:"field";', len(match.group(0)))
        if next_field > 0:
            chunk = chunk[:next_field]
        filter_match = _SERIALIZED_FILTER_KEY_RE.search(chunk)
        if not filter_match:
            continue
        value_chunk = chunk[filter_match.end() : filter_match.end() + 900]
        string_ids: set[int] = set()
        for found in _SERIALIZED_NUMERIC_STRING_RE.findall(value_chunk):
            try:
                sid = int(found)
            except ValueError:
                continue
            if sid > 0:
                string_ids.add(sid)
        if string_ids:
            out.update(string_ids)
            continue
        for found in _SERIALIZED_INT_RE.findall(value_chunk):
            try:
                sid = int(found)
            except ValueError:
                continue
            if sid > 0:
                out.add(sid)
    return out


def segment_dependency_maps(rows: list[dict[str, object]]) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    children_by_parent: dict[int, set[int]] = {}
    parents_by_child: dict[int, set[int]] = {}
    for row in rows:
        try:
            child_id = int(row.get("id") or 0)
        except Exception:
            child_id = 0
        if child_id <= 0:
            continue
        parents = {sid for sid in extract_leadlist_filter_segment_ids(row.get("filters")) if sid != child_id}
        if not parents:
            continue
        parents_by_child[child_id] = set(parents)
        for parent_id in parents:
            children_by_parent.setdefault(parent_id, set()).add(child_id)
    return children_by_parent, parents_by_child


def dependent_segment_closure(seed_ids: set[int], children_by_parent: dict[int, set[int]]) -> set[int]:
    out: set[int] = set()
    queue = deque(int(x) for x in seed_ids if int(x) > 0)
    while queue:
        parent_id = int(queue.popleft())
        for child_id in sorted(children_by_parent.get(parent_id, set())):
            if child_id in out:
                continue
            out.add(child_id)
            queue.append(child_id)
    return out


def dependency_segment_closure(seed_ids: set[int], parents_by_child: dict[int, set[int]]) -> set[int]:
    out: set[int] = set()
    queue = deque(int(x) for x in seed_ids if int(x) > 0)
    while queue:
        child_id = int(queue.popleft())
        for parent_id in sorted(parents_by_child.get(child_id, set())):
            if parent_id in out:
                continue
            out.add(parent_id)
            queue.append(parent_id)
    return out


def suppress_mautic_cascade_dependencies(
    candidate_ids: list[int],
    parents_by_child: dict[int, set[int]],
) -> tuple[list[int], set[int]]:
    """
    Remove explicit parent launches already covered by Mautic 7 segment rebuilds.

    Mautic 7 rebuilds leadlist-filter dependencies recursively before the
    requested segment. If MCD also schedules those parents as separate segment
    tasks in the same planning cycle, a child can rebuild its parent again and
    create repeated parent/child follow-up work.
    """
    ordered = list(dict.fromkeys(int(x) for x in candidate_ids if int(x) > 0))
    if not ordered or not parents_by_child:
        return ordered, set()
    candidate_set = set(ordered)
    covered = dependency_segment_closure(candidate_set, parents_by_child) & candidate_set
    if not covered:
        return ordered, set()
    return [sid for sid in ordered if sid not in covered], covered


def _timestamp_key(value: object) -> tuple[int, str]:
    raw = str(value or "").strip()
    if not raw:
        return (0, "")
    normalized = raw.replace("T", " ").replace("Z", "+00:00")
    try:
        return (1, datetime.fromisoformat(normalized).isoformat())
    except Exception:
        # MySQL DATETIME strings sort correctly lexicographically.
        return (1, raw)


def stale_dependent_segment_closure(
    seed_ids: set[int],
    rows: list[dict[str, object]],
    children_by_parent: dict[int, set[int]],
) -> set[int]:
    """
    Return dependent segments that still need a rebuild after their parent.

    A recently finished parent remains in memory for a safety window. Without
    this freshness check, the scheduler re-queues the same children on every
    tick for the whole window and can starve unrelated due segments.
    """
    by_id: dict[int, dict[str, object]] = {}
    for row in rows:
        try:
            sid = int(row.get("id") or 0)
        except Exception:
            sid = 0
        if sid > 0:
            by_id[sid] = row

    out: set[int] = set()
    queue = deque(int(x) for x in seed_ids if int(x) > 0)
    while queue:
        parent_id = int(queue.popleft())
        parent_built = _timestamp_key((by_id.get(parent_id) or {}).get("last_built_date"))
        for child_id in sorted(children_by_parent.get(parent_id, set())):
            if child_id in out:
                continue
            child_built = _timestamp_key((by_id.get(child_id) or {}).get("last_built_date"))
            if parent_built[0] and child_built >= parent_built:
                continue
            out.add(child_id)
            queue.append(child_id)
    return out


def segment_dependency_blocked_ids(
    *,
    root: str,
    candidate_ids: set[int],
    parents_by_child: dict[int, set[int]],
    running: dict[str, Any],
    recently_finished: set[int],
) -> set[int]:
    if not candidate_ids or not parents_by_child:
        return set()
    running_segments = {
        int(task.entity_id)
        for task in running.values()
        if getattr(task, "root", None) == root
        and getattr(task, "entity_id", None) is not None
        and getattr(task, "task_type", None) in {"segment", "segment_sql"}
    }
    blocked: set[int] = set()
    for child_id in candidate_ids:
        parents = parents_by_child.get(int(child_id), set())
        if not parents:
            continue
        if any(parent_id in running_segments for parent_id in parents):
            blocked.add(int(child_id))
            continue
        if any(parent_id in candidate_ids and parent_id not in recently_finished for parent_id in parents):
            blocked.add(int(child_id))
    return blocked
