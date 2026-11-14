"""Generic MCD adapter for authority-bound Upgrade OS operations.

Concrete package, service, path and configuration mechanics belong to the
external profile/artifact.  This module only loads the profile pinned by the
existing custom manifest and dispatches through the established framed custom
operation seam.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mcd_agent.custom_scripts import CustomSelectedTaskError, load_configured_profile_cache
from mcd_agent.service_profiles import _configured_external_operation


_SUCCESS_STATUSES = {
    "already_present",
    "applied",
    "noop",
    "ok",
    "planned",
    "rolled_back",
    "success",
}

_UPGRADE_OS_OPERATIONS = (
    ("apt", "apt_apply"),
    ("certificate", "certificate_material"),
    ("composer", "composer_host_prepare"),
    ("database", "database_prepare_import"),
    ("filesystem", "upgrade_tree_transition"),
    ("nginx", "nginx_service_transition"),
    ("php", "php_runtime_transition"),
    ("service", "upgrade_service_transition"),
)


class UpgradeOSExternalError(RuntimeError):
    """An installed Upgrade OS binding/profile failed closed."""


def upgrade_os_binding_identity(cfg: Any) -> dict[str, str]:
    """Return the immutable identity of the installed eight-operation closure."""
    root = Path(str(getattr(cfg, "custom_cache_dir", "") or ""))
    manifest = root / "manifest.json"
    try:
        if not root.is_absolute() or root.is_symlink() or manifest.is_symlink():
            raise UpgradeOSExternalError("upgrade_os_binding_manifest_invalid")
        raw = manifest.read_bytes()
        if not raw or len(raw) > 256 * 1024:
            raise UpgradeOSExternalError("upgrade_os_binding_manifest_invalid")
        document = json.loads(raw.decode("utf-8"))
    except UpgradeOSExternalError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise UpgradeOSExternalError("upgrade_os_binding_manifest_invalid") from exc
    rows = document.get("operation_bindings") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise UpgradeOSExternalError("upgrade_os_binding_manifest_invalid")

    normalized: list[dict[str, Any]] = []
    for component, operation in _UPGRADE_OS_OPERATIONS:
        matches = [
            row for row in rows
            if isinstance(row, dict)
            and row.get("component") == component
            and row.get("operation") == operation
        ]
        if len(matches) != 1:
            raise UpgradeOSExternalError("upgrade_os_binding_closure_incomplete")
        row = matches[0]
        allowed = {
            "component", "operation", "artifact", "profile_source", "args",
            "capture_result", "detach", "live_output", "offline_only",
            "runtime_input_schema", "runtime_input_mapping",
        }
        if set(row) != allowed:
            raise UpgradeOSExternalError("upgrade_os_binding_descriptor_invalid")
        artifact = row.get("artifact")
        profile = row.get("profile_source")
        runtime_schema = row.get("runtime_input_schema")
        runtime_mapping = row.get("runtime_input_mapping")
        expected_runtime_fields = [
            "operator_profile_json",
            "cfg_facts_json",
            "operation_input_json",
        ]
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"entrypoint", "key", "sha256"}
            or any(not isinstance(artifact.get(key), str) or not artifact[key] for key in artifact)
            or len(str(artifact.get("sha256"))) != 64
            or any(char not in "0123456789abcdef" for char in str(artifact.get("sha256")))
            or not isinstance(profile, dict)
            or set(profile) != {"type", "file", "sha256"}
            or profile.get("type") != "cached-json"
            or any(not isinstance(profile.get(key), str) or not profile[key] for key in profile)
            or len(str(profile.get("sha256"))) != 64
            or any(char not in "0123456789abcdef" for char in str(profile.get("sha256")))
            or row.get("args") != ["--runtime-input-stdin"]
            or row.get("capture_result") is not True
            or row.get("detach") is not False
            or row.get("live_output") is not False
            or row.get("offline_only") is not True
            or not isinstance(runtime_schema, dict)
            or set(runtime_schema) != {"type", "fields"}
            or runtime_schema.get("type") != "string-map"
            or runtime_schema.get("fields") != expected_runtime_fields
            or not isinstance(runtime_mapping, dict)
            or set(runtime_mapping) != set(expected_runtime_fields)
            or runtime_mapping.get("operator_profile_json") != {
                "encoding": "json",
                "envelope_key": "profile",
                "source": "existing_private_operator_profile",
            }
            or runtime_mapping.get("cfg_facts_json") != {
                "encoding": "json",
                "fields": [],
                "source": "effective_caller_cfg",
            }
            or runtime_mapping.get("operation_input_json") != {
                "encoding": "json",
                "envelope_key": "input",
                "source": "caller_operation_input",
            }
        ):
            raise UpgradeOSExternalError("upgrade_os_binding_descriptor_invalid")
        normalized.append({key: row[key] for key in sorted(row)})

    descriptor = {
        "schema": "mcd-upgrade-os-binding-v1",
        "operations": normalized,
    }
    encoded = json.dumps(
        descriptor,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "binding_schema": "mcd-upgrade-os-binding-v1",
        "binding_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def run_upgrade_os_operation(
    cfg: Any,
    *,
    component: str,
    operation: str,
    operation_input: dict[str, str],
) -> dict[str, Any]:
    """Run one authority-bound operation and fail closed when it is unbound."""
    if (
        not isinstance(component, str)
        or not component.strip()
        or not isinstance(operation, str)
        or not operation.strip()
        or not isinstance(operation_input, dict)
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in operation_input.items()
        )
    ):
        raise UpgradeOSExternalError("upgrade_os_operation_input_invalid")

    try:
        cached = load_configured_profile_cache(
            cfg,
            component=component,
            operation=operation,
        )
    except CustomSelectedTaskError as exc:
        raise UpgradeOSExternalError(str(exc) or "upgrade_os_profile_invalid") from exc

    if not isinstance(cached, dict):
        raise UpgradeOSExternalError("upgrade_os_binding_not_configured")
    profile = cached.get("profile")
    if not isinstance(profile, dict):
        raise UpgradeOSExternalError("upgrade_os_profile_invalid")

    receipt = _configured_external_operation(
        component,
        profile,
        cfg=cfg,
        operation=operation,
        operation_input=operation_input,
    )
    if receipt is None:
        raise UpgradeOSExternalError("upgrade_os_binding_not_configured")
    if not isinstance(receipt, dict):
        raise UpgradeOSExternalError("upgrade_os_operation_receipt_invalid")
    status = str(receipt.get("status") or "").strip().lower()
    if status not in _SUCCESS_STATUSES:
        reason = str(
            receipt.get("reason")
            or receipt.get("error")
            or "upgrade_os_external_operation_failed"
        ).strip()
        raise UpgradeOSExternalError(reason or "upgrade_os_external_operation_failed")
    return receipt
