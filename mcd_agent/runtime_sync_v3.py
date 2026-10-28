"""Generic MCD side of the opt-in MCC desired-runtime sync v3 contract."""

from __future__ import annotations

import hashlib
import json
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
    return revision, dict(runtime) if isinstance(runtime, Mapping) else {}


def conflict_safe_runtime(conflict: Mapping[str, Any], local_runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Project non-ambiguous central changes while preserving conflict keys locally."""
    base = conflict.get("base") if isinstance(conflict.get("base"), Mapping) else {}
    central = conflict.get("central") if isinstance(conflict.get("central"), Mapping) else {}
    host = conflict.get("host") if isinstance(conflict.get("host"), Mapping) else {}
    merge = three_way_merge(base, central, host, semantic_groups=conflict.get("semantic_conflict_groups", []))
    result = dict(merge["merged"])
    for item in conflict.get("conflicts", []):
        if not isinstance(item, Mapping):
            continue
        for key in item.get("keys", []):
            key = str(key)
            if key in local_runtime:
                result[key] = local_runtime[key]
            else:
                result.pop(key, None)
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
