"""Read explicitly selected immutable workload data through the existing cache.

No catalog discovery, activation policy, settings clock, or executable runner is
owned here. The caller supplies its selected resource and chooses when to apply
its literal values through the existing configuration/settings machinery.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import PurePosixPath
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


def load_universal_workload_resource(config: Any, selected: dict[str, Any], *,
                                     offline_only: bool = True) -> dict[str, Any]:
    """Verify the exact selected data closure without extracting or executing it."""
    from mcd_agent.custom_scripts import _fetch_script_to_cache

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
    archive_path = _fetch_script_to_cache(config, selected, offline_only=offline_only, data_only=True)
    # Recheck the bytes opened for parsing, rather than trusting a mutable path.
    raw = archive_path.read_bytes()
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


def _catalog_member(resource: dict[str, Any], pin: dict[str, Any]) -> dict[str, Any]:
    name = resource["root"] + "/" + _relative_path(pin.get("path"))
    value = resource["members"].get(name)
    expected = resource["pins"].get(name)
    if (not isinstance(value, dict) or expected is None
            or any(pin.get(key) != expected[key] for key in ("sha256", "size_bytes"))):
        raise RuntimeError("selected universal catalog member is missing")
    # Catalog references must agree with the exact already verified bytes.
    return value


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
