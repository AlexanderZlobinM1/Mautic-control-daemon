from __future__ import annotations

import json
import socket
from pathlib import Path

from mcd_agent.config import AgentConfig

_TEMPLATE_IDENTITY_PATH = Path("/opt/mcd/var/template_identity.json")
_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


def _read_machine_id() -> str:
    for p in _MACHINE_ID_PATHS:
        try:
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
                if raw:
                    return raw
        except Exception:
            continue
    return ""


def _read_template_marker() -> dict[str, str]:
    try:
        if not _TEMPLATE_IDENTITY_PATH.exists():
            return {}
        raw = json.loads(_TEMPLATE_IDENTITY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        source_host_name = str(raw.get("source_host_name", "")).strip()
        source_machine_id = str(raw.get("source_machine_id", "")).strip().lower()
        out: dict[str, str] = {}
        if source_host_name:
            out["source_host_name"] = source_host_name
        if source_machine_id:
            out["source_machine_id"] = source_machine_id
        return out
    except Exception:
        return {}


def _write_template_marker(source_host_name: str, source_machine_id: str) -> None:
    src = str(source_host_name or "").strip()
    mid = str(source_machine_id or "").strip().lower()
    if not src and not mid:
        return
    try:
        _TEMPLATE_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, str] = {}
        if src:
            payload["source_host_name"] = src
        if mid:
            payload["source_machine_id"] = mid
        _TEMPLATE_IDENTITY_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def resolve_agent_identity(cfg: AgentConfig) -> dict[str, object]:
    """
    Resolve host identity used for MCC API calls.

    For template clones:
    - original configured MCC host name is treated as source host
    - effective hostname switches to local OS hostname
    - mcc_host_name is intentionally blank to avoid matching source host row
    """
    local_hostname = (socket.gethostname() or "").strip() or "localhost"
    local_machine_id = _read_machine_id()
    configured_host_name = (cfg.mcc_host_name or "").strip()
    configured_template = bool(getattr(cfg, "host_template", False))
    autopromote_on_clone = bool(getattr(cfg, "template_autopromote_on_clone", True))
    marker = _read_template_marker()
    marker_source = str(marker.get("source_host_name", "")).strip()
    marker_machine_id = str(marker.get("source_machine_id", "")).strip().lower()
    marker_present = bool(marker_source or marker_machine_id)
    source_host_name = configured_host_name or marker_source
    source_machine_id = marker_machine_id
    template_capable = bool(configured_template or marker_present)
    if configured_template and not source_host_name:
        source_host_name = local_hostname
    if configured_template and source_host_name and (not marker_source or (local_machine_id and not marker_machine_id)):
        _write_template_marker(source_host_name, local_machine_id)
        if local_machine_id and not source_machine_id:
            source_machine_id = local_machine_id

    clone_by_hostname = bool(
        template_capable
        and autopromote_on_clone
        and source_host_name
        and local_hostname
        and source_host_name != local_hostname
    )
    clone_by_machine_id = bool(
        template_capable
        and autopromote_on_clone
        and source_machine_id
        and local_machine_id
        and source_machine_id != local_machine_id
    )
    clone_detected = bool(clone_by_hostname or clone_by_machine_id)
    effective_hostname = local_hostname if clone_detected else (configured_host_name or local_hostname)
    effective_mcc_host_name = "" if clone_detected else configured_host_name
    clone_reason = ""
    if clone_detected:
        if clone_by_machine_id:
            clone_reason = "machine_id_mismatch"
        elif clone_by_hostname:
            clone_reason = "hostname_mismatch"
    return {
        "local_hostname": local_hostname,
        "configured_host_name": configured_host_name or None,
        "effective_hostname": effective_hostname,
        "effective_mcc_host_name": effective_mcc_host_name,
        "is_template": bool(configured_template and not clone_detected),
        "autopromote_on_clone": autopromote_on_clone,
        "clone_detected": clone_detected,
        "clone_reason": clone_reason or None,
        "clone_by_hostname": clone_by_hostname,
        "clone_by_machine_id": clone_by_machine_id,
        "source_host_name": source_host_name or None,
    }
