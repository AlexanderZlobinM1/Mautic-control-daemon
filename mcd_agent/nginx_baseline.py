"""Core fact and pending shared Mautic/delete compatibility helpers.

Host baseline recipes run exclusively through the verified external task.
Shared vhost generators below remain pending their own extraction acceptance.
"""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Any

from mcd_agent.nginx_external import (
    default_stub_operation, ensure_nginx_baseline, ensure_cloudflare_real_ip,
    cloudflare_real_ip_state, nginx_baseline_satisfied,
)



_CLOUDFLARE_REAL_IP_PROFILE_KEYS = {
    "cloudflare_real_ip_enabled",
    "cloudflare_real_ip_target_file",
    "cloudflare_real_ip_header",
    "cloudflare_real_ip_cidrs",
    "cloudflare_real_ip_set_real_ip_from",
    "cloudflare_real_ip_recursive",
    "cloudflare_real_ip_remove_when_disabled",
}


@dataclass
class _Snapshot:
    path: Path
    kind: str
    backup: Path | None = None
    target: str | None = None


def _snapshot(path: Path, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> None:
    path = Path(path)
    if path in snapshots:
        return
    if path.is_symlink():
        snapshots[path] = _Snapshot(path=path, kind="symlink", target=os.readlink(path))
        return
    if os.path.exists(path):
        rel = str(path).lstrip("/").replace("/", "__")
        backup = backup_dir / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        snapshots[path] = _Snapshot(path=path, kind="file", backup=backup)
        return
    snapshots[path] = _Snapshot(path=path, kind="missing")


def _restore_snapshots(snapshots: dict[Path, _Snapshot]) -> None:
    for snap in reversed(list(snapshots.values())):
        path = snap.path
        try:
            if os.path.lexists(path):
                path.unlink()
            if snap.kind == "file" and snap.backup is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap.backup, path)
            elif snap.kind == "symlink" and snap.target is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(snap.target, path)
        except Exception:
            continue














def _ipv6_listen_forbidden() -> bool:
    try:
        from mcd_agent.env import ipv6_runtime_disabled, ipv6_status

        return ipv6_runtime_disabled(ipv6_status()) is True
    except Exception:
        return False




def cloudflare_real_ip_profile_present(profile: dict[str, Any] | None) -> bool:
    if not isinstance(profile, dict):
        return False
    return any(key in profile for key in _CLOUDFLARE_REAL_IP_PROFILE_KEYS)
