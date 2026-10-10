from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import subprocess
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from mcd_agent.models import MauticInstall

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


MANAGED_KEYWORDS = (
    "mautic:segments:update",
    "mautic:segment:update",
    "mautic:campaigns:update",
    "mautic:campaigns:trigger",
    "mautic:campaign:trigger",
    "mautic:campaigns:rebuild",
    "mautic:campaign:rebuild",
    "mautic:import",
    "doctrine:query:sql",
    "cache:clear",
    "cache:warm",
    "cache:warmup",
    "mautic:email:fetch",
    "mautic:emails:fetch",
)

EMAIL_SEND_KEYWORDS = ("mautic:emails:send",)
# Mautic 4 has two queued email sender forms in supported deployments.  The
# legacy spool command remains common on installed Mautic 4 roots, while the
# documented messenger consumer is used by queue-based installations.  These
# commands must never be treated as a Mautic 5+ sender migration.
MAUTIC4_QUEUED_EMAIL_SEND_KEYWORDS = (
    "mautic:emails:send",
    "messenger:consume email_transport",
    "messenger:consume email",
)
MESSAGE_QUEUE_SEND_KEYWORDS = ("mautic:messages:send",)

_MAX_CRON_WRAPPER_BYTES = 128 * 1024
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./-])(/[A-Za-z0-9_@%+=:,./-]+)")
_CONFIRMED_MAJOR_CACHE: dict[tuple[object, ...], int] = {}


@dataclass
class ModeResult:
    ok: bool
    lines: list[str]


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


def _is_managed_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    if "bin/console" in s:
        return any(k in s for k in MANAGED_KEYWORDS)
    return _cron_wrapper_has_managed_job(s)


def _cron_wrapper_has_managed_job(line: str) -> bool:
    return _cron_wrapper_has_keywords(line, MANAGED_KEYWORDS)


def _cron_wrapper_has_keywords(line: str, keywords: tuple[str, ...]) -> bool:
    for match in _ABSOLUTE_PATH_RE.finditer(line):
        token = match.group(1).rstrip(";|&)")
        path = Path(token)
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file() or st.st_size <= 0 or st.st_size > _MAX_CRON_WRAPPER_BYTES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "bin/console" not in content:
            continue
        if any(k in content for k in keywords):
            return True
    return False


def _is_direct_managed_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return "bin/console" in s and any(k in s for k in MANAGED_KEYWORDS)


def _is_mautic_email_fetch_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return "bin/console" in s and ("mautic:email:fetch" in s or "mautic:emails:fetch" in s)


def _is_mautic_email_send_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    if "bin/console" in s:
        return any(k in s for k in EMAIL_SEND_KEYWORDS)
    return _cron_wrapper_has_keywords(s, EMAIL_SEND_KEYWORDS)


def _job_payloads(line: str) -> list[str]:
    payloads = [line]
    for match in _ABSOLUTE_PATH_RE.finditer(line):
        path = Path(match.group(1).rstrip(";|&)"))
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size <= 0 or stat.st_size > _MAX_CRON_WRAPPER_BYTES:
                continue
            payloads.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return payloads


def _payload_console_majors(payload: str, confirmed_console_majors: dict[str, int]) -> set[int]:
    tokens = {match.group(1).rstrip(";|&)") for match in _ABSOLUTE_PATH_RE.finditer(payload)}
    return {int(major) for path, major in confirmed_console_majors.items() if path in tokens}


def _email_send_job_major(line: str, confirmed_console_majors: dict[str, int]) -> int | None:
    payloads = [payload for payload in _job_payloads(line) if any(key in payload for key in EMAIL_SEND_KEYWORDS)]
    if not payloads:
        return None
    majors: set[int] = set()
    for payload in payloads:
        payload_majors = _payload_console_majors(payload, confirmed_console_majors)
        if len(payload_majors) != 1:
            return None
        majors.update(payload_majors)
    return next(iter(majors)) if len(majors) == 1 else None


def _mautic4_queued_email_sender_job_major(
    line: str,
    confirmed_console_majors: dict[str, int],
) -> int | None:
    """Return the confirmed major for a Mautic 4 queued-email sender cron job.

    This deliberately excludes ``mautic:messages:send``: that command drains
    frequency-rule messages and cannot deliver a queued email transport.
    """
    payloads = [
        payload
        for payload in _job_payloads(line)
        if any(key in payload for key in MAUTIC4_QUEUED_EMAIL_SEND_KEYWORDS)
    ]
    if not payloads:
        return None
    majors: set[int] = set()
    for payload in payloads:
        payload_majors = _payload_console_majors(payload, confirmed_console_majors)
        if len(payload_majors) != 1:
            return None
        majors.update(payload_majors)
    return next(iter(majors)) if len(majors) == 1 else None


def _is_empty_leads_cleanup_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    lowered = s.lower()
    return (
        "bin/console" in lowered
        and "doctrine:query:sql" in lowered
        and "delete from" in lowered
        and "_leads" in lowered
        and "email is null" in lowered
        and "mobile is null" in lowered
    )


def _cron_fields(line: str) -> str:
    parts = line.strip().split()
    if len(parts) < 6:
        return ""
    return " ".join(parts[:5])


def _cron_interval_sec(line: str, default_sec: int = 900) -> int:
    fields = _cron_fields(line).split()
    if len(fields) != 5:
        return default_sec
    minute = fields[0]
    hour, dom, month, dow = fields[1:]
    if hour != "*" or dom != "*" or month != "*" or dow != "*":
        return default_sec
    m = re.fullmatch(r"\*/(\d+)", minute)
    if m:
        return max(60, int(m.group(1)) * 60)
    if minute == "*":
        return 60
    return default_sec


def _cron_migration_schedule(line: str, default_sec: int = 900) -> tuple[str, int, str]:
    fields = _cron_fields(line)
    if not fields:
        return "interval", int(default_sec), ""
    parts = fields.split()
    minute, hour, dom, month, dow = parts
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        m = re.fullmatch(r"\*/(\d+)", minute)
        if m:
            return "interval", max(60, int(m.group(1)) * 60), ""
        if minute == "*":
            return "interval", 60, ""
    return "cron", int(default_sec), fields


def _comment_managed(content: str, stamp: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if _is_managed_job(line):
            out.append(f"# MCD_MANAGED {stamp}: disabled by mcd profile=active")
            out.append("# " + line)
            changed += 1
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _comment_mautic_email_fetch(content: str, stamp: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if _is_mautic_email_fetch_job(line):
            out.append(f"# MCD_MANAGED {stamp}: disabled mautic email fetch by mcd monitored-email parser")
            out.append("# " + line)
            changed += 1
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _restore_mautic_email_fetch_comments(content: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    skip_next_managed = False
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("# MCD_MANAGED") and "mautic email fetch" in s and "monitored-email parser" in s:
            skip_next_managed = True
            changed += 1
            continue
        if skip_next_managed:
            if line.startswith("# "):
                legacy = line[2:]
                if _is_mautic_email_fetch_job(legacy):
                    out.append(legacy)
                    changed += 1
                    skip_next_managed = False
                    continue
            skip_next_managed = False
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _restore_mautic_email_send_comments(
    content: str,
    confirmed_console_majors: dict[str, int] | None = None,
) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    pending_marker: str | None = None
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("# MCD_MANAGED") and "disabled by mcd profile=active" in s:
            if pending_marker is not None:
                out.append(pending_marker)
            pending_marker = line
            continue
        if pending_marker is not None:
            if line.startswith("# "):
                legacy = line[2:]
                if _mautic4_queued_email_sender_job_major(
                    legacy,
                    confirmed_console_majors or {},
                ) == 4:
                    out.append(legacy)
                    changed += 1
                    pending_marker = None
                    continue
            out.append(pending_marker)
            pending_marker = None
        out.append(line)
    if pending_marker is not None:
        out.append(pending_marker)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _comment_mautic_email_send_by_version(
    content: str,
    stamp: str,
    confirmed_console_majors: dict[str, int],
) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if _email_send_job_major(line, confirmed_console_majors) in {5, 6, 7}:
            out.append(f"# MCD_MANAGED {stamp}: disabled by mcd profile=active")
            out.append("# " + line)
            changed += 1
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _reconcile_active_managed_content(
    content: str,
    stamp: str,
    confirmed_console_majors: dict[str, int] | None = None,
) -> tuple[str, int, int]:
    versions = confirmed_console_majors or {}
    restored_content, restored = _restore_mautic_email_send_comments(content, versions)
    managed_content, managed_commented = _comment_managed(restored_content, stamp)
    updated, email_commented = _comment_mautic_email_send_by_version(managed_content, stamp, versions)
    return updated, managed_commented + email_commented, restored


def _comment_empty_leads_cleanup(content: str, stamp: str) -> tuple[str, int, list[str]]:
    out: list[str] = []
    changed = 0
    interval_sec = 900
    cron_exprs: list[str] = []
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if _is_empty_leads_cleanup_job(line):
            schedule_type, parsed_interval, cron_expr = _cron_migration_schedule(line, interval_sec)
            if schedule_type == "cron" and cron_expr:
                cron_exprs.append(cron_expr)
            else:
                interval_sec = parsed_interval
            out.append(f"# MCD_MANAGED {stamp}: disabled empty leads cleanup by mcd profile=active")
            out.append("# " + line)
            changed += 1
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed, interval_sec, cron_exprs


def _managed_empty_leads_cleanup_schedules(content: str) -> tuple[int, list[str]]:
    interval_sec = 0
    cron_exprs: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("# "):
            continue
        legacy = line[2:].strip()
        if not _is_empty_leads_cleanup_job(legacy):
            continue
        schedule_type, parsed_interval, cron_expr = _cron_migration_schedule(legacy, 0)
        if schedule_type == "cron" and cron_expr:
            cron_exprs.append(cron_expr)
        elif parsed_interval > 0:
            interval_sec = max(interval_sec, parsed_interval)
    return interval_sec, cron_exprs


def _restore_managed_comments(content: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    expect_line = False
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("# MCD_MANAGED"):
            expect_line = True
            changed += 1
            continue
        if expect_line and line.startswith("# "):
            out.append(line[2:])
            changed += 1
            expect_line = False
            continue
        out.append(line)
        expect_line = False
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _ensure_backup(install_dir: str, user: str, content: str) -> Path:
    backup_dir = Path(install_dir) / "var" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"{user}.pre-active.crontab"
    if not p.exists():
        p.write_text(content, encoding="utf-8")
        os.chmod(p, 0o600)
    return p


def _restore_from_backup(install_dir: str, user: str) -> tuple[bool, str]:
    p = Path(install_dir) / "var" / "backup" / f"{user}.pre-active.crontab"
    if not p.exists():
        return False, f"backup not found for {user}: {p}"
    rc, out = _write_crontab(p.read_text(encoding="utf-8"), None if user == "root" else user)
    if rc != 0:
        return False, f"restore {user} failed: {out}"
    return True, f"restored {user} crontab from backup"


def _path_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
        return str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return None


def _confirmed_console_major_map(
    installs: Iterable[MauticInstall],
    php_bin: str,
    *,
    run_as_user: str | None = "www-data",
) -> tuple[dict[str, int], list[str]]:
    from mcd_agent.mautic_version_cache import confirmed_mautic_major

    confirmed: dict[str, int] = {}
    diagnostics: list[str] = []
    php_signature = _path_signature(Path(php_bin))
    for inst in installs:
        console = Path(str(inst.console_path or ""))
        root = Path(inst.root)
        lock_candidates = [root / "composer.lock"]
        if root.name.lower() in {"public", "docroot", "public_html"}:
            lock_candidates.append(root.parent / "composer.lock")
        cache_key = (
            str(console),
            _path_signature(console),
            tuple(sig for sig in (_path_signature(path) for path in lock_candidates) if sig is not None),
            php_signature,
            int(inst.mautic_major) if inst.mautic_major is not None else None,
            str(inst.local_php_path or ""),
        )
        major = _CONFIRMED_MAJOR_CACHE.get(cache_key)
        if major is None:
            major = confirmed_mautic_major(
                inst.root,
                php_bin,
                console_path=inst.console_path,
                local_php_path=inst.local_php_path,
                expected_major=inst.mautic_major,
                run_as_user=run_as_user,
            )
            if major is not None:
                _CONFIRMED_MAJOR_CACHE[cache_key] = major
        if major is None:
            diagnostics.append(f"{inst.name}: Mautic major is not independently confirmed; email send cron unchanged")
            continue
        try:
            resolved_console = str(console.resolve(strict=True))
            confirmed[resolved_console] = int(major)
            if console.is_absolute():
                confirmed[str(console)] = int(major)
        except OSError:
            diagnostics.append(f"{inst.name}: console path is unavailable; email send cron unchanged")
    return confirmed, diagnostics


def reconcile_managed_cron(
    *,
    profile_name: str,
    install_dir: str,
    installs: Iterable[MauticInstall] = (),
    php_bin: str = "/usr/bin/php",
    run_as_user: str | None = "www-data",
) -> ModeResult:
    """
    Idempotently keep active profiles from racing legacy Mautic cron jobs.

    Profile commands do this on explicit transitions. The daemon also calls
    this so hosts that were already active before an update converge without a
    manual profile toggle.
    """
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["managed cron reconcile requires root"])
    profile = (profile_name or "").strip().lower()
    if profile == "passive":
        return ModeResult(ok=True, lines=["passive profile, managed cron left unchanged"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    confirmed_console_majors, diagnostics = _confirmed_console_major_map(
        installs,
        php_bin,
        run_as_user=run_as_user,
    )
    lines: list[str] = list(diagnostics)
    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed, restored = _reconcile_active_managed_content(cur, stamp, confirmed_console_majors)
        if changed <= 0 and restored <= 0:
            lines.append(f"{user}: no managed cron change")
            continue
        rc2, out2 = _write_crontab(updated, None if user == "root" else user)
        if rc2 != 0:
            lines.append(f"{user}: failed to write crontab: {out2}")
            return ModeResult(ok=False, lines=lines)
        lines.append(f"{user}: commented managed cron lines={changed}; restored email spool consumers={restored}")
    return ModeResult(ok=True, lines=lines)


def _plugin_cron_line_matches(line: str, rule: dict[str, object]) -> bool:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return False
    root = str(rule.get("root", "") or "").rstrip("/")
    tokens = [str(x) for x in list(rule.get("match") or []) if str(x)]
    if not tokens:
        return False
    if all(token in text for token in tokens) and (not root or root in text):
        return True
    for match in _ABSOLUTE_PATH_RE.finditer(text):
        path = Path(match.group(1).rstrip(";|&)"))
        try:
            if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > _MAX_CRON_WRAPPER_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if all(token in content for token in tokens) and (not root or root in content):
            return True
    return False


def _plugin_cron_interval_sec(line: str) -> int:
    fields = _cron_fields(line).split()
    if len(fields) != 5:
        return 0
    minute, hour, dom, month, dow = fields
    if dom != "*" or month != "*" or dow != "*":
        return 0
    minute_step = re.fullmatch(r"\*/(\d+)", minute)
    if minute_step and hour == "*":
        return max(60, int(minute_step.group(1)) * 60)
    if minute == "*" and hour == "*":
        return 60
    if minute.isdigit() and hour == "*":
        return 3600
    hour_step = re.fullmatch(r"\*/(\d+)", hour)
    if minute.isdigit() and hour_step:
        return max(3600, int(hour_step.group(1)) * 3600)
    if minute.isdigit() and hour.isdigit():
        return 86_400
    return 0


def reconcile_plugin_operation_cron(
    *, profile_name: str, install_dir: str, rules: list[dict[str, object]]
) -> ModeResult:
    """Apply catalog-provided legacy cron rules for installed plugin operations."""
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["plugin operation cron reconcile requires root"])
    if (profile_name or "").strip().lower() == "passive":
        return ModeResult(ok=True, lines=["passive profile, plugin operation cron left unchanged"])
    safe_rules = [rule for rule in rules if isinstance(rule, dict) and str(rule.get("operation_key", "") or "")]
    if not safe_rules:
        return ModeResult(ok=True, lines=["no plugin operation cron rules"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    migrations: dict[tuple[str, str], tuple[int, str, str]] = {}
    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        out: list[str] = []
        changed = 0
        raw_lines = cur.splitlines()
        for line in raw_lines:
            matched = next((rule for rule in safe_rules if _plugin_cron_line_matches(line, rule)), None)
            if matched is None:
                out.append(line)
                continue
            op_key = str(matched.get("operation_key", "") or "")
            instance_key = str(matched.get("instance_key", "") or "")
            interval = _plugin_cron_interval_sec(line) if bool(matched.get("migrate_schedule", False)) else 0
            migrations[(instance_key, op_key)] = (
                interval,
                str(matched.get("enabled_field", "enabled") or "enabled"),
                str(matched.get("interval_field", "interval_sec") or "interval_sec"),
            )
            if str(matched.get("action", "comment") or "comment") == "remove":
                changed += 1
                continue
            out.append(f"# MCD_PLUGIN_OPERATION {op_key} {stamp}: disabled catalog-managed cron")
            out.append("# " + line)
            changed += 1
        updated = "\n".join(out) + ("\n" if cur.endswith("\n") else "")
        if changed > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
                return ModeResult(ok=False, lines=lines)
            lines.append(f"{user}: catalog plugin cron reconciled lines={changed}")
        else:
            for idx, line in enumerate(raw_lines[:-1]):
                marker = re.match(r"^# MCD_PLUGIN_OPERATION (\S+) ", line.strip())
                if not marker or not raw_lines[idx + 1].startswith("# "):
                    continue
                op_key = marker.group(1)
                rule = next((item for item in safe_rules if str(item.get("operation_key", "")) == op_key), None)
                if rule is None:
                    continue
                legacy = raw_lines[idx + 1][2:]
                interval = _plugin_cron_interval_sec(legacy) if bool(rule.get("migrate_schedule", False)) else 0
                migrations[(str(rule.get("instance_key", "") or ""), op_key)] = (
                    interval,
                    str(rule.get("enabled_field", "enabled") or "enabled"),
                    str(rule.get("interval_field", "interval_sec") or "interval_sec"),
                )
            lines.append(f"{user}: no catalog plugin cron change")
    for (instance_key, op_key), (interval, enabled_field, interval_field) in sorted(migrations.items()):
        migrated = {
            "instance_key": instance_key,
            "operation_key": op_key,
            "cron_found": True,
            "enabled_field": enabled_field,
            "interval_field": interval_field,
        }
        if interval > 0:
            migrated["interval_sec"] = interval
        lines.append(
            "MCD_PLUGIN_OPERATION_MIGRATE_JSON="
            + json.dumps(migrated, ensure_ascii=True, separators=(",", ":"))
        )
    return ModeResult(ok=True, lines=lines)


def _message_queue_cron_root(line: str, managed_roots: tuple[str, ...]) -> str | None:
    text = str(line or "").strip()
    if not text or text.startswith("#") or not managed_roots:
        return None
    if "bin/console" in text and any(token in text for token in MESSAGE_QUEUE_SEND_KEYWORDS):
        return next((root for root in managed_roots if root in text), None)
    for match in _ABSOLUTE_PATH_RE.finditer(text):
        path = Path(match.group(1).rstrip(";|&)"))
        try:
            if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > _MAX_CRON_WRAPPER_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not any(token in content for token in MESSAGE_QUEUE_SEND_KEYWORDS):
            continue
        combined = f"{text}\n{content}"
        root = next((item for item in managed_roots if item in combined), None)
        if root:
            return root
    return None


def _message_queue_marker(root: str, interval_sec: int, stamp: str) -> str:
    payload = json.dumps(
        {"root": root, "interval_sec": max(60, min(86_400, int(interval_sec or 3600)))},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"# MCD_MESSAGE_QUEUE {payload} {stamp}: disabled MCD-managed cron"


def _message_queue_marker_payload(line: str) -> dict[str, object] | None:
    match = re.match(r"^# MCD_MESSAGE_QUEUE (\{.*\}) \S+: disabled MCD-managed cron$", line.strip())
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return None
    if not isinstance(payload, dict) or not str(payload.get("root", "") or "").startswith("/"):
        return None
    return payload


def _comment_mautic_message_queue_cron(
    content: str,
    stamp: str,
    managed_roots: tuple[str, ...],
) -> tuple[str, int, dict[str, int]]:
    out: list[str] = []
    changed = 0
    migrations: dict[str, int] = {}
    for line in content.splitlines():
        root = _message_queue_cron_root(line, managed_roots)
        if root is None:
            out.append(line)
            continue
        interval_sec = _plugin_cron_interval_sec(line) or 3600
        interval_sec = max(60, min(86_400, interval_sec))
        migrations[root] = min(migrations.get(root, interval_sec), interval_sec)
        out.append(_message_queue_marker(root, interval_sec, stamp))
        out.append("# " + line)
        changed += 1
    updated = "\n".join(out) + ("\n" if content.endswith("\n") else "")
    return updated, changed, migrations


def _managed_mautic_message_queue_migrations(content: str) -> dict[str, int]:
    migrations: dict[str, int] = {}
    for line in content.splitlines():
        payload = _message_queue_marker_payload(line)
        if payload is None:
            continue
        root = str(payload.get("root", "") or "")
        try:
            interval_sec = max(60, min(86_400, int(payload.get("interval_sec", 3600) or 3600)))
        except (TypeError, ValueError):
            interval_sec = 3600
        migrations[root] = min(migrations.get(root, interval_sec), interval_sec)
    return migrations


def _restore_mautic_message_queue_cron(content: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    restore_next = False
    for line in content.splitlines():
        if _message_queue_marker_payload(line) is not None:
            restore_next = True
            changed += 1
            continue
        if restore_next and line.startswith("# "):
            out.append(line[2:])
            restore_next = False
            changed += 1
            continue
        restore_next = False
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def reconcile_mautic_message_queue_cron(
    *,
    profile_name: str,
    install_dir: str,
    managed_roots: list[str],
) -> ModeResult:
    """Move Mautic 5/6/7 message queue cron into per-instance MCD scheduling."""
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["mautic message queue cron reconcile requires root"])
    roots = tuple(
        sorted(
            {str(Path(root).resolve()) for root in managed_roots if str(root or "").strip()},
            key=len,
            reverse=True,
        )
    )
    profile = (profile_name or "").strip().lower()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    migrations: dict[str, int] = {}
    for user in ("root", "www-data"):
        rc, current = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        if profile == "passive":
            updated, changed = _restore_mautic_message_queue_cron(current)
            action = "restored mautic message queue cron lines"
        else:
            _ensure_backup(install_dir, user, current)
            updated, changed, discovered = _comment_mautic_message_queue_cron(current, stamp, roots)
            action = "commented mautic message queue cron lines"
            if not discovered:
                discovered = _managed_mautic_message_queue_migrations(current)
            for root, interval_sec in discovered.items():
                migrations[root] = min(migrations.get(root, interval_sec), interval_sec)
        if changed <= 0:
            lines.append(f"{user}: no mautic message queue cron change")
            continue
        rc2, output = _write_crontab(updated, None if user == "root" else user)
        if rc2 != 0:
            lines.append(f"{user}: failed to write crontab: {output}")
            return ModeResult(ok=False, lines=lines)
        lines.append(f"{user}: {action}={changed}")
    for root, interval_sec in sorted(migrations.items()):
        lines.append(
            "MCD_MESSAGE_QUEUE_MIGRATE_JSON="
            + json.dumps(
                {"root": root, "enabled": True, "interval_sec": interval_sec},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    return ModeResult(ok=True, lines=lines)


def reconcile_mautic_email_fetch_cron(*, profile_name: str, install_dir: str, enabled: bool) -> ModeResult:
    """
    Keep legacy mautic:email:fetch cron from racing the MCD monitored-email parser.
    """
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["mautic email fetch cron reconcile requires root"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    profile = (profile_name or "").strip().lower()
    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        if not enabled:
            updated, changed = _restore_mautic_email_fetch_comments(cur)
            if changed <= 0:
                lines.append(f"{user}: no mautic email fetch cron restore")
                continue
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
                return ModeResult(ok=False, lines=lines)
            lines.append(f"{user}: restored mautic email fetch cron lines={changed}")
            continue
        if profile == "passive":
            lines.append(f"{user}: passive profile, mautic email fetch cron left unchanged")
            continue
        updated, changed = _comment_mautic_email_fetch(cur, stamp)
        if changed <= 0:
            lines.append(f"{user}: no mautic email fetch cron change")
            continue
        rc2, out2 = _write_crontab(updated, None if user == "root" else user)
        if rc2 != 0:
            lines.append(f"{user}: failed to write crontab: {out2}")
        else:
            lines.append(f"{user}: commented mautic email fetch cron lines={changed}")
    return ModeResult(ok=True, lines=lines)


def reconcile_empty_leads_cleanup_cron(*, profile_name: str, install_dir: str) -> ModeResult:
    """
    Move legacy direct SQL cleanup cron into MCD-owned scheduling.

    Only the safe legacy shape is migrated:
    DELETE FROM <prefix>leads WHERE email IS NULL AND mobile IS NULL
    """
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["empty leads cleanup cron reconcile requires root"])
    profile = (profile_name or "").strip().lower()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    migrated_interval = 0
    migrated_cron_expr = ""
    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        if profile == "passive":
            lines.append(f"{user}: passive profile, empty leads cleanup cron left unchanged")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed, interval_sec, cron_exprs = _comment_empty_leads_cleanup(cur, stamp)
        if changed <= 0:
            managed_interval, managed_crons = _managed_empty_leads_cleanup_schedules(cur)
            if managed_crons:
                migrated_cron_expr = managed_crons[-1]
                lines.append(f"{user}: existing managed empty leads cleanup cron_expr='{migrated_cron_expr}'")
            elif managed_interval > 0:
                migrated_interval = max(migrated_interval, managed_interval)
                lines.append(f"{user}: existing managed empty leads cleanup interval_sec={managed_interval}")
            else:
                lines.append(f"{user}: no empty leads cleanup cron change")
            continue
        rc2, out2 = _write_crontab(updated, None if user == "root" else user)
        if rc2 != 0:
            lines.append(f"{user}: failed to write crontab: {out2}")
            return ModeResult(ok=False, lines=lines)
        migrated_interval = max(migrated_interval, int(interval_sec))
        if cron_exprs:
            migrated_cron_expr = cron_exprs[-1]
            lines.append(
                f"{user}: commented empty leads cleanup cron lines={changed} cron_expr='{migrated_cron_expr}'"
            )
        else:
            lines.append(f"{user}: commented empty leads cleanup cron lines={changed} interval_sec={interval_sec}")
    if migrated_cron_expr:
        lines.append(f"MCD_EMPTY_LEADS_MIGRATE schedule_type=cron cron_expr='{migrated_cron_expr}' mode=both_null")
    elif migrated_interval > 0:
        lines.append(f"MCD_EMPTY_LEADS_MIGRATE schedule_type=interval interval_sec={migrated_interval} mode=both_null")
    return ModeResult(ok=True, lines=lines)


def _profile_file(install_dir: str) -> Path:
    return Path(install_dir) / "var" / "last-active-profile.txt"


def _normalize_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _resolve_mutable_config_path(config_path: str) -> Path:
    """
    Resolve effective writable config file.

    In split-layout entrypoint mode (`mcd.toml` + include files), profile/runtime
    overrides must be edited in local override file (usually `/opt/mcd/etc/mcd.local.toml`)
    and not in package-managed entrypoint.
    """
    p = Path(config_path)
    if not p.exists():
        return p
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return p
    if "MCD_CONFIG_ENTRYPOINT v1" not in text:
        return p
    try:
        raw = tomllib.loads(text)
    except Exception:
        return p
    include = raw.get("include", {})
    files_raw: object = include.get("files", []) if isinstance(include, dict) else []
    files = _normalize_list(files_raw)
    resolved: list[Path] = []
    for item in files:
        fp = Path(item)
        if not fp.is_absolute():
            fp = (p.parent / fp).resolve()
        if fp != p:
            resolved.append(fp)
    for fp in reversed(resolved):
        if fp.exists():
            return fp
    if resolved:
        return resolved[-1]
    return p


def _read_profile_name(config_path: str) -> str:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return "custom"
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\[profile\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        return "custom"
    body = m.group(1)
    n = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', body)
    if n:
        return n.group(1).strip().lower()
    return "custom"


def _write_profile_name(config_path: str, name: str) -> None:
    p = _resolve_mutable_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    section = f'[profile]\nname = "{name}"\n\n'
    m = re.search(r"(?ms)^\[profile\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        p.write_text(section + text, encoding="utf-8")
        return
    body = m.group(1)
    if re.search(r'(?m)^\s*name\s*=\s*"[^"]+"', body):
        body2 = re.sub(r'(?m)^\s*name\s*=\s*"[^"]+"', f'name = "{name}"', body, count=1)
    else:
        body2 = f'name = "{name}"\n' + body
    text2 = text[: m.start(1)] + body2 + text[m.end(1) :]
    p.write_text(text2, encoding="utf-8")


def _restart_service(service: str = "mcd") -> tuple[bool, str]:
    proc = subprocess.run(["systemctl", "restart", service], capture_output=True, text=True)
    if proc.returncode == 0:
        return True, f"service restarted: {service}"
    out = (proc.stdout or proc.stderr or "").strip()
    return False, f"service restart failed ({service}): {out}"


def mode_status(*, pause_flag_path: str, config_path: str) -> ModeResult:
    profile = _read_profile_name(config_path)
    paused = Path(pause_flag_path).exists()
    effective_path = _resolve_mutable_config_path(config_path)
    return ModeResult(
        ok=True,
        lines=[
            f"profile={profile}",
            f"pause_flag={pause_flag_path}",
            f"pause_flag_exists={str(paused).lower()}",
            f"config_path={config_path}",
            f"config_mutable_path={effective_path}",
        ],
    )


def mode_activate(*, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile activate requires root"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    target_profile = current_profile
    if current_profile == "passive":
        pf = _profile_file(install_dir)
        if pf.exists():
            target_profile = (pf.read_text(encoding="utf-8").strip() or "tiny").lower()
        else:
            target_profile = "tiny"
    _write_profile_name(config_path, target_profile)
    lines.append(f"profile set: {target_profile}")

    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed, restored = _reconcile_active_managed_content(cur, stamp)
        if changed > 0 or restored > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
            else:
                lines.append(
                    f"{user}: commented managed cron lines={changed}; restored email spool consumers={restored}"
                )
        else:
            lines.append(f"{user}: no managed cron lines found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)
    return ModeResult(ok=True, lines=lines)


def mode_passive(*, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile passive requires root"])

    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    if current_profile != "passive":
        pf = _profile_file(install_dir)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(current_profile, encoding="utf-8")
        lines.append(f"saved previous profile: {current_profile}")
    _write_profile_name(config_path, "passive")
    lines.append("profile set: passive")

    for user in ("root", "www-data"):
        ok, msg = _restore_from_backup(install_dir, user)
        if ok:
            lines.append(msg)
            continue
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        updated, changed = _restore_managed_comments(cur)
        if changed > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed restore from markers: {out2}")
            else:
                lines.append(f"{user}: restored managed cron lines from markers={changed}")
        else:
            lines.append(f"{user}: no managed markers found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)

    return ModeResult(ok=True, lines=lines)


SUPPORTED_PROFILE_NAMES = (
    "passive",
    "tiny",
    "mini",
    "midi",
    "maxi",
    "hiload",
    "ultra",
    "farm-tiny",
    "farm-mini",
    "farm-midi",
    "farm-maxi",
    "farm-hiload",
    "farm-ultra",
    "custom",
)

# Runtime keys controlled by named profiles.
# If these remain in [runtime], they override profile baseline and can cause
# profile drift after any config/deploy reconciliation.
PROFILE_MANAGED_RUNTIME_KEYS = (
    "command_timeout_sec",
    "worker_watchdog_sec",
    "ring_mode",
    "disable_throttle",
    "disable_whitelist",
    "enable_import_polling",
    "import_stall_timeout_sec",
    "enable_campaign_rebuild",
    "segment_priority_weight_threshold",
    "segment_priority_size",
    "campaign_priority_size",
    "campaign_latest_priority_count",
    "segment_priority_parallel_idle",
    "segment_regular_parallel_idle",
    "segment_priority_parallel_throttled",
    "segment_regular_parallel_throttled",
    "segment_throttle_whitelist_only",
    "segment_throttle_whitelist_parallel",
    "segment_throttle_kill_non_whitelist",
    "queue_throttle_threshold",
    "queue_throttle_window_min",
    "campaign_total_parallel",
    "campaign_update_priority_parallel",
    "campaign_update_regular_parallel",
    "campaign_trigger_priority_parallel",
    "campaign_trigger_regular_parallel",
    "campaign_rebuild_priority_parallel",
    "campaign_rebuild_regular_parallel",
    "scheduler_host_max_parallel",
    "scheduler_elastic_slots_enabled",
    "scheduler_emergency_reserved_slots",
    "scheduler_instance_max_parallel",
    "scheduler_fairness_watchdog_sec",
)

# Legacy keys used by old config revisions; they are ignored by current runtime
# model and can create operator confusion after passive->active transitions.
LEGACY_RUNTIME_KEYS = (
    "max_parallel_campaigns",
    "max_parallel_segments_idle",
    "max_parallel_segments_active",
    "max_parallel_segments_non_whitelist_active",
    "segment_non_whitelist_policy",
)

# Keep SQL selection defaults owned by current agent runtime when host exits
# passive mode and switches to an active profile.
PROFILE_DEFAULT_SQL_KEYS = (
    "mail_queue_count",
    "segments_due",
    "segment_weights",
    "campaigns_due",
    "import_pending_count",
)


def _clear_profile_runtime_overrides(config_path: str) -> int:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^(\[runtime\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return 0
    header = m.group(1)
    body = m.group(2)
    removed = 0
    out_lines: list[str] = []
    key_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    for line in body.splitlines(keepends=True):
        km = key_re.match(line)
        if km and km.group(1) in PROFILE_MANAGED_RUNTIME_KEYS:
            removed += 1
            continue
        out_lines.append(line)
    if removed == 0:
        return 0
    new_body = "".join(out_lines)
    text2 = text[: m.start()] + header + new_body + text[m.end() :]
    p.write_text(text2, encoding="utf-8")
    return removed


def _clear_section_keys(config_path: str, section: str, keys: tuple[str, ...]) -> int:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^(\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return 0
    header = m.group(1)
    body = m.group(2)
    key_set = set(keys)
    removed = 0
    out_lines: list[str] = []
    key_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    for line in body.splitlines(keepends=True):
        km = key_re.match(line)
        if km and km.group(1) in key_set:
            removed += 1
            continue
        out_lines.append(line)
    if removed == 0:
        return 0
    text2 = text[: m.start()] + header + "".join(out_lines) + text[m.end() :]
    p.write_text(text2, encoding="utf-8")
    return removed


def _sanitize_on_passive_exit(config_path: str) -> tuple[int, int]:
    removed_legacy_runtime = _clear_section_keys(config_path, "runtime", LEGACY_RUNTIME_KEYS)
    removed_sql_overrides = _clear_section_keys(config_path, "sql", PROFILE_DEFAULT_SQL_KEYS)
    return removed_legacy_runtime, removed_sql_overrides


def profile_status(*, pause_flag_path: str, config_path: str) -> ModeResult:
    return mode_status(pause_flag_path=pause_flag_path, config_path=config_path)


def profile_set(*, profile: str, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    name = (profile or "").strip().lower()
    if name == "passive":
        res = mode_passive(
            pause_flag_path=pause_flag_path,
            install_dir=install_dir,
            config_path=config_path,
        )
        if res.ok:
            removed = _clear_profile_runtime_overrides(config_path)
            if removed:
                res.lines.append(f"runtime overrides cleared for profile baseline: {removed}")
        return res
    if name not in SUPPORTED_PROFILE_NAMES:
        return ModeResult(ok=False, lines=[f"unsupported profile: {profile}"])

    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile apply requires root"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    was_passive = current_profile == "passive"
    if was_passive:
        pf = _profile_file(install_dir)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(name, encoding="utf-8")
    _write_profile_name(config_path, name)
    lines.append(f"profile set: {name}")
    if name != "custom":
        removed = _clear_profile_runtime_overrides(config_path)
        if removed:
            lines.append(f"runtime overrides cleared for profile baseline: {removed}")
        if was_passive:
            removed_legacy_runtime, removed_sql_overrides = _sanitize_on_passive_exit(config_path)
            if removed_legacy_runtime:
                lines.append(f"legacy runtime keys cleared on passive exit: {removed_legacy_runtime}")
            if removed_sql_overrides:
                lines.append(f"legacy sql overrides cleared on passive exit: {removed_sql_overrides}")

    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed, restored = _reconcile_active_managed_content(cur, stamp)
        if changed > 0 or restored > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
            else:
                lines.append(
                    f"{user}: commented managed cron lines={changed}; restored email spool consumers={restored}"
                )
        else:
            lines.append(f"{user}: no managed cron lines found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)
    return ModeResult(ok=True, lines=lines)
