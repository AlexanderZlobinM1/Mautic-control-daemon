"""Generic prepared Mautic target input and safe archive materialization.

The MCC builds the target skeleton.  MCD only authenticates the existing MCC
API transport, verifies the exact artifact and manifest, and materializes the
payload for the existing upgrade lifecycle.  This module deliberately has no
Composer invocation, release selection, signature scheme or network fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping


SCHEMA = "mcd-mautic-prepared-target-input-v1"
MANIFEST_SCHEMA = "mcd-mautic-prepared-target-manifest-v1"
MANIFEST_NAME = ".mcd/prepared-target-manifest.json"
# The input envelope carries the complete MCC file manifest.  Real Mautic 6
# targets are several megabytes even before extraction; keep the envelope
# bounded, but do not reject a valid manifest merely because the application
# contains tens of thousands of verified files.
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PRESERVED_PATHS = ("config", "app/config", "var")
_INPUT_KEYS = {"schema", "prepared_target_id", "target_version", "install_type", "artifact", "manifest"}
_ARTIFACT_KEYS = {"path", "grant", "size_bytes", "sha256", "media_type"}
_MANIFEST_KEYS = {"schema", "target_version", "install_type", "application_root_relative", "preserve_paths", "files"}
_COMPOSER_ID_RE = re.compile(r"^mautic-(?P<target>[0-9]+\.[0-9]+\.[0-9]+)-php(?P<php>[0-9]+\.[0-9]+)-composer$")
_PHP_MINOR_RE = re.compile(r"^[0-9]+\.[0-9]+$")


class PreparedTargetError(ValueError):
    """Bounded, non-secret prepared-target rejection."""


def prepared_target_php(prepared_target_id: str) -> str | None:
    """Return the PHP major.minor bound to a Composer target identity."""
    match = _COMPOSER_ID_RE.fullmatch(str(prepared_target_id or ""))
    return match.group("php") if match else None


def validate_prepared_target_runtime(value: Mapping[str, Any], runtime_php: str) -> dict[str, Any]:
    """Require a Composer skeleton built for the exact runtime PHP line.

    Cross-PHP Composer cells are intentionally unsupported. ZIP targets have
    one official upstream artifact and therefore do not carry a PHP binding.
    """
    prepared = validate_prepared_target(value)
    if prepared["install_type"] != "composer":
        return prepared
    expected = prepared_target_php(prepared["prepared_target_id"])
    actual = str(runtime_php or "").strip()
    if not expected or not _PHP_MINOR_RE.fullmatch(actual):
        raise PreparedTargetError("prepared_target_runtime_php_unavailable")
    if actual != expected:
        raise PreparedTargetError("prepared_target_runtime_php_mismatch")
    return prepared


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _string(value: object, code: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise PreparedTargetError(code)
    return value


def _safe_relative(value: object, code: str) -> str:
    text = _string(value, code, maximum=4096).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PreparedTargetError(code)
    return path.as_posix()


def _exact(value: object, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PreparedTargetError(code)
    return dict(value)


def validate_prepared_target(value: object) -> dict[str, Any]:
    item = _exact(value, _INPUT_KEYS, "prepared_target_fields_invalid")
    if item["schema"] != SCHEMA:
        raise PreparedTargetError("prepared_target_schema_invalid")
    prepared_id = _string(item["prepared_target_id"], "prepared_target_id_invalid", maximum=128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", prepared_id):
        raise PreparedTargetError("prepared_target_id_invalid")
    target = _string(item["target_version"], "prepared_target_version_invalid", maximum=32)
    if not VERSION_RE.fullmatch(target):
        raise PreparedTargetError("prepared_target_version_invalid")
    install_type = item["install_type"]
    if install_type not in {"composer", "zip"}:
        raise PreparedTargetError("prepared_target_install_type_invalid")
    if install_type == "zip":
        canonical_id = f"mautic-{target}-zip"
        if prepared_id != canonical_id:
            raise PreparedTargetError("prepared_target_id_invalid")
    else:
        composer_id = _COMPOSER_ID_RE.fullmatch(prepared_id)
        if not composer_id or composer_id.group("target") != target:
            raise PreparedTargetError("prepared_target_id_invalid")

    artifact = _exact(item["artifact"], _ARTIFACT_KEYS, "prepared_target_artifact_fields_invalid")
    path = _string(artifact["path"], "prepared_target_artifact_path_invalid", maximum=2048)
    if not path.startswith("/") or "://" in path or "?" in path or "#" in path:
        raise PreparedTargetError("prepared_target_artifact_path_invalid")
    if any(part in {"", ".", ".."} for part in PurePosixPath(path).parts):
        raise PreparedTargetError("prepared_target_artifact_path_invalid")
    grant = _string(artifact["grant"], "prepared_target_artifact_grant_invalid", maximum=512)
    size = artifact["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise PreparedTargetError("prepared_target_artifact_size_invalid")
    sha = _string(artifact["sha256"], "prepared_target_artifact_sha256_invalid", maximum=71).lower()
    if not SHA256_RE.fullmatch(sha):
        raise PreparedTargetError("prepared_target_artifact_sha256_invalid")
    media_type = _string(artifact["media_type"], "prepared_target_artifact_media_type_invalid", maximum=128)
    if media_type != "application/zip":
        raise PreparedTargetError("prepared_target_artifact_media_type_invalid")

    manifest = _exact(item["manifest"], _MANIFEST_KEYS, "prepared_target_manifest_fields_invalid")
    if manifest["schema"] != MANIFEST_SCHEMA or manifest["target_version"] != target or manifest["install_type"] != install_type:
        raise PreparedTargetError("prepared_target_manifest_binding_invalid")
    root_relative = manifest["application_root_relative"]
    if root_relative not in {".", "docroot", "public"}:
        raise PreparedTargetError("prepared_target_manifest_root_invalid")
    preserve = manifest["preserve_paths"]
    if preserve != list(PRESERVED_PATHS):
        raise PreparedTargetError("prepared_target_manifest_preserve_paths_invalid")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > 200_000:
        raise PreparedTargetError("prepared_target_manifest_files_invalid")
    seen: set[str] = set()
    normalized_files: list[dict[str, str]] = []
    for row in files:
        row = _exact(row, {"path", "sha256"}, "prepared_target_manifest_file_invalid")
        relative = _safe_relative(row["path"], "prepared_target_manifest_file_path_invalid")
        if relative == MANIFEST_NAME or relative in seen or any(relative == prefix or relative.startswith(prefix + "/") for prefix in PRESERVED_PATHS):
            raise PreparedTargetError("prepared_target_manifest_file_path_invalid")
        digest = _string(row["sha256"], "prepared_target_manifest_file_sha256_invalid", maximum=64).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PreparedTargetError("prepared_target_manifest_file_sha256_invalid")
        seen.add(relative)
        normalized_files.append({"path": relative, "sha256": digest})
    if [row["path"] for row in normalized_files] != sorted(seen):
        raise PreparedTargetError("prepared_target_manifest_file_order_invalid")
    normalized = dict(item)
    normalized["prepared_target_id"] = prepared_id
    normalized["target_version"] = target
    normalized["artifact"] = {**artifact, "path": path, "grant": grant, "sha256": sha, "media_type": media_type}
    normalized["manifest"] = {**manifest, "preserve_paths": list(PRESERVED_PATHS), "files": normalized_files}
    return normalized


def load_prepared_target_file(path: str, expected_sha256: str = "") -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_INPUT_BYTES:
        raise PreparedTargetError("prepared_target_input_file_invalid")
    raw = target.read_bytes()
    if expected_sha256:
        expected = str(expected_sha256).lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected:
            raise PreparedTargetError("prepared_target_input_sha256_mismatch")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTargetError("prepared_target_input_json_invalid") from exc
    return validate_prepared_target(value)


def load_prepared_target_json(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise PreparedTargetError("prepared_target_input_size_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTargetError("prepared_target_input_json_invalid") from exc
    return validate_prepared_target(value)


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise PreparedTargetError("prepared_target_input_duplicate_key")
        value[key] = child
    return value


def _reject_constant(_value: str) -> None:
    raise PreparedTargetError("prepared_target_input_nonfinite_number")


def download_prepared_target(config: Any, prepared: Mapping[str, Any], *, opener: Callable[..., Any] | None = None) -> bytes:
    normalized = validate_prepared_target(prepared)
    base = str(getattr(config, "mcc_url", "") or "").rstrip("/")
    token = str(getattr(config, "mcc_token", "") or "").strip()
    if not base.startswith("https://") or not token:
        raise PreparedTargetError("prepared_target_mcc_transport_required")
    artifact = normalized["artifact"]
    request = urllib.request.Request(
        base + artifact["path"],
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/zip",
            "X-MCD-Prepared-Target": normalized["prepared_target_id"],
            "X-MCC-Artifact-Grant": artifact["grant"],
        },
        method="GET",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=120) as response:
            body = response.read(artifact["size_bytes"] + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreparedTargetError("prepared_target_artifact_download_failed") from exc
    if len(body) != artifact["size_bytes"] or _digest(body) != artifact["sha256"]:
        raise PreparedTargetError("prepared_target_artifact_integrity_invalid")
    return body


def _archive_member_path(name: str) -> str:
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        raise PreparedTargetError("prepared_target_archive_path_invalid")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PreparedTargetError("prepared_target_archive_path_invalid")
    return path.as_posix()


@dataclass(slots=True)
class PreparedTarget:
    input: dict[str, Any]
    directory: Path
    root: Path
    artifact_sha256: str

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def materialize_prepared_target(prepared: Mapping[str, Any], artifact: bytes, *, read_version: Callable[[Path], str | None]) -> PreparedTarget:
    normalized = validate_prepared_target(prepared)
    if len(artifact) != normalized["artifact"]["size_bytes"] or _digest(artifact) != normalized["artifact"]["sha256"]:
        raise PreparedTargetError("prepared_target_artifact_integrity_invalid")
    directory = Path(tempfile.mkdtemp(prefix="mcd-prepared-target-"))
    try:
        archive_path = directory / "target.zip"
        archive_path.write_bytes(artifact)
        extraction = directory / "source"
        extraction.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            infos = [(info, _archive_member_path(info.filename.rstrip("/"))) for info in archive.infolist() if info.filename.rstrip("/")]
            names = [name for _info, name in infos]
            if len(names) != len(set(names)) or MANIFEST_NAME not in names:
                raise PreparedTargetError("prepared_target_archive_manifest_missing")
            for info, name in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK or mode == stat.S_IFCHR or mode == stat.S_IFBLK or mode == stat.S_IFIFO:
                    raise PreparedTargetError("prepared_target_archive_special_file")
                destination = extraction / name
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
        manifest_path = extraction / MANIFEST_NAME
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise PreparedTargetError("prepared_target_archive_manifest_too_large")
        try:
            embedded = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_pairs,
                parse_constant=_reject_constant,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreparedTargetError("prepared_target_archive_manifest_invalid") from exc
        if embedded != normalized["manifest"]:
            raise PreparedTargetError("prepared_target_archive_manifest_mismatch")
        expected = {row["path"]: row["sha256"] for row in normalized["manifest"]["files"]}
        actual = {}
        for path in sorted(expected):
            candidate = extraction / path
            if candidate.is_symlink() or not candidate.is_file():
                raise PreparedTargetError("prepared_target_archive_file_missing")
            actual[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise PreparedTargetError("prepared_target_archive_file_digest_mismatch")
        archive_payload = {name for info, name in infos if not info.is_dir()} - {MANIFEST_NAME}
        if archive_payload != set(expected):
            raise PreparedTargetError("prepared_target_archive_file_set_mismatch")
        root = extraction / normalized["manifest"]["application_root_relative"] if normalized["manifest"]["application_root_relative"] != "." else extraction
        if not root.is_dir() or not (root / "app").is_dir() or not (root / "plugins").is_dir() or not (root / "bin/console").is_file():
            raise PreparedTargetError("prepared_target_application_layout_invalid")
        if read_version(root) != normalized["target_version"]:
            raise PreparedTargetError("prepared_target_version_mismatch")
        manifest_path.unlink()
        try:
            manifest_path.parent.rmdir()
        except OSError:
            pass
        return PreparedTarget(normalized, directory, root, normalized["artifact"]["sha256"])
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def merge_into_live(prepared_root: Path, live_root: Path) -> None:
    """Replace prepared files while preserving the live config and var trees."""
    if not prepared_root.is_dir() or not live_root.is_dir() or prepared_root == live_root:
        raise PreparedTargetError("prepared_target_live_root_invalid")
    for source in sorted(prepared_root.rglob("*")):
        relative = source.relative_to(prepared_root)
        if any(relative == Path(prefix) or prefix in relative.parents for prefix in PRESERVED_PATHS):
            continue
        destination = live_root / relative
        if source.is_symlink() or destination.is_symlink():
            raise PreparedTargetError("prepared_target_symlink_rejected")
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                if destination.is_symlink():
                    raise PreparedTargetError("prepared_target_symlink_rejected")
                destination.unlink()
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".mcd-prepared-tmp")
        if temp.exists() or temp.is_symlink():
            temp.unlink()
        shutil.copy2(source, temp)
        if destination.is_dir():
            shutil.rmtree(destination)
        os.replace(temp, destination)
