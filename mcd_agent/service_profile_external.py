"""Descriptor admission for service profiles; algorithms belong to artifacts."""
from __future__ import annotations

import hashlib
import json
import re
import struct
from typing import Any, Mapping

from mcd_agent.custom_scripts import CustomSelectedTaskError, run_custom_script_by_key
from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.scenario_launcher import MAX_CONTEXT_BYTES

BASE_RUNTIME_CAPABILITIES = frozenset({'custom-gzip-entrypoint', 'raw-jsonline-terminal-result'})
CUSTOM_RUNTIME_CAPABILITIES = BASE_RUNTIME_CAPABILITIES | {'framed-runtime-input'}
NGINX_OPERATION_INPUT_KEYS = {
    'default_stub_operation': {'apply'},
    'ensure_nginx_baseline': {'reload_service'},
    'ensure_cloudflare_real_ip': {'reload_service'},
    'nginx_baseline_satisfied': set(),
    'cloudflare_real_ip_state': set(),
    'write_nginx_vhost': {'root', 'domains', 'php_version'},
    'write_switched_vhost': {'source_root', 'nginx_root', 'php_version', 'site_available', 'site_enabled'},
}

def nginx_operation_input_valid(operation: Any, values: Any) -> bool:
    required = NGINX_OPERATION_INPUT_KEYS.get(operation) if isinstance(operation, str) else None
    if required is None or not isinstance(values, dict) or set(values) != required:
        return False
    if operation == 'write_nginx_vhost':
        return (isinstance(values['root'], str) and isinstance(values['php_version'], str)
                and isinstance(values['domains'], list)
                and all(isinstance(domain, str) for domain in values['domains']))
    if operation == 'write_switched_vhost':
        return (all(isinstance(values[key], str) for key in required - {'site_enabled'})
                and (values['site_enabled'] is None or isinstance(values['site_enabled'], str)))
    return all(type(value) is bool for value in values.values())



class ExternalProfileOperationError(RuntimeError):
    """A failed validated terminal may still carry the exact public outcome."""
    def __init__(self, terminal: dict[str, Any]):
        super().__init__("external_handler_execution_failed")
        self.terminal = terminal
        self.public_result = dict(terminal["result"])


def external_handler_selected(handler: Mapping[str, Any]) -> bool:
    if handler.get('state') == 'candidate' and handler.get('active') is False:
        return False
    if handler.get('state') == 'active' and handler.get('active') is True:
        return True
    raise ValueError('external_handler_activation_invalid')


def _runtime_input_schema(handler: Mapping[str, Any], *, component: str,
                          runtime_input: Any) -> tuple[set[str] | None, bool]:
    """Validate the generic descriptor-declared private string-map contract.

    Wazuh predates the descriptor schema and keeps its exact legacy contract.
    Other components may opt into the same bounded stdin transport only by
    declaring their field names in the external descriptor; MCD does not own
    component-specific secret names or defaults.
    """
    schema = handler.get("runtime_input_schema")
    if schema is None:
        if component != "wazuh":
            return None, runtime_input is None
        valid = (isinstance(runtime_input, dict)
                 and set(runtime_input) == {"registration_password"}
                 and isinstance(runtime_input["registration_password"], str))
        return {"registration_password"} if valid else None, valid
    if (not isinstance(schema, Mapping)
            or set(schema) != {"type", "fields"}
            or schema.get("type") != "string-map"):
        raise ValueError("external_handler_runtime_input_schema_invalid")
    fields = schema.get("fields")
    if (not isinstance(fields, list) or not fields
            or any(not isinstance(field, str) or not field for field in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError("external_handler_runtime_input_schema_invalid")
    expected = set(fields)
    if runtime_input is None:
        return expected, False
    if (not isinstance(runtime_input, dict) or set(runtime_input) != expected
            or any(not isinstance(value, str) for value in runtime_input.values())):
        raise ValueError("external_handler_runtime_input_invalid")
    return expected, True


def _admit_external_service_profile(cfg: Any, fetched: Mapping[str, Any], *,
                                   component: str, dry_run: bool = False) -> dict[str, Any]:
    handler = fetched.get('external_handler')
    if not isinstance(handler, Mapping):
        raise ValueError('external_handler_missing')
    if not external_handler_selected(handler):
        raise ValueError('external_handler_inactive')
    if handler.get('schema') != 'mcc-service-profile-external-handler-v1' or handler.get('transport') != 'mcd-custom-v1':
        raise ValueError('external_handler_transport_invalid')
    for coordinate in ('component', 'resolved_plan_key'):
        value = handler.get(coordinate)
        if not isinstance(value, str) or not value or value != fetched.get(coordinate):
            raise ValueError('external_handler_binding_invalid')
    if fetched.get('component') != component:
        raise ValueError('external_handler_requested_component_mismatch')
    runtime = handler.get('minimum_runtime')
    required = runtime.get('required_capabilities') if isinstance(runtime, Mapping) else None
    if (not isinstance(required, list) or not required or
            any(not isinstance(c, str) or c not in CUSTOM_RUNTIME_CAPABILITIES for c in required) or
            not BASE_RUNTIME_CAPABILITIES <= set(required)):
        raise ValueError('external_handler_capability_unavailable')
    runtime_input = fetched.get("runtime_input")
    if "runtime_input" in fetched:
        _fields, valid_runtime_input = _runtime_input_schema(
            handler, component=component, runtime_input=runtime_input
        )
        if not valid_runtime_input:
            raise ValueError("external_handler_runtime_input_invalid")
        try:
            input_bytes = json.dumps(runtime_input, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("external_handler_runtime_input_invalid") from None
        if len(input_bytes) > MAX_CONTEXT_BYTES:
            raise ValueError("external_handler_runtime_input_invalid")
        if "framed-runtime-input" not in required:
            raise ValueError("external_handler_runtime_input_unsupported")
    elif "runtime_input_schema" in handler:
        _runtime_input_schema(handler, component=component, runtime_input=None)
    profile_input = handler.get('profile_input')
    profile = fetched.get('profile')
    if not isinstance(profile_input, Mapping) or profile_input.get('source') != 'response.profile' or not (isinstance(profile, Mapping) or (component == 'nginx' and profile is None)):
        raise ValueError('external_handler_profile_input_invalid')
    if component == "wazuh" and profile.get("registration_password"):
        raise ValueError("external_handler_private_profile_rejected")
    expected_profile = profile_input.get('sha256')
    if not isinstance(expected_profile, str) or re.fullmatch('[0-9a-f]{64}', expected_profile) is None:
        raise ValueError('external_handler_profile_pin_invalid')
    profile_bytes = json.dumps(profile, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(profile_bytes).hexdigest() != expected_profile:
        raise ValueError('external_handler_profile_pin_mismatch')
    # Runtime facts come from current agent state, not profile/plan defaults.
    # The producer pins the same explicit context inside the composition data.
    if component == "wazuh":
        context = handler.get("runtime_context")
        identity_keys = {"effective_mcc_host_name", "configured_host_name", "local_hostname"}
        if not isinstance(context, Mapping) or set(context) != identity_keys:
            raise ValueError("external_handler_runtime_context_missing")
        if (any(context[key] is not None and not isinstance(context[key], str)
                for key in ("effective_mcc_host_name", "configured_host_name"))
                or not isinstance(context["local_hostname"], str)
                or not context["local_hostname"].strip()):
            raise ValueError("external_handler_runtime_context_invalid")
        if "runtime_context" in fetched and fetched["runtime_context"] != context:
            raise ValueError("external_handler_runtime_context_response_mismatch")
        identity = resolve_agent_identity(cfg)
        if not isinstance(identity, Mapping) or not identity_keys <= set(identity):
            raise ValueError("external_handler_runtime_context_unavailable")
        actual = {key: identity[key] for key in identity_keys}
        if dict(context) != actual:
            raise ValueError("external_handler_runtime_context_mismatch")
    elif component == "nginx":
        operation = fetched.get("operation")
        flags = fetched.get("operation_input")
        if (not nginx_operation_input_valid(operation, flags)
                or handler.get("operation") != operation or canonical_json(handler.get("operation_input")) != canonical_json(flags)):
            raise ValueError("external_handler_operation_input_invalid")
        context = handler.get("runtime_context")
        if (not isinstance(context, Mapping) or set(context) != {"ipv6_listen_forbidden"}
                or type(context["ipv6_listen_forbidden"]) is not bool):
            raise ValueError("external_handler_runtime_context_invalid")
        if fetched.get("runtime_context") != context:
            raise ValueError("external_handler_runtime_context_response_mismatch")
        from mcd_agent.nginx_baseline import _ipv6_listen_forbidden
        if context["ipv6_listen_forbidden"] is not _ipv6_listen_forbidden():
            raise ValueError("external_handler_runtime_context_mismatch")
        for field, value in (("runtime_context_input", context), ("operation_input_input", flags)):
            pin = handler.get(field)
            if not isinstance(pin, Mapping) or pin.get("sha256") != hashlib.sha256(canonical_json(value)).hexdigest():
                raise ValueError("external_handler_operation_pin_mismatch")
        fields = profile_input.get("fields")
        if (not isinstance(fields, list) or any(not isinstance(k, str) or not k for k in fields)
                or len(set(fields)) != len(fields) or (profile is not None and not set(profile) <= set(fields))):
            raise ValueError("external_handler_profile_projection_invalid")
    elif component == "mysql" or "runtime_context" in handler:
        context = handler.get("runtime_context")
        if not isinstance(context, Mapping) or set(context) != {"cluster_id"}:
            raise ValueError("external_handler_runtime_context_missing")
        declared = context["cluster_id"]
        if declared is not None and not isinstance(declared, str):
            raise ValueError("external_handler_runtime_context_invalid")
        if not hasattr(cfg, "cluster_id"):
            raise ValueError("external_handler_runtime_context_unavailable")
        actual = cfg.cluster_id
        if actual is not None and not isinstance(actual, str):
            raise ValueError("external_handler_runtime_context_invalid")
        if actual != declared:
            raise ValueError("external_handler_runtime_context_mismatch")
    artifact = handler.get('artifact')
    if not isinstance(artifact, Mapping) or artifact.get('media_type') != 'application/gzip':
        raise ValueError('external_handler_artifact_invalid')
    key, digest, entrypoint = (artifact.get(k) for k in ('key', 'sha256', 'entrypoint'))
    if not isinstance(key, str) or not key or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None or not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError('external_handler_artifact_pin_invalid')
    command = handler.get('dry_run_command' if dry_run else 'command')
    argv = command.get('argv') if isinstance(command, Mapping) else None
    prefix = ['custom', '--no-detach', '--expected-sha256', digest, key, '--']
    if not isinstance(argv, list) or len(argv) <= len(prefix) or argv[:len(prefix)] != prefix or any(not isinstance(v, str) or '\x00' in v for v in argv):
        raise ValueError('external_handler_dry_run_unavailable' if dry_run else 'external_handler_command_invalid')
    if (runtime_input is not None) != ("--runtime-input-stdin" in argv[len(prefix):]):
        raise ValueError("external_handler_runtime_input_command_mismatch")
    return {"key": key, "digest": digest, "entrypoint": entrypoint,
            "args": argv[len(prefix):], "runtime_input": runtime_input}


def validate_external_service_profile(cfg: Any, fetched: Mapping[str, Any], *, component: str) -> None:
    """Admit public coordinates/facts before caching, without downloads or writes."""
    _admit_external_service_profile(cfg, fetched, component=component)


def apply_external_service_profile(cfg: Any, fetched: Mapping[str, Any], *,
                                   component: str, dry_run: bool = False,
                                   offline_only: bool = False) -> dict[str, Any]:
    admitted = _admit_external_service_profile(cfg, fetched, component=component, dry_run=dry_run)
    options = {"runtime_input": admitted["runtime_input"]} if admitted["runtime_input"] is not None else {}
    if offline_only:
        options["offline_only"] = True
    rc, output = run_custom_script_by_key(cfg, script_key=admitted["key"], expected_sha256=admitted["digest"],
        expected_entrypoint=admitted["entrypoint"], args=admitted["args"], detach=False,
        live_output=False, capture_result=True, **options)
    if rc != 0:
        if offline_only and output in {"custom_selected_task_catalog_item_missing", "custom_selected_task_artifact_missing_or_corrupt"}:
            raise CustomSelectedTaskError(output)
        # Reuse the existing frame validator; no component business statuses are
        # interpreted by the generic decoder or this failure carrier.
        try:
            parsed = json.loads(output)
        except (ValueError, UnicodeError):
            raise RuntimeError('external_handler_execution_failed: ' + output) from None
        try:
            payload = canonical_json(parsed)
            frames = ExternalProcessPolicy._decode_output_frames(struct.pack("!I", len(payload)) + payload)
            terminal = frames[-1]
        except (ValueError, UnicodeError):
            # Never echo a rejected structured payload into an exception/log.
            raise RuntimeError("external_handler_failed_result_rejected") from None
        if (terminal is not None and terminal["status"] == "failed"
                and terminal["exit_code"] == rc and isinstance(terminal.get("result"), dict)):
            raise ExternalProfileOperationError(terminal)
        raise RuntimeError('external_handler_execution_failed: ' + output)
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError('external_handler_public_result_invalid')
    return result
