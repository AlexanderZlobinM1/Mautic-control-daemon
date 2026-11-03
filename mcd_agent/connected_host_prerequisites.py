"""Portal adapter for connected-host preparation and legacy pre-clean.

The signed external recipe owns all distro paths and host mechanics.  MCD
admits the exact cached closure, resolves only local installation identity and
invokes the existing generic runner.  Portal supplies identifiers and policy,
never paths, argv or credentials.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from typing import Any, Mapping

from mcd_agent.custom_scripts import (
    CustomSelectedTaskError,
    load_configured_profile_cache,
    run_configured_external_operation,
)
from mcd_agent.inventory import InstanceInventory


BINDING_SCHEMA = "mcd-mautic-connected-host-prerequisites-binding-v1"
CAPABILITY_ID = "mautic_host_prerequisites"
CAPABILITY_VERSION = "1.0"
CATALOG_ITEM_ID = "mcc.scenario.mautic-connected-host-prerequisites"
CATALOG_ITEM_REVISION = "1.0.4"
COMPONENT = CAPABILITY_ID
CONTEXT_SCHEMA = "mcd-mautic-connected-host-prerequisites-local-context-v1"
FRAME_SCHEMA = "mcd-self-hosted-executable-output-v1"
RESULT_SCHEMA = "mcd-mautic-connected-host-prerequisites-v1"
TOMBSTONE_SCHEMA = "mcd-mautic-connected-host-prerequisites-tombstone-v1"

COMMANDS = {
    "prepare_create": "portal://mcd/input/mautic-host-prerequisites-prepare-create-v1",
    "preclean_legacy": "portal://mcd/input/mautic-host-prerequisites-preclean-legacy-v1",
}
INPUT_FIELDS = {
    "operation_id",
    "target_instance_uid",
    "target_dns",
    "dns_check_mode",
    "dns_expected_addresses_json",
    "preserve_certificate",
}

_HEX = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_DNS = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class ConnectedHostPrerequisitesError(RuntimeError):
    """Bounded public admission or execution error."""


def _fail(code: str) -> ConnectedHostPrerequisitesError:
    return ConnectedHostPrerequisitesError(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, *, max_bytes: int, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise _fail(code)
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except ConnectedHostPrerequisitesError:
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
        "commands": [
            {
                "command_id": command_id,
                "version": "1.0",
                "execution": "generic_primitive",
                "mutates_state": True,
                "input_schema_ref": schema_ref,
                "target_scope": "host",
                "binding_schema": BINDING_SCHEMA,
                "binding_sha256": binding_sha256,
            }
            for command_id, schema_ref in COMMANDS.items()
        ],
    }


def installed_binding(config: Any) -> dict[str, Any]:
    """Validate the complete cached closure before advertising either command."""
    root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
    manifest, _ = _read_json(root / "manifest.json", max_bytes=512 * 1024,
                             code="mautic_host_prerequisites_manifest_invalid")
    resources = manifest.get("resources")
    if not isinstance(resources, list):
        raise _fail("mautic_host_prerequisites_binding_resource_missing")
    logical_identity_file = "bindings/mautic-connected-host-prerequisites-binding-v1.json"
    identity_rows = [
        row for row in resources
        if isinstance(row, Mapping)
        and row.get("role") == "binding_identity"
        and row.get("file") == logical_identity_file
    ]
    active_bindings = manifest.get("active_bindings")
    if active_bindings is not None and not isinstance(active_bindings, Mapping):
        raise _fail("mautic_host_prerequisites_binding_resource_invalid")
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
            raise _fail("mautic_host_prerequisites_binding_resource_invalid")
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
        raise _fail("mautic_host_prerequisites_binding_resource_missing")
    resource = identity_rows[0]
    allowed_resource_keys = {
        frozenset({"role", "file", "sha256", "size_bytes", "canonical_json_sha256"}),
        frozenset({"role", "file", "cache_file", "sha256", "size_bytes", "canonical_json_sha256"}),
    }
    if frozenset(resource) not in allowed_resource_keys or not isinstance(resource.get("size_bytes"), int):
        raise _fail("mautic_host_prerequisites_binding_resource_invalid")
    identity_path = _safe_cache_path(
        root, resource.get("cache_file", resource.get("file")),
        code="mautic_host_prerequisites_binding_resource_invalid",
    )
    identity, raw = _read_json(identity_path, max_bytes=256 * 1024,
                               code="mautic_host_prerequisites_binding_resource_invalid")
    binding_sha256 = "sha256:" + hashlib.sha256(_canonical(identity)).hexdigest()
    if (len(raw) != resource["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != resource.get("sha256")
            or hashlib.sha256(_canonical(identity)).hexdigest() != resource.get("canonical_json_sha256")
            or _SHA.fullmatch(binding_sha256) is None):
        raise _fail("mautic_host_prerequisites_binding_digest_mismatch")
    if selector is not None and selector.get("binding_sha256") != binding_sha256:
        raise _fail("mautic_host_prerequisites_binding_digest_mismatch")
    if (identity.get("schema") != BINDING_SCHEMA
            or identity.get("catalog_item_id") != CATALOG_ITEM_ID
            or identity.get("catalog_item_revision") != CATALOG_ITEM_REVISION):
        raise _fail("mautic_host_prerequisites_binding_identity_invalid")

    expected_identity_capability = _expected_capability(binding_sha256)
    expected_identity_capability["execution"] = "generic_primitive"
    expected_identity_capability["target_scope"] = "host"
    for command in expected_identity_capability["commands"]:
        command.pop("binding_schema")
        command.pop("binding_sha256")
    if identity.get("capability") != expected_identity_capability:
        raise _fail("mautic_host_prerequisites_capability_invalid")

    operation_bindings = identity.get("operation_bindings")
    all_manifest_bindings = manifest.get("operation_bindings")
    if (not isinstance(operation_bindings, list)
            or not isinstance(all_manifest_bindings, list)
            or any(not isinstance(row, Mapping) for row in operation_bindings)
            or any(sum(_canonical(row) == _canonical(candidate) for candidate in all_manifest_bindings if isinstance(candidate, Mapping)) != 1 for row in operation_bindings)
            or [row.get("operation") for row in operation_bindings if isinstance(row, Mapping)] != list(COMMANDS)):
        raise _fail("mautic_host_prerequisites_operation_bindings_invalid")

    input_contract = identity.get("input_contract")
    expected_input_rules = {
            "dns_check_mode": "require_exact",
            "dns_expected_addresses_json": "exactly one IPv4",
            "preserve_certificate": "true",
            "target_instance_uid": "equals canonical target_dns FQDN",
    }
    if (not isinstance(input_contract, Mapping)
            or set(input_contract) != {
                "type", "allowed_keys", "additional_properties", "worker_context_schema", "rules",
            }
            or input_contract.get("type") != "exact_string_map"
            or not isinstance(input_contract.get("allowed_keys"), list)
            or len(input_contract["allowed_keys"]) != len(INPUT_FIELDS)
            or set(input_contract["allowed_keys"]) != INPUT_FIELDS
            or input_contract.get("additional_properties") is not False
            or input_contract.get("worker_context_schema") != CONTEXT_SCHEMA
            or input_contract.get("rules") != expected_input_rules):
        raise _fail("mautic_host_prerequisites_input_contract_invalid")

    artifact = identity.get("artifact")
    profile = identity.get("profile")
    if not isinstance(artifact, Mapping) or not isinstance(profile, Mapping):
        raise _fail("mautic_host_prerequisites_binding_identity_invalid")
    if (not isinstance(artifact.get("sha256"), str)
            or _HEX.fullmatch(artifact["sha256"]) is None
            or not isinstance(artifact.get("size_bytes"), int)):
        raise _fail("mautic_host_prerequisites_artifact_identity_invalid")
    key = str(artifact.get("key") or "")
    archive = root / "downloads" / key / f"{key}-{artifact['sha256']}.tar.gz"
    try:
        if (archive.is_symlink() or not archive.is_file()
                or archive.stat().st_size != artifact["size_bytes"]
                or hashlib.sha256(archive.read_bytes()).hexdigest() != artifact["sha256"]):
            raise _fail("mautic_host_prerequisites_artifact_missing_or_corrupt")
    except OSError:
        raise _fail("mautic_host_prerequisites_artifact_missing_or_corrupt") from None
    if (profile.get("schema") != "mcc-mautic-connected-host-prerequisites-profile-v1"
            or profile.get("version") != CATALOG_ITEM_REVISION
            or not isinstance(profile.get("file"), str)
            or not isinstance(profile.get("sha256"), str)
            or _HEX.fullmatch(profile["sha256"]) is None):
        raise _fail("mautic_host_prerequisites_profile_identity_invalid")
    profile_path = _safe_cache_path(root, profile["file"],
                                    code="mautic_host_prerequisites_profile_missing_or_corrupt")
    try:
        if hashlib.sha256(profile_path.read_bytes()).hexdigest() != profile["sha256"]:
            raise _fail("mautic_host_prerequisites_profile_missing_or_corrupt")
        profile_value = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError:
        raise _fail("mautic_host_prerequisites_profile_missing_or_corrupt") from None
    except (UnicodeError, ValueError, TypeError):
        raise _fail("mautic_host_prerequisites_profile_missing_or_corrupt") from None
    if (not isinstance(profile_value, Mapping)
            or profile_value.get("catalog_item") != {"id": CATALOG_ITEM_ID, "revision": CATALOG_ITEM_REVISION}):
        raise _fail("mautic_host_prerequisites_profile_identity_invalid")

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
    except ConnectedHostPrerequisitesError:
        return None


def _validate_input(value: object) -> dict[str, str]:
    if (not isinstance(value, Mapping)
            or set(value) != INPUT_FIELDS
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items())):
        raise _fail("mautic_host_prerequisites_input_invalid")
    result = dict(value)
    if (_IDENTITY.fullmatch(result["operation_id"]) is None
            or "@" in result["target_instance_uid"]
            or _IDENTITY.fullmatch(result["target_instance_uid"]) is None
            or result["target_dns"] != result["target_dns"].lower()
            or _DNS.fullmatch(result["target_dns"]) is None
            or result["target_instance_uid"] != result["target_dns"]
            or result["dns_check_mode"] != "require_exact"
            or result["preserve_certificate"] != "true"):
        raise _fail("mautic_host_prerequisites_input_invalid")
    try:
        addresses = json.loads(result["dns_expected_addresses_json"])
    except (TypeError, ValueError):
        raise _fail("mautic_host_prerequisites_dns_policy_invalid") from None
    if (not isinstance(addresses, list)
            or len(addresses) != 1
            or any(not isinstance(item, str) or not item for item in addresses)
            or result["dns_expected_addresses_json"] != _canonical(addresses).decode("ascii")):
        raise _fail("mautic_host_prerequisites_dns_policy_invalid")
    try:
        if ipaddress.ip_address(addresses[0]).version != 4:
            raise ValueError
    except ValueError:
        raise _fail("mautic_host_prerequisites_dns_policy_invalid") from None
    return result


def _source_export_roots(config: Any) -> list[str]:
    base = Path(str(getattr(config, "custom_cache_dir", "") or "")) / "operation-output" / "mautic-source-export"
    if base.is_symlink() or not base.is_dir():
        return []
    roots: list[str] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if (child.is_symlink() or not child.is_dir()
                or any(path.is_symlink() or not path.is_file() for path in (
                    child / "files.tar.gz", child / "database.sql.gz",
                ))):
            continue
        roots.append(str(child.resolve()))
    return roots


def _tombstone_root(config: Any) -> Path:
    cache_root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise _fail("mautic_host_prerequisites_tombstone_invalid")
    root = cache_root
    for part in ("operation-state", "mautic-host-prerequisites"):
        root = root / part
        if root.is_symlink():
            raise _fail("mautic_host_prerequisites_tombstone_invalid")
        try:
            root.mkdir(mode=0o700, exist_ok=True)
            os.chmod(root, 0o700)
        except OSError:
            raise _fail("mautic_host_prerequisites_tombstone_invalid") from None
        if not root.is_dir() or root.stat().st_mode & 0o077:
            raise _fail("mautic_host_prerequisites_tombstone_invalid")
    return root.resolve()


def _tombstone_path(config: Any, operation_id: str) -> Path:
    name = hashlib.sha256(operation_id.encode("utf-8")).hexdigest() + ".json"
    return _tombstone_root(config) / name


def _store_tombstone(
    config: Any,
    operation_input: Mapping[str, str],
    target: Mapping[str, Any],
    source_export_roots: list[str],
    artifact_binding: Mapping[str, Any],
) -> None:
    value = {
        "schema": TOMBSTONE_SCHEMA,
        "operation_id": operation_input["operation_id"],
        "target_inventory": dict(target),
        "source_export_roots": list(source_export_roots),
        "artifact_binding": dict(artifact_binding),
    }
    raw = _canonical(value) + b"\n"
    path = _tombstone_path(config, operation_input["operation_id"])
    if path.exists() or path.is_symlink():
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise _fail("mautic_host_prerequisites_tombstone_invalid")
        except OSError:
            raise _fail("mautic_host_prerequisites_tombstone_invalid") from None
        existing, existing_raw = _read_json(
            path,
            max_bytes=64 * 1024,
            code="mautic_host_prerequisites_tombstone_invalid",
        )
        if existing != value or existing_raw != raw:
            raise _fail("mautic_host_prerequisites_tombstone_conflict")
        return
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".tombstone-", dir=str(path.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise _fail("mautic_host_prerequisites_tombstone_invalid")
            existing, existing_raw = _read_json(
                path,
                max_bytes=64 * 1024,
                code="mautic_host_prerequisites_tombstone_invalid",
            )
            if existing != value or existing_raw != raw:
                raise _fail("mautic_host_prerequisites_tombstone_conflict")
        os.unlink(temporary)
        temporary = ""
    except OSError:
        raise _fail("mautic_host_prerequisites_tombstone_invalid") from None
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _load_tombstone(
    config: Any,
    operation_input: Mapping[str, str],
    source_export_roots: list[str],
    artifact_binding: Mapping[str, Any],
) -> dict[str, Any]:
    path = _tombstone_path(config, operation_input["operation_id"])
    try:
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise _fail("mautic_host_prerequisites_tombstone_invalid")
    except FileNotFoundError:
        raise _fail("mautic_host_prerequisites_tombstone_missing") from None
    except OSError:
        raise _fail("mautic_host_prerequisites_tombstone_invalid") from None
    value, _ = _read_json(
        path,
        max_bytes=64 * 1024,
        code="mautic_host_prerequisites_tombstone_missing",
    )
    target = value.get("target_inventory")
    if (set(value) != {
            "schema", "operation_id", "target_inventory", "source_export_roots", "artifact_binding",
        }
            or value.get("schema") != TOMBSTONE_SCHEMA
            or value.get("operation_id") != operation_input["operation_id"]
            or value.get("source_export_roots") != source_export_roots
            or value.get("artifact_binding") != dict(artifact_binding)
            or not isinstance(target, Mapping)
            or set(target) != {
                "status", "instance_uid", "primary_domain", "domains", "root", "database_name", "database_user",
            }
            or target.get("status") != "present"
            or target.get("instance_uid") != operation_input["target_instance_uid"]
            or target.get("primary_domain") != operation_input["target_dns"]
            or not isinstance(target.get("domains"), list)
            or operation_input["target_dns"] not in target["domains"]
            or not isinstance(target.get("root"), str)
            or not Path(target["root"]).is_absolute()
            or not isinstance(target.get("database_name"), str)
            or not target["database_name"]
            or not isinstance(target.get("database_user"), str)
            or not target["database_user"]):
        raise _fail("mautic_host_prerequisites_tombstone_invalid")
    replay = dict(target)
    replay["status"] = "absent"
    return replay


def _inventory_context(
    command_id: str,
    operation_input: Mapping[str, str],
    job: Mapping[str, Any],
    config: Any,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    installation = job.get("installation")
    if not isinstance(installation, Mapping):
        raise _fail("mautic_host_prerequisites_installation_binding_invalid")
    control_id = str(installation.get("installation_id") or "")
    control_dns = str(installation.get("dns_name") or "").strip().lower()
    if _IDENTITY.fullmatch(control_id) is None or _DNS.fullmatch(control_dns) is None:
        raise _fail("mautic_host_prerequisites_installation_binding_invalid")

    wanted_uid = operation_input["target_instance_uid"].lower()
    wanted_dns = operation_input["target_dns"]
    inventory = InstanceInventory(str(getattr(config, "state_db_path", "") or ""))
    try:
        matches = []
        for item in inventory.list_instances():
            identities = {
                str(item.instance_uid or "").strip().lower(),
                str(item.primary_domain or "").strip().lower(),
                *(str(value).strip().lower() for value in (item.domains or [])),
            }
            if wanted_uid == str(item.instance_uid or "").strip().lower() or wanted_dns in identities:
                matches.append(item)
    finally:
        inventory.conn.close()
    if len(matches) > 1:
        raise _fail("mautic_host_prerequisites_target_ambiguous")
    if command_id == "prepare_create" and matches:
        raise _fail("mautic_host_prerequisites_target_occupied")

    target = {
        "status": "absent",
        "instance_uid": operation_input["target_instance_uid"],
        "primary_domain": operation_input["target_dns"],
        "domains": [operation_input["target_dns"]],
        "root": "",
        "database_name": "",
        "database_user": "",
    }
    if matches:
        item = matches[0]
        if ("@" in str(item.instance_uid or "")
                or str(item.instance_uid or "").strip().lower() != wanted_uid
                or str(item.primary_domain or "").strip().lower() != wanted_dns
                or not item.db):
            raise _fail("mautic_host_prerequisites_target_identity_mismatch")
        root = Path(str(item.root or ""))
        if root.is_symlink() or not root.is_absolute():
            raise _fail("mautic_host_prerequisites_target_context_invalid")
        target = {
            "status": "present",
            "instance_uid": str(item.instance_uid),
            "primary_domain": wanted_dns,
            "domains": sorted({wanted_dns, *(str(value).strip().lower() for value in (item.domains or []) if value)}),
            "root": str(root.resolve(strict=False)),
            "database_name": str(item.db.name or ""),
            "database_user": str(item.db.user or ""),
        }
        if not target["database_name"] or not target["database_user"]:
            raise _fail("mautic_host_prerequisites_target_context_invalid")

    artifact_binding = {
        "catalog_item_id": CATALOG_ITEM_ID,
        "catalog_item_revision": binding["catalog_item_revision"],
        "artifact_sha256": binding["artifact_sha256"],
        "profile_sha256": binding["profile_sha256"],
        "binding_sha256": binding["binding_sha256"],
    }
    source_export_roots = _source_export_roots(config)
    if command_id == "preclean_legacy":
        if not source_export_roots:
            raise _fail("mautic_host_prerequisites_source_export_missing")
        if matches:
            _store_tombstone(
                config,
                operation_input,
                target,
                source_export_roots,
                artifact_binding,
            )
        else:
            target = _load_tombstone(
                config,
                operation_input,
                source_export_roots,
                artifact_binding,
            )

    sibling_cli = Path(sys.executable).resolve().with_name("mcd-cli")
    cli_path = str(sibling_cli) if sibling_cli.is_file() else shutil.which("mcd-cli")
    config_path = Path(str(getattr(config, "config_file_path", "") or ""))
    if not cli_path or not Path(cli_path).is_file() or not config_path.is_file():
        raise _fail("mautic_host_prerequisites_runtime_context_invalid")
    return {
        "schema": CONTEXT_SCHEMA,
        "command_id": command_id,
        "control_installation_id": control_id,
        "control_dns": control_dns,
        "mcd_cli_path": str(Path(cli_path).resolve()),
        "mcd_config_path": str(config_path.resolve()),
        "target_inventory": target,
        "source_export_roots": source_export_roots,
        "artifact_binding": artifact_binding,
    }


def _validate_result(command_id: str, operation_input: Mapping[str, str], binding: Mapping[str, Any], result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise _fail("mautic_host_prerequisites_result_invalid")
    expected_binding = {
        "catalog_item_id": CATALOG_ITEM_ID,
        "catalog_item_revision": binding["catalog_item_revision"],
        "artifact_sha256": binding["artifact_sha256"],
        "profile_sha256": binding["profile_sha256"],
        "binding_sha256": binding["binding_sha256"],
    }
    target = result.get("target")
    expected_keys = {
        "schema", "contract_version", "status", "operation", "operation_id", "target",
        "mutation_started", "rollback", "artifact_binding", "preserved_boundaries", "post_state",
    }
    if (result.get("schema") != RESULT_SCHEMA
            or set(result) != expected_keys
            or result.get("contract_version") != "1.0"
            or result.get("status") not in {"applied", "noop"}
            or result.get("operation") != command_id
            or result.get("operation_id") != operation_input["operation_id"]
            or target != {
                "instance_uid": operation_input["target_instance_uid"],
                "dns": operation_input["target_dns"],
            }
            or not isinstance(result.get("mutation_started"), bool)
            or result.get("rollback") not in {"not_attempted", "not_needed", "succeeded"}
            or result.get("artifact_binding") != expected_binding
            or not isinstance(result.get("preserved_boundaries"), Mapping)
            or not isinstance(result.get("post_state"), Mapping)):
        raise _fail("mautic_host_prerequisites_result_invalid")

    boundaries = result["preserved_boundaries"]
    if set(boundaries) != {"certificate", "unrelated"}:
        raise _fail("mautic_host_prerequisites_result_invalid")
    certificate = boundaries["certificate"]
    unrelated = boundaries["unrelated"]
    if (not isinstance(certificate, Mapping)
            or set(certificate) != {
                "requested", "directory", "before_sha256", "after_sha256", "disposition", "unchanged",
            }
            or certificate.get("requested") is not True
            or not isinstance(certificate.get("directory"), str)
            or not Path(certificate["directory"]).is_absolute()
            or certificate.get("before_sha256") not in {""} and _SHA.fullmatch(str(certificate.get("before_sha256"))) is None
            or _SHA.fullmatch(str(certificate.get("after_sha256") or "")) is None
            or certificate.get("disposition") not in {"created", "preserved"}
            or not isinstance(certificate.get("unchanged"), bool)
            or not isinstance(unrelated, Mapping)
            or set(unrelated) != {"checked", "before_sha256", "after_sha256", "unchanged"}
            or unrelated.get("checked") is not True
            or _SHA.fullmatch(str(unrelated.get("before_sha256") or "")) is None
            or unrelated.get("after_sha256") != unrelated.get("before_sha256")
            or unrelated.get("unchanged") is not True):
        raise _fail("mautic_host_prerequisites_result_invalid")
    if command_id == "preclean_legacy":
        if (certificate["disposition"] != "preserved"
                or certificate["before_sha256"] != certificate["after_sha256"]
                or certificate["unchanged"] is not True):
            raise _fail("mautic_host_prerequisites_result_invalid")
    elif certificate["disposition"] == "created":
        if certificate["before_sha256"] != "" or certificate["unchanged"] is not False:
            raise _fail("mautic_host_prerequisites_result_invalid")
    elif (certificate["before_sha256"] != certificate["after_sha256"]
          or certificate["unchanged"] is not True):
        raise _fail("mautic_host_prerequisites_result_invalid")

    post_state = result["post_state"]
    if command_id == "prepare_create":
        if set(post_state) != {
            "managed_root_parent", "certificate", "temporary_vhost_absent", "nginx_valid", "dns",
        }:
            raise _fail("mautic_host_prerequisites_result_invalid")
        parent = post_state["managed_root_parent"]
        material = post_state["certificate"]
        dns = post_state["dns"]
        if (not isinstance(parent, Mapping)
                or set(parent) != {"path", "exists", "owned_by_operation"}
                or not isinstance(parent.get("path"), str)
                or not Path(parent["path"]).is_absolute()
                or parent.get("exists") is not True
                or not isinstance(parent.get("owned_by_operation"), bool)
                or not isinstance(material, Mapping)
                or set(material) != {"directory", "fullchain_sha256", "privkey_sha256", "covers_target_dns", "current"}
                or material.get("directory") != certificate["directory"]
                or _SHA.fullmatch(str(material.get("fullchain_sha256") or "")) is None
                or _SHA.fullmatch(str(material.get("privkey_sha256") or "")) is None
                or material.get("covers_target_dns") is not True
                or material.get("current") is not True
                or post_state.get("temporary_vhost_absent") is not True
                or post_state.get("nginx_valid") is not True
                or not isinstance(dns, Mapping)
                or set(dns) != {"mode", "observed_addresses"}
                or dns.get("mode") != operation_input["dns_check_mode"]
                or not isinstance(dns.get("observed_addresses"), list)
                or any(not isinstance(item, str) or not item for item in dns["observed_addresses"])
                or dns["observed_addresses"] != sorted(set(dns["observed_addresses"]))):
            raise _fail("mautic_host_prerequisites_result_invalid")
    else:
        expected_post = {
            "root_absent", "vhost_absent", "database_absent", "database_user_absent",
            "inventory_absent", "certificate_present", "nginx_valid", "mcd_cli_status",
            "mcd_cli_instances",
        }
        if (set(post_state) != expected_post
                or any(post_state.get(key) is not True for key in (
                    "root_absent", "vhost_absent", "database_absent", "database_user_absent",
                    "inventory_absent", "certificate_present", "nginx_valid",
                ))
                or post_state.get("mcd_cli_status") not in {"ok", "warning"}
                or isinstance(post_state.get("mcd_cli_instances"), bool)
                or not isinstance(post_state.get("mcd_cli_instances"), int)
                or post_state["mcd_cli_instances"] < 0):
            raise _fail("mautic_host_prerequisites_result_invalid")
    return dict(result)


def _validate_failure_frame(
    command_id: str,
    operation_input: Mapping[str, str],
    output: str,
) -> dict[str, Any] | None:
    """Keep bounded external failure evidence instead of reducing it to one code."""
    try:
        frame = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(frame, Mapping) or set(frame) != {
        "schema", "contract_version", "frame_type", "status", "exit_code",
        "completed", "total", "rollback", "result", "error_code",
    }:
        return None
    nested = frame.get("result")
    if not isinstance(nested, Mapping) or set(nested) != {
        "schema", "contract_version", "status", "operation", "operation_id",
        "error_code", "mutation_started", "rollback",
    }:
        return None
    error_code = nested.get("error_code")
    operation_id = nested.get("operation_id")
    exit_code = frame.get("exit_code")
    if (
        frame.get("schema") != FRAME_SCHEMA
        or frame.get("contract_version") != "1.0"
        or frame.get("frame_type") != "result"
        or frame.get("status") != "failed"
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code == 0
        or frame.get("completed") != 0
        or frame.get("total") != 1
        or frame.get("error_code") != error_code
        or frame.get("rollback") != nested.get("rollback")
        or nested.get("schema") != RESULT_SCHEMA
        or nested.get("contract_version") != "1.0"
        or nested.get("status") != "failed"
        or nested.get("operation") != command_id
        or operation_id not in {"", operation_input["operation_id"]}
        or not isinstance(error_code, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", error_code) is None
        or not isinstance(nested.get("mutation_started"), bool)
        or nested.get("rollback") not in {"not_attempted", "not_needed", "succeeded"}
    ):
        return None
    return dict(nested)


def execute(job: Mapping[str, Any], *, config: Any) -> Mapping[str, Any]:
    command = job.get("command")
    command_id = str((command or {}).get("command_id") or "") if isinstance(command, Mapping) else ""
    if command_id not in COMMANDS:
        return {"status": "failed", "reason": "mautic_host_prerequisites_command_invalid"}
    try:
        binding = installed_binding(config)
        if (command.get("binding_schema") != binding["binding_schema"]
                or command.get("binding_sha256") != binding["binding_sha256"]):
            raise _fail("mautic_host_prerequisites_binding_mismatch")
        operation_input = _validate_input(job.get("input"))
        context = _inventory_context(command_id, operation_input, job, config, binding)
        cached = load_configured_profile_cache(config, component=COMPONENT, operation=command_id)
        if not isinstance(cached, Mapping) or not isinstance(cached.get("profile"), dict):
            raise _fail("mautic_host_prerequisites_profile_missing_or_corrupt")
        rc, output = run_configured_external_operation(
            config,
            component=COMPONENT,
            operation=command_id,
            profile=dict(cached["profile"]),
            operation_input=operation_input,
            resolved_context=context,
        )
        if rc != 0:
            failure = _validate_failure_frame(command_id, operation_input, output)
            if failure is not None:
                return {
                    "status": "failed",
                    "reason": failure["error_code"],
                    "host_prerequisites": failure,
                }
            if isinstance(output, str) and re.fullmatch(r"custom_operation_[a-z0-9_]{1,112}", output):
                return {"status": "failed", "reason": output}
            raise _fail("mautic_host_prerequisites_external_operation_failed")
        result = _validate_result(command_id, operation_input, binding, json.loads(output))
        return {"status": "succeeded", "host_prerequisites": result}
    except (ConnectedHostPrerequisitesError, CustomSelectedTaskError) as exc:
        code = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(exc)) else "mautic_host_prerequisites_failed"
        return {"status": "failed", "reason": code}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "mautic_host_prerequisites_failed"}
