"""Portal/MCD OWH-AUTO v1 contract boundary.

This module contains only transport validation and safe receipt/request
builders.  It intentionally has no installer policy, OS-specific recipe,
scenario command, signing key, encryption layer, or additional credential.
"""

from __future__ import annotations

import re
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


CONTRACT_VERSION = "1.0"
INSTALL_REQUEST_SCHEMA = "portal-owh-auto-install-request-v1"
INSTALL_RESPONSE_SCHEMA = "portal-owh-auto-install-response-v1"
SCENARIO_REQUEST_SCHEMA = "portal-owh-auto-scenario-request-v1"
ARTIFACT_RESPONSE_SCHEMA = "portal-owh-auto-artifact-response-v1"
STAGE_RECEIPT_SCHEMA = "portal-owh-auto-stage-receipt-v1"
TERMINAL_RECEIPT_SCHEMA = "portal-owh-auto-terminal-receipt-v1"
MANUAL_ENROLLMENT_SCHEMA = "portal-mcd-manual-enrollment-v1"
STAGE_IDS = ("base_packages", "database_cache", "web_stack", "mautic")
OS_IDS = ("ubuntu",)
FORBIDDEN_KEYS = frozenset(
    {
        "signature",
        "agent_signature",
        "agent_key",
        "public_key",
        "private_key",
        "encryption",
        "encrypted",
        "access_token",
        "enrollment_token",
        "installation_token",
        "payload_token",
    }
)


class OwhAutoContractError(ValueError):
    """Stable, secret-free OWH-AUTO contract validation failure."""


def _fail(code: str) -> OwhAutoContractError:
    return OwhAutoContractError(code)


def _obj(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(code)
    return value


def _text(value: object, code: str, *, max_len: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise _fail(code)
    return value.strip()


def _digest(value: object, code: str) -> str:
    result = _text(value, code, max_len=71)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
        raise _fail(code)
    return result


def _allowed_hosts(value: object, code: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(host, str) or not host.strip() for host in value):
        raise _fail(code)
    return [host.strip() for host in value]


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise _fail("owh_auto_forbidden_extension")
            _reject_forbidden(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden(child)


def _target(value: object, code: str) -> dict[str, str]:
    item = _obj(value, code)
    dns_name = _text(item.get("dns_name"), code, max_len=253).lower()
    local_identity = _text(item.get("local_identity"), code, max_len=253).lower()
    if dns_name != local_identity or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", dns_name):
        raise _fail(code)
    return {"dns_name": dns_name, "local_identity": local_identity}


def _installation(value: object, code: str) -> dict[str, str]:
    item = _obj(value, code)
    return {
        "installation_id": _text(item.get("installation_id"), code),
        "dns_name": _text(item.get("dns_name"), code, max_len=253).lower(),
    }


def _stage(value: object, code: str) -> dict[str, Any]:
    item = _obj(value, code)
    stage_id = _text(item.get("id"), code, max_len=64)
    sequence = item.get("sequence")
    if stage_id not in STAGE_IDS or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise _fail(code)
    return {"id": stage_id, "sequence": sequence}


def _scalar_map(value: object, code: str) -> dict[str, str | int | float | bool | None]:
    item = _obj(value, code)
    result: dict[str, str | int | float | bool | None] = {}
    for key, child in item.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", key):
            raise _fail(code)
        if child is not None and not isinstance(child, (str, int, float, bool)):
            raise _fail(code)
        result[key] = child
    return result


def validate_install_request(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_install_request_invalid")
    if item.get("schema") != INSTALL_REQUEST_SCHEMA or item.get("contract_version") != CONTRACT_VERSION:
        raise _fail("owh_auto_install_request_invalid")
    access = _obj(item.get("commercial_access"), "owh_auto_install_access_invalid")
    allowed_hosts = _allowed_hosts(access.get("allowed_hosts"), "owh_auto_install_access_invalid")
    activation = item.get("activation")
    policy = item.get("scenario_policy")
    if activation != {"mode": "automatic", "activate_without_browser": True, "manual_action_required": False}:
        raise _fail("owh_auto_install_policy_invalid")
    if policy != {"selection": "latest_compatible", "transport": "https", "integrity": "sha256"}:
        raise _fail("owh_auto_install_policy_invalid")
    return {
        "schema": INSTALL_REQUEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "request_id": _text(item.get("request_id"), "owh_auto_install_request_invalid"),
        "idempotency_key": _text(item.get("idempotency_key"), "owh_auto_install_request_invalid"),
        "account_id": _text(item.get("account_id"), "owh_auto_install_request_invalid"),
        "target": _target(item.get("target"), "owh_auto_install_target_invalid"),
        "commercial_access": {
            "commercial_key_id": _text(access.get("commercial_key_id"), "owh_auto_install_access_invalid"),
            "commercial_api_key": _text(access.get("commercial_api_key"), "owh_auto_install_access_invalid"),
            **({"allowed_hosts": allowed_hosts} if allowed_hosts is not None else {}),
        },
        "activation": dict(activation),
        "scenario_policy": dict(policy),
    }


def validate_install_response(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_install_response_invalid")
    activation = _obj(item.get("activation"), "owh_auto_install_response_invalid")
    installation = _obj(item.get("installation"), "owh_auto_install_response_invalid")
    if item.get("schema") != INSTALL_RESPONSE_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or item.get("status") not in {"registered", "activated"}:
        raise _fail("owh_auto_install_response_invalid")
    if activation != {"state": "active", "mode": "automatic", "manual_action_required": False}:
        raise _fail("owh_auto_install_response_invalid")
    dns_name = _text(installation.get("dns_name"), "owh_auto_install_response_invalid", max_len=253).lower()
    local_identity = _text(installation.get("local_identity"), "owh_auto_install_response_invalid", max_len=253).lower()
    if dns_name != local_identity:
        raise _fail("owh_auto_install_identity_mismatch")
    return {
        "schema": INSTALL_RESPONSE_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "request_id": _text(item.get("request_id"), "owh_auto_install_response_invalid"),
        "status": str(item["status"]),
        "installation": {"installation_id": _text(installation.get("installation_id"), "owh_auto_install_response_invalid"), "dns_name": dns_name, "local_identity": local_identity},
        "activation": dict(activation),
    }


def build_scenario_request(*, request_id: str, installation_id: str, dns_name: str, stage_id: str, sequence: int, target: Mapping[str, str]) -> dict[str, Any]:
    if stage_id not in STAGE_IDS or sequence < 1:
        raise _fail("owh_auto_scenario_stage_invalid")
    result = {
        "schema": SCENARIO_REQUEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "request_id": _text(request_id, "owh_auto_scenario_request_invalid"),
        "installation": {"installation_id": _text(installation_id, "owh_auto_scenario_request_invalid"), "dns_name": _text(dns_name, "owh_auto_scenario_request_invalid", max_len=253).lower()},
        "stage": {"id": stage_id, "sequence": sequence},
        "target": {key: _text(target.get(key), "owh_auto_scenario_target_invalid") for key in ("os_id", "os_version", "architecture", "package_manager")},
        "selection": {"mode": "latest_compatible", "transport": "https", "integrity": "sha256"},
    }
    return validate_scenario_request(result)


def validate_scenario_request(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_scenario_request_invalid")
    selection = _obj(item.get("selection"), "owh_auto_scenario_selection_invalid")
    target = _obj(item.get("target"), "owh_auto_scenario_target_invalid")
    if item.get("schema") != SCENARIO_REQUEST_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or selection != {"mode": "latest_compatible", "transport": "https", "integrity": "sha256"}:
        raise _fail("owh_auto_scenario_request_invalid")
    return {
        "schema": SCENARIO_REQUEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "request_id": _text(item.get("request_id"), "owh_auto_scenario_request_invalid"),
        "installation": _installation(item.get("installation"), "owh_auto_scenario_request_invalid"),
        "stage": _stage(item.get("stage"), "owh_auto_scenario_stage_invalid"),
        "target": {key: _text(target.get(key), "owh_auto_scenario_target_invalid") for key in ("os_id", "os_version", "architecture", "package_manager")},
        "selection": dict(selection),
    }


def validate_artifact_response(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_artifact_response_invalid")
    status = item.get("status")
    if item.get("schema") != ARTIFACT_RESPONSE_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or status not in {"available", "unsupported"}:
        raise _fail("owh_auto_artifact_response_invalid")
    result: dict[str, Any] = {"schema": ARTIFACT_RESPONSE_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": _text(item.get("request_id"), "owh_auto_artifact_response_invalid"), "stage": _stage(item.get("stage"), "owh_auto_artifact_response_invalid"), "status": str(status)}
    if status == "unsupported":
        if item.get("reason_code") not in {"no_compatible_revision", "artifact_unavailable"}:
            raise _fail("owh_auto_artifact_response_invalid")
        result["reason_code"] = item["reason_code"]
        return result
    scenario = _obj(item.get("scenario"), "owh_auto_artifact_response_invalid")
    compatible = _obj(scenario.get("compatible_with"), "owh_auto_artifact_response_invalid")
    artifact = _obj(item.get("artifact"), "owh_auto_artifact_response_invalid")
    uri = _text(artifact.get("uri"), "owh_auto_artifact_response_invalid")
    if not uri.startswith("https://"):
        raise _fail("owh_auto_artifact_https_required")
    allowed_hosts = _allowed_hosts(item.get("allowed_hosts"), "owh_auto_artifact_response_invalid")
    size = artifact.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise _fail("owh_auto_artifact_response_invalid")
    result["scenario"] = {"scenario_id": _text(scenario.get("scenario_id"), "owh_auto_artifact_response_invalid"), "revision": _text(scenario.get("revision"), "owh_auto_artifact_response_invalid"), "compatible_with": {key: _text(compatible.get(key), "owh_auto_artifact_response_invalid") for key in ("os_id", "os_version", "architecture", "package_manager")}}
    result["artifact"] = {"uri": uri, "revision": _text(artifact.get("revision"), "owh_auto_artifact_response_invalid"), "digest": _digest(artifact.get("digest"), "owh_auto_artifact_response_invalid"), "size_bytes": size, "media_type": _text(artifact.get("media_type"), "owh_auto_artifact_response_invalid")}
    if allowed_hosts is not None:
        result["allowed_hosts"] = allowed_hosts
    return result


def validate_stage_receipt(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_stage_receipt_invalid")
    status = item.get("status")
    if item.get("schema") != STAGE_RECEIPT_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or status not in {"started", "succeeded", "failed"}:
        raise _fail("owh_auto_stage_receipt_invalid")
    result: dict[str, Any] = {"schema": STAGE_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": _text(item.get("request_id"), "owh_auto_stage_receipt_invalid"), "installation": _installation(item.get("installation"), "owh_auto_stage_receipt_invalid"), "stage": _stage(item.get("stage"), "owh_auto_stage_receipt_invalid"), "status": str(status), "observed_at": _text(item.get("observed_at"), "owh_auto_stage_receipt_invalid")}
    if "artifact_digest" in item:
        result["artifact_digest"] = _digest(item.get("artifact_digest"), "owh_auto_stage_receipt_invalid")
    if "result" in item:
        result["result"] = _scalar_map(item.get("result"), "owh_auto_stage_receipt_invalid")
    if status == "failed":
        error = _obj(item.get("error"), "owh_auto_stage_receipt_error_required")
        result["error"] = {"code": _text(error.get("code"), "owh_auto_stage_receipt_invalid"), "message": _text(error.get("message"), "owh_auto_stage_receipt_invalid")}
    return result


def validate_terminal_receipt(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_auto_terminal_receipt_invalid")
    status = item.get("status")
    stages = item.get("stages")
    if item.get("schema") != TERMINAL_RECEIPT_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or status not in {"succeeded", "failed"} or not isinstance(stages, list) or not stages:
        raise _fail("owh_auto_terminal_receipt_invalid")
    parsed_stages = []
    seen: set[str] = set()
    for entry in stages:
        stage_item = _obj(entry, "owh_auto_terminal_receipt_invalid")
        parsed = _stage(stage_item, "owh_auto_terminal_receipt_invalid")
        if parsed["id"] in seen or stage_item.get("status") not in {"succeeded", "failed"}:
            raise _fail("owh_auto_terminal_receipt_invalid")
        seen.add(parsed["id"])
        parsed_stages.append({**parsed, "status": str(stage_item["status"])})
    result: dict[str, Any] = {"schema": TERMINAL_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": _text(item.get("request_id"), "owh_auto_terminal_receipt_invalid"), "installation": _installation(item.get("installation"), "owh_auto_terminal_receipt_invalid"), "status": str(status), "started_at": _text(item.get("started_at"), "owh_auto_terminal_receipt_invalid"), "completed_at": _text(item.get("completed_at"), "owh_auto_terminal_receipt_invalid"), "stages": parsed_stages}
    if status == "failed":
        error = _obj(item.get("error"), "owh_auto_terminal_receipt_error_required")
        result["error"] = {"code": _text(error.get("code"), "owh_auto_terminal_receipt_invalid"), "message": _text(error.get("message"), "owh_auto_terminal_receipt_invalid")}
    return result


def validate_manual_enrollment(value: object) -> dict[str, Any]:
    _reject_forbidden(value)
    item = _obj(value, "owh_manual_enrollment_invalid")
    activation = _obj(item.get("activation"), "owh_manual_enrollment_invalid")
    if item.get("schema") != MANUAL_ENROLLMENT_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or item.get("mode") != "manual" or activation != {"state": "pending", "manual_action_required": True, "install_scenarios": False}:
        raise _fail("owh_manual_enrollment_invalid")
    target = _target(item.get("installation"), "owh_manual_enrollment_invalid")
    return {"schema": MANUAL_ENROLLMENT_SCHEMA, "contract_version": CONTRACT_VERSION, "mode": "manual", "installation": target, "activation": dict(activation)}


def _owh_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def accept_automatic_install_request(
    state: Any,
    request: Mapping[str, Any],
    *,
    register: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Accept Portal's automatic bootstrap and persist the complete binding.

    ``register`` is the delivery-specific HTTPS registration adapter.  Keeping
    it injected prevents this contract module from inventing Portal routes or
    embedding host/OS policy.  The commercial API key is persisted only in the
    existing protected state store and is never included in the response.
    """
    parsed = validate_install_request(request)
    # The automatic payload deliberately carries neither worker identity nor
    # Portal origin.  Both come from the already-installed MCD runtime.
    worker_id = str(state.get("agent_id") or "").strip()
    portal_base = str(state.get("portal_api_base") or "").strip()
    if not worker_id or not portal_base.startswith("https://"):
        raise OwhAutoContractError("owh_auto_worker_config_missing")
    existing_key_id = str(state.get("commercial_key_id") or "").strip()
    if existing_key_id and existing_key_id != parsed["commercial_access"]["commercial_key_id"]:
        raise OwhAutoContractError("owh_auto_commercial_key_mismatch")
    # Stage the account key before the registration adapter makes its HTTPS
    # call; it is protected state, never part of receipts or UI output.
    state.put("portal_agent_credential", parsed["commercial_access"]["commercial_api_key"])
    state.put("commercial_key_id", parsed["commercial_access"]["commercial_key_id"])
    response = validate_install_response(register(parsed))
    if response["request_id"] != parsed["request_id"]:
        raise OwhAutoContractError("owh_auto_install_request_mismatch")
    target = parsed["target"]
    installation = response["installation"]
    if installation["dns_name"] != target["dns_name"] or installation["local_identity"] != target["local_identity"]:
        raise OwhAutoContractError("owh_auto_install_identity_mismatch")
    installation_ids = state.get("installation_ids")
    installation_ids = dict(installation_ids) if isinstance(installation_ids, Mapping) else {}
    prior = installation_ids.get(target["local_identity"])
    if prior not in (None, installation["installation_id"]):
        raise OwhAutoContractError("owh_auto_installation_rebind")
    installation_ids[target["local_identity"]] = installation["installation_id"]
    state.put("installation_ids", installation_ids)
    state.put("commercial_portal_connection_state", "active")
    state.put("commercial_activation_state", "active")
    state.put("commercial_revoked", False)
    state.put("commercial_detached", False)
    state.put(
        "commercial_owh_auto_install_v1",
        {
            "request_id": parsed["request_id"],
            "idempotency_key": parsed["idempotency_key"],
            "account_id": parsed["account_id"],
            "installation_id": installation["installation_id"],
            "dns_name": target["dns_name"],
            "commercial_key_id": parsed["commercial_access"]["commercial_key_id"],
            "activation": "active",
        },
    )
    return response


class OwhAutoStageRunner:
    """Transport-neutral OWH-AUTO stage loop.

    Portal supplies the scenario selector and artifact bytes; MCD only checks
    HTTPS/SHA-256/size, invokes the supplied generic executor, and emits the
    contract receipts.  Receipt delivery is injected so the caller can use
    the same durable queue used by the existing Portal worker.
    """

    def __init__(
        self,
        *,
        request_scenario: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        download_artifact: Callable[[str, int], bytes],
        execute_stage: Callable[[Mapping[str, Any], bytes], Mapping[str, Any]],
        send_receipt: Callable[[Mapping[str, Any]], Any],
    ) -> None:
        self.request_scenario = request_scenario
        self.download_artifact = download_artifact
        self.execute_stage = execute_stage
        self.send_receipt = send_receipt

    def _send_stage(self, payload: Mapping[str, Any]) -> None:
        validated = validate_stage_receipt(payload)
        if self.send_receipt(validated) is False:
            raise OwhAutoContractError("owh_auto_receipt_delivery_failed")

    def _send_terminal(self, payload: Mapping[str, Any]) -> None:
        validated = validate_terminal_receipt(payload)
        if self.send_receipt(validated) is False:
            raise OwhAutoContractError("owh_auto_receipt_delivery_failed")

    def run(
        self,
        *,
        request_id: str,
        installation_id: str,
        dns_name: str,
        target: Mapping[str, str],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        started = started_at or _owh_utc()
        statuses: list[dict[str, Any]] = []
        terminal_status = "succeeded"
        terminal_error: dict[str, str] | None = None
        for sequence, stage_id in enumerate(STAGE_IDS, 1):
            stage = {"id": stage_id, "sequence": sequence}
            stage_request = build_scenario_request(request_id=request_id, installation_id=installation_id, dns_name=dns_name, stage_id=stage_id, sequence=sequence, target=target)
            self._send_stage({"schema": STAGE_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": request_id, "installation": {"installation_id": installation_id, "dns_name": dns_name}, "stage": stage, "status": "started", "observed_at": _owh_utc()})
            try:
                artifact_response = validate_artifact_response(self.request_scenario(stage_request))
                if artifact_response["stage"] != stage:
                    raise OwhAutoContractError("owh_auto_artifact_stage_mismatch")
                if artifact_response["status"] != "available":
                    raise OwhAutoContractError(str(artifact_response.get("reason_code") or "owh_auto_artifact_unavailable"))
                artifact = artifact_response["artifact"]
                body = self.download_artifact(str(artifact["uri"]), int(artifact["size_bytes"]))
                if not isinstance(body, bytes) or len(body) != int(artifact["size_bytes"]):
                    raise OwhAutoContractError("owh_auto_artifact_size_mismatch")
                if hashlib.sha256(body).hexdigest() != str(artifact["digest"])[7:]:
                    raise OwhAutoContractError("owh_auto_artifact_digest_mismatch")
                result = self.execute_stage(stage_request, body)
                result = dict(result) if isinstance(result, Mapping) else {"value": result}
                self._send_stage({"schema": STAGE_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": request_id, "installation": {"installation_id": installation_id, "dns_name": dns_name}, "stage": stage, "status": "succeeded", "observed_at": _owh_utc(), "artifact_digest": artifact["digest"], "result": result})
                statuses.append({**stage, "status": "succeeded"})
            except Exception as exc:
                code = str(exc)[:120] if str(exc) else type(exc).__name__[:120]
                error = {"code": code, "message": str(exc)[:1024] or "stage failed"}
                self._send_stage({"schema": STAGE_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": request_id, "installation": {"installation_id": installation_id, "dns_name": dns_name}, "stage": stage, "status": "failed", "observed_at": _owh_utc(), "error": error})
                statuses.append({**stage, "status": "failed"})
                terminal_status = "failed"
                terminal_error = error
                break
        terminal: dict[str, Any] = {"schema": TERMINAL_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": request_id, "installation": {"installation_id": installation_id, "dns_name": dns_name}, "status": terminal_status, "started_at": started, "completed_at": _owh_utc(), "stages": statuses}
        if terminal_error is not None:
            terminal["error"] = terminal_error
        self._send_terminal(terminal)
        return validate_terminal_receipt(terminal)
