from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA = "mcd-plugin-auto-update-policy-result-v1"
RUNTIME_KEY = "plugin_auto_update_instance_settings"

_INSTANCE_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class PluginAutoUpdatePolicyError(ValueError):
    pass


def validate_identity(
    *,
    instance_uid: str,
    plugin_uid: str,
    package_uid: str,
    operation_id: str,
) -> tuple[str, str, str, str]:
    instance = str(instance_uid or "").strip()
    plugin = str(plugin_uid or "").strip().lower()
    package = str(package_uid or "").strip().lower()
    operation = str(operation_id or "").strip()
    if not instance or "@" in instance or not _INSTANCE_UID_RE.fullmatch(instance):
        raise PluginAutoUpdatePolicyError("instance_uid_must_be_exact_bare_uid")
    if not _IDENTITY_RE.fullmatch(plugin):
        raise PluginAutoUpdatePolicyError("invalid_plugin_uid")
    if not _IDENTITY_RE.fullmatch(package):
        raise PluginAutoUpdatePolicyError("invalid_package_uid")
    if not _OPERATION_ID_RE.fullmatch(operation):
        raise PluginAutoUpdatePolicyError("invalid_operation_id")
    return instance, plugin, package, operation


def normalize_policy(raw: object) -> dict[str, dict[str, dict[str, object]]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, dict[str, object]]] = {}
    for raw_instance, raw_packages in raw.items():
        instance = str(raw_instance or "").strip()
        if not instance or "@" in instance or not _INSTANCE_UID_RE.fullmatch(instance):
            continue
        if not isinstance(raw_packages, dict):
            continue
        packages: dict[str, dict[str, object]] = {}
        for raw_package, raw_row in raw_packages.items():
            package = str(raw_package or "").strip().lower()
            if not _IDENTITY_RE.fullmatch(package) or not isinstance(raw_row, dict):
                continue
            plugin = str(raw_row.get("plugin_uid") or "").strip().lower()
            disabled = raw_row.get("auto_update_disabled")
            if not _IDENTITY_RE.fullmatch(plugin) or not isinstance(disabled, bool):
                continue
            packages[package] = {
                "plugin_uid": plugin,
                "auto_update_disabled": disabled,
            }
        if packages:
            normalized[instance] = packages
    return normalized


def read_policy(
    raw: object,
    *,
    instance_uid: str,
    plugin_uid: str,
    package_uid: str,
) -> bool:
    normalized = normalize_policy(raw)
    row = normalized.get(instance_uid, {}).get(package_uid)
    if not isinstance(row, dict):
        return False
    if row.get("plugin_uid") != plugin_uid:
        raise PluginAutoUpdatePolicyError("package_uid_owned_by_different_plugin_uid")
    return row.get("auto_update_disabled") is True


def write_policy(
    raw: object,
    *,
    instance_uid: str,
    plugin_uid: str,
    package_uid: str,
    disabled: bool,
) -> tuple[dict[str, dict[str, dict[str, object]]], bool]:
    normalized = normalize_policy(raw)
    existing = normalized.get(instance_uid, {}).get(package_uid)
    desired = {
        "plugin_uid": plugin_uid,
        "auto_update_disabled": bool(disabled),
    }
    if existing == desired:
        return normalized, False
    if isinstance(existing, dict) and existing.get("plugin_uid") != plugin_uid:
        raise PluginAutoUpdatePolicyError("package_uid_owned_by_different_plugin_uid")
    packages = dict(normalized.get(instance_uid, {}))
    # An explicit false row is a tombstone for deep-merge transports. Removing
    # the row locally would allow a stale remote true value to survive.
    packages[package_uid] = desired
    normalized[instance_uid] = packages
    return normalized, True


def terminal_result(
    *,
    status: str,
    action: str,
    operation_id: str,
    instance_uid: str,
    plugin_uid: str,
    package_uid: str,
    auto_update_disabled: bool,
    changed: bool,
    local_state_confirmed: bool = False,
    mcc_sync: object = None,
    reason: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": str(status),
        "action": str(action),
        "operation_id": str(operation_id),
        "instance_uid": str(instance_uid),
        "plugin_uid": str(plugin_uid),
        "package_uid": str(package_uid),
        "auto_update_disabled": bool(auto_update_disabled),
        "manual_update_allowed": True,
        "changed": bool(changed),
        "local_state_confirmed": bool(local_state_confirmed),
    }
    if isinstance(mcc_sync, dict):
        result["mcc_sync"] = dict(mcc_sync)
    if reason:
        result["reason"] = str(reason)
    canonical = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result
