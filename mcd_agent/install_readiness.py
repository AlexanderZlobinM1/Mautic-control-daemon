from __future__ import annotations

from datetime import datetime, timezone
import grp
import pwd
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from mcd_agent.apt_profile import collect_zabbix_agent_state
from mcd_agent.env import ipv6_runtime_disabled, ipv6_status, reconcile_ipv6_runtime_from_intent
from mcd_agent.nginx_baseline import cloudflare_real_ip_state


def _run(args: list[str], *, timeout_sec: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec, check=False)
        return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:
        return 1, str(exc)


def _cmd_version(cmd: str, args: list[str] | None = None) -> tuple[bool, str]:
    path = shutil.which(cmd)
    if not path:
        return False, ""
    rc, out = _run([path] + list(args or ["--version"]))
    if rc != 0 and not out:
        return True, ""
    return True, out.splitlines()[0].strip() if out else ""


def _version_tuple(raw: str) -> list[int]:
    nums = re.findall(r"\d+", str(raw or ""))
    out = [int(x) for x in nums[:3]]
    while len(out) < 3:
        out.append(0)
    return out


def _version_major_minor(raw: str) -> str:
    nums = _version_tuple(raw)
    if nums[0] <= 0:
        return ""
    return f"{nums[0]}.{nums[1]}"


def _database_version_tuple(raw: str, engine: str) -> list[int]:
    text = str(raw or "")
    if engine == "mariadb":
        for pattern in (r"\bDistrib\s+([0-9]+(?:\.[0-9]+){1,2})", r"\bVer\s+([0-9]+(?:\.[0-9]+){1,2})-MariaDB"):
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                return _version_tuple(m.group(1))
        m = re.search(r"\b([0-9]+(?:\.[0-9]+){1,2})-MariaDB\b", text, flags=re.IGNORECASE)
        if m:
            return _version_tuple(m.group(1))
    return _version_tuple(text)


def _ipv6_disabled() -> bool:
    return ipv6_runtime_disabled(ipv6_status()) is True


def _php_versions() -> dict[str, Any]:
    candidates: set[str] = set()
    php = shutil.which("php")
    if php:
        candidates.add(php)
    for p in Path("/usr/bin").glob("php[0-9]*.[0-9]*"):
        if p.is_file() and p.name not in {"phpize", "php-config"}:
            candidates.add(str(p))

    versions: dict[str, dict[str, Any]] = {}
    for path in sorted(candidates):
        rc, out = _run([path, "-v"])
        if rc != 0:
            continue
        line = out.splitlines()[0].strip() if out else ""
        mm = _version_major_minor(line)
        if not mm:
            continue
        versions[mm] = {"binary": path, "version": line}

    fpm: dict[str, dict[str, Any]] = {}
    rc, out = _run(["systemctl", "list-unit-files", "php*-fpm.service", "--type=service", "--no-legend"], timeout_sec=10)
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"\bphp([0-9]+\.[0-9]+)-fpm\.service\b", line)
            if not m:
                continue
            ver = m.group(1)
            active_rc, active = _run(["systemctl", "is-active", f"php{ver}-fpm"], timeout_sec=4)
            fpm[ver] = {
                "service": f"php{ver}-fpm",
                "active": active_rc == 0 and active.strip() == "active",
                "socket": f"/run/php/php{ver}-fpm.sock",
            }
    return {"versions": versions, "fpm": fpm}


def _database_state() -> dict[str, Any]:
    candidates = ["mariadb", "mysql"]
    version_line = ""
    for cmd in candidates:
        exists, line = _cmd_version(cmd, ["--version"])
        if exists:
            version_line = line
            break
    engine = ""
    if "mariadb" in version_line.lower():
        engine = "mariadb"
    elif version_line:
        engine = "mysql"
    service = "unknown"
    for svc in ("mariadb", "mysql"):
        rc, out = _run(["systemctl", "is-active", svc], timeout_sec=4)
        if rc == 0 and out.strip() == "active":
            service = svc
            break
    return {
        "engine": engine,
        "version": version_line,
        "version_tuple": _database_version_tuple(version_line, engine) if version_line else [0, 0, 0],
        "service": service,
        "active": service != "unknown",
    }


def _path_state(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"path": path, "exists": False, "is_dir": False}
    try:
        st = p.stat()
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except Exception:
            owner = str(st.st_uid)
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except Exception:
            group = str(st.st_gid)
        return {
            "path": path,
            "exists": True,
            "is_dir": p.is_dir(),
            "owner": owner,
            "group": group,
            "mode": oct(st.st_mode & 0o777),
        }
    except Exception as exc:
        return {"path": path, "exists": True, "is_dir": False, "error": str(exc)}


def collect_mautic_install_readiness() -> dict[str, Any]:
    reconcile_ipv6_runtime_from_intent()
    composer_exists, composer_version = _cmd_version("composer", ["--version"])
    node_exists, node_version = _cmd_version("node", ["--version"])
    npm_exists, npm_version = _cmd_version("npm", ["--version"])
    nginx_exists, nginx_version = _cmd_version("nginx", ["-v"])
    certbot_exists, certbot_version = _cmd_version("certbot", ["--version"])
    nginx_rc, nginx_state = _run(["systemctl", "is-active", "nginx"], timeout_sec=4)
    php = _php_versions()
    return {
        "schema": "mcd-mautic-install-readiness-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ipv6_disabled": _ipv6_disabled(),
        "nginx": {
            "installed": bool(nginx_exists),
            "active": nginx_rc == 0 and nginx_state.strip() == "active",
            "version": nginx_version,
            "cloudflare_real_ip": cloudflare_real_ip_state(),
        },
        "php": php,
        "composer": {
            "installed": bool(composer_exists),
            "version": composer_version,
            "path": shutil.which("composer") or "",
        },
        "node": {
            "installed": bool(node_exists),
            "version": node_version,
            "path": shutil.which("node") or "",
            "version_tuple": _version_tuple(node_version) if node_version else [0, 0, 0],
        },
        "npm": {"installed": bool(npm_exists), "version": npm_version, "path": shutil.which("npm") or ""},
        "certbot": {"installed": bool(certbot_exists), "version": certbot_version},
        "database": _database_state(),
        "zabbix_agent": collect_zabbix_agent_state(profile={"zabbix_agent_enabled": True}),
        "paths": {"var_www": _path_state("/var/www")},
    }
