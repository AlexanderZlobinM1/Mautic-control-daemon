from __future__ import annotations

import re
import subprocess
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


CRON_TASK_TYPES = {"segment", "segment_sql", "campaign_trigger", "campaign_rebuild", "campaign_update"}
IMPORT_TASK_TYPES = {"import"}
CACHE_TASK_TYPES = {"cache_clear", "cache_warm", "cache_warmup", "cache_hard"}


def _local_ip_identity_values() -> set[str]:
    vals: set[str] = set()
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        out = ""
    for token in re.split(r"\s+", out.strip()):
        ip = token.strip().split("%", 1)[0]
        if not ip or ":" in ip:
            continue
        if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip):
            continue
        vals.add(ip.lower())
        vals.add("host-" + ip.replace(".", "-").lower())
    return vals


def cluster_local_identity_values(cfg: AgentConfig) -> set[str]:
    vals: set[str] = set()
    for raw in (getattr(cfg, "mcc_host_name", None),):
        text = str(raw or "").strip().lower()
        if text:
            vals.add(text)
    try:
        ident = resolve_agent_identity(cfg)
    except Exception:
        ident = {}
    if isinstance(ident, dict):
        for key in (
            "local_hostname",
            "effective_hostname",
            "configured_host_name",
            "effective_mcc_host_name",
            "source_host_name",
        ):
            text = str(ident.get(key) or "").strip().lower()
            if text:
                vals.add(text)
    vals.update(_local_ip_identity_values())
    return vals


def _clean_host(value: Any) -> str:
    return str(value or "").strip()


def _clean_host_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = [x.strip() for x in values.replace("\n", ",").split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw = [str(x).strip() for x in values]
    else:
        raw = [str(values).strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def cluster_route_for_task_type(task_type: str) -> str:
    t = str(task_type or "").strip()
    if t in CRON_TASK_TYPES:
        return "cron"
    if t in IMPORT_TASK_TYPES:
        return "import"
    if t in CACHE_TASK_TYPES:
        return "cache"
    if t == "backup":
        return "backup"
    return "local"


def cluster_route_for_command(command: str) -> str:
    cmd = str(command or "").strip()
    if cmd in {
        "segments:update",
        "campaign:trigger",
        "campaign:rebuild",
        "campaigns:rebuild",
        "campaigns:update",
        "campaigns:trigger",
    }:
        return "cron"
    if cmd == "import":
        return "import"
    if cmd in {"cache:clear", "cache:warmup", "cache:hard"}:
        return "cache"
    return "local"


def cluster_route_targets(cfg: AgentConfig, route: str) -> list[str]:
    if not getattr(cfg, "cluster_id", None) or not bool(getattr(cfg, "cluster_routing_enabled", True)):
        return []
    route_name = str(route or "").strip().lower()
    if route_name == "cron":
        return _clean_host_list(getattr(cfg, "cluster_route_cron_host", None))
    if route_name == "import":
        return _clean_host_list(getattr(cfg, "cluster_route_import_host", None))
    if route_name == "backup":
        host = _clean_host(getattr(cfg, "cluster_route_backup_host", None)) or _clean_host(
            getattr(cfg, "backup_cluster_authority_host", None)
        )
        return _clean_host_list(host)
    if route_name == "cache":
        return _clean_host_list(getattr(cfg, "cluster_route_cache_hosts", []))
    return []


def cluster_route_authority_status(cfg: AgentConfig, route: str) -> dict[str, Any]:
    route_name = str(route or "").strip().lower()
    if not getattr(cfg, "cluster_id", None):
        return {"allowed": True, "reason": "not clustered", "route": route_name, "targets": []}
    if not bool(getattr(cfg, "cluster_routing_enabled", True)):
        return {"allowed": True, "reason": "cluster routing disabled", "route": route_name, "targets": []}
    targets = cluster_route_targets(cfg, route_name)
    if not targets:
        return {"allowed": True, "reason": "route target not configured", "route": route_name, "targets": []}
    local = cluster_local_identity_values(cfg)
    for target in targets:
        if target.strip().lower() in local:
            return {
                "allowed": True,
                "reason": "route target match",
                "route": route_name,
                "targets": targets,
                "local_identity": sorted(local),
            }
    return {
        "allowed": False,
        "reason": "route target mismatch",
        "route": route_name,
        "targets": targets,
        "local_identity": sorted(local),
    }


def cluster_route_allows(cfg: AgentConfig, route: str) -> bool:
    return bool(cluster_route_authority_status(cfg, route).get("allowed", False))
