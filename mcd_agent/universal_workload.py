"""Read explicitly selected immutable workload data through the existing cache.

No catalog discovery, activation policy, settings clock, or executable runner is
owned here. The caller supplies its selected resource and chooses when to apply
its literal values through the existing configuration/settings machinery.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Any


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError("universal resource path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError("universal resource path is invalid")
    return value


def selected_universal_resource(ref: dict[str, Any]) -> dict[str, Any]:
    """Adapt the existing host runtime-map reference to the shared data cache."""
    fields = {"schema", "key", "file", "sha256", "size_bytes", "resource_root", "catalog", "files"}
    if (not isinstance(ref, dict) or set(ref) != fields
            or ref["schema"] != "mcc-universal-workload-resource-ref-v1"
            or type(ref["size_bytes"]) is not int or ref["size_bytes"] < 0):
        raise RuntimeError("selected universal workload resource reference is invalid")
    _relative_path(ref["file"])
    selected = {key: ref[key] for key in ("key", "file", "sha256", "size_bytes")}
    selected.update(kind="data_only_no_entrypoint", entrypoint=None,
                    resource={key: ref[key] for key in ("resource_root", "catalog", "files")})
    _selection_pins(selected)
    return selected


def _selection_pins(selected: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if (selected.get("kind") != "data_only_no_entrypoint"
            or selected.get("entrypoint") is not None
            or selected.get("key") in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(selected.get("key", "")))):
        raise RuntimeError("selected universal resource must be data-only with an exact key")
    descriptor = selected.get("resource")
    if not isinstance(descriptor, dict):
        raise RuntimeError("selected universal resource descriptor is missing")
    root = _relative_path(descriptor.get("resource_root"))
    rows = descriptor.get("files")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("selected universal resource member pins are missing")
    pins: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("selected universal resource member pin is invalid")
        name = _relative_path(row.get("path"))
        if (not name.startswith(root + "/") or name in pins
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))
                or type(row.get("size_bytes")) is not int or row["size_bytes"] < 0):
            raise RuntimeError("selected universal resource member pin is invalid")
        pins[name] = row
    catalog_pin = descriptor.get("catalog")
    if not isinstance(catalog_pin, dict) or pins.get(catalog_pin.get("path")) != catalog_pin:
        raise RuntimeError("selected universal resource catalog pin is missing or inconsistent")
    return root, pins, catalog_pin


def verify_universal_workload_archive(raw: bytes, selected: dict[str, Any]) -> dict[str, Any]:
    """Verify immutable data before a package install or cache preseed mutates state."""
    root, pins, catalog_pin = _selection_pins(selected)
    if "size_bytes" in selected and len(raw) != selected["size_bytes"]:
        raise RuntimeError("selected universal resource archive size mismatch")
    # Recheck the bytes opened for parsing, rather than trusting a mutable path.
    if hashlib.sha256(raw).hexdigest() != selected["sha256"]:
        raise RuntimeError("selected universal resource archive checksum mismatch")
    members: dict[str, Any] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            name = _relative_path(member.name)
            pin = pins.get(name)
            if not member.isfile() or name in members or pin is None or member.size != pin["size_bytes"]:
                raise RuntimeError("selected universal resource has an unexpected member")
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError("selected universal resource member is unreadable")
            data = stream.read()
            if hashlib.sha256(data).hexdigest() != pin["sha256"]:
                raise RuntimeError("selected universal resource member checksum mismatch")
            members[name] = json.loads(data)
    if set(members) != set(pins):
        raise RuntimeError("selected universal resource required member is missing")
    catalog = members[catalog_pin["path"]]
    if not isinstance(catalog, dict):
        raise RuntimeError("selected universal resource catalog is invalid")
    return {"root": root, "catalog": catalog, "members": members, "pins": pins}


def load_universal_workload_resource(config: Any, selected: dict[str, Any], *,
                                     offline_only: bool = True) -> dict[str, Any]:
    from mcd_agent.custom_scripts import _fetch_script_to_cache
    _selection_pins(selected)
    path = _fetch_script_to_cache(config, selected, offline_only=offline_only, data_only=True)
    return verify_universal_workload_archive(path.read_bytes(), selected)


def required_workload_resource(config: Any) -> dict[str, Any]:
    """Read the exact selected data; named profiles have no embedded fallback."""
    from mcd_agent.custom_scripts import CustomSelectedTaskError
    ref = getattr(config, "universal_workload_resource_ref", None)
    if not ref:
        raise CustomSelectedTaskError("custom_selected_task_artifact_missing_or_corrupt")
    return load_universal_workload_resource(config, selected_universal_resource(ref))


def preseed_universal_workload_resource(config: Any, ref: dict[str, Any], raw: bytes) -> Any:
    """Use the normal cache's atomic writer; never select or fetch another revision."""
    from mcd_agent.custom_scripts import _fetch_script_to_cache
    selected = selected_universal_resource(ref)
    verify_universal_workload_archive(raw, selected)
    return _fetch_script_to_cache(config, selected, offline_only=True, data_only=True, preseed_bytes=raw)


def preseed_runtime_workload_resources(*, runtime_dir: str | Path, cache_dir: str | Path,
                                      expected_manifest_sha256: str | None = None,
                                      verify_only: bool = False) -> dict[str, Any]:
    """Prepare verified ready-package data before configuration/profile loading.

    Package installers already verify their detached archive/manifest pins. This
    callable repeats payload/closure verification, uses installed code only, and
    does not load configuration, register an agent, apply a profile or start it.
    """
    from mcd_agent.custom_scripts import standalone_custom_cache_config
    from mcd_agent.self_update import _runtime_manifest

    runtime = Path(runtime_dir)
    manifest_path = runtime / "mcd-runtime-manifest.json"
    if expected_manifest_sha256 is not None and hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_manifest_sha256:
        raise RuntimeError("runtime resource manifest checksum mismatch")
    manifest = _runtime_manifest(runtime)
    if manifest is None:
        raise RuntimeError("runtime resource preseed requires a verified runtime manifest")
    ref = manifest.get("universal_workload_resource_ref")
    if ref is None:
        return {"status": "not_required"}
    selected = selected_universal_resource(ref)
    config = standalone_custom_cache_config(cache_dir)
    if verify_only:
        from mcd_agent.custom_scripts import _fetch_script_to_cache
        cached = _fetch_script_to_cache(config, selected, offline_only=True, data_only=True)
        verify_universal_workload_archive(cached.read_bytes(), selected)
    else:
        cached = preseed_universal_workload_resource(config, ref, (runtime / ref["file"]).read_bytes())
    return {"status": "ok", "key": ref["key"], "sha256": ref["sha256"], "cache_path": str(cached)}


def resolve_universal_workload_reference(*, runtime_settings: dict[str, Any], config: Any,
                                         runtime_dir: str | Path) -> dict[str, Any] | None:
    """Resolve explicit durable settings first; the package is initial intake only."""
    from mcd_agent.custom_scripts import CustomSelectedTaskError
    key = "universal_workload_resource_ref"
    runtime = Path(runtime_dir)
    if key in runtime_settings:
        ref = runtime_settings[key]
        if isinstance(ref, str):
            ref = json.loads(ref) if ref.strip() else None
        if ref is None:
            return None  # Literal null remains a value on the existing wire.
        if ref == {}:
            return {}  # An absent/tombstoned selection suppresses initial data.
        selected = selected_universal_resource(ref)
        try:
            load_universal_workload_resource(config, selected, offline_only=True)
        except CustomSelectedTaskError:
            embedded = runtime / ref["file"]
            if embedded.is_file() and not embedded.is_symlink() and hashlib.sha256(embedded.read_bytes()).hexdigest() == ref["sha256"]:
                preseed_universal_workload_resource(config, ref, embedded.read_bytes())
            else:
                # Same selected pins only. No manifest/latest/activation lookup.
                online = bool(config.custom_repo_base_url or config.plugins_repo_base_url or config.mcc_url)
                load_universal_workload_resource(config, selected, offline_only=not online)
        return dict(ref)
    manifest_path = runtime / "mcd-runtime-manifest.json"
    if not manifest_path.is_file():
        return {}
    from mcd_agent.self_update import _runtime_manifest
    manifest = _runtime_manifest(runtime)
    ref = manifest.get(key) if manifest is not None else None
    if ref is None:
        return {}
    selected_universal_resource(ref)
    preseed_universal_workload_resource(config, ref, (runtime / ref["file"]).read_bytes())
    return dict(ref)


def _catalog_member(resource: dict[str, Any], pin: dict[str, Any]) -> dict[str, Any]:
    name = resource["root"] + "/" + _relative_path(pin.get("path"))
    value = resource["members"].get(name)
    expected = resource["pins"].get(name)
    if (not isinstance(value, dict) or expected is None
            or any(pin.get(key) != expected[key] for key in ("sha256", "size_bytes"))):
        raise RuntimeError("selected universal catalog member is missing")
    # Catalog references must agree with the exact already verified bytes.
    return value


def workload_profile_names(resource: dict[str, Any], *, include_legacy: bool = False) -> tuple[str, ...]:
    """The catalog owns selectable identities; legacy records remain readable."""
    names = []
    for name, pin in resource["catalog"].get("profiles", {}).items():
        profile = _catalog_member(resource, pin)
        if profile.get("profile_id") != name:
            raise RuntimeError("selected universal workload profile identity is inconsistent")
        if include_legacy or not profile.get("legacy_compatibility_only", False):
            names.append(name)
    custom = resource["catalog"].get("custom", {})
    if custom.get("operator_owned") is True:
        names.append(custom["profile_id"])
    return tuple(names)


def workload_profile_values(resource: dict[str, Any], profile_id: str) -> dict[str, Any]:
    catalog = resource["catalog"]
    custom = catalog.get("custom", {})
    if profile_id == custom.get("profile_id") and custom.get("operator_owned") is True:
        return {}
    pin = catalog.get("profiles", {}).get(profile_id)
    if not isinstance(pin, dict):
        raise RuntimeError("selected universal workload profile is unknown")
    profile = _catalog_member(resource, pin)
    values = profile.get("values")
    if (profile.get("profile_id") != profile_id or not isinstance(values, dict)
            or set(values) != set(catalog.get("profile_owned_fields", []))):
        raise RuntimeError("selected universal workload profile values are inconsistent")
    # Inherited defaults describe the existing global configuration. They are
    # not profile-owned edits and must not overwrite explicit operator settings.
    return dict(values)


def workload_resume_profile(resource: dict[str, Any]) -> str:
    """Resolve the catalog's existing resume policy, never guess an active preset."""
    from mcd_agent.custom_scripts import CustomSelectedTaskError
    defaults = resource["catalog"].get("defaults")
    name = defaults.get("resume_profile") if isinstance(defaults, dict) else None
    if not isinstance(name, str):
        raise CustomSelectedTaskError("custom_selected_task_resume_profile_default_missing_or_invalid")
    custom = resource["catalog"].get("custom", {})
    if name == custom.get("profile_id") and custom.get("operator_owned") is True:
        return name
    pin = resource["catalog"].get("profiles", {}).get(name)
    if not isinstance(pin, dict):
        raise CustomSelectedTaskError("custom_selected_task_resume_profile_default_missing_or_invalid")
    profile = _catalog_member(resource, pin)
    if profile.get("legacy_compatibility_only", False):
        raise CustomSelectedTaskError("custom_selected_task_resume_profile_default_missing_or_invalid")
    workload_profile_values(resource, name)
    return name


def recommend_workload_profile(resource: dict[str, Any], *, cpu_count: int,
                               memory_kib: float, farm: bool = False) -> str:
    table = _catalog_member(resource, resource["catalog"]["hardware_recommendations"])
    inputs = {"cpu_count": max(1, int(cpu_count or 1)), "memory_kib": max(0, float(memory_kib or 0))}
    comparisons = {"min_inclusive": lambda x, y: x >= y, "max_inclusive": lambda x, y: x <= y,
                   "min_exclusive": lambda x, y: x > y, "max_exclusive": lambda x, y: x < y}
    for row in table["rows"]:
        matches = True
        for field, bounds in row["bounds"].items():
            if field not in inputs or not isinstance(bounds, dict) or not set(bounds) <= comparisons.keys():
                raise RuntimeError("selected universal hardware recommendation bound is unsupported")
            matches = matches and all(comparisons[op](inputs[field], value) for op, value in bounds.items())
        if matches:
            result = row["recommended_farm_profile" if farm else "recommended_profile"]
            if result not in resource["catalog"]["profiles"]:
                raise RuntimeError("selected universal hardware recommendation profile is missing")
            return result
    raise RuntimeError("selected universal hardware recommendation has no matching row")
