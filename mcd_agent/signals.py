from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from mcd_agent.config import AgentConfig


def _run_journal(args: list[str], timeout_sec: int = 4) -> str:
    base = ["journalctl", "--no-pager", "-o", "cat"]
    cmd = base + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except Exception:
        return ""
    if p.returncode != 0:
        return p.stdout or ""
    return p.stdout or ""


def _systemd_list_units(patterns: list[str], *, active_only: bool) -> list[str]:
    cmd = ["systemctl"]
    if active_only:
        cmd += ["list-units", "--type=service", "--all"]
    else:
        cmd += ["list-unit-files", "--type=service"]
    cmd += ["--no-legend", "--no-pager"]
    cmd += patterns
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out: list[str] = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if not unit.endswith(".service"):
            continue
        out.append(unit)
    return out


def _detect_php_fpm_units() -> list[str]:
    """
    Return PHP-FPM systemd units in preferred order:
    1) active units
    2) installed unit files
    """
    patterns = ["php*-fpm.service", "php-fpm.service"]
    active = _systemd_list_units(patterns, active_only=True)
    installed = _systemd_list_units(patterns, active_only=False)
    ordered: list[str] = []
    seen: set[str] = set()
    for unit in active + installed:
        if unit in seen:
            continue
        seen.add(unit)
        ordered.append(unit)
    return ordered


def _count(text: str, pattern: str) -> int:
    if not text:
        return 0
    return len(re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE))


def _tail_file(path: str, lines: int = 4000, timeout_sec: int = 3) -> str:
    try:
        p = subprocess.run(["tail", "-n", str(lines), path], capture_output=True, text=True, timeout=timeout_sec)
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout or ""


def _ps_console_processes(timeout_sec: int = 4) -> list[dict[str, Any]]:
    try:
        p = subprocess.run(
            ["ps", "-eo", "pid=,etimes=,args="],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    for raw in (p.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            elapsed = int(parts[1])
        except Exception:
            continue
        args = str(parts[2] or "").strip()
        lower = args.lower()
        if "php" not in lower or "console" not in lower:
            continue
        if "mautic:" not in lower and "messenger:" not in lower and "pagehit:" not in lower:
            continue
        out.append({"pid": pid, "elapsed_sec": elapsed, "args": args})
    out.sort(key=lambda row: int(row.get("elapsed_sec", 0) or 0), reverse=True)
    return out


def _shadow_running_tasks(cfg: AgentConfig | None) -> dict[str, Any]:
    if cfg is None:
        return {"tracked_total": 0, "duplicate_task_keys": 0, "by_type": {}, "sample": []}
    db_path = str(getattr(cfg, "state_db_path", "") or "").strip()
    if not db_path:
        return {"tracked_total": 0, "duplicate_task_keys": 0, "by_type": {}, "sample": []}
    path = Path(db_path)
    if not path.exists():
        return {"tracked_total": 0, "duplicate_task_keys": 0, "by_type": {}, "sample": []}
    try:
        conn = sqlite3.connect(str(path), timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, root, task_key, task_type, entity_id, pid, command_str, started_at
            FROM tasks
            WHERE state='running'
            ORDER BY id ASC
            """
        ).fetchall()
    except Exception:
        return {"tracked_total": 0, "duplicate_task_keys": 0, "by_type": {}, "sample": []}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    by_type: dict[str, int] = {}
    key_counts: dict[str, int] = {}
    sample: list[dict[str, Any]] = []
    for row in rows:
        task_type = str(row["task_type"] or "").strip() or "unknown"
        by_type[task_type] = int(by_type.get(task_type, 0) or 0) + 1
        task_key = str(row["task_key"] or "").strip()
        if task_key:
            key_counts[task_key] = int(key_counts.get(task_key, 0) or 0) + 1
        if len(sample) < 20:
            sample.append(
                {
                    "id": int(row["id"] or 0),
                    "root": str(row["root"] or "").strip(),
                    "task_type": task_type,
                    "entity_id": int(row["entity_id"]) if row["entity_id"] is not None else None,
                    "pid": int(row["pid"] or 0),
                    "started_at": float(row["started_at"] or 0.0),
                }
            )
    duplicate_task_keys = sum(1 for count in key_counts.values() if int(count or 0) > 1)
    return {
        "tracked_total": len(rows),
        "duplicate_task_keys": duplicate_task_keys,
        "by_type": by_type,
        "sample": sample,
    }


def _read_meminfo_kib() -> dict[str, int]:
    p = Path("/proc/meminfo")
    if not p.exists():
        return {}
    out: dict[str, int] = {}
    try:
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in raw:
                continue
            key, rest = raw.split(":", 1)
            m = re.search(r"(\d+)", rest)
            if not m:
                continue
            out[key.strip()] = int(m.group(1))
    except Exception:
        return {}
    return out


def _swap_signal() -> dict[str, Any]:
    mem = _read_meminfo_kib()
    total_kib = int(mem.get("SwapTotal", 0) or 0)
    free_kib = int(mem.get("SwapFree", 0) or 0)
    used_kib = max(0, total_kib - free_kib)
    total_mb = int(total_kib // 1024)
    used_mb = int(used_kib // 1024)
    used_pct = 0.0
    if total_kib > 0:
        used_pct = (float(used_kib) / float(total_kib)) * 100.0
    level = 0
    if total_mb > 0:
        if used_pct >= 80.0 or used_mb >= 12_288:
            level = 2
        elif used_pct >= 50.0 or used_mb >= 4_096:
            level = 1
    return {
        "level": level,
        "used_mb": used_mb,
        "total_mb": total_mb,
        "used_pct": round(used_pct, 2),
    }


def _filesystem_signal() -> dict[str, Any]:
    roots = ["/", "/var", "/var/www", "/var/lib/mysql"]
    out: dict[str, dict[str, Any]] = {}
    seen_mounts: set[str] = set()
    for raw in roots:
        p = Path(raw)
        if not p.exists():
            continue
        try:
            usage = shutil.disk_usage(str(p))
        except Exception:
            continue
        mount = raw
        device = ""
        try:
            proc = subprocess.run(["df", "-P", str(p)], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                lines = [x for x in (proc.stdout or "").splitlines() if x.strip()]
                if len(lines) >= 2:
                    cols = lines[-1].split()
                    if len(cols) >= 6:
                        device = cols[0]
                        mount = cols[5]
        except Exception:
            pass
        key = str(mount or raw)
        if key in seen_mounts:
            continue
        seen_mounts.add(key)
        total = int(usage.total)
        used = int(usage.used)
        free = int(usage.free)
        used_pct = (float(used) / float(total)) * 100.0 if total > 0 else 0.0
        out[key] = {
            "path": raw,
            "mount": mount,
            "device": device,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "used_pct": round(used_pct, 2),
        }
    worst = 0.0
    for item in out.values():
        try:
            worst = max(worst, float(item.get("used_pct", 0.0) or 0.0))
        except Exception:
            continue
    level = 0
    if worst >= 97.0:
        level = 4
    elif worst >= 92.0:
        level = 3
    elif worst >= 85.0:
        level = 2
    elif worst >= 75.0:
        level = 1
    return {"level": level, "worst_used_pct": round(worst, 2), "filesystems": out}


def _parse_nginx_access_ts(line: str) -> datetime | None:
    # Example: [23/Feb/2026:11:00:02 +0000]
    m = re.search(r"\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+\-]\d{4})\]", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S %z")
    except Exception:
        return None


def _parse_nginx_error_ts(line: str) -> datetime | None:
    # Example: 2026/02/23 11:00:02
    m = re.match(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        local_tz = datetime.now().astimezone().tzinfo
        return datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S").replace(tzinfo=local_tz)
    except Exception:
        return None


def _count_nginx_file_signals(window_min: int) -> tuple[int, int]:
    now = datetime.now().astimezone()
    cutoff = now.timestamp() - (max(1, int(window_min)) * 60)

    access_paths = ["/var/log/nginx/access.log", "/var/log/nginx/access.log.1"]
    error_paths = ["/var/log/nginx/error.log", "/var/log/nginx/error.log.1"]

    http_5xx = 0
    web_critical = 0
    for ap in access_paths:
        if not Path(ap).exists():
            continue
        for line in _tail_file(ap, lines=4000).splitlines():
            ts = _parse_nginx_access_ts(line)
            if not ts or ts.timestamp() < cutoff:
                continue
            if re.search(r'"\s(50[0-9]|52[0-9])\s', line):
                http_5xx += 1

    web_err_pat = re.compile(
        r"while reading response header from upstream|upstream timed out|no live upstreams|connect\(\) failed|php message",
        flags=re.IGNORECASE,
    )
    for ep in error_paths:
        if not Path(ep).exists():
            continue
        for line in _tail_file(ep, lines=4000).splitlines():
            ts = _parse_nginx_error_ts(line)
            if not ts or ts.timestamp() < cutoff:
                continue
            if web_err_pat.search(line):
                web_critical += 1

    return http_5xx, web_critical


def collect_signals(window_min: int = 15, cfg: AgentConfig | None = None) -> dict[str, object]:
    window = max(1, min(1440, int(window_min)))
    since = f"-{window} min"
    console_rows = _ps_console_processes()
    php_stuck_sec = max(60, int(getattr(cfg, "php_console_stuck_sec", 1800) or 1800))
    console_stuck = [row for row in console_rows if int(row.get("elapsed_sec", 0) or 0) >= php_stuck_sec]
    scheduler_shadow = _shadow_running_tasks(cfg)
    tracked_total = int(scheduler_shadow.get("tracked_total", 0) or 0)
    duplicate_task_keys = int(scheduler_shadow.get("duplicate_task_keys", 0) or 0)
    live_console_total = len(console_rows)
    scheduler_drift = max(0, tracked_total - live_console_total)
    swap_state = _swap_signal()
    fs_state = _filesystem_signal()

    kernel = _run_journal(["-k", "--since", since, "-n", "2000"])
    mysql = "\n".join(
        [
            _run_journal(["-u", "mysql", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "mariadb", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "mysqld", "--since", since, "-n", "1200"]),
        ]
    )
    php_units = _detect_php_fpm_units()
    if not php_units:
        php_units = ["php-fpm.service"]
    php_fpm = "\n".join(
        _run_journal(["-u", unit, "--since", since, "-n", "1200"]) for unit in php_units
    )
    web = "\n".join(
        [
            _run_journal(["-u", "nginx", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "apache2", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "httpd", "--since", since, "-n", "1200"]),
        ]
    )
    file_http_5xx, file_web_critical = _count_nginx_file_signals(window)

    totals = {
        "oom_kill": _count(kernel, r"oom-kill|out of memory|killed process \d+"),
        "mysql_critical": _count(
            mysql,
            r"table .* marked as crashed|lost connection to mysql server|innodb: .*?(fatal|assert|corrupt)|segmentation fault|signal \d+",
        ),
        "php_fpm_max_children": _count(php_fpm, r"server reached pm\.max_children"),
        "http_5xx": _count(web, r"\b(50[0-9]|52[0-9])\b") + file_http_5xx,
        "web_critical": file_web_critical,
        "scheduler_state_drift": scheduler_drift,
        "scheduler_duplicate_task_keys": duplicate_task_keys,
        "php_console_stuck": len(console_stuck),
        "swap_pressure_level": int(swap_state.get("level", 0) or 0),
        "disk_pressure_level": int(fs_state.get("level", 0) or 0),
    }

    comp = {
        "kernel": {"oom_kill": totals["oom_kill"]},
        "database": {"mysql_critical": totals["mysql_critical"]},
        "php_fpm": {"max_children": totals["php_fpm_max_children"]},
        "web": {"http_5xx": totals["http_5xx"], "critical_errors": totals["web_critical"]},
        "scheduler": {
            "tracked_total": tracked_total,
            "live_console": live_console_total,
            "drift": scheduler_drift,
            "duplicate_task_keys": duplicate_task_keys,
        },
        "runtime": {
            "php_console_stuck": len(console_stuck),
            "swap_pressure_level": int(swap_state.get("level", 0) or 0),
        },
        "disk": {
            "pressure_level": int(fs_state.get("level", 0) or 0),
            "worst_used_pct": fs_state.get("worst_used_pct", 0.0),
        },
    }
    scheduler_level = 0
    if scheduler_drift >= 100 or duplicate_task_keys >= 20:
        scheduler_level = 4
    elif scheduler_drift >= 20 or duplicate_task_keys >= 5:
        scheduler_level = 3
    elif scheduler_drift > 0 or duplicate_task_keys > 0:
        scheduler_level = 2
    runtime_level = 0
    if len(console_stuck) >= 8:
        runtime_level = max(runtime_level, 3)
    elif console_stuck:
        runtime_level = max(runtime_level, 1)
    runtime_level = max(runtime_level, int(swap_state.get("level", 0) or 0))
    levels = {
        "kernel": min(5, totals["oom_kill"]),
        "database": min(5, totals["mysql_critical"]),
        "php_fpm": min(5, (totals["php_fpm_max_children"] // 2) + (1 if totals["php_fpm_max_children"] > 0 else 0)),
        "web": min(5, (totals["http_5xx"] // 10 + (1 if totals["http_5xx"] > 0 else 0)) + (1 if totals["web_critical"] > 0 else 0)),
        "scheduler": scheduler_level,
        "runtime": runtime_level,
        "disk": int(fs_state.get("level", 0) or 0),
    }
    overall = max(levels.values()) if levels else 0
    status = "ok" if overall == 0 else ("warn" if overall <= 2 else "critical")

    payload: dict[str, object] = {
        "window_min": window,
        "collected_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall": {"level": overall, "status": status},
        "components": {
            "kernel": {"level": levels["kernel"], "signals": comp["kernel"]},
            "database": {"level": levels["database"], "signals": comp["database"]},
            "php_fpm": {"level": levels["php_fpm"], "signals": comp["php_fpm"]},
            "web": {"level": levels["web"], "signals": comp["web"]},
            "scheduler": {"level": levels["scheduler"], "signals": comp["scheduler"]},
            "runtime": {"level": levels["runtime"], "signals": comp["runtime"]},
            "disk": {"level": levels["disk"], "signals": comp["disk"]},
        },
        "totals": totals,
    }
    payload["details"] = {
        "scheduler": {
            "tracked_total": tracked_total,
            "live_console": live_console_total,
            "drift": scheduler_drift,
            "duplicate_task_keys": duplicate_task_keys,
            "by_type": scheduler_shadow.get("by_type", {}),
            "sample": scheduler_shadow.get("sample", []),
        },
        "php_console_recent": console_rows[:20],
        "swap": swap_state,
        "filesystems": fs_state.get("filesystems", {}),
    }
    return payload


def format_signals_text(payload: dict[str, object]) -> str:
    overall = payload.get("overall", {}) if isinstance(payload, dict) else {}
    totals = payload.get("totals", {}) if isinstance(payload, dict) else {}
    lines = [
        f"status={overall.get('status', '-')}",
        f"level={overall.get('level', '-')}",
        f"window_min={payload.get('window_min', '-')}",
        f"collected_at_utc={payload.get('collected_at_utc', '-')}",
    ]
    if isinstance(totals, dict):
        for k in (
            "oom_kill",
            "mysql_critical",
            "php_fpm_max_children",
            "http_5xx",
            "web_critical",
            "scheduler_state_drift",
            "scheduler_duplicate_task_keys",
            "php_console_stuck",
            "swap_pressure_level",
            "disk_pressure_level",
        ):
            lines.append(f"{k}={int(totals.get(k, 0) or 0)}")
    return "\n".join(lines)


def format_signals_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
