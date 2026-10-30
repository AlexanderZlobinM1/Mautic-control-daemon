"""Portal generic-command broker boundary for MCD.

Only declared, typed generic primitives may cross this boundary.  The module
does not interpret shell, scripts, argv, credentials, keys, tokens, or
scenario policy.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from mcd_agent.commercial_portal import canonical_json


CONTRACT_VERSION = "1.0"
CAPABILITY_INVENTORY_SCHEMA = "portal-mcd-capability-inventory-v1"
COMMAND_REQUEST_SCHEMA = "portal-mcd-generic-command-request-v1"
COMMAND_JOB_SCHEMA = "portal-mcd-generic-command-job-v1"
STAGE_RECEIPT_SCHEMA = "portal-mcd-generic-stage-receipt-v1"
TERMINAL_RECEIPT_SCHEMA = "portal-mcd-generic-terminal-receipt-v1"


class GenericCommandContractError(ValueError):
    """Secret-free generic broker validation failure."""


def _error(code: str) -> GenericCommandContractError:
    return GenericCommandContractError(code)


def _obj(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(code)
    return value


def _text(value: object, code: str, max_len: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise _error(code)
    return value.strip()


def _identifier(value: object, code: str, max_len: int = 192) -> str:
    result = _text(value, code, max_len)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", result):
        raise _error(code)
    return result


def _sha(value: object, code: str) -> str:
    result = _text(value, code, 71)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
        raise _error(code)
    return result


def _identity(value: object, code: str) -> dict[str, str]:
    item = _obj(value, code)
    installation_id = _identifier(item.get("installation_id"), code)
    dns_name = _text(item.get("dns_name"), code, 253).lower()
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", dns_name):
        raise _error(code)
    return {"installation_id": installation_id, "dns_name": dns_name}


def _secret_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if re.fullmatch(r"signature|agent_signature|agent_key|encryption|encrypted|access_token|enrollment_token|installation_token", str(key), re.IGNORECASE):
                raise _error("portal_mcd_generic_security_extension_forbidden")
            _secret_free(child)
    elif isinstance(value, list):
        for child in value:
            _secret_free(child)


def _scalar(value: object, code: str, depth: int = 0, *, max_depth: int = 5) -> object:
    if depth > max_depth:
        raise _error(code)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_scalar(item, code, depth + 1, max_depth=max_depth) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            text = str(key)
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", text) or re.fullmatch(r"shell|script|command_line|argv|executable|sudo|password|private_key|secret|token|credential", text, re.IGNORECASE):
                raise _error(code)
            result[text] = _scalar(child, code, depth + 1, max_depth=max_depth)
        return result
    raise _error(code)


def capability_inventory_digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _capability(value: object, code: str) -> dict[str, Any]:
    item = _obj(value, code)
    commands = item.get("commands")
    if not isinstance(commands, list) or not commands:
        raise _error(code)
    parsed = []
    for entry in commands:
        command = _obj(entry, code)
        if command.get("execution") != "generic_primitive" or not isinstance(command.get("mutates_state"), bool):
            raise _error(code)
        parsed.append({"command_id": _identifier(command.get("command_id"), code), "version": _identifier(command.get("version"), code, 64), "execution": "generic_primitive", "mutates_state": command["mutates_state"], "input_schema_ref": _text(command.get("input_schema_ref"), code)})
    keys = [f"{item['command_id']}:{item['version']}" for item in parsed]
    if len(set(keys)) != len(keys):
        raise _error(code)
    return {"capability_id": _identifier(item.get("capability_id"), code), "version": _identifier(item.get("version"), code, 64), "commands": parsed}


def validate_capability_inventory(value: object) -> dict[str, Any]:
    _secret_free(value)
    item = _obj(value, "portal_mcd_capability_inventory_invalid")
    if item.get("schema") != CAPABILITY_INVENTORY_SCHEMA or item.get("contract_version") != CONTRACT_VERSION:
        raise _error("portal_mcd_capability_inventory_invalid")
    raw = item.get("capabilities")
    if not isinstance(raw, list) or not raw:
        raise _error("portal_mcd_capability_inventory_invalid")
    capabilities = [_capability(entry, "portal_mcd_capability_inventory_invalid") for entry in raw]
    keys = [f"{entry['capability_id']}:{entry['version']}" for entry in capabilities]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise _error("portal_mcd_capability_inventory_invalid")
    result = {"schema": CAPABILITY_INVENTORY_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": _text(item.get("request_id"), "portal_mcd_capability_inventory_invalid"), "installation": _identity(item.get("installation"), "portal_mcd_capability_inventory_invalid"), "observed_at": _text(item.get("observed_at"), "portal_mcd_capability_inventory_invalid"), "capabilities": capabilities, "inventory_sha256": _sha(item.get("inventory_sha256"), "portal_mcd_capability_inventory_digest_invalid")}
    unsigned = dict(result)
    unsigned.pop("inventory_sha256")
    if result["inventory_sha256"] != capability_inventory_digest(unsigned):
        raise _error("portal_mcd_capability_inventory_digest_invalid")
    return result


def validate_generic_command_request(value: object) -> dict[str, Any]:
    _secret_free(value)
    item = _obj(value, "portal_mcd_generic_command_request_invalid")
    if item.get("schema") != COMMAND_REQUEST_SCHEMA or item.get("contract_version") != CONTRACT_VERSION:
        raise _error("portal_mcd_generic_command_request_invalid")
    parsed = _scalar(item.get("input"), "portal_mcd_generic_command_input_invalid")
    if not isinstance(parsed, Mapping):
        raise _error("portal_mcd_generic_command_input_invalid")
    return {"schema": COMMAND_REQUEST_SCHEMA, "contract_version": CONTRACT_VERSION, "request_id": _identifier(item.get("request_id"), "portal_mcd_generic_command_request_invalid"), "idempotency_key": _identifier(item.get("idempotency_key"), "portal_mcd_generic_command_request_invalid"), "installation_id": _identifier(item.get("installation_id"), "portal_mcd_generic_command_request_invalid"), "capability_id": _identifier(item.get("capability_id"), "portal_mcd_generic_command_request_invalid"), "command_id": _identifier(item.get("command_id"), "portal_mcd_generic_command_request_invalid"), "command_version": _identifier(item.get("command_version"), "portal_mcd_generic_command_request_invalid", 64), "input": dict(parsed), "expected_inventory_sha256": _sha(item.get("expected_inventory_sha256"), "portal_mcd_generic_command_request_invalid")}


def validate_generic_command_job(value: object, inventory: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _secret_free(value)
    item = _obj(value, "portal_mcd_generic_command_job_invalid")
    installation = _identity(item.get("installation"), "portal_mcd_generic_command_job_invalid")
    capability = _obj(item.get("capability"), "portal_mcd_generic_command_job_invalid")
    command = _obj(item.get("command"), "portal_mcd_generic_command_job_invalid")
    parsed = _scalar(item.get("input"), "portal_mcd_generic_command_input_invalid")
    cursor = item.get("delivery_cursor")
    if item.get("schema") != COMMAND_JOB_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1 or not isinstance(parsed, Mapping) or command.get("execution") != "generic_primitive" or not isinstance(command.get("mutates_state"), bool):
        raise _error("portal_mcd_generic_command_job_invalid")
    result = {"schema": COMMAND_JOB_SCHEMA, "contract_version": CONTRACT_VERSION, "job_id": _identifier(item.get("job_id"), "portal_mcd_generic_command_job_invalid"), "request_id": _identifier(item.get("request_id"), "portal_mcd_generic_command_job_invalid"), "idempotency_key": _identifier(item.get("idempotency_key"), "portal_mcd_generic_command_job_invalid"), "command_id": _identifier(item.get("command_id"), "portal_mcd_generic_command_job_invalid"), "delivery_cursor": cursor, "installation": installation, "capability": {"capability_id": _identifier(capability.get("capability_id"), "portal_mcd_generic_command_job_invalid"), "version": _identifier(capability.get("version"), "portal_mcd_generic_command_job_invalid", 64)}, "command": {"command_id": _identifier(command.get("command_id"), "portal_mcd_generic_command_job_invalid"), "version": _identifier(command.get("version"), "portal_mcd_generic_command_job_invalid", 64), "execution": "generic_primitive", "mutates_state": command["mutates_state"]}, "input": dict(parsed), "issued_at": _text(item.get("issued_at"), "portal_mcd_generic_command_job_invalid"), "expires_at": _text(item.get("expires_at"), "portal_mcd_generic_command_job_invalid")}
    if inventory is not None:
        match = next((entry for entry in inventory.get("capabilities", []) if entry["capability_id"] == result["capability"]["capability_id"] and entry["version"] == result["capability"]["version"]), None)
        advertised = next((entry for entry in (match or {}).get("commands", []) if entry["command_id"] == result["command"]["command_id"] and entry["version"] == result["command"]["version"]), None)
        if advertised is None or advertised["mutates_state"] != result["command"]["mutates_state"]:
            raise _error("portal_mcd_generic_command_not_advertised")
    return result


def _receipt_base(item: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {"job_id": _identifier(item.get("job_id"), code), "request_id": _identifier(item.get("request_id"), code), "command_id": _identifier(item.get("command_id"), code), "installation": _identity(item.get("installation"), code)}


def _result(value: object, code: str) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = _scalar(value, code, max_depth=8)
    if not isinstance(parsed, Mapping):
        raise _error(code)
    return dict(parsed)


def _error_value(value: object, code: str) -> dict[str, str] | None:
    if value is None:
        return None
    item = _obj(value, code)
    return {"code": _identifier(item.get("code"), code), "message": _text(item.get("message"), code, 1024)}


def validate_generic_stage_receipt(value: object) -> dict[str, Any]:
    _secret_free(value)
    item = _obj(value, "portal_mcd_generic_stage_receipt_invalid")
    sequence = item.get("sequence")
    status = item.get("status")
    if item.get("schema") != STAGE_RECEIPT_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1 or status not in {"started", "succeeded", "failed"}:
        raise _error("portal_mcd_generic_stage_receipt_invalid")
    result = {"schema": STAGE_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, **_receipt_base(item, "portal_mcd_generic_stage_receipt_invalid"), "stage_id": _identifier(item.get("stage_id"), "portal_mcd_generic_stage_receipt_invalid"), "sequence": sequence, "status": status, "observed_at": _text(item.get("observed_at"), "portal_mcd_generic_stage_receipt_invalid")}
    parsed_result = _result(item.get("result"), "portal_mcd_generic_stage_receipt_invalid")
    error = _error_value(item.get("error"), "portal_mcd_generic_stage_receipt_invalid")
    if parsed_result is not None:
        result["result"] = parsed_result
    if status == "failed" and error is None:
        raise _error("portal_mcd_generic_stage_receipt_error_required")
    if error is not None:
        result["error"] = error
    return result


def validate_generic_terminal_receipt(value: object) -> dict[str, Any]:
    _secret_free(value)
    item = _obj(value, "portal_mcd_generic_terminal_receipt_invalid")
    sequence = item.get("sequence")
    status = item.get("status")
    if item.get("schema") != TERMINAL_RECEIPT_SCHEMA or item.get("contract_version") != CONTRACT_VERSION or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1 or status not in {"succeeded", "failed"}:
        raise _error("portal_mcd_generic_terminal_receipt_invalid")
    result = {"schema": TERMINAL_RECEIPT_SCHEMA, "contract_version": CONTRACT_VERSION, **_receipt_base(item, "portal_mcd_generic_terminal_receipt_invalid"), "sequence": sequence, "status": status, "started_at": _text(item.get("started_at"), "portal_mcd_generic_terminal_receipt_invalid"), "completed_at": _text(item.get("completed_at"), "portal_mcd_generic_terminal_receipt_invalid")}
    parsed_result = _result(item.get("result"), "portal_mcd_generic_terminal_receipt_invalid")
    error = _error_value(item.get("error"), "portal_mcd_generic_terminal_receipt_invalid")
    if parsed_result is not None:
        result["result"] = parsed_result
    if status == "failed" and error is None:
        raise _error("portal_mcd_generic_terminal_receipt_error_required")
    if error is not None:
        result["error"] = error
    return result
