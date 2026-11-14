"""Read-only selected-instance context for immutable MCC upgrade plans."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcd_agent import __version__
from mcd_agent.mautic_patch_fact_binding import discover_execution_context

SCHEMA = "mcd-mautic-patch-context-v1"
FEATURES = {"typed_context_preflight_v1": True}


def load_readonly_discovery_config(config_path: str):
    """Parse only discovery inputs, without migrations or config history."""
    from mcd_agent.config import (_load_toml_with_includes, _normalize_list,
                                  _normalize_int_list, _parse_manual_instances)
    data = _load_toml_with_includes(config_path)
    discovery = data.get("discovery", {})
    if not isinstance(discovery, dict):
        raise ValueError("invalid discovery config")
    mcc = data.get("mcc", {})
    if not isinstance(mcc, dict):
        mcc = {}
    return SimpleNamespace(
        discovery_roots=_normalize_list(discovery.get("roots", ["/var/www"])),
        exclude_path_contains=_normalize_list(discovery.get("exclude_path_contains", [])),
        supported_mautic_majors=_normalize_int_list(discovery.get("supported_mautic_majors", [4, 5, 6, 7])),
        custom_instances=_parse_manual_instances(data.get("instances", [])),
        mcc_host_name=str(mcc.get("host_name") or "").strip(),
    )


def preflight(config_path: str, root: str, instance_uid: str) -> dict[str, Any]:
    """No ordinary config loader: it migrates files and records history."""
    if (not isinstance(instance_uid, str) or not instance_uid or instance_uid.strip() != instance_uid
            or not root or not Path(root).is_absolute() or ".." in Path(root).parts
            or str(Path(root)) != root):
        raise ValueError("patch_context_selection_invalid")
    try:
        config = load_readonly_discovery_config(config_path)
        from mcd_agent.mautic_patch_stage import application_root
        expected_application_root = str(application_root(root))
        context = discover_execution_context(config, SimpleNamespace(root=root, instance_uid=instance_uid))
    except Exception as exc:
        # Config and local PHP errors may contain secret-bearing text.
        raise ValueError("patch_context_discovery_unavailable") from exc
    if context is None or context["application_root"] != expected_application_root:
        raise ValueError("patch_context_unknown_or_mismatch")
    return {"schema": SCHEMA, "agent_version": __version__, "features": dict(FEATURES),
            "execution_context": context}


def error(reason: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "agent_version": __version__, "features": dict(FEATURES),
            "status": "error", "code": reason}
