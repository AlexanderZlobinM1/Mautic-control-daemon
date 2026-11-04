#!/usr/bin/env python3
"""Install a pinned runtime-only package, including the legacy first bridge.

The normal 1.5.127 updater expects ``pyproject.toml`` in the downloaded
archive.  A runtime-only release intentionally does not contain that checkout
file, so the legacy updater cannot perform the first transition by itself.
This small, separately transported component installer performs that one
transition without editing MCC, changing update policy, or touching Mautic
configuration/data.

The bridge is deliberately fail-closed:

* it accepts a locally transported archive and verifies its detached manifest;
* it takes the same durable MCD update lease and scheduler admission barrier;
* an unreadable/active backup lock aborts before mutation;
* the wheel is installed and imported from the managed venv before restart;
* verified artifacts are activated under ``/opt/mcd/runtime``;
* legacy project sources are retained only during the activation transaction
  and retired after verified activation, never used as installed identity;
* the supervisor uses ``WorkingDirectory=/opt/mcd`` and ``KillMode=process``;
* failure restores the old source, managed-venv package and running service,
  even when the legacy source has no ``pyproject.toml``.

It is not part of the runtime archive.  The source tree is the transport
input for the bridge only; the active supervisor imports the installed wheel.

An already runtime-only DEV host may also receive an explicitly authorized
same-version rebuild.  That path is disabled by default and requires exact
target, archive and manifest pins; the incoming manifest must differ from the
active manifest.  It exists for immutable unlisted DEV candidates and never
turns a mutable version label into an implicit update channel.
"""

from __future__ import annotations

import argparse
import ast
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - the bridge is POSIX-only
    fcntl = None  # type: ignore[assignment]


MANIFEST_NAME = "mcd-runtime-manifest.json"
MANIFEST_SCHEMA = "mcd-runtime-package-v1"
REQUIRED_RUNTIME_FILES = {
    "mcd_agent/__init__.py",
    "mcd_agent/daemon.py",
    "mcd_agent/state_push.py",
    "mcd_agent/mcd_control.py",
}
RECEIPT_SCHEMA = "mcd-runtime-bootstrap-receipt-v1"
STATE_NAME = "mcd-self-update.json"
SERVICE_DROPIN_NAME = "99-mcd-runtime-package.conf"
FORBIDDEN_TOP_LEVEL = {
    ".git",
    ".pytest_cache",
    "ALGORITHM.md",
    "CHANGELOG.md",
    "README.md",
    "TECHNICAL_DEBT.md",
    "TEST_ALGORITHM.md",
    "docs",
    "tests",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
}


class BridgeError(RuntimeError):
    """A fail-closed bridge validation or mutation error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_key(value: str) -> tuple[int, int, int]:
    numbers = [part for part in re.sub(r"[^0-9.]", ".", value).split(".") if part.isdigit()]
    while len(numbers) < 3:
        numbers.append("0")
    return tuple(int(part) for part in numbers[:3])  # type: ignore[return-value]


def _safe_relative(root: Path, raw_name: str) -> Path:
    name = str(raw_name or "").strip()
    if not name or Path(name).is_absolute():
        raise BridgeError(f"unsafe archive/manifest path: {raw_name!r}")
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise BridgeError(f"archive/manifest path escapes root: {raw_name!r}") from exc
    return candidate


# The transported installer is self-contained: keep these pure data checks
# equivalent to the installed universal_workload consumer, with no source imports.
def _universal_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError("universal resource path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError("universal resource path is invalid")
    return value


def _universal_selected_resource(ref: dict[str, Any]) -> dict[str, Any]:
    """Adapt the existing host runtime-map reference to the shared data cache."""
    fields = {"schema", "key", "file", "sha256", "size_bytes", "resource_root", "catalog", "files"}
    if (not isinstance(ref, dict) or set(ref) != fields
            or ref["schema"] != "mcc-universal-workload-resource-ref-v1"
            or type(ref["size_bytes"]) is not int or ref["size_bytes"] < 0):
        raise RuntimeError("selected universal workload resource reference is invalid")
    _universal_relative_path(ref["file"])
    selected = {key: ref[key] for key in ("key", "file", "sha256", "size_bytes")}
    selected.update(kind="data_only_no_entrypoint", entrypoint=None,
                    resource={key: ref[key] for key in ("resource_root", "catalog", "files")})
    _universal_selection_pins(selected)
    return selected


def _universal_selection_pins(selected: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if (selected.get("kind") != "data_only_no_entrypoint"
            or selected.get("entrypoint") is not None
            or selected.get("key") in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(selected.get("key", "")))):
        raise RuntimeError("selected universal resource must be data-only with an exact key")
    descriptor = selected.get("resource")
    if not isinstance(descriptor, dict):
        raise RuntimeError("selected universal resource descriptor is missing")
    root = _universal_relative_path(descriptor.get("resource_root"))
    rows = descriptor.get("files")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("selected universal resource member pins are missing")
    pins: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("selected universal resource member pin is invalid")
        name = _universal_relative_path(row.get("path"))
        if (not name.startswith(root + "/") or name in pins
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))
                or type(row.get("size_bytes")) is not int or row["size_bytes"] < 0):
            raise RuntimeError("selected universal resource member pin is invalid")
        pins[name] = row
    catalog_pin = descriptor.get("catalog")
    if not isinstance(catalog_pin, dict) or pins.get(catalog_pin.get("path")) != catalog_pin:
        raise RuntimeError("selected universal resource catalog pin is missing or inconsistent")
    return root, pins, catalog_pin


def _universal_verify_archive(raw: bytes, selected: dict[str, Any]) -> dict[str, Any]:
    """Verify immutable data before a package install or cache preseed mutates state."""
    root, pins, catalog_pin = _universal_selection_pins(selected)
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
            name = _universal_relative_path(member.name)
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



def _runtime_dependency_wheelhouse(stage: Path, manifest: dict[str, Any]) -> Path | None:
    """Admit an optional complete flat wheelhouse through the existing payload pins."""
    declared = manifest.get("dependency_wheelhouse")
    root = stage / "wheelhouse"
    payload = manifest.get("payload_sha256", {})
    if declared is None:
        if root.exists() or root.is_symlink() or any(str(name).startswith("wheelhouse/") for name in payload):
            raise RuntimeError("runtime dependency wheelhouse is undeclared")
        return None
    if declared != "wheelhouse" or root.is_symlink() or not root.is_dir() or not isinstance(payload, dict):
        raise RuntimeError("runtime dependency wheelhouse is invalid")
    entries = sorted(root.iterdir())
    if not entries:
        raise RuntimeError("runtime dependency wheelhouse is empty")
    names = set()
    for path in entries:
        if (path.is_symlink() or not path.is_file() or path.suffix != ".whl"
                or re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", path.name) is None):
            raise RuntimeError("runtime dependency wheelhouse contains a non-wheel")
        name = "wheelhouse/" + path.name
        expected = payload.get(name)
        if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("runtime dependency wheelhouse checksum mismatch")
        names.add(name)
    listed = {name for name in payload if str(name).startswith("wheelhouse/")}
    if listed != names:
        raise RuntimeError("runtime dependency wheelhouse closure mismatch")
    return root


def _validate_manifest(stage: Path) -> dict[str, Any]:
    manifest_path = stage / MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BridgeError(f"runtime manifest unreadable: {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or str(raw.get("schema") or "") != MANIFEST_SCHEMA:
        raise BridgeError(f"unsupported runtime manifest schema: {manifest_path}")
    version = str(raw.get("version") or "").strip()
    wheel_name = str(raw.get("wheel") or "").strip()
    requirements_name = str(raw.get("requirements") or "").strip()
    payload = raw.get("payload_sha256")
    if not version or not wheel_name or not requirements_name or not isinstance(payload, dict):
        raise BridgeError("runtime manifest is incomplete")

    listed: set[str] = set()
    for raw_name, expected in payload.items():
        name = str(raw_name or "").strip()
        expected_sha = str(expected or "").strip().lower()
        file_path = _safe_relative(stage, name)
        if not file_path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise BridgeError(f"runtime manifest payload is invalid: {name}")
        actual = _sha256(file_path).lower()
        if actual != expected_sha:
            raise BridgeError(f"runtime manifest payload checksum mismatch: {name}")
        listed.add(name)

    actual_files = {
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*")
        if path.is_file()
        and path.name != MANIFEST_NAME
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    unlisted = sorted(actual_files - listed)
    if unlisted:
        raise BridgeError(f"runtime manifest has unlisted payload files: {unlisted[:8]}")
    missing_required = sorted(REQUIRED_RUNTIME_FILES - listed)
    if missing_required:
        raise BridgeError(f"runtime manifest is missing required import files: {missing_required}")
    for forbidden in FORBIDDEN_TOP_LEVEL:
        if (stage / forbidden).exists():
            raise BridgeError(f"runtime-only payload contains forbidden material: {forbidden}")

    _runtime_dependency_wheelhouse(stage, raw)
    wheel_path = _safe_relative(stage, wheel_name)
    requirements_path = _safe_relative(stage, requirements_name)
    if not wheel_path.is_file() or not requirements_path.is_file():
        raise BridgeError("runtime manifest points to missing wheel or requirements")
    if not (stage / "mcd_agent" / "__init__.py").is_file():
        raise BridgeError("runtime-only payload is missing mcd_agent package")
    ref = raw.get("universal_workload_resource_ref")
    if ref is not None:
        selected = _universal_selected_resource(ref)
        if payload.get(ref["file"]) != ref["sha256"]:
            raise BridgeError("runtime manifest does not bind the selected universal resource payload")
        _universal_verify_archive(_safe_relative(stage, ref["file"]).read_bytes(), selected)
    return raw


def _extract_archive(archive: Path, destination: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mcd-runtime-bridge-") as temp:
        extracted = Path(temp) / "payload"
        extracted.mkdir()
        try:
            with tarfile.open(archive, "r:*") as handle:
                base = extracted.resolve()
                for member in handle.getmembers():
                    target = (extracted / member.name).resolve()
                    if target != base and base not in target.parents:
                        raise BridgeError(f"archive path escapes staging root: {member.name}")
                    if member.issym() or member.islnk() or member.isdev():
                        raise BridgeError(f"runtime archive contains unsupported link/device: {member.name}")
                handle.extractall(extracted)
        except (tarfile.TarError, OSError) as exc:
            raise BridgeError(f"runtime archive extraction failed: {exc}") from exc

        entries = [entry for entry in extracted.iterdir() if entry.name != ".DS_Store"]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted
        if not any(root.iterdir()):
            raise BridgeError("runtime archive is empty")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(root, destination, symlinks=False)
    return _validate_manifest(destination)


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int = 0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "env": env or _isolated_env(),
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if timeout_sec > 0:
        kwargs["timeout"] = timeout_sec
    try:
        proc = subprocess.run(command, **kwargs)
    except OSError as exc:
        raise BridgeError(f"command unavailable: {' '.join(command)}: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        raise BridgeError(f"command failed ({proc.returncode}): {' '.join(command)}: {detail[-4000:]}")
    return proc


def _venv_python(install_dir: Path) -> Path:
    path = install_dir / "venv" / "bin" / "python"
    if not path.is_file():
        raise BridgeError(f"managed venv python not found: {path}")
    return path


def _site_dir(python: Path) -> Path:
    probe = _run(
        [
            str(python),
            "-c",
            "import site, sysconfig; print(site.getsitepackages()[0] if site.getsitepackages() else sysconfig.get_paths()['purelib'])",
        ]
    )
    path = Path((probe.stdout or "").strip())
    if not path.is_dir():
        raise BridgeError(f"managed venv site-packages not found: {path}")
    return path


def _remove_agent_shadow_hooks(site_dir: Path, source_dir: Path) -> list[str]:
    roots = {str(source_dir.resolve()), str((source_dir / "mcd_agent").resolve())}
    removed: list[str] = []
    for candidate in sorted(site_dir.iterdir()):
        lower = candidate.name.lower()
        remove = lower in {"mcd_agent.egg-link", "mcd-agent.egg-link"}
        remove = remove or (lower.startswith("__editable__") and "mcd_agent" in lower)
        if candidate.suffix == ".pth" and candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
            except OSError:
                content = ""
            if any(root in content for root in roots):
                kept = [line for line in content.splitlines(keepends=True) if line.strip() not in roots]
                if kept:
                    candidate.write_text("".join(kept), encoding="utf-8")
                    removed.append(candidate.name)
                    continue
                remove = True
        if not remove:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink(missing_ok=True)
        removed.append(candidate.name)
    return removed


def _remove_agent_distribution_metadata(site_dir: Path, *, preserve_records: bool = False,
                                        keep: str = "") -> list[str]:
    """Keep pip's ownership RECORD until uninstall; clear stale metadata after."""
    removed: list[str] = []
    for candidate in sorted(site_dir.glob("mcd_agent-*.dist-info")):
        if not candidate.is_dir() or candidate.name == keep:
            continue
        if preserve_records and (candidate / "RECORD").is_file():
            continue
        shutil.rmtree(candidate)
        removed.append(candidate.name)
    return removed


def _wheel_distribution_metadata_name(wheel_path: Path) -> str:
    with zipfile.ZipFile(wheel_path) as wheel:
        names = {name.split("/", 1)[0] for name in wheel.namelist()
                 if name.startswith("mcd_agent-") and name.endswith(".dist-info/RECORD")}
    if len(names) != 1:
        raise RuntimeError("runtime wheel must contain exactly one MCD ownership RECORD")
    return names.pop()


def _is_agent_runtime_entry(path: Path) -> bool:
    """Return whether a managed-venv entry belongs to the MCD package."""
    name = path.name.lower()
    if name == "mcd_agent":
        return True
    if name in {"mcd_agent.egg-link", "mcd-agent.egg-link"}:
        return True
    if name.startswith("__editable__") and "mcd_agent" in name:
        return True
    if (name.startswith("mcd_agent-") or name.startswith("mcd-agent-")) and name.endswith(
        (".dist-info", ".egg-info")
    ):
        return True
    if path.suffix == ".pth" and path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return "mcd_agent" in content or "mcd-agent" in content
    return False


def _copy_runtime_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _remove_runtime_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _snapshot_legacy_runtime_package(install_dir: Path) -> Path:
    """Snapshot old MCD venv entries before installing the successor.

    Old source-tree installations can be valid with no ``pyproject.toml`` in
    ``src`` (the supervisor may import that tree through its service
    environment). A pip reinstall cannot reconstruct such a package during
    rollback, so retain the exact managed-venv entries instead.
    """
    site_dir = _site_dir(_venv_python(install_dir))
    snapshot = install_dir / "var" / "updates" / f"legacy-runtime-{os.getpid()}-{time.time_ns()}"
    snapshot.mkdir(parents=True, exist_ok=False)
    (snapshot / "site-dir").write_text(str(site_dir.resolve()) + "\n", encoding="utf-8")
    for entry in sorted(site_dir.iterdir(), key=lambda item: item.name):
        if _is_agent_runtime_entry(entry):
            _copy_runtime_entry(entry, snapshot / entry.name)
    return snapshot


def _restore_legacy_runtime_package(install_dir: Path, snapshot: Path) -> None:
    """Restore pre-bridge MCD venv entries without source packaging."""
    site_dir_file = snapshot / "site-dir"
    site_dir = (
        Path(site_dir_file.read_text(encoding="utf-8").strip())
        if site_dir_file.is_file()
        else _site_dir(_venv_python(install_dir))
    )
    if not site_dir.is_dir():
        raise BridgeError(f"managed venv site-packages missing during rollback: {site_dir}")
    for entry in sorted(site_dir.iterdir(), key=lambda item: item.name):
        if _is_agent_runtime_entry(entry):
            _remove_runtime_entry(entry)
    for entry in sorted(snapshot.iterdir(), key=lambda item: item.name):
        if entry.name != "site-dir":
            _copy_runtime_entry(entry, site_dir / entry.name)


def _install_runtime(install_dir: Path, stage: Path, manifest: dict[str, Any], *,
                     custom_cache_dir: Path | None = None) -> dict[str, str]:
    if manifest.get("universal_workload_resource_ref") is not None or manifest.get("dependency_wheelhouse") is not None:
        _validate_manifest(stage)
    if manifest.get("universal_workload_resource_ref") is not None:
        if custom_cache_dir is None:
            raise BridgeError("selected universal resource requires explicit custom cache directory")
    python = _venv_python(install_dir)
    requirements = stage / str(manifest["requirements"])
    wheel = stage / str(manifest["wheel"])
    metadata_name = _wheel_distribution_metadata_name(wheel)
    wheelhouse = _runtime_dependency_wheelhouse(stage, manifest)
    dependency_options = (["--no-index", "--find-links", str(wheelhouse), "--only-binary=:all:"]
                          if wheelhouse is not None else [])
    _run([str(python), "-m", "pip", "--version"])
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            *dependency_options,
            "-r",
            str(requirements),
        ],
        cwd=Path("/"),
    )
    site_dir = _site_dir(python)
    removed_metadata = _remove_agent_distribution_metadata(site_dir, preserve_records=True)
    removed_hooks = _remove_agent_shadow_hooks(site_dir, install_dir / "src")
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ],
        cwd=Path("/"),
    )
    removed_metadata.extend(_remove_agent_distribution_metadata(site_dir, keep=metadata_name))
    version_probe = _run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m, mcd_agent; print(m.version('mcd-agent')); print(mcd_agent.__version__); print(mcd_agent.__file__)",
        ],
        cwd=install_dir,
    )
    lines = [line.strip() for line in (version_probe.stdout or "").splitlines() if line.strip()]
    if len(lines) < 3:
        raise BridgeError("installed runtime verification returned incomplete output")
    package_version, imported_version, imported_path = lines[-3:]
    expected = str(manifest.get("version") or "").strip()
    expected_path = (site_dir / "mcd_agent" / "__init__.py").resolve()
    if package_version != expected or imported_version != expected:
        raise BridgeError(
            f"installed runtime version mismatch expected={expected} metadata={package_version} imported={imported_version}"
        )
    if Path(imported_path).resolve() != expected_path:
        raise BridgeError(f"installed runtime path mismatch expected={expected_path} imported={imported_path}")
    resource_cache: dict[str, Any] = {"status": "not_required"}
    if manifest.get("universal_workload_resource_ref") is not None:
        preseed = _run([str(python), "-I", "-c",
              "import json,sys;from mcd_agent.universal_workload import preseed_runtime_workload_resources;"
              "print(json.dumps(preseed_runtime_workload_resources(runtime_dir=sys.argv[1],cache_dir=sys.argv[2],"
              "expected_manifest_sha256=sys.argv[3]),sort_keys=True))",
              str(stage), str(custom_cache_dir), _sha256(stage / MANIFEST_NAME)], cwd=Path("/"))
        try:
            resource_cache = json.loads((preseed.stdout or "").strip().splitlines()[-1])
        except Exception as exc:
            raise BridgeError("selected universal resource preseed returned invalid output") from exc
        ref = manifest["universal_workload_resource_ref"]
        cached = Path(str(resource_cache.get("cache_path") or "")).resolve()
        cache_root = Path(custom_cache_dir).resolve()
        if (resource_cache.get("status") != "ok" or resource_cache.get("key") != ref["key"]
                or resource_cache.get("sha256") != ref["sha256"]
                or not cached.is_file() or cached.is_symlink()
                or cache_root not in cached.parents or _sha256(cached) != ref["sha256"]):
            raise BridgeError("selected universal resource preseed verification failed")
    return {
        "package_version": package_version,
        "imported_version": imported_version,
        "imported_path": str(Path(imported_path).resolve()),
        "site_dir": str(site_dir.resolve()),
        "removed_metadata": json.dumps(removed_metadata, ensure_ascii=True),
        "removed_shadow_hooks": json.dumps(removed_hooks, ensure_ascii=True),
        "resource_cache": json.dumps(resource_cache, ensure_ascii=True, sort_keys=True),
    }


def _backup_lock_probe(install_dir: Path, config: Path) -> dict[str, Any]:
    """Read the legacy agent's backup lock without writing config/state."""
    if not config.is_file():
        raise BridgeError(f"MCD config not found for backup-lock probe: {config}")
    python = _venv_python(install_dir)
    code = (
        "import json, sys; "
        "from mcd_agent.config import load_config; "
        "from mcd_agent.backup import backup_lock_active; "
        "cfg=load_config(sys.argv[1], allow_recover_from_mcc=False); "
        "print(json.dumps({'enabled': bool(cfg.backup_enabled), 'active': bool(backup_lock_active(cfg))}))"
    )
    result = _run([str(python), "-c", code, str(config)], cwd=Path("/"))
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        raise BridgeError(f"backup-lock probe returned invalid JSON: {result.stdout!r}") from exc
    if not isinstance(payload, dict) or bool(payload.get("active")):
        raise BridgeError("MCD update deferred: backup lock is active")
    return {"enabled": bool(payload.get("enabled")), "active": False}


_PROFILE_AUTHORITY_SCHEMA = "mcd-profile-authority-probe-v1"
_PROFILE_SELECTION_FILE = "hardware-profile-selection.json"


def _profile_strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            return line[:index]
    if quote:
        raise BridgeError("unterminated string while reading include.files")
    return line


def _profile_array_depth(value: str) -> int:
    depth = 0
    quote = ""
    escaped = False
    for char in value:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise BridgeError("malformed include.files array")
    if quote:
        raise BridgeError("unterminated string in include.files array")
    return depth


def _profile_include_files_raw(config: Path) -> list[str]:
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeError(f"cannot read declared config file {config}: {exc}") from exc
    section = ""
    value_parts: list[str] | None = None
    values: list[str] | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        clean = _profile_strip_toml_comment(lines[index]).strip()
        index += 1
        if not clean:
            continue
        if clean.startswith("["):
            match = re.fullmatch(
                r"(?:\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\]|"
                r"\[\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\]\])",
                clean,
            )
            if not match:
                raise BridgeError(f"malformed TOML section in {config}")
            section = match.group(1) or match.group(2)
            continue
        if section != "include":
            continue
        if "=" not in clean:
            raise BridgeError(f"malformed include declaration in {config}")
        key, first_value = clean.split("=", 1)
        if key.strip() != "files":
            continue
        if value_parts is not None:
            raise BridgeError(f"duplicate include.files declaration in {config}")
        value_parts = [first_value.strip()]
        depth = _profile_array_depth(value_parts[0])
        while depth > 0 and index < len(lines):
            value_parts.append(_profile_strip_toml_comment(lines[index]).strip())
            index += 1
            depth = _profile_array_depth("\n".join(value_parts))
        if depth != 0:
            raise BridgeError(f"unterminated include.files array in {config}")
        try:
            parsed = ast.literal_eval("\n".join(value_parts))
        except (SyntaxError, ValueError) as exc:
            raise BridgeError(f"malformed include.files value in {config}") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, str) or not item.strip() for item in parsed):
            raise BridgeError(f"include.files must be a non-empty-string array in {config}")
        values = [item.strip() for item in parsed]
    if value_parts is None:
        return []
    return values or []


def _profile_include_files(config: Path) -> list[Path]:
    return [
        (config.parent / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
        for item in _profile_include_files_raw(config)
    ]


def _profile_config_files(config: Path, *, require_existing: bool = True) -> list[dict[str, Any]]:
    if not config.is_file():
        raise BridgeError(f"declared config file is missing: {config}")
    files: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        files.append({"path": str(resolved), "exists": resolved.is_file()})
        for child in _profile_include_files(resolved):
            if not child.is_file():
                files.append({"path": str(child), "exists": False})
                seen.add(child)
                if require_existing:
                    raise BridgeError(f"declared include file is missing: {child}")
                continue
            visit(child)

    visit(config)
    return files


def _profile_mutable_config_path(config: Path) -> Path:
    direct = _profile_include_files(config)
    existing = [path for path in direct if path.is_file()]
    return existing[-1] if existing else config


def _capture_profile_authority(install_dir: Path, config: Path) -> dict[str, Any]:
    """Capture profile authority without a co-transported probe sidecar."""
    # A source-free runtime can inherit exact legacy /opt/mcd/src include
    # declarations from the migration that retired that tree.  Capture those
    # missing paths as evidence; the verified successor runtime must repair
    # them before service restart.
    files = _profile_config_files(config, require_existing=False)
    selection_path = install_dir / "var" / _PROFILE_SELECTION_FILE
    exists = selection_path.is_file()
    raw = selection_path.read_bytes() if exists else b""
    return {
        "schema": _PROFILE_AUTHORITY_SCHEMA,
        "read_only": True,
        "capture_phase": "pre_runtime_install",
        "install_dir": str(install_dir),
        "config_path": str(config),
        "config_mutable_path": str(_profile_mutable_config_path(config)),
        "config_files": files,
        "selection_path": str(selection_path),
        "selection_exists": exists,
        "selection_sha256": _sha256(selection_path) if exists else "",
        "selection_bytes_base64": base64.b64encode(raw).decode("ascii"),
    }


def _declared_profile_name(path: Path) -> str:
    """Read only the literal [profile].name needed for package closure admission."""
    if not path.is_file():
        return ""
    section = ""
    selected = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        clean = _profile_strip_toml_comment(raw).strip()
        if not clean:
            continue
        if clean.startswith("["):
            match = re.fullmatch(r"\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\]", clean)
            section = match.group(1) if match else ""
            continue
        if section != "profile" or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        if key.strip() != "name":
            continue
        try:
            parsed = ast.literal_eval(value.strip())
        except (SyntaxError, ValueError) as exc:
            raise BridgeError(f"malformed profile.name in {path}") from exc
        if not isinstance(parsed, str):
            raise BridgeError(f"profile.name must be a string in {path}")
        selected = parsed.strip().lower()
    return selected


def _effective_declared_profile_name(config: Path) -> str:
    selected = ""
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        nonlocal selected
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            return
        seen.add(resolved)
        selected = _declared_profile_name(resolved) or selected
        for child in _profile_include_files(resolved):
            visit(child)

    visit(config)
    return selected


def _require_runtime_profile_closure(config: Path, manifest: dict[str, Any]) -> str:
    selected = _effective_declared_profile_name(config)
    if selected not in {"", "custom"} and manifest.get("universal_workload_resource_ref") is None:
        raise BridgeError(
            "named workload profile requires a runtime package with universal_workload_resource_ref"
        )
    return selected


def _migrate_runtime_config_includes(config: Path, install_dir: Path, runtime_dir: Path) -> dict[str, Any]:
    """Point exact managed legacy defaults at the verified runtime payload."""
    text = config.read_text(encoding="utf-8")
    before_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    legacy_root = (install_dir / "src" / "etc").resolve()
    runtime_root = (runtime_dir / "etc").resolve()
    allowed = {"mcd-agent.system.example.toml", "mcd-agent.operator.example.toml"}
    replacements: list[tuple[str, str]] = []
    for raw, resolved in zip(
        _profile_include_files_raw(config),
        _profile_include_files(config),
        strict=True,
    ):
        if resolved.parent != legacy_root or resolved.name not in allowed:
            continue
        successor = runtime_root / resolved.name
        if not successor.is_file() or successor.is_symlink():
            raise BridgeError(f"successor runtime include is missing: {successor}")
        replacement = str(successor) if Path(raw).is_absolute() else os.path.relpath(successor, config.parent)
        replacements.append((raw, replacement))

    updated = text
    for old, new in replacements:
        double_old = json.dumps(old)
        single_old = "'" + old.replace("'", "\\'") + "'"
        if double_old in updated:
            updated = updated.replace(double_old, json.dumps(new))
        elif single_old in updated:
            updated = updated.replace(single_old, "'" + new.replace("'", "\\'") + "'")
        else:
            raise BridgeError(f"cannot rewrite declared legacy include without changing unrelated config: {old}")

    if updated != text:
        mode = config.stat().st_mode & 0o777
        staged = config.with_name(f".{config.name}.runtime-includes-{os.getpid()}.tmp")
        staged.write_text(updated, encoding="utf-8")
        os.chmod(staged, mode)
        os.replace(staged, config)
    _profile_config_files(config, require_existing=True)
    return {
        "changed": updated != text,
        "replacements": [{"from": old, "to": new} for old, new in replacements],
        "before_sha256": before_sha256,
        "after_sha256": _sha256(config),
    }


def _pause_path(install_dir: Path) -> Path:
    return install_dir / "var" / "scheduler.pause"


def _pause_lock_path(install_dir: Path) -> Path:
    return install_dir / "var" / "scheduler.pause.lock"


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[Any]:
    if fcntl is None:
        raise BridgeError("POSIX flock is required for the runtime bridge")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BridgeError(f"MCD update deferred: update lease is active: {path}") from exc
    try:
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _scheduler_barrier(install_dir: Path) -> Iterator[bool]:
    pause = _pause_path(install_dir)
    lock = _pause_lock_path(install_dir)
    with _exclusive_lock(lock):
        existed = pause.exists()
        if not existed:
            pause.parent.mkdir(parents=True, exist_ok=True)
            pause.write_text("mcd-runtime-bootstrap\n", encoding="utf-8")
        try:
            yield not existed
        finally:
            if not existed:
                pause.unlink(missing_ok=True)


def _state_read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _state_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.bridge-{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _systemd_dropin_path(systemd_dir: Path, service_name: str) -> Path:
    return systemd_dir / f"{service_name}.service.d" / SERVICE_DROPIN_NAME


def _systemd_quote(value: Path | str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text or "\0" in text:
        raise BridgeError("systemd unit value contains a forbidden control character")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_systemd_dropin(
    systemd_dir: Path,
    service_name: str,
    install_dir: Path,
    config: Path,
) -> tuple[Path, bytes | None]:
    path = _systemd_dropin_path(systemd_dir, service_name)
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    python = _venv_python(install_dir)
    path.write_text(
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart={_systemd_quote(python)} -I -B -m mcd_agent run --config {_systemd_quote(config)}\n"
        f"WorkingDirectory={install_dir}\n"
        f"Environment={_systemd_quote('MCD_CONFIG=' + str(config))}\n"
        "KillMode=process\n",
        encoding="utf-8",
    )
    return path, previous


def _restore_dropin(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(previous)


def _retire_obsolete_dev_migration_dropin(
    systemd_dir: Path,
    service_name: str,
    install_dir: Path,
) -> tuple[Path | None, bytes | None]:
    path = systemd_dir / f"{service_name}.service.d" / "98-mcd-active-runtime-migration.conf"
    if not path.exists():
        return None, None
    previous = path.read_bytes()
    text = previous.decode("utf-8", errors="replace")
    required = (f"{install_dir}-dev-1.7/", "mcd-two-tenant-fixture/mcd.toml")
    if not all(marker in text for marker in required):
        raise BridgeError(f"refusing to retire unrecognized service drop-in: {path}")
    path.unlink()
    return path, previous


def _systemd_reload(systemctl: str) -> None:
    _run([systemctl, "daemon-reload"], cwd=Path("/"))


def _canonical_service_readback(
    systemctl: str,
    service_name: str,
    install_dir: Path,
    config: Path,
) -> dict[str, Any]:
    cat = _run([systemctl, "cat", service_name], cwd=Path("/"), check=False)
    show = _run(
        [
            systemctl,
            "show",
            service_name,
            "--property=ExecStart",
            "--property=WorkingDirectory",
            "--property=KillMode",
            "--property=Environment",
            "--no-pager",
        ],
        cwd=Path("/"),
        check=False,
    )
    if cat.returncode != 0 or show.returncode != 0:
        raise BridgeError(f"cannot read canonical systemd service: {service_name}")
    unit_text = str(cat.stdout or "")
    effective = str(show.stdout or "")
    forbidden = (f"{install_dir}-dev-1.7", "mcd-two-tenant-fixture", "fixture/mcd.toml")
    if any(marker in unit_text or marker in effective for marker in forbidden):
        raise BridgeError("canonical systemd service retains a development fixture reference")
    expected_python = str(_venv_python(install_dir))
    expected_config = str(config)
    checks = {
        "exec_python": expected_python in effective,
        "exec_config": f"--config {expected_config}" in effective,
        "working_directory": f"WorkingDirectory={install_dir}" in effective,
        "kill_mode": "KillMode=process" in effective,
    }
    if not all(checks.values()):
        raise BridgeError(f"canonical systemd service mismatch: {checks}")
    return {
        "exec_python": expected_python,
        "config": expected_config,
        "working_directory": str(install_dir),
        "kill_mode": "process",
        "development_fixture_refs": 0,
    }


def _restart_and_health(systemctl: str, service_name: str, timeout_sec: int) -> dict[str, Any]:
    _run([systemctl, "restart", service_name], cwd=Path("/"), timeout_sec=max(30, timeout_sec))
    deadline = time.monotonic() + max(10, timeout_sec)
    while time.monotonic() < deadline:
        active = _run([systemctl, "is-active", "--quiet", service_name], cwd=Path("/"), check=False)
        if active.returncode == 0:
            show = _run(
                [systemctl, "show", service_name, "--property=MainPID", "--property=ActiveState", "--value"],
                cwd=Path("/"),
                check=False,
            )
            values = [line.strip() for line in (show.stdout or "").splitlines() if line.strip()]
            return {"active": True, "main_pid": values[0] if values else "", "show": values}
        time.sleep(1)
    raise BridgeError(f"MCD service did not become active: {service_name}")


def _scheduler_status(install_dir: Path, config: Path) -> dict[str, Any] | None:
    if not config.is_file():
        return None
    python = _venv_python(install_dir)
    proc = _run(
        # ``mcd_agent.cli`` is the implementation module and deliberately has
        # no module-level ``__main__`` dispatch.  Calling it with ``-m`` can
        # therefore exit successfully without emitting JSON, which makes a
        # live bridge receipt report ``invalid_json`` even though the probe
        # never ran.  Use the shipped entry point so the receipt observes the
        # actual scheduler state.
        [str(python), "-m", "mcd_agent", "scheduler", "--config", str(config), "status", "--verbose", "--json"],
        cwd=install_dir,
        check=False,
    )
    if proc.returncode != 0:
        return {"status": "probe_failed", "returncode": proc.returncode}
    try:
        payload = json.loads((proc.stdout or "").strip())
    except Exception:
        return {"status": "invalid_json"}
    return payload if isinstance(payload, dict) else {"status": "invalid_payload"}


def _write_receipt(install_dir: Path, payload: dict[str, Any]) -> Path:
    path = install_dir / "var" / "runtime-bootstrap-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def _successful_replay(
    install_dir: Path,
    archive_sha256: str,
    expected_target: str,
) -> dict[str, Any] | None:
    """Return a fresh readback for an already completed exact candidate.

    MCC transport can replay a command after an SSH timeout.  Once the
    bridge has written a successful receipt, the same immutable archive must
    be a read-only acknowledgement rather than a second install.  A missing
    or inconsistent receipt falls through to the normal fail-closed path.
    """
    receipt_path = install_dir / "var" / "runtime-bootstrap-receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != RECEIPT_SCHEMA:
        return None
    if payload.get("status") != "success":
        return None
    if str(payload.get("archive_sha256") or "").lower() != archive_sha256.lower():
        return None
    target = str(payload.get("target") or "").strip()
    if not target or (expected_target and target != expected_target):
        return None
    if (install_dir / "src").exists():
        return None
    source_manifest = install_dir / "runtime" / MANIFEST_NAME
    if not source_manifest.is_file():
        return None
    try:
        manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or str(manifest.get("version") or "").strip() != target:
        return None
    try:
        readback = _install_runtime_probe(install_dir, target)
    except Exception:  # noqa: BLE001 - stale/incomplete state must use normal guarded flow
        return None
    replay = dict(payload)
    replay["idempotent_replay"] = True
    replay["replayed_at"] = datetime.now(timezone.utc).isoformat()
    replay["readback"] = readback
    return replay


def _same_version_pinned_rebuild_allowed(
    args: argparse.Namespace,
    *,
    original_dir: Path,
    runtime_dir: Path,
    current_manifest: Path,
    staged_manifest: Path,
    current_version: str,
    target_version: str,
) -> bool:
    """Admit only an explicit, fully pinned runtime-to-runtime DEV rebuild."""
    if not bool(getattr(args, "allow_same_version_rebuild", False)):
        return False
    if original_dir != runtime_dir or not current_manifest.is_file():
        raise BridgeError("same-version rebuild requires an active runtime-only layout")
    if _version_key(target_version) != _version_key(current_version):
        raise BridgeError("same-version rebuild flag is valid only for an equal runtime version")
    expected_target = str(getattr(args, "expected_target", "") or "").strip()
    expected_archive = str(getattr(args, "expected_archive_sha256", "") or "").strip().lower()
    expected_manifest = str(getattr(args, "expected_manifest_sha256", "") or "").strip().lower()
    if expected_target != target_version or not re.fullmatch(r"[0-9a-f]{64}", expected_archive):
        raise BridgeError("same-version rebuild requires exact target and archive pins")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest):
        raise BridgeError("same-version rebuild requires an exact manifest pin")
    staged_sha = _sha256(staged_manifest).lower()
    if staged_sha != expected_manifest:
        raise BridgeError(
            f"runtime manifest SHA mismatch expected={expected_manifest} actual={staged_sha}"
        )
    if _sha256(current_manifest).lower() == staged_sha:
        raise BridgeError("same-version rebuild is already active but has no matching success receipt")
    return True


def bridge(args: argparse.Namespace) -> dict[str, Any]:
    install_dir = Path(args.install_dir).resolve()
    archive = Path(args.archive).resolve()
    config = Path(args.config or (install_dir / "etc" / "mcd.toml")).resolve()
    systemd_dir = Path(args.systemd_dir).resolve()
    if not archive.is_file():
        raise BridgeError(f"runtime archive not found: {archive}")
    actual_archive_sha = _sha256(archive)
    if args.expected_archive_sha256:
        if actual_archive_sha.lower() != args.expected_archive_sha256.lower():
            raise BridgeError(
                f"runtime archive SHA mismatch expected={args.expected_archive_sha256} actual={actual_archive_sha}"
            )
    replay = _successful_replay(install_dir, actual_archive_sha, str(args.expected_target or "").strip())
    if replay is not None:
        return replay
    legacy_dir = install_dir / "src"
    runtime_dir = install_dir / "runtime"
    src_dir = runtime_dir if runtime_dir.is_dir() else legacy_dir
    original_dir = src_dir
    if runtime_dir.exists() and legacy_dir.exists():
        raise BridgeError("parallel legacy src requires audited retirement before runtime activation")
    if not src_dir.is_dir():
        raise BridgeError(f"legacy source directory not found: {src_dir}")
    if not (src_dir / "mcd_agent" / "__init__.py").is_file():
        raise BridgeError(f"legacy MCD source package not found: {src_dir}")
    old_source_version = ""
    try:
        old_text = (src_dir / "mcd_agent" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", old_text)
        old_source_version = match.group(1) if match else ""
    except OSError:
        pass

    profile_authority = _capture_profile_authority(install_dir, config)
    config_before_bytes = config.read_bytes()

    updates_dir = install_dir / "var" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    stage = updates_dir / f"runtime.next-bridge-{os.getpid()}-{time.time_ns()}"
    old_src = updates_dir / f"runtime.prev-bridge-{int(time.time())}"
    state_path = install_dir / "var" / STATE_NAME
    before_state = _state_read(state_path)
    before_state_bytes = state_path.read_bytes() if state_path.is_file() else None
    before_state_sha = _sha256(state_path) if state_path.is_file() else ""
    before_scheduler = _scheduler_status(install_dir, config)
    systemd_path: Path | None = None
    previous_dropin: bytes | None = None
    obsolete_dropin_path: Path | None = None
    obsolete_dropin_previous: bytes | None = None
    canonical_service: dict[str, Any] | None = None
    switched = False
    old_source_moved = False
    migration_complete = False
    manifest: dict[str, Any] = {}
    target = ""
    legacy_runtime_snapshot: Path | None = None
    config_migration: dict[str, Any] = {"changed": False, "replacements": []}

    with _exclusive_lock(install_dir / "var" / "mcd-self-update.lock"):
        if not args.sandbox:
            _backup_lock_probe(install_dir, config)
        manifest = _extract_archive(archive, stage)
        target = str(manifest.get("version") or "").strip()
        if args.expected_target and target != args.expected_target:
            raise BridgeError(f"runtime target mismatch expected={args.expected_target} actual={target}")
        declared_profile = _require_runtime_profile_closure(config, manifest)
        if args.expected_manifest_sha256:
            actual_manifest_sha = _sha256(stage / MANIFEST_NAME)
            if actual_manifest_sha.lower() != args.expected_manifest_sha256.lower():
                raise BridgeError(
                    f"runtime manifest SHA mismatch expected={args.expected_manifest_sha256} actual={actual_manifest_sha}"
                )
        same_version_pinned_rebuild = False
        if _version_key(target) <= _version_key(old_source_version) and (src_dir / MANIFEST_NAME).is_file():
            same_pinned_layout_migration = (
                original_dir == legacy_dir
                and _version_key(target) == _version_key(old_source_version)
                and _sha256(src_dir / MANIFEST_NAME) == _sha256(stage / MANIFEST_NAME)
            )
            if _version_key(target) == _version_key(old_source_version) and not same_pinned_layout_migration:
                same_version_pinned_rebuild = _same_version_pinned_rebuild_allowed(
                    args,
                    original_dir=original_dir,
                    runtime_dir=runtime_dir,
                    current_manifest=src_dir / MANIFEST_NAME,
                    staged_manifest=stage / MANIFEST_NAME,
                    current_version=old_source_version,
                    target_version=target,
                )
            if not same_pinned_layout_migration and not same_version_pinned_rebuild:
                raise BridgeError(f"runtime bridge target is not newer than active source: {old_source_version} -> {target}")

        with _scheduler_barrier(install_dir) as barrier_created:
            try:
                # Install and import the successor before moving the active
                # source directory.  The legacy daemon remains runnable until
                # every package/staging check succeeds.
                legacy_runtime_snapshot = _snapshot_legacy_runtime_package(install_dir)
                package_result = _install_runtime(install_dir, stage, manifest, custom_cache_dir=getattr(args, "custom_cache_dir", None))

                if old_src.exists():
                    raise BridgeError(f"rollback source path already exists: {old_src}")
                os.replace(src_dir, old_src)
                old_source_moved = True
                os.replace(stage, runtime_dir)
                src_dir = runtime_dir
                switched = True

                config_migration = _migrate_runtime_config_includes(config, install_dir, runtime_dir)

                if not args.no_systemd:
                    systemd_path, previous_dropin = _write_systemd_dropin(
                        systemd_dir,
                        args.service_name,
                        install_dir,
                        config,
                    )
                    obsolete_dropin_path, obsolete_dropin_previous = _retire_obsolete_dev_migration_dropin(
                        systemd_dir,
                        args.service_name,
                        install_dir,
                    )
                if not args.no_systemd:
                    _systemd_reload(args.systemctl)

                state = dict(before_state)
                state.update(
                    {
                        "last_status": "success",
                        "last_result": f"runtime bootstrap updated to {target}",
                        "last_target": target,
                        "last_attempt_ts": int(time.time()),
                        "last_session_id": "runtime-bootstrap",
                    }
                )
                _state_write(state_path, state)

                service_result: dict[str, Any] = {"skipped": True}
                if not args.no_systemd:
                    service_result = _restart_and_health(args.systemctl, args.service_name, args.health_timeout_sec)
                    systemd_path.unlink()
                    _systemd_reload(args.systemctl)
                    canonical_service = _canonical_service_readback(
                        args.systemctl,
                        args.service_name,
                        install_dir,
                        config,
                    )
                    service_result = _restart_and_health(args.systemctl, args.service_name, args.health_timeout_sec)
                    canonical_service = _canonical_service_readback(
                        args.systemctl,
                        args.service_name,
                        install_dir,
                        config,
                    )

                post_scheduler = _scheduler_status(install_dir, config)
                post_import = _install_runtime_probe(install_dir, target)
                receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "status": "success",
                    "bridge": (
                        "runtime-to-runtime-pinned-rebuild"
                        if same_version_pinned_rebuild
                        else "legacy-source-to-runtime-only"
                    ),
                    "target": target,
                    "archive": str(archive),
                    "archive_sha256": _sha256(archive),
                    "manifest_sha256": _sha256(src_dir / MANIFEST_NAME),
                    "before": {
                        "source_version": old_source_version,
                        "source_path": str(old_src),
                        "state_sha256": before_state_sha,
                        "scheduler": before_scheduler,
                        "profile_authority": profile_authority,
                    },
                    "after": {
                        "source_version": target,
                        "source_path": str(src_dir),
                        "installed": package_result,
                        "import_probe": post_import,
                        "scheduler": post_scheduler,
                        "state_sha256": _sha256(state_path) if state_path.is_file() else "",
                        "config_includes": config_migration,
                        "declared_profile": declared_profile or None,
                    },
                    "service": {
                        "systemd": not bool(args.no_systemd),
                        "dropin": "",
                        "transition_dropin_retired": str(systemd_path) if systemd_path else "",
                        "obsolete_dev_dropin_retired": (
                            str(obsolete_dropin_path) if obsolete_dropin_path else ""
                        ),
                        "working_directory": str(install_dir),
                        "kill_mode": "process",
                        "health": service_result,
                        "canonical_readback": canonical_service,
                    },
                    "scheduler_barrier_created": bool(barrier_created),
                    "rollback_source_retained": False,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                receipt_path = _write_receipt(install_dir, receipt)
                receipt["receipt"] = str(receipt_path)
                _write_receipt(install_dir, receipt)
                migration_complete = True
                return receipt
            except Exception:
                # Do not leave the service pointing at a half-installed source.
                if switched or old_source_moved:
                    if src_dir.exists():
                        shutil.rmtree(src_dir)
                    os.replace(old_src, original_dir)
                if systemd_path is not None:
                    _restore_dropin(systemd_path, previous_dropin)
                    if obsolete_dropin_path is not None:
                        _restore_dropin(obsolete_dropin_path, obsolete_dropin_previous)
                    if not args.no_systemd:
                        _systemd_reload(args.systemctl)
                if legacy_runtime_snapshot is not None:
                    try:
                        _restore_legacy_runtime_package(install_dir, legacy_runtime_snapshot)
                    except Exception:
                        # Preserve the original error. The source rollback is
                        # still recorded by the caller's traceback/error path;
                        # the managed venv snapshot is best-effort cleanup.
                        pass
                if config.read_bytes() != config_before_bytes:
                    temporary_config = config.with_name(f".{config.name}.rollback-{os.getpid()}.tmp")
                    temporary_config.write_bytes(config_before_bytes)
                    os.chmod(temporary_config, config.stat().st_mode & 0o777)
                    os.replace(temporary_config, config)
                if before_state_bytes is None:
                    state_path.unlink(missing_ok=True)
                else:
                    temporary = state_path.with_name(f".{state_path.name}.rollback-{os.getpid()}.tmp")
                    temporary.write_bytes(before_state_bytes)
                    os.replace(temporary, state_path)
                raise
            finally:
                if migration_complete and old_src.exists():
                    shutil.rmtree(old_src)
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
                if legacy_runtime_snapshot is not None and legacy_runtime_snapshot.exists():
                    shutil.rmtree(legacy_runtime_snapshot, ignore_errors=True)


def _install_runtime_probe(install_dir: Path, target: str) -> dict[str, str]:
    python = _venv_python(install_dir)
    probe = _run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m, mcd_agent; print(m.version('mcd-agent')); print(mcd_agent.__version__); print(mcd_agent.__file__)",
        ],
        cwd=install_dir,
    )
    lines = [line.strip() for line in (probe.stdout or "").splitlines() if line.strip()]
    if len(lines) < 3 or lines[-3] != target or lines[-2] != target:
        raise BridgeError(f"post-switch runtime probe mismatch target={target}: {lines!r}")
    return {"metadata_version": lines[-3], "imported_version": lines[-2], "imported_path": str(Path(lines[-1]).resolve())}


def _fresh_config_readiness(install_dir: Path, config: Path, cache_dir: Path) -> dict[str, Any]:
    """Prove the installed package can load its complete fresh-host config.

    Import/version checks alone do not exercise profile application.  The
    universal workload selection is consumed during config loading, so this
    probe must pass before an external wrapper may create/start the service or
    publish a successful bootstrap receipt.
    """
    python = _venv_python(install_dir)
    code = (
        "import json,sys;"
        "from mcd_agent.config import load_config;"
        "from mcd_agent.universal_workload import required_workload_resource;"
        "cfg=load_config(sys.argv[1],allow_recover_from_mcc=False);"
        "resource=required_workload_resource(cfg);"
        "print(json.dumps({'status':'ok','cache_dir':cfg.custom_cache_dir,"
        "'profile_name':cfg.profile_name,'resource_root':resource['root']},sort_keys=True))"
    )
    probe = _run([str(python), "-I", "-c", code, str(config)], cwd=Path("/"))
    try:
        payload = json.loads((probe.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        raise BridgeError("fresh runtime config readiness returned invalid output") from exc
    if (not isinstance(payload, dict) or payload.get("status") != "ok"
            or Path(str(payload.get("cache_dir") or "")).resolve() != cache_dir.resolve()
            or not str(payload.get("profile_name") or "").strip()
            or not str(payload.get("resource_root") or "").strip()):
        raise BridgeError("fresh runtime config readiness failed")
    return payload


def fresh_install(args: argparse.Namespace) -> dict[str, Any]:
    """Install one immutable runtime into an otherwise empty managed prefix.

    The external family wrapper owns the venv/config/cache path and service
    provisioning.  This generic bridge owns archive verification, wheel
    installation, exact selected-data cache preparation and activation under
    ``runtime``.  It never creates a development/source tree.
    """
    install_dir = Path(args.install_dir).resolve()
    archive = Path(args.archive).resolve()
    config = Path(args.config or (install_dir / "etc" / "mcd.toml")).resolve()
    runtime_dir = install_dir / "runtime"
    legacy_dir = install_dir / "src"
    custom_cache_arg = getattr(args, "custom_cache_dir", None)
    if not archive.is_file():
        raise BridgeError(f"runtime archive not found: {archive}")
    archive_sha256 = _sha256(archive)
    if args.expected_archive_sha256 and archive_sha256.lower() != args.expected_archive_sha256.lower():
        raise BridgeError(
            f"runtime archive SHA mismatch expected={args.expected_archive_sha256} actual={archive_sha256}"
        )
    if custom_cache_arg is None:
        raise BridgeError("fresh runtime install requires an explicit custom cache directory")
    custom_cache_dir = Path(custom_cache_arg).resolve()
    if (not install_dir.is_dir() or install_dir.is_symlink() or not config.is_file()
            or config.is_symlink() or not custom_cache_dir.is_dir() or custom_cache_dir.is_symlink()):
        raise BridgeError("fresh runtime install requires a prepared managed prefix, config and cache")
    _venv_python(install_dir)
    replay = _successful_replay(install_dir, archive_sha256, str(args.expected_target or "").strip())
    if replay is not None:
        replay["config_readiness"] = _fresh_config_readiness(install_dir, config, custom_cache_dir)
        return replay
    if legacy_dir.exists() or runtime_dir.exists():
        raise BridgeError("fresh runtime install requires an empty managed prefix with no source/runtime tree")

    updates_dir = install_dir / "var" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    stage = updates_dir / f"runtime.next-fresh-{os.getpid()}-{time.time_ns()}"
    package_snapshot: Path | None = None
    activated = False
    cached_path: Path | None = None
    cached_before = False
    completed = False
    with _exclusive_lock(install_dir / "var" / "mcd-self-update.lock"):
        try:
            manifest = _extract_archive(archive, stage)
            target = str(manifest.get("version") or "").strip()
            if args.expected_target and target != args.expected_target:
                raise BridgeError(f"runtime target mismatch expected={args.expected_target} actual={target}")
            manifest_sha256 = _sha256(stage / MANIFEST_NAME)
            if (args.expected_manifest_sha256
                    and manifest_sha256.lower() != args.expected_manifest_sha256.lower()):
                raise BridgeError(
                    "runtime manifest SHA mismatch "
                    f"expected={args.expected_manifest_sha256} actual={manifest_sha256}"
                )
            ref = manifest.get("universal_workload_resource_ref")
            if ref is not None:
                selected = _universal_selected_resource(ref)
                cached_path = (
                    custom_cache_dir / "downloads" / selected["key"]
                    / f"{selected['key']}-{selected['sha256']}.tar.gz"
                )
                cached_before = cached_path.is_file()

            package_snapshot = _snapshot_legacy_runtime_package(install_dir)
            package_result = _install_runtime(
                install_dir, stage, manifest, custom_cache_dir=custom_cache_dir
            )
            os.replace(stage, runtime_dir)
            activated = True
            import_probe = _install_runtime_probe(install_dir, target)
            config_probe = _fresh_config_readiness(install_dir, config, custom_cache_dir)
            if legacy_dir.exists():
                raise BridgeError("fresh runtime install created a forbidden source tree")
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "status": "success",
                "bridge": "fresh-runtime-only",
                "target": target,
                "archive": str(archive),
                "archive_sha256": archive_sha256,
                "manifest_sha256": _sha256(runtime_dir / MANIFEST_NAME),
                "before": {"source_path": None, "fresh_prefix": True},
                "after": {
                    "source_version": target,
                    "source_path": str(runtime_dir),
                    "installed": package_result,
                    "import_probe": import_probe,
                    "config_readiness": config_probe,
                },
                "service": {
                    "systemd": False,
                    "external_wrapper_required": True,
                    "ready_for_service_start": True,
                },
                "rollback_source_retained": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            receipt_path = _write_receipt(install_dir, receipt)
            receipt["receipt"] = str(receipt_path)
            _write_receipt(install_dir, receipt)
            completed = True
            return receipt
        except Exception:
            if activated and runtime_dir.exists():
                shutil.rmtree(runtime_dir, ignore_errors=True)
            if package_snapshot is not None:
                try:
                    _restore_legacy_runtime_package(install_dir, package_snapshot)
                except Exception:
                    pass
            if cached_path is not None and not cached_before:
                cached_path.unlink(missing_ok=True)
                for parent in (cached_path.parent, cached_path.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if package_snapshot is not None and package_snapshot.exists():
                shutil.rmtree(package_snapshot, ignore_errors=True)
            if not completed:
                (install_dir / "var" / "runtime-bootstrap-receipt.json").unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default="/opt/mcd")
    parser.add_argument("--archive", help="Locally transported runtime-only archive")
    parser.add_argument("--config")
    parser.add_argument("--expected-archive-sha256")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-target")
    parser.add_argument("--service-name", default="mcd")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--health-timeout-sec", type=int, default=45)
    parser.add_argument("--no-systemd", action="store_true", help="Only for disposable sandbox proof")
    parser.add_argument("--sandbox", action="store_true", help="Allow the disposable no-backup-probe path")
    parser.add_argument(
        "--capture-profile-authority",
        action="store_true",
        help="Read-only pre-bridge profile-selection capture; does not require an archive",
    )
    parser.add_argument(
        "--allow-same-version-rebuild",
        action="store_true",
        help="DEV only: install a different runtime manifest at the same version; exact target/archive/manifest pins are mandatory",
    )
    parser.add_argument(
        "--fresh-install",
        action="store_true",
        help="Install into a prepared empty prefix; service provisioning remains external",
    )
    parser.add_argument("--custom-cache-dir", type=Path, help="Explicit existing data cache for a selected universal resource")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.capture_profile_authority:
        install_dir = Path(args.install_dir).resolve()
        config = Path(args.config or (install_dir / "etc" / "mcd.toml")).resolve()
        print(json.dumps(_capture_profile_authority(install_dir, config), ensure_ascii=True, indent=2))
        return 0
    if not args.archive:
        print("--archive is required unless --capture-profile-authority is used", file=sys.stderr)
        return 2
    if args.no_systemd and not args.sandbox:
        print("--no-systemd is restricted to --sandbox", file=sys.stderr)
        return 2
    try:
        result = fresh_install(args) if args.fresh_install else bridge(args)
    except Exception as exc:
        payload = {"schema": RECEIPT_SCHEMA, "status": "failed", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
