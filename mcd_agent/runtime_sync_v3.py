"""Generic MCD side of the opt-in MCC desired-runtime sync v3 contract."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping

SCHEMA = "mcc-desired-runtime-sync-v3"
PROTOCOL = 3
STATE_KEY = "mcc_runtime_sync_v3"


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _value(mapping: Mapping[str, Any], key: str) -> Any:
    return mapping.get(key)


def _changed(base: Mapping[str, Any], current: Mapping[str, Any], key: str) -> bool:
    return (key in base, _value(base, key)) != (key in current, _value(current, key))


def normalize_groups(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("semantic_conflict_groups_must_be_list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("semantic_conflict_group_must_be_object")
        group_id = str(item.get("id") or "").strip()
        keys = item.get("keys")
        if not group_id or not isinstance(keys, list) or not keys:
            raise ValueError("semantic_conflict_group_invalid")
        unique = list(dict.fromkeys(str(key).strip() for key in keys))
        if group_id in seen or any(not key for key in unique):
            raise ValueError("semantic_conflict_group_invalid")
        seen.add(group_id)
        result.append({"id": group_id, "keys": unique})
    return result


def three_way_merge(
    base: Mapping[str, Any],
    central: Mapping[str, Any],
    host: Mapping[str, Any],
    *,
    semantic_groups: object = None,
) -> dict[str, Any]:
    """Mirror MCC v3 merge semantics for safe local projection and fixtures."""
    groups = normalize_groups(semantic_groups)
    group_for: dict[str, dict[str, Any]] = {}
    for group in groups:
        for key in group["keys"]:
            group_for[key] = group
    keys = set(base) | set(central) | set(host)
    merged: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    handled: set[str] = set()
    for key in sorted(keys):
        if key in handled:
            continue
        group = group_for.get(key)
        group_keys = list(group["keys"]) if group else [key]
        handled.update(group_keys)
        central_changed = any(_changed(base, central, item) for item in group_keys)
        host_changed = any(_changed(base, host, item) for item in group_keys)
        base_values = {item: _value(base, item) for item in group_keys}
        central_values = {item: _value(central, item) for item in group_keys}
        host_values = {item: _value(host, item) for item in group_keys}
        if not central_changed and not host_changed:
            merged.update(base_values)
        elif central_changed and not host_changed:
            merged.update(central_values)
        elif host_changed and not central_changed:
            merged.update(host_values)
        elif central_values == host_values:
            merged.update(central_values)
        else:
            conflicts.append({
                "group_id": group["id"] if group else key,
                "keys": group_keys,
                "base": base_values,
                "central": central_values,
                "host": host_values,
            })
    return {
        "status": "conflict" if conflicts else "merged",
        "merged": merged,
        "conflicts": conflicts,
        "semantic_conflict_groups": groups,
    }


def runtime_changes(base: Mapping[str, Any], current: Mapping[str, Any], *, allowed_keys: set[str] | None = None) -> dict[str, Any]:
    """Return only host-side changes; absent keys are represented by JSON null."""
    keys = set(base) | set(current)
    if allowed_keys is not None:
        keys &= {str(key) for key in allowed_keys}
    changes: dict[str, Any] = {}
    for key in sorted(keys):
        if _changed(base, current, key):
            changes[key] = current.get(key)
    return changes


def intent_id(*, scope_key: str, base_revision: int, base: Mapping[str, Any], changes: Mapping[str, Any], groups: object) -> str:
    payload = {
        "scope": "host",
        "scope_key": str(scope_key),
        "base_revision": int(base_revision),
        "base": dict(base),
        "changes": dict(changes),
        "semantic_conflict_groups": normalize_groups(groups),
    }
    return "mcd-v3-" + hashlib.sha256(canonical_json(payload)).hexdigest()


def desired_runtime_from_response(response: Mapping[str, Any]) -> tuple[int | None, dict[str, Any]]:
    desired = response.get("desired_state")
    if not isinstance(desired, Mapping):
        return None, {}
    revision = desired.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = desired.get("desired_revision")
    revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    runtime = desired.get("runtime_overrides")
    if not isinstance(runtime, Mapping):
        return None, {}
    return revision, dict(runtime) if isinstance(runtime, Mapping) else {}


def conflict_safe_runtime(conflict: Mapping[str, Any], local_runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Project non-ambiguous central changes while preserving conflict keys locally."""
    base = conflict.get("base") if isinstance(conflict.get("base"), Mapping) else {}
    central = conflict.get("central") if isinstance(conflict.get("central"), Mapping) else {}
    host = conflict.get("host") if isinstance(conflict.get("host"), Mapping) else {}
    merge = three_way_merge(base, central, host, semantic_groups=conflict.get("semantic_conflict_groups", []))
    result = dict(merge["merged"])
    # A top-level three-way merge is retained for scalar/legacy fields.  Maps
    # that represent instance settings are merged per UID so an unrelated
    # instance is not lost merely because one sibling cell is ambiguous.
    for key in set(base) | set(central) | set(host):
        if not _is_instance_runtime_map_key(key):
            continue
        result[key] = _merge_instance_map_cells(
            str(key), base.get(key), central.get(key), host.get(key), local_runtime.get(key),
        )
        if not result[key]:
            result.pop(key, None)
    for scope, key, uid in conflict_cells(conflict):
        if scope == "host":
            if key in local_runtime:
                result[key] = deepcopy(local_runtime[key])
            else:
                result.pop(key, None)
            continue
        if uid == "*":
            if key in local_runtime:
                result[key] = deepcopy(local_runtime[key])
            else:
                result.pop(key, None)
            continue
        _set_runtime_cell(result, (scope, key, uid), _get_runtime_cell(local_runtime, (scope, key, uid)))
    return result


def conflict_keys(conflict: Mapping[str, Any]) -> set[str]:
    """Return the top-level runtime keys whose values are ambiguous."""
    keys: set[str] = set()
    raw_conflicts = conflict.get("conflicts")
    if not isinstance(raw_conflicts, list):
        return keys
    for item in raw_conflicts:
        if not isinstance(item, Mapping):
            continue
        raw_keys = item.get("keys")
        if not isinstance(raw_keys, list):
            continue
        keys.update(str(key).strip() for key in raw_keys if str(key).strip())
    return keys


def _is_instance_runtime_map_key(key: object) -> bool:
    """Recognize the public instance-map naming convention generically."""
    text = str(key or "").strip()
    return text.endswith("_instance_settings") or text in {
        "segment_recurring_priority_v1", "segment_special_rings_v1",
    }


def _cell_value(mapping: object, uid: str) -> tuple[bool, Any]:
    if not isinstance(mapping, Mapping):
        return False, None
    if uid in mapping:
        return True, mapping[uid]
    base = uid.split("@", 1)[0]
    matches = [(str(raw_uid), value) for raw_uid, value in mapping.items()
               if str(raw_uid).split("@", 1)[0] == base]
    return (True, matches[0][1]) if len(matches) == 1 else (False, None)


def _get_runtime_cell(runtime: Mapping[str, Any], cell: tuple[str, str, str]) -> tuple[bool, Any]:
    scope, key, uid = cell
    if scope == "host":
        return (key in runtime, runtime.get(key))
    rows = runtime.get(key)
    return _cell_value(rows, uid)


def _set_runtime_cell(runtime: dict[str, Any], cell: tuple[str, str, str], value: tuple[bool, Any]) -> None:
    scope, key, uid = cell
    present, item = value
    if scope == "host":
        if present:
            runtime[key] = deepcopy(item)
        else:
            runtime.pop(key, None)
        return
    rows = runtime.get(key)
    rows = dict(rows) if isinstance(rows, Mapping) else {}
    for raw_uid in list(rows):
        if str(raw_uid) == uid or str(raw_uid).split("@", 1)[0] == uid.split("@", 1)[0]:
            rows.pop(raw_uid, None)
    if present:
        rows[uid] = deepcopy(item)
    if rows:
        runtime[key] = rows
    else:
        runtime.pop(key, None)


def _merge_instance_map_cells(
    key: str, base: object, central: object, host: object, local: object,
) -> dict[str, Any]:
    """Merge an instance map without turning one UID conflict into a map lock."""
    maps = [item for item in (base, central, host, local) if isinstance(item, Mapping)]
    uids = {str(uid) for item in maps for uid in item}
    result: dict[str, Any] = {}
    for uid in sorted(uids):
        b_present, b_value = _cell_value(base, uid)
        c_present, c_value = _cell_value(central, uid)
        h_present, h_value = _cell_value(host, uid)
        c_changed = (b_present, b_value) != (c_present, c_value)
        h_changed = (b_present, b_value) != (h_present, h_value)
        if c_changed and not h_changed:
            selected = (c_present, c_value)
        elif h_changed and not c_changed:
            selected = (h_present, h_value)
        elif (c_present, c_value) == (h_present, h_value):
            selected = (c_present, c_value)
        elif c_changed and h_changed:
            selected = _get_runtime_cell(
                local if isinstance(local, Mapping) else {},
                ("instance", key, uid),
            )
        else:
            selected = (b_present, b_value)
        if selected[0]:
            result[uid] = deepcopy(selected[1])
    return result


def conflict_cells(conflict: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    """Return ambiguous cells as ``(scope, parameter_path, instance_uid)``.

    MCC conflict records from older revisions contain only top-level keys.  A
    map-shaped record with base/central/host values is safely narrowed to the
    UIDs that changed on both sides and disagree.  If that evidence is absent,
    the function returns a wildcard instance cell, preserving the old
    fail-closed behavior without claiming precision that the receipt lacks.
    """
    cells: set[tuple[str, str, str]] = set()
    raw_conflicts = conflict.get("conflicts")
    if not isinstance(raw_conflicts, list):
        return cells
    for item in raw_conflicts:
        if not isinstance(item, Mapping):
            continue
        explicit = item.get("cells")
        if isinstance(explicit, list):
            for raw_cell in explicit:
                if not isinstance(raw_cell, Mapping):
                    continue
                key = str(raw_cell.get("parameter_path") or raw_cell.get("key") or "").strip()
                if not key:
                    continue
                scope = "instance" if str(raw_cell.get("scope", "host")).strip().lower() == "instance" else "host"
                uid = str(raw_cell.get("instance_uid") or raw_cell.get("uid") or "").strip()
                cells.add((scope, key, uid if scope == "instance" else ""))
        item_scope = str(item.get("scope", "host") or "host").strip().lower()
        item_uid = str(item.get("instance_uid") or item.get("uid") or "").strip()
        base_values = item.get("base") if isinstance(item.get("base"), Mapping) else {}
        central_values = item.get("central") if isinstance(item.get("central"), Mapping) else {}
        host_values = item.get("host") if isinstance(item.get("host"), Mapping) else {}
        raw_keys = item.get("keys")
        if not isinstance(raw_keys, list):
            continue
        for raw_key in raw_keys:
            key = str(raw_key).strip()
            if not key:
                continue
            if item_scope == "instance" and item_uid:
                cells.add(("instance", key, item_uid))
                continue
            if not _is_instance_runtime_map_key(key):
                cells.add(("host", key, ""))
                continue
            base_map = base_values.get(key)
            central_map = central_values.get(key)
            host_map = host_values.get(key)
            if not any(isinstance(value, Mapping) for value in (base_map, central_map, host_map)):
                cells.add(("instance", key, "*"))
                continue
            uids = {
                str(uid)
                for value in (base_map, central_map, host_map)
                if isinstance(value, Mapping)
                for uid in value
            }
            narrowed = False
            for uid in sorted(uids):
                b = _cell_value(base_map, uid)
                c = _cell_value(central_map, uid)
                h = _cell_value(host_map, uid)
                if b != c and b != h and c != h:
                    cells.add(("instance", key, uid))
                    narrowed = True
            if not narrowed:
                cells.add(("instance", key, "*"))
    return cells


def conflict_safe_runtime_from_central(
    conflict: Mapping[str, Any],
    central_runtime: Mapping[str, Any],
    local_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a fresh central snapshot while holding only conflict keys."""
    result = deepcopy(dict(central_runtime))
    for scope, key, uid in conflict_cells(conflict):
        if scope == "host":
            _set_runtime_cell(result, (scope, key, uid), _get_runtime_cell(local_runtime, (scope, key, uid)))
        elif uid == "*":
            if key in local_runtime:
                result[key] = deepcopy(local_runtime[key])
            else:
                result.pop(key, None)
        else:
            _set_runtime_cell(result, (scope, key, uid), _get_runtime_cell(local_runtime, (scope, key, uid)))
    return result


def conflict_safe_metadata(
    conflict: Mapping[str, Any],
    remote_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove central stamps for held keys so LWW cannot re-apply them."""
    result = deepcopy(dict(remote_metadata))
    held = conflict_cells(conflict)
    if not held:
        return result
    host = result.get("host")
    if isinstance(host, Mapping):
        result["host"] = {
            key: value for key, value in host.items()
            if ("host", str(key), "") not in held
        }
    instances = result.get("instances")
    if isinstance(instances, Mapping):
        filtered: dict[str, Any] = {}
        for raw_uid, fields in instances.items():
            if not isinstance(fields, Mapping):
                continue
            uid = str(raw_uid)
            next_fields = {
                key: value for key, value in fields.items()
                if not any(
                    scope == "instance" and key == str(path_key)
                    and (held_uid == "*" or held_uid == uid or held_uid.split("@", 1)[0] == uid.split("@", 1)[0])
                    for scope, path_key, held_uid in held
                )
            }
            if next_fields:
                filtered[uid] = next_fields
        result["instances"] = filtered
    return result


def conflict_active(state: object) -> bool:
    return isinstance(state, Mapping) and str(state.get("status") or "").strip().lower() == "conflict"


def resolution_seen(state: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
    desired = response.get("desired_state")
    if not isinstance(desired, Mapping):
        return False
    source = str(desired.get("source") or "").strip()
    revision = desired.get("revision")
    base_revision = state.get("base_revision")
    return source == "sync_v3_operator_resolution" and isinstance(revision, int) and isinstance(base_revision, int) and revision > base_revision
