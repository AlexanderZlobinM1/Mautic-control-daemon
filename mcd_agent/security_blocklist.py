from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import fcntl
import ipaddress
import json
import logging
from pathlib import Path
import socket
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.custom_scripts import run_configured_external_operation
from mcd_agent.host_identity import resolve_agent_identity


SYNC_LOCK_PATH = Path("/run/mcd/security-blocklist.lock")
SECURITY_EXTERNAL_COMPONENT = "security_blocklist"
SECURITY_EXTERNAL_OPERATIONS = {
    "security_blocklist_apply",
    "security_blocklist_status",
    "security_blocklist_cleanup",
}
SECURITY_EXTERNAL_PATH_FIELDS = (
    "filter_path",
    "auth_filter_path",
    "jail_path",
    "log_path",
    "state_path",
)
SECURITY_EXTERNAL_REQUIRED_APPLY_FIELDS = (
    "managed_root",
    "enabled",
    "allowlist",
    "blocked",
    "required_allowlist",
    *SECURITY_EXTERNAL_PATH_FIELDS,
    "client_path",
    "jail_name",
    "ssh_jails",
    "action_template",
    "generated_at",
    "validate_command",
    "reload_command",
    "ban_command_prefix",
    "unban_command_prefix",
)
SECURITY_EXTERNAL_STATUS_FIELDS = (
    "managed_root",
    *SECURITY_EXTERNAL_PATH_FIELDS,
    "jail_name",
)


def _security_poll_interval(value: Any, *, default: int = 300) -> int:
    """Clamp the MCC-provided security poll interval to the daemon contract."""
    try:
        candidate = int(value) if value is not None and str(value).strip() else int(default)
    except (TypeError, ValueError):
        candidate = int(default)
    return max(30, min(3600, candidate))


def _security_root_relative_path(value: Any) -> str:
    """Normalize a caller root to the external runner's root-relative contract."""
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise ValueError("security_external_managed_root_invalid")
    parts = raw.split("/")
    if raw.startswith("/"):
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("security_external_managed_root_invalid")
    return "/".join(parts)


def _security_relative_path(value: Any, field: str) -> str:
    """Admit only clean paths relative to the external managed root."""
    raw = str(value or "").strip()
    if not raw or "\x00" in raw or raw.startswith("/"):
        raise ValueError(f"security_external_{field}_invalid")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"security_external_{field}_invalid")
    return "/".join(parts)


def _security_external_binding_present(config: AgentConfig, operation: str) -> bool:
    """Return whether the exact component-qualified MCC binding is installed."""
    if operation not in SECURITY_EXTERNAL_OPERATIONS:
        return False
    try:
        manifest = Path(str(getattr(config, "custom_cache_dir"))) / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 256 * 1024:
            return False
        document = json.loads(manifest.read_text(encoding="utf-8"))
        bindings = document.get("operation_bindings") if isinstance(document, dict) else None
        return isinstance(bindings, list) and any(
            isinstance(item, dict)
            and item.get("component") == SECURITY_EXTERNAL_COMPONENT
            and item.get("operation") == operation
            for item in bindings
        )
    except (AttributeError, OSError, UnicodeError, ValueError, TypeError):
        return False


def _security_external_runtime(
    profile: dict[str, Any],
    *,
    required_allowlist: set[str],
    operation: str,
) -> dict[str, Any]:
    """Build the exact non-secret runtime map declared by the MCC binding."""
    raw_runtime = profile.get("runtime", profile.get("external_runtime"))
    if raw_runtime is None:
        raw_runtime = profile
    if not isinstance(raw_runtime, dict):
        raise ValueError("security_external_runtime_invalid")
    runtime = dict(raw_runtime)
    runtime["managed_root"] = _security_root_relative_path(runtime.get("managed_root"))
    for field in SECURITY_EXTERNAL_PATH_FIELDS:
        runtime[field] = _security_relative_path(runtime.get(field), field)
    runtime["jail_name"] = str(runtime.get("jail_name") or "").strip()
    if not runtime["jail_name"] or any(char in runtime["jail_name"] for char in "\x00\n\r"):
        raise ValueError("security_external_jail_name_invalid")
    if operation == "security_blocklist_apply":
        for field in SECURITY_EXTERNAL_REQUIRED_APPLY_FIELDS:
            if field not in runtime:
                raise ValueError("security_external_runtime_invalid")
        runtime["enabled"] = bool(profile.get("enabled", runtime.get("enabled")))
        runtime["allowlist"] = sorted(_normalize_addresses(profile.get("allowlist", runtime.get("allowlist"))))
        runtime["blocked"] = sorted(_normalize_addresses(profile.get("blocked", runtime.get("blocked"))))
        runtime["required_allowlist"] = sorted(required_allowlist)
    return runtime


def _decode_security_external_result(code: int, output: str, *, operation: str) -> dict[str, Any]:
    """Decode the external runner's typed public security result without fallback."""
    try:
        result = json.loads(output)
    except (TypeError, ValueError, UnicodeError):
        return {"status": "error", "reason": "security_external_result_invalid"}
    if not isinstance(result, dict):
        return {"status": "error", "reason": "security_external_result_invalid"}
    if code != 0:
        if (
            result.get("schema") == "mcd-self-hosted-executable-output-v1"
            and result.get("frame_type") == "result"
            and result.get("status") == "failed"
            and isinstance(result.get("result"), dict)
        ):
            nested = dict(result["result"])
            nested["status"] = "error"
            nested.setdefault("reason", result.get("error_code") or "security_external_failed")
            return nested
        return {"status": "error", "reason": str(result.get("reason") or result.get("error_code") or "security_external_failed")}
    if result.get("schema") != "mcd-security-os-v1" or result.get("component") != "security_os":
        return {"status": "error", "reason": "security_external_result_invalid"}
    expected_operation = {
        "security_blocklist_apply": "apply",
        "security_blocklist_status": "status",
        "security_blocklist_cleanup": "cleanup",
    }.get(operation)
    if expected_operation is None or str(result.get("operation") or "") != expected_operation:
        return {"status": "error", "reason": "security_external_result_operation_mismatch"}
    if str(result.get("status") or "") not in {"applied", "noop", "cleaned", "status", "error"}:
        return {"status": "error", "reason": "security_external_result_status_invalid"}
    return result


def _configured_security_operation(
    config: AgentConfig,
    *,
    operation: str,
    profile: dict[str, Any],
    required_allowlist: set[str] | None = None,
    operation_input: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run an admitted MCC security operation; None means no binding is installed."""
    if not _security_external_binding_present(config, operation):
        return None
    try:
        runtime_profile = dict(profile)
        if operation_input is not None:
            runtime_profile["runtime"] = dict(operation_input)
        runtime = _security_external_runtime(
            runtime_profile,
            required_allowlist=set(required_allowlist or set()),
            operation=operation,
        )
        if operation in {"security_blocklist_status", "security_blocklist_cleanup"}:
            runtime = {field: runtime[field] for field in SECURITY_EXTERNAL_STATUS_FIELDS}
    except ValueError as exc:
        return {"status": "error", "reason": str(exc)}
    code, output = run_configured_external_operation(
        config,
        component=SECURITY_EXTERNAL_COMPONENT,
        operation=operation,
        profile={},
        operation_input=runtime,
    )
    if code == 2 and output == "custom_operation_binding_not_configured":
        return {"status": "error", "reason": "security_external_binding_lost"}
    return _decode_security_external_result(code, output, operation=operation)


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
    item = ipaddress.ip_network(raw, strict=False) if "/" in raw else ipaddress.ip_address(raw)
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
        if target.version == allowed.version and (target.subnet_of(allowed) or allowed.subnet_of(target)):
            return True
    return False


def _control_plane_addresses(cfg: AgentConfig) -> set[str]:
    """Resolve addresses that the external recipe must never block."""
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


def sync_security_blocklist_once(cfg: AgentConfig) -> dict[str, Any]:
    """Fetch policy and dispatch the complete host mutation to MCC's recipe."""
    SYNC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SYNC_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "noop", "reason": "sync_already_running"}
        try:
            required_allowlist = _control_plane_addresses(cfg)
            fetched = fetch_security_blocklist(cfg)
            if str(fetched.get("status") or "").strip().lower() != "ok":
                return {"status": "error", "reason": fetched.get("reason", "fetch_failed"), "fetch": fetched}
            profile = fetched.get("profile")
            if not isinstance(profile, dict):
                return {"status": "error", "reason": "invalid profile payload", "fetch": fetched}
            external = _configured_security_operation(
                cfg,
                operation="security_blocklist_apply",
                profile=profile,
                required_allowlist=required_allowlist,
            )
            if external is None:
                return {
                    "status": "error",
                    "reason": "security_external_binding_not_configured",
                    "fetch": fetched,
                }
            external["poll_sec"] = _security_poll_interval(profile.get("poll_sec"))
            return {
                "status": str(external.get("status") or "error"),
                "fetch": fetched,
                "apply": external,
            }
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
