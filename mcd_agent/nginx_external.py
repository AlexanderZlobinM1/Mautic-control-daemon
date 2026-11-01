"""Compatibility entrypoints for the selected external nginx task.

No OS recipes, host defaults, generators or profile formulas belong here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcd_agent.custom_scripts import (
    CustomSelectedTaskError, _PROFILE_NOT_SUPPLIED, cached_selected_task,
    standalone_custom_cache_config,
)
from mcd_agent.service_profile_external import (
    ExternalProfileOperationError, apply_external_service_profile,
)


def _invoke(operation: str, operation_input: dict[str, Any], *,
            profile: Any = _PROFILE_NOT_SUPPLIED, cfg: Any = None,
            cache_dir: str | None = None) -> dict[str, Any]:
    from mcd_agent.nginx_baseline import _ipv6_listen_forbidden
    config = cfg if cfg is not None else standalone_custom_cache_config(cache_dir)
    directory = getattr(config, "custom_cache_dir", None)
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise CustomSelectedTaskError("custom_cache_settings_invalid")
    context = {"ipv6_listen_forbidden": _ipv6_listen_forbidden()}
    options = dict(component="nginx", operation=operation, operation_input=operation_input,
                   runtime_context=context, profile=profile)
    try:
        fetched = cached_selected_task(config, **options)
    except CustomSelectedTaskError:
        if cfg is None or not getattr(cfg, "mcc_url", None):
            raise
        from mcd_agent.service_profiles import fetch_service_profile
        request = dict(operation=operation, operation_input=operation_input)
        if operation == "cloudflare_real_ip_state":
            request["profile_present"] = profile is not None
        response = fetch_service_profile(cfg, "nginx", **request)
        if response.get("status") != "ok":
            raise CustomSelectedTaskError(str(response.get("reason") or "custom_selected_task_fetch_failed"))
        fetched = cached_selected_task(config, **options)
    try:
        return apply_external_service_profile(config, fetched, component="nginx", offline_only=True)
    except ExternalProfileOperationError as error:
        return error.public_result


def _outcome(operation: str, operation_input: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    try:
        return _invoke(operation, operation_input, **kwargs)
    except (CustomSelectedTaskError, ValueError, RuntimeError) as error:
        result = {"status": "error", "reason": str(error), "component": "nginx", "operation": operation}
        if operation == "ensure_nginx_baseline":
            result.update(error=str(error), changed=False, actions=[])
        return result


def default_stub_operation(*, apply: bool = False, cache_dir: str | None = None) -> dict[str, Any]:
    return _outcome("default_stub_operation", {"apply": apply}, cache_dir=cache_dir)


def ensure_nginx_baseline(*, reload_service: bool = True, cfg: Any = None,
                          cache_dir: str | None = None) -> dict[str, Any]:
    return _outcome("ensure_nginx_baseline", {"reload_service": reload_service}, cfg=cfg, cache_dir=cache_dir)


def ensure_cloudflare_real_ip(profile: dict[str, Any] | None, *, reload_service: bool = True,
                             cfg: Any = None, cache_dir: str | None = None) -> dict[str, Any]:
    return _outcome("ensure_cloudflare_real_ip", {"reload_service": reload_service},
                    profile=profile, cfg=cfg, cache_dir=cache_dir)


def cloudflare_real_ip_state(profile: dict[str, Any] | None = None, *, cfg: Any = None,
                            cache_dir: str | None = None) -> dict[str, Any]:
    return _outcome("cloudflare_real_ip_state", {}, profile=profile, cfg=cfg, cache_dir=cache_dir)


def nginx_baseline_satisfied(*, cfg: Any = None, cache_dir: str | None = None) -> bool:
    result = _invoke("nginx_baseline_satisfied", {}, cfg=cfg, cache_dir=cache_dir)
    if type(result.get("satisfied")) is not bool:
        raise CustomSelectedTaskError("custom_selected_task_observation_invalid")
    return result["satisfied"]
