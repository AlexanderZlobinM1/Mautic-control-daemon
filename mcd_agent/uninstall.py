from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess


@dataclass
class UninstallResult:
    ok: bool
    lines: list[str]


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, out


def _read_crontab(user: str | None = None) -> tuple[int, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-l")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.returncode, ""
    return 0, proc.stdout or ""


def _write_crontab(content: str, user: str | None = None) -> tuple[int, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-")
    proc = subprocess.run(cmd, input=content, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, out


def _restore_from_markers(content: str) -> str:
    out: list[str] = []
    restore_next = False
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("# MCD_MANAGED") or s.startswith("# mcd-managed:"):
            restore_next = True
            continue
        if restore_next and line.startswith("# "):
            out.append(line[2:])
            restore_next = False
            continue
        out.append(line)
        restore_next = False
    return "\n".join(out) + ("\n" if content.endswith("\n") else "")


def run_uninstall(
    *,
    service_name: str = "mcd",
    install_dir: str = "/opt/mcd",
    etc_dir: str = "/etc/mcd",
    purge: bool = True,
) -> UninstallResult:
    lines: list[str] = []
    if os.geteuid() != 0:
        return UninstallResult(ok=False, lines=["Uninstall requires root privileges"])

    backup_root = Path(install_dir) / "var" / "backup" / "root.pre-mcd.crontab"
    backup_www = Path(install_dir) / "var" / "backup" / "www-data.pre-mcd.crontab"

    # Stop and disable service first.
    rc, out = _run(["systemctl", "stop", service_name])
    lines.append(f"systemctl stop {service_name}: rc={rc} {out}".strip())
    rc, out = _run(["systemctl", "disable", service_name])
    lines.append(f"systemctl disable {service_name}: rc={rc} {out}".strip())

    # Restore crontab for root.
    if backup_root.exists():
        content = backup_root.read_text(encoding="utf-8")
        rc, out = _write_crontab(content, None)
        lines.append(f"restore root crontab from backup: rc={rc} {out}".strip())
    else:
        rc, cur = _read_crontab(None)
        if rc == 0 and cur.strip():
            restored = _restore_from_markers(cur)
            rc2, out2 = _write_crontab(restored, None)
            lines.append(f"restore root crontab from markers: rc={rc2} {out2}".strip())
        else:
            lines.append("root crontab backup not found and current crontab empty/unreadable")

    # Restore crontab for www-data.
    if backup_www.exists():
        content = backup_www.read_text(encoding="utf-8")
        rc, out = _write_crontab(content, "www-data")
        lines.append(f"restore www-data crontab from backup: rc={rc} {out}".strip())
    else:
        lines.append("www-data crontab backup not found")

    # Remove service unit and reload daemon.
    unit = Path("/etc/systemd/system") / f"{service_name}.service"
    if unit.exists():
        unit.unlink()
        lines.append(f"removed unit file: {unit}")
    rc, out = _run(["systemctl", "daemon-reload"])
    lines.append(f"systemctl daemon-reload: rc={rc} {out}".strip())

    # Remove wrapper binary.
    wrapper = Path("/usr/local/bin/mcd-cli")
    if wrapper.exists():
        wrapper.unlink()
        lines.append(f"removed wrapper: {wrapper}")

    # Remove config symlink/file.
    link = Path(etc_dir) / "mcd.toml"
    if link.exists() or link.is_symlink():
        link.unlink()
        lines.append(f"removed config link: {link}")

    if purge:
        for p in (Path(install_dir), Path(etc_dir)):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                lines.append(f"purged path: {p}")

    return UninstallResult(ok=True, lines=lines)

