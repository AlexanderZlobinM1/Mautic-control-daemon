"""Consume the corporate MCC scenario catalog through the normal state push.

MCC owns publication and applicability.  MCD owns authenticated retrieval,
integrity verification, local activation and execution.  This module only
handles the durable runtime catalog; it never executes a scenario.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib import request
from urllib.error import HTTPError, URLError

from json import JSONDecodeError

from mcd_agent.mcc_transport import mcc_request


SYNC_SCHEMA = "mcc-mcd-scenario-sync-v1"
RUNTIME_SCHEMA = "mcc-mcd-scenario-runtime-v1"
CONTRACT_VERSION = "1.0"
RUNTIME_DIRECTORY = "scenario"
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ScenarioSyncError(ValueError):
    """A bounded, secret-free scenario synchronization failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes, *, prefixed: bool = False) -> str:
    digest = hashlib.sha256(value).hexdigest()
    return "sha256:" + digest if prefixed else digest


def _raw_sha(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if not _SHA_RE.fullmatch(raw):
        raise ScenarioSyncError("scenario_sync_sha256_invalid")
    return raw


def _safe_name(value: object, code: str) -> str:
    result = str(value or "").strip().lower()
    if not _NAME_RE.fullmatch(result):
        raise ScenarioSyncError(code)
    return result


def _safe_version(value: object) -> str:
    result = str(value or "").strip()
    if not _VERSION_RE.fullmatch(result):
        raise ScenarioSyncError("scenario_sync_version_invalid")
    return result


def _safe_file(value: object, code: str) -> str:
    result = str(value or "").strip()
    path = Path(result)
    if not _FILE_RE.fullmatch(result) or path.is_absolute() or ".." in path.parts:
        raise ScenarioSyncError(code)
    return result


def _state_dir(config: Any) -> Path:
    path = Path(str(getattr(config, "state_db_path", "") or "")).expanduser()
    if not path.is_absolute() or not path.name:
        raise ScenarioSyncError("scenario_sync_state_path_invalid")
    return path.resolve().parent


def runtime_root(config: Any) -> Path:
    install_dir = Path(str(getattr(config, "mcd_install_dir", "/opt/mcd") or "/opt/mcd")).expanduser()
    if not install_dir.is_absolute() or install_dir.is_symlink():
        raise ScenarioSyncError("scenario_sync_runtime_path_invalid")
    return install_dir / "runtime" / RUNTIME_DIRECTORY


def _runtime_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_rows = payload.get("scenarios")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ScenarioSyncError("scenario_sync_scenarios_invalid")
    seen: set[tuple[str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ScenarioSyncError("scenario_sync_scenario_invalid")
        allowed = {"id", "version", "entrypoint", "package", "dependencies", "download_url"}
        if set(raw) != allowed:
            raise ScenarioSyncError("scenario_sync_scenario_fields_invalid")
        scenario_id = _safe_name(raw.get("id"), "scenario_sync_id_invalid")
        version = _safe_version(raw.get("version"))
        key = (scenario_id, version)
        if key in seen:
            raise ScenarioSyncError("scenario_sync_duplicate_scenario")
        seen.add(key)
        entrypoint = _safe_file(raw.get("entrypoint"), "scenario_sync_entrypoint_invalid")
        package = raw.get("package")
        if not isinstance(package, Mapping) or set(package) != {"file", "sha256", "size"}:
            raise ScenarioSyncError("scenario_sync_package_invalid")
        package_file = _safe_file(package.get("file"), "scenario_sync_package_file_invalid")
        package_sha = _raw_sha(package.get("sha256"))
        size = package.get("size")
        if type(size) is not int or not 1 <= size <= MAX_PACKAGE_BYTES:
            raise ScenarioSyncError("scenario_sync_package_size_invalid")
        dependencies = raw.get("dependencies")
        if not isinstance(dependencies, list):
            raise ScenarioSyncError("scenario_sync_dependencies_invalid")
        normalized_dependencies: list[dict[str, str]] = []
        for dependency in dependencies:
            if not isinstance(dependency, Mapping) or set(dependency) != {"id", "version"}:
                raise ScenarioSyncError("scenario_sync_dependency_invalid")
            normalized_dependencies.append({
                "id": _safe_name(dependency.get("id"), "scenario_sync_dependency_id_invalid"),
                "version": _safe_version(dependency.get("version")),
            })
        url = str(raw.get("download_url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ScenarioSyncError("scenario_sync_download_url_invalid")
        rows.append({
            "id": scenario_id,
            "version": version,
            "entrypoint": entrypoint,
            "package": {"file": package_file, "sha256": package_sha, "size": size},
            "dependencies": normalized_dependencies,
            "download_url": url,
        })
    return rows


def _manifest_projection(*, family: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": RUNTIME_SCHEMA,
        "family": family,
        "runtime_directory": RUNTIME_DIRECTORY,
        "scenarios": [
            {
                "id": row["id"],
                "version": row["version"],
                "entrypoint": row["entrypoint"],
                "package": {
                    "file": row["package"]["file"],
                    "sha256": "sha256:" + _raw_sha(row["package"]["sha256"]),
                    "size": row["package"]["size"],
                },
                "dependencies": row["dependencies"],
            }
            for row in rows
        ],
    }


def _validate_sync(config: Any, value: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if value.get("schema") != SYNC_SCHEMA or value.get("contract_version") != CONTRACT_VERSION:
        raise ScenarioSyncError("scenario_sync_schema_invalid")
    if value.get("runtime_directory") != RUNTIME_DIRECTORY or value.get("replace") is not True:
        raise ScenarioSyncError("scenario_sync_runtime_invalid")
    family = _safe_name(value.get("family"), "scenario_sync_family_invalid")
    rows = _runtime_rows(value)
    expected_manifest = "sha256:" + _sha256(_canonical(_manifest_projection(family=family, rows=rows)))
    if str(value.get("manifest_sha256") or "").lower() != expected_manifest:
        raise ScenarioSyncError("scenario_sync_manifest_sha256_mismatch")
    configured = str(getattr(config, "mcc_url", "") or "").strip().rstrip("/")
    configured_url = urlsplit(configured)
    if configured_url.scheme != "https" or not configured_url.netloc:
        raise ScenarioSyncError("scenario_sync_mcc_url_invalid")
    for row in rows:
        parsed = urlsplit(row["download_url"])
        if (parsed.scheme, parsed.netloc) != (configured_url.scheme, configured_url.netloc):
            raise ScenarioSyncError("scenario_sync_download_origin_invalid")
        expected_prefix = f"/api/v1/agent/scenarios/{family}/{row['id']}/{row['version']}/artifact"
        if parsed.path != expected_prefix:
            raise ScenarioSyncError("scenario_sync_download_path_invalid")
    return family, str(value["manifest_sha256"]).lower(), rows


def _package_path(root: Path, row: Mapping[str, Any]) -> Path:
    package = row["package"]
    return root / str(row["id"]) / str(row["version"]) / str(package["file"])


def _package_matches(path: Path, row: Mapping[str, Any]) -> bool:
    try:
        package = row["package"]
        return path.is_file() and not path.is_symlink() and path.stat().st_size == int(package["size"]) and _sha256(path.read_bytes()) == package["sha256"]
    except (OSError, ValueError, TypeError, KeyError):
        return False


def read_runtime(config: Any) -> dict[str, Any]:
    """Return the verified projection sent back to MCC, or an empty object."""
    try:
        root = runtime_root(config)
        manifest_path = root / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("schema") != RUNTIME_SCHEMA or value.get("runtime_directory") != RUNTIME_DIRECTORY:
            return {}
        family = _safe_name(value.get("family"), "scenario_sync_family_invalid")
        rows = _runtime_rows({
            "schema": SYNC_SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "family": family,
            "runtime_directory": RUNTIME_DIRECTORY,
            "replace": True,
            "manifest_sha256": value.get("manifest_sha256"),
            "scenarios": [dict(row, download_url="https://invalid.invalid") for row in value.get("scenarios", [])],
        })
        manifest = _manifest_projection(family=family, rows=rows)
        expected = "sha256:" + _sha256(_canonical(manifest))
        if str(value.get("manifest_sha256") or "").lower() != expected:
            return {}
        for row in rows:
            if not _package_matches(_package_path(root, row), row):
                return {}
        return {**manifest, "manifest_sha256": expected}
    except (OSError, ValueError, TypeError, KeyError, JSONDecodeError, ScenarioSyncError):
        return {}


def local_scenario(config: Any, scenario_id: str) -> dict[str, Any] | None:
    runtime = read_runtime(config)
    for row in runtime.get("scenarios", []):
        if row.get("id") != scenario_id:
            continue
        root = runtime_root(config)
        path = _package_path(root, row)
        package = row["package"]
        media_type = "application/x-sh" if str(package["file"]).lower().endswith((".sh", ".bash")) else "application/x-elf"
        digest = "sha256:" + _raw_sha(package["sha256"])
        revision = str(row["version"])
        return {
            "id": str(row["id"]),
            "version": revision,
            "entrypoint": str(row["entrypoint"]),
            "path": str(path),
            "pin": {
                "artifact_path": str(path),
                "artifact_grant": "mcc-state-sync",
                "artifact_digest": digest,
                "artifact_size_bytes": int(package["size"]),
                "artifact_media_type": media_type,
                "artifact_revision": revision,
                "artifact_trust_mode": "mcc_catalog_sha256",
                "artifact_transport": "mcc_authenticated_api",
                "trusted_catalog": {
                    "source": "mcc",
                    "transport": "mcc_authenticated_api",
                    "status": "active",
                    "revision": revision,
                    "artifact_digest": digest,
                },
            },
        }
    return None


def _download(config: Any, row: Mapping[str, Any], destination: Path) -> None:
    url = str(row["download_url"])
    req = mcc_request(url, headers={"Accept": "application/octet-stream", "Authorization": f"Bearer {getattr(config, 'mcc_token', '')}"})
    try:
        with request.urlopen(req, timeout=60) as response:
            content = response.read(MAX_PACKAGE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise ScenarioSyncError("scenario_sync_download_failed") from exc
    package = row["package"]
    if len(content) != int(package["size"]) or _sha256(content) != package["sha256"]:
        raise ScenarioSyncError("scenario_sync_package_integrity_mismatch")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + ".tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_receipt(config: Any, receipt: Mapping[str, Any]) -> None:
    directory = _state_dir(config) / "scenario-sync"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "last-receipt.json"
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(_canonical(dict(receipt)).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def apply_sync(config: Any, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, fetch and atomically activate one MCC runtime catalog."""
    try:
        family, manifest_sha, rows = _validate_sync(config, value)
        current = read_runtime(config)
        if current.get("manifest_sha256") == manifest_sha:
            receipt = {"schema": "mcd-scenario-sync-receipt-v1", "status": "already_current", "manifest_sha256": manifest_sha, "family": family}
            _write_receipt(config, receipt)
            return receipt
        root = runtime_root(config)
        parent = root.parent
        parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".scenario-sync-", dir=str(parent)))
        cached: list[str] = []
        downloaded: list[str] = []
        try:
            current_root = root
            for row in rows:
                destination = _package_path(stage, row)
                current_path = _package_path(current_root, row)
                if _package_matches(current_path, row):
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.copyfile(current_path, destination)
                    cached.append(f"{row['id']}@{row['version']}")
                else:
                    _download(config, row, destination)
                    downloaded.append(f"{row['id']}@{row['version']}")
            manifest = {**_manifest_projection(family=family, rows=rows), "manifest_sha256": manifest_sha}
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(_canonical(manifest) + b"\n")
            os.chmod(manifest_path, 0o600)
            old = parent / (".scenario-old-" + secrets.token_hex(8))
            if root.exists() or root.is_symlink():
                os.replace(root, old)
            os.replace(stage, root)
            if old.exists():
                shutil.rmtree(old)
            receipt = {"schema": "mcd-scenario-sync-receipt-v1", "status": "activated", "manifest_sha256": manifest_sha, "family": family, "cached": cached, "downloaded": downloaded}
            _write_receipt(config, receipt)
            return receipt
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    except ScenarioSyncError as exc:
        receipt = {"schema": "mcd-scenario-sync-receipt-v1", "status": "failed", "reason": exc.code}
        try:
            _write_receipt(config, receipt)
        except Exception:
            pass
        return receipt
    except (OSError, ValueError, TypeError, KeyError) as exc:
        receipt = {"schema": "mcd-scenario-sync-receipt-v1", "status": "failed", "reason": "scenario_sync_activation_failed"}
        try:
            _write_receipt(config, receipt)
        except Exception:
            pass
        return receipt
