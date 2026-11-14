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
    ExternalProfileOperationError, apply_external_service_profile, nginx_operation_input_valid,
)


_MANAGED_RECEIPT_KEYS = {
    'schema', 'operation_id', 'domain', 'site', 'available_path', 'enabled_path',
    'enabled_target', 'available_sha256', 'created_available',
    'created_enabled_symlink',
}


class ManagedNginxVhostError(RuntimeError):
    """A validated managed-vhost failure with its bounded public result."""

    def __init__(self, public_result: dict[str, Any]):
        super().__init__('nginx_vhost_operation_failed')
        self.public_result = dict(public_result)


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
        try:
            return apply_external_service_profile(config, fetched, component="nginx", offline_only=True)
        except CustomSelectedTaskError as error:
            transport = cfg is not None and any(getattr(config, key, None) for key in
                ("custom_repo_base_url", "plugins_repo_base_url", "mcc_url"))
            if str(error) != "custom_selected_task_artifact_missing_or_corrupt" or not transport:
                raise
            # Admission already succeeded; repair only these selected pins,
            # never query latest or retry an operation that started mutating.
            from mcd_agent.custom_scripts import run_custom_script_by_key
            artifact = fetched["external_handler"]["artifact"]
            code, _output = run_custom_script_by_key(config, script_key=artifact["key"],
                expected_sha256=artifact["sha256"], expected_entrypoint=artifact.get("entrypoint"),
                fetch_only=True, detach=False)
            if code != 0:
                raise CustomSelectedTaskError("custom_selected_task_artifact_repair_failed")
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


def nginx_vhost_operation(operation: str, parameters: dict[str, Any], *,
                          cfg: Any = None, cache_dir: str | None = None) -> dict[str, Any]:
    if operation not in {'write_nginx_vhost', 'write_switched_vhost'} or not nginx_operation_input_valid(operation, parameters):
        raise CustomSelectedTaskError('nginx_operation_input_invalid')
    if operation == 'write_nginx_vhost' and not parameters['domains']:
        raise RuntimeError('at least one domain is required for nginx vhost')
    result = _invoke(operation, parameters, profile=None, cfg=cfg, cache_dir=cache_dir)
    if isinstance(result.get('error'), str):
        raise RuntimeError(result['error'])
    expected = {'site'} if operation == 'write_nginx_vhost' else {'active', 'backup'}
    if set(result) != expected or any(not isinstance(value, str) for value in result.values()):
        raise CustomSelectedTaskError('custom_selected_task_result_invalid')
    return result


def _managed_receipt_valid(receipt: Any, *, operation_id: str,
                           domain: str, site: str | None = None) -> bool:
    return (
        isinstance(receipt, dict)
        and set(receipt) == _MANAGED_RECEIPT_KEYS
        and receipt.get('schema') == 'mcc-nginx-managed-vhost-ownership-v1'
        and receipt.get('operation_id') == operation_id
        and receipt.get('domain') == domain
        and all(isinstance(receipt.get(key), str) and receipt[key] for key in (
            'operation_id', 'domain', 'site', 'available_path', 'enabled_path',
            'enabled_target', 'available_sha256'))
        and len(receipt['available_sha256']) == 64
        and all(char in '0123456789abcdef' for char in receipt['available_sha256'])
        and type(receipt.get('created_available')) is bool
        and type(receipt.get('created_enabled_symlink')) is bool
        and (site is None or receipt.get('site') == site)
    )


def write_managed_nginx_vhost(*, operation_id: str, root: Path, domains: list[str],
                              php_version: str, cfg: Any = None,
                              cache_dir: str | None = None) -> dict[str, Any]:
    """Create an image-owned vhost and retain the external ownership receipt."""
    parameters = {
        'operation_id': operation_id,
        'root': str(root),
        'domains': domains,
        'php_version': php_version,
    }
    if not nginx_operation_input_valid('write_managed_nginx_vhost', parameters):
        raise CustomSelectedTaskError('nginx_operation_input_invalid')
    if not operation_id or not domains:
        raise CustomSelectedTaskError('nginx_operation_input_invalid')
    result = _invoke('write_managed_nginx_vhost', parameters, profile=None,
                     cfg=cfg, cache_dir=cache_dir)
    if (set(result) != {'site', 'ownership_receipt'}
            or not isinstance(result.get('site'), str) or not result['site']
            or not _managed_receipt_valid(
                result.get('ownership_receipt'), operation_id=operation_id,
                domain=domains[0], site=result['site'])):
        raise CustomSelectedTaskError('custom_selected_task_result_invalid')
    return result


def cleanup_managed_vhost(*, operation_id: str, domain: str,
                          ownership_receipt: dict[str, Any], cfg: Any = None,
                          cache_dir: str | None = None) -> dict[str, Any]:
    """Remove only the vhost objects proven owned by a managed-write receipt."""
    if not _managed_receipt_valid(
            ownership_receipt, operation_id=operation_id, domain=domain):
        raise CustomSelectedTaskError('nginx_operation_input_invalid')
    parameters = {
        'operation_id': operation_id,
        'domain': domain,
        'ownership_receipt': ownership_receipt,
    }
    if not nginx_operation_input_valid('cleanup_managed_vhost', parameters):
        raise CustomSelectedTaskError('nginx_operation_input_invalid')
    result = _invoke('cleanup_managed_vhost', parameters, profile=None,
                     cfg=cfg, cache_dir=cache_dir)
    common = (
        isinstance(result, dict)
        and set(result) in ({'schema', 'status', 'operation_id', 'domain',
                             'ownership_receipt_sha256', 'removed',
                             'mutation_started', 'rollback_available'},
                            {'schema', 'status', 'operation_id', 'domain',
                             'ownership_receipt_sha256', 'removed',
                             'mutation_started', 'rollback_available', 'error'})
        and result.get('schema') == 'mcc-nginx-managed-vhost-cleanup-v1'
        and result.get('operation_id') == operation_id
        and result.get('domain') == domain
        and isinstance(result.get('ownership_receipt_sha256'), str)
        and len(result['ownership_receipt_sha256']) == 64
        and all(char in '0123456789abcdef' for char in result['ownership_receipt_sha256'])
        and isinstance(result.get('removed'), dict)
        and set(result['removed']) == {'available', 'enabled'}
        and all(type(result['removed'][key]) is bool for key in ('available', 'enabled'))
        and type(result.get('mutation_started')) is bool
        and result.get('rollback_available') is False
    )
    if not common:
        raise CustomSelectedTaskError('custom_selected_task_result_invalid')
    if result.get('status') == 'error':
        if set(result) != {'schema', 'status', 'operation_id', 'domain',
                           'ownership_receipt_sha256', 'removed',
                           'mutation_started', 'rollback_available', 'error'} or not isinstance(result.get('error'), str):
            raise CustomSelectedTaskError('custom_selected_task_result_invalid')
        raise ManagedNginxVhostError(result)
    if (result.get('status') not in {'applied', 'noop'}
            or 'error' in result):
        raise CustomSelectedTaskError('custom_selected_task_result_invalid')
    return result


def write_nginx_vhost(*, root: Path, domains: list[str], php_version: str, cfg: Any = None,
                       cache_dir: str | None = None) -> str:
    return nginx_vhost_operation('write_nginx_vhost', {'root': str(root), 'domains': domains,
        'php_version': php_version}, cfg=cfg, cache_dir=cache_dir)['site']


def write_switched_vhost(plan: Any, *, cfg: Any = None, cache_dir: str | None = None) -> dict[str, str]:
    parameters = {key: str(getattr(plan, key)) for key in ('source_root', 'nginx_root', 'php_version', 'site_available')}
    parameters['site_enabled'] = str(plan.site_enabled) if plan.site_enabled is not None else None
    return nginx_vhost_operation('write_switched_vhost', parameters, cfg=cfg, cache_dir=cache_dir)
