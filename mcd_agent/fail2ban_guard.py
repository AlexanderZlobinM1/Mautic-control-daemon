from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


NGINX_4XX_FILTER = Path("/etc/fail2ban/filter.d/nginx-4xx-scan.conf")
NGINX_4XX_JAIL = "nginx-4xx-scan"
# Fail2ban applies jail.d files lexicographically.  The safety override must
# sort after provider/site files (for example sales-snap-*.local), otherwise a
# later enabled=true silently resurrects the path-agnostic jail on reload.
NGINX_4XX_SAFETY_OVERRIDE = Path("/etc/fail2ban/jail.d/zzzz-mcd-nginx-4xx-safety.local")
NGINX_4XX_SAFETY_MARKER = "# Managed by MCD: disable path-agnostic 4xx bans"


def _definition_value(text: str, key: str) -> str:
    match = re.search(rf"(?ims)^\s*{re.escape(key)}\s*=\s*(.*?)(?=^\S|\Z)", text)
    return match.group(1).strip() if match else ""


def _is_path_agnostic_4xx_filter(text: str) -> bool:
    failregex = _definition_value(text, "failregex")
    if not failregex:
        return False
    generic_target = "[^\"]+" in failregex or "[^\\\"]+" in failregex
    broad_statuses = all(status in failregex for status in ("400", "403", "404"))
    return generic_target and broad_statuses


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _run(client: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [client, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _status_ips(output: str) -> set[str]:
    for raw in output.splitlines():
        if "Banned IP list:" in raw:
            return {value for value in raw.split("Banned IP list:", 1)[1].split() if value}
    return set()


def _active_jails(output: str) -> list[str]:
    for raw in output.splitlines():
        if "Jail list:" in raw:
            return [value.strip() for value in raw.split("Jail list:", 1)[1].split(",") if value.strip()]
    return []


def ensure_nginx_4xx_scan_safety(
    *,
    filter_path: Path = NGINX_4XX_FILTER,
    override_path: Path = NGINX_4XX_SAFETY_OVERRIDE,
    jail: str = NGINX_4XX_JAIL,
) -> dict[str, Any]:
    if not filter_path.exists():
        return {"status": "skipped", "reason": "nginx_4xx_filter_missing", "changed": False}
    if not _is_path_agnostic_4xx_filter(filter_path.read_text(encoding="utf-8", errors="ignore")):
        return {"status": "noop", "reason": "filter_is_path_scoped", "changed": False}
    if os.geteuid() != 0:
        return {"status": "skipped", "reason": "root_required", "changed": False}

    client = shutil.which("fail2ban-client")
    if not client:
        return {"status": "skipped", "reason": "fail2ban_client_missing", "changed": False}

    all_status = _run(client, "status")
    active = _active_jails(all_status.stdout) if all_status.returncode == 0 else []
    bad_status = _run(client, "status", jail) if jail in active else None
    bad_ips = _status_ips(bad_status.stdout) if bad_status and bad_status.returncode == 0 else set()

    preserved_by_jail: dict[str, set[str]] = {}
    for other in active:
        if other == jail:
            continue
        status = _run(client, "status", other)
        if status.returncode == 0:
            overlap = bad_ips & _status_ips(status.stdout)
            if overlap:
                preserved_by_jail[other] = overlap

    content = f"{NGINX_4XX_SAFETY_MARKER}\n[{jail}]\nenabled = false\n"
    changed = not override_path.exists() or override_path.read_text(encoding="utf-8", errors="ignore") != content
    if changed:
        _atomic_write(override_path, content)

    if changed:
        reload_proc = _run(client, "reload")
        if reload_proc.returncode != 0:
            detail = (reload_proc.stderr or reload_proc.stdout or f"rc={reload_proc.returncode}").strip()
            return {"status": "error", "reason": f"fail2ban_reload_failed:{detail}", "changed": changed}

        refreshed = _run(client, "status")
        active = _active_jails(refreshed.stdout) if refreshed.returncode == 0 else active
        if jail in active:
            refreshed_bad = _run(client, "status", jail)
            if refreshed_bad.returncode == 0:
                bad_ips |= _status_ips(refreshed_bad.stdout)
            for other in active:
                if other == jail:
                    continue
                status = _run(client, "status", other)
                if status.returncode == 0:
                    overlap = bad_ips & _status_ips(status.stdout)
                    if overlap:
                        preserved_by_jail.setdefault(other, set()).update(overlap)

    if jail in active:
        for ip in sorted(bad_ips):
            _run(client, "set", jail, "unbanip", ip)
        _run(client, "stop", jail)

    for other, ips in preserved_by_jail.items():
        for ip in sorted(ips):
            _run(client, "set", other, "unbanip", ip)
            _run(client, "set", other, "banip", ip)

    preserved = set().union(*preserved_by_jail.values()) if preserved_by_jail else set()
    return {
        "status": "applied" if changed or jail in active else "noop",
        "reason": "path_agnostic_4xx_jail_disabled",
        "changed": changed or jail in active,
        "released_bans": len(bad_ips - preserved),
        "preserved_bans": len(preserved),
    }


def ensure_nginx_4xx_browser_icon_guard(**kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point retained for agents upgrading from 0.10.31."""
    return ensure_nginx_4xx_scan_safety(**kwargs)
