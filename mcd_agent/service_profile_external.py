"""Descriptor admission for service profiles; algorithms belong to artifacts."""
from __future__ import annotations

import hashlib
import json
import re
import struct
from typing import Any, Mapping

from mcd_agent.custom_scripts import run_custom_script_by_key
from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy

CUSTOM_RUNTIME_CAPABILITIES = frozenset({'custom-gzip-entrypoint', 'raw-jsonline-terminal-result'})


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


def apply_external_service_profile(cfg: Any, fetched: Mapping[str, Any], *,
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
            set(required) != CUSTOM_RUNTIME_CAPABILITIES):
        raise ValueError('external_handler_capability_unavailable')
    profile_input = handler.get('profile_input')
    profile = fetched.get('profile')
    if not isinstance(profile_input, Mapping) or profile_input.get('source') != 'response.profile' or not isinstance(profile, Mapping):
        raise ValueError('external_handler_profile_input_invalid')
    expected_profile = profile_input.get('sha256')
    if not isinstance(expected_profile, str) or re.fullmatch('[0-9a-f]{64}', expected_profile) is None:
        raise ValueError('external_handler_profile_pin_invalid')
    profile_bytes = json.dumps(profile, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(profile_bytes).hexdigest() != expected_profile:
        raise ValueError('external_handler_profile_pin_mismatch')
    # Cluster membership is current agent state, not a hardware-plan default.
    # The producer pins the same explicit context inside the composition data.
    if component == "mysql" or "runtime_context" in handler:
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
    rc, output = run_custom_script_by_key(cfg, script_key=key, expected_sha256=digest,
        expected_entrypoint=entrypoint, args=argv[len(prefix):], detach=False,
        live_output=False, capture_result=True)
    if rc != 0:
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
