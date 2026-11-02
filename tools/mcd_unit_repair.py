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

    unit = systemd_dir / f"{service}.service"
    if not unit.is_file():
        raise RepairError(f"systemd unit missing: {unit}")
    dropin = systemd_dir / f"{service}.service.d" / DROPIN_NAME
    previous = dropin.read_bytes() if dropin.exists() else None
    content = "[Service]\nWorkingDirectory=/opt/mcd\nKillMode=process\n"
    before = {prop: _show(systemctl, service, prop) for prop in ("WorkingDirectory", "ExecStart", "KillMode")}
    try:
        _atomic_write(dropin, content)
        _run([systemctl, "daemon-reload"])
        _run([systemctl, "restart", service])
        after = _health(systemctl, service, timeout)
        receipt = {
            "schema": "mcd-unit-repair-v1",
            "status": "success",
            "service": service,
            "dropin": str(dropin),
            "before": before,
            "after": after,
            "config": str(config),
            "venv_python": str(venv_python),
            "source_checkout_used": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_path = install_dir / "var" / "repair" / f"mcd-unit-repair-{int(time.time())}.json"
        _atomic_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n", 0o600)
        receipt["receipt"] = str(receipt_path)
        return receipt
    except Exception:
        if previous is None:
            dropin.unlink(missing_ok=True)
        else:
            dropin.write_bytes(previous)
        _run([systemctl, "daemon-reload"], check=False)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default="/opt/mcd")
    parser.add_argument("--service", default="mcd")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--health-timeout-sec", type=int, default=45)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = repair(
            install_dir=Path(args.install_dir).resolve(),
            service=args.service,
            systemctl=args.systemctl,
            systemd_dir=Path(args.systemd_dir).resolve(),
            timeout=args.health_timeout_sec,
        )
    except RepairError as exc:
        print(f"mcd unit repair failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) if args.json else result["receipt"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
