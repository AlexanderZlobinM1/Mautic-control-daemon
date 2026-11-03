"""Portal adapter for the externally versioned connected-host lifecycle.

MCD owns only admission, cache integrity and execution through the existing
external-operation runner.  All host mechanics and profile values stay in the
MCC-published binding closure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from mcd_agent.custom_scripts import (
    CustomSelectedTaskError,
    load_configured_profile_cache,
    run_configured_external_operation,
)


BINDING_SCHEMA = "mcd-mautic-connected-host-lifecycle-binding-v1"
CAPABILITY_ID = "mautic_lifecycle"
CAPABILITY_VERSION = "1.0"
CATALOG_ITEM_ID = "mcc.scenario.debian-family-self-hosted"
COMPONENT = "mautic_lifecycle"

_COMMANDS = {
    "preflight": (False, "portal://mcd/input/mautic-lifecycle-preflight-v1", "occupancy.preflight"),
    "create": (True, "portal://mcd/input/mautic-lifecycle-create-v1", "instance.create"),
    "cleanup_owned": (True, "portal://mcd/input/mautic-lifecycle-cleanup-owned-v1", "instance.cleanup_owned"),
    "recreate": (True, "portal://mcd/input/mautic-lifecycle-recreate-v1", "instance.recreate"),
}
_PLAN_OPERATIONS = {"instance.create", "instance.cleanup_owned", "instance.recreate"}
_JSON_FIELDS = {
    "dns_expected_addresses_json",
    "files_archive_json",
    "database_archive_json",
    "nginx_test_argv_json",
    "nginx_reload_argv_json",
    "mysql_admin_argv_json",
    "mysql_dump_argv_json",
    "mysql_restore_argv_json",
    "ownership_receipt_json",
    "preflight_receipt_json",
}
_STRING_FIELDS = {
    "action", "plan_operation", "target_instance_uid", "target_dns", "operation_id",
    "managed_root", "database_name", "database_user", "vhost_available_path",
    "vhost_enabled_path", "certificate_directory", "dns_check_mode", "php_version",
    "php_fpm_socket", "runtime_php_bin", "runtime_user", "runtime_state_dir",
    "filesystem_owner", "filesystem_group", "mcd_cli_path", "mcd_config_path",
    "health_url", "health_connect_address", "health_wait_seconds", "expected_plan_hash",
    "ownership_receipt_sha256", "preflight_receipt_sha256", "expected_before_state_sha256",
    "catalog_item_id", "catalog_item_revision", "artifact_sha256", "profile_sha256",
    "binding_sha256",
}
_INPUT_FIELDS = _STRING_FIELDS | _JSON_FIELDS
_HEX = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_DNS = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class ConnectedHostLifecycleError(RuntimeError):
    """A bounded, public lifecycle admission or execution error."""


def _fail(code: str) -> ConnectedHostLifecycleError:
    return ConnectedHostLifecycleError(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, *, max_bytes: int, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise _fail(code)
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except ConnectedHostLifecycleError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise _fail(code) from None
    if not isinstance(value, dict):
        raise _fail(code)
    return value, raw


def _safe_cache_path(root: Path, relative: object, *, code: str) -> Path:
    if not isinstance(relative, str):
        raise _fail(code)
    posix = PurePosixPath(relative)
    if not relative or posix.is_absolute() or posix.as_posix() != relative or any(part in {"", ".", ".."} for part in posix.parts):
        raise _fail(code)
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise _fail(code)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = cursor.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _fail(code) from None
    if resolved_root not in resolved.parents:
        raise _fail(code)
    return resolved


def _expected_capability(binding_schema: str, binding_sha256: str) -> dict[str, Any]:
    return {
        "capability_id": CAPABILITY_ID,
        "version": CAPABILITY_VERSION,
        "execution": "generic_primitive",
        "target_scope": "host",
        "commands": [
            {
                "command_id": command_id,
                "version": "1.0",
                "execution": "generic_primitive",
                "mutates_state": values[0],
                "input_schema_ref": values[1],
                "target_scope": "host",
                "binding_schema": binding_schema,
                "binding_sha256": binding_sha256,
            }
            for command_id, values in _COMMANDS.items()
        ],
    }


def installed_binding(config: Any) -> dict[str, Any]:
    """Validate the complete cached closure and return its advertised identity."""
    root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
    manifest, _raw = _read_json(root / "manifest.json", max_bytes=512 * 1024,
                                code="mautic_lifecycle_manifest_invalid")
    resources = manifest.get("resources")
    if not isinstance(resources, list):
        raise _fail("mautic_lifecycle_binding_resource_missing")
    identity_rows = [
        row for row in resources
        if isinstance(row, Mapping)
        and row.get("role") == "binding_identity"
        and row.get("cache_file", row.get("file")) == "bindings/mautic-connected-host-lifecycle-binding-v1.json"
    ]
    if len(identity_rows) != 1:
        raise _fail("mautic_lifecycle_binding_resource_missing")
    resource = identity_rows[0]
    allowed_resource_keys = {
        frozenset({"role", "file", "sha256", "size_bytes", "canonical_json_sha256"}),
        frozenset({"role", "file", "cache_file", "sha256", "size_bytes", "canonical_json_sha256"}),
    }
    if frozenset(resource) not in allowed_resource_keys or not isinstance(resource.get("size_bytes"), int):
        raise _fail("mautic_lifecycle_binding_resource_invalid")
    identity_path = _safe_cache_path(root, resource.get("cache_file", resource.get("file")), code="mautic_lifecycle_binding_resource_invalid")
    identity, identity_raw = _read_json(identity_path, max_bytes=256 * 1024,
                                        code="mautic_lifecycle_binding_resource_invalid")
    binding_sha256 = "sha256:" + hashlib.sha256(_canonical(identity)).hexdigest()
    if (len(identity_raw) != resource["size_bytes"]
            or hashlib.sha256(identity_raw).hexdigest() != resource.get("sha256")
            or hashlib.sha256(_canonical(identity)).hexdigest() != resource.get("canonical_json_sha256")
            or _SHA.fullmatch(binding_sha256) is None):
        raise _fail("mautic_lifecycle_binding_digest_mismatch")
    catalog_revision = identity.get("catalog_item_revision")
    if (identity.get("schema") != BINDING_SCHEMA
            or identity.get("catalog_item_id") != CATALOG_ITEM_ID
            or not isinstance(catalog_revision, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", catalog_revision) is None):
        raise _fail("mautic_lifecycle_binding_identity_invalid")

    capability = identity.get("capability")
    expected_identity_capability = _expected_capability(BINDING_SCHEMA, binding_sha256)
    for command in expected_identity_capability["commands"]:
        command.pop("binding_schema")
        command.pop("binding_sha256")
        command.pop("execution")
    if capability != expected_identity_capability:
        raise _fail("mautic_lifecycle_capability_invalid")

    operation_bindings = identity.get("operation_bindings")
    all_manifest_bindings = manifest.get("operation_bindings")
    manifest_bindings = [
        row for row in all_manifest_bindings
        if isinstance(row, Mapping) and row.get("component") == COMPONENT
    ] if isinstance(all_manifest_bindings, list) else None
    if not isinstance(operation_bindings, list) or operation_bindings != manifest_bindings:
        raise _fail("mautic_lifecycle_operation_bindings_mismatch")
    if [row.get("operation") for row in operation_bindings if isinstance(row, Mapping)] != list(_COMMANDS):
        raise _fail("mautic_lifecycle_operation_bindings_invalid")

    artifact = identity.get("artifact")
    profile = identity.get("profile")
    if not isinstance(artifact, Mapping) or not isinstance(profile, Mapping):
        raise _fail("mautic_lifecycle_binding_identity_invalid")
    if not isinstance(artifact.get("sha256"), str) or _HEX.fullmatch(artifact["sha256"]) is None or not isinstance(artifact.get("size_bytes"), int):
        raise _fail("mautic_lifecycle_artifact_identity_invalid")
    key = str(artifact.get("key") or "")
    archive = root / "downloads" / key / f"{key}-{artifact['sha256']}.tar.gz"
    try:
        if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != artifact["size_bytes"] or hashlib.sha256(archive.read_bytes()).hexdigest() != artifact["sha256"]:
            raise _fail("mautic_lifecycle_artifact_missing_or_corrupt")
    except OSError:
        raise _fail("mautic_lifecycle_artifact_missing_or_corrupt") from None
    if not isinstance(profile.get("file"), str) or not isinstance(profile.get("sha256"), str) or _HEX.fullmatch(profile["sha256"]) is None:
        raise _fail("mautic_lifecycle_profile_identity_invalid")
    profile_path = _safe_cache_path(root, profile["file"], code="mautic_lifecycle_profile_missing_or_corrupt")
    try:
        if hashlib.sha256(profile_path.read_bytes()).hexdigest() != profile["sha256"]:
            raise _fail("mautic_lifecycle_profile_missing_or_corrupt")
        profile_value = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError:
        raise _fail("mautic_lifecycle_profile_missing_or_corrupt") from None
    except (UnicodeError, ValueError, TypeError):
        raise _fail("mautic_lifecycle_profile_missing_or_corrupt") from None
    if (not isinstance(profile_value, Mapping)
            or profile_value.get("catalog_item") != {"id": CATALOG_ITEM_ID, "revision": catalog_revision}):
        raise _fail("mautic_lifecycle_profile_identity_invalid")

    advertised_capability = _expected_capability(BINDING_SCHEMA, binding_sha256)
    advertised_capability.pop("execution", None)
    advertised_capability.pop("target_scope", None)
    return {
        "binding_schema": BINDING_SCHEMA,
        "binding_sha256": binding_sha256,
        "artifact_sha256": artifact["sha256"],
        "profile_sha256": profile["sha256"],
        "catalog_item_revision": catalog_revision,
        "capability": advertised_capability,
    }


def capability(config: Any) -> dict[str, Any] | None:
    """Advertise only a fully cached and independently verified closure."""
    try:
        return installed_binding(config)["capability"]
    except ConnectedHostLifecycleError:
        return None


def _canonical_json_string(value: str, *, allow_empty: bool) -> bool:
    if allow_empty and value == "":
        return True
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    return value == _canonical(parsed).decode("ascii")


def _receipt_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _validate_input(command_id: str, value: object, binding: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _INPUT_FIELDS or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise _fail("mautic_lifecycle_input_invalid")
    result = dict(value)
    if any(len(item) > 262144 for item in result.values()) or sum(len(item) for item in result.values()) > 524288:
        raise _fail("mautic_lifecycle_input_invalid")
    if result["action"] != _COMMANDS[command_id][2] or result["plan_operation"] not in _PLAN_OPERATIONS:
        raise _fail("mautic_lifecycle_action_invalid")
    if command_id != "preflight" and result["plan_operation"] != result["action"]:
        raise _fail("mautic_lifecycle_action_invalid")
    uid = result["target_instance_uid"]
    if "@" in uid or _UID.fullmatch(uid) is None:
        raise _fail("mautic_lifecycle_instance_uid_invalid")
    if result["target_dns"] != result["target_dns"].lower() or _DNS.fullmatch(result["target_dns"]) is None:
        raise _fail("mautic_lifecycle_dns_invalid")
    if result["catalog_item_id"] != CATALOG_ITEM_ID or result["catalog_item_revision"] != binding["catalog_item_revision"]:
        raise _fail("mautic_lifecycle_catalog_identity_invalid")
    if result["artifact_sha256"] != binding["artifact_sha256"] or result["profile_sha256"] != binding["profile_sha256"] or result["binding_sha256"] != binding["binding_sha256"]:
        raise _fail("mautic_lifecycle_binding_mismatch")
    if result["dns_check_mode"] not in {"skip", "require_absent", "require_exact"} or re.fullmatch(r"\d+\.\d+", result["php_version"]) is None:
        raise _fail("mautic_lifecycle_input_invalid")
    if not result["health_wait_seconds"].isdigit() or not 1 <= int(result["health_wait_seconds"]) <= 600:
        raise _fail("mautic_lifecycle_input_invalid")
    for field in _JSON_FIELDS:
        if not _canonical_json_string(result[field], allow_empty=field in {"ownership_receipt_json", "preflight_receipt_json"}):
            raise _fail("mautic_lifecycle_json_input_invalid")

    mutation = command_id != "preflight"
    needs_owner = result["plan_operation"] in {"instance.cleanup_owned", "instance.recreate"}
    if mutation:
        if (_SHA.fullmatch(result["expected_plan_hash"]) is None
                or _SHA.fullmatch(result["expected_before_state_sha256"]) is None
                or _HEX.fullmatch(result["preflight_receipt_sha256"]) is None
                or not result["preflight_receipt_json"]
                or _receipt_digest(result["preflight_receipt_json"]) != result["preflight_receipt_sha256"]):
            raise _fail("mautic_lifecycle_preflight_receipt_invalid")
    elif any(result[field] for field in ("expected_plan_hash", "expected_before_state_sha256", "preflight_receipt_sha256", "preflight_receipt_json")):
        raise _fail("mautic_lifecycle_preflight_input_invalid")
    if needs_owner:
        if (_HEX.fullmatch(result["ownership_receipt_sha256"]) is None
                or not result["ownership_receipt_json"]
                or _receipt_digest(result["ownership_receipt_json"]) != result["ownership_receipt_sha256"]):
            raise _fail("mautic_lifecycle_ownership_receipt_invalid")
    elif result["ownership_receipt_sha256"] or result["ownership_receipt_json"]:
        raise _fail("mautic_lifecycle_ownership_receipt_unexpected")
    return result


def _validate_result(
    command_id: str,
    operation_input: Mapping[str, str],
    binding: Mapping[str, Any],
    result: object,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise _fail("mautic_lifecycle_result_invalid")
    expected_binding = {
        "catalog_item_id": CATALOG_ITEM_ID,
        "catalog_item_revision": binding["catalog_item_revision"],
        "artifact_sha256": binding["artifact_sha256"],
        "profile_sha256": binding["profile_sha256"],
        "binding_sha256": binding["binding_sha256"],
    }
    expected_operation = (
        operation_input["plan_operation"]
        if command_id == "preflight"
        else _COMMANDS[command_id][2]
    )
    allowed_status = {
        "preflight": {"planned"},
        "create": {"applied", "noop"},
        "cleanup_owned": {"applied", "noop"},
        "recreate": {"applied", "noop"},
    }
    if (result.get("schema") != "mcc-mautic-connected-host-lifecycle-result-v1"
            or result.get("contract_version") != "1.0"
            or result.get("operation") != expected_operation
            or result.get("status") not in allowed_status[command_id]
            or result.get("artifact_binding") != expected_binding):
        raise _fail("mautic_lifecycle_result_invalid")

    if command_id == "preflight":
        receipt = result.get("preflight_receipt")
        digest = result.get("preflight_receipt_sha256")
        if (result.get("read_only") is not True
                or result.get("mutation_started") is not False
                or _SHA.fullmatch(str(result.get("plan_hash") or "")) is None
                or _SHA.fullmatch(str(result.get("before_state_sha256") or "")) is None
                or not isinstance(receipt, Mapping)
                or _HEX.fullmatch(str(digest or "")) is None
                or hashlib.sha256(_canonical(receipt)).hexdigest() != digest
                or receipt.get("schema") != "mcc-mautic-connected-host-preflight-v1"
                or receipt.get("scenario_revision") != binding["catalog_item_revision"]
                or receipt.get("operation_id") != operation_input["operation_id"]
                or receipt.get("operation") != operation_input["plan_operation"]
                or receipt.get("plan_hash") != result.get("plan_hash")
                or receipt.get("before_state_sha256") != result.get("before_state_sha256")
                or receipt.get("domain") != operation_input["target_dns"]
                or receipt.get("instance_uid") != operation_input["target_instance_uid"]
                or receipt.get("ownership_receipt_sha256") != operation_input["ownership_receipt_sha256"]
                or receipt.get("artifact_binding") != expected_binding):
            raise _fail("mautic_lifecycle_result_receipt_invalid")
        return result

    if command_id in {"create", "recreate"}:
        receipt = result.get("ownership_receipt")
        digest = result.get("ownership_receipt_sha256")
        if (not isinstance(receipt, Mapping)
                or _HEX.fullmatch(str(digest or "")) is None
                or hashlib.sha256(_canonical(receipt)).hexdigest() != digest
                or receipt.get("schema") != "mcc-mautic-connected-host-ownership-v1"
                or receipt.get("scenario_revision") != binding["catalog_item_revision"]
                or receipt.get("domain") != operation_input["target_dns"]
                or receipt.get("instance_uid") != operation_input["target_instance_uid"]
                or receipt.get("managed_root") != operation_input["managed_root"]
                or receipt.get("database_name") != operation_input["database_name"]
                or receipt.get("database_user") != operation_input["database_user"]
                or receipt.get("vhost_available_path") != operation_input["vhost_available_path"]
                or receipt.get("vhost_enabled_path") != operation_input["vhost_enabled_path"]):
            raise _fail("mautic_lifecycle_result_receipt_invalid")
        if command_id == "create":
            if (result.get("plan_hash") != operation_input["expected_plan_hash"]
                    or receipt.get("plan_hash") != operation_input["expected_plan_hash"]):
                raise _fail("mautic_lifecycle_result_receipt_invalid")
        else:
            if (result.get("recreate_plan_hash") != operation_input["expected_plan_hash"]
                    or result.get("replaced_ownership_receipt_sha256") != operation_input["ownership_receipt_sha256"]):
                raise _fail("mautic_lifecycle_result_receipt_invalid")
        return result

    receipt = result.get("cleanup_receipt")
    if (result.get("plan_hash") != operation_input["expected_plan_hash"]
            or not isinstance(receipt, Mapping)
            or receipt.get("schema") != "mcc-mautic-connected-host-cleanup-v1"
            or receipt.get("scenario_revision") != binding["catalog_item_revision"]
            or receipt.get("cleanup_operation_id") != operation_input["operation_id"]
            or receipt.get("ownership_receipt_sha256") != operation_input["ownership_receipt_sha256"]
            or receipt.get("plan_hash") != operation_input["expected_plan_hash"]
            or receipt.get("domain") != operation_input["target_dns"]
            or receipt.get("instance_uid") != operation_input["target_instance_uid"]):
        raise _fail("mautic_lifecycle_result_receipt_invalid")
    return result


def execute(job: Mapping[str, Any], *, config: Any) -> Mapping[str, Any]:
    """Run one admitted lifecycle operation through the existing runner."""
    command = job.get("command")
    if not isinstance(command, Mapping):
        return {"status": "failed", "reason": "mautic_lifecycle_command_invalid"}
    command_id = str(command.get("command_id") or "")
    if command_id not in _COMMANDS:
        return {"status": "failed", "reason": "mautic_lifecycle_command_invalid"}
    try:
        binding = installed_binding(config)
        if (command.get("binding_schema") != binding["binding_schema"]
                or command.get("binding_sha256") != binding["binding_sha256"]):
            raise _fail("mautic_lifecycle_binding_mismatch")
        operation_input = _validate_input(command_id, job.get("input"), binding)
        cached = load_configured_profile_cache(config, component=COMPONENT, operation=command_id)
        if not isinstance(cached, Mapping) or not isinstance(cached.get("profile"), dict):
            raise _fail("mautic_lifecycle_profile_missing_or_corrupt")
        rc, output = run_configured_external_operation(
            config,
            component=COMPONENT,
            operation=command_id,
            profile=dict(cached["profile"]),
            operation_input=operation_input,
        )
        if rc != 0:
            raise _fail("mautic_lifecycle_external_operation_failed")
        result = _validate_result(command_id, operation_input, binding, json.loads(output))
        return {"status": "succeeded", "lifecycle": result}
    except (ConnectedHostLifecycleError, CustomSelectedTaskError) as exc:
        code = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(exc)) else "mautic_lifecycle_failed"
        return {"status": "failed", "reason": code}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "mautic_lifecycle_failed"}
