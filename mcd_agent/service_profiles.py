from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent.apt_profile import apply_apt_profile
from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.service_profile_external import (
    CUSTOM_RUNTIME_CAPABILITIES, ExternalProfileOperationError, apply_external_service_profile,
)
from mcd_agent.mautic_db_indexes import apply_mautic_db_indexes
from mcd_agent.wazuh_profile import apply_wazuh_profile


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


def fetch_service_profile(cfg: AgentConfig, component: str) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    comp = (component or "").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "docker", "apt", "wazuh", "mautic_db_indexes", "db_indexes"}:
        return {"status": "error", "reason": f"unsupported component: {component}"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    if comp in {"php_fpm", "mysql", "docker"}:
        payload["external_handler_capabilities"] = sorted(CUSTOM_RUNTIME_CAPABILITIES)
    url = base + f"/api/v1/agent/service-profile/{comp.replace('_', '-')}"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def fetch_php_fpm_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "php_fpm")


def fetch_mysql_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "mysql")


def fetch_apt_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "apt")


def fetch_wazuh_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "wazuh")


def _service_profile_apply_outcome(applied: dict[str, Any]) -> tuple[str, str | None]:
    status = str(applied.get("status") or "").strip().lower()
    if status in {"applied", "planned", "noop", "disabled", "ok", "already_present"}:
        return "ok", None
    if status in {"skipped", "deferred"}:
        return "skipped", str(applied.get("reason") or status or "skipped")
    return "error", str(applied.get("reason") or status or "apply_failed")


def _docker_runtime_state() -> dict[str, Any]:
    docker = shutil.which("docker") or ""
    active = False
    daemon_reachable = False
    version = ""
    if docker:
        proc = subprocess.run([docker, "--version"], capture_output=True, text=True, check=False)
        version = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[0] if ((proc.stdout or "") + (proc.stderr or "")).strip() else ""
        active_proc = subprocess.run(
            ["systemctl", "is-active", "docker"], capture_output=True, text=True, check=False
        )
        active = active_proc.returncode == 0 and (active_proc.stdout or "").strip() == "active"
        info = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        daemon_reachable = info.returncode == 0 and bool((info.stdout or "").strip())
    return {
        "installed": bool(docker),
        "path": docker,
        "version": version,
        "active": active,
        "daemon_reachable": daemon_reachable,
    }


def apply_docker_profile(*, dry_run: bool = False) -> dict[str, Any]:
    """Install the local Docker runtime only after an explicit operator action."""
    before = _docker_runtime_state()
    if before["installed"] and before["active"] and before["daemon_reachable"]:
        return {"status": "noop", "reason": "docker_runtime_ready", "before": before, "after": before}
    commands: list[list[str]] = []
    if not before["installed"]:
        commands.extend(
            [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "docker.io"],
            ]
        )
    commands.append(["systemctl", "enable", "--now", "docker"])
    if dry_run:
        return {"status": "planned", "before": before, "commands": commands}
    if os.geteuid() != 0:
        raise RuntimeError("Docker installation requires root")
    for command in commands:
        proc = subprocess.run(command, capture_output=True, text=True, check=False, timeout=1800)
        if proc.returncode != 0:
            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            return {
                "status": "error",
                "reason": f"command_failed:{' '.join(command)}:{detail[-1000:]}",
                "before": before,
                "commands": commands,
            }
    after = _docker_runtime_state()
    if not (after["installed"] and after["active"] and after["daemon_reachable"]):
        return {
            "status": "error",
            "reason": "docker_runtime_verification_failed",
            "before": before,
            "after": after,
            "commands": commands,
        }
    return {"status": "applied", "before": before, "after": after, "commands": commands}


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
) -> dict[str, Any]:
    comp = (component or "php_fpm").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "apt", "wazuh", "docker", "mautic_db_indexes", "db_indexes"}:
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
    fetched = fetch_service_profile(cfg, comp)
    status = str(fetched.get("status", "")).strip().lower()
    if status != "ok":
        return {"status": "error", "reason": fetched.get("reason", status or "fetch_failed"), "fetch": fetched}
    profile = fetched.get("profile")
    if not isinstance(profile, dict):
        return {"status": "error", "reason": "invalid profile payload", "fetch": fetched}
    external_failed = False
    if comp in {"php_fpm", "mysql", "docker"}:
        # Only delivery/admission lives here; the artifact owns the algorithm.
        # No builtin writer or fallback exists after external selection.
        try:
            applied = apply_external_service_profile(cfg, fetched, component=comp, dry_run=dry_run)
        except ExternalProfileOperationError as exc:
            applied = exc.public_result
            external_failed = True
    elif comp == "wazuh":
        applied = apply_wazuh_profile(profile, cfg, dry_run=dry_run)
    else:
        if os.geteuid() != 0:
            raise RuntimeError("service-profile apply requires root")
        applied = apply_apt_profile(
            profile,
            dry_run=dry_run,
            cfg=cfg,
            force_zabbix_bootstrap=not bool(dry_run),
            force_repo_rescan=bool(force_apt_repo_rescan),
        )
    result_status, reason = _service_profile_apply_outcome(applied)
    if external_failed and result_status == "ok":
        result_status, reason = "error", "external_handler_execution_failed"
    out = {"status": result_status, "fetch": fetched, "apply": applied}
    if reason:
        out["reason"] = reason
    return out
