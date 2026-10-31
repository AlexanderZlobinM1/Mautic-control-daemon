from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import ipaddress
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import fcntl

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


FILTER_PATH = Path("/etc/fail2ban/filter.d/mcc-global.conf")
MAUTIC_AUTH_FILTER_PATH = Path("/etc/fail2ban/filter.d/mautic-auth-nginx.conf")
JAIL_PATH = Path("/etc/fail2ban/jail.d/98-mcd-security.local")
LOG_PATH = Path("/var/log/fail2ban-mcc-global.log")
SYNC_LOCK_PATH = Path("/run/mcd/security-blocklist.lock")
JAIL_NAME = "mcc-global"
MANAGED_MARKER = "# Managed by MCD: Wazuh/Cloudflare local Fail2ban enforcement"


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 15) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = mcc_request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def fetch_security_blocklist(cfg: AgentConfig) -> dict[str, Any]:
    if not cfg.mcc_url:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    try:
        return _post_json(
            cfg.mcc_url.rstrip("/") + "/api/v1/agent/security-blocklist",
            payload,
            cfg.mcc_token,
            timeout_sec=15,
        )
    except HTTPError as exc:
        return {"status": "error", "reason": f"http_{exc.code}"}
    except URLError as exc:
        return {"status": "error", "reason": f"urlerror:{exc.reason}"}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


def _normalize_ip_or_network(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty address")
    if "/" in raw:
        item = ipaddress.ip_network(raw, strict=False)
    else:
        item = ipaddress.ip_address(raw)
    if not item.is_global:
        raise ValueError("address is not global")
    return str(item)


def _normalize_addresses(values: Any) -> set[str]:
    rows = values if isinstance(values, list) else []
    result: set[str] = set()
    for raw in rows:
        try:
            result.add(_normalize_ip_or_network(raw))
        except Exception:
            continue
    return result


def _allowlisted(value: str, allowlist: set[str]) -> bool:
    try:
        target = ipaddress.ip_network(value, strict=False)
    except Exception:
        return True
    for raw in allowlist:
        try:
            allowed = ipaddress.ip_network(raw, strict=False)
        except Exception:
            continue
        if target.version != allowed.version:
            continue
        if target.subnet_of(allowed) or allowed.subnet_of(target):
            return True
    return False


def _control_plane_addresses(cfg: AgentConfig) -> set[str]:
    """Resolve addresses that must remain reachable for MCC synchronization."""
    hostname = str(urlsplit(str(getattr(cfg, "mcc_url", "") or "")).hostname or "").strip()
    if not hostname:
        return set()
    try:
        return {str(ipaddress.ip_address(hostname))}
    except ValueError:
        pass

    addresses: set[str] = set()
    try:
        for row in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
            try:
                addresses.add(str(ipaddress.ip_address(str(row[4][0]))))
            except (IndexError, TypeError, ValueError):
                continue
    except OSError as exc:
        logging.warning("control-plane allowlist DNS resolution failed for %s: %s", hostname, exc)
    return addresses


def _append_required_ignore_addresses(content: str, required: set[str]) -> str:
    if not required:
        return content
    output: list[str] = []
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.lower().startswith("ignoreip") or "=" not in line:
            output.append(line)
            continue
        prefix, raw = line.split("=", 1)
        newline = "\n" if raw.endswith("\n") else ""
        current = raw.strip().split()
        merged = [*current, *(item for item in sorted(required) if item not in current)]
        output.append(f"{prefix}= {' '.join(merged)}{newline}")
    return "".join(output)


def _ensure_control_plane_allowlist(cfg: AgentConfig) -> set[str]:
    """Persist and enforce MCC reachability before fetching central policy."""
    required = _control_plane_addresses(cfg)
    if not required:
        return set()

    client = shutil.which("fail2ban-client")
    changed = False
    if JAIL_PATH.is_file():
        current = JAIL_PATH.read_text(encoding="utf-8", errors="ignore")
        updated = _append_required_ignore_addresses(current, required)
        changed = _atomic_write(JAIL_PATH, updated)

    if client:
        if changed:
            _client_ok(_run(client, "-t", timeout=120), "fail2ban control-plane allowlist validation")
            _client_ok(_run(client, "reload", timeout=120), "fail2ban control-plane allowlist reload")
        for jail in ("sshd", "mautic-auth-nginx", JAIL_NAME):
            if not _jail_is_active(client, jail):
                continue
            for batch in _address_batches(sorted(required)):
                _run(client, "set", jail, "unbanip", *batch, timeout=120)
    return required


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> bool:
    current = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else None
    if current == content:
        return False
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
    return True


def _run(*args: str, timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, env=env)


def _ensure_fail2ban_installed() -> tuple[bool, str]:
    client = shutil.which("fail2ban-client")
    if client:
        return False, client
    apt = shutil.which("apt-get")
    if not apt:
        raise RuntimeError("fail2ban is missing and apt-get is unavailable")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    proc = _run(apt, "install", "-y", "fail2ban", timeout=600, env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()[-1000:]
        raise RuntimeError(f"fail2ban install failed: {detail}")
    client = shutil.which("fail2ban-client")
    if not client:
        raise RuntimeError("fail2ban-client is missing after package installation")
    return True, client


def _jail_config(*, enabled: bool, allowlist: set[str], action: str) -> str:
    ignore = ["127.0.0.1/8", "::1", *sorted(allowlist)]
    return (
        f"{MANAGED_MARKER}\n"
        "[sshd]\n"
        "enabled = true\n"
        "mode = aggressive\n"
        "backend = systemd\n"
        f"ignoreip = {' '.join(ignore)}\n"
        "maxretry = 3\n"
        "findtime = 10m\n"
        "bantime = 7d\n"
        "bantime.increment = true\n"
        "bantime.factor = 2\n"
        "bantime.maxtime = 30d\n"
        # Keep the managed action name separate from legacy sshd jails.  A
        # shared action name made Fail2ban retain a stale nftables action.
        f"action = {action.format(name='mcd-sshd')}\n"
        "\n"
        # The filter matches only Mautic login POSTs across every nginx vhost.
        # Do not use a generic 4xx jail: normal Mautic and API traffic can
        # legitimately return client errors.
        "[mautic-auth-nginx]\n"
        "enabled = true\n"
        "filter = mautic-auth-nginx\n"
        "backend = auto\n"
        "logpath = /var/log/nginx/*access.log\n"
        f"ignoreip = {' '.join(ignore)}\n"
        "maxretry = 3\n"
        "findtime = 10m\n"
        "bantime = 7d\n"
        "bantime.increment = true\n"
        "bantime.factor = 2\n"
        "bantime.maxtime = 30d\n"
        # The source IP attempted a credential attack.  Once detected, reject
        # it at the host boundary, including any direct service it may probe.
        f"action = {action.format(name='mcd-mautic-auth-nginx')}\n"
        "\n"
        f"[{JAIL_NAME}]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        "filter = mcc-global\n"
        f"logpath = {LOG_PATH}\n"
        "backend = polling\n"
        f"ignoreip = {' '.join(ignore)}\n"
        "maxretry = 1\n"
        "findtime = 10m\n"
        "bantime = -1\n"
        f"action = {action.format(name='mcc-global')}\n"
    )


def _filter_config() -> str:
    return (
        f"{MANAGED_MARKER}\n"
        "[Definition]\n"
        "failregex = ^MCC-GLOBAL-BAN <HOST>$\n"
        "ignoreregex =\n"
    )


def _mautic_auth_filter_config() -> str:
    return (
        f"{MANAGED_MARKER}\n"
        "# Match Mautic login POSTs across every nginx vhost (v4-v7).\n"
        "[Definition]\n"
        "failregex = ^<HOST> .*\"POST \\S*(?:/index\\.php)?/s/login(?:_check)?(?:[/?#][^\" ]*)? HTTP/[0-9.]+\" \\d{3} .*$\n"
        "            ^<HOST> .*\"POST \\S*(?:/index\\.php)?/login(?:_check)?(?:[/?#][^\" ]*)? HTTP/[0-9.]+\" \\d{3} .*$\n"
        "ignoreregex =\n"
    )


def _status_ips(client: str, jail: str) -> set[str]:
    proc = _run(client, "status", jail)
    if proc.returncode != 0:
        return set()
    for raw in proc.stdout.splitlines():
        if "Banned IP list:" in raw:
            return {item for item in raw.split("Banned IP list:", 1)[1].split() if item}
    return set()


def _jail_is_active(client: str, jail: str) -> bool:
    return _run(client, "status", jail).returncode == 0


def _client_ok(proc: subprocess.CompletedProcess[str], operation: str) -> None:
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()[-800:]
    raise RuntimeError(f"{operation} failed: {detail}")


def _verify_ssh_firewall_action(action: str) -> None:
    """Prove that Fail2ban installed an enforceable SSH action, not only a ticket."""
    if action.startswith("iptables["):
        proc = _run("iptables", "-S")
        _client_ok(proc, "iptables SSH jail verification")
        if "f2b-mcd-sshd" not in proc.stdout:
            raise RuntimeError("Fail2ban SSH jail has no active iptables chain")
        return
    proc = _run("nft", "list", "table", "inet", "f2b-table")
    _client_ok(proc, "nftables SSH jail verification")
    if "mcd-sshd" not in proc.stdout:
        raise RuntimeError("Fail2ban SSH jail has no active nftables set")


def _wait_for_ssh_firewall_action(action: str, timeout_sec: float = 20.0) -> None:
    """Wait for Fail2ban to restore persistent tickets and its firewall chain."""
    deadline = time.monotonic() + timeout_sec
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            _verify_ssh_firewall_action(action)
            return
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.25)
    raise last_error or RuntimeError("Fail2ban SSH jail verification timed out")


def _chunks(values: list[str], size: int = 250) -> list[list[str]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def _address_batches(values: list[str], size: int = 250) -> list[list[str]]:
    by_family: dict[int, list[str]] = {4: [], 6: []}
    for value in values:
        try:
            family = ipaddress.ip_network(value, strict=False).version
        except Exception:
            continue
        by_family[family].append(value)
    return [batch for family in (4, 6) for batch in _chunks(by_family[family], size)]


def apply_security_blocklist_profile(
    profile: dict[str, Any],
    *,
    required_allowlist: set[str] | None = None,
) -> dict[str, Any]:
    enabled = bool(profile.get("enabled", False))
    if os.geteuid() != 0:
        return {"status": "error", "reason": "root_required", "changed": False}

    allowlist = _normalize_addresses(profile.get("allowlist")) | set(required_allowlist or set())
    desired = {
        item
        for item in _normalize_addresses(profile.get("blocked"))
        if not _allowlisted(item, allowlist)
    }
    client = shutil.which("fail2ban-client")
    if not enabled and not client and not JAIL_PATH.exists():
        return {"status": "noop", "reason": "disabled_not_installed", "changed": False, "poll_sec": profile.get("poll_sec", 60)}

    installed = False
    if enabled:
        installed, client = _ensure_fail2ban_installed()
    elif not client:
        return {"status": "noop", "reason": "disabled_client_missing", "changed": False, "poll_sec": profile.get("poll_sec", 60)}
    assert client is not None

    # Debian's legacy nftables-allports action can report tickets as banned
    # while the nft table was never created.  The generic iptables action uses
    # the host's active netfilter compatibility layer and is verifiable.
    action = "iptables[type=allports, name={name}]" if shutil.which("iptables") else "nftables[type=allports, name={name}]"
    before_filter = FILTER_PATH.read_text(encoding="utf-8", errors="ignore") if FILTER_PATH.is_file() else None
    before_mautic_filter = (
        MAUTIC_AUTH_FILTER_PATH.read_text(encoding="utf-8", errors="ignore")
        if MAUTIC_AUTH_FILTER_PATH.is_file()
        else None
    )
    before_jail = JAIL_PATH.read_text(encoding="utf-8", errors="ignore") if JAIL_PATH.is_file() else None
    changed_files = False
    changed_files |= _atomic_write(FILTER_PATH, _filter_config())
    changed_files |= _atomic_write(MAUTIC_AUTH_FILTER_PATH, _mautic_auth_filter_config())
    changed_files |= _atomic_write(
        JAIL_PATH,
        _jail_config(enabled=enabled, allowlist=allowlist, action=action),
    )
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    os.chmod(LOG_PATH, 0o640)
    try:
        _client_ok(_run(client, "-t", timeout=120), "fail2ban config validation")
        _client_ok(_run("systemctl", "enable", "--now", "fail2ban", timeout=120), "fail2ban enable")
        if changed_files:
            # A reload retains an old action name for an existing jail on some
            # Fail2ban releases. Restarting preserves persistent tickets and
            # makes the new managed chain authoritative before verification.
            _client_ok(_run("systemctl", "restart", "fail2ban", timeout=120), "fail2ban restart")
        # Existing hosts can have an independently managed SSH jail or no
        # SSH Fail2ban jail at all. That must not prevent the separate MCC web
        # scanner blocklist from being installed. Verify the SSH chain only
        # when this host exposes an active SSH jail.
        if _jail_is_active(client, "sshd"):
            _wait_for_ssh_firewall_action(action)
        elif changed_files:
            logging.info("security blocklist: SSH Fail2ban jail is not active; web blocklist remains enabled")
    except Exception:
        if before_filter is None:
            FILTER_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(FILTER_PATH, before_filter)
        if before_mautic_filter is None:
            MAUTIC_AUTH_FILTER_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(MAUTIC_AUTH_FILTER_PATH, before_mautic_filter)
        if before_jail is None:
            JAIL_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(JAIL_PATH, before_jail)
        _run(client, "reload", timeout=120)
        raise

    if not enabled:
        current = _status_ips(client, JAIL_NAME)
        for ip_value in sorted(current):
            _run(client, "set", JAIL_NAME, "unbanip", ip_value)
        return {
            "status": "applied" if current or changed_files else "noop",
            "reason": "disabled",
            "changed": bool(current or changed_files),
            "removed": len(current),
            "poll_sec": profile.get("poll_sec", 60),
        }

    current = _status_ips(client, JAIL_NAME)
    added = sorted(desired - current)
    removed = sorted(current - desired)
    # Preserve protection when a host is interrupted mid-reconcile: install
    # current desired blocks before removing stale entries. A stale block is
    # safer than a temporary unprotected source IP.
    for batch in _address_batches(added):
        _client_ok(
            _run(client, "set", JAIL_NAME, "banip", *batch, timeout=120),
            f"ban {len(batch)} addresses",
        )
    for batch in _address_batches(removed):
        _client_ok(
            _run(client, "set", JAIL_NAME, "unbanip", *batch, timeout=120),
            f"unban {len(batch)} addresses",
        )
    for jail in ("sshd", "mautic-auth-nginx"):
        for batch in _address_batches(sorted(allowlist)):
            _run(client, "set", jail, "unbanip", *batch, timeout=120)

    return {
        "status": "applied" if installed or changed_files or added or removed else "noop",
        "changed": bool(installed or changed_files or added or removed),
        "installed": installed,
        "desired": len(desired),
        "added": added,
        "removed": removed,
        "poll_sec": profile.get("poll_sec", 60),
        "generated_at": str(profile.get("generated_at") or ""),
    }


def sync_security_blocklist_once(cfg: AgentConfig) -> dict[str, Any]:
    SYNC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SYNC_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "noop", "reason": "sync_already_running"}
        try:
            required_allowlist = _ensure_control_plane_allowlist(cfg)
            fetched = fetch_security_blocklist(cfg)
            if str(fetched.get("status") or "").strip().lower() != "ok":
                return {"status": "error", "reason": fetched.get("reason", "fetch_failed"), "fetch": fetched}
            profile = fetched.get("profile")
            if not isinstance(profile, dict):
                return {"status": "error", "reason": "invalid profile payload", "fetch": fetched}
            applied = apply_security_blocklist_profile(profile, required_allowlist=required_allowlist)
            return {"status": str(applied.get("status") or "error"), "fetch": fetched, "apply": applied}
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
