from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
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


CONFIG_ROOT = Path("/etc/mcd/local-mail")
STATE_ROOT = Path("/var/lib/mcd/local-mail")
DOMAINS_PATH = CONFIG_ROOT / "domains.json"
QUOTA_DB_PATH = STATE_ROOT / "quota.sqlite3"
SENDMAIL_MC = Path("/etc/mail/sendmail.mc")
SENDMAIL_ISOLATED_MC = CONFIG_ROOT / "sendmail.mc"
SENDMAIL_ISOLATED_CF = CONFIG_ROOT / "sendmail.cf"
SENDMAIL_ISOLATED_QUEUE = Path("/var/spool/mqueue-mcd")
SENDMAIL_ISOLATED_SERVICE = Path("/etc/systemd/system/mcd-local-mail-sendmail.service")
POSTFIX_MAIN_CF = Path("/etc/postfix/main.cf")
POSTFIX_MAIN_CF_BASELINE = STATE_ROOT / "postfix.main.cf.baseline"
INBOUND_BASELINE_MANIFEST = STATE_ROOT / "inbound-baseline.json"
SENDMAIL_HOST_MC_BASELINE = STATE_ROOT / "sendmail.host.mc.baseline"
SENDMAIL_HOST_CF = Path("/etc/mail/sendmail.cf")
SENDMAIL_HOST_CF_BASELINE = STATE_ROOT / "sendmail.host.cf.baseline"
SENDMAIL_LOCAL_HOST_NAMES = Path("/etc/mail/local-host-names")
SENDMAIL_LOCAL_HOST_NAMES_BASELINE = STATE_ROOT / "sendmail.local-host-names.baseline"
SENDMAIL_VIRTUSER = Path("/etc/mail/virtusertable")
SENDMAIL_VIRTUSER_BASELINE = STATE_ROOT / "sendmail.virtusertable.baseline"
SENDMAIL_VIRTUSER_DB = Path("/etc/mail/virtusertable.db")
SENDMAIL_VIRTUSER_DB_BASELINE = STATE_ROOT / "sendmail.virtusertable.db.baseline"
MAIL_ALIASES = Path("/etc/aliases")
MAIL_ALIASES_BASELINE = STATE_ROOT / "aliases.baseline"
MAIL_ALIASES_DB = Path("/etc/aliases.db")
MAIL_ALIASES_DB_BASELINE = STATE_ROOT / "aliases.db.baseline"
POSTFIX_VIRTUAL_ALIASES = CONFIG_ROOT / "postfix-virtual-aliases"
OPENDKIM_CONF = Path("/etc/opendkim.conf")
OPENDKIM_CONF_BASELINE = STATE_ROOT / "opendkim.conf.baseline"
OPENDKIM_DEFAULT = Path("/etc/default/opendkim")
OPENDKIM_DEFAULT_BASELINE = STATE_ROOT / "opendkim.default.baseline"
OPENDKIM_KEYS = Path("/etc/opendkim/keys")
OPENDKIM_KEY_TABLE = CONFIG_ROOT / "KeyTable"
OPENDKIM_SIGNING_TABLE = CONFIG_ROOT / "SigningTable"
SUBMIT_WRAPPER = Path("/usr/local/bin/mcd-mail-submit")
SUBMIT_ROOT_HELPER = Path("/usr/local/libexec/mcd-mail-submit-root")
SUBMIT_SUDOERS = Path("/etc/sudoers.d/mcd-local-mail")
RECEIVE_WRAPPER = Path("/usr/local/bin/mcd-mail-receive")
RECEIVE_ROOT_HELPER = Path("/usr/local/libexec/mcd-mail-receive-root")
RECEIVE_SUDOERS = Path("/etc/sudoers.d/mcd-local-mail-receive")
SMTP_FIREWALL_HELPER = Path("/usr/local/libexec/mcd-local-mail-firewall")
SMTP_FIREWALL_SERVICE = Path("/etc/systemd/system/mcd-local-mail-firewall.service")
SENDMAIL_BIN = Path("/usr/sbin/sendmail")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SENDMAIL_BEGIN = "dnl MCD LOCAL MAIL BEGIN"
_SENDMAIL_END = "dnl MCD LOCAL MAIL END"
_OPENDKIM_BEGIN = "# MCD LOCAL MAIL BEGIN"
_OPENDKIM_END = "# MCD LOCAL MAIL END"
_INBOUND_BEGIN = "# MCD LOCAL MAIL INBOUND BEGIN"
_INBOUND_END = "# MCD LOCAL MAIL INBOUND END"
_SENDMAIL_INBOUND_BEGIN = "dnl MCD LOCAL MAIL INBOUND BEGIN"
_SENDMAIL_INBOUND_END = "dnl MCD LOCAL MAIL INBOUND END"
_SENDMAIL_ISOLATED_PORT = 2525
_SENDMAIL_SERVICE_NAME = "mcd-local-mail-sendmail"
_SMTP_FIREWALL_SERVICE_NAME = "mcd-local-mail-firewall"
_SMTP_FIREWALL_COMMENT = "MCD own-host inbound SMTP"
_SMTP_EGRESS_COMMENT = "MCD own-host outbound SMTP via MTA only"
_SMTP_FORWARD_COMMENT = "MCD own-host block forwarded SMTP"
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


def _write_atomic(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _save_domains(payload: dict[str, Any]) -> None:
    _write_atomic(DOMAINS_PATH, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def _backup_once(source: Path, target: Path) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    os.chmod(target, 0o600)


def _inbound_baseline_files() -> tuple[tuple[Path, Path], ...]:
    return (
        (SENDMAIL_MC, SENDMAIL_HOST_MC_BASELINE),
        (SENDMAIL_HOST_CF, SENDMAIL_HOST_CF_BASELINE),
        (SENDMAIL_LOCAL_HOST_NAMES, SENDMAIL_LOCAL_HOST_NAMES_BASELINE),
        (SENDMAIL_VIRTUSER, SENDMAIL_VIRTUSER_BASELINE),
        (SENDMAIL_VIRTUSER_DB, SENDMAIL_VIRTUSER_DB_BASELINE),
        (MAIL_ALIASES, MAIL_ALIASES_BASELINE),
        (MAIL_ALIASES_DB, MAIL_ALIASES_DB_BASELINE),
    )


def _backup_inbound_once() -> None:
    if INBOUND_BASELINE_MANIFEST.exists():
        return
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, bool] = {}
    for source, backup in _inbound_baseline_files():
        exists = source.exists()
        manifest[str(source)] = exists
        if exists:
            shutil.copy2(source, backup)
            os.chmod(backup, 0o600)
    _write_atomic(INBOUND_BASELINE_MANIFEST, json.dumps(manifest, sort_keys=True) + "\n", mode=0o600)


def _restore_inbound_baseline() -> None:
    if not INBOUND_BASELINE_MANIFEST.exists():
        return
    try:
        manifest = json.loads(INBOUND_BASELINE_MANIFEST.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("own-host inbound baseline manifest is invalid") from exc
    for target, backup in _inbound_baseline_files():
        if bool(manifest.get(str(target))):
            if not backup.exists():
                raise RuntimeError(f"own-host inbound baseline is missing: {backup}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        else:
            target.unlink(missing_ok=True)
    for _target, backup in _inbound_baseline_files():
        backup.unlink(missing_ok=True)
    INBOUND_BASELINE_MANIFEST.unlink(missing_ok=True)


def _managed_text(text: str, begin: str, end: str, lines: list[str]) -> str:
    base = _strip_block(text, begin, end).rstrip()
    block = "\n".join([begin, *lines, end])
    return (base + "\n" + block + "\n") if base else (block + "\n")


def _inbound_alias_name(domain: str, kind: str) -> str:
    digest = hashlib.sha256(f"{domain}:{kind}".encode("ascii")).hexdigest()[:16]
    return f"mcd_{kind}_{digest}"


def _inbound_route_lines(domains: dict[str, Any]) -> tuple[list[str], list[str]]:
    aliases: list[str] = []
    virtual: list[str] = []
    for domain in sorted(domains):
        bounce_alias = _inbound_alias_name(domain, "bounce")
        feedback_alias = _inbound_alias_name(domain, "fbl")
        aliases.extend(
            [
                f'{bounce_alias}: "|{RECEIVE_WRAPPER} --instance-domain={domain} --kind=bounce"',
                f'{feedback_alias}: "|{RECEIVE_WRAPPER} --instance-domain={domain} --kind=feedback_loop"',
            ]
        )
        virtual.extend(
            [
                f"bounce@{domain} {bounce_alias}",
                f"fbl@{domain} {feedback_alias}",
                f"abuse@{domain} {feedback_alias}",
                f"@{domain} error:5.1.1:550 No such own-host mailbox",
            ]
        )
    return aliases, virtual


def _write_receive_wrapper() -> None:
    _write_atomic(
        RECEIVE_ROOT_HELPER,
        """#!/bin/sh
set -eu
[ "$#" -eq 2 ] || exit 64
case "$1" in
  --instance-domain=*) domain=${1#--instance-domain=} ;;
  *) exit 64 ;;
esac
case "$domain" in
  ''|.*|*..*|*.|*[!a-z0-9.-]*) exit 64 ;;
esac
case "$2" in
  --kind=bounce|--kind=feedback_loop) ;;
  *) exit 64 ;;
esac
exec /usr/local/bin/mcd-cli local-mail receive "$1" "$2"
""",
        mode=0o755,
    )
    _write_atomic(
        RECEIVE_WRAPPER,
        f"#!/bin/sh\nexec sudo -n {RECEIVE_ROOT_HELPER} \"$@\"\n",
        mode=0o755,
    )
    users: list[str] = []
    for name in ("daemon", "mail", "smmsp", "postfix"):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        users.append(name)
    if not users:
        raise RuntimeError("no supported MTA service account exists for own-host inbound mail")
    rules = "".join(f"{name} ALL=(root) NOPASSWD: {RECEIVE_ROOT_HELPER} *\n" for name in users)
    _write_atomic(RECEIVE_SUDOERS, rules, mode=0o440)
    rc, out = _run(["visudo", "-cf", str(RECEIVE_SUDOERS)], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("local-mail receive sudoers validation failed: " + out)


def _remove_receive_wrapper() -> None:
    for path in (RECEIVE_WRAPPER, RECEIVE_ROOT_HELPER, RECEIVE_SUDOERS, POSTFIX_VIRTUAL_ALIASES):
        path.unlink(missing_ok=True)
    Path(str(POSTFIX_VIRTUAL_ALIASES) + ".db").unlink(missing_ok=True)


def _smtp_firewall_rule(iptables: str, operation: str) -> list[str]:
    return [
        iptables,
        "-w",
        operation,
        "INPUT",
        "-p",
        "tcp",
        "--dport",
        "25",
        "-m",
        "comment",
        "--comment",
        _SMTP_FIREWALL_COMMENT,
        "-j",
        "ACCEPT",
    ]


def _smtp_egress_rule(iptables: str, operation: str) -> list[str]:
    return [
        iptables, "-w", operation, "OUTPUT", "-p", "tcp", "!", "-d", "127.0.0.0/8",
        "--dport", "25", "-m", "owner", "!", "--uid-owner", "0", "-m", "comment",
        "--comment", _SMTP_EGRESS_COMMENT, "-j", "REJECT", "--reject-with", "tcp-reset",
    ]


def _smtp_forward_rule(iptables: str, operation: str) -> list[str]:
    return [
        iptables, "-w", operation, "FORWARD", "-p", "tcp", "--dport", "25", "-m", "comment",
        "--comment", _SMTP_FORWARD_COMMENT, "-j", "REJECT", "--reject-with", "tcp-reset",
    ]


def _configure_smtp_firewall() -> None:
    iptables = shutil.which("iptables")
    if not iptables:
        raise RuntimeError("iptables is required for own-host inbound SMTP")
    helper = f"""#!/bin/sh
set -eu
IPTABLES={shlex.quote(iptables)}
COMMENT={shlex.quote(_SMTP_FIREWALL_COMMENT)}
EGRESS_COMMENT={shlex.quote(_SMTP_EGRESS_COMMENT)}
FORWARD_COMMENT={shlex.quote(_SMTP_FORWARD_COMMENT)}
case "${{1:-}}" in
  allow)
    "$IPTABLES" -w -C INPUT -p tcp --dport 25 -m comment --comment "$COMMENT" -j ACCEPT 2>/dev/null ||
      "$IPTABLES" -w -I INPUT 1 -p tcp --dport 25 -m comment --comment "$COMMENT" -j ACCEPT
    "$IPTABLES" -w -C OUTPUT -p tcp ! -d 127.0.0.0/8 --dport 25 -m owner ! --uid-owner 0 -m comment --comment "$EGRESS_COMMENT" -j REJECT --reject-with tcp-reset 2>/dev/null ||
      "$IPTABLES" -w -I OUTPUT 1 -p tcp ! -d 127.0.0.0/8 --dport 25 -m owner ! --uid-owner 0 -m comment --comment "$EGRESS_COMMENT" -j REJECT --reject-with tcp-reset
    "$IPTABLES" -w -C FORWARD -p tcp --dport 25 -m comment --comment "$FORWARD_COMMENT" -j REJECT --reject-with tcp-reset 2>/dev/null ||
      "$IPTABLES" -w -I FORWARD 1 -p tcp --dport 25 -m comment --comment "$FORWARD_COMMENT" -j REJECT --reject-with tcp-reset
    ;;
  deny)
    while "$IPTABLES" -w -C INPUT -p tcp --dport 25 -m comment --comment "$COMMENT" -j ACCEPT 2>/dev/null; do
      "$IPTABLES" -w -D INPUT -p tcp --dport 25 -m comment --comment "$COMMENT" -j ACCEPT
    done
    while "$IPTABLES" -w -C OUTPUT -p tcp ! -d 127.0.0.0/8 --dport 25 -m owner ! --uid-owner 0 -m comment --comment "$EGRESS_COMMENT" -j REJECT --reject-with tcp-reset 2>/dev/null; do
      "$IPTABLES" -w -D OUTPUT -p tcp ! -d 127.0.0.0/8 --dport 25 -m owner ! --uid-owner 0 -m comment --comment "$EGRESS_COMMENT" -j REJECT --reject-with tcp-reset
    done
    while "$IPTABLES" -w -C FORWARD -p tcp --dport 25 -m comment --comment "$FORWARD_COMMENT" -j REJECT --reject-with tcp-reset 2>/dev/null; do
      "$IPTABLES" -w -D FORWARD -p tcp --dport 25 -m comment --comment "$FORWARD_COMMENT" -j REJECT --reject-with tcp-reset
    done
    ;;
  *) exit 64 ;;
esac
"""
    _write_atomic(SMTP_FIREWALL_HELPER, helper, mode=0o755)
    _write_atomic(
        SMTP_FIREWALL_SERVICE,
        f"""[Unit]
Description=MCD own-host SMTP firewall rules
After=network-pre.target
Before=network.target sendmail.service postfix.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={SMTP_FIREWALL_HELPER} allow
ExecStop={SMTP_FIREWALL_HELPER} deny

[Install]
WantedBy=multi-user.target
""",
        mode=0o644,
    )
    rc, out = _run([str(SMTP_FIREWALL_HELPER), "allow"], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("own-host SMTP firewall rule failed: " + out)
    rc, out = _run(["systemctl", "daemon-reload"], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("systemd reload failed for own-host SMTP firewall: " + out)
    rc, out = _run(["systemctl", "enable", "--now", _SMTP_FIREWALL_SERVICE_NAME], timeout_sec=90)
    if rc != 0:
        raise RuntimeError("own-host SMTP firewall service failed: " + out)


def _remove_smtp_firewall() -> None:
    _run(["systemctl", "disable", "--now", _SMTP_FIREWALL_SERVICE_NAME], timeout_sec=90)
    iptables = shutil.which("iptables")
    if iptables:
        for rule in (_smtp_firewall_rule, _smtp_egress_rule, _smtp_forward_rule):
            for _attempt in range(32):
                rc, _out = _run(rule(iptables, "-C"), timeout_sec=15)
                if rc != 0:
                    break
                rc, out = _run(rule(iptables, "-D"), timeout_sec=15)
                if rc != 0:
                    raise RuntimeError("own-host SMTP firewall cleanup failed: " + out)
            else:
                raise RuntimeError("own-host SMTP firewall cleanup exceeded the duplicate-rule limit")
    SMTP_FIREWALL_SERVICE.unlink(missing_ok=True)
    SMTP_FIREWALL_HELPER.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"], timeout_sec=30)


def _package_installed(name: str) -> bool:
    rc, out = _run(["dpkg-query", "-W", "-f=${db:Status-Status}", name], timeout_sec=30)
    return rc == 0 and out.strip() == "installed"


def _apt_install() -> str:
    sendmail_path = shutil.which("sendmail")
    native_sendmail = _package_installed("sendmail-bin")
    native_postfix = _package_installed("postfix")
    if native_sendmail:
        mta = "sendmail"
    elif native_postfix:
        domains = _load_domains().get("domains", {})
        managed_postfix = POSTFIX_MAIN_CF_BASELINE.exists() or any(
            isinstance(item, dict) and str(item.get("mta") or "") == "postfix"
            for item in (domains.values() if isinstance(domains, dict) else [])
        )
        if not managed_postfix:
            raise RuntimeError("existing unmanaged Postfix must be reviewed before enabling own-host mail")
        mta = "postfix"
    elif sendmail_path:
        raise RuntimeError(
            f"unsupported existing MTA owns {sendmail_path}; own-host mail will not replace it automatically"
        )
    else:
        mta = "postfix"
    packages: list[str] = []
    if not native_sendmail and not native_postfix:
        packages.append("postfix")
    if not _package_installed("opendkim"):
        packages.append("opendkim")
    if not _package_installed("opendkim-tools"):
        packages.append("opendkim-tools")
    if shutil.which("dig") is None:
        packages.append("dnsutils")
    if shutil.which("sudo") is None:
        packages.append("sudo")
    if not packages:
        return mta
    if "postfix" in packages:
        rc, out = _run(
            ["debconf-set-selections"],
            timeout_sec=30,
            input_bytes=(
                b"postfix postfix/main_mailer_type select Internet Site\n"
                b"postfix postfix/mailname string localhost\n"
            ),
        )
        if rc != 0:
            raise RuntimeError("postfix package preseed failed: " + out)
    rc, out = _run(["apt-get", "update"], timeout_sec=600)
    if rc != 0:
        raise RuntimeError("apt-get update failed: " + out)
    rc, out = _run(
        ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *packages],
        timeout_sec=900,
    )
    if rc != 0:
        raise RuntimeError("mail package installation failed: " + out)
    missing_tools = [
        name
        for name in ("sendmail", "opendkim", "opendkim-testkey", "sudo", "dig")
        if shutil.which(name) is None
    ]
    if missing_tools:
        raise RuntimeError("mail package installation did not provide: " + ", ".join(missing_tools))
    return mta


def _strip_block(text: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\s*", re.DOTALL)
    return re.sub(pattern, "", str(text or "")).rstrip() + "\n"


def _sendmail_direct_text(text: str, mail_hostname: str) -> str:
    out = _strip_block(text, _SENDMAIL_BEGIN, _SENDMAIL_END)
    disabled_patterns = (
        r"^\s*define\(\s*`SMART_HOST'",
        r"^\s*FEATURE\(\s*`allmasquerade'",
        r"^\s*FEATURE\(\s*`masquerade_envelope'",
        r"^\s*MASQUERADE_AS\(",
        r"^\s*MASQUERADE_DOMAIN\(",
        r"^\s*DAEMON_OPTIONS\(",
        r"^\s*QUEUE_DIR\(",
        r"^\s*define\(\s*`QUEUE_DIR'",
        r"^\s*define\(\s*`confPID_FILE'",
        r"^\s*CLIENT_OPTIONS\(",
    )
    lines: list[str] = []
    domain_replaced = False
    for line in out.splitlines():
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in disabled_patterns):
            lines.append("dnl MCD disabled for own-host mail: " + line)
            continue
        if re.search(r"^\s*define\(\s*`confDOMAIN_NAME'", line, re.IGNORECASE):
            lines.append(f"define(`confDOMAIN_NAME', `{mail_hostname}')dnl")
            domain_replaced = True
            continue
        lines.append(line)
    managed_block = [
        _SENDMAIL_BEGIN,
        f"DAEMON_OPTIONS(`Family=inet, Name=MCD, Port={_SENDMAIL_ISOLATED_PORT}, Addr=127.0.0.1')dnl",
        "CLIENT_OPTIONS(`Family=inet, Address=0.0.0.0')dnl",
        f"define(`QUEUE_DIR', `{SENDMAIL_ISOLATED_QUEUE}')dnl",
        "define(`confPID_FILE', `/run/mcd-local-mail-sendmail.pid')dnl",
        "INPUT_MAIL_FILTER(`opendkim', `S=inet:8891@localhost, F=T, T=R:2m')dnl",
        "define(`confMILTER_MACROS_ENVFROM', `i, {auth_type}, {auth_authen}, {auth_ssf}, {auth_author}, {mail_mailer}, {mail_host}, {mail_addr}')dnl",
        _SENDMAIL_END,
    ]
    insert_at = next(
        (index for index, line in enumerate(lines) if re.search(r"^\s*MAILER\(", line, re.IGNORECASE)),
        len(lines),
    )
    prefix: list[str] = []
    if not domain_replaced:
        prefix.append(f"define(`confDOMAIN_NAME', `{mail_hostname}')dnl")
    lines[insert_at:insert_at] = [*prefix, *managed_block]
    return "\n".join(lines).rstrip() + "\n"


def _configure_sendmail(mail_hostname: str) -> None:
    if not SENDMAIL_MC.exists():
        raise RuntimeError(f"sendmail.mc not found: {SENDMAIL_MC}")
    current = SENDMAIL_MC.read_text(encoding="utf-8", errors="replace")
    _write_atomic(SENDMAIL_ISOLATED_MC, _sendmail_direct_text(current, mail_hostname), mode=0o600)
    proc = subprocess.run(
        ["m4", str(SENDMAIL_ISOLATED_MC)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "isolated Sendmail configuration build failed: "
            + (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        )
    compiled = proc.stdout.decode("utf-8", errors="strict")
    expected_queue = f"O QueueDirectory={SENDMAIL_ISOLATED_QUEUE}"
    if expected_queue not in compiled.splitlines():
        raise RuntimeError("isolated Sendmail configuration did not compile the dedicated queue path")
    _write_atomic(SENDMAIL_ISOLATED_CF, compiled, mode=0o600)
    SENDMAIL_ISOLATED_QUEUE.mkdir(parents=True, exist_ok=True)
    os.chmod(SENDMAIL_ISOLATED_QUEUE, 0o770)
    try:
        shutil.chown(SENDMAIL_ISOLATED_QUEUE, user="root", group="smmta")
    except LookupError as exc:
        raise RuntimeError("Sendmail service account is missing") from exc
    _write_atomic(
        SENDMAIL_ISOLATED_SERVICE,
        """[Unit]
Description=MCD isolated own-host Sendmail
After=network-online.target opendkim.service
Wants=network-online.target
Requires=opendkim.service

[Service]
Type=simple
ExecStart=/usr/sbin/sendmail -C/etc/mcd/local-mail/sendmail.cf -bD -q5m
ExecReload=/bin/kill -HUP $MAINPID
KillSignal=SIGINT
TimeoutStopSec=15s
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
""",
        mode=0o644,
    )
    rc, out = _run([str(SENDMAIL_BIN), f"-C{SENDMAIL_ISOLATED_CF}", "-bt", "-d0.1"], timeout_sec=30, input_bytes=b"$=w\n")
    if rc != 0:
        raise RuntimeError("isolated Sendmail validation failed: " + out)
    rc, out = _run(["systemctl", "daemon-reload"], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("systemd reload failed for isolated Sendmail: " + out)


def _sendmail_inbound_text(text: str, mail_hostname: str) -> str:
    base = _strip_block(text, _SENDMAIL_INBOUND_BEGIN, _SENDMAIL_INBOUND_END)
    lines: list[str] = []
    domain_replaced = False
    for line in base.splitlines():
        active_mta = (
            re.search(r"^\s*DAEMON_OPTIONS\(", line, re.IGNORECASE)
            and re.search(r"Port\s*=\s*(?:smtp|25)(?:[,`'\)])", line, re.IGNORECASE)
            and not re.search(r"Port\s*=\s*submission", line, re.IGNORECASE)
        )
        if re.search(r"^\s*define\(\s*`confDOMAIN_NAME'", line, re.IGNORECASE):
            lines.append(f"define(`confDOMAIN_NAME', `{mail_hostname}')dnl")
            domain_replaced = True
            continue
        lines.append("dnl MCD inbound replaced: " + line if active_mta else line)
    has_virtusertable = any(
        re.search(r"^\s*FEATURE\(\s*`virtusertable'", line, re.IGNORECASE)
        for line in lines
    )
    managed = [
        _SENDMAIL_INBOUND_BEGIN,
        *([] if domain_replaced else [f"define(`confDOMAIN_NAME', `{mail_hostname}')dnl"]),
        "DAEMON_OPTIONS(`Family=inet, Name=MCD-Inbound, Port=smtp, M=A')dnl",
    ]
    if not has_virtusertable:
        managed.append("FEATURE(`virtusertable', `hash -o /etc/mail/virtusertable.db')dnl")
    managed.append(_SENDMAIL_INBOUND_END)
    insert_at = next(
        (index for index, line in enumerate(lines) if re.search(r"^\s*MAILER(?:_DEFINITIONS|\()", line, re.IGNORECASE)),
        len(lines),
    )
    lines[insert_at:insert_at] = managed
    return "\n".join(lines).rstrip() + "\n"


def _configure_common_inbound_files(domains: dict[str, Any]) -> tuple[list[str], list[str]]:
    _write_receive_wrapper()
    aliases, virtual = _inbound_route_lines(domains)
    aliases_existing = MAIL_ALIASES.read_text(encoding="utf-8", errors="replace") if MAIL_ALIASES.exists() else ""
    _write_atomic(
        MAIL_ALIASES,
        _managed_text(aliases_existing, _INBOUND_BEGIN, _INBOUND_END, aliases),
        mode=0o644,
    )
    rc, out = _run([shutil.which("newaliases") or "/usr/bin/newaliases"], timeout_sec=60)
    if rc != 0:
        raise RuntimeError("own-host inbound aliases build failed: " + out)
    return aliases, virtual


def _configure_sendmail_inbound(domains: dict[str, Any]) -> None:
    if not SENDMAIL_MC.exists():
        raise RuntimeError(f"sendmail.mc not found: {SENDMAIL_MC}")
    _aliases, virtual = _configure_common_inbound_files(domains)
    local_hosts = (
        SENDMAIL_LOCAL_HOST_NAMES.read_text(encoding="utf-8", errors="replace")
        if SENDMAIL_LOCAL_HOST_NAMES.exists()
        else ""
    )
    _write_atomic(
        SENDMAIL_LOCAL_HOST_NAMES,
        _managed_text(local_hosts, _INBOUND_BEGIN, _INBOUND_END, sorted(domains)),
        mode=0o644,
    )
    virt_existing = SENDMAIL_VIRTUSER.read_text(encoding="utf-8", errors="replace") if SENDMAIL_VIRTUSER.exists() else ""
    _write_atomic(
        SENDMAIL_VIRTUSER,
        _managed_text(virt_existing, _INBOUND_BEGIN, _INBOUND_END, virtual),
        mode=0o644,
    )
    rc, out = _run(
        [shutil.which("makemap") or "/usr/sbin/makemap", "hash", str(SENDMAIL_VIRTUSER)],
        timeout_sec=60,
        input_bytes=SENDMAIL_VIRTUSER.read_bytes(),
    )
    if rc != 0:
        raise RuntimeError("own-host inbound virtusertable build failed: " + out)
    current = SENDMAIL_MC.read_text(encoding="utf-8", errors="replace")
    stat = SENDMAIL_MC.stat()
    primary_domain = sorted(domains)[0]
    mail_hostname = str(domains[primary_domain].get("mail_hostname") or "mail." + primary_domain)
    _write_atomic(SENDMAIL_MC, _sendmail_inbound_text(current, mail_hostname), mode=stat.st_mode & 0o777)
    os.chown(SENDMAIL_MC, stat.st_uid, stat.st_gid)
    proc = subprocess.run(
        ["m4", str(SENDMAIL_MC)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "own-host inbound Sendmail build failed: "
            + (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        )
    cf_mode = (SENDMAIL_HOST_CF.stat().st_mode & 0o777) if SENDMAIL_HOST_CF.exists() else 0o644
    _write_atomic(SENDMAIL_HOST_CF, proc.stdout.decode("utf-8", errors="strict"), mode=cf_mode)
    rc, out = _run([str(SENDMAIL_BIN), "-C" + str(SENDMAIL_HOST_CF), "-bt", "-d0.1"], timeout_sec=30, input_bytes=b"$=w\n")
    if rc != 0:
        raise RuntimeError("own-host inbound Sendmail validation failed: " + out)


def _configure_postfix_inbound(domains: dict[str, Any]) -> None:
    _aliases, virtual = _configure_common_inbound_files(domains)
    postfix_virtual: list[str] = []
    for line in virtual:
        address, target = line.split(" ", 1)
        if address.startswith("@"):
            continue
        postfix_virtual.append(f"{address} {target}@localhost")
    _write_atomic(POSTFIX_VIRTUAL_ALIASES, "\n".join(postfix_virtual) + "\n", mode=0o600)
    rc, out = _run([shutil.which("postmap") or "/usr/sbin/postmap", str(POSTFIX_VIRTUAL_ALIASES)], timeout_sec=60)
    if rc != 0:
        raise RuntimeError("own-host inbound Postfix map build failed: " + out)
    settings = {
        "inet_interfaces": "all",
        "virtual_alias_domains": ", ".join(sorted(domains)),
        "virtual_alias_maps": "hash:" + str(POSTFIX_VIRTUAL_ALIASES),
    }
    for key, value in settings.items():
        rc, out = _run(["postconf", "-e", f"{key} = {value}"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError(f"Postfix inbound setting failed for {key}: {out}")
    rc, out = _run(["postfix", "check"], timeout_sec=60)
    if rc != 0:
        raise RuntimeError("Postfix inbound validation failed: " + out)


def _configure_inbound(domains: dict[str, Any], *, mta: str) -> None:
    if not domains:
        raise RuntimeError("own-host inbound configuration requires at least one domain")
    _backup_inbound_once()
    if mta == "sendmail":
        _configure_sendmail_inbound(domains)
        return
    if mta == "postfix":
        _configure_postfix_inbound(domains)
        return
    raise RuntimeError(f"unsupported managed inbound MTA: {mta}")


def _configure_postfix(mail_hostname: str) -> None:
    if not POSTFIX_MAIN_CF.exists():
        raise RuntimeError(f"Postfix main.cf not found: {POSTFIX_MAIN_CF}")
    _backup_once(POSTFIX_MAIN_CF, POSTFIX_MAIN_CF_BASELINE)
    settings = {
        "myhostname": mail_hostname,
        "myorigin": "$myhostname",
        "inet_protocols": "ipv4",
        "inet_interfaces": "loopback-only",
        "mydestination": "$myhostname, localhost.$mydomain, localhost",
        "relayhost": "",
        "smtp_tls_security_level": "may",
        "smtp_tls_CApath": "/etc/ssl/certs",
        "smtpd_milters": "inet:localhost:8891",
        "non_smtpd_milters": "inet:localhost:8891",
        "milter_default_action": "tempfail",
        "milter_protocol": "6",
    }
    for key, value in settings.items():
        rc, out = _run(["postconf", "-e", f"{key} = {value}"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError(f"Postfix setting failed for {key}: {out}")
    rc, out = _run(["postfix", "check"], timeout_sec=60)
    if rc != 0:
        raise RuntimeError("Postfix validation failed: " + out)


def _configure_mta(mta: str, mail_hostname: str) -> None:
    if mta == "postfix":
        _configure_postfix(mail_hostname)
        return
    if mta == "sendmail":
        _configure_sendmail(mail_hostname)
        return
    raise RuntimeError(f"unsupported managed MTA: {mta}")


def _delivery_service(mta: str) -> str:
    return _SENDMAIL_SERVICE_NAME if mta == "sendmail" else "postfix"


def _remove_isolated_sendmail() -> None:
    _run(["systemctl", "disable", "--now", _SENDMAIL_SERVICE_NAME], timeout_sec=90)
    for path in (SENDMAIL_ISOLATED_SERVICE, SENDMAIL_ISOLATED_CF, SENDMAIL_ISOLATED_MC):
        path.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"], timeout_sec=30)


def _managed_mta(domains: dict[str, Any], default: str = "sendmail") -> str:
    values = {
        str(item.get("mta") or default).strip().lower()
        for item in domains.values()
        if isinstance(item, dict)
    }
    if len(values) > 1:
        raise RuntimeError("managed own-host domains disagree on the host MTA")
    return next(iter(values), default)


def _configure_opendkim(domains: dict[str, Any], *, mta: str = "sendmail") -> None:
    existing = OPENDKIM_CONF.read_text(encoding="utf-8", errors="replace") if OPENDKIM_CONF.exists() else ""
    managed = _OPENDKIM_BEGIN in existing and _OPENDKIM_END in existing
    if not managed and re.search(r"(?im)^\s*(KeyTable|SigningTable)\s+", existing):
        raise RuntimeError("existing unmanaged OpenDKIM signing configuration must be reviewed before enabling own-host mail")
    _backup_once(OPENDKIM_CONF, OPENDKIM_CONF_BASELINE)
    _backup_once(OPENDKIM_DEFAULT, OPENDKIM_DEFAULT_BASELINE)
    base = _strip_block(existing, _OPENDKIM_BEGIN, _OPENDKIM_END)
    base = "\n".join(
        line
        for line in base.splitlines()
        if not re.search(r"^\s*(Mode|Socket|Canonicalization|OversignHeaders)\s+", line, re.IGNORECASE)
    ).rstrip() + "\n"
    socket_value = "inet:8891@localhost"
    block = "\n".join(
        [
            _OPENDKIM_BEGIN,
            "Mode s",
            f"Socket {socket_value}",
            "Canonicalization relaxed/simple",
            "OversignHeaders From",
            f"KeyTable refile:{OPENDKIM_KEY_TABLE}",
            f"SigningTable refile:{OPENDKIM_SIGNING_TABLE}",
            _OPENDKIM_END,
        ]
    )
    _write_atomic(OPENDKIM_CONF, base.rstrip() + "\n" + block + "\n", mode=0o644)
    default_text = OPENDKIM_DEFAULT.read_text(encoding="utf-8", errors="replace") if OPENDKIM_DEFAULT.exists() else ""
    if re.search(r"(?m)^\s*SOCKET=", default_text):
        default_text = re.sub(r"(?m)^\s*SOCKET=.*$", f'SOCKET="{socket_value}"', default_text)
    else:
        default_text = default_text.rstrip() + f'\nSOCKET="{socket_value}"\n'
    _write_atomic(OPENDKIM_DEFAULT, default_text, mode=0o644)

    key_lines: list[str] = []
    signing_lines: list[str] = []
    for domain, item in sorted(domains.items()):
        selector = str(item.get("selector") or "mcd").strip()
        key_path = OPENDKIM_KEYS / domain / f"{selector}.private"
        key_lines.append(f"{selector}._domainkey.{domain} {domain}:{selector}:{key_path}")
        signing_lines.append(f"*@{domain} {selector}._domainkey.{domain}")
    _write_atomic(OPENDKIM_KEY_TABLE, "\n".join(key_lines) + "\n", mode=0o640)
    _write_atomic(OPENDKIM_SIGNING_TABLE, "\n".join(signing_lines) + "\n", mode=0o640)
    try:
        shutil.chown(OPENDKIM_KEY_TABLE, user="root", group="opendkim")
        shutil.chown(OPENDKIM_SIGNING_TABLE, user="root", group="opendkim")
    except LookupError as exc:
        raise RuntimeError("opendkim service account is missing") from exc


def _write_key(domain: str, selector: str, private_key: str) -> Path:
    key_dir = OPENDKIM_KEYS / domain
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"{selector}.private"
    _write_atomic(key_path, private_key.rstrip() + "\n", mode=0o600)
    try:
        shutil.chown(key_dir, user="opendkim", group="opendkim")
        shutil.chown(key_path, user="opendkim", group="opendkim")
    except LookupError as exc:
        raise RuntimeError("opendkim service account is missing") from exc
    return key_path


def _write_submit_wrapper() -> None:
    _write_atomic(
        SUBMIT_ROOT_HELPER,
        "#!/bin/sh\nexec /usr/local/bin/mcd-cli local-mail submit \"$@\"\n",
        mode=0o755,
    )
    _write_atomic(
        SUBMIT_WRAPPER,
        f"#!/bin/sh\nexec sudo -n {SUBMIT_ROOT_HELPER} \"$@\"\n",
        mode=0o755,
    )
    _write_atomic(
        SUBMIT_SUDOERS,
        f"www-data ALL=(root) NOPASSWD: {SUBMIT_ROOT_HELPER} *\n",
        mode=0o440,
    )
    rc, out = _run(["visudo", "-cf", str(SUBMIT_SUDOERS)], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("local-mail sudoers validation failed: " + out)


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


def _configure_mautic(root: str, domain: str, settings: dict[str, Any] | None = None) -> None:
    path = _local_php(root)
    text = path.read_text(encoding="utf-8", errors="replace")
    stat = path.stat()
    values = dict(settings or {})
    command = f"{SUBMIT_WRAPPER} --instance-domain={domain} -- -oi -t"
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
    inbound_was_managed = INBOUND_BASELINE_MANIFEST.exists()
    firewall_was_managed = SMTP_FIREWALL_SERVICE.exists()
    selector = str(material.get("selector") or "mcd").strip()
    requested_mail_hostname = primary_mail_hostname
    mta = _apt_install()
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_ROOT, 0o700)
    os.chmod(STATE_ROOT, 0o700)
    key_path = _write_key(clean, selector, str(material.get("private_key_pem") or ""))
    domains[clean] = {
        "instance_domain": clean,
        "root": str(Path(root)),
        "mail_hostname": requested_mail_hostname,
        "selector": selector,
        "key_path": str(key_path),
        "daily_limit": int(material.get("daily_limit") or 100),
        "monthly_limit": int(material.get("monthly_limit") or 1000),
        "config_schema_version": int(material.get("config_schema_version") or 1),
        "mta": mta,
    }
    payload["mail_identity_hostname"] = primary_mail_hostname
    try:
        _save_domains(payload)
        _seed_quota(clean, material)
        _write_submit_wrapper()
        _configure_opendkim(domains, mta=mta)
        if first_activation and mta == "postfix":
            _run(["systemctl", "stop", "postfix"], timeout_sec=90)
        _configure_mta(mta, primary_mail_hostname)
        _configure_inbound(domains, mta=mta)
        _configure_smtp_firewall()
        sender_settings = material.get("settings") if isinstance(material.get("settings"), dict) else {}
        _configure_mautic(root, clean, sender_settings)
        if validate_runtime:
            _clear_mautic_cache(cfg, root, php_bin=php_bin)
        services = ["opendkim", _delivery_service(mta)]
        if mta == "sendmail":
            services.append("sendmail")
        for service in services:
            rc, out = _run(["systemctl", "enable", "--now", service], timeout_sec=90)
            if rc != 0:
                raise RuntimeError(f"{service} start failed: {out}")
            rc, out = _run(["systemctl", "restart", service], timeout_sec=90)
            if rc != 0:
                raise RuntimeError(f"{service} restart failed: {out}")
        if mta == "sendmail":
            rc, out = _run(
                [str(SENDMAIL_BIN), f"-C{SENDMAIL_ISOLATED_CF}", "-bt", "-d0.1"],
                timeout_sec=30,
                input_bytes=b"$=w\n",
            )
            if rc != 0:
                raise RuntimeError("isolated Sendmail validation failed: " + out)
        if mta == "postfix":
            rc, out = _run(["postfix", "check"], timeout_sec=60)
            if rc != 0:
                raise RuntimeError("Postfix validation failed: " + out)
        dkim_error = ""
        for attempt in range(5):
            rc, out = _run(
                ["opendkim-testkey", "-d", clean, "-s", selector, "-k", str(key_path)],
                timeout_sec=30,
            )
            if rc == 0:
                dkim_error = ""
                break
            dkim_error = out
            if attempt < 4:
                import time

                time.sleep(5)
        if dkim_error:
            raise RuntimeError("DKIM validation failed: " + dkim_error)
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
                if mta == "postfix":
                    _restore_baseline(POSTFIX_MAIN_CF_BASELINE, POSTFIX_MAIN_CF)
                else:
                    _remove_isolated_sendmail()
                _restore_inbound_baseline()
                _remove_receive_wrapper()
                _remove_smtp_firewall()
                _restore_baseline(OPENDKIM_CONF_BASELINE, OPENDKIM_CONF)
                _restore_baseline(OPENDKIM_DEFAULT_BASELINE, OPENDKIM_DEFAULT)
                _run(["systemctl", "disable", "--now", "opendkim"], timeout_sec=90)
            elif previous_domains:
                previous_mta = _managed_mta(previous_domains, mta)
                _configure_opendkim(previous_domains, mta=previous_mta)
                _configure_mta(
                    previous_mta,
                    str(
                        payload_before.get("mail_identity_hostname")
                        or next(iter(previous_domains.values())).get("mail_hostname")
                        or ""
                    ),
                )
                if inbound_was_managed:
                    _configure_inbound(previous_domains, mta=previous_mta)
                else:
                    _restore_inbound_baseline()
                    _remove_receive_wrapper()
                if firewall_was_managed:
                    _configure_smtp_firewall()
                else:
                    _remove_smtp_firewall()
                _run(["systemctl", "restart", "opendkim"], timeout_sec=90)
                _run(["systemctl", "restart", _delivery_service(previous_mta)], timeout_sec=90)
                if previous_mta == "sendmail":
                    _run(["systemctl", "restart", "sendmail"], timeout_sec=90)
            elif mta == "postfix":
                _run(["systemctl", "restart", "postfix"], timeout_sec=90)
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if clean not in payload_before.get("domains", {}) and key_path.exists():
            try:
                key_path.unlink()
            except OSError as key_exc:
                rollback_errors.append(f"temporary DKIM key cleanup failed: {key_exc}")
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
        "delivery_service": _delivery_service(mta),
        "test_email": test_result,
        **quota_state(clean),
    }


def _restore_baseline(source: Path, target: Path) -> None:
    if source.exists():
        shutil.copy2(source, target)


def disable_local_mail(cfg: AgentConfig, *, domain: str) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("local-mail disable must run as root")
    clean = _domain(domain)
    payload = _load_domains()
    domains = payload["domains"]
    payload_before = json.loads(json.dumps(payload))
    inbound_was_managed = INBOUND_BASELINE_MANIFEST.exists()
    firewall_was_managed = SMTP_FIREWALL_SERVICE.exists()
    item = domains.get(clean)
    if not isinstance(item, dict):
        return {"status": "ok", "instance_domain": clean, "remaining_domains": len(domains), "already_disabled": True}
    domains.pop(clean, None)
    mta = str(item.get("mta") or "sendmail").strip().lower()
    try:
        _save_domains(payload)
        if domains:
            remaining_mta = _managed_mta(domains, mta)
            _configure_opendkim(domains, mta=remaining_mta)
            _configure_mta(
                remaining_mta,
                str(payload.get("mail_identity_hostname") or next(iter(domains.values())).get("mail_hostname") or ""),
            )
            _configure_inbound(domains, mta=remaining_mta)
            _configure_smtp_firewall()
            services = ["opendkim", _delivery_service(remaining_mta)]
            if remaining_mta == "sendmail":
                services.append("sendmail")
            for service in services:
                rc, out = _run(["systemctl", "restart", service], timeout_sec=90)
                if rc != 0:
                    raise RuntimeError(f"{service} restart failed: {out}")
        else:
            if mta == "postfix":
                _restore_baseline(POSTFIX_MAIN_CF_BASELINE, POSTFIX_MAIN_CF)
            else:
                _remove_isolated_sendmail()
            _restore_inbound_baseline()
            _remove_receive_wrapper()
            _remove_smtp_firewall()
            _restore_baseline(OPENDKIM_CONF_BASELINE, OPENDKIM_CONF)
            _restore_baseline(OPENDKIM_DEFAULT_BASELINE, OPENDKIM_DEFAULT)
            _run(["systemctl", "disable", "--now", "opendkim"], timeout_sec=90)
            if mta == "postfix":
                rc, out = _run(["systemctl", "restart", "postfix"], timeout_sec=90)
                if rc != 0:
                    raise RuntimeError("postfix restart failed: " + out)
            else:
                rc, out = _run(["systemctl", "restart", "sendmail"], timeout_sec=90)
                if rc != 0:
                    raise RuntimeError("sendmail restart failed: " + out)
    except Exception as exc:
        _save_domains(payload_before)
        previous_domains = payload_before.get("domains") if isinstance(payload_before.get("domains"), dict) else {}
        try:
            if previous_domains:
                previous_mta = _managed_mta(previous_domains, mta)
                _configure_opendkim(previous_domains, mta=previous_mta)
                _configure_mta(
                    previous_mta,
                    str(
                        payload_before.get("mail_identity_hostname")
                        or next(iter(previous_domains.values())).get("mail_hostname")
                        or ""
                    ),
                )
                if inbound_was_managed:
                    _configure_inbound(previous_domains, mta=previous_mta)
                else:
                    _restore_inbound_baseline()
                    _remove_receive_wrapper()
                if firewall_was_managed:
                    _configure_smtp_firewall()
                else:
                    _remove_smtp_firewall()
                _run(["systemctl", "restart", "opendkim"], timeout_sec=90)
                _run(["systemctl", "restart", _delivery_service(previous_mta)], timeout_sec=90)
                if previous_mta == "sendmail":
                    _run(["systemctl", "restart", "sendmail"], timeout_sec=90)
        except Exception as rollback_exc:
            raise RuntimeError(f"{exc}; rollback failed: {rollback_exc}") from exc
        raise
    if item:
        key_path = Path(str(item.get("key_path") or ""))
        if key_path.exists() and OPENDKIM_KEYS in key_path.parents:
            key_path.unlink()
            try:
                key_path.parent.rmdir()
            except OSError:
                pass
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
    if not bool(identity.get("fcrdns")):
        errors.append(
            "mail identity is not forward-confirmed: "
            + str(identity.get("error") or "unknown error")
        )
    required_services = (_delivery_service(mta), "opendkim", _SMTP_FIREWALL_SERVICE_NAME)
    for service in required_services:
        rc, out = _run(["systemctl", "is-active", service], timeout_sec=15)
        service_status[service] = out.strip() or ("active" if rc == 0 else "inactive")
        if rc != 0:
            errors.append(f"{service} is not active")
    if mta == "sendmail":
        rc, out = _run(["systemctl", "is-active", "sendmail"], timeout_sec=15)
        service_status["sendmail"] = out.strip() or ("active" if rc == 0 else "inactive")
        if rc != 0:
            errors.append("sendmail is not active")
    iptables = shutil.which("iptables")
    if not iptables:
        errors.append("iptables is not available")
    else:
        firewall_checks = (
            (_smtp_firewall_rule, "own-host inbound SMTP firewall rule is missing"),
            (_smtp_egress_rule, "own-host outbound SMTP process restriction is missing"),
            (_smtp_forward_rule, "own-host forwarded SMTP restriction is missing"),
        )
        for rule, message in firewall_checks:
            rc, _out = _run(rule(iptables, "-C"), timeout_sec=15)
            if rc != 0:
                errors.append(message)
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
        "error": "; ".join(errors),
        **state,
    }
