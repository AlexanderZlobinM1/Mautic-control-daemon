from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

SYSCTL_FILE = Path("/etc/sysctl.d/99-disable-ipv6.conf")
PERSISTENT_KEYS = [
    "net.ipv6.conf.all.disable_ipv6",
    "net.ipv6.conf.default.disable_ipv6",
    "net.ipv6.conf.lo.disable_ipv6",
]


def _runtime_ipv6_keys() -> list[str]:
    keys = list(PERSISTENT_KEYS)
    root = Path("/proc/sys/net/ipv6/conf")
    try:
        for p in sorted(root.glob("*/disable_ipv6")):
            iface = p.parent.name
            key = f"net.ipv6.conf.{iface}.disable_ipv6"
            if key not in keys:
                keys.append(key)
    except Exception:
        pass
    return keys


def ipv6_status() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in _runtime_ipv6_keys():
        p = Path("/proc/sys") / Path(key.replace(".", "/"))
        try:
            out[key] = p.read_text(encoding="utf-8").strip()
        except Exception:
            out[key] = "?"
    out["persistent_file"] = str(SYSCTL_FILE)
    out["persistent_exists"] = "1" if SYSCTL_FILE.exists() else "0"
    out["persistent_intent"] = "1" if _persistent_file_disables_ipv6() else "0"
    return out


def _status_ipv6_keys(st: dict[str, str]) -> list[str]:
    return sorted(
        k
        for k in st
        if k.startswith("net.ipv6.conf.")
        and k.endswith(".disable_ipv6")
    )


def ipv6_runtime_disabled(st: dict[str, str] | None = None) -> bool | None:
    data = st if st is not None else ipv6_status()
    keys = _status_ipv6_keys(data)
    if not keys:
        return True if ipv6_disable_intent_enabled(data) else None
    vals = [str(data.get(k, "?")).strip() for k in keys]
    known = [v for v in vals if v in {"0", "1"}]
    if not known:
        return True if ipv6_disable_intent_enabled(data) else None
    if any(v == "0" for v in known):
        return False
    if any(v not in {"0", "1"} for v in vals):
        return True if ipv6_disable_intent_enabled(data) else None
    return True


def ipv6_disable_intent_enabled(st: dict[str, str] | None = None) -> bool:
    data = st if st is not None else ipv6_status()
    if str(data.get("persistent_intent", "")).strip() == "1":
        return True
    if str(data.get("persistent_exists", "")).strip() != "1":
        return False
    if all(str(data.get(k, "")).strip() == "1" for k in PERSISTENT_KEYS):
        return True
    return _persistent_file_disables_ipv6()


def _persistent_file_disables_ipv6() -> bool:
    try:
        text = SYSCTL_FILE.read_text(encoding="utf-8")
    except Exception:
        return False
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip()
    return all(found.get(k) == "1" for k in PERSISTENT_KEYS)


def reconcile_ipv6_runtime_from_intent() -> list[str]:
    st = ipv6_status()
    if not ipv6_disable_intent_enabled(st):
        return []
    changed: list[str] = []
    for key in _status_ipv6_keys(st):
        if str(st.get(key, "")).strip() == "1":
            continue
        proc = subprocess.run(["sysctl", "-w", f"{key}=1"], capture_output=True, text=True)
        msg = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0:
            changed.append(f"ok {key}=1 {msg}".strip())
        else:
            changed.append(f"fail {key}=1 {msg}".strip())
    return changed


def set_ipv6_disabled(disabled: bool) -> list[str]:
    val = "1" if disabled else "0"
    lines = [f"{k}={val}" for k in PERSISTENT_KEYS]
    SYSCTL_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logs: list[str] = [f"wrote {SYSCTL_FILE}"]
    # `all/default/lo` are not enough on every kernel/runtime path: an already
    # configured interface can stay enabled while MCD reports all/default/lo as
    # disabled. Apply the same value to all currently visible interfaces too.
    for key in _runtime_ipv6_keys():
        proc = subprocess.run(["sysctl", "-w", f"{key}={val}"], capture_output=True, text=True)
        msg = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0:
            logs.append(f"ok {key}={val} {msg}".strip())
        else:
            logs.append(f"fail {key}={val} {msg}".strip())
    return logs


def default_policy() -> dict[str, Any]:
    cf_cidrs = [
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    ]
    return {
        "version": 1,
        "scope": "host",
        "meta": {
            "name": "central-default",
            "description": "Plan-only baseline for apt/firewall/db/php/web",
        },
        "apt": {
            "enabled": False,
            "update_cache": True,
            "upgrade_mode": "none",
            "packages_present": [],
            "packages_absent": [],
        },
        "iptables": {
            "enabled": False,
            "default_input_policy": "ACCEPT",
            "allow_inbound_tcp_ports": [22, 80, 443],
            "allow_ipv4_sources": [],
            "drop_ipv4_sources": [],
        },
        "database": {
            "enabled": False,
            "engine": "auto",
            "max_connections": None,
            "innodb_buffer_pool_size": None,
            "slow_query_log": None,
        },
        "php": {
            "enabled": False,
            "fpm_service": "auto",
            "pool": "www",
            "pm": "ondemand",
            "pm_max_children": None,
            "pm_max_requests": None,
            "memory_limit": None,
        },
        "web": {
            "enabled": False,
            "engine": "auto",
            "worker_connections": None,
            "keepalive_timeout": None,
            "gzip": None,
            "cloudflare_real_ip": {
                "enabled": False,
                "target_file": "/etc/nginx/conf.d/default.conf",
                "real_ip_header": "CF-Connecting-IP",
                "set_real_ip_from": cf_cidrs,
                "reload_command": "systemctl restart nginx",
            },
        },
    }


def parse_policy_text(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        return default_policy()
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("policy payload must be a JSON object")
    out = default_policy()
    for key in ("meta", "apt", "iptables", "database", "php", "web"):
        val = raw.get(key)
        if isinstance(val, dict):
            cur = out.get(key)
            if isinstance(cur, dict):
                cur.update(val)
            else:
                out[key] = val
    web_in = raw.get("web")
    if isinstance(web_in, dict):
        cf_in = web_in.get("cloudflare_real_ip")
        web_cur = out.get("web")
        if isinstance(cf_in, dict) and isinstance(web_cur, dict):
            cf_cur = web_cur.get("cloudflare_real_ip")
            if isinstance(cf_cur, dict):
                cf_cur.update(cf_in)
    if isinstance(raw.get("version"), int):
        out["version"] = int(raw["version"])
    return out


def _service_active(name: str) -> bool:
    proc = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return proc.returncode == 0 and (proc.stdout or "").strip() == "active"


def collect_host_facts() -> dict[str, Any]:
    php_ver = None
    if Path("/etc/php").exists():
        versions = sorted([p.name for p in Path("/etc/php").iterdir() if p.is_dir()])
        if versions:
            php_ver = versions[-1]
    web_engine = "none"
    if _service_active("nginx"):
        web_engine = "nginx"
    elif _service_active("apache2"):
        web_engine = "apache"
    db_engine = "none"
    if _service_active("mariadb"):
        db_engine = "mariadb"
    elif _service_active("mysql"):
        db_engine = "mysql"
    return {
        "has_apt": Path("/usr/bin/apt-get").exists(),
        "php_version": php_ver,
        "web_engine": web_engine,
        "db_engine": db_engine,
        "ufw_active": _service_active("ufw"),
        "has_iptables": Path("/usr/sbin/iptables").exists() or Path("/sbin/iptables").exists(),
    }


def build_policy_plan(policy: dict[str, Any], component: str = "all") -> list[str]:
    comp = (component or "all").strip().lower()
    facts = collect_host_facts()
    lines: list[str] = []
    lines.append("PLAN ONLY: no changes are applied")
    lines.append(
        "Host facts: "
        f"apt={facts['has_apt']} web={facts['web_engine']} db={facts['db_engine']} "
        f"php={facts['php_version'] or '-'} ufw={facts['ufw_active']}"
    )

    def enabled(name: str) -> bool:
        sec = policy.get(name)
        return isinstance(sec, dict) and bool(sec.get("enabled"))

    if comp in {"all", "apt"}:
        if enabled("apt"):
            apt = policy["apt"]
            lines.append("[apt] would run cache update: " + str(bool(apt.get("update_cache", True))).lower())
            lines.append("[apt] would run upgrade mode: " + str(apt.get("upgrade_mode", "none")))
            lines.append("[apt] would ensure packages present: " + ", ".join(map(str, apt.get("packages_present", []))) or "-")
            lines.append("[apt] would ensure packages absent: " + ", ".join(map(str, apt.get("packages_absent", []))) or "-")
        else:
            lines.append("[apt] disabled")

    if comp in {"all", "iptables"}:
        if enabled("iptables"):
            fw = policy["iptables"]
            lines.append("[iptables] would set INPUT policy: " + str(fw.get("default_input_policy", "ACCEPT")))
            lines.append("[iptables] would allow TCP ports: " + ", ".join(map(str, fw.get("allow_inbound_tcp_ports", []))) or "-")
            lines.append("[iptables] would allow IPv4 sources: " + ", ".join(map(str, fw.get("allow_ipv4_sources", []))) or "-")
            lines.append("[iptables] would drop IPv4 sources: " + ", ".join(map(str, fw.get("drop_ipv4_sources", []))) or "-")
        else:
            lines.append("[iptables] disabled")

    if comp in {"all", "database"}:
        if enabled("database"):
            db = policy["database"]
            lines.append("[database] engine policy: " + str(db.get("engine", "auto")))
            lines.append("[database] would set max_connections: " + str(db.get("max_connections")))
            lines.append("[database] would set innodb_buffer_pool_size: " + str(db.get("innodb_buffer_pool_size")))
            lines.append("[database] would set slow_query_log: " + str(db.get("slow_query_log")))
        else:
            lines.append("[database] disabled")

    if comp in {"all", "php"}:
        if enabled("php"):
            php = policy["php"]
            lines.append("[php] pool=" + str(php.get("pool", "www")) + " pm=" + str(php.get("pm", "ondemand")))
            lines.append("[php] would set pm.max_children: " + str(php.get("pm_max_children")))
            lines.append("[php] would set pm.max_requests: " + str(php.get("pm_max_requests")))
            lines.append("[php] would set memory_limit: " + str(php.get("memory_limit")))
        else:
            lines.append("[php] disabled")

    if comp in {"all", "web", "web_cf_real_ip"}:
        if enabled("web"):
            web = policy["web"]
            if comp in {"all", "web"}:
                lines.append("[web] engine policy: " + str(web.get("engine", "auto")))
                lines.append("[web] would set worker_connections: " + str(web.get("worker_connections")))
                lines.append("[web] would set keepalive_timeout: " + str(web.get("keepalive_timeout")))
                lines.append("[web] would set gzip: " + str(web.get("gzip")))
            cf = web.get("cloudflare_real_ip") if isinstance(web, dict) else None
            if isinstance(cf, dict):
                if bool(cf.get("enabled", False)):
                    cidrs = cf.get("set_real_ip_from") if isinstance(cf.get("set_real_ip_from"), list) else []
                    lines.append("[web.cloudflare_real_ip] enabled")
                    lines.append("[web.cloudflare_real_ip] target_file: " + str(cf.get("target_file", "/etc/nginx/conf.d/default.conf")))
                    lines.append("[web.cloudflare_real_ip] real_ip_header: " + str(cf.get("real_ip_header", "CF-Connecting-IP")))
                    lines.append("[web.cloudflare_real_ip] set_real_ip_from count: " + str(len(cidrs)))
                    lines.append("[web.cloudflare_real_ip] reload_command: " + str(cf.get("reload_command", "systemctl restart nginx")))
                else:
                    lines.append("[web.cloudflare_real_ip] disabled")
        else:
            lines.append("[web] disabled")

    return lines
