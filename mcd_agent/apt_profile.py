from __future__ import annotations

import configparser
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib import request as urlrequest

from mcd_agent.nginx_baseline import (
    cloudflare_real_ip_profile_present,
    cloudflare_real_ip_state,
    ensure_cloudflare_real_ip,
    ensure_nginx_baseline,
    nginx_baseline_satisfied,
)


def _run(cmd: list[str], *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_sec)),
        env={**os.environ, **env},
    )


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int, min_v: int = 0, max_v: int = 86400) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out < min_v:
        out = min_v
    if out > max_v:
        out = max_v
    return out


def _primary_ipv4(timeout_sec: int = 5) -> str:
    proc = _run(["sh", "-c", "hostname -I 2>/dev/null | tr ' ' '\\n' | grep -E '^[0-9]+(\\.[0-9]+){3}$' | grep -v '^127\\.' | head -n1"], timeout_sec=timeout_sec)
    value = (proc.stdout or "").strip().splitlines()
    if value:
        return value[0].strip()
    proc2 = _run(["sh", "-c", "ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i==\"src\") {print $(i+1); exit}}'"], timeout_sec=timeout_sec)
    value2 = (proc2.stdout or "").strip().splitlines()
    return value2[0].strip() if value2 else ""


def _desired_hosts_with_local_fqdn(text: str, *, ip: str, hostname: str, suffix: str = "localdomain") -> tuple[str, str]:
    short = str(hostname or "").strip().split(".", 1)[0]
    clean_suffix = re.sub(r"[^A-Za-z0-9.-]+", "", str(suffix or "localdomain").strip()).strip(".") or "localdomain"
    if not short or not ip or "." in str(hostname or "").strip():
        return text, ""
    fqdn = f"{short}.{clean_suffix}"
    desired = f"{ip} {fqdn} {short}"
    lines = str(text or "").splitlines()
    out: list[str] = []
    done = False
    changed = False
    for raw in lines:
        parts = raw.split()
        if not done and parts and parts[0] == ip:
            out.append(desired)
            done = True
            if raw.strip() != desired:
                changed = True
            continue
        if short in parts[1:] or fqdn in parts[1:]:
            changed = True
            continue
        out.append(raw)
    if not done:
        out.append(desired)
        changed = True
    if not changed:
        return text, ""
    return "\n".join(out).rstrip("\n") + "\n", fqdn


def ensure_local_fqdn_hosts_entry(*, suffix: str = "localdomain") -> tuple[bool, str]:
    proc = _run(["hostname"], timeout_sec=5)
    hostname = (proc.stdout or "").strip().splitlines()[0].strip() if (proc.stdout or "").strip() else ""
    if not hostname or "." in hostname:
        return False, "hosts_fqdn:skipped"
    ip = _primary_ipv4(timeout_sec=5)
    if not ip:
        return False, "hosts_fqdn:no_primary_ipv4"
    hosts_path = Path("/etc/hosts")
    try:
        current = hosts_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return False, f"hosts_fqdn:read_failed:{e}"
    desired, fqdn = _desired_hosts_with_local_fqdn(current, ip=ip, hostname=hostname, suffix=suffix)
    if not fqdn:
        return False, "hosts_fqdn:already_ok"
    try:
        backup = hosts_path.with_name(f"hosts.mcd-pre-fqdn-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        shutil.copy2(hosts_path, backup)
        hosts_path.write_text(desired, encoding="utf-8")
        hosts_path.chmod(0o644)
    except Exception as e:
        return False, f"hosts_fqdn:write_failed:{e}"
    return True, f"hosts_fqdn:{fqdn}"


def _services_for_present_packages(packages: list[str]) -> list[str]:
    mapping: dict[str, list[str]] = {
        "nginx": ["nginx"],
        "redis": ["redis-server"],
        "redis-server": ["redis-server"],
        "sendmail": ["sendmail"],
        "mariadb-server": ["mariadb"],
        "mariadb-client": [],
        "mysql-server": ["mysql"],
        "percona-server-server": ["mysql"],
        "percona-xtradb-cluster-server": ["mysql"],
        "zabbix-agent2": ["zabbix-agent2"],
    }
    out: list[str] = []
    for raw in packages:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name.startswith("php") and name.endswith("-fpm"):
            out.append(name)
            continue
        if name.startswith("php") and re.match(r"^php\d+\.\d+-fpm$", name):
            out.append(name)
            continue
        out.extend(mapping.get(name, []))
    dedup: list[str] = []
    seen: set[str] = set()
    for svc in out:
        if svc in seen:
            continue
        seen.add(svc)
        dedup.append(svc)
    return dedup


def _enable_start_services(services: list[str], *, timeout_sec: int = 45) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    for raw in services:
        service = str(raw or "").strip()
        if not service:
            continue
        p = _run(["systemctl", "enable", "--now", service], timeout_sec=timeout_sec)
        if p.returncode == 0:
            actions.append(f"service_started:{service}")
            continue
        msg = (p.stderr or p.stdout or "").strip()
        if "does not exist" in msg or "not found" in msg or "not be found" in msg:
            actions.append(f"service_missing:{service}")
            continue
        errors.append(f"service_start_failed:{service}:{msg or p.returncode}")
    return actions, errors


def _unit_exists(service: str, *, timeout_sec: int = 8) -> bool:
    p = _run(["systemctl", "list-unit-files", f"{service}.service", "--no-legend"], timeout_sec=timeout_sec)
    return p.returncode == 0 and f"{service}.service" in str(p.stdout or "")


def _php_fpm_services(*, timeout_sec: int = 8) -> list[str]:
    p = _run(
        ["systemctl", "list-unit-files", "php*-fpm.service", "--type=service", "--no-legend"],
        timeout_sec=timeout_sec,
    )
    if p.returncode != 0:
        return []
    out: list[str] = []
    for raw in (p.stdout or "").splitlines():
        name = raw.strip().split(None, 1)[0].strip()
        if name.endswith(".service"):
            out.append(name.removesuffix(".service"))
    return sorted(set(out))


def _service_enable_and_start(service: str, *, timeout_sec: int = 45) -> tuple[bool, list[str]]:
    actions: list[str] = []
    enabled = _run(["systemctl", "is-enabled", service], timeout_sec=timeout_sec)
    enabled_state = (enabled.stdout or enabled.stderr or "").strip()
    allowed_enabled_states = {"enabled", "enabled-runtime", "static", "alias", "indirect", "generated"}
    if enabled.returncode != 0 or enabled_state not in allowed_enabled_states:
        en = _run(["systemctl", "enable", service], timeout_sec=timeout_sec)
        if en.returncode != 0:
            msg = (en.stderr or en.stdout or "").strip()
            return False, [f"service_postcheck_enable_failed:{service}:{msg or en.returncode}"]
        actions.append(f"service_postcheck_enabled:{service}")

    active = _run(["systemctl", "is-active", service], timeout_sec=timeout_sec)
    active_state = (active.stdout or active.stderr or "").strip()
    if active.returncode == 0 and active_state == "active":
        actions.append(f"service_postcheck_active:{service}")
        return True, actions

    st = _run(["systemctl", "start", service], timeout_sec=timeout_sec)
    if st.returncode != 0:
        msg = (st.stderr or st.stdout or "").strip()
        return False, [*actions, f"service_postcheck_start_failed:{service}:{msg or st.returncode}"]
    active = _run(["systemctl", "is-active", service], timeout_sec=timeout_sec)
    active_state = (active.stdout or active.stderr or "").strip()
    if active.returncode != 0 or active_state != "active":
        return False, [*actions, f"service_postcheck_not_active:{service}:{active_state or 'inactive'}"]
    actions.append(f"service_postcheck_started:{service}")
    return True, actions


def _apt_upgrade_service_postcheck(*, timeout_sec: int = 90) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []

    if _unit_exists("nginx", timeout_sec=timeout_sec):
        ok, service_actions = _service_enable_and_start("nginx", timeout_sec=timeout_sec)
        actions.extend(service_actions)
        if not ok:
            errors.extend(service_actions)
        else:
            baseline = ensure_nginx_baseline(reload_service=True)
            baseline_actions = baseline.get("actions") if isinstance(baseline, dict) else []
            if isinstance(baseline_actions, list):
                actions.extend(f"nginx_baseline:{str(x)}" for x in baseline_actions)
            if str(baseline.get("status", "") if isinstance(baseline, dict) else "").lower() == "error":
                errors.append(f"nginx_baseline_failed:{baseline.get('error', 'unknown error')}")
            test = _run(["nginx", "-t"], timeout_sec=timeout_sec)
            if test.returncode == 0:
                actions.append("nginx_config_test:ok")
            else:
                msg = (test.stderr or test.stdout or "").strip()
                errors.append(f"nginx_config_test_failed:{msg or test.returncode}")
    else:
        actions.append("service_postcheck_missing:nginx")

    db_service = "mariadb" if _unit_exists("mariadb", timeout_sec=timeout_sec) else ("mysql" if _unit_exists("mysql", timeout_sec=timeout_sec) else "")
    if db_service:
        ok, service_actions = _service_enable_and_start(db_service, timeout_sec=timeout_sec)
        actions.extend(service_actions)
        if not ok:
            errors.extend(service_actions)
    else:
        actions.append("service_postcheck_missing:mysql")

    if _unit_exists("cron", timeout_sec=timeout_sec):
        ok, service_actions = _service_enable_and_start("cron", timeout_sec=timeout_sec)
        actions.extend(service_actions)
        if not ok:
            errors.extend(service_actions)
    else:
        actions.append("service_postcheck_missing:cron")

    php_services = _php_fpm_services(timeout_sec=timeout_sec)
    if php_services:
        php_ok = False
        for service in php_services:
            ok, service_actions = _service_enable_and_start(service, timeout_sec=timeout_sec)
            actions.extend(service_actions)
            if ok:
                php_ok = True
            else:
                errors.extend(service_actions)
        if not php_ok:
            errors.append("php_fpm_postcheck_no_active_service")
    else:
        actions.append("service_postcheck_missing:php-fpm")

    return actions, errors


def _version_tuple(raw: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", str(raw or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return int(nums[0]), int(nums[1]), int(nums[2])


def _cmd_first_line(cmd: list[str], *, timeout_sec: int = 10) -> tuple[int, str]:
    p = _run(cmd, timeout_sec=timeout_sec)
    line = (p.stdout or p.stderr or "").strip().splitlines()
    return int(p.returncode), line[0].strip() if line else ""


def _nodejs20_satisfied() -> tuple[bool, str]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        return False, "node_or_npm_missing"
    rc, line = _cmd_first_line([node, "--version"], timeout_sec=8)
    if rc != 0:
        return False, "node_version_failed"
    if _version_tuple(line) < (20, 0, 0):
        return False, f"node_too_old:{line or '-'}"
    npm_rc, npm_line = _cmd_first_line([npm, "--version"], timeout_sec=8)
    if npm_rc != 0:
        return False, "npm_version_failed"
    return True, f"nodejs20:{line or '-'} npm:{npm_line or '-'}"


def _ensure_nodejs20(*, timeout_sec: int) -> tuple[bool, list[str], list[str]]:
    ok, reason = _nodejs20_satisfied()
    if ok:
        return False, ["nodejs20:already_ok:" + reason], []
    actions = [f"nodejs20:prepare:{reason}"]
    errors: list[str] = []
    script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get purge -y nodejs libnode-dev nodejs-doc npm >/dev/null 2>&1 || true; "
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; "
        "apt-get install -y nodejs"
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(60, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode != 0:
        errors.append((p.stderr or p.stdout or "nodejs20 install failed").strip())
        return False, actions, errors
    ok_after, reason_after = _nodejs20_satisfied()
    if not ok_after:
        errors.append(f"nodejs20 validation failed:{reason_after}")
        return False, actions, errors
    actions.append("nodejs20:installed:" + reason_after)
    return True, actions, []


def _composer_global_satisfied() -> tuple[bool, str]:
    composer = shutil.which("composer")
    if not composer:
        return False, "composer_missing"
    rc, line = _cmd_first_line([composer, "--version"], timeout_sec=10)
    if rc != 0:
        return False, "composer_version_failed"
    if Path(composer) != Path("/usr/local/bin/composer"):
        return False, f"composer_not_global:{composer}"
    www_data = shutil.which("runuser")
    if www_data:
        p = _run(["runuser", "-u", "www-data", "--", composer, "--version"], timeout_sec=20)
        if p.returncode != 0:
            return False, "composer_www_data_check_failed"
    return True, f"{composer}:{line or '-'}"


def _ensure_composer_global(*, timeout_sec: int) -> tuple[bool, list[str], list[str]]:
    ok, reason = _composer_global_satisfied()
    if ok:
        return False, ["composer_global:already_ok:" + reason], []
    if not shutil.which("php"):
        return False, ["composer_global:prepare:" + reason], ["composer_global:php_cli_missing"]
    actions = [f"composer_global:prepare:{reason}"]
    errors: list[str] = []
    try:
        with urlrequest.urlopen("https://composer.github.io/installer.sig", timeout=20) as resp:
            expected_sig = resp.read().decode("ascii", errors="ignore").strip()
        with urlrequest.urlopen("https://getcomposer.org/installer", timeout=45) as resp:
            installer = resp.read()
    except Exception as e:
        return False, actions, [f"composer_global:download_failed:{e}"]
    got_sig = hashlib.sha384(installer).hexdigest()
    if not expected_sig or got_sig.lower() != expected_sig.lower():
        return False, actions, ["composer_global:installer_signature_mismatch"]
    with tempfile.NamedTemporaryFile(prefix="composer-setup-", suffix=".php", delete=False) as tmp:
        tmp.write(installer)
        setup_path = Path(tmp.name)
    try:
        p = _run(["php", str(setup_path), "--install-dir=/usr/local/bin", "--filename=composer"], timeout_sec=max(45, int(timeout_sec)))
        if p.returncode != 0:
            errors.append((p.stderr or p.stdout or "composer install failed").strip())
            return False, actions, errors
        Path("/usr/local/bin/composer").chmod(0o755)
    finally:
        try:
            setup_path.unlink(missing_ok=True)
        except Exception:
            pass
    ok_after, reason_after = _composer_global_satisfied()
    if not ok_after:
        errors.append(f"composer_global validation failed:{reason_after}")
        return False, actions, errors
    actions.append("composer_global:installed:" + reason_after)
    return True, actions, []


def _ensure_var_www() -> tuple[bool, str, list[str]]:
    path = Path("/var/www")
    changed = False
    errors: list[str] = []
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            changed = True
        if not path.is_dir():
            return changed, "var_www:not_directory", ["var_www:not_directory"]
        try:
            import grp
            import pwd

            uid = pwd.getpwnam("www-data").pw_uid
            gid = grp.getgrnam("www-data").gr_gid
            st = path.stat()
            if st.st_uid != uid or st.st_gid != gid:
                os.chown(path, uid, gid)
                changed = True
        except Exception as e:
            errors.append(f"var_www:chown_failed:{e}")
        mode = path.stat().st_mode & 0o777
        if mode != 0o755:
            path.chmod(0o755)
            changed = True
    except Exception as e:
        return changed, f"var_www:failed:{e}", [f"var_www:failed:{e}"]
    if errors:
        return changed, "var_www:warning", errors
    return changed, "var_www:ok", []


def _normalize_source_line(line: str) -> str:
    x = line.split("#", 1)[0].strip()
    x = re.sub(r"\s+", " ", x)
    return x


def _collect_list_source_lines() -> dict[str, list[str]]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    out: dict[str, list[str]] = {}
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        lines: list[str] = []
        try:
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                n = _normalize_source_line(raw)
                if not n:
                    continue
                if n.startswith("deb "):
                    lines.append(n)
        except Exception:
            continue
        if lines:
            out[str(p)] = lines
    return out


def detect_duplicate_list_sources() -> dict[str, Any]:
    per_file = _collect_list_source_lines()
    counts: dict[str, int] = {}
    for lines in per_file.values():
        for line in lines:
            counts[line] = counts.get(line, 0) + 1
    duplicates = {k: v for k, v in counts.items() if v > 1}
    return {
        "count": int(sum(v - 1 for v in duplicates.values())),
        "items": [{"source": k, "count": v} for k, v in sorted(duplicates.items())],
    }


def dedupe_list_sources() -> dict[str, Any]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    seen: set[str] = set()
    changed_files: list[str] = []
    removed = 0
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        try:
            original = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        new_lines: list[str] = []
        file_changed = False
        for line in original:
            n = _normalize_source_line(line)
            if n.startswith("deb "):
                if n in seen:
                    removed += 1
                    file_changed = True
                    continue
                seen.add(n)
            new_lines.append(line)
        if file_changed:
            p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
            changed_files.append(str(p))
    return {"changed_files": changed_files, "removed_lines": int(removed)}


def _parse_upgradable_count(raw: str) -> int:
    count = 0
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" in x and "upgradable from:" in x:
            count += 1
    return count


def _parse_upgradable_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" not in x or "upgradable from:" not in x:
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("/", 1)[0].strip()
        candidate = str(parts[1]).strip()
        if not name or name in seen:
            continue
        m = re.search(r"\[upgradable from:\s*([^\]]+)\]", x)
        current = m.group(1).strip() if m else ""
        out.append({"name": name, "current": current, "candidate": candidate})
        seen.add(name)
    return out


def _parse_sim_upgrade_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x.startswith("Inst "):
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = str(parts[1]).strip()
        if not name or name in seen:
            continue
        out.append({"name": name, "current": "", "candidate": ""})
        seen.add(name)
    return out


def _parse_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    in_block = False
    for line in (raw or "").splitlines():
        x = line.strip()
        low = x.lower()
        if "deferred due to phasing" in low:
            in_block = True
            continue
        if not in_block:
            continue
        if not x:
            continue
        if re.match(r"^\d+\s+upgraded,\s+\d+\s+newly installed,", x):
            break
        if x.startswith("The following "):
            break
        for tok in x.split():
            name = tok.strip()
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*$", name):
                out.add(name)
    return out


def _parse_policy_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    current = ""
    for line in (raw or "").splitlines():
        x = line.rstrip()
        s = x.strip()
        if not s:
            continue
        # apt-cache policy block header: "<pkg>:"
        if not x.startswith((" ", "\t")) and s.endswith(":") and "/" not in s:
            current = s[:-1].strip()
            continue
        if not current:
            continue
        if "(phased " in s.lower():
            out.add(current)
    return out


def _policy_phasing_packages(packages: set[str], timeout_sec: int = 45) -> set[str]:
    names = sorted({str(x).strip() for x in packages if str(x).strip()})
    if not names:
        return set()
    out: set[str] = set()
    # Keep command line bounded for safety.
    chunk = 50
    for i in range(0, len(names), chunk):
        part = names[i : i + chunk]
        try:
            p = _run(["apt-cache", "policy", *part], timeout_sec=timeout_sec)
            merged = f"{p.stdout or ''}\n{p.stderr or ''}"
            out.update(_parse_policy_phasing_packages(merged))
        except Exception:
            continue
    return out


def _pending_updates(timeout_sec: int = 45) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    upgradable: list[dict[str, str]] = []
    try:
        p = _run(["apt", "list", "--upgradable"], timeout_sec=timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        cnt = _parse_upgradable_count(merged)
        upgradable = _parse_upgradable_packages(merged)
        if p.returncode not in (0,):
            errors.append(f"apt_list_upgradable_rc_{p.returncode}")
            return (
                {
                    "pending_total": int(cnt),
                    "pending_regular": int(cnt),
                    "pending_phasing": 0,
                    "pending_hold": 0,
                    "pending_updates": int(cnt),
                    "upgradable_packages": upgradable,
                    "phasing_packages": [],
                    "held_packages": [],
                },
                errors,
            )
    except Exception as e:
        errors.append(f"apt_list_upgradable_exception:{e}")

    sim_stdout = ""
    try:
        p2 = _run(["apt-get", "-s", "upgrade"], timeout_sec=timeout_sec)
        sim_stdout = p2.stdout or ""
        if not upgradable:
            upgradable = _parse_sim_upgrade_packages(sim_stdout)
        cnt = len(upgradable)
        if p2.returncode not in (0, 100):
            errors.append(f"apt_get_sim_upgrade_rc_{p2.returncode}")
    except Exception as e:
        errors.append(f"apt_get_sim_upgrade_exception:{e}")
    hold_set: set[str] = set()
    try:
        p3 = _run(["apt-mark", "showhold"], timeout_sec=timeout_sec)
        if p3.returncode == 0:
            hold_set = {x.strip() for x in (p3.stdout or "").splitlines() if x.strip()}
    except Exception:
        hold_set = set()
    phasing_set = _parse_phasing_packages(sim_stdout)
    upgradable_names = {str(row.get("name", "")).strip() for row in upgradable if str(row.get("name", "")).strip()}
    # Ubuntu phased updates often appear as "kept back" without explicit phasing section.
    # Fallback to apt-cache policy phased marker, e.g. "(phased 60%)".
    if upgradable_names:
        policy_phasing = _policy_phasing_packages(upgradable_names, timeout_sec=max(10, int(timeout_sec)))
        phasing_set.update(policy_phasing)
    phasing_set = {x for x in phasing_set if x in upgradable_names}
    hold_set = {x for x in hold_set if x in upgradable_names}

    pending_regular = 0
    pending_pack_rows: list[dict[str, str]] = []
    for row in upgradable:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        tags: list[str] = []
        if name in phasing_set:
            tags.append("phasing")
        if name in hold_set:
            tags.append("hold")
        if not tags:
            pending_regular += 1
        pending_pack_rows.append(
            {
                "name": name,
                "current": str(row.get("current", "")).strip(),
                "candidate": str(row.get("candidate", "")).strip(),
                "state": "+".join(tags) if tags else "regular",
            }
        )

    payload = {
        "pending_total": int(len(pending_pack_rows)),
        "pending_regular": int(pending_regular),
        "pending_phasing": int(len(phasing_set)),
        "pending_hold": int(len(hold_set)),
        # Backward-compatible field consumed by MCC and older clients.
        "pending_updates": int(pending_regular),
        "upgradable_packages": pending_pack_rows[:200],
        "phasing_packages": sorted(phasing_set),
        "held_packages": sorted(hold_set),
    }
    return payload, errors


def _apt_update_errors(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.startswith("E:"):
            out.append(x)
        elif "NO_PUBKEY" in x or "EXPKEYSIG" in x or "not signed" in x.lower():
            out.append(x)
    dedup: list[str] = []
    seen: set[str] = set()
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        dedup.append(x)
    return dedup[:20]


def _has_ppa_marker(marker: str) -> bool:
    marker_l = marker.strip().lower()
    if not marker_l:
        return False
    for p in _active_source_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if marker_l in txt:
            return True
    return False


def _ensure_ppa(ppa: str, *, timeout_sec: int) -> tuple[bool, str]:
    add_repo = Path("/usr/bin/add-apt-repository")
    if not add_repo.exists():
        return False, "add-apt-repository not found"
    cmd = [str(add_repo), "-y", f"ppa:{ppa}"]
    p = _run(cmd, timeout_sec=timeout_sec)
    if p.returncode == 0:
        return True, f"added ppa:{ppa}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"add ppa:{ppa} failed"


_NGINX_OFFICIAL_KEY_URL = "https://nginx.org/keys/nginx_signing.key"
_NGINX_OFFICIAL_KEYRING = Path("/usr/share/keyrings/nginx-archive-keyring.gpg")
_NGINX_OFFICIAL_SOURCE = Path("/etc/apt/sources.list.d/nginx.list")
_NGINX_OFFICIAL_FINGERPRINT = "573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62"
_ZABBIX_AGENT_DROPIN = Path("/etc/zabbix/zabbix_agent2.d/99-mcd-server.conf")
_ZABBIX_AGENT_BASE_CONF = Path("/etc/zabbix/zabbix_agent2.conf")
_ZABBIX_DISK_HEALTH_SCRIPT = Path("/usr/local/bin/zbx_smart_health_worst.sh")
_ZABBIX_DISK_HEALTH_DROPIN = Path("/etc/zabbix/zabbix_agent2.d/disk_health.conf")
_ZABBIX_DISK_HEALTH_SUDOERS = Path("/etc/sudoers.d/zabbix-smart")
_ZABBIX_DISK_HEALTH_SCRIPT_TEXT = r'''#!/usr/bin/env bash
# Output: one integer, 1..5, for the worst physical disk or md array.
# 5=GOOD, 4=OK, 3=ATTENTION, 2=BAD, 1=ALARM.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

score_min=5
score_min_func() {
  local cur="$1"
  if [ "$cur" -lt "$score_min" ]; then score_min="$cur"; fi
}

score_mdraid() {
  local s=5
  if [ -r /proc/mdstat ]; then
    if awk '
      /\[[0-9]+\/[0-9]+\]/ {
        match($0, /\[([0-9]+)\/([0-9]+)\]/, a);
        if (a[1] != a[2]) { print "DEG"; exit }
      }
      /\[[U_]+\]/ { if ($0 ~ /_/) { print "DEG"; exit } }
    ' /proc/mdstat | grep -q DEG; then
      s=1
    fi
  fi
  echo "$s"
}

score_nvme_temperature() {
  local temperature="$1" warning="$2" critical="$3" s=5
  [[ "$temperature" =~ ^[0-9]+$ ]] || { echo "$s"; return; }

  # WCTEMP/CCTEMP come from the NVMe controller and are expressed in Kelvin.
  # Use those model-specific limits instead of a generic NVMe temperature
  # ladder. Older controllers without limits retain the conservative fallback.
  if ! [[ "$warning" =~ ^[0-9]+$ ]] || [ "$warning" -le 273 ]; then warning=343; fi
  if ! [[ "$critical" =~ ^[0-9]+$ ]] || [ "$critical" -le "$warning" ]; then critical=$((warning + 10)); fi
  warning=$((warning - 273))
  critical=$((critical - 273))
  local attention=$((warning - 10)) elevated=$((warning - 20))
  [ "$elevated" -lt 50 ] && elevated=50

  if [ "$temperature" -ge "$critical" ]; then s=1
  elif [ "$temperature" -ge "$warning" ]; then s=2
  elif [ "$temperature" -ge "$attention" ]; then s=3
  elif [ "$temperature" -ge "$elevated" ]; then s=4; fi
  echo "$s"
}

score_nvme() {
  local dev="$1" s=5 out identity
  if ! out=$(sudo nvme smart-log "$dev" 2>/dev/null); then echo; return; fi
  local cw me pu t
  cw=$(echo "$out" | awk -F: '/critical_warning/{gsub(/ /,"",$2); print $2; exit}' || echo 0)
  [ -z "$cw" ] && cw=0
  if [ "$cw" != "0" ]; then s=1; fi
  me=$(echo "$out" | awk -F: '/media_errors/{gsub(/ /,"",$2); print $2; exit}' || echo 0)
  [ -z "$me" ] && me=0
  if [ "$me" -gt 0 ] && [ "$s" -gt 2 ]; then s=2; fi
  pu=$(echo "$out" | awk -F: '/percentage_used/{gsub(/ /,"",$2); print $2; exit}' || true)
  if [[ "${pu:-}" =~ ^[0-9]+$ ]]; then
    if [ "$pu" -ge 95 ] && [ "$s" -gt 2 ]; then s=2
    elif [ "$pu" -ge 80 ] && [ "$s" -gt 3 ]; then s=3
    elif [ "$pu" -ge 60 ] && [ "$s" -gt 4 ]; then s=4; fi
  fi
  t=$(echo "$out" | awk -F'[ :]' '/^temperature/{for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/){print $i; exit}}' || echo '')
  if [[ "$t" =~ ^[0-9]+$ ]]; then
    identity=$(sudo nvme id-ctrl "$dev" 2>/dev/null || true)
    local wt ct ts
    wt=$(echo "$identity" | awk -F: '/^wctemp/{gsub(/ /,"",$2); print $2; exit}' || true)
    ct=$(echo "$identity" | awk -F: '/^cctemp/{gsub(/ /,"",$2); print $2; exit}' || true)
    ts=$(score_nvme_temperature "$t" "$wt" "$ct")
    if [[ "$ts" =~ ^[1-5]$ ]] && [ "$ts" -lt "$s" ]; then s="$ts"; fi
  fi
  echo "$s"
}

score_sata() {
  local dev="$1" s=5 H A
  H=$(sudo smartctl -H "$dev" 2>/dev/null || true)
  A=$(sudo smartctl -A "$dev" 2>/dev/null || true)
  echo "$H" | grep -qi 'SMART overall-health self-assessment test result: PASSED' || s=1
  get_raw() { echo "$A" | awk -v id="$1" '$1==id {print $10; exit}'; }
  local realloc pending uncor crc temp
  realloc=$(get_raw 5); realloc=${realloc:-0}
  pending=$(get_raw 197); pending=${pending:-0}
  uncor=$(get_raw 198); uncor=${uncor:-0}
  crc=$(get_raw 199); crc=${crc:-0}
  if [ "$pending" -gt 0 ] || [ "$uncor" -gt 0 ]; then s=1; fi
  if [ "$realloc" -gt 0 ] && [ "$s" -gt 2 ]; then s=2; fi
  if [ "$crc" -gt 0 ] && [ "$s" -gt 3 ]; then s=3; fi
  temp=$(get_raw 194)
  if [[ "$temp" =~ ^[0-9]+$ ]]; then
    if [ "$temp" -ge 80 ]; then s=1
    elif [ "$temp" -ge 70 ] && [ "$s" -gt 2 ]; then s=2
    elif [ "$temp" -ge 60 ] && [ "$s" -gt 3 ]; then s=3
    elif [ "$temp" -ge 50 ] && [ "$s" -gt 4 ]; then s=4; fi
  fi
  echo "$s"
}

md_sc=$(score_mdraid || echo 5)
[[ "$md_sc" =~ ^[1-5]$ ]] && score_min_func "$md_sc"
for d in $(ls /dev/nvme*n1 2>/dev/null || true); do
  sc=$(score_nvme "$d" || echo 5)
  [[ "$sc" =~ ^[1-5]$ ]] && score_min_func "$sc"
done
for d in $(ls /dev/sd[a-z] 2>/dev/null || true); do
  sc=$(score_sata "$d" || echo 5)
  [[ "$sc" =~ ^[1-5]$ ]] && score_min_func "$sc"
done
echo "${score_min:-5}"
'''
_ZABBIX_DISK_HEALTH_DROPIN_TEXT = "UserParameter=smart.health.worst,/usr/local/bin/zbx_smart_health_worst.sh\n"
_ZABBIX_DISK_HEALTH_SUDOERS_TEXT = """Defaults:zabbix !requiretty
Cmnd_Alias ZBX_SMART = /usr/sbin/smartctl *, /usr/sbin/nvme *, /usr/bin/nvme *
zabbix ALL=(root) NOPASSWD: ZBX_SMART
"""


def _ubuntu_codename() -> str:
    proc = _run(["lsb_release", "-cs"], timeout_sec=10) if shutil.which("lsb_release") else None
    if proc is not None and proc.returncode == 0:
        value = (proc.stdout or "").strip().splitlines()
        if value and re.match(r"^[a-z][a-z0-9._-]+$", value[0]):
            return value[0]
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore").splitlines():
            if raw.startswith("VERSION_CODENAME="):
                value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                if value and re.match(r"^[a-z][a-z0-9._-]+$", value):
                    return value
    except Exception:
        pass
    raise RuntimeError("ubuntu codename not detected")


def _os_release_value(key: str) -> str:
    wanted = str(key or "").strip()
    if not wanted:
        return ""
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore").splitlines():
            if not raw.startswith(f"{wanted}="):
                continue
            return raw.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def _ubuntu_version_id() -> str:
    return _os_release_value("VERSION_ID") or "24.04"


def _gpg_keyring_has_fingerprint(path: Path, fingerprint: str) -> bool:
    if not path.exists() or not shutil.which("gpg"):
        return False
    proc = _run(["gpg", "--show-keys", "--with-colons", "--fingerprint", str(path)], timeout_sec=20)
    if proc.returncode != 0:
        return False
    expected = re.sub(r"[^A-Fa-f0-9]", "", str(fingerprint or "")).upper()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9].strip().upper() == expected:
                return True
    return False


def _write_nginx_official_keyring(*, timeout_sec: int) -> tuple[bool, str]:
    if not shutil.which("gpg"):
        return False, "gpg not found; install gnupg2 before enabling nginx.org repository"
    try:
        with urlrequest.urlopen(_NGINX_OFFICIAL_KEY_URL, timeout=max(10, min(int(timeout_sec), 60))) as resp:
            key_bytes = resp.read()
    except Exception as e:
        return False, f"nginx key download failed:{e}"
    if not key_bytes:
        return False, "nginx key download returned empty body"
    _NGINX_OFFICIAL_KEYRING.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="nginx-signing-", suffix=".key", delete=False) as raw_tmp:
        raw_tmp.write(key_bytes)
        raw_tmp_path = Path(raw_tmp.name)
    with tempfile.NamedTemporaryFile(prefix="nginx-keyring-", suffix=".gpg", delete=False) as gpg_tmp:
        gpg_tmp_path = Path(gpg_tmp.name)
    try:
        proc = _run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(gpg_tmp_path), str(raw_tmp_path)], timeout_sec=30)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return False, msg or "nginx key dearmor failed"
        if not _gpg_keyring_has_fingerprint(gpg_tmp_path, _NGINX_OFFICIAL_FINGERPRINT):
            return False, "nginx key fingerprint mismatch"
        gpg_tmp_path.chmod(0o644)
        os.replace(str(gpg_tmp_path), str(_NGINX_OFFICIAL_KEYRING))
        return True, f"installed:{_NGINX_OFFICIAL_KEYRING}"
    finally:
        try:
            raw_tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            gpg_tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _nginx_official_repo_line(codename: str) -> str:
    return (
        "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] "
        f"https://nginx.org/packages/ubuntu {codename} nginx"
    )


def _nginx_official_repo_present() -> bool:
    txt = _all_sources_text_lower()
    return "nginx.org/packages/ubuntu" in txt and " nginx" in txt


def _source_has_ondrej_nginx_marker(text: str) -> bool:
    low = text.lower()
    return any(
        marker in low
        for marker in (
            "ondrej/nginx",
            "ppa.launchpadcontent.net/ondrej/nginx",
            "ppa.launchpad.net/ondrej/nginx",
        )
    )


def _disable_ondrej_nginx_sources() -> list[str]:
    changed: list[str] = []
    leftovers = sorted(Path("/etc/apt/sources.list.d").glob("*ondrej*nginx*.mcd-disabled-*"))
    for p in leftovers:
        try:
            p.unlink()
            changed.append(f"removed_leftover:{p}")
        except Exception:
            continue
    for p in _active_source_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not _source_has_ondrej_nginx_marker(text):
            continue
        if p.suffix == ".sources":
            try:
                p.unlink()
                changed.append(f"removed:{p}")
            except Exception:
                continue
            continue
        lines = text.splitlines()
        new_lines: list[str] = []
        touched = False
        for line in lines:
            if _source_has_ondrej_nginx_marker(line) and not line.lstrip().startswith("#"):
                new_lines.append("# disabled by mcd nginx_official_stable profile: " + line)
                touched = True
            else:
                new_lines.append(line)
        if touched:
            p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
            changed.append(str(p))
    return changed


def _active_ondrej_nginx_source_present() -> bool:
    for p in _active_source_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if _source_has_ondrej_nginx_marker(line):
                return True
    return False


def _ondrej_nginx_disabled_leftovers_present() -> bool:
    try:
        return any(Path("/etc/apt/sources.list.d").glob("*ondrej*nginx*.mcd-disabled-*"))
    except Exception:
        return False


def _nginx_official_profile_satisfied(*, remove_ondrej: bool) -> bool:
    if not _gpg_keyring_has_fingerprint(_NGINX_OFFICIAL_KEYRING, _NGINX_OFFICIAL_FINGERPRINT):
        return False
    if not _nginx_official_repo_present():
        return False
    if _ondrej_nginx_disabled_leftovers_present():
        return False
    if remove_ondrej and _active_ondrej_nginx_source_present():
        return False
    if not nginx_baseline_satisfied():
        return False
    return True


def _ensure_nginx_official_stable_repo(
    *,
    remove_ondrej: bool,
    timeout_sec: int,
) -> tuple[bool, str, list[str]]:
    actions: list[str] = []
    if remove_ondrej:
        disabled = _disable_ondrej_nginx_sources()
        if disabled:
            actions.append("disabled_ondrej_nginx_sources:" + ",".join(disabled))

    key_ok = _gpg_keyring_has_fingerprint(_NGINX_OFFICIAL_KEYRING, _NGINX_OFFICIAL_FINGERPRINT)
    if not key_ok:
        ok, msg = _write_nginx_official_keyring(timeout_sec=timeout_sec)
        if not ok:
            return False, msg, actions
        actions.append(msg)

    codename = _ubuntu_codename()
    desired = _nginx_official_repo_line(codename) + "\n"
    existing = ""
    try:
        if _NGINX_OFFICIAL_SOURCE.exists():
            existing = _NGINX_OFFICIAL_SOURCE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        existing = ""
    if existing != desired:
        _NGINX_OFFICIAL_SOURCE.parent.mkdir(parents=True, exist_ok=True)
        _NGINX_OFFICIAL_SOURCE.write_text(desired, encoding="utf-8")
        actions.append(f"wrote:{_NGINX_OFFICIAL_SOURCE}")

    if not _nginx_official_repo_present():
        return False, "nginx.org repository source not detected after write", actions
    if not actions:
        actions.append("nginx_official_stable_repo:already_present")
    return True, "nginx_official_stable_repo:ok", actions


def _run_mariadb_repo_setup(version: str, *, timeout_sec: int) -> tuple[bool, str]:
    v = str(version or "").strip() or "mariadb-11.4"
    script = (
        "set -euo pipefail; "
        "curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup "
        f"| bash -s -- --mariadb-server-version=\"{v}\""
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(30, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, f"mariadb_repo_setup:{v}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"mariadb_repo_setup:{v} failed"


def _run_percona_repo_setup(target: str, *, timeout_sec: int) -> tuple[bool, str]:
    ch = str(target or "").strip().lower()
    aliases = {
        "ps84": "ps-84-lts",
        "ps-84": "ps-84-lts",
        "ps84lts": "ps-84-lts",
        "pxc84": "pxc-84-lts",
        "pxc-84": "pxc-84-lts",
        "pxc84lts": "pxc-84-lts",
    }
    ch = aliases.get(ch, ch)
    if ch not in {"ps80", "pxc80", "ps-84-lts", "pxc-84-lts"}:
        return False, f"percona_repo_setup:unsupported_target:{ch or '-'}"
    script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "deb='/tmp/percona-release_latest.generic_all.deb'; "
        "if ! command -v percona-release >/dev/null 2>&1; then "
        "curl -fsSL -o \"$deb\" https://repo.percona.com/apt/percona-release_latest.generic_all.deb; "
        "dpkg -i \"$deb\" >/dev/null 2>&1 || apt-get install -y \"$deb\" >/dev/null 2>&1; "
        "fi; "
        "percona-release disable all >/dev/null 2>&1 || true; "
        f"percona-release setup -y {ch} --scheme https"
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(30, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, f"percona_repo_setup:{ch}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"percona_repo_setup:{ch} failed"


def _run_mysql84_repo_setup(*, timeout_sec: int) -> tuple[bool, str]:
    script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "if grep -Rqs 'repo.mysql.com' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then exit 0; fi; "
        "apt-get update -qq >/dev/null 2>&1 || true; "
        "apt-get install -y wget gnupg lsb-release >/dev/null 2>&1 || true; "
        "deb='/tmp/mysql-apt-config_0.8.33-1_all.deb'; "
        "wget -qO \"$deb\" https://dev.mysql.com/get/mysql-apt-config_0.8.33-1_all.deb; "
        "echo 'mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts' | debconf-set-selections || true; "
        "echo 'mysql-apt-config mysql-apt-config/select-product select Ok' | debconf-set-selections || true; "
        "dpkg -i \"$deb\" >/dev/null 2>&1 || apt-get install -y \"$deb\" >/dev/null 2>&1"
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(45, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, "mysql_repo_setup:8.4"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or "mysql_repo_setup:8.4 failed"


def _zabbix_agent_repo_present(version: str) -> bool:
    v = str(version or "").strip() or "7.0"
    txt = _all_sources_text_lower()
    return "repo.zabbix.com" in txt and f"/zabbix/{v}/" in txt


def _ensure_zabbix_agent_repo(version: str, *, timeout_sec: int) -> tuple[bool, str]:
    v = re.sub(r"[^0-9.]", "", str(version or "").strip()) or "7.0"
    ubuntu_version = _ubuntu_version_id()
    deb_name = f"zabbix-release_latest_{v}+ubuntu{ubuntu_version}_all.deb"
    url = f"https://repo.zabbix.com/zabbix/{v}/ubuntu/pool/main/z/zabbix-release/{deb_name}"
    with tempfile.NamedTemporaryFile(prefix="zabbix-release-", suffix=".deb", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            with urlrequest.urlopen(url, timeout=max(20, min(int(timeout_sec), 90))) as resp:
                tmp_path.write_bytes(resp.read())
        except Exception as e:
            return False, f"zabbix_repo_download_failed:{e}"
        if tmp_path.stat().st_size <= 0:
            return False, "zabbix_repo_download_empty"
        p = _run(["dpkg", "-i", str(tmp_path)], timeout_sec=max(30, int(timeout_sec)))
        if p.returncode != 0:
            p2 = _run(["apt-get", "install", "-y", str(tmp_path)], timeout_sec=max(30, int(timeout_sec)))
            if p2.returncode != 0:
                msg = (p2.stderr or p2.stdout or p.stderr or p.stdout or "").strip()
                return False, msg or "zabbix_repo_install_failed"
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
    if not _zabbix_agent_repo_present(v):
        return False, "zabbix_repo_source_not_detected_after_install"
    return True, f"zabbix_agent_repo:{v}"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zbx_marker_path(cfg: Any | None = None) -> Path:
    if cfg is not None:
        state_db = str(getattr(cfg, "state_db_path", "") or "").strip()
        if state_db:
            return Path(state_db).parent / "zabbix-mysql-bootstrap.json"
    return Path("/opt/mcd/var/zabbix-mysql-bootstrap.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _apt_repo_marker_path(cfg: Any | None = None) -> Path:
    if cfg is not None:
        state_db = str(getattr(cfg, "state_db_path", "") or "").strip()
        if state_db:
            return Path(state_db).parent / "apt-repo-profiles.json"
    return Path("/opt/mcd/var/apt-repo-profiles.json")


def collect_apt_repo_profiles_state(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _apt_repo_marker_path(cfg)
    marker = _read_json(marker_path)
    profiles = marker.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    return {
        "marker_path": str(marker_path),
        "updated_at_utc": str(marker.get("updated_at_utc", "") or ""),
        "profiles": profiles,
    }


def clear_apt_repo_profile_markers(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _apt_repo_marker_path(cfg)
    previous = _read_json(marker_path)
    prev_profiles = previous.get("profiles")
    prev_count = len(prev_profiles) if isinstance(prev_profiles, dict) else 0
    removed = False
    try:
        if marker_path.exists():
            marker_path.unlink()
            removed = True
    except Exception:
        removed = False
    return {
        "status": "ok",
        "removed": bool(removed),
        "profiles_count_before": int(prev_count),
        "marker_path": str(marker_path),
    }


def _active_source_files() -> list[Path]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.sources")))
    return [p for p in files if p.exists() and p.is_file()]


def _all_sources_text_lower() -> str:
    chunks: list[str] = []
    for p in _active_source_files():
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="ignore").lower())
        except Exception:
            continue
    return "\n".join(chunks)


def _has_source_markers(markers: list[str]) -> bool:
    checks = [str(x).strip().lower() for x in markers if str(x).strip()]
    if not checks:
        return False
    txt = _all_sources_text_lower()
    return any(x in txt for x in checks)


def _dpkg_installed_versions(*, timeout_sec: int = 20) -> dict[str, str]:
    proc = _run(
        ["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n"],
        timeout_sec=max(10, int(timeout_sec)),
    )
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        pkg = str(parts[0]).strip().lower()
        status = str(parts[1]).strip().lower()
        ver = str(parts[2]).strip()
        if not pkg or "install ok installed" not in status:
            continue
        out[pkg] = ver
    return out


def _version_mm(raw: str) -> str:
    m = re.search(r"(\d+)\.(\d+)", str(raw or ""))
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}"


def _detect_db_repo_target(*, timeout_sec: int = 20) -> dict[str, str]:
    installed = _dpkg_installed_versions(timeout_sec=timeout_sec)
    if not installed:
        return {
            "profile": "none",
            "action": "none",
            "reason": "dpkg_query_failed_or_empty",
        }

    # Priority order: PXC -> Percona Server -> MariaDB -> MySQL
    for pkg, ver in installed.items():
        if pkg.startswith("percona-xtradb-cluster-server"):
            mm = _version_mm(ver)
            if mm == "8.4":
                return {"profile": "percona_cluster_8_4", "action": "ensure", "reason": "detected_percona_cluster", "package": pkg, "version": ver}
            if mm == "8.0" or not mm:
                return {"profile": "percona_cluster_8_0", "action": "ensure", "reason": "detected_percona_cluster", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"unsupported_percona_cluster_{mm or 'unknown'}", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg.startswith("percona-server-server"):
            mm = _version_mm(ver)
            if mm == "8.4":
                return {"profile": "percona_server_8_4", "action": "ensure", "reason": "detected_percona_server", "package": pkg, "version": ver}
            if mm == "8.0" or not mm:
                return {"profile": "percona_server_8_0", "action": "ensure", "reason": "detected_percona_server", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"unsupported_percona_server_{mm or 'unknown'}", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg == "mariadb-server" or pkg.startswith("mariadb-server-"):
            mm = _version_mm(ver)
            if mm == "11.4":
                return {"profile": "mariadb_11_4", "action": "ensure", "reason": "detected_mariadb_11_4", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"mariadb_{mm or 'unknown'}_no_profile", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg in {"mysql-server", "mysql-community-server"} or pkg.startswith("mysql-server-"):
            mm = _version_mm(ver)
            if mm == "8.0":
                return {"profile": "mysql_8_0", "action": "skip", "reason": "mysql_8_0_repo_unchanged", "package": pkg, "version": ver}
            if mm == "8.4":
                return {"profile": "mysql_8_4", "action": "ensure", "reason": "detected_mysql_8_4", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"mysql_{mm or 'unknown'}_no_profile", "package": pkg, "version": ver}

    return {"profile": "none", "action": "none", "reason": "db_stack_not_detected"}


def _repo_profile_present(profile_key: str) -> bool:
    key = str(profile_key or "").strip().lower()
    txt = _all_sources_text_lower()
    if key == "mariadb_11_4":
        for raw_line in txt.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not any(x in line for x in ("r.mariadb.com", "deb.mariadb.org", "mariadb.com", "dlm.mariadb.com")):
                continue
            if "11.4" in line or "mariadb-11.4" in line:
                return True
        return False
    if key == "percona_server_8_0":
        return ("repo.percona.com" in txt) and ("ps80" in txt or "ps-80" in txt)
    if key == "percona_cluster_8_0":
        return ("repo.percona.com" in txt) and ("pxc80" in txt or "pxc-80" in txt)
    if key == "percona_server_8_4":
        return ("repo.percona.com" in txt) and ("ps-84-lts" in txt or "ps84" in txt)
    if key == "percona_cluster_8_4":
        return ("repo.percona.com" in txt) and ("pxc-84-lts" in txt or "pxc84" in txt)
    if key == "mysql_8_4":
        return ("repo.mysql.com" in txt) and ("mysql-8.4" in txt or "mysql-8.4-lts" in txt)
    if key == "mysql_8_0":
        return True
    return False


def _apply_repo_profiles(
    profile: dict[str, Any],
    *,
    cfg: Any | None = None,
    timeout_sec: int = 180,
    force_rescan: bool = False,
) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    marker_path = _apt_repo_marker_path(cfg)
    marker = _read_json(marker_path)
    profiles = marker.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        marker["profiles"] = profiles

    now = _now_utc_iso()
    profile_hash = hashlib.sha256(json.dumps(profile or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _mark(
        key: str,
        *,
        status: str,
        applied: bool,
        reason: str = "",
        detected: dict[str, str] | None = None,
        attempted_once: bool | None = None,
    ) -> None:
        row = profiles.get(key)
        if not isinstance(row, dict):
            row = {}
        row["last_status"] = str(status or "")
        row["applied"] = bool(applied)
        row["profile_hash"] = profile_hash
        row["checked_at_utc"] = now
        row["last_error"] = "" if applied else str(reason or "")
        if bool(applied):
            row["applied_at_utc"] = now
        if attempted_once is None:
            row["attempted_once"] = bool(row.get("attempted_once", False)) or bool(applied)
        else:
            row["attempted_once"] = bool(attempted_once)
        if isinstance(detected, dict):
            row["detected"] = detected
        profiles[key] = row

    # DB repo profile (auto-detected by installed DB stack).
    db_enabled = _bool(profile.get("db_repo_profile_enabled", profile.get("mariadb_repo_setup_enabled")), True)
    db_once = _bool(profile.get("db_repo_profile_apply_once"), True)
    db_row = profiles.get("db_repo") if isinstance(profiles.get("db_repo"), dict) else {}
    db_same_profile = str(db_row.get("profile_hash", "") or "") == profile_hash
    if not db_enabled:
        actions.append("db_repo_profile:disabled")
    elif db_once and bool(db_row.get("applied", False)) and db_same_profile and not force_rescan:
        actions.append("db_repo_profile:skip_once")
    else:
        explicit_db_target = str(profile.get("db_repo_profile_target", "") or "").strip().lower()
        if explicit_db_target in {
            "mariadb_11_4",
            "mysql_8_4",
            "percona_server_8_4",
            "percona_cluster_8_4",
            "percona_server_8_0",
            "percona_cluster_8_0",
        }:
            detected = {
                "profile": explicit_db_target,
                "action": "ensure",
                "reason": "explicit_db_repo_profile_target",
            }
        else:
            detected = _detect_db_repo_target(timeout_sec=max(10, int(timeout_sec)))
        det_profile = str(detected.get("profile", "none") or "none")
        det_action = str(detected.get("action", "none") or "none")
        if det_action in {"none", "skip"}:
            _mark("db_repo", status="skipped", applied=True, reason=str(detected.get("reason", "")), detected=detected)
            actions.append(f"db_repo_profile:skipped:{det_profile}")
        else:
            if _repo_profile_present(det_profile):
                _mark("db_repo", status="already_present", applied=True, detected=detected)
                actions.append(f"db_repo_profile:already_present:{det_profile}")
            else:
                ok = False
                msg = f"unsupported_repo_profile:{det_profile}"
                if det_profile == "mariadb_11_4":
                    ok, msg = _run_mariadb_repo_setup(
                        str(profile.get("mariadb_repo_setup_version", "mariadb-11.4")),
                        timeout_sec=max(30, int(timeout_sec)),
                    )
                elif det_profile == "percona_server_8_4":
                    ok, msg = _run_percona_repo_setup("ps-84-lts", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "percona_server_8_0":
                    ok, msg = _run_percona_repo_setup("ps80", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "percona_cluster_8_4":
                    ok, msg = _run_percona_repo_setup("pxc-84-lts", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "percona_cluster_8_0":
                    ok, msg = _run_percona_repo_setup("pxc80", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "mysql_8_4":
                    ok, msg = _run_mysql84_repo_setup(timeout_sec=max(45, int(timeout_sec)))
                if ok:
                    _mark("db_repo", status="applied", applied=True, detected=detected)
                    actions.append(f"db_repo_profile:applied:{det_profile}")
                    actions.append(msg)
                else:
                    _mark("db_repo", status="error", applied=False, reason=msg, detected=detected, attempted_once=False)
                    errors.append(f"db_repo_profile:{msg}")

    # Ondrej PHP repo profile (independent one-time profile).
    php_enabled = _bool(profile.get("ondrej_php_profile_enabled", profile.get("ensure_ondrej_php_ppa")), True)
    php_once = _bool(profile.get("ondrej_php_profile_apply_once"), True)
    php_row = profiles.get("ondrej_php") if isinstance(profiles.get("ondrej_php"), dict) else {}
    php_same_profile = str(php_row.get("profile_hash", "") or "") == profile_hash
    if not php_enabled:
        actions.append("ondrej_php_profile:disabled")
    elif php_once and bool(php_row.get("applied", False)) and php_same_profile and not force_rescan:
        actions.append("ondrej_php_profile:skip_once")
    else:
        if _has_ppa_marker("ondrej/php"):
            _mark("ondrej_php", status="already_present", applied=True)
            actions.append("ondrej_php_profile:already_present")
        else:
            ok, msg = _ensure_ppa("ondrej/php", timeout_sec=max(20, int(timeout_sec)))
            if ok:
                _mark("ondrej_php", status="applied", applied=True)
                actions.append("ondrej_php_profile:applied")
                actions.append(msg)
            else:
                _mark("ondrej_php", status="error", applied=False, reason=msg, attempted_once=False)
                errors.append(f"ondrej_php_profile:{msg}")

    # Official stable nginx.org repo profile. This replaces the historical Ondrej
    # nginx PPA for nginx packages, while keeping Ondrej PHP independent.
    nginx_official_enabled = _bool(
        profile.get("nginx_official_stable_profile_enabled", profile.get("ensure_nginx_official_stable_repo")),
        False,
    )
    nginx_official_once = _bool(profile.get("nginx_official_stable_profile_apply_once"), True)
    nginx_official_remove_ondrej = _bool(profile.get("nginx_official_stable_remove_ondrej"), True)
    nginx_official_row = profiles.get("nginx_official_stable") if isinstance(profiles.get("nginx_official_stable"), dict) else {}
    nginx_official_same_profile = str(nginx_official_row.get("profile_hash", "") or "") == profile_hash
    if not nginx_official_enabled:
        actions.append("nginx_official_stable_profile:disabled")
    elif (
        nginx_official_once
        and bool(nginx_official_row.get("applied", False))
        and nginx_official_same_profile
        and _nginx_official_profile_satisfied(remove_ondrej=bool(nginx_official_remove_ondrej))
        and not force_rescan
    ):
        actions.append("nginx_official_stable_profile:skip_once")
    else:
        ok, msg, repo_actions = _ensure_nginx_official_stable_repo(
            remove_ondrej=bool(nginx_official_remove_ondrej),
            timeout_sec=max(30, int(timeout_sec)),
        )
        actions.extend(repo_actions)
        if ok:
            baseline = ensure_nginx_baseline(reload_service=True)
            baseline_actions = baseline.get("actions") if isinstance(baseline, dict) else []
            if isinstance(baseline_actions, list):
                actions.extend(f"nginx_runtime_baseline:{x}" for x in baseline_actions)
            if str(baseline.get("status", "") if isinstance(baseline, dict) else "").lower() == "error":
                err = str(baseline.get("error", "nginx runtime baseline failed") if isinstance(baseline, dict) else "nginx runtime baseline failed")
                _mark("nginx_official_stable", status="error", applied=False, reason=err, attempted_once=False)
                errors.append(f"nginx_official_stable_profile:{err}")
            else:
                _mark("nginx_official_stable", status="applied", applied=True)
                actions.append("nginx_official_stable_profile:applied")
        else:
            _mark("nginx_official_stable", status="error", applied=False, reason=msg, attempted_once=False)
            errors.append(f"nginx_official_stable_profile:{msg}")

    # Legacy Ondrej nginx repo profile (independent one-time profile).
    nginx_enabled = _bool(profile.get("ondrej_nginx_profile_enabled", profile.get("ensure_ondrej_nginx_ppa")), False)
    nginx_once = _bool(profile.get("ondrej_nginx_profile_apply_once"), True)
    nginx_row = profiles.get("ondrej_nginx") if isinstance(profiles.get("ondrej_nginx"), dict) else {}
    nginx_same_profile = str(nginx_row.get("profile_hash", "") or "") == profile_hash
    if nginx_official_enabled:
        actions.append("ondrej_nginx_profile:disabled_by_nginx_official_stable")
    elif not nginx_enabled:
        actions.append("ondrej_nginx_profile:disabled")
    elif nginx_once and bool(nginx_row.get("applied", False)) and nginx_same_profile and not force_rescan:
        actions.append("ondrej_nginx_profile:skip_once")
    else:
        if _has_ppa_marker("ondrej/nginx"):
            _mark("ondrej_nginx", status="already_present", applied=True)
            actions.append("ondrej_nginx_profile:already_present")
        else:
            ok, msg = _ensure_ppa("ondrej/nginx", timeout_sec=max(20, int(timeout_sec)))
            if ok:
                _mark("ondrej_nginx", status="applied", applied=True)
                actions.append("ondrej_nginx_profile:applied")
                actions.append(msg)
            else:
                _mark("ondrej_nginx", status="error", applied=False, reason=msg, attempted_once=False)
                errors.append(f"ondrej_nginx_profile:{msg}")

    zbx_agent_enabled = _bool(profile.get("zabbix_agent_enabled"), False)
    zbx_repo_enabled = _bool(profile.get("zabbix_agent_repo_enabled"), zbx_agent_enabled)
    zbx_repo_once = _bool(profile.get("zabbix_agent_repo_apply_once"), True)
    zbx_repo_version = re.sub(r"[^0-9.]", "", str(profile.get("zabbix_agent_repo_version", "7.0") or "7.0")) or "7.0"
    zbx_row = profiles.get("zabbix_agent_repo") if isinstance(profiles.get("zabbix_agent_repo"), dict) else {}
    zbx_same_profile = str(zbx_row.get("profile_hash", "") or "") == profile_hash
    if not zbx_agent_enabled:
        actions.append("zabbix_agent_repo:disabled")
    elif not zbx_repo_enabled:
        actions.append("zabbix_agent_repo:repo_disabled")
    elif (
        zbx_repo_once
        and bool(zbx_row.get("applied", False))
        and zbx_same_profile
        and _zabbix_agent_repo_present(zbx_repo_version)
        and not force_rescan
    ):
        actions.append("zabbix_agent_repo:skip_once")
    else:
        if _zabbix_agent_repo_present(zbx_repo_version):
            _mark("zabbix_agent_repo", status="already_present", applied=True)
            actions.append(f"zabbix_agent_repo:already_present:{zbx_repo_version}")
        else:
            ok, msg = _ensure_zabbix_agent_repo(zbx_repo_version, timeout_sec=max(30, int(timeout_sec)))
            if ok:
                _mark("zabbix_agent_repo", status="applied", applied=True)
                actions.append(msg)
            else:
                _mark("zabbix_agent_repo", status="error", applied=False, reason=msg, attempted_once=False)
                errors.append(f"zabbix_agent_repo:{msg}")

    marker["updated_at_utc"] = now
    _write_json(marker_path, marker)
    return actions, errors


def _normalize_unattended_blacklist(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = [str(x or "").strip() for x in raw]
    else:
        text = str(raw or "").strip()
        items = [x.strip() for x in text.split(",")] if text else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        if not re.match(r"^[A-Za-z0-9+_.:*?-]+$", item):
            continue
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _validate_cron_expr(expr: str) -> bool:
    raw = str(expr or "").strip()
    if not raw:
        return False
    parts = raw.split()
    if len(parts) != 5:
        return False
    token_re = re.compile(r"^[0-9*/,\-]+$")
    if not all(token_re.match(x) for x in parts):
        return False
    return True


def _write_if_changed(path: Path, content: str) -> bool:
    prev = ""
    try:
        if path.exists():
            prev = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        prev = ""
    if prev == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _apply_unattended_upgrades(profile: dict[str, Any], *, timeout_sec: int = 180) -> tuple[list[str], list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    changed: list[str] = []

    mode = str(profile.get("unattended_upgrade_mode", "off") or "off").strip().lower()
    if mode not in {"off", "security", "all"}:
        mode = "off"
    schedule = str(profile.get("unattended_upgrade_schedule_cron", "") or "").strip()
    if mode != "off" and not schedule:
        schedule = "30 23 * * 0"
    if schedule and not _validate_cron_expr(schedule):
        errors.append(f"unattended_upgrade_invalid_schedule:{schedule}")
        schedule = ""

    blacklist = _normalize_unattended_blacklist(
        profile.get(
            "unattended_upgrade_blacklist",
            ["mysql", "mariadb", "percona", "php", "nginx", "haproxy"],
        )
    )

    conf_auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    conf_u = Path("/etc/apt/apt.conf.d/52mcd-unattended-upgrades")
    cron_file = Path("/etc/cron.d/mcd-unattended-upgrades")

    if mode == "off":
        auto_txt = (
            "// managed by mcd apt profile\n"
            "APT::Periodic::Update-Package-Lists \"0\";\n"
            "APT::Periodic::Unattended-Upgrade \"0\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"0\";\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if conf_u.exists():
            try:
                conf_u.unlink()
                changed.append(str(conf_u))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_conf_failed:{e}")
        if cron_file.exists():
            try:
                cron_file.unlink()
                changed.append(str(cron_file))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_cron_failed:{e}")
        actions.append("unattended_upgrade:off")
        return actions, errors, changed

    p_install = _run(["apt-get", "install", "-y", "unattended-upgrades"], timeout_sec=max(30, int(timeout_sec)))
    if p_install.returncode != 0:
        errors.append((p_install.stderr or p_install.stdout or "install unattended-upgrades failed").strip())
        actions.append("unattended_upgrade:package_install_failed")
        return actions, errors, changed

    u_lines: list[str] = [
        "// managed by mcd apt profile",
        'Unattended-Upgrade::MinimalSteps "true";',
        'Unattended-Upgrade::Remove-Unused-Dependencies "true";',
        'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";',
        'Unattended-Upgrade::Automatic-Reboot "false";',
    ]
    if mode == "all":
        u_lines.extend(
            [
                "Unattended-Upgrade::Origins-Pattern {",
                '  "origin=*";',
                "};",
            ]
        )
    if blacklist:
        u_lines.append("Unattended-Upgrade::Package-Blacklist {")
        for item in blacklist:
            u_lines.append(f'  "{item}";')
        u_lines.append("};")
    u_txt = "\n".join(u_lines).rstrip("\n") + "\n"
    if _write_if_changed(conf_u, u_txt):
        changed.append(str(conf_u))

    if schedule:
        auto_txt = (
            "// managed by mcd apt profile (cron-scheduled)\n"
            "APT::Periodic::Update-Package-Lists \"0\";\n"
            "APT::Periodic::Unattended-Upgrade \"0\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"0\";\n"
        )
        cron_txt = (
            "# managed by mcd apt profile\n"
            f"{schedule} root /usr/bin/flock -n /run/mcd-unattended-upgrades.lock "
            "/bin/bash -lc '/usr/bin/apt-get update -qq && /usr/bin/unattended-upgrade -v' "
            ">> /var/log/unattended-upgrades/mcd-cron.log 2>&1\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if _write_if_changed(cron_file, cron_txt):
            changed.append(str(cron_file))
        actions.append(f"unattended_upgrade:scheduled:{schedule}")
    else:
        auto_txt = (
            "// managed by mcd apt profile (apt periodic)\n"
            "APT::Periodic::Update-Package-Lists \"1\";\n"
            "APT::Periodic::Unattended-Upgrade \"1\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"1\";\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if cron_file.exists():
            try:
                cron_file.unlink()
                changed.append(str(cron_file))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_cron_failed:{e}")
        actions.append("unattended_upgrade:apt_periodic")

    actions.append(f"unattended_upgrade:mode={mode}")
    return actions, errors, changed


def collect_unattended_upgrade_state() -> dict[str, Any]:
    conf_auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    conf_u = Path("/etc/apt/apt.conf.d/52mcd-unattended-upgrades")
    cron_file = Path("/etc/cron.d/mcd-unattended-upgrades")
    state = {
        "managed_conf_present": bool(conf_u.exists()),
        "cron_present": bool(cron_file.exists()),
        "auto_upgrades_present": bool(conf_auto.exists()),
    }
    if conf_auto.exists():
        try:
            txt = conf_auto.read_text(encoding="utf-8", errors="ignore")
            state["auto_mode"] = "enabled" if '"1"' in txt else "disabled"
        except Exception:
            state["auto_mode"] = "unknown"
    else:
        state["auto_mode"] = "missing"
    return state


def _mysql_client_bin() -> str | None:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _sql_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "''")


def _normalize_grants(raw: Any) -> list[str]:
    default = ["REPLICATION CLIENT", "PROCESS", "SHOW DATABASES", "SHOW VIEW"]
    if not isinstance(raw, list):
        return default
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().upper()
        if not s:
            continue
        if not re.match(r"^[A-Z_ ]+$", s):
            continue
        out.append(s)
    return out or default


def _is_local_mysql_host(raw: str) -> bool:
    host = str(raw or "").strip().lower()
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _run_mysql_root_sql(sql: str, *, timeout_sec: int = 20, cfg: Any | None = None) -> tuple[bool, str]:
    mysql_bin = _mysql_client_bin()
    if not mysql_bin:
        return False, "mysql_client_not_found"
    candidates: list[dict[str, str]] = [
        {
            "label": "root_socket",
            "user": "",
            "password": "",
            "socket": "",
            "protocol": "socket",
        }
    ]
    # Some hosts use password-protected root; fallback to distro-maintained DB admin creds.
    debian_cnf = Path("/etc/mysql/debian.cnf")
    if debian_cnf.exists() and debian_cnf.is_file():
        try:
            parser = configparser.RawConfigParser(interpolation=None)
            parser.read(debian_cnf, encoding="utf-8")
            sections = [s for s in parser.sections()]
            if "" not in sections:
                sections.insert(0, "")
            seen: set[tuple[str, str, str]] = set()
            for sec in sections:
                user = parser.get(sec, "user", fallback="").strip() if sec else parser.defaults().get("user", "").strip()
                password = parser.get(sec, "password", fallback="").strip() if sec else parser.defaults().get("password", "").strip()
                socket = parser.get(sec, "socket", fallback="").strip() if sec else parser.defaults().get("socket", "").strip()
                if not user:
                    continue
                key = (user, password, socket)
                if key in seen:
                    continue
                seen.add(key)
                label = f"debian_cnf:{sec or 'defaults'}"
                candidates.append(
                    {
                        "label": label,
                        "user": user,
                        "password": password,
                        "socket": socket,
                        "protocol": "socket",
                    }
                )
        except Exception:
            pass

    # Optional fallback: MCD runtime state DB credentials can be a valid local
    # DB-admin source on some hosts even when root socket auth is disabled.
    if cfg is not None:
        try:
            rt_user = str(getattr(cfg, "state_mysql_user", "") or "").strip()
            rt_password = str(getattr(cfg, "state_mysql_password", "") or "")
            rt_socket = str(getattr(cfg, "state_mysql_unix_socket", "") or "").strip()
            rt_host = str(getattr(cfg, "state_mysql_host", "") or "").strip() or "127.0.0.1"
            rt_port = int(getattr(cfg, "state_mysql_port", 3306) or 3306)
            if rt_user:
                if rt_socket:
                    candidates.append(
                        {
                            "label": "runtime_state:socket",
                            "user": rt_user,
                            "password": rt_password,
                            "socket": rt_socket,
                            "protocol": "socket",
                        }
                    )
                else:
                    candidates.append(
                        {
                            "label": "runtime_state:tcp",
                            "user": rt_user,
                            "password": rt_password,
                            "host": rt_host,
                            "port": str(rt_port),
                            "protocol": "tcp",
                        }
                    )
        except Exception:
            pass

    errors: list[str] = []
    for cand in candidates:
        protocol = str(cand.get("protocol", "socket") or "socket").strip().lower()
        cmd = [mysql_bin, "--batch", "--skip-column-names"]
        if protocol == "tcp":
            host = str(cand.get("host", "")).strip() or "127.0.0.1"
            port = str(cand.get("port", "")).strip()
            cmd.extend(["--protocol=tcp", "-h", host])
            if port:
                cmd.extend(["-P", port])
        else:
            cmd.append("--protocol=socket")
        user = str(cand.get("user", "")).strip()
        password = str(cand.get("password", ""))
        socket = str(cand.get("socket", "")).strip()
        if protocol != "tcp" and socket:
            cmd.extend(["--socket", socket])
        if user:
            cmd.extend(["-u", user])
        if password:
            cmd.append(f"-p{password}")
        cmd.extend(["-e", sql])
        proc = _run(cmd, timeout_sec=timeout_sec)
        if proc.returncode == 0:
            return True, (proc.stdout or "").strip()
        detail = (proc.stderr or proc.stdout or "").strip() or f"mysql_exec_failed_rc_{proc.returncode}"
        errors.append(f"{cand.get('label', 'unknown')}:{detail}")

    return False, " | ".join(errors[:3])


def _needs_validate_password_policy_workaround(detail: str) -> bool:
    txt = str(detail or "").strip().lower()
    if not txt:
        return False
    return (
        "error 1819" in txt
        or "does not satisfy the current policy requirements" in txt
        or ("validate_password" in txt and "policy" in txt)
    )


def _mysql_get_validate_password_policy(*, timeout_sec: int = 12, cfg: Any | None = None) -> tuple[bool, str, str]:
    ok, detail = _run_mysql_root_sql(
        "SHOW VARIABLES LIKE 'validate_password.policy';",
        timeout_sec=timeout_sec,
        cfg=cfg,
    )
    if not ok:
        return False, "", str(detail or "validate_password_policy_probe_failed")
    policy = ""
    for raw in (detail or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2 and parts[0].strip().lower() == "validate_password.policy":
            policy = parts[1].strip()
            break
    if not policy:
        return False, "", f"validate_password_policy_not_found:{(detail or '').strip()}"
    return True, policy, ""


def _run_sql_with_validate_password_policy_relax(
    sql: str,
    *,
    timeout_sec: int = 20,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    policy_ok, previous_policy_raw, policy_err = _mysql_get_validate_password_policy(
        timeout_sec=max(5, int(timeout_sec)),
        cfg=cfg,
    )
    if not policy_ok:
        return False, str(policy_err or "validate_password_policy_probe_failed")

    previous_policy = str(previous_policy_raw or "").strip().upper() or "MEDIUM"
    lowered = False
    if previous_policy != "LOW":
        ok_low, low_detail = _run_mysql_root_sql(
            "SET GLOBAL validate_password.policy='LOW';",
            timeout_sec=max(5, int(timeout_sec)),
            cfg=cfg,
        )
        if not ok_low:
            return False, f"validate_password_policy_set_low_failed:{str(low_detail or '').strip()}"
        lowered = True

    ok_apply, apply_detail = _run_mysql_root_sql(sql, timeout_sec=max(8, int(timeout_sec)), cfg=cfg)

    restore_warn = ""
    if lowered:
        ok_restore, restore_detail = _run_mysql_root_sql(
            f"SET GLOBAL validate_password.policy='{_sql_escape(previous_policy)}';",
            timeout_sec=max(5, int(timeout_sec)),
            cfg=cfg,
        )
        if not ok_restore:
            restore_warn = f"validate_password_policy_restore_failed:{str(restore_detail or '').strip()}"

    if not ok_apply:
        reason = f"apply_failed_under_relaxed_policy:{str(apply_detail or '').strip()}"
        if restore_warn:
            reason = f"{reason} | {restore_warn}"
        return False, reason

    detail = str(apply_detail or "").strip()
    if restore_warn:
        detail = f"{detail} | {restore_warn}" if detail else restore_warn
    return True, detail


def _mysql_user_exists(
    user: str,
    host: str,
    *,
    timeout_sec: int = 12,
    cfg: Any | None = None,
) -> tuple[bool | None, str]:
    sql = (
        "SELECT 1 FROM mysql.user "
        f"WHERE User='{_sql_escape(user)}' AND Host='{_sql_escape(host)}' LIMIT 1;"
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=timeout_sec, cfg=cfg)
    if not ok:
        return None, detail
    exists = any(x.strip() == "1" for x in (detail or "").splitlines())
    return exists, ""


def collect_zabbix_mysql_monitor_state(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)
    out = {
        "status": str(marker.get("last_status", "unknown") or "unknown"),
        "applied": bool(marker.get("applied", False)),
        "user": str(marker.get("user", "zbx_monitor") or "zbx_monitor"),
        "host": str(marker.get("host", "127.0.0.1") or "127.0.0.1"),
        "attempted_at_utc": str(marker.get("attempted_at_utc", "") or ""),
        "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        "last_error": str(marker.get("last_error", "") or ""),
        "marker_path": str(marker_path),
    }
    return out


def ensure_zabbix_mysql_monitor_user(
    profile: dict[str, Any] | None,
    *,
    cfg: Any | None = None,
    force: bool = False,
    timeout_sec: int = 20,
) -> dict[str, Any]:
    prof = profile if isinstance(profile, dict) else {}
    enabled = _bool(prof.get("zabbix_mysql_monitor_enabled"), True)
    user = str(prof.get("zabbix_mysql_monitor_user", "zbx_monitor") or "zbx_monitor").strip() or "zbx_monitor"
    host = str(prof.get("zabbix_mysql_monitor_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    password = str(prof.get("zabbix_mysql_monitor_password", "zbx_monitor") or "zbx_monitor")
    grants = _normalize_grants(prof.get("zabbix_mysql_monitor_grants"))
    apply_once = _bool(prof.get("zabbix_mysql_monitor_apply_once"), True)
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)

    if not enabled:
        result = {
            "status": "disabled",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "reason": "zabbix_mysql_monitor_disabled",
        }
        marker.update(
            {
                "last_status": "disabled",
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return result

    if apply_once and not force and bool(marker.get("applied", False)):
        return {
            "status": "noop",
            "reason": "already_applied_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        }

    exists_before, exists_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)), cfg=cfg)
    if exists_before is True:
        marker.update(
            {
                "last_status": "already_present",
                "applied": True,
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "applied_at_utc": str(marker.get("applied_at_utc") or _now_utc_iso()),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "already_present",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "user_exists_before": True,
            "user_exists_after": True,
        }
    if exists_before is None:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": str(exists_err or "mysql_user_probe_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(exists_err or "mysql_user_probe_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    if apply_once and not force and bool(marker.get("attempted_once", False)):
        return {
            "status": "skipped",
            "reason": "already_attempted_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "last_status": str(marker.get("last_status", "") or ""),
            "last_error": str(marker.get("last_error", "") or ""),
        }

    grants_sql = ", ".join(grants)
    sql = "\n".join(
        [
            f"CREATE USER IF NOT EXISTS '{_sql_escape(user)}'@'{_sql_escape(host)}' IDENTIFIED BY '{_sql_escape(password)}';",
            f"GRANT {grants_sql} ON *.* TO '{_sql_escape(user)}'@'{_sql_escape(host)}';",
            "FLUSH PRIVILEGES;",
        ]
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=max(8, int(timeout_sec)), cfg=cfg)
    validate_password_policy_relaxed = False
    if not ok and _needs_validate_password_policy_workaround(detail):
        wrk_ok, wrk_detail = _run_sql_with_validate_password_policy_relax(
            sql,
            timeout_sec=max(8, int(timeout_sec)),
            cfg=cfg,
        )
        if wrk_ok:
            ok = True
            detail = wrk_detail
            validate_password_policy_relaxed = True
        else:
            detail = f"{str(detail or '').strip()} | {str(wrk_detail or '').strip()}".strip(" |")
    attempted_at = _now_utc_iso()
    if not ok:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": str(detail or "mysql_exec_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(detail or "mysql_exec_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    exists_after, verify_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)), cfg=cfg)
    if exists_after is not True:
        reason = str(verify_err or "mysql_user_not_visible_after_apply")
        marker.update(
            {
                "last_status": "error",
                "applied": False,
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": reason,
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": reason,
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    marker.update(
        {
            "last_status": "applied",
            "applied": True,
            "user": user,
            "host": host,
            "attempted_at_utc": attempted_at,
            "applied_at_utc": _now_utc_iso(),
            "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
            "last_error": "",
        }
    )
    _write_json(marker_path, marker)
    return {
        "status": "applied",
        "user": user,
        "host": host,
        "marker_path": str(marker_path),
        "user_exists_before": False,
        "user_exists_after": True,
        "grants": grants,
        "validate_password_policy_relaxed": bool(validate_password_policy_relaxed),
    }


def _normalize_ip_networks(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        try:
            network = str(ipaddress.ip_network(value, strict=False))
        except Exception:
            try:
                network = str(ipaddress.ip_address(value))
            except Exception:
                continue
        if network in seen:
            continue
        seen.add(network)
        out.append(network)
    return out


def _read_key_value_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _zabbix_agent_include_present(text: str) -> bool:
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("include=") and "zabbix_agent2.d" in line:
            return True
    return False


def _desired_zabbix_agent_dropin(profile: dict[str, Any]) -> dict[str, str]:
    server = str(profile.get("zabbix_agent_server", "") or "").strip()
    active = str(profile.get("zabbix_agent_server_active", "") or "").strip() or server
    hostname = str(profile.get("zabbix_agent_hostname", "") or "").strip()
    port = str(_int(profile.get("zabbix_agent_port"), 10050, 1, 65535))
    out = {
        "Server": server,
        "ServerActive": active,
        "ListenPort": port,
    }
    if hostname:
        out["Hostname"] = hostname
    return out


def _write_zabbix_agent_dropin(profile: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    desired = _desired_zabbix_agent_dropin(profile)
    if not desired.get("Server"):
        return False, actions, ["zabbix_agent_server_missing"]

    if _ZABBIX_AGENT_BASE_CONF.exists():
        try:
            base = _ZABBIX_AGENT_BASE_CONF.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return False, actions, [f"zabbix_agent_base_conf_read_failed:{e}"]
        if not _zabbix_agent_include_present(base):
            try:
                _ZABBIX_AGENT_BASE_CONF.write_text(
                    base.rstrip("\n")
                    + "\n\n# managed by mcd service profile\n"
                    + "Include=/etc/zabbix/zabbix_agent2.d/*.conf\n",
                    encoding="utf-8",
                )
                actions.append(f"zabbix_agent_include_added:{_ZABBIX_AGENT_BASE_CONF}")
            except Exception as e:
                return False, actions, [f"zabbix_agent_base_conf_write_failed:{e}"]

    lines = ["# managed by mcd service profile (zabbix agent endpoint)"]
    for key in ("Server", "ServerActive", "Hostname", "ListenPort"):
        value = desired.get(key)
        if value:
            lines.append(f"{key}={value}")
    content = "\n".join(lines).rstrip("\n") + "\n"
    try:
        changed = _write_if_changed(_ZABBIX_AGENT_DROPIN, content)
        _ZABBIX_AGENT_DROPIN.chmod(0o644)
    except Exception as e:
        return False, actions, [f"zabbix_agent_dropin_write_failed:{e}"]
    actions.append(f"zabbix_agent_dropin:{'changed' if changed else 'already_ok'}")
    return bool(changed), actions, errors


def _service_active(name: str, *, timeout_sec: int = 5) -> bool:
    p = _run(["systemctl", "is-active", name], timeout_sec=timeout_sec)
    return p.returncode == 0 and (p.stdout or "").strip() == "active"


def _service_enabled(name: str, *, timeout_sec: int = 5) -> bool:
    p = _run(["systemctl", "is-enabled", name], timeout_sec=timeout_sec)
    return p.returncode == 0 and (p.stdout or "").strip() in {"enabled", "enabled-runtime"}


def _tcp_port_listening(port: int, *, timeout_sec: int = 5) -> bool:
    ss = shutil.which("ss")
    if not ss:
        return False
    p = _run([ss, "-ltn"], timeout_sec=timeout_sec)
    if p.returncode != 0:
        return False
    needle = f":{int(port)}"
    for raw in (p.stdout or "").splitlines():
        line = raw.strip()
        if needle in line:
            return True
    return False


def _ufw_active() -> bool:
    if not shutil.which("ufw"):
        return False
    p = _run(["ufw", "status"], timeout_sec=8)
    first = (p.stdout or p.stderr or "").strip().splitlines()
    return p.returncode == 0 and bool(first) and first[0].strip().lower() == "status: active"


def _ensure_zabbix_agent_firewall(profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    if not _bool(profile.get("zabbix_agent_firewall_enabled"), True):
        return ["zabbix_agent_firewall:disabled"], []
    port = _int(profile.get("zabbix_agent_port"), 10050, 1, 65535)
    sources = _normalize_ip_networks(profile.get("zabbix_agent_firewall_sources"))
    if not sources:
        return ["zabbix_agent_firewall:no_sources"], []
    actions: list[str] = []
    errors: list[str] = []

    if _ufw_active():
        for src in sources:
            p = _run(
                ["ufw", "allow", "proto", "tcp", "from", src, "to", "any", "port", str(port), "comment", "mcd zabbix agent"],
                timeout_sec=20,
            )
            if p.returncode == 0:
                actions.append(f"zabbix_agent_firewall_ufw:{src}:{port}")
            else:
                errors.append(f"zabbix_agent_firewall_ufw:{src}:{(p.stderr or p.stdout or p.returncode)}")
        return actions, errors

    iptables = shutil.which("iptables")
    iptables_save = shutil.which("iptables-save")
    if not iptables:
        return ["zabbix_agent_firewall:iptables_missing"], []
    for src in sources:
        check = _run([iptables, "-w", "-C", "INPUT", "-p", "tcp", "-s", src, "--dport", str(port), "-j", "ACCEPT"], timeout_sec=10)
        if check.returncode == 0:
            actions.append(f"zabbix_agent_firewall_iptables:already_present:{src}:{port}")
            continue
        add = _run([iptables, "-w", "-I", "INPUT", "1", "-p", "tcp", "-s", src, "--dport", str(port), "-j", "ACCEPT"], timeout_sec=10)
        if add.returncode == 0:
            actions.append(f"zabbix_agent_firewall_iptables:added:{src}:{port}")
        else:
            errors.append(f"zabbix_agent_firewall_iptables:{src}:{(add.stderr or add.stdout or add.returncode)}")
    rules_path = Path("/etc/iptables/rules.v4")
    if iptables_save and rules_path.parent.exists() and not errors:
        save = _run([iptables_save], timeout_sec=15)
        if save.returncode == 0:
            try:
                rules_path.write_text(save.stdout or "", encoding="utf-8")
                actions.append(f"zabbix_agent_firewall_saved:{rules_path}")
            except Exception as e:
                errors.append(f"zabbix_agent_firewall_save_failed:{e}")
    return actions, errors


def collect_zabbix_agent_state(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    prof = profile if isinstance(profile, dict) else {}
    enabled = _bool(prof.get("zabbix_agent_enabled"), False)
    desired = _desired_zabbix_agent_dropin(prof)
    installed = _dpkg_installed_versions(timeout_sec=12).get("zabbix-agent2", "")
    dropin_values = _read_key_value_file(_ZABBIX_AGENT_DROPIN)
    port = _int(prof.get("zabbix_agent_port"), 10050, 1, 65535)
    active = _service_active("zabbix-agent2", timeout_sec=4)
    service_enabled = _service_enabled("zabbix-agent2", timeout_sec=4)
    listening = _tcp_port_listening(port, timeout_sec=4)
    config_matches = True
    mismatches: list[str] = []
    for key, wanted in desired.items():
        if not wanted:
            continue
        got = str(dropin_values.get(key, "") or "").strip()
        if got != str(wanted):
            config_matches = False
            mismatches.append(key)
    if not enabled:
        status = "disabled"
        message = "disabled by MCC profile"
    elif not installed:
        status = "missing"
        message = "zabbix-agent2 package is not installed"
    elif desired.get("Server") and not config_matches:
        status = "config_mismatch"
        message = "managed drop-in does not match MCC Zabbix settings"
    elif not active:
        status = "inactive"
        message = "zabbix-agent2 service is not active"
    elif not listening:
        status = "not_listening"
        message = f"zabbix-agent2 is not listening on tcp/{port}"
    else:
        status = "ok"
        message = f"zabbix-agent2 {installed}, active"
    return {
        "status": status,
        "message": message,
        "enabled": bool(enabled),
        "installed": bool(installed),
        "version": str(installed or ""),
        "service": {"active": bool(active), "enabled": bool(service_enabled)},
        "dropin": {
            "path": str(_ZABBIX_AGENT_DROPIN),
            "present": bool(_ZABBIX_AGENT_DROPIN.exists()),
            "values": dropin_values,
            "matches": bool(config_matches),
            "mismatches": mismatches,
        },
        "desired": desired,
        "listen_port": int(port),
        "listening": bool(listening),
        "firewall_sources": _normalize_ip_networks(prof.get("zabbix_agent_firewall_sources")),
    }


def ensure_zabbix_disk_health(profile: dict[str, Any]) -> dict[str, Any]:
    if not _bool(profile.get("zabbix_disk_health_enabled"), False):
        return {"status": "disabled", "reason": "zabbix_disk_health_disabled", "actions": []}
    installed = _dpkg_installed_versions(timeout_sec=12)
    missing = [name for name in ("smartmontools", "nvme-cli") if not installed.get(name)]
    if missing:
        return {"status": "error", "reason": f"missing packages: {', '.join(missing)}", "actions": []}

    actions: list[str] = []
    errors: list[str] = []
    try:
        if _write_if_changed(_ZABBIX_DISK_HEALTH_SCRIPT, _ZABBIX_DISK_HEALTH_SCRIPT_TEXT):
            actions.append(f"zabbix_disk_health_script:written:{_ZABBIX_DISK_HEALTH_SCRIPT}")
        _ZABBIX_DISK_HEALTH_SCRIPT.chmod(0o755)
        if _write_if_changed(_ZABBIX_DISK_HEALTH_DROPIN, _ZABBIX_DISK_HEALTH_DROPIN_TEXT):
            actions.append(f"zabbix_disk_health_dropin:written:{_ZABBIX_DISK_HEALTH_DROPIN}")
        _ZABBIX_DISK_HEALTH_DROPIN.chmod(0o644)
        if _write_if_changed(_ZABBIX_DISK_HEALTH_SUDOERS, _ZABBIX_DISK_HEALTH_SUDOERS_TEXT):
            actions.append(f"zabbix_disk_health_sudoers:written:{_ZABBIX_DISK_HEALTH_SUDOERS}")
        _ZABBIX_DISK_HEALTH_SUDOERS.chmod(0o440)
    except Exception as exc:
        errors.append(f"write_failed:{exc}")

    if not errors:
        visudo = shutil.which("visudo")
        if not visudo:
            errors.append("visudo_not_found")
        else:
            checked = _run([visudo, "-cf", str(_ZABBIX_DISK_HEALTH_SUDOERS)], timeout_sec=15)
            if checked.returncode != 0:
                errors.append(f"sudoers_invalid:{(checked.stderr or checked.stdout or checked.returncode)}")

    if errors:
        return {"status": "error", "reason": "; ".join(errors[:5]), "actions": actions}
    return {"status": "applied", "actions": actions}


def ensure_zabbix_agent(profile: dict[str, Any], *, timeout_sec: int = 90) -> dict[str, Any]:
    if not _bool(profile.get("zabbix_agent_enabled"), False):
        return {"status": "disabled", "reason": "zabbix_agent_disabled"}
    if not str(profile.get("zabbix_agent_server", "") or "").strip():
        return {"status": "error", "reason": "zabbix_agent_server_missing"}
    installed = _dpkg_installed_versions(timeout_sec=12).get("zabbix-agent2", "")
    if not installed:
        return {"status": "error", "reason": "zabbix-agent2 package missing after package install"}
    actions: list[str] = []
    errors: list[str] = []
    disk_health = ensure_zabbix_disk_health(profile)
    disk_status = str(disk_health.get("status") or "").strip().lower()
    actions.extend([str(x) for x in list(disk_health.get("actions") or [])])
    if disk_status == "error":
        errors.append(f"zabbix_disk_health:{disk_health.get('reason') or 'unknown_error'}")
    else:
        actions.append(f"zabbix_disk_health:{disk_status or 'unknown'}")
    _, cfg_actions, cfg_errors = _write_zabbix_agent_dropin(profile)
    actions.extend(cfg_actions)
    errors.extend(cfg_errors)
    fw_actions, fw_errors = _ensure_zabbix_agent_firewall(profile)
    actions.extend(fw_actions)
    errors.extend(fw_errors)
    if not errors:
        p = _run(["systemctl", "enable", "--now", "zabbix-agent2"], timeout_sec=max(30, int(timeout_sec)))
        if p.returncode == 0:
            actions.append("zabbix_agent_service:enabled_started")
        else:
            errors.append(f"zabbix_agent_service_start_failed:{(p.stderr or p.stdout or p.returncode)}")
        p2 = _run(["systemctl", "restart", "zabbix-agent2"], timeout_sec=max(30, int(timeout_sec)))
        if p2.returncode == 0:
            actions.append("zabbix_agent_service:restarted")
        else:
            errors.append(f"zabbix_agent_service_restart_failed:{(p2.stderr or p2.stdout or p2.returncode)}")
    state = collect_zabbix_agent_state(profile)
    if errors:
        return {"status": "error", "reason": "; ".join(str(x) for x in errors[:5]), "actions": actions, "state": state}
    if str(state.get("status", "")) != "ok":
        return {"status": "error", "reason": str(state.get("message") or state.get("status") or "zabbix_agent_not_ok"), "actions": actions, "state": state}
    return {"status": "applied", "actions": actions, "state": state}


def _collect_wazuh_agent_state_for_push() -> dict[str, Any]:
    try:
        from mcd_agent.wazuh_profile import collect_wazuh_agent_state

        state = collect_wazuh_agent_state()
    except Exception as exc:
        return {
            "installed": False,
            "version": "",
            "error": str(exc)[:500],
        }
    if isinstance(state, dict):
        return state
    return {
        "installed": False,
        "version": "",
        "error": "invalid_wazuh_agent_state",
    }


def collect_apt_state(
    *,
    timeout_sec: int = 45,
    cfg: Any | None = None,
    profile: dict[str, Any] | None = None,
    auto_bootstrap_zabbix: bool = True,
) -> dict[str, Any]:
    pending_info, pending_err = _pending_updates(timeout_sec=max(10, int(timeout_sec)))
    pending = int(pending_info.get("pending_updates", 0) or 0)
    pending_total = int(pending_info.get("pending_total", pending) or pending)
    pending_phasing = int(pending_info.get("pending_phasing", 0) or 0)
    pending_hold = int(pending_info.get("pending_hold", 0) or 0)
    duplicates = detect_duplicate_list_sources()
    errors: list[str] = list(pending_err)
    dup_count = int(duplicates.get("count", 0) or 0)
    if dup_count > 0:
        errors.append(f"duplicate_apt_sources:{dup_count}")

    status = "ok"
    if errors:
        status = "error"
    elif pending > 0:
        status = "updates_pending"
    elif pending_phasing > 0 or pending_hold > 0:
        status = "updates_deferred"

    if status == "ok":
        level = 0
    elif status == "updates_deferred":
        level = 2
    else:
        level = 5
    zbx_payload = (
        ensure_zabbix_mysql_monitor_user({}, cfg=cfg, force=False, timeout_sec=min(12, max(5, int(timeout_sec))))
        if bool(auto_bootstrap_zabbix)
        else collect_zabbix_mysql_monitor_state(cfg=cfg)
    )
    zbx_agent_payload = collect_zabbix_agent_state(profile=profile)
    return {
        "status": status,
        "level": level,
        "pending_total": int(pending_total),
        "pending_updates": int(pending),
        "pending_regular": int(pending),
        "pending_phasing": int(pending_phasing),
        "pending_hold": int(pending_hold),
        "error_count": int(len(errors)),
        "errors": errors[:20],
        "duplicate_sources": duplicates,
        "upgradable_packages": list(pending_info.get("upgradable_packages", []) or [])[:200],
        "phasing_packages": list(pending_info.get("phasing_packages", []) or [])[:200],
        "held_packages": list(pending_info.get("held_packages", []) or [])[:200],
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zabbix_agent": zbx_agent_payload,
        "zabbix_mysql_monitor": zbx_payload,
        "cloudflare_real_ip": cloudflare_real_ip_state(profile),
        "wazuh_agent": _collect_wazuh_agent_state_for_push(),
        "repo_profiles": collect_apt_repo_profiles_state(cfg=cfg),
        "unattended_upgrade": collect_unattended_upgrade_state(),
    }


def apply_apt_profile(
    profile: dict[str, Any],
    *,
    dry_run: bool = False,
    cfg: Any | None = None,
    force_zabbix_bootstrap: bool = False,
    force_repo_rescan: bool = False,
) -> dict[str, Any]:
    if profile is None:
        profile = {}
    if dry_run:
        current = collect_apt_state(timeout_sec=25, cfg=cfg, profile=profile, auto_bootstrap_zabbix=False)
        return {
            "status": "planned",
            "actions": [
                "remove_third_party_sources",
                "dedupe_list_sources",
                "repo_profiles",
                "nginx_official_stable_repo",
                "unattended_upgrades",
                "repair_repos_on_error",
                "apt_update",
                "ensure_local_fqdn_hosts",
                "optional_package_ops",
                "ensure_package_services_started",
                "ensure_nodejs20",
                "ensure_composer_global",
                "ensure_var_www",
                "cloudflare_real_ip",
                "zabbix_agent_repo",
                "zabbix_agent_config",
                "zabbix_agent_firewall",
                "zabbix_mysql_monitor_bootstrap",
            ],
            "current": current,
        }

    actions: list[str] = []
    changed: list[str] = []
    errors: list[str] = []

    refresh_cache = _bool(profile.get("refresh_cache"), True)
    refresh_timeout_sec = _int(profile.get("refresh_timeout_sec"), 180, 30, 3600)
    repair_on_error = _bool(profile.get("repair_on_error"), True)
    remove_third_party = _bool(profile.get("remove_third_party_sources"), True)
    dedupe_sources = _bool(profile.get("dedupe_list_sources"), True)
    mariadb_repair = _bool(profile.get("mariadb_repo_setup_enabled"), True)
    mariadb_version = str(profile.get("mariadb_repo_setup_version", "mariadb-11.4"))
    ensure_php_ppa = _bool(profile.get("ensure_ondrej_php_ppa"), False)
    ensure_nginx_ppa = _bool(profile.get("ensure_ondrej_nginx_ppa"), False)
    ensure_apache_ppa = _bool(profile.get("ensure_ondrej_apache_ppa"), False)
    db_repo_profile_enabled = _bool(profile.get("db_repo_profile_enabled", mariadb_repair), True)
    ondrej_php_profile_enabled = _bool(profile.get("ondrej_php_profile_enabled", ensure_php_ppa), True)
    ondrej_nginx_profile_enabled = _bool(profile.get("ondrej_nginx_profile_enabled", ensure_nginx_ppa), False)
    nginx_official_stable_profile_enabled = _bool(
        profile.get("nginx_official_stable_profile_enabled", profile.get("ensure_nginx_official_stable_repo")),
        False,
    )
    ensure_local_fqdn_hosts = _bool(profile.get("ensure_local_fqdn_hosts"), True)
    local_fqdn_suffix = str(profile.get("local_fqdn_suffix", "localdomain") or "localdomain")
    ensure_package_services_started = _bool(profile.get("ensure_package_services_started"), True)
    ensure_nodejs20 = _bool(profile.get("ensure_nodejs20"), False)
    ensure_composer_global = _bool(profile.get("ensure_composer_global"), False)
    ensure_var_www = _bool(profile.get("ensure_var_www"), False)
    upgrade_mode = str(profile.get("upgrade_mode", "none")).strip().lower() or "none"
    packages_present = [str(x).strip() for x in list(profile.get("packages_present", []) or []) if str(x).strip()]
    packages_absent = [str(x).strip() for x in list(profile.get("packages_absent", []) or []) if str(x).strip()]

    if remove_third_party:
        p = Path("/etc/apt/sources.list.d/third-party.sources")
        if p.exists():
            p.unlink()
            changed.append(str(p))
            actions.append("removed:/etc/apt/sources.list.d/third-party.sources")

    if dedupe_sources:
        dedupe = dedupe_list_sources()
        removed = int(dedupe.get("removed_lines", 0) or 0)
        if removed > 0:
            actions.append(f"dedupe_list_sources:removed={removed}")
        for f in list(dedupe.get("changed_files", []) or []):
            changed.append(str(f))

    repo_actions, repo_errors = _apply_repo_profiles(
        profile,
        cfg=cfg,
        timeout_sec=max(30, int(refresh_timeout_sec)),
        force_rescan=bool(force_repo_rescan),
    )
    actions.extend(repo_actions)
    errors.extend(repo_errors)
    unattended_actions, unattended_errors, unattended_changed = _apply_unattended_upgrades(
        profile, timeout_sec=max(30, int(refresh_timeout_sec))
    )
    actions.extend(unattended_actions)
    errors.extend(unattended_errors)
    changed.extend(unattended_changed)

    def _run_update() -> list[str]:
        if not refresh_cache:
            return []
        p = _run(["apt-get", "update", "--allow-releaseinfo-change"], timeout_sec=refresh_timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        errs = _apt_update_errors(merged)
        if p.returncode != 0 and not errs:
            errs = [f"apt-get update failed rc={p.returncode}"]
        return errs

    update_errors = _run_update()
    if update_errors:
        errors.extend(update_errors)

    if repair_on_error and update_errors:
        fixed_any = False
        txt = "\n".join(update_errors).lower()
        if (not db_repo_profile_enabled) and mariadb_repair and ("mariadb" in txt or "r.mariadb.com" in txt):
            ok, msg = _run_mariadb_repo_setup(mariadb_version, timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if (not ondrej_php_profile_enabled) and ensure_php_ppa and ("ondrej" in txt or "php" in txt) and not _has_ppa_marker("ondrej/php"):
            ok, msg = _ensure_ppa("ondrej/php", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if (
            (not nginx_official_stable_profile_enabled)
            and (not ondrej_nginx_profile_enabled)
            and ensure_nginx_ppa
            and ("ondrej" in txt or "nginx" in txt)
            and not _has_ppa_marker("ondrej/nginx")
        ):
            ok, msg = _ensure_ppa("ondrej/nginx", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if ensure_apache_ppa and ("ondrej" in txt or "apache" in txt or "apache2" in txt) and not _has_ppa_marker("ondrej/apache2"):
            ok, msg = _ensure_ppa("ondrej/apache2", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if fixed_any:
            retry_errors = _run_update()
            errors = [e for e in errors if not e.startswith("E:") and "apt-get update failed" not in e]
            errors.extend(retry_errors)
            if not retry_errors:
                actions.append("apt_update_repair_ok")

    if ensure_local_fqdn_hosts:
        changed_hosts, hosts_msg = ensure_local_fqdn_hosts_entry(suffix=local_fqdn_suffix)
        actions.append(hosts_msg)
        if changed_hosts:
            changed.append("/etc/hosts")
    else:
        actions.append("hosts_fqdn:disabled")

    if packages_present:
        cmd = ["apt-get", "install", "-y"] + packages_present
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_present:{len(packages_present)}")
            if ensure_package_services_started:
                svc_actions, svc_errors = _enable_start_services(
                    _services_for_present_packages(packages_present),
                    timeout_sec=max(45, min(int(refresh_timeout_sec), 180)),
                )
                actions.extend(svc_actions)
                errors.extend(svc_errors)
            else:
                actions.append("package_services_start:disabled")
        else:
            errors.append((p.stderr or p.stdout or "apt install failed").strip())

    zbx_agent_result = ensure_zabbix_agent(profile, timeout_sec=max(45, int(refresh_timeout_sec)))
    zbx_agent_status = str(zbx_agent_result.get("status", "") or "").strip().lower()
    if zbx_agent_status in {"applied", "disabled"}:
        actions.append(f"zabbix_agent:{zbx_agent_status}")
        for act in list(zbx_agent_result.get("actions", []) or []):
            actions.append(str(act))
    elif zbx_agent_status == "error":
        errors.append(f"zabbix_agent:{str(zbx_agent_result.get('reason', 'unknown_error'))}")
        for act in list(zbx_agent_result.get("actions", []) or []):
            actions.append(str(act))
    else:
        actions.append(f"zabbix_agent:{zbx_agent_status or 'unknown'}")

    if ensure_nodejs20:
        node_changed, node_actions, node_errors = _ensure_nodejs20(timeout_sec=max(90, int(refresh_timeout_sec)))
        actions.extend(node_actions)
        errors.extend(node_errors)
        if node_changed:
            changed.append("/usr/bin/node")
    else:
        actions.append("nodejs20:disabled")

    if ensure_composer_global:
        composer_changed, composer_actions, composer_errors = _ensure_composer_global(timeout_sec=max(90, int(refresh_timeout_sec)))
        actions.extend(composer_actions)
        errors.extend(composer_errors)
        if composer_changed:
            changed.append("/usr/local/bin/composer")
    else:
        actions.append("composer_global:disabled")

    if ensure_var_www:
        var_changed, var_msg, var_errors = _ensure_var_www()
        actions.append(var_msg)
        errors.extend(var_errors)
        if var_changed:
            changed.append("/var/www")
    else:
        actions.append("var_www:disabled")

    if cloudflare_real_ip_profile_present(profile):
        cf_result = ensure_cloudflare_real_ip(profile, reload_service=True)
        cf_status = str(cf_result.get("status", "") or "").strip().lower()
        for act in list(cf_result.get("actions", []) or []):
            actions.append(str(act))
        for changed_file in list(cf_result.get("changed_files", []) or []):
            changed.append(str(changed_file))
        if cf_status in {"error"}:
            errors.append(f"cloudflare_real_ip:{str(cf_result.get('reason', 'unknown_error'))}")
        elif cf_status:
            actions.append(f"cloudflare_real_ip:{cf_status}")
    else:
        actions.append("cloudflare_real_ip:unmanaged")

    if packages_absent:
        cmd = ["apt-get", "purge", "-y"] + packages_absent
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_absent:{len(packages_absent)}")
        else:
            errors.append((p.stderr or p.stdout or "apt purge failed").strip())

    if upgrade_mode in {"safe", "upgrade"}:
        p = _run(["apt-get", "upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:upgrade")
            if ensure_package_services_started:
                svc_actions, svc_errors = _apt_upgrade_service_postcheck(timeout_sec=max(45, min(int(refresh_timeout_sec), 180)))
                actions.extend(svc_actions)
                errors.extend(svc_errors)
            else:
                actions.append("upgrade_service_postcheck:disabled")
        else:
            errors.append((p.stderr or p.stdout or "apt upgrade failed").strip())
    elif upgrade_mode in {"full", "dist", "dist-upgrade"}:
        p = _run(["apt-get", "dist-upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:dist-upgrade")
            if ensure_package_services_started:
                svc_actions, svc_errors = _apt_upgrade_service_postcheck(timeout_sec=max(45, min(int(refresh_timeout_sec), 180)))
                actions.extend(svc_actions)
                errors.extend(svc_errors)
            else:
                actions.append("upgrade_service_postcheck:disabled")
        else:
            errors.append((p.stderr or p.stdout or "apt dist-upgrade failed").strip())

    zbx_result = ensure_zabbix_mysql_monitor_user(
        profile,
        cfg=cfg,
        force=bool(force_zabbix_bootstrap),
        timeout_sec=max(8, int(refresh_timeout_sec)),
    )
    zbx_status = str(zbx_result.get("status", "") or "").strip().lower()
    if zbx_status in {"applied", "already_present", "noop", "disabled", "skipped"}:
        actions.append(f"zabbix_mysql_monitor:{zbx_status}")
    elif zbx_status == "error":
        errors.append(f"zabbix_mysql_monitor:{str(zbx_result.get('reason', 'unknown_error'))}")
    else:
        actions.append(f"zabbix_mysql_monitor:{zbx_status or 'unknown'}")

    state = collect_apt_state(timeout_sec=45, cfg=cfg, profile=profile, auto_bootstrap_zabbix=False)
    if isinstance(zbx_agent_result.get("state"), dict):
        state["zabbix_agent"] = zbx_agent_result.get("state")
    state["zabbix_mysql_monitor"] = zbx_result
    if errors:
        # Preserve explicit action errors together with state-derived errors.
        merged = list(errors)
        for e in list(state.get("errors", []) or []):
            if e not in merged:
                merged.append(e)
        state["errors"] = merged[:40]
        state["error_count"] = int(len(merged))
        state["status"] = "error"
        state["level"] = 5

    return {
        "status": "applied",
        "actions": actions,
        "changed_files": changed,
        "state": state,
    }
