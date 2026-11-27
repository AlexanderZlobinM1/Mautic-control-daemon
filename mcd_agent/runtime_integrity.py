"""Fail-closed checks for the runtime that actually executes MCD."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any


_MANIFEST_NAME = "mcd-runtime-manifest.json"
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def _clean_env(*, extra_path: str = "") -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    if extra_path:
        env["PYTHONPATH"] = extra_path
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_error(errors: list[dict[str, Any]], code: str, **facts: Any) -> None:
    item: dict[str, Any] = {"code": code}
    item.update({key: value for key, value in facts.items() if value not in (None, "")})
    errors.append(item)


def _module_snapshot() -> dict[str, Any]:
    import mcd_agent
    import mcd_agent.daemon as daemon
    import mcd_agent.empty_leads_cleanup as cleanup

    daemon_path = Path(str(getattr(daemon, "__file__", ""))).resolve()
    cleanup_path = Path(str(getattr(cleanup, "__file__", ""))).resolve()
    return {
        "running_version": str(getattr(mcd_agent, "__version__", "") or ""),
        "package_version": str(importlib.metadata.version("mcd-agent") or ""),
        "module_path": str(Path(str(getattr(mcd_agent, "__file__", ""))).resolve()),
        "daemon_path": str(daemon_path),
        "daemon_sha256": _sha256(daemon_path) if daemon_path.is_file() else "",
        "cleanup_module_path": str(cleanup_path),
        "cleanup_module_sha256": _sha256(cleanup_path) if cleanup_path.is_file() else "",
        "python": str(Path(sys.executable).resolve()),
    }


def _probe_interpreter(interpreter: Path) -> dict[str, Any]:
    code = (
        "import hashlib, importlib.metadata as m, json, pathlib, sys; "
        "import mcd_agent, mcd_agent.daemon as d, mcd_agent.empty_leads_cleanup as c; "
        "p=pathlib.Path(d.__file__).resolve(); h=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ''; "
        "cp=pathlib.Path(c.__file__).resolve(); ch=hashlib.sha256(cp.read_bytes()).hexdigest() if cp.is_file() else ''; "
        "print(json.dumps({'running_version':str(getattr(mcd_agent,'__version__','') or ''),"
        "'package_version':str(m.version('mcd-agent') or ''),"
        "'module_path':str(pathlib.Path(mcd_agent.__file__).resolve()),"
        "'daemon_path':str(p),'daemon_sha256':h,'cleanup_module_path':str(cp),"
        "'cleanup_module_sha256':ch,'python':str(pathlib.Path(sys.executable).resolve())},"
        "sort_keys=True))"
    )
    try:
        proc = subprocess.run(
            [str(interpreter), "-I", "-c", code], cwd="/", env=_clean_env(),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"{type(exc).__name__}:{exc}"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "interpreter probe failed").strip()[-1000:]}
    try:
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "interpreter probe returned invalid JSON"}
    return payload if isinstance(payload, dict) else {"error": "interpreter probe returned non-object"}


def _systemd_property(service: str, property_name: str) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "show", service, f"--property={property_name}", "--value"],
            cwd="/", env=_clean_env(), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{type(exc).__name__}:{exc}"
    if proc.returncode != 0:
        return "", (proc.stderr or proc.stdout or "systemctl show failed").strip()[-1000:]
    return (proc.stdout or "").strip(), ""


def _parse_exec_start(raw: str) -> str:
    value = str(raw or "").strip()
    match = re.search(r"(?:^|\s)path=([^;\s]+)", value)
    if match:
        return match.group(1).strip()
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    for token in tokens:
        if token.startswith("/"):
            return token
    return ""


def _wrapper_path(interpreter: Path, explicit: str | Path | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(interpreter.parent / "mcd-cli")
    found = shutil.which("mcd-cli")
    if found:
        candidates.append(Path(found))
    if Path(sys.argv[0]).name == "mcd-cli":
        candidates.append(Path(sys.argv[0]))
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if str(resolved) in seen or not resolved.is_file():
            continue
        seen.add(str(resolved))
        return resolved
    return None


def _wrapper_probe(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "missing"}
    try:
        proc = subprocess.run(
            [str(path), "--version"], cwd="/", env=_clean_env(),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "path": str(path), "error": f"{type(exc).__name__}:{exc}"}
    output = (proc.stdout or proc.stderr or "").strip()
    match = _VERSION_RE.search(output)
    return {
        "status": "ok" if proc.returncode == 0 and match else "error",
        "path": str(path), "version": match.group(1) if match else "",
        "returncode": proc.returncode, "output": output[-500:],
    }


def _under_site_packages(path: str) -> bool:
    try:
        parts = Path(path).resolve().parts
    except OSError:
        return False
    return "site-packages" in parts or "dist-packages" in parts


def check_runtime_integrity(
    *, install_dir: str | Path = "/opt/mcd", service_name: str = "mcd.service",
    wrapper_path: str | Path | None = None, expected_version: str = "",
    expected_source_commit: str = "", expected_daemon_sha256: str = "",
    expected_archive_sha256: str = "", strict: bool = False,
) -> dict[str, Any]:
    """Return machine-readable parity evidence for the active MCD runtime."""
    install = Path(install_dir).resolve()
    runtime = install / "runtime"
    manifest_path = runtime / _MANIFEST_NAME
    errors: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "schema": "mcd-runtime-integrity-v1", "install_dir": str(install),
        "runtime_dir": str(runtime), "manifest_path": str(manifest_path),
        "strict": bool(strict), "candidate": {}, "current": {},
        "systemd": {}, "wrapper": {}, "errors": errors,
    }
    if not manifest_path.is_file():
        if strict:
            _record_error(errors, "runtime_manifest_missing", path=str(manifest_path))
            payload["status"] = "failed"
        else:
            payload["status"] = "not_applicable"
        return payload
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _record_error(errors, "runtime_manifest_invalid", error=str(exc))
        payload["status"] = "failed"
        return payload
    if not isinstance(manifest, dict) or manifest.get("schema") != "mcd-runtime-package-v1":
        _record_error(errors, "runtime_manifest_schema_invalid")
        payload["status"] = "failed"
        return payload
    manifest_payload = manifest.get("payload_sha256") if isinstance(manifest.get("payload_sha256"), dict) else {}
    candidate_version = str(manifest.get("version") or "").strip()
    candidate_commit = str(manifest.get("source_commit") or "").strip()
    candidate_daemon = str(manifest_payload.get("mcd_agent/daemon.py") or "").strip().lower()
    candidate_cleanup = str(manifest_payload.get("mcd_agent/empty_leads_cleanup.py") or "").strip().lower()
    wheel_name = str(manifest.get("wheel") or "").strip()
    wheel_path = (runtime / wheel_name).resolve() if wheel_name else runtime / "__missing_wheel__"
    candidate_wheel = str(manifest_payload.get(wheel_name) or "").strip().lower()
    payload["candidate"] = {
        "version": candidate_version, "source_commit": candidate_commit,
        "daemon_sha256": candidate_daemon,
        "cleanup_module_sha256": candidate_cleanup,
        "wheel": str(wheel_path),
        "wheel_sha256": candidate_wheel, "manifest_sha256": _sha256(manifest_path),
    }
    required_version = str(expected_version or candidate_version).strip()
    required_commit = str(expected_source_commit or candidate_commit).strip()
    required_daemon = str(expected_daemon_sha256 or candidate_daemon).strip().lower()
    if not candidate_version:
        _record_error(errors, "runtime_candidate_version_missing")
    if required_version and candidate_version != required_version:
        _record_error(errors, "runtime_candidate_version_mismatch", expected=required_version, actual=candidate_version)
    if required_commit and candidate_commit != required_commit:
        _record_error(errors, "runtime_candidate_source_commit_mismatch", expected=required_commit, actual=candidate_commit)
    if not candidate_daemon or not re.fullmatch(r"[0-9a-f]{64}", candidate_daemon):
        _record_error(errors, "runtime_candidate_daemon_hash_missing")
    elif not (runtime / "mcd_agent" / "daemon.py").is_file() or _sha256(runtime / "mcd_agent" / "daemon.py") != candidate_daemon:
        _record_error(errors, "runtime_source_daemon_hash_mismatch", expected=candidate_daemon)
    if candidate_cleanup and (
        not (runtime / "mcd_agent" / "empty_leads_cleanup.py").is_file()
        or _sha256(runtime / "mcd_agent" / "empty_leads_cleanup.py") != candidate_cleanup
    ):
        _record_error(errors, "runtime_source_cleanup_module_hash_mismatch", expected=candidate_cleanup)
    if not candidate_wheel or not re.fullmatch(r"[0-9a-f]{64}", candidate_wheel):
        _record_error(errors, "runtime_candidate_wheel_hash_missing")
    elif wheel_path.is_file() and _sha256(wheel_path) != candidate_wheel:
        _record_error(errors, "runtime_wheel_hash_mismatch", expected=candidate_wheel)
    elif not wheel_path.is_file():
        _record_error(errors, "runtime_wheel_missing", path=str(wheel_path))
    if expected_archive_sha256:
        receipt_path = install / "var" / "runtime-package-receipt.json"
        observed_archive = ""
        if receipt_path.is_file():
            try:
                observed_archive = str(json.loads(receipt_path.read_text(encoding="utf-8")).get("source_archive_sha256") or "").strip().lower()
            except (OSError, ValueError):
                observed_archive = ""
        if observed_archive != str(expected_archive_sha256).strip().lower():
            _record_error(errors, "runtime_archive_identity_mismatch", expected=expected_archive_sha256, actual=observed_archive)
    try:
        current = _module_snapshot()
    except Exception as exc:
        current = {"error": f"{type(exc).__name__}:{exc}"}
        _record_error(errors, "runtime_current_import_probe_failed", error=str(exc))
    payload["current"] = current
    if isinstance(current, dict) and "error" not in current:
        for key, code in (("running_version", "runtime_current_version_mismatch"), ("package_version", "runtime_current_package_metadata_mismatch")):
            if required_version and str(current.get(key) or "") != required_version:
                _record_error(errors, code, expected=required_version, actual=current.get(key))
        if required_daemon and str(current.get("daemon_sha256") or "").lower() != required_daemon:
            _record_error(errors, "runtime_current_daemon_hash_mismatch", expected=required_daemon, actual=current.get("daemon_sha256"))
        if candidate_cleanup and str(current.get("cleanup_module_sha256") or "").lower() != candidate_cleanup:
            _record_error(errors, "runtime_current_cleanup_module_hash_mismatch", expected=candidate_cleanup, actual=current.get("cleanup_module_sha256"))
    exec_raw, systemd_error = _systemd_property(service_name, "ExecStart")
    active, _ = _systemd_property(service_name, "ActiveState")
    main_pid, _ = _systemd_property(service_name, "MainPID")
    systemd_interpreter = Path(_parse_exec_start(exec_raw)) if exec_raw else None
    payload["systemd"] = {"service": service_name, "active_state": active, "main_pid": main_pid, "exec_start": exec_raw, "interpreter": str(systemd_interpreter) if systemd_interpreter else ""}
    if not exec_raw:
        if strict:
            _record_error(errors, "runtime_systemd_execstart_missing", error=systemd_error)
    else:
        expected_interpreter = install / "venv" / "bin" / "python"
        if systemd_interpreter is None or systemd_interpreter.resolve() != expected_interpreter.resolve():
            _record_error(errors, "runtime_systemd_interpreter_mismatch", expected=str(expected_interpreter), actual=str(systemd_interpreter or ""))
        if active and active != "active":
            _record_error(errors, "runtime_systemd_not_active", actual=active)
        probe = _probe_interpreter(systemd_interpreter or expected_interpreter)
        payload["systemd"]["probe"] = probe
        if probe.get("error"):
            _record_error(errors, "runtime_systemd_import_probe_failed", error=probe["error"])
        else:
            for key, code in (("running_version", "runtime_systemd_running_version_mismatch"), ("package_version", "runtime_systemd_package_metadata_mismatch")):
                if required_version and str(probe.get(key) or "") != required_version:
                    _record_error(errors, code, expected=required_version, actual=probe.get(key))
            if required_daemon and str(probe.get("daemon_sha256") or "").lower() != required_daemon:
                _record_error(errors, "runtime_systemd_daemon_hash_mismatch", expected=required_daemon, actual=probe.get("daemon_sha256"))
            if candidate_cleanup and str(probe.get("cleanup_module_sha256") or "").lower() != candidate_cleanup:
                _record_error(errors, "runtime_systemd_cleanup_module_hash_mismatch", expected=candidate_cleanup, actual=probe.get("cleanup_module_sha256"))
            if not _under_site_packages(str(probe.get("module_path") or "")):
                _record_error(errors, "runtime_systemd_imported_path_not_site_packages", actual=probe.get("module_path"))
    wrapper = _wrapper_probe(_wrapper_path(systemd_interpreter or Path(sys.executable), wrapper_path))
    payload["wrapper"] = wrapper
    if wrapper.get("status") == "missing":
        if strict:
            _record_error(errors, "runtime_mcd_cli_wrapper_missing")
    elif wrapper.get("status") != "ok":
        _record_error(errors, "runtime_mcd_cli_wrapper_probe_failed", error=wrapper.get("error") or wrapper.get("output"))
    elif required_version and str(wrapper.get("version") or "") != required_version:
        _record_error(errors, "runtime_mcd_cli_wrapper_version_mismatch", expected=required_version, actual=wrapper.get("version"))
    payload["status"] = "failed" if errors else "ok"
    return payload
