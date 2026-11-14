from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import json
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.service_profile_external import (
    CUSTOM_RUNTIME_CAPABILITIES, nginx_operation_input_valid, ExternalProfileOperationError, apply_external_service_profile,
    validate_external_service_profile,
)
from mcd_agent.custom_scripts import CustomSelectedTaskError, load_configured_profile_cache, run_configured_external_operation
from mcd_agent.mautic_db_indexes import apply_mautic_db_indexes


_MYSQL_GALERA_MARKERS = (
    "wsrep_on",
    "wsrep_provider",
    "wsrep_cluster_address",
    "wsrep_cluster_name",
)

def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = mcc_request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


def fetch_service_profile(cfg: AgentConfig, component: str, *, include_runtime_input: bool = False,
                          operation: str | None = None, operation_input: dict[str, Any] | None = None,
                          profile_present: bool | None = None,
                          cache_operation: str | None = None) -> dict[str, Any]:
    comp = (component or "").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "docker", "apt", "wazuh", "nginx", "mautic_db_indexes", "db_indexes"}:
        return {"status": "error", "reason": f"unsupported component: {component}"}
    base = _api_base(cfg)
    if not base:
        if cache_operation:
            try:
                cached = load_configured_profile_cache(
                    cfg, component=comp, operation=cache_operation,
                )
            except CustomSelectedTaskError as exc:
                return {"status": "error", "reason": str(exc)}
            if cached is not None:
                return {
                    "status": "ok",
                    "component": comp,
                    "profile": cached["profile"],
                    "profile_source": cached["source"],
                }
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    if comp == "nginx":
        if (not nginx_operation_input_valid(operation, operation_input)
                or (operation == "cloudflare_real_ip_state" and type(profile_present) is not bool)
                or (operation != "cloudflare_real_ip_state" and profile_present is not None)):
            return {"status": "error", "reason": "nginx_operation_input_invalid"}
    elif operation is not None:
        # Explicit non-nginx operations are data selected by MCC.  MCD only
        # carries the opaque operation coordinates to the authority and later
        # checks that the returned external handler is bound to the same input.
        if not isinstance(operation, str) or not operation.strip() or not isinstance(operation_input, dict):
            return {"status": "error", "reason": "service_profile_operation_input_invalid"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    if comp == "wazuh":
        identity_fields = {
            "mcc_host_name": "effective_mcc_host_name",
            "configured_host_name": "configured_host_name",
            "agent_hostname": "local_hostname",
        }
        if any(source not in ident for source in identity_fields.values()):
            return {"status": "error", "reason": "wazuh_identity_context_unavailable"}
        # Preserve explicit null facts; the legacy string envelope loses them.
        payload.update({target: ident[source] for target, source in identity_fields.items()})
    if comp == "nginx":
        from mcd_agent.nginx_baseline import _ipv6_listen_forbidden
        payload.update(operation=operation, operation_input=operation_input,
                       ipv6_listen_forbidden=_ipv6_listen_forbidden())
        if operation == "cloudflare_real_ip_state":
            payload["profile_present"] = profile_present
    elif operation is not None:
        payload.update(operation=operation, operation_input=operation_input)
    if comp in {"php_fpm", "mysql", "docker", "wazuh", "nginx", "apt"}:
        payload["external_handler_capabilities"] = sorted(CUSTOM_RUNTIME_CAPABILITIES)
    url = base + f"/api/v1/agent/service-profile/{comp.replace('_', '-')}"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        if comp == "nginx" and out["status"] == "ok":
            if (out.get("operation") != operation or json.dumps(out.get("operation_input"), sort_keys=True) != json.dumps(operation_input, sort_keys=True)
                    or "runtime_input" in out
                    or (operation == "cloudflare_real_ip_state" and (out.get("profile") is not None) != profile_present)):
                raise ValueError("nginx_response_input_mismatch")
            handler = out.get("external_handler")
            if isinstance(handler, dict) and handler.get("active") is True and handler.get("state") == "active":
                validate_external_service_profile(cfg, out, component="nginx")
                from mcd_agent.custom_scripts import cache_selected_task, run_custom_script_by_key
                artifact = handler["artifact"]
                code, _output = run_custom_script_by_key(cfg, script_key=artifact["key"],
                    expected_sha256=artifact["sha256"], expected_entrypoint=artifact["entrypoint"], fetch_only=True)
                if code != 0:
                    raise ValueError("nginx_selected_task_prefetch_failed")
                cache_selected_task(cfg, out)
        return out if include_runtime_input else public_service_profile_response(out)
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def public_service_profile_response(fetched: dict[str, Any]) -> dict[str, Any]:
    """Private execution input stays in memory, never in CLI/task receipts."""
    return {key: value for key, value in fetched.items() if key != "runtime_input"}


def fetch_php_fpm_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "php_fpm")


def fetch_mysql_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "mysql")


def fetch_wazuh_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "wazuh")


def _service_profile_apply_outcome(applied: dict[str, Any]) -> tuple[str, str | None]:
    status = str(applied.get("status") or "").strip().lower()
    if status in {"applied", "planned", "noop", "disabled", "ok", "already_present"}:
        return "ok", None
    if status in {"skipped", "deferred"}:
        return "skipped", str(applied.get("reason") or status or "skipped")
    return "error", str(applied.get("reason") or status or "apply_failed")


def _decode_configured_operation_result(code: int, output: str) -> dict[str, Any]:
    """Preserve the public result inside the established failed terminal frame."""
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        return {"status": "error", "reason": output or "custom_operation_receipt_invalid"}
    if not isinstance(result, dict):
        return {"status": "error", "reason": "custom_operation_receipt_invalid"}
    if code == 0:
        return result
    if (
        result.get("schema") == "mcd-self-hosted-executable-output-v1"
        and result.get("frame_type") == "result"
        and result.get("status") == "failed"
        and isinstance(result.get("result"), dict)
    ):
        # The executor's terminal envelope is transport metadata. The nested
        # result is the operation's public typed receipt and must remain the
        # service-profile API result.
        nested = dict(result["result"])
        if str(nested.get("status") or "").strip().lower() != "error":
            nested["status"] = "error"
            nested.setdefault(
                "reason",
                str(result.get("error_code") or "custom_operation_failed"),
            )
        return nested
    return {
        "status": "error",
        "reason": str(result.get("reason") or result.get("error_code") or "custom_operation_failed"),
        "external": result,
    }


def _configured_external_operation(
    component: str,
    profile: dict[str, Any],
    *,
    cfg: AgentConfig,
    operation: str,
    operation_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Dispatch one declared operation through the existing custom seam.

    ``None`` means that MCC has not installed this optional binding yet.  Any
    other dispatch or terminal failure is returned as an error and must not
    silently fall back to an implementation in MCD.
    """
    code, output = run_configured_external_operation(
        cfg,
        component=component,
        operation=operation,
        profile=profile,
        operation_input=operation_input,
    )
    if code == 2 and output == "custom_operation_binding_not_configured":
        return None
    return _decode_configured_operation_result(code, output)


def _agent_cluster_mode(cfg: AgentConfig) -> bool:
    return bool(str(getattr(cfg, "cluster_id", "") or "").strip())


def _cluster_db_maintenance_guard(
    cfg: AgentConfig,
    component: str,
    *,
    allow_cluster_db_maintenance: bool = False,
) -> dict[str, Any] | None:
    """
    Cluster DB/schema changes must not be daemon side effects.

    Galera/PXC can turn even "online" ALTERs into cluster-wide TOI/NBO stalls.
    In cluster mode, DB-heavy service-profile components are therefore skipped
    unless an operator explicitly runs a maintenance command with the dedicated
    override flag.
    """
    comp = (component or "").strip().lower().replace("-", "_")
    if comp not in {"mysql", "mautic_db_indexes", "db_indexes"}:
        return None
    if not (_agent_cluster_mode(cfg) or _mysql_galera_config_detected()):
        return None
    if allow_cluster_db_maintenance:
        return None
    return {
        "status": "skipped",
        "reason": "cluster_db_maintenance_requires_explicit_operator_flag",
        "component": comp,
        "cluster_id": str(getattr(cfg, "cluster_id", "") or "").strip(),
        "manual_command": (
            "mcd-cli service-profile apply --component "
            f"{comp} --allow-cluster-db-maintenance"
        ),
    }


def _mysql_galera_config_detected() -> bool:
    paths = [
        Path("/etc/mysql/my.cnf"),
        Path("/etc/mysql/mysql.cnf"),
        Path("/etc/mysql/conf.d/galera.cnf"),
        Path("/etc/mysql/mysql.conf.d/galera.cnf"),
        Path("/etc/mysql/mysql.conf.d/zz-ananas-cluster-role.cnf"),
    ]
    try:
        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if any(marker in text for marker in _MYSQL_GALERA_MARKERS):
                return True
    except Exception:
        return False
    return False


def service_profiles_apply_once(
    cfg: AgentConfig,
    *,
    component: str = "php_fpm",
    dry_run: bool = False,
    force_apt_repo_rescan: bool = False,
    allow_cluster_db_maintenance: bool = False,
    operation: str | None = None, operation_input: dict[str, Any] | None = None,
    profile_present: bool | None = None,
) -> dict[str, Any]:
    comp = (component or "php_fpm").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "apt", "wazuh", "docker", "nginx", "mautic_db_indexes", "db_indexes"}:
        return {"status": "skipped", "reason": f"unsupported component: {component}"}
    guard = _cluster_db_maintenance_guard(
        cfg,
        comp,
        allow_cluster_db_maintenance=allow_cluster_db_maintenance,
    )
    if guard is not None:
        return guard
    if comp in {"mautic_db_indexes", "db_indexes"}:
        applied = apply_mautic_db_indexes(cfg, dry_run=dry_run)
        status = str(applied.get("status") or "").strip().lower()
        if status in {"applied", "noop", "planned"}:
            return {"status": "ok", "fetch": {"status": "ok", "source": "builtin"}, "apply": applied}
        if status == "deferred":
            return {"status": "skipped", "reason": applied.get("reason", "deferred"), "apply": applied}
        return {"status": "error", "reason": applied.get("reason", status or "apply_failed"), "apply": applied}
    operation_options = (
        dict(operation=operation, operation_input=operation_input, profile_present=profile_present)
        if comp == "nginx" or operation is not None else {}
    )
    cache_operation = "apt_apply" if comp == "apt" else None
    fetched = fetch_service_profile(
        cfg, comp, include_runtime_input=True,
        cache_operation=cache_operation,
        **operation_options,
    )
    public_fetch = public_service_profile_response(fetched)
    status = str(fetched.get("status", "")).strip().lower()
    if status != "ok":
        return {"status": "error", "reason": fetched.get("reason", status or "fetch_failed"), "fetch": public_fetch}
    profile = fetched.get("profile")
    if not isinstance(profile, dict) and not (comp == "nginx" and profile is None):
        return {"status": "error", "reason": "invalid profile payload", "fetch": public_fetch}
    external_failed = False
    if comp in {"php_fpm", "mysql", "docker", "wazuh", "nginx"}:
        # Only delivery/admission lives here; the artifact owns the algorithm.
        # No builtin writer or fallback exists after external selection.
        try:
            applied = apply_external_service_profile(cfg, fetched, component=comp, dry_run=dry_run)
        except ExternalProfileOperationError as exc:
            applied = exc.public_result
            external_failed = True
        except (ValueError, RuntimeError) as error:
            if comp != "nginx":
                raise
            return {"status": "error", "reason": str(error), "fetch": public_fetch}
    else:
        external_apply = _configured_external_operation(
            "apt",
            profile,
            cfg=cfg,
            operation="apt_apply",
            operation_input={
                "dry_run": bool(dry_run),
                "force_repo_rescan": bool(force_apt_repo_rescan),
            },
        )
        if external_apply is not None:
            # The owner composition owns ordering, system facts and all
            # component operations. MCD only admits and executes one bound
            # operation, then preserves its public receipt semantics.
            applied = external_apply
        else:
            # APT is an MCC-owned external composition.  Absence of its
            # binding is a concrete configuration error, never permission to
            # execute the retired builtin implementation.
            applied = {"status": "error", "reason": "apt_apply_binding_not_configured"}
    if comp == "nginx":
        # The external wrapper owns writer/observer outcome semantics, including
        # successful drift observations. Reuse its validated terminal decision.
        result_status = "error" if external_failed else "ok"
        reason = str(applied.get("reason") or applied.get("error") or "apply_failed") if external_failed else None
    else:
        result_status, reason = _service_profile_apply_outcome(applied)
    if external_failed and result_status == "ok":
        result_status, reason = "error", "external_handler_execution_failed"
    out = {"status": result_status, "fetch": public_fetch, "apply": applied}
    if reason:
        out["reason"] = reason
    return out
