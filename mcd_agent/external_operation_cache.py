"""Portal-only cache synchronization for signed external-operation closures."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from mcd_agent.archive_utils import safe_extract_tar


CAPABILITY_ID = "external_operation_cache"
COMMAND_ID = "sync_binding"
DELIVERY_SCHEMA = "portal-mcd-transient-artifact-delivery-v1"
CLOSURE_SCHEMA = "mcc-external-operation-cache-closure-v1"
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_INPUT_FIELDS = {
    "catalog_item_id", "catalog_item_revision", "closure_digest",
    "closure_size_bytes", "closure_media_type", "binding_schema", "binding_sha256",
}


class ExternalOperationCacheError(RuntimeError):
    pass


def _fail(code: str) -> ExternalOperationCacheError:
    return ExternalOperationCacheError(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_input(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _INPUT_FIELDS:
        raise _fail("external_operation_cache_input_invalid")
    result = dict(value)
    if (not isinstance(result["catalog_item_id"], str)
            or not isinstance(result["catalog_item_revision"], str)
            or not isinstance(result["closure_digest"], str)
            or _SHA.fullmatch(result["closure_digest"]) is None
            or isinstance(result["closure_size_bytes"], bool)
            or not isinstance(result["closure_size_bytes"], int)
            or not 0 < result["closure_size_bytes"] <= 128 * 1024 * 1024
            or result["closure_media_type"] != "application/gzip"
            or not isinstance(result["binding_schema"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", result["binding_schema"]) is None
            or not isinstance(result["binding_sha256"], str)
            or _SHA.fullmatch(result["binding_sha256"]) is None):
        raise _fail("external_operation_cache_input_invalid")
    return result


def _ingress_path(config: Any, digest: str) -> Path:
    return Path(str(getattr(config, "custom_cache_dir", "") or "")) / "portal-ingress" / (digest[7:] + ".tar.gz")


def _install_target(root: Path, relative: PurePosixPath) -> Path:
    if root.is_symlink():
        raise _fail("external_operation_cache_root_invalid")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise _fail("external_operation_cache_install_path_invalid")
    return cursor


def stage_transient_delivery(
    command: Mapping[str, Any],
    *,
    config: Any,
    downloader: Callable[..., bytes],
) -> dict[str, Any]:
    """Download one transient Portal grant, then return a grant-free job."""
    result = dict(command)
    delivery = result.pop("artifact_delivery", None)
    capability = result.get("capability")
    declared_command = result.get("command")
    if (not isinstance(capability, Mapping)
            or capability.get("capability_id") != CAPABILITY_ID
            or not isinstance(declared_command, Mapping)
            or declared_command.get("command_id") != COMMAND_ID):
        raise _fail("external_operation_cache_delivery_command_invalid")
    value = _validate_input(result.get("input"))
    if not isinstance(delivery, Mapping) or set(delivery) != {"schema", "contract_version", "artifacts"}:
        raise _fail("external_operation_cache_delivery_invalid")
    artifacts = delivery.get("artifacts")
    if delivery.get("schema") != DELIVERY_SCHEMA or delivery.get("contract_version") != "1.0" or not isinstance(artifacts, list) or len(artifacts) != 1:
        raise _fail("external_operation_cache_delivery_invalid")
    artifact = artifacts[0]
    expected_keys = {
        "role", "artifact_path", "artifact_grant", "artifact_digest",
        "artifact_size_bytes", "artifact_media_type", "artifact_revision", "trusted_catalog",
    }
    if not isinstance(artifact, Mapping) or set(artifact) != expected_keys:
        raise _fail("external_operation_cache_delivery_invalid")
    catalog = artifact.get("trusted_catalog")
    if not isinstance(catalog, Mapping) or set(catalog) != {"source", "transport", "status", "revision", "artifact_digest"}:
        raise _fail("external_operation_cache_delivery_invalid")
    path = artifact.get("artifact_path")
    grant = artifact.get("artifact_grant")
    if (artifact.get("role") != "binding_closure"
            or artifact.get("artifact_digest") != value["closure_digest"]
            or artifact.get("artifact_size_bytes") != value["closure_size_bytes"]
            or artifact.get("artifact_media_type") != value["closure_media_type"]
            or artifact.get("artifact_revision") != value["catalog_item_revision"]
            or catalog != {
                "source": "mcc", "transport": "portal_authenticated_catalog", "status": "active",
                "revision": value["catalog_item_revision"], "artifact_digest": value["closure_digest"],
            }
            or not isinstance(path, str) or not path.startswith("/") or "://" in path or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
            or not isinstance(grant, str) or not grant or len(grant) > 512):
        raise _fail("external_operation_cache_delivery_binding_mismatch")
    try:
        body = downloader(path=path, grant=grant, expected_size=value["closure_size_bytes"])
    except Exception as exc:
        raise _fail("external_operation_cache_delivery_failed") from exc
    if not isinstance(body, bytes) or len(body) != value["closure_size_bytes"] or "sha256:" + hashlib.sha256(body).hexdigest() != value["closure_digest"]:
        raise _fail("external_operation_cache_delivery_integrity_invalid")
    target = _ingress_path(config, value["closure_digest"])
    if target.parent.parent.is_symlink():
        raise _fail("external_operation_cache_root_invalid")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".ingress-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


def _relative(value: object, code: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise _fail(code)
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail(code)
    return path


def _binding_cache_file(digest: str) -> PurePosixPath:
    if _HEX.fullmatch(digest) is None:
        raise _fail("external_operation_cache_resource_invalid")
    return PurePosixPath("bindings/by-sha256") / f"{digest}.json"


def _normalize_binding_manifest(
    manifest: object,
    *,
    logical_file: PurePosixPath,
    resource_sha256: str,
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], PurePosixPath]:
    if not isinstance(manifest, Mapping):
        raise _fail("external_operation_cache_manifest_invalid")
    normalized = dict(manifest)
    resources = normalized.get("resources")
    if not isinstance(resources, list) or any(not isinstance(row, Mapping) for row in resources):
        raise _fail("external_operation_cache_manifest_invalid")
    candidates = [
        row for row in resources
        if row.get("role") == "binding_identity"
        and row.get("cache_file", row.get("file")) == logical_file.as_posix()
        and row.get("sha256") == resource_sha256
    ]
    if len(candidates) != 1:
        raise _fail("external_operation_cache_binding_identity_missing")
    cache_file = _binding_cache_file(resource_sha256)
    normalized_resources: list[dict[str, Any]] = []
    for row in resources:
        copied = dict(row)
        if row is candidates[0]:
            copied["file"] = logical_file.as_posix()
            copied["cache_file"] = cache_file.as_posix()
        normalized_resources.append(copied)
    normalized["resources"] = normalized_resources

    active = normalized.get("active_bindings", {})
    if not isinstance(active, Mapping) or active:
        raise _fail("external_operation_cache_manifest_invalid")
    normalized_active = {value["binding_schema"]: {
        "catalog_item_id": value["catalog_item_id"],
        "catalog_item_revision": value["catalog_item_revision"],
        "binding_sha256": value["binding_sha256"],
        "cache_file": cache_file.as_posix(),
        "resource_sha256": resource_sha256,
    }}
    normalized["active_bindings"] = normalized_active
    return normalized, cache_file


def _merge_manifest(existing: object, incoming: object) -> dict[str, Any]:
    if not isinstance(incoming, Mapping) or incoming.get("manifest_version") != 1:
        raise _fail("external_operation_cache_manifest_invalid")
    current = dict(existing) if isinstance(existing, Mapping) else {"manifest_version": 1, "items": []}
    if current.get("manifest_version") != 1 or not isinstance(current.get("items"), list):
        raise _fail("external_operation_cache_existing_manifest_invalid")

    def merge_rows(field: str, identity: Callable[[Mapping[str, Any]], tuple[Any, ...]]) -> None:
        old = current.get(field, [])
        new = incoming.get(field, [])
        if not isinstance(old, list) or not isinstance(new, list) or any(not isinstance(row, Mapping) for row in [*old, *new]):
            raise _fail("external_operation_cache_manifest_invalid")
        merged = [dict(row) for row in old]
        by_key = {identity(row): row for row in merged}
        if len(by_key) != len(merged):
            raise _fail("external_operation_cache_existing_manifest_invalid")
        for row in new:
            key = identity(row)
            prior = by_key.get(key)
            if prior is not None and _canonical(prior) != _canonical(row):
                raise _fail("external_operation_cache_manifest_conflict")
            if prior is None:
                copied = dict(row)
                merged.append(copied)
                by_key[key] = copied
        current[field] = merged

    def item_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
        """Match the identities already supported by custom manifest v1.

        A bundle key is not unique by itself: one immutable bundle may expose
        several entrypoints as separate items. Group rows intentionally have
        no key at all. Preserve both established forms while still making a
        conflicting replacement of the same logical row fail closed.
        """
        key = row.get("key")
        entrypoint = row.get("entrypoint")
        if isinstance(key, str) and key:
            return ("item", key, entrypoint if isinstance(entrypoint, str) else None)
        row_type = row.get("type")
        name = row.get("name")
        if isinstance(row_type, str) and row_type and isinstance(name, str) and name:
            return ("named", row_type, name)
        return ("opaque", hashlib.sha256(_canonical(row)).hexdigest())

    old_items = current.get("items", [])
    new_items = incoming.get("items", [])
    if isinstance(old_items, list) and isinstance(new_items, list):
        old_artifacts: dict[str, set[tuple[Any, Any]]] = {}
        for row in old_items:
            if isinstance(row, Mapping) and isinstance(row.get("key"), str) and row["key"]:
                old_artifacts.setdefault(row["key"], set()).add((row.get("file"), row.get("sha256")))
        for row in new_items:
            if not isinstance(row, Mapping) or not isinstance(row.get("key"), str) or not row["key"]:
                continue
            prior = old_artifacts.get(row["key"])
            if prior is not None and (row.get("file"), row.get("sha256")) not in prior:
                raise _fail("external_operation_cache_manifest_conflict")

    def operation_binding_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
        artifact = row.get("artifact")
        if not isinstance(artifact, Mapping):
            return (row.get("component"), row.get("operation"), None, None, None)
        return (
            row.get("component"), row.get("operation"),
            artifact.get("key"), artifact.get("sha256"), artifact.get("entrypoint"),
        )

    merge_rows("items", item_identity)
    merge_rows("operation_bindings", operation_binding_identity)
    merge_rows("resources", lambda row: (row.get("role"), row.get("cache_file", row.get("file"))))

    old_active = current.get("active_bindings", {})
    new_active = incoming.get("active_bindings", {})
    if (not isinstance(old_active, Mapping) or not isinstance(new_active, Mapping)
            or any(not isinstance(key, str) or not isinstance(row, Mapping) for key, row in [*old_active.items(), *new_active.items()])):
        raise _fail("external_operation_cache_manifest_invalid")
    active = {key: dict(row) for key, row in old_active.items()}
    for key, row in new_active.items():
        prior = active.get(key)
        if prior is not None and prior.get("catalog_item_id") != row.get("catalog_item_id"):
            raise _fail("external_operation_cache_manifest_conflict")
        active[key] = dict(row)
    if active:
        current["active_bindings"] = active
    return current


def sync_binding(job: Mapping[str, Any], *, config: Any) -> Mapping[str, Any]:
    """Verify and atomically activate one already downloaded cache closure."""
    try:
        value = _validate_input(job.get("input"))
        ingress = _ingress_path(config, value["closure_digest"])
        if ingress.is_symlink() or not ingress.is_file() or ingress.stat().st_size != value["closure_size_bytes"] or "sha256:" + hashlib.sha256(ingress.read_bytes()).hexdigest() != value["closure_digest"]:
            raise _fail("external_operation_cache_ingress_missing_or_corrupt")
        root = Path(str(getattr(config, "custom_cache_dir", "") or ""))
        if root.is_symlink():
            raise _fail("external_operation_cache_root_invalid")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".binding-sync-", dir=root))
        try:
            with tarfile.open(ingress, "r:gz") as archive:
                safe_extract_tar(archive, staged)
            descriptor_path = staged / "descriptor.json"
            if descriptor_path.is_symlink() or not descriptor_path.is_file() or descriptor_path.stat().st_size > 256 * 1024:
                raise _fail("external_operation_cache_descriptor_invalid")
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if (not isinstance(descriptor, Mapping)
                    or descriptor.get("schema") != CLOSURE_SCHEMA
                    or descriptor.get("catalog_item_id") != value["catalog_item_id"]
                    or descriptor.get("catalog_item_revision") != value["catalog_item_revision"]
                    or descriptor.get("binding_schema") != value["binding_schema"]
                    or descriptor.get("binding_sha256") != value["binding_sha256"]
                    or descriptor.get("install_mode") != "atomic_append_only_preserve_existing"
                    or descriptor.get("offline_only_after_sync") is not True):
                raise _fail("external_operation_cache_descriptor_invalid")
            resources = descriptor.get("resources")
            roles = [row.get("role") for row in resources if isinstance(row, Mapping)] if isinstance(resources, list) else []
            if not isinstance(resources, list) or len(resources) != 4 or set(roles) != {
                "binding_identity", "operation_binding_manifest", "operator_profile", "executable_bundle",
            }:
                raise _fail("external_operation_cache_descriptor_invalid")
            declared_paths = {"descriptor.json"}
            prepared: list[tuple[Path, PurePosixPath, str, str]] = []
            incoming_manifest: dict[str, Any] | None = None
            binding_install_path: PurePosixPath | None = None
            binding_resource_sha256: str | None = None
            for resource in resources:
                if not isinstance(resource, Mapping) or set(resource) != {"path", "role", "install_path", "sha256", "size_bytes"}:
                    raise _fail("external_operation_cache_resource_invalid")
                source_rel = _relative(resource.get("path"), "external_operation_cache_resource_invalid")
                install_rel = _relative(resource.get("install_path"), "external_operation_cache_resource_invalid")
                source = staged.joinpath(*source_rel.parts)
                if source.is_symlink() or not source.is_file() or not isinstance(resource.get("size_bytes"), int) or source.stat().st_size != resource["size_bytes"] or not isinstance(resource.get("sha256"), str) or _HEX.fullmatch(resource["sha256"]) is None or hashlib.sha256(source.read_bytes()).hexdigest() != resource["sha256"]:
                    raise _fail("external_operation_cache_resource_invalid")
                declared_paths.add(source_rel.as_posix())
                if install_rel.as_posix() == "manifest.json":
                    if resource.get("role") != "operation_binding_manifest":
                        raise _fail("external_operation_cache_resource_invalid")
                    incoming_manifest = json.loads(source.read_text(encoding="utf-8"))
                else:
                    prepared.append((source, install_rel, resource["sha256"], str(resource["role"])))
                    if resource.get("role") == "binding_identity":
                        binding_install_path = install_rel
                        binding_resource_sha256 = resource["sha256"]
            actual_paths = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
            if actual_paths != declared_paths or incoming_manifest is None:
                raise _fail("external_operation_cache_closure_members_invalid")
            if binding_install_path is None or binding_resource_sha256 is None:
                raise _fail("external_operation_cache_binding_identity_missing")
            incoming_manifest, binding_cache_file = _normalize_binding_manifest(
                incoming_manifest,
                logical_file=binding_install_path,
                resource_sha256=binding_resource_sha256,
                value=value,
            )
            prepared_targets = [
                (
                    source,
                    _install_target(root, binding_cache_file if role == "binding_identity" else install_rel),
                    digest,
                )
                for source, install_rel, digest, role in prepared
            ]

            identity_rows = [row for row in resources if isinstance(row, Mapping) and row.get("role") == "binding_identity"]
            if len(identity_rows) != 1:
                raise _fail("external_operation_cache_binding_identity_missing")
            identity = json.loads((staged / str(identity_rows[0]["path"])).read_text(encoding="utf-8"))
            if "sha256:" + hashlib.sha256(_canonical(identity)).hexdigest() != value["binding_sha256"]:
                raise _fail("external_operation_cache_binding_digest_mismatch")

            # Prove that this closure alone can produce the exact advertised
            # capability before touching the live manifest.  Existing rows are
            # merged and conflict-checked later under the cache lock.
            validation_root = staged / ".validation-cache"
            validation_root.mkdir(mode=0o700)
            validation_manifest = _merge_manifest({"manifest_version": 1, "items": []}, incoming_manifest)
            (validation_root / "manifest.json").write_bytes(_canonical(validation_manifest) + b"\n")
            for source, live_target, _digest in prepared_targets:
                relative = live_target.relative_to(root)
                target = validation_root / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            if value["binding_schema"] == "mcd-mautic-connected-host-lifecycle-binding-v1":
                from mcd_agent.connected_host_lifecycle import installed_binding
            elif value["binding_schema"] == "mcd-mautic-connected-host-source-export-binding-v1":
                from mcd_agent.connected_host_source_export import installed_binding
            elif value["binding_schema"] == "mcd-mautic-connected-host-prerequisites-binding-v1":
                from mcd_agent.connected_host_prerequisites import installed_binding
            else:
                raise _fail("external_operation_cache_binding_schema_unsupported")
            admitted = installed_binding(SimpleNamespace(custom_cache_dir=str(validation_root)))
            if admitted.get("binding_sha256") != value["binding_sha256"]:
                raise _fail("external_operation_cache_binding_digest_mismatch")

            lock = os.open(root, os.O_RDONLY)
            temporary_manifest: Path | None = None
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                manifest_path = root / "manifest.json"
                try:
                    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    existing = {"manifest_version": 1, "items": []}
                except (OSError, UnicodeError, ValueError):
                    raise _fail("external_operation_cache_existing_manifest_invalid") from None
                merged = _merge_manifest(existing, incoming_manifest)
                for source, target, digest in prepared_targets:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if target.exists():
                        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                            raise _fail("external_operation_cache_install_conflict")
                        continue
                    temporary_resource: Path | None = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".resource-", delete=False) as stream:
                            temporary_resource = Path(stream.name)
                            stream.write(source.read_bytes())
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.chmod(temporary_resource, 0o600)
                        os.replace(temporary_resource, target)
                    finally:
                        if temporary_resource is not None:
                            temporary_resource.unlink(missing_ok=True)
                with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".manifest-", delete=False) as stream:
                    temporary_manifest = Path(stream.name)
                    stream.write(_canonical(merged) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary_manifest, manifest_path.stat().st_mode & 0o777 if manifest_path.exists() else 0o644)
                os.replace(temporary_manifest, manifest_path)
            finally:
                if temporary_manifest is not None:
                    temporary_manifest.unlink(missing_ok=True)
                os.close(lock)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        return {
            "status": "succeeded",
            "catalog_item_id": value["catalog_item_id"],
            "catalog_item_revision": value["catalog_item_revision"],
            "binding_schema": value["binding_schema"],
            "binding_sha256": value["binding_sha256"],
            "closure_digest": value["closure_digest"],
        }
    except ExternalOperationCacheError as exc:
        return {"status": "failed", "reason": str(exc)}
    except (OSError, UnicodeError, ValueError, TypeError, tarfile.TarError, json.JSONDecodeError):
        return {"status": "failed", "reason": "external_operation_cache_sync_failed"}
