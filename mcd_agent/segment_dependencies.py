from __future__ import annotations

import re
from io import BytesIO
from collections import deque
from datetime import datetime
from typing import Any, Iterable

import phpserialize

_SERIALIZED_LEADLIST_FIELD_RE = re.compile(
    r's:\d+:"field";s:\d+:"leadlist"',
    re.IGNORECASE,
)
_SERIALIZED_FILTER_KEY_RE = re.compile(
    r's:\d+:"filter";',
    re.IGNORECASE,
)


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
        # Decode exactly the filter value. Scanning integer tokens also reads
        # array keys and the next clause's index as segment dependencies.
        try:
            value = phpserialize.load(
                BytesIO(chunk[filter_match.end() :].encode("utf-8")),
                decode_strings=True,
            )
        except (ValueError, TypeError):
            continue
        values = value.values() if isinstance(value, dict) else [value]
        for found in values:
            try:
                sid = int(found)
            except (ValueError, TypeError):
                continue
            if sid > 0:
                out.add(sid)
    return out


def segment_dependency_maps(rows: list[dict[str, object]]) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    children_by_parent: dict[int, set[int]] = {}
    parents_by_child: dict[int, set[int]] = {}
    valid_ids: set[int] = set()
    for row in rows:
        try:
            child_id = int(row.get("id") or 0)
        except Exception:
            child_id = 0
        if child_id > 0:
            valid_ids.add(child_id)
    for row in rows:
        try:
            child_id = int(row.get("id") or 0)
        except Exception:
            child_id = 0
        if child_id <= 0:
            continue
        parents = {
            sid
            for sid in extract_leadlist_filter_segment_ids(row.get("filters"))
            if sid != child_id and sid in valid_ids
        }
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


def terminal_dependent_segment_ids(seed_id: int, children_by_parent: dict[int, set[int]]) -> set[int]:
    sid = int(seed_id or 0)
    if sid <= 0:
        return set()
    descendants = dependent_segment_closure({sid}, children_by_parent)
    if not descendants:
        return {sid}
    terminals = {child_id for child_id in descendants if not children_by_parent.get(child_id)}
    return terminals or {sid}


def mautic7_terminal_segment_plan(
    candidate_ids: list[int],
    children_by_parent: dict[int, set[int]],
) -> tuple[list[int], set[int]]:
    """
    Preserve every published due segment in the Mautic 7 plan.

    A terminal segment command can traverse leadlist-filter dependencies, but
    that traversal is not evidence that each referenced customer-visible lead
    list was rebuilt. Dependency maps still serialize related work; they must
    never suppress a due segment from the scheduler plan.
    """
    ordered = list(dict.fromkeys(int(x) for x in candidate_ids if int(x) > 0))
    return ordered, set()


def dependency_expanded_segment_plan(
    candidate_ids: list[int],
    parents_by_child: dict[int, set[int]],
) -> list[int]:
    """
    Expand Mautic <=6 segment work to include dependencies before children.

    Older Mautic branches do not rebuild leadlist-filter dependencies when a
    single segment id is requested, so MCD has to emulate the chain order.
    """
    seeds = list(dict.fromkeys(int(x) for x in candidate_ids if int(x) > 0))
    if not seeds or not parents_by_child:
        return seeds
    planned_set = set(seeds) | dependency_segment_closure(set(seeds), parents_by_child)
    outgoing: dict[int, set[int]] = {sid: set() for sid in planned_set}
    indeg: dict[int, int] = {sid: 0 for sid in planned_set}
    for child_id in sorted(planned_set):
        for parent_id in sorted(parents_by_child.get(child_id, set())):
            if parent_id not in planned_set:
                continue
            if child_id not in outgoing[parent_id]:
                outgoing[parent_id].add(child_id)
                indeg[child_id] += 1

    queue = sorted([sid for sid in planned_set if indeg[sid] == 0])
    ordered: list[int] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing.get(current, set())):
            indeg[nxt] = max(0, indeg[nxt] - 1)
            if indeg[nxt] == 0 and nxt not in queue and nxt not in ordered:
                queue.append(nxt)
    if len(ordered) == len(planned_set):
        return ordered
    return ordered + [sid for sid in sorted(planned_set) if sid not in set(ordered)]


def dependency_execution_chains(
    candidate_ids: list[int],
    parents_by_child: dict[int, set[int]],
    children_by_parent: dict[int, set[int]],
    *,
    mautic_major: int,
    eligible_ids: Iterable[int] | None = None,
) -> list[list[int]]:
    """Partition one dispatch snapshot into executable dependency chains.

    Mautic 6 and older do not rebuild a lead-list dependency closure when a
    child is requested with ``-i``.  A connected closure therefore owns one
    scheduler slot and is executed in one topological worker command.  The
    input is already the durable pending projection, so a parent that is
    already terminal or running is intentionally not pulled into a later
    chain; the normal dependency guard will release the child afterward.
    ``eligible_ids`` optionally projects that pending graph onto one durable
    tier.  This is required when a full-plan ring contains a fresh remaining
    descendant: it must not be pulled into an important parent's worker merely
    because the graph component is connected.

    Mautic 7 has native dependency traversal.  Independent components keep
    independent workers, but one connected overlap owns one worker for its
    complete durable closure.  The scheduler executes only the component's
    native leaf roots sequentially; already-covered ancestors are represented
    in the chain for claim/terminal accounting and are excluded from later
    native roots by the runtime dispatcher.
    """

    eligible: set[int] | None = None
    if eligible_ids is not None:
        eligible = set()
        for raw_value in eligible_ids:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                eligible.add(value)

    ordered: list[int] = []
    for raw_value in candidate_ids:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0 and (eligible is None or value in eligible) and value not in ordered:
            ordered.append(value)
    if not ordered:
        return []
    candidate_set = set(ordered)
    position = {sid: index for index, sid in enumerate(ordered)}
    parents = parents_by_child or {}
    children = children_by_parent or {}
    components: list[list[int]] = []
    unseen = set(candidate_set)
    while unseen:
        seed = min(unseen, key=lambda sid: position[sid])
        component: set[int] = set()
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            if current in component or current not in candidate_set:
                continue
            component.add(current)
            neighbours = set(parents.get(current, set())) | set(children.get(current, set()))
            frontier.extend(value for value in neighbours if value in candidate_set and value not in component)
        unseen.difference_update(component)
        components.append(sorted(component, key=lambda sid: position[sid]))

    chains: list[list[int]] = []
    for component in sorted(components, key=lambda values: position[values[0]]):
        component_set = set(component)
        if int(mautic_major or 0) >= 7:
            native_roots = [
                sid
                for sid in component
                if not (set(children.get(sid, set())) & component_set)
            ]
            if not native_roots:
                # Cyclic or incomplete dependency data must still remain one
                # durable ownership unit; the runtime command will fail
                # closed rather than silently creating overlapping workers.
                native_roots = [component[0]]
            covered_members = [sid for sid in component if sid not in native_roots]
            chains.append([*native_roots, *covered_members])
            continue
        outgoing: dict[int, set[int]] = {sid: set() for sid in component}
        indegree: dict[int, int] = {sid: 0 for sid in component}
        for child in component:
            for parent in parents.get(child, set()):
                if parent not in component_set or child in outgoing[parent]:
                    continue
                outgoing[parent].add(child)
                indegree[child] += 1
        ready = sorted(
            (sid for sid in component if indegree[sid] == 0),
            key=lambda sid: position[sid],
        )
        chain: list[int] = []
        while ready:
            current = ready.pop(0)
            chain.append(current)
            for child in sorted(outgoing[current], key=lambda sid: position[sid]):
                indegree[child] = max(0, indegree[child] - 1)
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=lambda sid: position[sid])
        if len(chain) != len(component):
            # Invalid/cyclic dependency data must not drop work. Preserve the
            # durable observed order for the unresolved remainder; the normal
            # dependency conflict guard remains fail-closed for the launch.
            chain.extend(sid for sid in component if sid not in set(chain))
        chains.append(chain)
    return chains


def segment_related_ids(
    segment_id: int,
    parents_by_child: dict[int, set[int]],
    children_by_parent: dict[int, set[int]],
) -> set[int]:
    sid = int(segment_id or 0)
    if sid <= 0:
        return set()
    return {sid} | dependency_segment_closure({sid}, parents_by_child) | dependent_segment_closure({sid}, children_by_parent)


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
