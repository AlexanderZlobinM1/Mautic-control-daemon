#!/usr/bin/env python3
"""Repair an existing MCD systemd unit without touching Mautic data.

This is the supported first-aid operation for the 1.6.40 ``/opt/mcd/src``
regression.  It only installs a managed systemd drop-in, reloads systemd and
restarts MCD.  The installed venv, config, state database and queues remain
the source of truth; a source checkout is never created or used.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


DROPIN_NAME = "99-mcd-runtime-package.conf"


class RepairError(RuntimeError):
    pass


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, cwd="/", text=True, capture_output=True, check=False)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise RepairError(f"{' '.join(argv)}: {detail}")
    return proc


def _show(systemctl: str, service: str, prop: str) -> str:
    proc = _run([systemctl, "show", service, f"--property={prop}", "--value"])
    return (proc.stdout or "").strip()


def _runtime_probe(venv_python: Path, install_dir: Path, expected_version: str = "") -> dict[str, str]:
    code = (
        "import importlib.metadata as m, mcd_agent; "
        "print(m.version('mcd-agent')); print(mcd_agent.__version__); print(mcd_agent.__file__)"
    )
    proc = subprocess.run(
        [str(venv_python), "-I", "-B", "-c", code],
        cwd=str(install_dir), env={"PATH": os.defpath}, text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RepairError(f"managed venv import probe failed: {(proc.stderr or proc.stdout).strip()}")
    values = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(values) < 3:
        raise RepairError("managed venv import probe returned incomplete output")
    metadata_version, imported_version, imported_path = values[-3:]
    if expected_version and metadata_version != expected_version:
        raise RepairError(f"managed venv version mismatch expected={expected_version} actual={metadata_version}")
    resolved = str(Path(imported_path).resolve())
    if "/src/" in f"/{resolved.lstrip('/')}" or resolved.startswith(str(install_dir / "src")):
        raise RepairError(f"managed venv imports forbidden source checkout: {resolved}")
    if str((install_dir / "venv").resolve()) not in resolved:
        raise RepairError(f"managed venv imports outside managed venv: {resolved}")
    return {"metadata_version": metadata_version, "imported_version": imported_version, "imported_path": resolved}


def _scheduler_status(venv_python: Path, install_dir: Path, config: Path) -> dict[str, object]:
    proc = subprocess.run(
        [str(venv_python), "-m", "mcd_agent", "scheduler", "--config", str(config),
         "status", "--verbose", "--json"],
        cwd=str(install_dir), env={"PATH": os.defpath}, text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RepairError(f"scheduler status failed: {(proc.stderr or proc.stdout).strip()}")
    output = proc.stdout or ""
    payload: object | None = None
    try:
        payload = json.loads(output.strip())
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        required = {"running_tasks", "tracked_running_tasks", "external_running_tasks"}
        for index, char in enumerate(output):
            if char != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and required.issubset(candidate):
                payload = candidate
                break
    if payload is None:
        raise RepairError("scheduler status did not return JSON")
    if not isinstance(payload, dict) or bool(payload.get("paused")):
        raise RepairError("scheduler is paused after repair")
    for key in ("running_tasks", "tracked_running_tasks", "external_running_tasks"):
        value = payload.get(key)
        if not isinstance(value, int) or value < 0:
            raise RepairError(f"scheduler status missing valid {key}")
    return payload


def _scheduler_gate(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    def task_keys(payload: dict[str, object]) -> set[str]:
        rows = []
        for key in ("tracked_tasks", "external_tasks"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
        return {json.dumps(row, sort_keys=True, ensure_ascii=True) for row in rows if isinstance(row, dict)}
    before_keys = task_keys(before)
    after_keys = task_keys(after)
    if before_keys and not (before_keys & after_keys):
        raise RepairError("scheduler worker re-adoption evidence is missing")
    return {
        "admission": "accepted" if not bool(after.get("paused")) else "rejected",
        "completion_probe": "not_executed",
        "status_probe": "ok",
        "worker_re_adoption": "verified" if before_keys & after_keys else "not_applicable_no_running_tasks",
        "before_running_tasks": int(before.get("running_tasks", 0) or 0),
        "after_running_tasks": int(after.get("running_tasks", 0) or 0),
        "pilot_cycle_required": True,
    }


def _rollback_readback(systemctl: str, service: str) -> dict[str, object]:
    active = _run([systemctl, "is-active", "--quiet", service], check=False)
    return {
        "active": active.returncode == 0,
        "ActiveState": _show(systemctl, service, "ActiveState"),
        "SubState": _show(systemctl, service, "SubState"),
        "WorkingDirectory": _show(systemctl, service, "WorkingDirectory"),
        "ExecStart": _show(systemctl, service, "ExecStart"),
        "KillMode": _show(systemctl, service, "KillMode"),
    }


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _health(systemctl: str, service: str, timeout: int) -> dict[str, str]:
    deadline = time.monotonic() + max(10, timeout)
    while time.monotonic() < deadline:
        active = _run([systemctl, "is-active", "--quiet", service], check=False)
        if active.returncode == 0:
            values = {
                "ActiveState": _show(systemctl, service, "ActiveState"),
                "SubState": _show(systemctl, service, "SubState"),
                "MainPID": _show(systemctl, service, "MainPID"),
                "WorkingDirectory": _show(systemctl, service, "WorkingDirectory"),
                "ExecStart": _show(systemctl, service, "ExecStart"),
                "KillMode": _show(systemctl, service, "KillMode"),
            }
            if values["WorkingDirectory"] != "/opt/mcd":
                raise RepairError(f"unexpected WorkingDirectory={values['WorkingDirectory']!r}")
            if values["KillMode"] != "process":
                raise RepairError(f"unexpected KillMode={values['KillMode']!r}")
            if "-m mcd_agent run" not in values["ExecStart"]:
                raise RepairError("ExecStart does not invoke mcd_agent run")
            return values
        time.sleep(1)
    raise RepairError(f"{service} did not become active within {timeout}s")


def repair(
    *,
    install_dir: Path,
    service: str,
    systemctl: str,
    systemd_dir: Path,
    timeout: int,
    allow_nonproduction: bool = False,
    expected_runtime_version: str = "",
) -> dict[str, object]:
    if install_dir != Path("/opt/mcd") and not allow_nonproduction:
        raise RepairError("production repair requires --install-dir /opt/mcd")
    venv_python = install_dir / "venv" / "bin" / "python"
    config = install_dir / "etc" / "mcd.toml"
    if not venv_python.is_file() or not os.access(venv_python, os.X_OK):
        raise RepairError(f"managed venv python missing or not executable: {venv_python}")
    if not config.is_file():
        raise RepairError(f"MCD config missing: {config}")
    if (install_dir / "src").exists():
        raise RepairError("refusing repair while forbidden /opt/mcd/src exists")
    runtime_before = _runtime_probe(venv_python, install_dir, expected_runtime_version)

    unit = systemd_dir / f"{service}.service"
    if not unit.is_file():
        raise RepairError(f"systemd unit missing: {unit}")
    dropin = systemd_dir / f"{service}.service.d" / DROPIN_NAME
    previous = dropin.read_bytes() if dropin.exists() else None
    content = "[Service]\nWorkingDirectory=/opt/mcd\nKillMode=process\n"
    before = {prop: _show(systemctl, service, prop) for prop in ("WorkingDirectory", "ExecStart", "KillMode")}
    scheduler_before = _scheduler_status(venv_python, install_dir, config)
    try:
        _atomic_write(dropin, content)
        _run([systemctl, "daemon-reload"])
        _run([systemctl, "restart", service])
        after = _health(systemctl, service, timeout)
        runtime_after = _runtime_probe(venv_python, install_dir, expected_runtime_version)
        scheduler_after = _scheduler_status(venv_python, install_dir, config)
        scheduler_gate = _scheduler_gate(scheduler_before, scheduler_after)
        receipt = {
            "schema": "mcd-unit-repair-v1",
            "status": "success",
            "service": service,
            "dropin": str(dropin),
            "before": before,
            "after": after,
            "runtime_before": runtime_before,
            "runtime_after": runtime_after,
            "scheduler_before": scheduler_before,
            "scheduler_after": scheduler_after,
            "scheduler_gate": scheduler_gate,
            "config": str(config),
            "venv_python": str(venv_python),
            "source_checkout_used": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_path = install_dir / "var" / "repair" / f"mcd-unit-repair-{int(time.time())}.json"
        _atomic_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n", 0o600)
        receipt["receipt"] = str(receipt_path)
        return receipt
    except Exception as original_error:
        rollback = {"status": "started"}
        if previous is None:
            dropin.unlink(missing_ok=True)
        else:
            dropin.write_bytes(previous)
        reload_result = _run([systemctl, "daemon-reload"], check=False)
        rollback["daemon_reload"] = reload_result.returncode == 0
        restart_result = _run([systemctl, "restart", service], check=False)
        rollback["restart"] = restart_result.returncode == 0
        rollback["readback"] = _rollback_readback(systemctl, service)
        rollback["health"] = "readback_only_original_state_may_be_unhealthy"
        rollback["error"] = str(original_error)
        failure = install_dir / "var" / "repair" / f"mcd-unit-repair-failed-{int(time.time())}.json"
        _atomic_write(failure, json.dumps({"schema": "mcd-unit-repair-v1", "status": "failed", "rollback": rollback}, indent=2) + "\n", 0o600)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default="/opt/mcd")
    parser.add_argument("--service", default="mcd")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--health-timeout-sec", type=int, default=45)
    parser.add_argument("--expected-runtime-version", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = repair(
            install_dir=Path(args.install_dir).resolve(),
            service=args.service,
            systemctl=args.systemctl,
            systemd_dir=Path(args.systemd_dir).resolve(),
            timeout=args.health_timeout_sec,
            expected_runtime_version=args.expected_runtime_version,
        )
    except RepairError as exc:
        print(f"mcd unit repair failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) if args.json else result["receipt"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
