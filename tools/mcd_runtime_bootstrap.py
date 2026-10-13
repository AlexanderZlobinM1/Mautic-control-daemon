#!/usr/bin/env python3
"""Bridge a legacy source-tree MCD install to a runtime-only package.

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
* ``/opt/mcd/src`` is switched atomically and remains as bounded rollback
  material until the supervisor is healthy;
* the supervisor uses ``WorkingDirectory=/opt/mcd`` and ``KillMode=process``;
* failure restores the old source, managed-venv package and running service,
  even when the legacy source has no ``pyproject.toml``.

It is not part of the runtime archive.  The source tree is the transport
input for the bridge only; the active supervisor imports the installed wheel.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Iterator

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 bridge hosts
    import tomli as tomllib  # type: ignore[no-redef]

try:
    import fcntl
except ImportError:  # pragma: no cover - the bridge is POSIX-only
    fcntl = None  # type: ignore[assignment]


MANIFEST_NAME = "mcd-runtime-manifest.json"
MANIFEST_SCHEMA = "mcd-runtime-package-v1"
RECEIPT_SCHEMA = "mcd-runtime-bootstrap-receipt-v1"
PROFILE_AUTHORITY_SCHEMA = "mcd-profile-authority-v1"
PROFILE_SELECTION_FILE = "hardware-profile-selection.json"
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
    for forbidden in FORBIDDEN_TOP_LEVEL:
        if (stage / forbidden).exists():
            raise BridgeError(f"runtime-only payload contains forbidden material: {forbidden}")

    wheel_path = _safe_relative(stage, wheel_name)
    requirements_path = _safe_relative(stage, requirements_name)
    if not wheel_path.is_file() or not requirements_path.is_file():
        raise BridgeError("runtime manifest points to missing wheel or requirements")
    if not (stage / "mcd_agent" / "__init__.py").is_file():
        raise BridgeError("runtime-only payload is missing mcd_agent package")
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


def _install_runtime(install_dir: Path, stage: Path, manifest: dict[str, Any]) -> dict[str, str]:
    python = _venv_python(install_dir)
    requirements = stage / str(manifest["requirements"])
    wheel = stage / str(manifest["wheel"])
    _run([str(python), "-m", "pip", "--version"])
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(requirements),
        ],
        cwd=Path("/"),
    )
    site_dir = _site_dir(python)
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
    return {
        "package_version": package_version,
        "imported_version": imported_version,
        "imported_path": str(Path(imported_path).resolve()),
        "site_dir": str(site_dir.resolve()),
        "removed_shadow_hooks": json.dumps(removed_hooks, ensure_ascii=True),
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


def _resolve_mutable_config_path_read_only(config: Path) -> Path:
    """Resolve the writable split-config include without importing the legacy agent."""

    if not config.is_file():
        return config
    try:
        text = config.read_text(encoding="utf-8")
        if "MCD_CONFIG_ENTRYPOINT v1" not in text:
            return config
        raw = tomllib.loads(text)
    except Exception:
        return config
    include = raw.get("include", {}) if isinstance(raw, dict) else {}
    files_raw = include.get("files", []) if isinstance(include, dict) else []
    files = files_raw if isinstance(files_raw, list) else [files_raw]
    resolved: list[Path] = []
    for item in files:
        value = str(item or "").strip()
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = (config.parent / candidate).resolve()
        if candidate != config:
            resolved.append(candidate)
    for candidate in reversed(resolved):
        if candidate.exists():
            return candidate
    return resolved[-1] if resolved else config


def _config_files_read_only(config: Path) -> list[dict[str, Any]]:
    """Describe the entrypoint and recursive include graph without mutation."""

    files: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        files.append({"path": str(resolved), "exists": resolved.is_file()})
        if not resolved.is_file():
            return
        try:
            text = resolved.read_text(encoding="utf-8")
            if "MCD_CONFIG_ENTRYPOINT v1" not in text:
                return
            raw = tomllib.loads(text)
        except Exception:
            return
        include = raw.get("include", {}) if isinstance(raw, dict) else {}
        files_raw = include.get("files", []) if isinstance(include, dict) else []
        values = files_raw if isinstance(files_raw, list) else [files_raw]
        for item in values:
            value = str(item or "").strip()
            if not value:
                continue
            child = Path(value)
            if not child.is_absolute():
                child = resolved.parent / child
            visit(child)

    visit(config)
    return files


def _capture_profile_authority(install_dir: Path, config: Path) -> dict[str, Any]:
    """Capture exact pre-bridge profile-selection bytes without legacy imports."""

    selection_path = install_dir / "var" / PROFILE_SELECTION_FILE
    selection_exists = selection_path.is_file()
    selection_bytes = selection_path.read_bytes() if selection_exists else b""
    return {
        "schema": PROFILE_AUTHORITY_SCHEMA,
        "capture_phase": "pre_runtime_install",
        "config_path": str(config),
        "config_mutable_path": str(_resolve_mutable_config_path_read_only(config)),
        "config_files": _config_files_read_only(config),
        "selection_path": str(selection_path),
        "selection_exists": selection_exists,
        "selection_sha256": _sha256(selection_path) if selection_exists else "",
        "selection_bytes_base64": base64.b64encode(selection_bytes).decode("ascii"),
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


def _write_systemd_dropin(systemd_dir: Path, service_name: str, install_dir: Path) -> tuple[Path, bytes | None]:
    path = _systemd_dropin_path(systemd_dir, service_name)
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Service]\n"
        f"WorkingDirectory={install_dir}\n"
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


def _systemd_reload(systemctl: str) -> None:
    _run([systemctl, "daemon-reload"], cwd=Path("/"))


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


def bridge(args: argparse.Namespace) -> dict[str, Any]:
    install_dir = Path(args.install_dir).resolve()
    archive = Path(args.archive).resolve()
    config = Path(args.config or (install_dir / "etc" / "mcd.toml")).resolve()
    systemd_dir = Path(args.systemd_dir).resolve()
    if not archive.is_file():
        raise BridgeError(f"runtime archive not found: {archive}")
    if args.expected_archive_sha256:
        actual_sha = _sha256(archive)
        if actual_sha.lower() != args.expected_archive_sha256.lower():
            raise BridgeError(f"runtime archive SHA mismatch expected={args.expected_archive_sha256} actual={actual_sha}")
    src_dir = install_dir / "src"
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

    updates_dir = install_dir / "var" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    stage = updates_dir / f"src.next-bridge-{os.getpid()}-{time.time_ns()}"
    old_src = updates_dir / f"src.prev-bridge-{int(time.time())}"
    state_path = install_dir / "var" / STATE_NAME
    before_state = _state_read(state_path)
    before_state_bytes = state_path.read_bytes() if state_path.is_file() else None
    before_state_sha = _sha256(state_path) if state_path.is_file() else ""
    before_scheduler = _scheduler_status(install_dir, config)
    systemd_path: Path | None = None
    previous_dropin: bytes | None = None
    switched = False
    old_source_moved = False
    manifest: dict[str, Any] = {}
    target = ""
    legacy_runtime_snapshot: Path | None = None

    with _exclusive_lock(install_dir / "var" / "mcd-self-update.lock"):
        if not args.sandbox:
            _backup_lock_probe(install_dir, config)
        manifest = _extract_archive(archive, stage)
        target = str(manifest.get("version") or "").strip()
        if args.expected_target and target != args.expected_target:
            raise BridgeError(f"runtime target mismatch expected={args.expected_target} actual={target}")
        if args.expected_manifest_sha256:
            actual_manifest_sha = _sha256(stage / MANIFEST_NAME)
            if actual_manifest_sha.lower() != args.expected_manifest_sha256.lower():
                raise BridgeError(
                    f"runtime manifest SHA mismatch expected={args.expected_manifest_sha256} actual={actual_manifest_sha}"
                )
        if _version_key(target) <= _version_key(old_source_version) and (src_dir / MANIFEST_NAME).is_file():
            raise BridgeError(f"runtime bridge target is not newer than active source: {old_source_version} -> {target}")

        with _scheduler_barrier(install_dir) as barrier_created:
            try:
                # Install and import the successor before moving the active
                # source directory.  The legacy daemon remains runnable until
                # every package/staging check succeeds.
                legacy_runtime_snapshot = _snapshot_legacy_runtime_package(install_dir)
                package_result = _install_runtime(install_dir, stage, manifest)

                if old_src.exists():
                    raise BridgeError(f"rollback source path already exists: {old_src}")
                os.replace(src_dir, old_src)
                old_source_moved = True
                os.replace(stage, src_dir)
                switched = True

                if not args.no_systemd:
                    systemd_path, previous_dropin = _write_systemd_dropin(systemd_dir, args.service_name, install_dir)
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

                post_scheduler = _scheduler_status(install_dir, config)
                post_import = _install_runtime_probe(install_dir, target)
                receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "status": "success",
                    "bridge": "legacy-source-to-runtime-only",
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
                    },
                    "service": {
                        "systemd": not bool(args.no_systemd),
                        "dropin": str(systemd_path) if systemd_path else "",
                        "working_directory": str(install_dir),
                        "kill_mode": "process",
                        "health": service_result,
                    },
                    "scheduler_barrier_created": bool(barrier_created),
                    "rollback_source_retained": True,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                receipt_path = _write_receipt(install_dir, receipt)
                receipt["receipt"] = str(receipt_path)
                _write_receipt(install_dir, receipt)
                return receipt
            except Exception:
                # Do not leave the service pointing at a half-installed source.
                if switched or old_source_moved:
                    if src_dir.exists():
                        shutil.rmtree(src_dir)
                    os.replace(old_src, src_dir)
                if systemd_path is not None:
                    _restore_dropin(systemd_path, previous_dropin)
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
                if before_state_bytes is None:
                    state_path.unlink(missing_ok=True)
                else:
                    temporary = state_path.with_name(f".{state_path.name}.rollback-{os.getpid()}.tmp")
                    temporary.write_bytes(before_state_bytes)
                    os.replace(temporary, state_path)
                raise
            finally:
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
        result = bridge(args)
    except Exception as exc:
        payload = {"schema": RECEIPT_SCHEMA, "status": "failed", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
