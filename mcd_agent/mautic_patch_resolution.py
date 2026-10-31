"""Authenticated transport for MCC-resolved Mautic patch plans."""
from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import hashlib
import os
import stat
from pathlib import Path
import importlib.resources
import json
import base64
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


RESOLUTION_SCHEMA = "mcd-mautic-patch-resolution-v1"
REQUEST_SCHEMA = "mcc-mautic-patch-resolve-v1"
RESPONSE_SCHEMA = "mcc-mautic-patch-resolved-v1"
PLAN_SCHEMA = "mcd-mautic-patch-plan-v3"
EVIDENCE_SCHEMA = "mcd-mautic-patch-evidence-v1"
EVIDENCE_ACCEPTED_SCHEMA = "mcc-mautic-patch-evidence-accepted-v1"

_TRIGGERS = {"daemon_reconcile", "plugin_reload_failure", "upgrade_lifecycle", "operator_action"}
_OPERATIONS = {"status", "verify", "apply", "rollback"}
_PHASES = {
    "before_background_imports",
    "before_cache_warmup",
    "before_campaign_trigger",
    "before_campaign_execution",
    "before_doctrine_migrations",
    "post_source_install",
    "before_plugin_reload",
    "dependency_update_preflight",
    "before_asset_generation",
    "preflight_frontend_assets",
    "legacy_runtime_plugin_repair",
    "image_build_before_packaging",
}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_MCC_403_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_IMPLEMENTED_EXECUTION_KINDS = {"git_patch_v1"}


class MauticPatchResolutionError(RuntimeError):
    """The MCC catalog could not safely resolve or record a patch operation."""


def _contract() -> dict[str, Any]:
    path = importlib.resources.files("mcd_agent").joinpath(
        "contracts/mautic-patch-resolution-v1.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MauticPatchResolutionError("resolution_contract_unavailable") from exc
    if value.get("schema") != RESOLUTION_SCHEMA or value.get("minimum_agent_version") != __version__:
        raise MauticPatchResolutionError("resolution_contract_version_mismatch")
    return value


def canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_plan_file(path: str, expected_sha256: str) -> str:
    """Read a bounded private host plan and verify its immutable canonical hash."""
    if not _SHA64_RE.fullmatch(str(expected_sha256 or "")):
        raise MauticPatchResolutionError("plan_file_sha256_required")
    if not Path(path).is_absolute():
        raise MauticPatchResolutionError("plan_file_absolute_path_required")
    maximum = int(_contract()["max_plan_bytes"])
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or info.st_size > maximum:
            raise MauticPatchResolutionError("plan_file_permissions_or_size_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise MauticPatchResolutionError("plan_file_too_large")
    finally:
        os.close(fd)
    text = raw.decode("utf-8")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MauticPatchResolutionError("plan_file_duplicate_key")
            result[key] = value
        return result
    plan = json.loads(text, object_pairs_hook=unique)
    if canonical_json_sha256(plan) != expected_sha256:
        raise MauticPatchResolutionError("plan_file_sha256_mismatch")
    return text


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _endpoint(config: AgentConfig, suffix: str) -> str:
    base = str(getattr(config, "mcc_url", "") or "").strip().rstrip("/")
    if not base:
        raise MauticPatchResolutionError("mcc_url_missing")
    if base.endswith("/api/v1"):
        return base + suffix
    return base + "/api/v1" + suffix


def _http_error_reason(exc: urllib.error.HTTPError) -> str:
    prefix = f"mcc_patch_http_{int(exc.code)}"
    if int(exc.code) != 403:
        return prefix
    try:
        raw = exc.read(4096)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return prefix
    detail = value.get("detail") if isinstance(value, dict) else None
    if isinstance(detail, str) and _MCC_403_DETAIL_RE.fullmatch(detail):
        return f"{prefix}:{detail}"
    return prefix


def _post_json(config: AgentConfig, suffix: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = str(getattr(config, "mcc_token", "") or "").strip()
    if not token:
        raise MauticPatchResolutionError("mcc_token_missing")
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    request = mcc_request(
        _endpoint(config, suffix),
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise MauticPatchResolutionError(_http_error_reason(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MauticPatchResolutionError(f"mcc_patch_transport_failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MauticPatchResolutionError("mcc_patch_response_invalid_json") from exc
    if not isinstance(value, dict):
        raise MauticPatchResolutionError("mcc_patch_response_not_object")
    return value


def _host_identity(config: AgentConfig) -> tuple[str, str]:
    identity = resolve_agent_identity(config)
    hostname = str(identity.get("effective_hostname") or "").strip()
    host_name = str(identity.get("effective_mcc_host_name") or "").strip() or hostname
    if not hostname:
        raise MauticPatchResolutionError("mcc_host_identity_unavailable")
    return host_name, hostname


def _resolved_host_id(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise MauticPatchResolutionError("mcc_resolved_host_id_invalid") from exc
    if str(parsed) != raw.lower():
        raise MauticPatchResolutionError("mcc_resolved_host_id_invalid")
    return raw


def _agent_capabilities(contract: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: contract[key]
        for key in (
            "schema",
            "minimum_agent_version",
            "plan_schema",
            "evidence_schema",
            "triggers",
            "operations",
            "execution_kinds",
            "phases",
            "gate_kinds",
            "gate_groups",
            "max_plan_bytes",
            "max_payload_bytes",
            "install_types",
        )
    }
    result["execution_kinds"] = [kind for kind in contract["execution_kinds"] if kind in _IMPLEMENTED_EXECUTION_KINDS]
    result["predicate_kinds"] = contract.get("predicate_kinds", [])
    result["features"] = contract.get("features", {})
    return result


def _safe_relative_path(value: Any, *, suffix: str | None = None) -> bool:
    raw = str(value or "")
    if not raw or "\\" in raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        return False
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if suffix and not raw.endswith(suffix):
        return False
    return True


def _validate_gates(record: dict[str, Any], source_paths: set[str]) -> None:
    gates = record.get("gate")
    if not isinstance(gates, list) or not gates:
        raise MauticPatchResolutionError("patch_record_gate_invalid")
    groups: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict):
            raise MauticPatchResolutionError("patch_gate_not_object")
        kind = gate.get("kind")
        group = gate.get("group")
        path = gate.get("path")
        if group not in {"vulnerable", "fixed"} or not _safe_relative_path(path) or path not in source_paths:
            raise MauticPatchResolutionError("patch_gate_context_invalid")
        if kind == "exact_count":
            allowed = {"group", "kind", "path", "needle", "expected_count", "allow_missing_path"}
            count = gate.get("expected_count")
            if (
                set(gate) - allowed
                or not isinstance(gate.get("needle"), str)
                or not gate["needle"]
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or (gate.get("allow_missing_path") is True and count != 0)
                or ("allow_missing_path" in gate and not isinstance(gate["allow_missing_path"], bool))
                or (group == "fixed" and count == 0)
            ):
                raise MauticPatchResolutionError("patch_exact_count_gate_invalid")
        elif kind == "path_state":
            if set(gate) != {"group", "kind", "path", "expected_state"} or gate.get("expected_state") not in {"absent", "present"}:
                raise MauticPatchResolutionError("patch_path_state_gate_invalid")
        elif kind == "sha256":
            if set(gate) != {"group", "kind", "path", "expected_sha256"} or not _SHA64_RE.fullmatch(str(gate.get("expected_sha256", ""))):
                raise MauticPatchResolutionError("patch_sha256_gate_invalid")
        else:
            raise MauticPatchResolutionError("patch_gate_kind_unsupported")
        groups.add(group)
    if groups != {"vulnerable", "fixed"}:
        raise MauticPatchResolutionError("patch_gate_groups_incomplete")
    if record.get("gate_logic") == "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error" and any(sum(gate["group"] == group for gate in gates) != 1 for group in groups):
        raise MauticPatchResolutionError("patch_gate_exactly_one_cardinality_invalid")


def _validate_legacy_state(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "kind", "backup_suffix", "metadata_suffix", "metadata_fields", "expected_marker"
    }:
        raise MauticPatchResolutionError("legacy_state_descriptor_invalid")
    kind = value.get("kind")
    backup_suffix = value.get("backup_suffix")
    metadata_suffix = value.get("metadata_suffix")
    fields = value.get("metadata_fields")
    marker = value.get("expected_marker")
    if kind == "backup_file":
        if not isinstance(backup_suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9._-]{1,80}", backup_suffix):
            raise MauticPatchResolutionError("legacy_state_backup_suffix_invalid")
        if metadata_suffix is not None or fields != {} or marker is not None:
            raise MauticPatchResolutionError("legacy_state_backup_file_shape_invalid")
    elif kind == "backup_file_with_metadata":
        if not isinstance(backup_suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9._-]{1,80}", backup_suffix):
            raise MauticPatchResolutionError("legacy_state_backup_suffix_invalid")
        if not isinstance(metadata_suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9._-]{1,80}", metadata_suffix):
            raise MauticPatchResolutionError("legacy_state_metadata_suffix_invalid")
        required = {"marker", "path", "original_sha256", "applied_sha256"}
        if not isinstance(fields, dict) or set(fields) != required or any(not isinstance(v, str) or not v for v in fields.values()):
            raise MauticPatchResolutionError("legacy_state_metadata_fields_invalid")
        if not isinstance(marker, str) or not marker:
            raise MauticPatchResolutionError("legacy_state_marker_invalid")
    else:
        raise MauticPatchResolutionError("legacy_state_kind_unsupported")


def _validate_plan_records(plan: dict[str, Any], contract: dict[str, Any], trigger: str, phase: str) -> None:
    plan_fields = set(contract["plan_fields"])
    if not plan_fields.issubset(plan) or set(plan) - plan_fields - set(contract.get("plan_optional_fields", [])):
        raise MauticPatchResolutionError("patch_plan_fields_invalid")
    from mcd_agent.mautic_patch_fact_binding import validate_context
    try:
        validate_context(plan)
    except ValueError as exc:
        raise MauticPatchResolutionError(str(exc)) from exc
    if plan.get("install_type") not in set(contract["install_types"]):
        raise MauticPatchResolutionError("patch_plan_install_type_invalid")
    if plan.get("trigger") != trigger or plan.get("phase") != phase:
        raise MauticPatchResolutionError("patch_plan_trigger_phase_mismatch")
    if plan.get("operation") not in _OPERATIONS:
        raise MauticPatchResolutionError("patch_plan_operation_invalid")
    patches = plan.get("patches")
    if not isinstance(patches, list) or not patches or len(patches) > 32:
        raise MauticPatchResolutionError("patch_plan_records_invalid")
    payloads = plan.get("payloads")
    if not isinstance(payloads, list):
        raise MauticPatchResolutionError("patch_plan_payloads_invalid")
    payload_by_path: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict) or set(payload) != {"path", "sha256", "content_base64"}:
            raise MauticPatchResolutionError("patch_payload_fields_invalid")
        path = payload.get("path")
        if not _safe_relative_path(path, suffix=".patch") or path in payload_by_path:
            raise MauticPatchResolutionError("patch_payload_path_invalid")
        payload_by_path[path] = payload

    record_ids: set[str] = set()
    used_payload_paths: set[str] = set()
    previous_order = -1
    for record in patches:
        if not isinstance(record, dict):
            raise MauticPatchResolutionError("patch_record_not_object")
        kind = record.get("execution_kind")
        allowlists = contract["patch_record_field_allowlists"].get(kind)
        if not isinstance(allowlists, dict):
            raise MauticPatchResolutionError("patch_execution_kind_unsupported")
        required = set(allowlists["required"])
        optional = set(allowlists["optional"])
        if not required.issubset(record) or set(record) - required - optional:
            raise MauticPatchResolutionError("patch_record_fields_invalid")
        patch_id = record.get("id")
        if not isinstance(patch_id, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,95}", patch_id) or patch_id in record_ids:
            raise MauticPatchResolutionError("patch_record_id_invalid")
        record_ids.add(patch_id)
        triggers = record.get("triggers")
        phases = record.get("phases")
        order = record.get("phase_order")
        if (
            not isinstance(triggers, list)
            or not triggers
            or any(value not in _TRIGGERS for value in triggers)
            or trigger not in triggers
            or not isinstance(phases, list)
            or not phases
            or any(value not in _PHASES for value in phases)
            or (trigger != "upgrade_lifecycle" and phase not in phases)
            or (trigger == "upgrade_lifecycle" and phase != "dependency_update_preflight")
            or isinstance(order, bool)
            or not isinstance(order, int)
            or not 0 <= order <= 10000
            or order < previous_order
        ):
            raise MauticPatchResolutionError("patch_record_trigger_phase_order_invalid")
        previous_order = order
        if "allow_unknown_version" in record and not isinstance(record["allow_unknown_version"], bool):
            raise MauticPatchResolutionError("patch_unknown_version_flag_invalid")
        _validate_legacy_state(record.get("legacy_state"))
        source_paths = record.get("source_paths", [])
        if kind == "git_patch_v1":
            if "parameters" in record and record["parameters"] != {}:
                raise MauticPatchResolutionError("git_patch_parameters_not_supported")
            if not isinstance(source_paths, list) or not source_paths or any(not isinstance(p, str) or not _safe_relative_path(p) for p in source_paths) or len(set(source_paths)) != len(source_paths):
                raise MauticPatchResolutionError("patch_source_paths_invalid")
            if record.get("gate_logic") not in {
                "vulnerable_all; fixed_all; mixed_or_unknown=error",
                "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error",
            }:
                raise MauticPatchResolutionError("patch_gate_logic_invalid")
            _validate_gates(record, set(source_paths))
            payload_path = record.get("payload_path")
            if not _safe_relative_path(payload_path, suffix=".patch") or payload_path not in payload_by_path or payload_path in used_payload_paths:
                raise MauticPatchResolutionError("patch_payload_reference_invalid")
            used_payload_paths.add(payload_path)
        else:
            if source_paths or "payload_path" in record:
                raise MauticPatchResolutionError("db_operation_has_file_payload")
            parameters = record.get("parameters")
            if not isinstance(parameters, dict) or set(parameters) != {"table", "column", "predicate", "replacement_json"}:
                raise MauticPatchResolutionError("db_operation_parameters_invalid")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(parameters.get("table", ""))) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(parameters.get("column", ""))):
                raise MauticPatchResolutionError("db_operation_identifier_invalid")
            if parameters.get("predicate") != "null_or_empty_or_invalid_json" or not isinstance(parameters.get("replacement_json"), str):
                raise MauticPatchResolutionError("db_operation_value_invalid")
            try:
                json.loads(parameters["replacement_json"])
            except ValueError as exc:
                raise MauticPatchResolutionError("db_operation_replacement_json_invalid") from exc
            if record.get("gate") not in (None, []):
                raise MauticPatchResolutionError("db_operation_gate_unsupported")
    if used_payload_paths != set(payload_by_path):
        raise MauticPatchResolutionError("unreferenced_patch_payload")


def resolve_plan(
    config: AgentConfig,
    install: Any,
    *,
    trigger: str,
    phase: str,
    operation: str,
    observed_version: str | None,
    install_type: str,
    observed_major: int | None = None,
    target_version: str | None = None,
    run_id: str | None = None,
    cached_catalog_revision: str | None = None,
    cached_catalog_sha256: str | None = None,
) -> dict[str, Any]:
    contract = _contract()
    if trigger not in _TRIGGERS or trigger not in contract["triggers"]:
        raise MauticPatchResolutionError("unsupported_patch_trigger")
    if phase not in _PHASES or phase not in contract["phases"]:
        raise MauticPatchResolutionError("unsupported_patch_phase")
    if operation not in _OPERATIONS or operation not in contract["operations"]:
        raise MauticPatchResolutionError("unsupported_patch_operation")
    if operation == "rollback" and not run_id:
        raise MauticPatchResolutionError("rollback_run_id_required")
    if run_id is None and operation != "rollback":
        run_id = uuid.uuid4().hex
    if run_id is not None and not _RUN_ID_RE.fullmatch(str(run_id)):
        raise MauticPatchResolutionError("invalid_patch_run_id")
    if observed_version is None and (isinstance(observed_major, bool) or observed_major not in range(1, 100)):
        raise MauticPatchResolutionError("unknown_version_requires_confirmed_major")

    host_name, hostname = _host_identity(config)
    instance_uid = str(getattr(install, "instance_uid", "") or "").strip()
    install_type = str(install_type or "").strip().lower()
    if not instance_uid or install_type not in set(contract["install_types"]):
        raise MauticPatchResolutionError("patch_instance_identity_invalid")

    request_payload = {
        "schema": REQUEST_SCHEMA,
        "mcc_host_name": host_name,
        "hostname": hostname,
        "agent_version": __version__,
        "instance_uid": instance_uid,
        "observed_version": observed_version,
        "observed_major": observed_major,
        "target_version": target_version,
        "install_type": install_type,
        "trigger": trigger,
        "phase": phase,
        "operation": operation,
        "run_id": run_id,
        "cached_catalog_revision": cached_catalog_revision,
        "cached_catalog_sha256": cached_catalog_sha256,
        "agent_patch_contract": _agent_capabilities(contract),
    }
    from mcd_agent.mautic_patch_fact_binding import discover_execution_context, needs_facts
    execution_context = discover_execution_context(config, install)
    if execution_context is not None:
        request_payload["execution_context"] = execution_context
    result = _post_json(config, "/agent/mautic-patches/resolve", request_payload)
    if result.get("schema") != RESPONSE_SCHEMA:
        raise MauticPatchResolutionError("mcc_patch_response_schema_mismatch")
    if result.get("instance_uid") != instance_uid:
        raise MauticPatchResolutionError("mcc_patch_instance_mismatch")
    _resolved_host_id(result.get("host_id"))
    status = result.get("status")
    if status not in {"selected", "noop", "blocked"}:
        raise MauticPatchResolutionError("mcc_patch_response_status_invalid")
    # MCC may return a deliberately minimal blocked response when admission
    # fails before catalog selection (for example, an unsupported execution
    # kind). There is no selected plan to bind to the request in that case.
    if status == "blocked" and "trigger" not in result and "phase" not in result:
        if result.get("plan") is not None:
            raise MauticPatchResolutionError("mcc_nonselected_response_has_plan")
        return result
    if result.get("trigger") != trigger or result.get("phase") != phase:
        raise MauticPatchResolutionError("mcc_patch_response_context_mismatch")
    if not str(result.get("catalog_revision", "") or "").strip() or not _SHA64_RE.fullmatch(str(result.get("catalog_sha256", ""))):
        raise MauticPatchResolutionError("mcc_catalog_identity_invalid")
    if not _SHA40_RE.fullmatch(str(result.get("registry_commit", ""))):
        raise MauticPatchResolutionError("mcc_registry_commit_invalid")
    if not _SHA64_RE.fullmatch(str(result.get("registry_sha256", ""))):
        raise MauticPatchResolutionError("mcc_registry_sha256_invalid")
    if status != "selected":
        if result.get("plan") is not None:
            raise MauticPatchResolutionError("mcc_nonselected_response_has_plan")
        return result

    plan = result.get("plan")
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise MauticPatchResolutionError("mcc_patch_plan_schema_mismatch")
    if result.get("plan_sha256") != canonical_json_sha256(plan):
        raise MauticPatchResolutionError("mcc_patch_plan_sha256_mismatch")
    _validate_plan_records(plan, contract, trigger, phase)
    if needs_facts(plan) and (execution_context is None or plan.get("execution_context") != execution_context):
        raise MauticPatchResolutionError("mcc_plan_execution_context_mismatch")
    if plan.get("registry_commit") != result.get("registry_commit") or plan.get("registry_sha256") != result.get("registry_sha256"):
        raise MauticPatchResolutionError("mcc_plan_registry_mismatch")
    if plan.get("instance_uid") not in (None, instance_uid):
        raise MauticPatchResolutionError("mcc_plan_instance_mismatch")
    if plan.get("trigger") != trigger or plan.get("phase") != phase or plan.get("operation") != operation:
        raise MauticPatchResolutionError("mcc_plan_context_mismatch")
    if (
        plan.get("source_version") != result.get("source_version")
        or plan.get("target_version") != result.get("target_version")
        or plan.get("install_type") != result.get("install_type")
    ):
        raise MauticPatchResolutionError("mcc_plan_response_metadata_mismatch")
    if plan.get("run_id") != run_id:
        raise MauticPatchResolutionError("mcc_plan_run_id_mismatch")
    source_value = plan.get("source_version")
    target_value = plan.get("target_version")
    source_tuple = _version_tuple(str(source_value)) if source_value is not None else None
    target_tuple = _version_tuple(str(target_value)) if target_value is not None else None
    if trigger == "upgrade_lifecycle":
        if source_tuple is None or target_tuple is None or source_tuple >= target_tuple:
            raise MauticPatchResolutionError("upgrade_plan_version_relation_invalid")
    elif source_value is None and target_value is None:
        if observed_version is not None or observed_major is None or not all(record.get("allow_unknown_version") is True for record in plan["patches"]):
            raise MauticPatchResolutionError("unknown_version_plan_not_authorized")
    elif source_tuple is None or target_tuple is None or source_tuple != target_tuple:
        if source_tuple is None or target_tuple is None or source_tuple > target_tuple:
            raise MauticPatchResolutionError("remediation_plan_version_relation_invalid")
        raise MauticPatchResolutionError("remediation_plan_version_mismatch")
    elif observed_version is not None and source_tuple != _version_tuple(observed_version):
        raise MauticPatchResolutionError("remediation_plan_observed_version_mismatch")
    if len(json.dumps(plan, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > int(contract["max_plan_bytes"]):
        raise MauticPatchResolutionError("mcc_patch_plan_too_large")
    payloads = plan.get("payloads", [])
    if not isinstance(payloads, list):
        raise MauticPatchResolutionError("mcc_patch_payloads_invalid")
    payload_bytes = 0
    for item in payloads:
        if not isinstance(item, dict) or not isinstance(item.get("content_base64"), str):
            raise MauticPatchResolutionError("mcc_patch_payload_invalid")
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise MauticPatchResolutionError("mcc_patch_payload_invalid_base64") from exc
        payload_bytes += len(content)
        if not _SHA64_RE.fullmatch(str(item.get("sha256", ""))):
            raise MauticPatchResolutionError("mcc_patch_payload_sha256_invalid")
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise MauticPatchResolutionError("mcc_patch_payload_sha256_mismatch")
    if payload_bytes > int(contract["max_payload_bytes"]):
        raise MauticPatchResolutionError("mcc_patch_payload_too_large")
    return result


def report_evidence(
    config: AgentConfig,
    *,
    instance_uid: str,
    mcc_host_name: str,
    hostname: str,
    host_id: str,
    catalog_revision: str,
    catalog_sha256: str,
    plan_sha256: str,
    trigger: str,
    phase: str,
    run_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    contract = _contract()
    if trigger not in _TRIGGERS or phase not in _PHASES or not _RUN_ID_RE.fullmatch(run_id):
        raise MauticPatchResolutionError("patch_evidence_context_invalid")
    if not _SHA64_RE.fullmatch(catalog_sha256) or not _SHA64_RE.fullmatch(plan_sha256):
        raise MauticPatchResolutionError("patch_evidence_hash_invalid")
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "mcc_host_name": mcc_host_name,
        "hostname": hostname,
        "host_id": _resolved_host_id(host_id),
        "instance_uid": instance_uid,
        "catalog_revision": catalog_revision,
        "catalog_sha256": catalog_sha256,
        "plan_sha256": plan_sha256,
        "trigger": trigger,
        "phase": phase,
        "run_id": run_id,
        "evidence": evidence,
    }
    result = _post_json(config, "/agent/mautic-patches/evidence", payload)
    if result.get("schema") != EVIDENCE_ACCEPTED_SCHEMA or result.get("status") != "accepted":
        raise MauticPatchResolutionError("mcc_patch_evidence_not_accepted")
    if not str(result.get("evidence_id", "") or "").strip():
        raise MauticPatchResolutionError("mcc_patch_evidence_id_missing")
    return result
