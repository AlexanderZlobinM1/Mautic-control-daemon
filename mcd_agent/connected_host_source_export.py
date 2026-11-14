"""Portal adapter for the externally versioned connected-host source export.

MCD resolves the selected local installation and its private database access,
then delegates the actual read-only export algorithm to the signed external
operation.  Portal never supplies local paths, database credentials or argv.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Mapping

from mcd_agent.custom_scripts import (
    CustomSelectedTaskError,
    load_configured_profile_cache,
    run_configured_external_operation,
)
from mcd_agent.inventory import InstanceInventory


BINDING_SCHEMA = "mcd-mautic-connected-host-source-export-binding-v1"
CAPABILITY_ID = "mautic_source_export"
CAPABILITY_VERSION = "1.0"
CATALOG_ITEM_ID = "mcc.scenario.mautic-connected-host-source-export"
CATALOG_ITEM_REVISION = "1.0.0"
COMMAND_ID = "source_export"
COMPONENT = CAPABILITY_ID
CONTEXT_SCHEMA = "mcd-mautic-connected-host-source-export-local-context-v1"
RESULT_SCHEMA = "mcd-mautic-connected-host-source-export-v1"
INPUT_SCHEMA_REF = "portal://mcd/input/mautic-source-export-v1"

_HEX = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_DNS = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class ConnectedHostSourceExportError(RuntimeError):
    """Bounded public admission or execution error."""


def _fail(code: str) -> ConnectedHostSourceExportError:
    return ConnectedHostSourceExportError(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, *, max_bytes: int, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise _fail(code)
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except ConnectedHostSourceExportError:
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


def _expected_capability(binding_sha256: str) -> dict[str, Any]:
    return {
        "capability_id": CAPABILITY_ID,
        "version": CAPABILITY_VERSION,
        "commands": [{
            "command_id": COMMAND_ID,
            "version": "1.0",
            "execution": "generic_primitive",
            "mutates_state": False,
            "input_schema_ref": INPUT_SCHEMA_REF,
            "target_scope": "host",
            "binding_schema": BINDING_SCHEMA,
            "binding_sha256": binding_sha256,
        }],
    }


def installed_binding(config: Any) -> dict[str, Any]:
    """Validate the complete cached closure and return its public coordinate."""
    root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
    manifest, _ = _read_json(root / "manifest.json", max_bytes=512 * 1024,
                             code="mautic_source_export_manifest_invalid")
    resources = manifest.get("resources")
    if not isinstance(resources, list):
        raise _fail("mautic_source_export_binding_resource_missing")
    logical_identity_file = "bindings/mautic-connected-host-source-export-binding-v1.json"
    identity_rows = [
        row for row in resources
        if isinstance(row, Mapping)
        and row.get("role") == "binding_identity"
        and row.get("file") == logical_identity_file
    ]
    active_bindings = manifest.get("active_bindings")
    if active_bindings is not None and not isinstance(active_bindings, Mapping):
        raise _fail("mautic_source_export_binding_resource_invalid")
    selector = active_bindings.get(BINDING_SCHEMA) if isinstance(active_bindings, Mapping) else None
    if selector is not None:
        if (not isinstance(selector, Mapping)
                or set(selector) != {
                    "catalog_item_id", "catalog_item_revision", "binding_sha256",
                    "cache_file", "resource_sha256",
                }
                or selector.get("catalog_item_id") != CATALOG_ITEM_ID
                or selector.get("catalog_item_revision") != CATALOG_ITEM_REVISION
                or not isinstance(selector.get("binding_sha256"), str)
                or _SHA.fullmatch(selector["binding_sha256"]) is None
                or not isinstance(selector.get("cache_file"), str)
                or not isinstance(selector.get("resource_sha256"), str)
                or _HEX.fullmatch(selector["resource_sha256"]) is None):
            raise _fail("mautic_source_export_binding_resource_invalid")
        identity_rows = [
            row for row in identity_rows
            if row.get("cache_file") == selector["cache_file"]
            and row.get("sha256") == selector["resource_sha256"]
            and row.get("canonical_json_sha256") == selector["binding_sha256"][7:]
        ]
    else:
        identity_rows = [
            row for row in identity_rows
            if row.get("cache_file", row.get("file")) == logical_identity_file
        ]
    if len(identity_rows) != 1:
        raise _fail("mautic_source_export_binding_resource_missing")
    resource = identity_rows[0]
    allowed_resource_keys = {
        frozenset({"role", "file", "sha256", "size_bytes", "canonical_json_sha256"}),
        frozenset({"role", "file", "cache_file", "sha256", "size_bytes", "canonical_json_sha256"}),
    }
    if frozenset(resource) not in allowed_resource_keys or not isinstance(resource.get("size_bytes"), int):
        raise _fail("mautic_source_export_binding_resource_invalid")
    identity_path = _safe_cache_path(
        root,
        resource.get("cache_file", resource.get("file")),
        code="mautic_source_export_binding_resource_invalid",
    )
    identity, raw = _read_json(identity_path, max_bytes=256 * 1024,
                               code="mautic_source_export_binding_resource_invalid")
    binding_sha256 = "sha256:" + hashlib.sha256(_canonical(identity)).hexdigest()
    if (len(raw) != resource["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != resource.get("sha256")
            or hashlib.sha256(_canonical(identity)).hexdigest() != resource.get("canonical_json_sha256")
            or _SHA.fullmatch(binding_sha256) is None):
        raise _fail("mautic_source_export_binding_digest_mismatch")
    if selector is not None and selector.get("binding_sha256") != binding_sha256:
        raise _fail("mautic_source_export_binding_digest_mismatch")
    if (identity.get("schema") != BINDING_SCHEMA
            or identity.get("catalog_item_id") != CATALOG_ITEM_ID
            or identity.get("catalog_item_revision") != CATALOG_ITEM_REVISION):
        raise _fail("mautic_source_export_binding_identity_invalid")

    expected_identity_capability = _expected_capability(binding_sha256)
    expected_identity_capability["execution"] = "generic_primitive"
    expected_identity_capability["target_scope"] = "host"
    for command in expected_identity_capability["commands"]:
        command.pop("binding_schema")
        command.pop("binding_sha256")
    if identity.get("capability") != expected_identity_capability:
        raise _fail("mautic_source_export_capability_invalid")

    operation_bindings = identity.get("operation_bindings")
    all_manifest_bindings = manifest.get("operation_bindings")
    if (not isinstance(operation_bindings, list)
            or not isinstance(all_manifest_bindings, list)
            or any(not isinstance(row, Mapping) for row in operation_bindings)
            or any(sum(_canonical(row) == _canonical(candidate) for candidate in all_manifest_bindings if isinstance(candidate, Mapping)) != 1 for row in operation_bindings)
            or len(operation_bindings) != 1
            or operation_bindings[0].get("operation") != COMMAND_ID):
        raise _fail("mautic_source_export_operation_bindings_invalid")

    artifact = identity.get("artifact")
    profile = identity.get("profile")
    if not isinstance(artifact, Mapping) or not isinstance(profile, Mapping):
        raise _fail("mautic_source_export_binding_identity_invalid")
    if (not isinstance(artifact.get("sha256"), str)
            or _HEX.fullmatch(artifact["sha256"]) is None
            or not isinstance(artifact.get("size_bytes"), int)):
        raise _fail("mautic_source_export_artifact_identity_invalid")
    key = str(artifact.get("key") or "")
    archive = root / "downloads" / key / f"{key}-{artifact['sha256']}.tar.gz"
    try:
        if (archive.is_symlink() or not archive.is_file()
                or archive.stat().st_size != artifact["size_bytes"]
                or hashlib.sha256(archive.read_bytes()).hexdigest() != artifact["sha256"]):
            raise _fail("mautic_source_export_artifact_missing_or_corrupt")
    except OSError:
        raise _fail("mautic_source_export_artifact_missing_or_corrupt") from None
    if (profile.get("schema") != "mcc-mautic-connected-host-source-export-profile-v1"
            or profile.get("version") != "1.0.0"
            or not isinstance(profile.get("file"), str)
            or not isinstance(profile.get("sha256"), str)
            or _HEX.fullmatch(profile["sha256"]) is None):
        raise _fail("mautic_source_export_profile_identity_invalid")
    profile_path = _safe_cache_path(root, profile["file"], code="mautic_source_export_profile_missing_or_corrupt")
    try:
        if hashlib.sha256(profile_path.read_bytes()).hexdigest() != profile["sha256"]:
            raise _fail("mautic_source_export_profile_missing_or_corrupt")
        profile_value = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError:
        raise _fail("mautic_source_export_profile_missing_or_corrupt") from None
    except (UnicodeError, ValueError, TypeError):
        raise _fail("mautic_source_export_profile_missing_or_corrupt") from None
    if (not isinstance(profile_value, Mapping)
            or profile_value.get("catalog_item") != {"id": CATALOG_ITEM_ID, "revision": CATALOG_ITEM_REVISION}):
        raise _fail("mautic_source_export_profile_identity_invalid")

    return {
        "binding_schema": BINDING_SCHEMA,
        "binding_sha256": binding_sha256,
        "artifact_sha256": artifact["sha256"],
        "profile_sha256": profile["sha256"],
        "catalog_item_revision": CATALOG_ITEM_REVISION,
        "capability": _expected_capability(binding_sha256),
    }


def capability(config: Any) -> dict[str, Any] | None:
    try:
        return installed_binding(config)["capability"]
    except ConnectedHostSourceExportError:
        return None


def _validate_input(job: Mapping[str, Any]) -> dict[str, str]:
    value = job.get("input")
    installation = job.get("installation")
    if (not isinstance(value, Mapping)
            or set(value) != {"source_installation_id", "operation_id"}
            or any(not isinstance(item, str) for item in value.values())
            or not isinstance(installation, Mapping)):
        raise _fail("mautic_source_export_input_invalid")
    result = dict(value)
    if (any(_IDENTITY.fullmatch(result[field]) is None for field in result)
            or result["source_installation_id"] != installation.get("installation_id")):
        raise _fail("mautic_source_export_installation_binding_invalid")
    return result


def _select_source(job: Mapping[str, Any], config: Any) -> Any:
    installation = job.get("installation")
    dns_name = str((installation or {}).get("dns_name") or "").strip().lower()
    if _DNS.fullmatch(dns_name) is None:
        raise _fail("mautic_source_export_installation_binding_invalid")
    inventory = InstanceInventory(str(getattr(config, "state_db_path", "") or ""))
    try:
        matches = []
        for item in inventory.list_instances():
            identities = {
                str(item.instance_uid or "").strip().lower(),
                str(item.primary_domain or "").strip().lower(),
                str(item.name or "").strip().lower(),
                *(str(value).strip().lower() for value in (item.domains or [])),
            }
            if dns_name in identities:
                matches.append(item)
    finally:
        inventory.conn.close()
    if len(matches) != 1 or not matches[0].db:
        raise _fail("mautic_source_export_source_unavailable")
    source = matches[0]
    if "@" in str(source.instance_uid or "") or not source.primary_domain:
        raise _fail("mautic_source_export_source_identity_invalid")
    root = Path(str(source.root or ""))
    if root.is_symlink() or not root.is_dir():
        raise _fail("mautic_source_export_source_root_invalid")
    return source


def _output_root(config: Any) -> Path:
    cache_root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise _fail("mautic_source_export_output_root_invalid")
    output = cache_root / "operation-output" / "mautic-source-export"
    cursor = cache_root
    for part in ("operation-output", "mautic-source-export"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise _fail("mautic_source_export_output_root_invalid")
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output, 0o700)
    except OSError:
        raise _fail("mautic_source_export_output_root_invalid") from None
    if not output.is_dir() or output.stat().st_mode & 0o077:
        raise _fail("mautic_source_export_output_root_invalid")
    return output


def _build_context(job: Mapping[str, Any], operation_input: Mapping[str, str], config: Any) -> tuple[dict[str, Any], str]:
    source = _select_source(job, config)
    dump_binary = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not dump_binary:
        raise _fail("mautic_source_export_dump_client_unavailable")
    output_root = _output_root(config).resolve()
    from mcd_agent.backup import _mysql_defaults_file
    defaults = _mysql_defaults_file(
        host=source.db.host,
        port=source.db.port,
        user=source.db.user,
        password=source.db.password,
    )
    context = {
        "schema": CONTEXT_SCHEMA,
        "source_installation_id": operation_input["source_installation_id"],
        "instance_uid": str(source.instance_uid),
        "primary_domain": str(source.primary_domain).strip().lower(),
        "source_root": str(Path(source.root).resolve()),
        "database_name": str(source.db.name),
        "output_root": str(output_root),
        "mysql_dump_argv": [dump_binary, f"--defaults-extra-file={defaults.name}"],
    }
    return context, defaults.name


def _file_pin(value: object, *, format_name: str, output_root: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size_bytes", "format"}:
        raise _fail("mautic_source_export_result_invalid")
    path_value = value.get("path")
    if (not isinstance(path_value, str)
            or not isinstance(value.get("sha256"), str)
            or _HEX.fullmatch(value["sha256"]) is None
            or isinstance(value.get("size_bytes"), bool)
            or not isinstance(value.get("size_bytes"), int)
            or value["size_bytes"] <= 0
            or value.get("format") != format_name):
        raise _fail("mautic_source_export_result_invalid")
    path = Path(path_value)
    try:
        if (path.is_symlink() or not path.is_file()
                or output_root not in path.resolve(strict=True).parents
                or path.stat().st_size != value["size_bytes"]):
            raise _fail("mautic_source_export_result_artifact_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != value["sha256"]:
            raise _fail("mautic_source_export_result_artifact_invalid")
    except OSError:
        raise _fail("mautic_source_export_result_artifact_invalid") from None
    return dict(value)


def _validate_result(result: object, operation_input: Mapping[str, str], context: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
    expected_keys = {
        "schema", "contract_version", "status", "operation", "operation_id", "source",
        "files_archive", "database_archive", "source_snapshot_sha256", "excluded_runtime_paths",
        "source_mutation", "output_rollback",
    }
    expected_source = {
        "source_installation_id": operation_input["source_installation_id"],
        "instance_uid": context["instance_uid"],
        "primary_domain": context["primary_domain"],
        "source_root": context["source_root"],
        "database_name": context["database_name"],
    }
    if (not isinstance(result, dict)
            or set(result) != expected_keys
            or result.get("schema") != RESULT_SCHEMA
            or result.get("contract_version") != "1.0"
            or result.get("status") != "succeeded"
            or result.get("operation") != "source.export"
            or result.get("operation_id") != operation_input["operation_id"]
            or result.get("source") != expected_source
            or _SHA.fullmatch(str(result.get("source_snapshot_sha256") or "")) is None
            or not isinstance(result.get("excluded_runtime_paths"), list)
            or any(not isinstance(item, str) for item in result["excluded_runtime_paths"])
            or result.get("source_mutation") is not False
            or result.get("output_rollback") != "not_needed"):
        raise _fail("mautic_source_export_result_invalid")
    checked = dict(result)
    checked["files_archive"] = _file_pin(result["files_archive"], format_name="tar.gz", output_root=output_root)
    checked["database_archive"] = _file_pin(result["database_archive"], format_name="sql.gz", output_root=output_root)
    return checked


def execute(job: Mapping[str, Any], *, config: Any) -> Mapping[str, Any]:
    command = job.get("command")
    if not isinstance(command, Mapping) or command.get("command_id") != COMMAND_ID:
        return {"status": "failed", "reason": "mautic_source_export_command_invalid"}
    defaults_path = ""
    try:
        binding = installed_binding(config)
        if (command.get("binding_schema") != binding["binding_schema"]
                or command.get("binding_sha256") != binding["binding_sha256"]):
            raise _fail("mautic_source_export_binding_mismatch")
        operation_input = _validate_input(job)
        context, defaults_path = _build_context(job, operation_input, config)
        cached = load_configured_profile_cache(config, component=COMPONENT, operation=COMMAND_ID)
        if not isinstance(cached, Mapping) or not isinstance(cached.get("profile"), dict):
            raise _fail("mautic_source_export_profile_missing_or_corrupt")
        rc, output = run_configured_external_operation(
            config,
            component=COMPONENT,
            operation=COMMAND_ID,
            profile=dict(cached["profile"]),
            operation_input=operation_input,
            resolved_context=context,
        )
        if rc != 0:
            raise _fail("mautic_source_export_external_operation_failed")
        result = _validate_result(json.loads(output), operation_input, context, Path(context["output_root"]))
        return {"status": "succeeded", "source_export": result}
    except (ConnectedHostSourceExportError, CustomSelectedTaskError) as exc:
        code = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(exc)) else "mautic_source_export_failed"
        return {"status": "failed", "reason": code}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "mautic_source_export_failed"}
    finally:
        if defaults_path:
            try:
                os.remove(defaults_path)
            except OSError:
                pass
