from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
from urllib import parse, request

from mcd_agent.config import AgentConfig
from mcd_agent.custom_scripts import run_configured_external_operation
from mcd_agent.service_profiles import _decode_configured_operation_result


CONFIG_ROOT = Path("/etc/mcd/local-mail")
STATE_ROOT = Path("/var/lib/mcd/local-mail")
DOMAINS_PATH = CONFIG_ROOT / "domains.json"
QUOTA_DB_PATH = STATE_ROOT / "quota.sqlite3"
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SENDMAIL_ISOLATED_PORT = 2525
_MAIL_TEST_SCRIPT = r"""<?php
require $argv[2];
$included = include $argv[1];
if (is_array($included)) {
    $params = $included;
} elseif (isset($parameters) && is_array($parameters)) {
    $params = $parameters;
} else {
    $params = [];
}
if (isset($params['parameters']) && is_array($params['parameters'])) { $params = $params['parameters']; }
$dsn = (string)($params['mailer_dsn'] ?? '');
$dsn = str_replace('%%', '%', $dsn);
$from = (string)($params['mailer_from_email'] ?? '');
$fromName = (string)($params['mailer_from_name'] ?? 'Sales Snap');
$returnPath = (string)($params['mailer_return_path'] ?? '');
if ($dsn === '' || $from === '') { fwrite(STDERR, "mailer_dsn or mailer_from_email is missing\n"); exit(2); }
$transport = Symfony\Component\Mailer\Transport::fromDsn($dsn);
$mailer = new Symfony\Component\Mailer\Mailer($transport);
$email = (new Symfony\Component\Mime\Email())
    ->from(new Symfony\Component\Mime\Address($from, $fromName))
    ->to($argv[3])
    ->subject('Sales Snap mail configuration test')
    ->text('Mail configuration "'.$argv[4].'" was applied and tested successfully by MCC/MCD.');
if ($returnPath !== '') { $email->returnPath($returnPath); }
$mailer->send($email);
echo "test message accepted\n";
"""


def _configured_mail_os_operation(
    cfg: AgentConfig,
    *,
    operation: str,
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    """Run an MCC-owned mail OS operation through MCD's existing seam."""
    code, output = run_configured_external_operation(
        cfg,
        component="mail",
        operation=operation,
        profile={},
        operation_input=runtime,
    )
    if code == 2 and output == "custom_operation_binding_not_configured":
        return None
    return _decode_configured_operation_result(code, output)


def _mail_identity_runtime(
    *,
    mta: str,
    mail_hostname: str,
    domains: list[str],
    selector: str,
) -> dict[str, Any]:
    return {
        "mta": str(mta),
        "mail_hostname": str(mail_hostname),
        "domains": sorted(str(name) for name in domains),
        "selector": str(selector),
    }


def _mail_key_runtime(
    *,
    mta: str,
    mail_hostname: str,
    domains: list[str],
    selector: str,
    key_domain: str,
    private_key_pem: str | None = None,
) -> dict[str, Any]:
    runtime = _mail_identity_runtime(
        mta=mta,
        mail_hostname=mail_hostname,
        domains=domains,
        selector=selector,
    )
    runtime["key_domain"] = str(key_domain)
    if private_key_pem is not None:
        runtime["private_key_pem"] = str(private_key_pem)
    return runtime


def _mail_submit_runtime(
    *,
    mta: str,
    mail_hostname: str,
    domains: list[str],
    selector: str,
) -> dict[str, Any]:
    return _mail_identity_runtime(
        mta=mta,
        mail_hostname=mail_hostname,
        domains=domains,
        selector=selector,
    )


def _resolved_submit_command(result: dict[str, Any] | None) -> str:
    """Read the non-secret submit executable capability returned by MCC."""
    value = result.get("submit_command") if isinstance(result, dict) else None
    command = str(value or "").strip()
    if not command or any(ord(char) < 32 or ord(char) == 127 for char in command):
        raise RuntimeError("mail_os_submit_interface_command_invalid")
    return command


def _run(args: list[str], *, timeout_sec: int = 300, input_bytes: bytes | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        input=input_bytes,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    output = (proc.stdout or b"") + (proc.stderr or b"")
    return int(proc.returncode), output.decode("utf-8", errors="replace").strip()


def _domain(raw: str) -> str:
    value = str(raw or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(value):
        raise RuntimeError(f"invalid instance domain: {raw}")
    return value


def _host_name(cfg: AgentConfig) -> str:
    return str(cfg.mcc_host_name or socket.gethostname()).strip()


def _mcc_json(
    cfg: AgentConfig,
    path: str,
    *,
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    base = str(cfg.mcc_url or "").rstrip("/")
    token = str(cfg.mcc_token or "").strip()
    if not base or not token:
        raise RuntimeError("mcc.url and mcc.token are required")
    url = base + "/" + str(path or "").lstrip("/")
    if query:
        url += "?" + parse.urlencode(query)
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = mcc_request(url, data=body, headers=headers, method=method)
    with request.urlopen(req, timeout=max(1, int(timeout_sec))) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise RuntimeError("MCC returned invalid local-mail JSON")
    return parsed


def fetch_material(cfg: AgentConfig, domain: str) -> dict[str, Any]:
    return _mcc_json(
        cfg,
        "/api/v1/agent/local-mail/material",
        query={"domain": _domain(domain), "host_name": _host_name(cfg)},
    )


def _load_domains() -> dict[str, Any]:
    if not DOMAINS_PATH.exists():
        return {"schema": 1, "mail_identity_hostname": "", "domains": {}}
    try:
        payload = json.loads(DOMAINS_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    domains = payload.get("domains")
    if not isinstance(domains, dict):
        domains = {}
    return {
        "schema": 1,
        "mail_identity_hostname": str(payload.get("mail_identity_hostname") or "").strip().lower(),
        "domains": domains,
    }


def _managed_mta(domains: dict[str, Any], default: str = "sendmail") -> str:
    values = {
        str(item.get("mta") or default).strip().lower()
        for item in domains.values()
        if isinstance(item, dict)
    }
    if len(values) > 1:
        raise RuntimeError("managed own-host domains disagree on the host MTA")
    return next(iter(values), default)


def _write_atomic(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _save_domains(payload: dict[str, Any]) -> None:
    # Historical releases persisted a Debian key_path in this application
    # record. Remove that legacy metadata on the next state write; no host
    # filesystem action is implied by the migration.
    persisted = json.loads(json.dumps(payload))
    for item in (persisted.get("domains") or {}).values():
        if isinstance(item, dict):
            item.pop("key_path", None)
    _write_atomic(DOMAINS_PATH, json.dumps(persisted, ensure_ascii=True, indent=2) + "\n")


def _local_php(root: str) -> Path:
    base = Path(str(root or "").strip())
    if not base.is_absolute() or Path("/var/www") not in base.parents:
        raise RuntimeError("instance root must be below /var/www")
    for relative in ("config/local.php", "app/config/local.php"):
        path = base / relative
        if path.exists():
            return path
    raise RuntimeError("Mautic local.php not found")


def _php_quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _symfony_config_escape(value: str) -> str:
    return str(value).replace("%", "%%")


def _set_php_parameter(text: str, key: str, value: str) -> str:
    pattern = rf"(['\"]{re.escape(key)}['\"]\s*=>\s*)(?:['\"][^'\"]*['\"]|null)"
    updated, count = re.subn(pattern, lambda match: match.group(1) + _php_quote(value), text)
    if count:
        return updated

    # Fresh and partially migrated Mautic installations can have a valid
    # local.php with database parameters only. Add a new mail parameter only
    # to an explicit Mautic parameters array; do not guess at arbitrary PHP.
    if re.search(rf"['\"]{re.escape(key)}['\"]\s*=>", text):
        raise RuntimeError(f"Mautic local.php parameter has an unsupported value: {key}")
    return _insert_php_parameter(text, key, value)


def _insert_php_parameter(text: str, key: str, value: str) -> str:
    containers = (
        r"['\"]parameters['\"]\s*=>\s*array\s*\(",
        r"['\"]parameters['\"]\s*=>\s*\[",
        r"\$parameters\s*=\s*array\s*\(",
        r"\$parameters\s*=\s*\[",
    )
    for pattern in containers:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is None:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        base_indent = re.match(r"[ \t]*", text[line_start:match.start()]).group(0)
        entry = f"\n{base_indent}    {_php_quote(key)} => {_php_quote(value)},"
        return text[: match.end()] + entry + text[match.end() :]
    raise RuntimeError(f"Mautic local.php parameters array is missing: cannot add {key}")


def _set_php_parameter_if_present(text: str, key: str, value: str) -> str:
    if not re.search(rf"['\"]{re.escape(key)}['\"]\s*=>", text):
        return text
    return _set_php_parameter(text, key, value)


def _configure_mautic(
    root: str,
    domain: str,
    settings: dict[str, Any] | None = None,
    *,
    submit_command: str,
) -> None:
    path = _local_php(root)
    text = path.read_text(encoding="utf-8", errors="replace")
    stat = path.stat()
    values = dict(settings or {})
    command = f"{submit_command} --instance-domain={domain} -- -oi -t"
    dsn = "sendmail://default?command=" + parse.quote(command, safe="")
    text = _set_php_parameter(text, "mailer_dsn", _symfony_config_escape(dsn))
    text = _set_php_parameter(text, "mailer_from_email", str(values.get("from_email") or f"mailer@{domain}"))
    text = _set_php_parameter_if_present(text, "mailer_from_name", str(values.get("from_name") or "Sales Snap"))
    text = _set_php_parameter(text, "mailer_return_path", str(values.get("return_path") or f"bounce@{domain}"))
    _write_atomic(path, text, mode=stat.st_mode & 0o777)
    os.chown(path, stat.st_uid, stat.st_gid)


def _clear_mautic_cache(cfg: AgentConfig, root: str, *, php_bin: str | None = None) -> None:
    console = Path(root) / "bin" / "console"
    if not console.exists():
        raise RuntimeError("Mautic bin/console not found")
    rc, out = _run(
        ["sudo", "-u", "www-data", str(php_bin or cfg.php_bin or "/usr/bin/php"), str(console), "cache:clear"],
        timeout_sec=max(600, int(cfg.command_timeout_sec or 300)),
    )
    if rc != 0:
        command = " ".join(
            ["sudo", "-u", "www-data", str(php_bin or cfg.php_bin or "/usr/bin/php"), str(console), "cache:clear"]
        )
        raise RuntimeError(
            f"Mautic cache clear failed after mail configuration (rc={rc}; command={command}): {out}"
        )


def _send_test_message(
    cfg: AgentConfig,
    *,
    root: str,
    recipient: str,
    profile_name: str,
    php_bin: str | None = None,
) -> str:
    target = str(recipient or "").strip().lower()
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", target):
        raise RuntimeError("valid MCC user email is required for the mail configuration test")
    local_php = _local_php(root)
    base = Path(root)
    autoload_candidates = [base / "vendor" / "autoload.php", base.parent / "vendor" / "autoload.php"]
    autoload = next((path for path in autoload_candidates if path.exists()), None)
    if autoload is None:
        raise RuntimeError("Mautic vendor/autoload.php not found for mail test")
    with tempfile.NamedTemporaryFile(prefix="mcd-mail-test-", suffix=".php", delete=False) as handle:
        script_path = Path(handle.name)
        handle.write(_MAIL_TEST_SCRIPT.encode("utf-8"))
    try:
        os.chmod(script_path, 0o644)
        rc, out = _run(
            [
                "sudo",
                "-u",
                "www-data",
                str(php_bin or cfg.php_bin or "/usr/bin/php"),
                str(script_path),
                str(local_php),
                str(autoload),
                target,
                str(profile_name or "Mail profile")[:80],
            ],
            timeout_sec=max(120, int(cfg.command_timeout_sec or 300)),
        )
    finally:
        script_path.unlink(missing_ok=True)
    if rc != 0:
        raise RuntimeError("test email failed: " + out)
    return out or "test message accepted"


def _quota_connection() -> sqlite3.Connection:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(QUOTA_DB_PATH), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS counters ("
        "domain TEXT PRIMARY KEY, daily_period TEXT NOT NULL, daily_used INTEGER NOT NULL, "
        "monthly_period TEXT NOT NULL, monthly_used INTEGER NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def _periods(now: datetime | None = None) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    return current.strftime("%Y-%m-%d"), current.strftime("%Y-%m")


def _seed_quota(domain: str, material: dict[str, Any]) -> None:
    day, month = _periods()
    source_day = str(material.get("daily_period") or "")
    source_month = str(material.get("monthly_period") or "")
    source_daily = max(0, int(material.get("daily_used") or 0)) if source_day == day else 0
    source_monthly = max(0, int(material.get("monthly_used") or 0)) if source_month == month else 0
    with _quota_connection() as conn:
        existing = conn.execute(
            "SELECT daily_period,daily_used,monthly_period,monthly_used FROM counters WHERE domain=?",
            (domain,),
        ).fetchone()
        if existing:
            local_daily = int(existing[1]) if str(existing[0]) == day else 0
            local_monthly = int(existing[3]) if str(existing[2]) == month else 0
            source_daily = max(source_daily, local_daily)
            source_monthly = max(source_monthly, local_monthly)
        conn.execute(
            "INSERT INTO counters(domain,daily_period,daily_used,monthly_period,monthly_used,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET daily_period=excluded.daily_period,"
            "daily_used=excluded.daily_used,monthly_period=excluded.monthly_period,"
            "monthly_used=excluded.monthly_used,updated_at=excluded.updated_at",
            (domain, day, source_daily, month, source_monthly, datetime.now(timezone.utc).isoformat()),
        )


def quota_state(domain: str) -> dict[str, Any]:
    day, month = _periods()
    with _quota_connection() as conn:
        row = conn.execute(
            "SELECT daily_period,daily_used,monthly_period,monthly_used FROM counters WHERE domain=?",
            (_domain(domain),),
        ).fetchone()
    return {
        "daily_period": day,
        "daily_used": int(row[1]) if row and str(row[0]) == day else 0,
        "monthly_period": month,
        "monthly_used": int(row[3]) if row and str(row[2]) == month else 0,
    }


def _push_status(cfg: AgentConfig, domain: str, *, status: str, error: str = "") -> None:
    state = quota_state(domain)
    _mcc_json(
        cfg,
        "/api/v1/agent/local-mail/status",
        payload={
            "instance_domain": domain,
            "mcc_host_name": _host_name(cfg),
            "hostname": socket.gethostname(),
            "status": status,
            "error": error,
            **state,
        },
        timeout_sec=3,
    )


def _public_dns_short(dig: str, record_type: str, name: str) -> tuple[int, str]:
    last_rc, last_out = 1, ""
    for resolver in ("1.1.1.1", "8.8.8.8", ""):
        command = [dig]
        if resolver:
            command.append(f"@{resolver}")
        command.extend(["+short", record_type, name])
        rc, out = _run(command, timeout_sec=15)
        last_rc, last_out = rc, out
        if rc == 0 and out.strip():
            return rc, out
    return last_rc, last_out


def _mail_identity(mail_hostname: str) -> dict[str, Any]:
    hostname = str(mail_hostname or "").strip().rstrip(".").lower()
    result: dict[str, Any] = {
        "hostname": hostname,
        "ipv4": [],
        "ptr": {},
        "fcrdns": False,
        "error": "",
    }
    if not hostname:
        result["error"] = "mail hostname is empty"
        return result
    dig = shutil.which("dig")
    if not dig:
        result["error"] = "dig is not available for authoritative mail identity checks"
        return result
    rc, out = _public_dns_short(dig, "A", hostname)
    addresses = sorted(
        {
            line.strip()
            for line in out.splitlines()
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line.strip())
        }
    )
    if rc != 0 or not addresses:
        result["error"] = "mail hostname has no public IPv4 address"
        return result
    result["ipv4"] = addresses
    ptr: dict[str, str] = {}
    mismatches: list[str] = []
    for address in addresses:
        rc, out = _public_dns_short(dig, "-x", address)
        reverse = next((line.strip().rstrip(".").lower() for line in out.splitlines() if line.strip()), "")
        if rc != 0 or not reverse:
            mismatches.append(f"{address} has no PTR")
            continue
        ptr[address] = reverse
        if reverse != hostname:
            mismatches.append(f"{address} PTR is {reverse}, expected {hostname}")
    result["ptr"] = ptr
    result["fcrdns"] = bool(addresses) and not mismatches
    if mismatches:
        result["error"] = "; ".join(mismatches)
    return result


def _preflight_local_mail_material(
    cfg: AgentConfig,
    clean: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    material = fetch_material(cfg, clean)
    if not bool(material.get("enabled")):
        raise RuntimeError("own-host mail is disabled in MCC")
    mail_hostname = _domain(str(material.get("mail_hostname") or "mail." + clean))
    identity = _mail_identity(mail_hostname)
    if not bool(identity.get("fcrdns")):
        raise RuntimeError(
            "own-host mail identity is not forward-confirmed: "
            + str(identity.get("error") or "unknown error")
        )
    return material, mail_hostname, identity


def preflight_local_mail(cfg: AgentConfig, *, domain: str) -> dict[str, Any]:
    clean = _domain(domain)
    _material, mail_hostname, identity = _preflight_local_mail_material(cfg, clean)
    return {
        "status": "ok",
        "instance_domain": clean,
        "mail_identity_hostname": mail_hostname,
        "mail_identity": identity,
    }


def configure_local_mail(
    cfg: AgentConfig,
    *,
    domain: str,
    root: str,
    test_recipient: str = "",
    profile_name: str = "Own host",
    php_bin: str | None = None,
    validate_runtime: bool = True,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("local-mail configure must run as root")
    clean = _domain(domain)
    material, primary_mail_hostname, _identity = _preflight_local_mail_material(cfg, clean)
    # MCC resolves local hostname aliases and validates canonical host ownership
    # plus the agent source IP before returning private DKIM material.
    local_php_path = _local_php(root)
    local_php_before = local_php_path.read_text(encoding="utf-8", errors="replace")
    local_php_stat = local_php_path.stat()
    payload = _load_domains()
    payload_before = json.loads(json.dumps(payload))
    domains = payload["domains"]
    first_activation = not bool(domains)
    selector = str(material.get("selector") or "mcd").strip()
    requested_mail_hostname = primary_mail_hostname
    mta = _managed_mta(domains, "postfix")
    next_domain_names = sorted({str(name) for name in domains} | {clean})
    identity_runtime = _mail_identity_runtime(
        mta=mta,
        mail_hostname=requested_mail_hostname,
        domains=next_domain_names,
        selector=selector,
    )
    filesystem_started = False
    lifecycle: dict[str, Any] = {}
    lifecycle_started = False
    key_started = False
    submit_started = False
    try:
        external_os = _configured_mail_os_operation(
            cfg,
            operation="mail_os_filesystem_apply",
            runtime=identity_runtime,
        )
        filesystem_started = external_os is not None
        if external_os is None:
            raise RuntimeError("mail_os_filesystem_apply_binding_not_configured")
        if str(external_os.get("status") or "").strip().lower() not in {"applied", "noop", "cleaned"}:
            raise RuntimeError(str(external_os.get("error") or external_os.get("reason") or "mail_os_filesystem_apply_failed"))
        key_result = _configured_mail_os_operation(
            cfg,
            operation="mail_os_opendkim_key_apply",
            runtime=_mail_key_runtime(
                mta=mta,
                mail_hostname=requested_mail_hostname,
                domains=next_domain_names,
                selector=selector,
                key_domain=clean,
                private_key_pem=str(material.get("private_key_pem") or ""),
            ),
        )
        key_started = key_result is not None
        if key_result is None or str(key_result.get("status") or "").strip().lower() not in {"applied", "noop"}:
            raise RuntimeError("mail_os_opendkim_key_apply_failed")
        submit_result = _configured_mail_os_operation(
            cfg,
            operation="mail_os_submit_interface_apply",
            runtime=_mail_submit_runtime(
                mta=mta,
                mail_hostname=requested_mail_hostname,
                domains=next_domain_names,
                selector=selector,
            ),
        )
        submit_started = submit_result is not None
        if submit_result is None or str(submit_result.get("status") or "").strip().lower() not in {"applied", "noop"}:
            raise RuntimeError("mail_os_submit_interface_apply_failed")
        submit_command = _resolved_submit_command(submit_result)
        lifecycle_result = _configured_mail_os_operation(
            cfg,
            operation="mail_os_lifecycle_apply",
            runtime=identity_runtime,
        )
        lifecycle_started = lifecycle_result is not None
        if lifecycle_result is None or str(lifecycle_result.get("status") or "").strip().lower() not in {"applied", "noop"}:
            raise RuntimeError("mail_os_lifecycle_apply_failed")
        lifecycle = lifecycle_result
        CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(CONFIG_ROOT, 0o700)
        os.chmod(STATE_ROOT, 0o700)
        domains[clean] = {
            "instance_domain": clean,
            "root": str(Path(root)),
            "mail_hostname": requested_mail_hostname,
            "selector": selector,
            "daily_limit": int(material.get("daily_limit") or 100),
            "monthly_limit": int(material.get("monthly_limit") or 1000),
            "config_schema_version": int(material.get("config_schema_version") or 1),
            "mta": mta,
        }
        payload["mail_identity_hostname"] = primary_mail_hostname
        _save_domains(payload)
        _seed_quota(clean, material)
        sender_settings = material.get("settings") if isinstance(material.get("settings"), dict) else {}
        _configure_mautic(root, clean, sender_settings, submit_command=submit_command)
        if validate_runtime:
            _clear_mautic_cache(cfg, root, php_bin=php_bin)
        test_result = ""
        if validate_runtime and str(test_recipient or "").strip():
            test_result = _send_test_message(
                cfg,
                root=root,
                recipient=test_recipient,
                profile_name=profile_name,
                php_bin=php_bin,
            )
        _push_status(cfg, clean, status="ok")
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            _write_atomic(local_php_path, local_php_before, mode=local_php_stat.st_mode & 0o777)
            os.chown(local_php_path, local_php_stat.st_uid, local_php_stat.st_gid)
            if validate_runtime:
                _clear_mautic_cache(cfg, root, php_bin=php_bin)
            _save_domains(payload_before)
            previous_domains = payload_before.get("domains") if isinstance(payload_before.get("domains"), dict) else {}
            if first_activation:
                if lifecycle_started:
                    lifecycle_cleanup = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_lifecycle_cleanup",
                        runtime={
                            "mta": mta,
                            "mail_hostname": primary_mail_hostname,
                            "domains": [],
                            "selector": selector,
                        },
                    )
                    if lifecycle_cleanup is None or str(lifecycle_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                        raise RuntimeError("mail_os_lifecycle_cleanup_failed")
                if submit_started:
                    submit_cleanup = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_submit_interface_cleanup",
                        runtime=_mail_submit_runtime(
                            mta=mta,
                            mail_hostname=primary_mail_hostname,
                            domains=[],
                            selector=selector,
                        ),
                    )
                    if submit_cleanup is None or str(submit_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                        raise RuntimeError("mail_os_submit_interface_cleanup_failed")
                if filesystem_started:
                    cleanup = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_filesystem_cleanup",
                        runtime=_mail_identity_runtime(
                            mta=mta,
                            mail_hostname=primary_mail_hostname,
                            domains=[clean],
                            selector=selector,
                        ),
                    )
                    if cleanup is None or str(cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                        raise RuntimeError("mail_os_filesystem_cleanup_failed")
                if key_started:
                    key_cleanup = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_opendkim_key_cleanup",
                        runtime=_mail_key_runtime(
                            mta=mta,
                            mail_hostname=primary_mail_hostname,
                            domains=[],
                            selector=selector,
                            key_domain=clean,
                        ),
                    )
                    if key_cleanup is None or str(key_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                        raise RuntimeError("mail_os_opendkim_key_cleanup_failed")
            elif previous_domains:
                if key_started:
                    key_cleanup = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_opendkim_key_cleanup",
                        runtime=_mail_key_runtime(
                            mta=mta,
                            mail_hostname=primary_mail_hostname,
                            domains=[],
                            selector=selector,
                            key_domain=clean,
                        ),
                    )
                    if key_cleanup is None or str(key_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                        raise RuntimeError("mail_os_opendkim_key_cleanup_failed")
                previous_mta = _managed_mta(previous_domains, mta)
                previous_hostname = str(
                    payload_before.get("mail_identity_hostname")
                    or next(iter(previous_domains.values())).get("mail_hostname")
                    or ""
                )
                restore = _configured_mail_os_operation(
                    cfg,
                    operation="mail_os_filesystem_apply",
                    runtime=_mail_identity_runtime(
                        mta=previous_mta,
                        mail_hostname=previous_hostname,
                        domains=sorted(str(name) for name in previous_domains),
                        selector=str(next(iter(previous_domains.values())).get("selector") or "mcd"),
                    ),
                )
                if restore is None or str(restore.get("status") or "").strip().lower() not in {"applied", "noop", "cleaned"}:
                    raise RuntimeError("mail_os_filesystem_apply_failed")
                lifecycle = _configured_mail_os_operation(
                    cfg,
                    operation="mail_os_lifecycle_apply",
                    runtime=_mail_identity_runtime(
                        mta=previous_mta,
                        mail_hostname=previous_hostname,
                        domains=sorted(str(name) for name in previous_domains),
                        selector=str(next(iter(previous_domains.values())).get("selector") or "mcd"),
                    ),
                )
                if lifecycle is None or str(lifecycle.get("status") or "").strip().lower() not in {"applied", "noop"}:
                    raise RuntimeError("mail_os_lifecycle_apply_failed")
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RuntimeError(f"{exc}; rollback errors: {'; '.join(rollback_errors)}") from exc
        raise
    return {
        "status": "ok",
        "instance_domain": clean,
        "mail_hostname": domains[clean]["mail_hostname"],
        "mta": mta,
        "inbound_feedback": {
            "return_path": f"bounce@{clean}",
            "feedback_address": f"fbl@{clean}",
            "abuse_address": f"abuse@{clean}",
        },
        "delivery_service": str(lifecycle.get("delivery_service") or mta),
        "test_email": test_result,
        **quota_state(clean),
    }


def disable_local_mail(cfg: AgentConfig, *, domain: str) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("local-mail disable must run as root")
    clean = _domain(domain)
    payload = _load_domains()
    domains = payload["domains"]
    payload_before = json.loads(json.dumps(payload))
    item = domains.get(clean)
    if not isinstance(item, dict):
        return {"status": "ok", "instance_domain": clean, "remaining_domains": len(domains), "already_disabled": True}
    domains.pop(clean, None)
    mta = str(item.get("mta") or "sendmail").strip().lower()
    selector = str(item.get("selector") or "mcd").strip()
    submit_cleanup_succeeded = False
    try:
        _save_domains(payload)
        if domains:
            remaining_mta = _managed_mta(domains, mta)
            remaining_hostname = str(payload.get("mail_identity_hostname") or next(iter(domains.values())).get("mail_hostname") or "")
            external_os = _configured_mail_os_operation(
                cfg,
                operation="mail_os_filesystem_apply",
                runtime=_mail_identity_runtime(
                    mta=remaining_mta,
                    mail_hostname=remaining_hostname,
                    domains=sorted(str(name) for name in domains),
                    selector=str(next(iter(domains.values())).get("selector") or "mcd"),
                ),
            )
            if external_os is None or str(external_os.get("status") or "").strip().lower() not in {"applied", "noop", "cleaned"}:
                raise RuntimeError("mail_os_filesystem_apply_failed")
            lifecycle = _configured_mail_os_operation(
                cfg,
                operation="mail_os_lifecycle_apply",
                runtime=_mail_identity_runtime(
                    mta=remaining_mta,
                    mail_hostname=remaining_hostname,
                    domains=sorted(str(name) for name in domains),
                    selector=str(next(iter(domains.values())).get("selector") or "mcd"),
                ),
            )
            if lifecycle is None or str(lifecycle.get("status") or "").strip().lower() not in {"applied", "noop"}:
                raise RuntimeError("mail_os_lifecycle_apply_failed")
            key_cleanup = _configured_mail_os_operation(
                cfg,
                operation="mail_os_opendkim_key_cleanup",
                runtime=_mail_key_runtime(
                    mta=remaining_mta,
                    mail_hostname=remaining_hostname,
                    domains=sorted(str(name) for name in domains),
                    selector=selector,
                    key_domain=clean,
                ),
            )
            if key_cleanup is None or str(key_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                raise RuntimeError("mail_os_opendkim_key_cleanup_failed")
        else:
            lifecycle = _configured_mail_os_operation(
                cfg,
                operation="mail_os_lifecycle_cleanup",
                runtime=_mail_identity_runtime(
                    mta=mta,
                    mail_hostname=str(payload.get("mail_identity_hostname") or ""),
                    domains=[],
                    selector=selector,
                ),
            )
            if lifecycle is None or str(lifecycle.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                raise RuntimeError("mail_os_lifecycle_cleanup_failed")
            submit_cleanup = _configured_mail_os_operation(
                cfg,
                operation="mail_os_submit_interface_cleanup",
                runtime=_mail_submit_runtime(
                    mta=mta,
                    mail_hostname=str(payload.get("mail_identity_hostname") or ""),
                    domains=[],
                    selector=selector,
                ),
            )
            if submit_cleanup is None or str(submit_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                raise RuntimeError("mail_os_submit_interface_cleanup_failed")
            submit_cleanup_succeeded = True
            external_os = _configured_mail_os_operation(
                cfg,
                operation="mail_os_filesystem_cleanup",
                runtime=_mail_identity_runtime(
                    mta=mta,
                    mail_hostname=str(payload.get("mail_identity_hostname") or ""),
                    domains=[clean],
                    selector=selector,
                ),
            )
            if external_os is None or str(external_os.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                raise RuntimeError("mail_os_filesystem_cleanup_failed")
            key_cleanup = _configured_mail_os_operation(
                cfg,
                operation="mail_os_opendkim_key_cleanup",
                runtime=_mail_key_runtime(
                    mta=mta,
                    mail_hostname=str(payload.get("mail_identity_hostname") or ""),
                    domains=[],
                    selector=selector,
                    key_domain=clean,
                ),
            )
            if key_cleanup is None or str(key_cleanup.get("status") or "").strip().lower() not in {"cleaned", "noop"}:
                raise RuntimeError("mail_os_opendkim_key_cleanup_failed")
    except Exception as exc:
        _save_domains(payload_before)
        previous_domains = payload_before.get("domains") if isinstance(payload_before.get("domains"), dict) else {}
        try:
            if previous_domains:
                previous_mta = _managed_mta(previous_domains, mta)
                previous_hostname = str(
                    payload_before.get("mail_identity_hostname")
                    or next(iter(previous_domains.values())).get("mail_hostname")
                    or ""
                )
                restore = _configured_mail_os_operation(
                    cfg,
                    operation="mail_os_filesystem_apply",
                    runtime=_mail_identity_runtime(
                        mta=previous_mta,
                        mail_hostname=previous_hostname,
                        domains=sorted(str(name) for name in previous_domains),
                        selector=str(next(iter(previous_domains.values())).get("selector") or "mcd"),
                    ),
                )
                if restore is None or str(restore.get("status") or "").strip().lower() not in {"applied", "noop", "cleaned"}:
                    raise RuntimeError("mail_os_filesystem_apply_failed")
                lifecycle = _configured_mail_os_operation(
                    cfg,
                    operation="mail_os_lifecycle_apply",
                    runtime=_mail_identity_runtime(
                        mta=previous_mta,
                        mail_hostname=previous_hostname,
                        domains=sorted(str(name) for name in previous_domains),
                        selector=str(next(iter(previous_domains.values())).get("selector") or "mcd"),
                    ),
                )
                if lifecycle is None or str(lifecycle.get("status") or "").strip().lower() not in {"applied", "noop"}:
                    raise RuntimeError("mail_os_lifecycle_apply_failed")
                if submit_cleanup_succeeded:
                    submit_restore = _configured_mail_os_operation(
                        cfg,
                        operation="mail_os_submit_interface_apply",
                        runtime=_mail_submit_runtime(
                            mta=previous_mta,
                            mail_hostname=previous_hostname,
                            domains=sorted(str(name) for name in previous_domains),
                            selector=str(next(iter(previous_domains.values())).get("selector") or "mcd"),
                        ),
                    )
                    if submit_restore is None or str(submit_restore.get("status") or "").strip().lower() not in {"applied", "noop"}:
                        raise RuntimeError("mail_os_submit_interface_apply_failed")
        except Exception as rollback_exc:
            raise RuntimeError(f"{exc}; rollback failed: {rollback_exc}") from exc
        raise
    return {"status": "ok", "instance_domain": clean, "remaining_domains": len(domains)}


def receive_local_mail(
    cfg: AgentConfig,
    *,
    domain: str,
    kind: str,
    data: bytes,
) -> dict[str, Any]:
    clean = _domain(domain)
    role = str(kind or "").strip().lower()
    if role not in {"bounce", "feedback_loop"}:
        raise RuntimeError(f"unsupported own-host inbound message type: {kind}")
    if len(data) > 25 * 1024 * 1024:
        raise RuntimeError("own-host inbound message exceeds 25 MiB")
    payload = _load_domains()
    item = payload["domains"].get(clean)
    if not isinstance(item, dict):
        raise RuntimeError(f"own-host inbound mail is not configured for {clean}")

    from mcd_agent.db import MauticDB
    from mcd_agent.discovery import discover_mautic
    from mcd_agent.monitored_email import parse_monitored_message

    parsed = parse_monitored_message(data, (role,))
    if parsed is None:
        return {"status": "ignored", "instance_domain": clean, "type": role}
    configured_root = str(item.get("root") or "").strip()
    installs = discover_mautic(
        cfg.discovery_roots,
        cfg.exclude_path_contains,
        cfg.supported_mautic_majors,
        cfg.custom_instances,
    )
    install = next(
        (
            candidate
            for candidate in installs
            if (
                clean == str(getattr(candidate, "primary_domain", "") or "").strip().lower()
                or clean in {str(value).strip().lower() for value in getattr(candidate, "domains", [])}
                or (
                    configured_root
                    and Path(str(getattr(candidate, "root", "") or "")).resolve()
                    == Path(configured_root).resolve()
                )
            )
        ),
        None,
    )
    if install is None or getattr(install, "db", None) is None:
        raise RuntimeError(f"Mautic database configuration was not found for own-host inbound domain {clean}")
    result = MauticDB(install.db).add_dnc_for_emails(
        parsed.emails,
        reason=parsed.reason,
        comments=parsed.comments,
    )
    return {
        "status": "processed",
        "instance_domain": clean,
        "type": parsed.kind,
        "emails": len(parsed.emails),
        "contacts": int(result.get("contacts", 0) or 0),
        "dnc_added": int(result.get("added", 0) or 0),
        "dnc_existing": int(result.get("existing", 0) or 0),
    }


def _message_recipients(args: list[str], data: bytes) -> list[str]:
    recipients: list[str] = []
    if "--" in args:
        recipients.extend(value for value in args[args.index("--") + 1 :] if value and not value.startswith("-"))
    if not recipients:
        try:
            message = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
            values = [str(message.get(name)) for name in ("to", "cc", "bcc") if message.get(name)]
            recipients.extend(address for _name, address in getaddresses(values) if address)
        except Exception:
            recipients = []
    return sorted({value.strip().lower() for value in recipients if value.strip()})


def _message_sender(data: bytes, domain: str) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
        values = [
            str(message.get(name))
            for name in ("return-path", "sender", "from")
            if message.get(name)
        ]
        addresses = [address.strip().lower() for _name, address in getaddresses(values) if address.strip()]
        if addresses:
            return addresses[0]
    except Exception:
        pass
    return f"mailer@{domain}"


def _deliver_message(*, item: dict[str, Any], domain: str, recipients: list[str], data: bytes) -> tuple[int, int]:
    mta = str(item.get("mta") or "sendmail").strip().lower()
    port = _SENDMAIL_ISOLATED_PORT if mta == "sendmail" else 25
    wire_data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    try:
        with smtplib.SMTP("127.0.0.1", port, timeout=30) as client:
            refused = client.sendmail(_message_sender(data, domain), recipients, wire_data)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"own-host mail delivery failed for {domain}: {exc}", file=sys.stderr)
        return 75, len(recipients)
    refused_count = len(refused)
    if refused_count >= len(recipients):
        return 75, refused_count
    return 0, refused_count


def _reserve_quota(domain: str, count: int, daily_limit: int, monthly_limit: int) -> dict[str, Any]:
    day, month = _periods()
    conn = _quota_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT daily_period,daily_used,monthly_period,monthly_used FROM counters WHERE domain=?",
            (domain,),
        ).fetchone()
        daily_used = int(row[1]) if row and str(row[0]) == day else 0
        monthly_used = int(row[3]) if row and str(row[2]) == month else 0
        if daily_used + count > daily_limit or monthly_used + count > monthly_limit:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"own-host mail quota exceeded for {domain}: daily {daily_used}/{daily_limit}, monthly {monthly_used}/{monthly_limit}"
            )
        daily_used += count
        monthly_used += count
        conn.execute(
            "INSERT INTO counters(domain,daily_period,daily_used,monthly_period,monthly_used,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET daily_period=excluded.daily_period,"
            "daily_used=excluded.daily_used,monthly_period=excluded.monthly_period,"
            "monthly_used=excluded.monthly_used,updated_at=excluded.updated_at",
            (domain, day, daily_used, month, monthly_used, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("COMMIT")
        return {"daily_period": day, "daily_used": daily_used, "monthly_period": month, "monthly_used": monthly_used}
    finally:
        conn.close()


def _release_quota(domain: str, count: int) -> None:
    day, month = _periods()
    with _quota_connection() as conn:
        conn.execute(
            "UPDATE counters SET daily_used=CASE WHEN daily_period=? THEN MAX(0,daily_used-?) ELSE daily_used END,"
            "monthly_used=CASE WHEN monthly_period=? THEN MAX(0,monthly_used-?) ELSE monthly_used END,updated_at=? WHERE domain=?",
            (day, count, month, count, datetime.now(timezone.utc).isoformat(), domain),
        )


def submit_local_mail(
    *,
    domain: str,
    sendmail_args: list[str],
    data: bytes | None = None,
    cfg: AgentConfig | None = None,
) -> int:
    clean = _domain(domain)
    payload = _load_domains()
    item = payload["domains"].get(clean)
    if not isinstance(item, dict):
        print(f"own-host mail is not configured for {clean}", file=sys.stderr)
        return 69
    message_data = data if data is not None else sys.stdin.buffer.read()
    raw_args = list(sendmail_args or [])
    recipients = _message_recipients(raw_args, message_data)
    count = max(1, len(recipients))
    try:
        _reserve_quota(clean, count, int(item.get("daily_limit") or 100), int(item.get("monthly_limit") or 1000))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    if not recipients:
        _release_quota(clean, count)
        print(f"own-host mail has no recipients for {clean}", file=sys.stderr)
        return 64
    delivery_rc, refused_count = _deliver_message(
        item=item,
        domain=clean,
        recipients=recipients,
        data=message_data,
    )
    if refused_count:
        _release_quota(clean, min(count, refused_count))
    if delivery_rc != 0 and refused_count < count:
        _release_quota(clean, count - refused_count)
    elif cfg is not None:
        try:
            _push_status(cfg, clean, status="ok")
        except Exception:
            pass
    return int(delivery_rc)


def local_mail_status(cfg: AgentConfig, *, domain: str, push: bool = False) -> dict[str, Any]:
    clean = _domain(domain)
    payload = _load_domains()
    item = payload["domains"].get(clean)
    if not isinstance(item, dict):
        return {"status": "disabled", "instance_domain": clean}
    state = quota_state(clean)
    service_status: dict[str, str] = {}
    errors: list[str] = []
    mta = str(item.get("mta") or "sendmail").strip().lower()
    domains = payload.get("domains") if isinstance(payload.get("domains"), dict) else {}
    primary_mail_hostname = str(payload.get("mail_identity_hostname") or item.get("mail_hostname") or "mail." + clean)
    identity = _mail_identity(primary_mail_hostname)
    selector = str(item.get("selector") or "mcd")
    identity_runtime = _mail_identity_runtime(
        mta=mta,
        mail_hostname=primary_mail_hostname,
        domains=sorted(str(name) for name in domains),
        selector=selector,
    )
    key_status: dict[str, Any] = {}
    submit_status: dict[str, Any] = {}
    if not bool(identity.get("fcrdns")):
        errors.append(
            "mail identity is not forward-confirmed: "
            + str(identity.get("error") or "unknown error")
        )
    external_status = _configured_mail_os_operation(
        cfg,
        operation="mail_os_lifecycle_status",
        runtime=identity_runtime,
    )
    if external_status is None:
        errors.append("mail_os_lifecycle_status_binding_not_configured")
    else:
        raw_services = external_status.get("services")
        if isinstance(raw_services, dict):
            service_status = {str(name): str(value) for name, value in raw_services.items()}
        external_error = str(external_status.get("error") or "").strip()
        if external_error:
            errors.append(external_error)
        external_state = str(external_status.get("status") or "").strip().lower()
        if external_state not in {"ok", "ready", "noop"}:
            errors.append(external_error or "mail_os_lifecycle_status_failed")

    def check_read_only_status(
        operation: str,
        runtime: dict[str, Any],
        binding_error: str,
        failed_error: str,
    ) -> dict[str, Any]:
        result = _configured_mail_os_operation(cfg, operation=operation, runtime=runtime)
        if result is None:
            errors.append(binding_error)
            return {}
        if result.get("read_only") is not True:
            errors.append(f"{failed_error}_not_read_only")
        result_state = str(result.get("status") or "").strip().lower()
        result_error = str(result.get("error") or result.get("reason") or "").strip()
        if result_state not in {"ok", "ready", "noop"}:
            errors.append(result_error or failed_error)
        elif result_error:
            errors.append(result_error)
        return result

    key_status = check_read_only_status(
        "mail_os_opendkim_key_status",
        _mail_key_runtime(
            mta=mta,
            mail_hostname=primary_mail_hostname,
            domains=sorted(str(name) for name in domains),
            selector=selector,
            key_domain=clean,
        ),
        "mail_os_opendkim_key_status_binding_not_configured",
        "mail_os_opendkim_key_status_failed",
    )
    submit_status = check_read_only_status(
        "mail_os_submit_interface_status",
        _mail_submit_runtime(
            mta=mta,
            mail_hostname=primary_mail_hostname,
            domains=sorted(str(name) for name in domains),
            selector=selector,
        ),
        "mail_os_submit_interface_status_binding_not_configured",
        "mail_os_submit_interface_status_failed",
    )
    status = "ok" if not errors else "error"
    if push:
        _push_status(cfg, clean, status=status, error="; ".join(errors))
    return {
        "status": status,
        "instance_domain": clean,
        "mail_hostname": str(item.get("mail_hostname") or ""),
        "mta": mta,
        "mail_identity": identity,
        "daily_limit": int(item.get("daily_limit") or 100),
        "monthly_limit": int(item.get("monthly_limit") or 1000),
        "services": service_status,
        "opendkim_key": key_status,
        "submit_interface": submit_status,
        "error": "; ".join(errors),
        **state,
    }
