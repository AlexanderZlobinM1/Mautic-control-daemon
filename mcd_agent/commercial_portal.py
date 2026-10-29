"""MCD-owned SalesSnap Portal commercial transport boundary (contract v1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import socket
import ssl
import platform
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import request
from urllib.parse import quote
from urllib.error import HTTPError, URLError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from mcd_agent import __version__

CONTRACT = "com.sales-snap.portal-commercial/1.0"
CONTRACT_VERSION = "1.0"
SELF_HOSTED_MCD_JOB_SCHEMA = "portal-self-hosted-mcd-job-v2"
SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA = "mcd-self-hosted-stage-receipt-v2"
SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA = "mcd-self-hosted-terminal-receipt-v2"
SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION = "2.0"
SELF_HOSTED_MCD_CONTRACT_VERSION = "2.1"
SELF_HOSTED_MCD_CONTRACT_VERSIONS = frozenset({SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION, SELF_HOSTED_MCD_CONTRACT_VERSION})
SELF_HOSTED_MCD_STAGE_IDS = frozenset({"scenario_dispatch", "execution"})
SELF_HOSTED_MCD_STAGE_STATUSES = frozenset({"queued", "running", "waiting", "succeeded", "failed"})
SELF_HOSTED_MCD_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
SELF_HOSTED_MCD_ROLLBACK_STATUSES = frozenset({"not_attempted", "succeeded", "failed"})
SELF_HOSTED_MCD_SCENARIO_STAGES = (
    ("base_packages", "ensure_supported_base_packages"),
    ("database_cache", "provision_database_and_cache"),
    ("web_stack", "configure_web_and_vhost"),
    ("mautic", "composer_install_current_mautic"),
)
PORTAL_MCD_ADOPT_CONTRACT_VERSION = "1.0"
PORTAL_MCD_POLL_REQUEST_SCHEMA = "portal-mcd-poll-request-v1"
PORTAL_MCD_POLL_RESPONSE_SCHEMA = "portal-mcd-poll-response-v1"
PORTAL_MCD_MAX_LONG_POLL_MS = 30_000
PORTAL_MCD_MAX_BACKOFF_MS = 300_000
STANDALONE_SETTINGS_SNAPSHOT_SCHEMA = "mcd-standalone-settings-snapshot-v1"
INVENTORY_STATE_SCHEMA = "mcd-commercial-inventory-state-v1"
INVENTORY_VOLATILE_FIELDS = frozenset({"request_id", "observed_at"})
MCC_ARTIFACT_SIGNATURE_DOMAIN = b"com.sales-snap.portal-mcc/artifact-signature/1.0\n"
PORTAL_ARTIFACT_SIGNATURE_DOMAIN = b"com.sales-snap.portal/artifact-signature/1.0\n"
PORTAL_ARTIFACT_TRUST_SCHEMA = "portal-artifact-trust-v1"
PORTAL_ARTIFACT_TRUST_UPDATE_SCHEMA = "portal-mcd-artifact-trust-update-v1"
PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION = "1.0"
PORTAL_ARTIFACT_TRUST_DOMAIN = "portal_self_host_scenario_artifact"
MCC_CATALOG_SHA256_TRUST_MODE = "mcc_catalog_sha256"
MCC_AUTHENTICATED_ARTIFACT_TRANSPORT = "mcc_authenticated_api"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
PORTAL_USER_AGENT = f"mcd-agent/{__version__} portal-commercial/{CONTRACT_VERSION}"


class CommercialPortalError(RuntimeError):
    """Safe, typed diagnostics for outbound Portal failures.

    The exception deliberately carries only normalized stage/code/status data;
    response bodies, URLs, query strings, credentials and activation material
    are never retained or rendered.
    """

    def __init__(self, *, stage: str, code: str, http_status: int | None = None, retryable: bool = False):
        self.stage = str(stage)
        self.code = str(code)
        self.http_status = int(http_status) if http_status is not None else None
        self.retryable = bool(retryable)
        detail = f" stage={self.stage}"
        if self.http_status is not None:
            detail += f" http_status={self.http_status}"
        detail += f" retryable={str(self.retryable).lower()}"
        super().__init__(f"{self.code}{detail}")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage,
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload


def _portal_http_error(*, stage: str, status: int, code: str | None = None) -> CommercialPortalError:
    normalized_stage = re.sub(r"[^a-z0-9]+", "_", str(stage).lower()).strip("_") or "request"
    normalized_status = int(status)
    normalized_code = re.sub(r"[^a-z0-9_]+", "_", str(code or "").lower()).strip("_")
    if not normalized_code or len(normalized_code) > 120:
        normalized_code = f"commercial_{normalized_stage}_http_{normalized_status}"
    return CommercialPortalError(
        stage=stage,
        code=normalized_code,
        http_status=normalized_status,
        retryable=normalized_status in {408, 425, 429} or normalized_status >= 500,
    )


def _portal_http_error_from_exception(*, stage: str, exc: HTTPError) -> CommercialPortalError:
    """Keep a bounded, typed Portal error code without retaining response data."""
    code = ""
    try:
        raw = exc.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        candidate = payload.get("code") if isinstance(payload, Mapping) else None
        if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,120}", candidate):
            code = candidate
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return _portal_http_error(stage=stage, status=exc.code, code=code or None)


def _portal_transport_error(*, stage: str) -> CommercialPortalError:
    return CommercialPortalError(stage=stage, code="commercial_portal_transport_error", retryable=True)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_standalone_settings(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return the one canonical, secret-free settings value set used by MCD."""
    if not isinstance(values, Mapping):
        raise ValueError("commercial_standalone_settings_invalid")
    return dict(values)


def standalone_settings_etag(values: Mapping[str, Any]) -> str:
    """Hash exactly the settings values represented in Portal inventory."""
    return _sha256(canonical_json(canonical_standalone_settings(values)))


def build_standalone_settings_snapshot(
    values: Mapping[str, Any],
    *,
    revision: int,
    sync: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the durable settings shape shared by inventory and noop checks."""
    clean = canonical_standalone_settings(values)
    return {
        "values": clean,
        "revision": int(revision),
        "etag": standalone_settings_etag(clean),
        "sync": dict(sync),
    }


def canonical_inventory_content(inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Return durable inventory content with only transport volatility removed."""
    if not isinstance(inventory, Mapping):
        raise ValueError("commercial_inventory_invalid")
    content = dict(inventory)
    for key in INVENTORY_VOLATILE_FIELDS:
        content.pop(key, None)
    return content


def inventory_durable_hash(inventory: Mapping[str, Any]) -> str:
    """Hash the complete durable inventory, excluding only volatile transport fields."""
    return _sha256(canonical_json(canonical_inventory_content(inventory)))


def _valid_inventory_hash(value: object) -> bool:
    text = str(value or "")
    return len(text) == 71 and text.startswith("sha256:") and all(char in "0123456789abcdef" for char in text[7:])


def _host_fingerprint() -> str:
    return "sha256:" + hashlib.sha256(socket.gethostname().encode()).hexdigest()


def _require_https(url: str) -> None:
    if not str(url).lower().startswith("https://"):
        raise ValueError("commercial_portal_https_required")


def build_portal_mcd_poll_request(*, request_id: str, idempotency_key: str, binding: Mapping[str, str], cursor: str | None, wait_ms: int, backoff_ms: int) -> dict[str, Any]:
    """Build the exact Stage2 outbound poll envelope."""
    if not request_id or not idempotency_key:
        raise ValueError("portal_mcd_poll_request_invalid")
    required = ("agent_id", "installation_id", "host_fingerprint", "commercial_key_id")
    if any(not str(binding.get(key) or "") for key in required):
        raise ValueError("portal_mcd_poll_request_binding_invalid")
    wait = int(wait_ms)
    backoff = int(backoff_ms)
    if wait < 0 or wait > PORTAL_MCD_MAX_LONG_POLL_MS or backoff < 0 or backoff > PORTAL_MCD_MAX_BACKOFF_MS:
        raise ValueError("portal_mcd_poll_request_invalid")
    return {
        "schema": PORTAL_MCD_POLL_REQUEST_SCHEMA,
        "contract_version": PORTAL_MCD_ADOPT_CONTRACT_VERSION,
        "request_id": str(request_id),
        "idempotency_key": str(idempotency_key),
        "binding": {key: str(binding[key]) for key in required},
        "direction": "mcd_to_portal",
        "cursor": None if cursor is None or cursor == "" else str(cursor),
        "wait_ms": wait,
        "backoff_ms": backoff,
    }


def verify_portal_mcd_poll_response(response: Mapping[str, Any], *, public_keys: Mapping[str, str] | None = None, expected_request_id: str, expected_idempotency_key: str, expected_binding: Mapping[str, str]) -> dict[str, Any]:
    """Validate an unsigned API-key-authenticated Portal poll response."""
    if response.get("schema") != PORTAL_MCD_POLL_RESPONSE_SCHEMA or response.get("contract_version") != PORTAL_MCD_ADOPT_CONTRACT_VERSION:
        raise ValueError("portal_mcd_poll_response_invalid")
    if response.get("request_id") != expected_request_id or response.get("idempotency_key") != expected_idempotency_key:
        raise ValueError("portal_mcd_poll_response_idempotency_mismatch")
    binding = response.get("binding")
    if not isinstance(binding, Mapping) or any(str(binding.get(key) or "") != str(expected_binding.get(key) or "") for key in ("agent_id", "installation_id", "host_fingerprint", "commercial_key_id")):
        raise ValueError("portal_mcd_poll_response_binding_mismatch")
    next_cursor = response.get("next_command_cursor", response.get("next_cursor"))
    commands = response.get("commands")
    retry_after_ms = response.get("retry_after_ms")
    if not ((isinstance(next_cursor, str) and next_cursor) or (isinstance(next_cursor, int) and not isinstance(next_cursor, bool) and next_cursor >= 0)) or not isinstance(commands, list) or any(not isinstance(command, Mapping) for command in commands) or isinstance(retry_after_ms, bool) or not isinstance(retry_after_ms, int) or retry_after_ms < 0 or retry_after_ms > PORTAL_MCD_MAX_BACKOFF_MS:
        raise ValueError("portal_mcd_poll_response_invalid")
    _assert_no_contract_secrets(response)
    return {"next_cursor": str(next_cursor), "commands": [dict(command) for command in commands], "retry_after_ms": retry_after_ms}


def verify_portal_mcd_attach_response(response: Mapping[str, Any], *, public_keys: Mapping[str, str] | None = None, expected_request_id: str, expected_idempotency_key: str, expected_binding: Mapping[str, str]) -> dict[str, Any]:
    """Validate the API-key-authenticated installation binding returned by adoption."""
    if response.get("schema") != "portal-mcd-attach-v1" or response.get("contract_version") != PORTAL_MCD_ADOPT_CONTRACT_VERSION:
        raise ValueError("portal_mcd_attach_response_invalid")
    if response.get("request_id") != expected_request_id or response.get("idempotency_key") != expected_idempotency_key:
        raise ValueError("portal_mcd_attach_response_idempotency_mismatch")
    binding = response.get("binding")
    if not isinstance(binding, Mapping) or any(str(binding.get(key) or "") != str(expected_binding.get(key) or "") for key in ("agent_id", "installation_id", "host_fingerprint", "commercial_key_id")):
        raise ValueError("portal_mcd_attach_response_binding_mismatch")
    connection = response.get("connection")
    inventory = response.get("inventory")
    if response.get("status") not in {"attached", "already_attached"} or not isinstance(connection, Mapping) or connection.get("status") != "active" or connection.get("channel_contract") != PORTAL_MCD_ADOPT_CONTRACT_VERSION or not isinstance(inventory, Mapping) or inventory.get("schema") != "portal-mcd-inventory-snapshot-v1" or inventory.get("complete") is not True:
        raise ValueError("portal_mcd_attach_response_invalid")
    _assert_no_contract_secrets(response)
    return dict(response)


def verify_signed_job(job: Mapping[str, Any], public_keys: Mapping[str, str] | None = None, *, now: float | None = None) -> dict[str, Any]:
    """Validate a Portal job envelope without a second signing-key layer."""
    import time

    if job.get("schema") != "portal-mcd-job-delivery-v1" or job.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("commercial_job_schema_unsupported")
    item = job.get("job")
    if not isinstance(item, dict):
        raise ValueError("commercial_job_missing")
    required = ("job_id", "idempotency_key", "agent_id", "installation_id", "command", "issued_at", "expires_at", "nonce", "scope", "package")
    if any(not item.get(key) for key in required):
        raise ValueError("commercial_job_fields_missing")
    current = time.time() if now is None else float(now)
    if not isinstance(item["issued_at"], str) or not isinstance(item["expires_at"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", item["issued_at"]) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", item["expires_at"]):
        raise ValueError("commercial_job_time_invalid")
    try:
        issued = datetime.fromisoformat(str(item["issued_at"]).replace("Z", "+00:00")).timestamp()
        expires = datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("commercial_job_time_invalid") from exc
    if expires <= issued or current < issued - 60 or current >= expires:
        raise ValueError("commercial_job_expired_or_not_yet_valid")
    scope = item["scope"]
    package = item["package"]
    if not isinstance(scope, dict) or not isinstance(package, dict):
        raise ValueError("commercial_job_shape_invalid")
    if item["scope"].get("agent_id") != item["agent_id"] or item["scope"].get("installation_id") != item["installation_id"]:
        raise ValueError("commercial_job_scope_mismatch")
    allowed_commands = item["scope"].get("allowed_commands")
    if isinstance(allowed_commands, list) and item["command"] not in allowed_commands:
        raise ValueError("commercial_job_command_not_allowed")
    digest = str(package.get("sha256") or package.get("digest") or "").lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("commercial_package_digest_invalid")
    scoped_digest = str(item["scope"].get("artifact_sha256") or "").lower().removeprefix("sha256:")
    if scoped_digest and scoped_digest != digest:
        raise ValueError("commercial_job_artifact_scope_mismatch")
    if not package.get("package_id") or not package.get("revision") or not package.get("media_type"):
        raise ValueError("commercial_package_identity_missing")
    size = int(package.get("size_bytes") or 0)
    if size < 0 or size > MAX_ARTIFACT_BYTES:
        raise ValueError("commercial_package_size_invalid")
    applicability = package.get("applicability")
    if not isinstance(applicability, dict):
        raise ValueError("commercial_package_applicability_missing")
    return {"job_id": str(item["job_id"]), "idempotency_key": str(item["idempotency_key"]), "command": str(item["command"]), "package_id": str(package["package_id"]), "revision": str(package["revision"]), "digest": "sha256:" + digest, "size_bytes": size, "media_type": str(package["media_type"]), "applicability": applicability}


def _require_sha256_digest(value: object, code: str) -> str:
    digest = str(value or "").lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(code)
    return digest


def _require_contract_string(value: object, code: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(code)
    return value.strip()


def _artifact_trust_key(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"algorithm", "key_id", "value", "status", "not_before", "not_after"}:
        raise ValueError(code)
    if value.get("algorithm") != "ed25519" or value.get("status") not in {"active", "revoked"}:
        raise ValueError(code)
    key_id = _require_contract_string(value.get("key_id"), code, maximum=128)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", key_id):
        raise ValueError(code)
    public = _require_contract_string(value.get("value"), code, maximum=43)
    if len(public) != 43 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", public):
        raise ValueError(code)
    result: dict[str, Any] = {"algorithm": "ed25519", "key_id": key_id, "value": public, "status": value["status"]}
    for field in ("not_before", "not_after"):
        if field in value:
            item = value[field]
            if item is not None:
                item = _require_contract_string(item, code, maximum=64)
                try:
                    datetime.fromisoformat(item.replace("Z", "+00:00"))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(code) from exc
            result[field] = item
    if result.get("not_before") and result.get("not_after"):
        before = datetime.fromisoformat(str(result["not_before"]).replace("Z", "+00:00"))
        after = datetime.fromisoformat(str(result["not_after"]).replace("Z", "+00:00"))
        if after <= before:
            raise ValueError(code)
    return result


def _normalize_artifact_trust_keys(value: object, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(code)
    keys = [_artifact_trust_key(item, code) for item in value]
    keys.sort(key=lambda item: item["key_id"])
    if len({item["key_id"] for item in keys}) != len(keys):
        raise ValueError(code)
    return keys


def _artifact_trust_digest(keys: list[dict[str, Any]]) -> str:
    return _sha256(canonical_json({
        "schema": PORTAL_ARTIFACT_TRUST_SCHEMA,
        "contract_version": PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION,
        "trust_domain": PORTAL_ARTIFACT_TRUST_DOMAIN,
        "keys": keys,
    }))


def validate_portal_artifact_trust(value: object, *, code: str = "portal_artifact_trust_invalid") -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema", "contract_version", "trust_domain", "trust_revision", "trust_digest", "keys"}:
        raise ValueError(code)
    if value.get("schema") != PORTAL_ARTIFACT_TRUST_SCHEMA or value.get("contract_version") != PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION or value.get("trust_domain") != PORTAL_ARTIFACT_TRUST_DOMAIN:
        raise ValueError(code)
    revision = value.get("trust_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError(code)
    keys = _normalize_artifact_trust_keys(value.get("keys"), code)
    digest = _require_sha256_digest(value.get("trust_digest"), code)
    if digest != _artifact_trust_digest(keys):
        raise ValueError(f"{code}_revision")
    return {"schema": PORTAL_ARTIFACT_TRUST_SCHEMA, "contract_version": PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION, "trust_domain": PORTAL_ARTIFACT_TRUST_DOMAIN, "trust_revision": revision, "trust_digest": digest, "keys": keys}


def validate_portal_artifact_trust_update(value: object, *, code: str = "portal_artifact_trust_update_invalid") -> dict[str, Any]:
    required = {"schema", "contract_version", "command_id", "delivery_cursor", "request_id", "idempotency_key", "binding", "trust_domain", "trust_revision", "trust_digest", "keys", "issued_at", "expires_at"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError(code)
    if value.get("schema") != PORTAL_ARTIFACT_TRUST_UPDATE_SCHEMA or value.get("contract_version") != PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION or value.get("trust_domain") != PORTAL_ARTIFACT_TRUST_DOMAIN:
        raise ValueError(code)
    command_id = _require_contract_string(value.get("command_id"), code, maximum=128)
    request_id = _require_contract_string(value.get("request_id"), code)
    idempotency_key = _require_contract_string(value.get("idempotency_key"), code, maximum=128)
    cursor = value.get("delivery_cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1:
        raise ValueError(code)
    binding = value.get("binding")
    if not isinstance(binding, Mapping) or set(binding) != {"agent_id", "installation_id", "host_fingerprint", "commercial_key_id"}:
        raise ValueError(code)
    normalized_binding = {
        "agent_id": _require_contract_string(binding.get("agent_id"), code),
        "installation_id": _require_contract_string(binding.get("installation_id"), code),
        "host_fingerprint": _require_sha256_digest(binding.get("host_fingerprint"), code),
        "commercial_key_id": _require_contract_string(binding.get("commercial_key_id"), code),
    }
    revision = value.get("trust_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError(code)
    keys = _normalize_artifact_trust_keys(value.get("keys"), code)
    digest = _require_sha256_digest(value.get("trust_digest"), code)
    if digest != _artifact_trust_digest(keys):
        raise ValueError(f"{code}_revision")
    issued_at = _require_contract_string(value.get("issued_at"), code, maximum=64)
    expires_at = _require_contract_string(value.get("expires_at"), code, maximum=64)
    try:
        issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(code) from exc
    if expires <= issued:
        raise ValueError(f"{code}_time")
    return {"schema": PORTAL_ARTIFACT_TRUST_UPDATE_SCHEMA, "contract_version": PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION, "command_id": command_id, "delivery_cursor": cursor, "request_id": request_id, "idempotency_key": idempotency_key, "binding": normalized_binding, "trust_domain": PORTAL_ARTIFACT_TRUST_DOMAIN, "trust_revision": revision, "trust_digest": digest, "keys": keys, "issued_at": issued_at, "expires_at": expires_at}


def portal_artifact_trust_keys(value: object) -> dict[str, dict[str, Any] | str]:
    """Return verifier material while preserving canonical key status metadata."""
    if isinstance(value, Mapping) and value.get("schema") == PORTAL_ARTIFACT_TRUST_SCHEMA:
        trust = validate_portal_artifact_trust(value)
        return {key["key_id"]: dict(key) for key in trust["keys"]}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items() if isinstance(item, (str, Mapping))}
    return {}


def _assert_no_contract_secrets(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if re.search(r"password|private.?key|secret|activation.?code|agent.?credential|enrollment.?token|access.?token", str(key), re.I):
                raise ValueError("self_hosted_mcd_secret_in_contract")
            _assert_no_contract_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_contract_secrets(child)


def _public_commercial_key_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only non-secret commercial-key identity metadata."""
    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            return {
                str(key): clean(child)
                for key, child in item.items()
                if not re.search(r"password|private.?key|secret|activation.?code|agent.?credential|enrollment.?token|access.?token", str(key), re.I)
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    result = clean(value)
    return result if isinstance(result, dict) else {}


def _self_hosted_facts_digest(facts: Mapping[str, Any]) -> str:
    normalized = dict(facts)
    normalized["capabilities"] = sorted(set(str(item) for item in normalized.get("capabilities", [])))
    return _sha256(canonical_json(normalized))


def _validate_self_hosted_facts(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("self_hosted_mcd_facts_invalid")
    required = ("os_id", "os_version", "architecture", "package_manager", "capabilities", "target_kind")
    if any(not isinstance(value.get(key), str) or not str(value.get(key)).strip() for key in required[:4]):
        raise ValueError("self_hosted_mcd_facts_invalid")
    if value.get("target_kind") not in {"new_server", "existing_server"}:
        raise ValueError("self_hosted_mcd_facts_invalid")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item.strip() for item in capabilities):
        raise ValueError("self_hosted_mcd_facts_invalid")
    normalized = {
        "os_id": str(value["os_id"]).strip(),
        "os_version": str(value["os_version"]).strip(),
        "architecture": str(value["architecture"]).strip(),
        "package_manager": str(value["package_manager"]).strip(),
        "capabilities": sorted(set(str(item).strip() for item in capabilities)),
        "target_kind": str(value["target_kind"]),
    }
    optional = {"detector_schema", "classification_reason", "classification_evidence"}
    if set(value) - set(normalized) - optional:
        raise ValueError("self_hosted_mcd_facts_invalid")
    if "detector_schema" in value:
        if not isinstance(value["detector_schema"], str) or not str(value["detector_schema"]).strip():
            raise ValueError("self_hosted_mcd_facts_invalid")
    if ("classification_reason" in value) != ("classification_evidence" in value):
        raise ValueError("self_hosted_mcd_facts_invalid")
    if "classification_reason" in value:
        reason = value["classification_reason"]
        evidence = value["classification_evidence"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256 or not isinstance(evidence, Mapping):
            raise ValueError("self_hosted_mcd_facts_invalid")
        normalized["classification_reason"] = reason.strip()
        normalized["classification_evidence"] = dict(evidence)
    return normalized


def _validate_self_hosted_scenario(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("self_hosted_mcd_scenario_binding_invalid")
    mode = value.get("mode")
    if mode not in {"bootstrap", "scan"}:
        raise ValueError("self_hosted_mcd_scenario_binding_invalid")
    return {
        "scenario_id": _require_contract_string(value.get("scenario_id"), "self_hosted_mcd_scenario_binding_invalid"),
        "revision": _require_contract_string(value.get("revision"), "self_hosted_mcd_scenario_binding_invalid"),
        "mode": mode,
        "artifact_digest": _require_sha256_digest(value.get("artifact_digest"), "self_hosted_mcd_scenario_binding_invalid"),
    }


def _validate_self_hosted_artifact_pin(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(code)
    size = value.get("artifact_size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"{code}_size")
    digest = _require_sha256_digest(value.get("artifact_digest"), f"{code}_digest")
    media_type = _require_contract_string(value.get("artifact_media_type"), f"{code}_media_type")
    revision = _require_contract_string(value.get("artifact_revision"), f"{code}_revision")
    # Artifact transport is authenticated separately by the Portal API key;
    # the artifact contract carries only immutable size/SHA-256 integrity.
    # Accept the former mode label as a legacy wire value, but never require
    # or inspect its old signature metadata.
    trust_mode = str(value.get("artifact_trust_mode") or "https_sha256").strip()
    if trust_mode == MCC_CATALOG_SHA256_TRUST_MODE:
        if media_type not in {"application/x-sh", "application/x-elf"}:
            raise ValueError(f"{code}_media_type")
        if value.get("artifact_transport") != MCC_AUTHENTICATED_ARTIFACT_TRANSPORT:
            raise ValueError(f"{code}_transport")
        path = value.get("artifact_path")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "://" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
        ):
            raise ValueError(f"{code}_path")
        grant = value.get("artifact_grant")
        if not isinstance(grant, str) or not grant.strip() or len(grant) > 512:
            raise ValueError(f"{code}_grant")
        catalog = value.get("trusted_catalog")
        if not isinstance(catalog, Mapping) or set(catalog) != {"source", "transport", "status", "revision", "artifact_digest"}:
            raise ValueError(f"{code}_catalog")
        if catalog.get("source") != "mcc" or catalog.get("transport") != MCC_AUTHENTICATED_ARTIFACT_TRANSPORT or catalog.get("status") != "active":
            raise ValueError(f"{code}_catalog")
        catalog_revision = _require_contract_string(catalog.get("revision"), f"{code}_catalog")
        catalog_digest = _require_sha256_digest(catalog.get("artifact_digest"), f"{code}_catalog")
        if catalog_revision != revision or catalog_digest != digest:
            raise ValueError(f"{code}_catalog_binding_mismatch")
        if "signature" in value:
            raise ValueError(f"{code}_signature_forbidden")
        return {
            "artifact_trust_mode": MCC_CATALOG_SHA256_TRUST_MODE,
            "artifact_transport": MCC_AUTHENTICATED_ARTIFACT_TRANSPORT,
            "artifact_path": path,
            "artifact_grant": grant,
            "trusted_catalog": dict(catalog),
            "artifact_digest": digest,
            "artifact_size_bytes": size,
            "artifact_media_type": media_type,
            "artifact_revision": revision,
        }
    if trust_mode not in {"https_sha256", "sha256_https", "ed25519"}:
        raise ValueError(f"{code}_trust_mode")
    uri = value.get("artifact_uri")
    if not isinstance(uri, str) or not uri.startswith("https://"):
        raise ValueError(f"{code}_uri")
    return {
        "artifact_trust_mode": "https_sha256",
        "artifact_uri": uri,
        "artifact_digest": digest,
        "artifact_size_bytes": size,
        "artifact_media_type": media_type,
        "artifact_revision": revision,
    }


def verify_self_hosted_mcd_job(
    job: Mapping[str, Any],
    portal_keys: Mapping[str, str],
    *,
    expected_agent_id: str | None = None,
    expected_installation_id: str | None = None,
    expected_commercial_key_id: str | None = None,
    expected_host_fingerprint: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate the exact Portal self-hosted MCD v2 envelope."""
    if not isinstance(job, Mapping):
        raise ValueError("self_hosted_mcd_job_invalid")
    _assert_no_contract_secrets(job)
    contract_version = job.get("contract_version")
    if job.get("schema") != SELF_HOSTED_MCD_JOB_SCHEMA or contract_version not in SELF_HOSTED_MCD_CONTRACT_VERSIONS:
        raise ValueError("self_hosted_mcd_job_invalid")
    required = ("job_id", "request_id", "idempotency_key", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "scenario", "applicability", "scenario_artifact", "execution", "issued_at", "expires_at", "nonce")
    if any(not job.get(key) for key in required):
        raise ValueError("self_hosted_mcd_job_fields_missing")
    identity = {key: _require_contract_string(job.get(key), "self_hosted_mcd_job_invalid") for key in ("job_id", "request_id", "idempotency_key", "account_id", "commercial_key_id", "agent_id", "installation_id", "nonce")}
    host_fingerprint = _require_sha256_digest(job.get("host_fingerprint"), "self_hosted_mcd_host_fingerprint_invalid")
    for key, expected in (("agent_id", expected_agent_id), ("installation_id", expected_installation_id), ("commercial_key_id", expected_commercial_key_id), ("host_fingerprint", expected_host_fingerprint)):
        if expected and identity.get(key, host_fingerprint if key == "host_fingerprint" else None) != expected:
            raise ValueError("self_hosted_mcd_binding_mismatch")
    scenario = _validate_self_hosted_scenario(job.get("scenario"))
    applicability = job.get("applicability")
    if not isinstance(applicability, Mapping):
        raise ValueError("self_hosted_mcd_applicability_invalid")
    facts = _validate_self_hosted_facts(applicability.get("facts"))
    if _require_sha256_digest(applicability.get("facts_digest"), "self_hosted_mcd_facts_digest_invalid") != _self_hosted_facts_digest(facts):
        raise ValueError("self_hosted_mcd_facts_digest_invalid")
    bootstrap = None
    if contract_version == SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION:
        bootstrap = _validate_self_hosted_artifact_pin(job.get("bootstrap_artifact"), "self_hosted_mcd_bootstrap_artifact_invalid")
    elif "bootstrap_artifact" in job:
        raise ValueError("self_hosted_mcd_bootstrap_artifact_forbidden")
    scenario_artifact = _validate_self_hosted_artifact_pin(job.get("scenario_artifact"), "self_hosted_mcd_scenario_artifact_invalid")
    if scenario_artifact["artifact_digest"] != scenario["artifact_digest"]:
        raise ValueError("self_hosted_mcd_scenario_digest_mismatch")
    if scenario_artifact["artifact_revision"] != scenario["revision"]:
        raise ValueError("self_hosted_mcd_scenario_revision_mismatch")
    execution = job.get("execution")
    if not isinstance(execution, Mapping) or execution.get("primitive") != "scenario.apply" or execution.get("version") != "1.0":
        raise ValueError("self_hosted_mcd_execution_invalid")
    stage_selection = job.get("stage_selection")
    if contract_version == SELF_HOSTED_MCD_CONTRACT_VERSION:
        if not isinstance(stage_selection, Mapping):
            raise ValueError("self_hosted_mcd_stage_selection_missing")
        expected_stages = {stage: (sequence, operation) for sequence, (stage, operation) in enumerate(SELF_HOSTED_MCD_SCENARIO_STAGES, 1)}
        if set(stage_selection) != {"stage_id", "sequence", "operation", "depends_on"}:
            raise ValueError("self_hosted_mcd_stage_selection_invalid")
        stage_id = stage_selection.get("stage_id")
        sequence = stage_selection.get("sequence")
        operation = stage_selection.get("operation")
        depends_on = stage_selection.get("depends_on")
        if stage_id not in expected_stages or isinstance(sequence, bool) or not isinstance(sequence, int) or expected_stages[stage_id][0] != sequence or operation != expected_stages[stage_id][1] or not isinstance(depends_on, list):
            raise ValueError("self_hosted_mcd_stage_selection_invalid")
        expected_dependencies = [item[0] for item in SELF_HOSTED_MCD_SCENARIO_STAGES[: sequence - 1]]
        if depends_on != expected_dependencies:
            raise ValueError("self_hosted_mcd_stage_selection_order_invalid")
        execution_stage = execution.get("stage")
        if not isinstance(execution_stage, Mapping) or set(execution_stage) != {"stage_id", "sequence", "operation"} or dict(execution_stage) != {key: stage_selection[key] for key in ("stage_id", "sequence", "operation")}:
            raise ValueError("self_hosted_mcd_execution_stage_mismatch")
    elif stage_selection is not None:
        raise ValueError("self_hosted_mcd_stage_selection_forbidden")
    issued_text = _require_contract_string(job.get("issued_at"), "self_hosted_mcd_job_time_invalid")
    expires_text = _require_contract_string(job.get("expires_at"), "self_hosted_mcd_job_time_invalid")
    try:
        issued = datetime.fromisoformat(issued_text.replace("Z", "+00:00")).timestamp()
        expires = datetime.fromisoformat(expires_text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("self_hosted_mcd_job_time_invalid") from exc
    current = time.time() if now is None else float(now)
    if expires <= issued or current < issued - 60 or current >= expires:
        raise ValueError("self_hosted_mcd_job_expired_or_not_yet_valid")
    return {"job": dict(job), "contract_version": str(contract_version), "scenario": scenario, "facts": facts, "bootstrap_artifact": bootstrap, "scenario_artifact": scenario_artifact, "stage_selection": dict(stage_selection) if isinstance(stage_selection, Mapping) else None, "job_id": identity["job_id"], "idempotency_key": identity["idempotency_key"], "host_fingerprint": host_fingerprint}


def verify_self_hosted_external_artifact(
    pin: Mapping[str, Any],
    artifact: bytes,
    publisher_keys: Mapping[str, Any],
    *,
    kind: str,
    signature_domain: bytes | None = None,
) -> dict[str, Any]:
    """Verify exact bytes using only the immutable SHA-256 artifact pin.

    The trusted MCC catalog path is authenticated by the MCC API transport and
    catalog/job metadata; it deliberately verifies SHA-256 only and does not
    claim that the digest authenticates an artifact author. Legacy signature
    fields are ignored and are not part of the normalized contract.
    """
    normalized = _validate_self_hosted_artifact_pin(pin, f"self_hosted_mcd_{kind}_artifact_invalid")
    if len(artifact) != normalized["artifact_size_bytes"] or hashlib.sha256(artifact).hexdigest() != normalized["artifact_digest"][7:]:
        raise ValueError("self_hosted_mcd_artifact_integrity_invalid")
    return normalized


def download_external_artifact(uri: str, *, expected_size: int, timeout_sec: int = 35) -> bytes:
    """Fetch one pinned HTTPS artifact with a strict bounded response size."""
    if not isinstance(uri, str) or not uri.startswith("https://"):
        raise ValueError("self_hosted_mcd_artifact_uri_invalid")
    expected = int(expected_size)
    if expected <= 0 or expected > MAX_ARTIFACT_BYTES:
        raise ValueError("self_hosted_mcd_artifact_size_invalid")
    req = request.Request(uri, headers={"Accept": "application/octet-stream", "User-Agent": PORTAL_USER_AGENT}, method="GET")
    try:
        with request.urlopen(req, timeout=max(1, min(60, int(timeout_sec))), context=ssl.create_default_context()) as response:
            body = response.read(expected + 1)
    except HTTPError as exc:
        raise _portal_http_error(stage="artifact.download", status=exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise _portal_transport_error(stage="artifact.download") from exc
    if len(body) != expected:
        raise CommercialPortalError(stage="artifact.download", code="self_hosted_mcd_artifact_size_mismatch")
    return body


class CommercialState:
    """Small durable Portal state store, isolated from corporate MCC state."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS commercial_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commercial_nonces (nonce TEXT PRIMARY KEY, seen_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commercial_receipts (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commercial_usage_sequences (metric TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            # Agent, Portal job-signing, and artifact-signing material is
            # retired. Existing state is scrubbed on open; the commercial
            # lane uses only the API credential over HTTPS and SHA-256 checks.
            db.execute(
                "DELETE FROM commercial_meta WHERE key IN ("
                "'agent_private_key','agent_key_id','portal_job_signing_keys',"
                "'portal_artifact_trust_v1','portal_artifact_signing_keys',"
                "'portal_artifact_trust_revision','portal_artifact_trust_digest',"
                "'portal_artifact_trust_domain','portal_artifact_trust_source',"
                "'portal_artifact_trust_updates_v1','portal_mcc_publisher_keys'"
                ")"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, key: str) -> Any:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM commercial_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=True, sort_keys=True)))

    def put_many(self, values: Mapping[str, Any], *, delete_keys: tuple[str, ...] = ()) -> None:
        """Commit a related state transition as one durable transaction."""
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            for key in delete_keys:
                db.execute("DELETE FROM commercial_meta WHERE key=?", (str(key),))
            for key, value in values.items():
                db.execute(
                    "INSERT INTO commercial_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(key), json.dumps(value, ensure_ascii=True, sort_keys=True)),
                )

    def delete(self, key: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM commercial_meta WHERE key=?", (str(key),))

    def apply_portal_artifact_trust_update(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically apply one Portal trust revision and remember its idempotency."""
        update = validate_portal_artifact_trust_update(command)
        fingerprint = hashlib.sha256(canonical_json(update)).hexdigest()
        command_id = update["command_id"]
        idempotency_key = update["idempotency_key"]
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM commercial_meta WHERE key=?", ("portal_artifact_trust_updates_v1",)).fetchone()
            history = json.loads(row[0]) if row else {}
            if not isinstance(history, dict):
                raise ValueError("portal_artifact_trust_update_state_invalid")
            for identity in (command_id, idempotency_key):
                previous = history.get(identity)
                if isinstance(previous, Mapping):
                    if previous.get("fingerprint") != fingerprint:
                        raise ValueError("portal_artifact_trust_update_idempotency_conflict")
                    replay = dict(previous.get("result") or {})
                    replay["idempotent"] = True
                    return replay

            current_row = db.execute("SELECT value FROM commercial_meta WHERE key=?", ("portal_artifact_trust_v1",)).fetchone()
            current = json.loads(current_row[0]) if current_row else None
            current_revision = 0
            current_digest = None
            if current is not None:
                current = validate_portal_artifact_trust(current, code="portal_artifact_trust_state_invalid")
                current_revision = int(current["trust_revision"])
                current_digest = str(current["trust_digest"])
            requested_revision = int(update["trust_revision"])
            requested_digest = str(update["trust_digest"])
            if requested_revision < current_revision:
                raise ValueError("portal_artifact_trust_revision_stale")
            if requested_revision == current_revision and current_digest != requested_digest:
                raise ValueError("portal_artifact_trust_revision_conflict")

            trust = {
                "schema": PORTAL_ARTIFACT_TRUST_SCHEMA,
                "contract_version": PORTAL_ARTIFACT_TRUST_CONTRACT_VERSION,
                "trust_domain": PORTAL_ARTIFACT_TRUST_DOMAIN,
                "trust_revision": requested_revision,
                "trust_digest": requested_digest,
                "keys": list(update["keys"]),
            }
            if current is None or requested_revision > current_revision:
                encoded_trust = json.dumps(trust, ensure_ascii=True, sort_keys=True)
                keys = {item["key_id"]: dict(item) for item in update["keys"]}
                writes = {
                    "portal_artifact_trust_v1": encoded_trust,
                    "portal_artifact_signing_keys": json.dumps(keys, ensure_ascii=True, sort_keys=True),
                    "portal_artifact_trust_revision": json.dumps(requested_revision),
                    "portal_artifact_trust_digest": json.dumps(requested_digest),
                    "portal_artifact_trust_domain": json.dumps(PORTAL_ARTIFACT_TRUST_DOMAIN),
                    "portal_artifact_trust_source": json.dumps("portal"),
                }
                for key, value in writes.items():
                    db.execute("INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            result = {"status": "succeeded", "trust_revision": requested_revision, "trust_digest": requested_digest, "idempotent": current is not None and requested_revision == current_revision}
            record = {"fingerprint": fingerprint, "result": result}
            history[command_id] = record
            history[idempotency_key] = record
            db.execute("INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("portal_artifact_trust_updates_v1", json.dumps(history, ensure_ascii=True, sort_keys=True)))
        return result

    @staticmethod
    def _inventory_state_record(raw: object) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return {"schema": INVENTORY_STATE_SCHEMA, "installations": {}}
        installations = raw.get("installations")
        return {
            "schema": INVENTORY_STATE_SCHEMA,
            "installations": dict(installations) if isinstance(installations, Mapping) else {},
        }

    def inventory_revision(self, installation_id: str) -> int:
        """Read the durable per-installation inventory revision."""
        raw = self.get("commercial_inventory_state_v1")
        state = self._inventory_state_record(raw)
        record = state["installations"].get(str(installation_id))
        if isinstance(record, Mapping):
            value = record.get("snapshot_revision", record.get("revision", 0))
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, value)
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0
        legacy = self.get("commercial_inventory_revision")
        try:
            return max(0, int(legacy or 0))
        except (TypeError, ValueError):
            return 0

    def prepare_inventory_snapshot(self, *, installation_id: str, inventory: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically assign a monotonic revision to changed durable inventory."""
        installation = str(installation_id)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM commercial_meta WHERE key=?", ("commercial_inventory_state_v1",)).fetchone()
            state = self._inventory_state_record(json.loads(row[0]) if row else None)
            installations = state["installations"]
            previous = installations.get(installation)
            previous = dict(previous) if isinstance(previous, Mapping) else {}
            raw_revision = previous.get("snapshot_revision", previous.get("revision", 0))
            try:
                if isinstance(raw_revision, bool):
                    raise ValueError
                revision = int(raw_revision or 0)
            except (TypeError, ValueError):
                raise ValueError("commercial_inventory_state_invalid")
            if revision < 0:
                raise ValueError("commercial_inventory_state_invalid")
            previous_hash = str(previous.get("durable_hash") or "")
            if previous_hash and not _valid_inventory_hash(previous_hash):
                raise ValueError("commercial_inventory_state_invalid")
            if revision > 0 and not previous_hash and not isinstance(previous.get("durable_snapshot"), Mapping):
                raise ValueError("commercial_inventory_state_invalid")

            candidate = dict(inventory)
            candidate["snapshot_revision"] = str(revision)
            candidate_hash = inventory_durable_hash(candidate)
            if previous_hash:
                changed = previous_hash != candidate_hash
            elif isinstance(previous.get("durable_snapshot"), Mapping):
                changed = inventory_durable_hash(previous["durable_snapshot"]) != candidate_hash
            else:
                # Existing 1.5.254 state has revision=0 but no durable hash.
                # A one-time bump makes the first post-upgrade publication
                # unambiguously newer without relying on volatile fields.
                changed = True
            if changed:
                revision += 1
                candidate["snapshot_revision"] = str(revision)
                candidate_hash = inventory_durable_hash(candidate)

            durable_snapshot = canonical_inventory_content(candidate)
            record = {
                "schema": INVENTORY_STATE_SCHEMA,
                "snapshot_revision": revision,
                "durable_hash": candidate_hash,
                "durable_snapshot": durable_snapshot,
                "last_published": previous.get("last_published"),
                "last_acknowledged": previous.get("last_acknowledged"),
            }
            installations[installation] = record
            state["installations"] = installations
            encoded = json.dumps(state, ensure_ascii=True, sort_keys=True)
            db.execute(
                "INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("commercial_inventory_state_v1", encoded),
            )
            db.execute(
                "INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("commercial_inventory_revision", json.dumps(revision)),
            )
        return candidate, record

    def acknowledge_inventory(self, *, installation_id: str, inventory: Mapping[str, Any]) -> bool:
        """Pin the last successfully published inventory without regressing state."""
        installation = str(installation_id)
        durable_hash = inventory_durable_hash(inventory)
        try:
            revision = int(inventory.get("snapshot_revision") or 0)
        except (TypeError, ValueError):
            return False
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM commercial_meta WHERE key=?", ("commercial_inventory_state_v1",)).fetchone()
            state = self._inventory_state_record(json.loads(row[0]) if row else None)
            record = state["installations"].get(installation)
            try:
                record_revision = int(record.get("snapshot_revision") or -1) if isinstance(record, Mapping) else -1
            except (TypeError, ValueError):
                return False
            if not isinstance(record, Mapping) or str(record.get("durable_hash") or "") != durable_hash or record_revision != revision:
                return False
            next_record = dict(record)
            pin = {"durable_hash": durable_hash, "snapshot_revision": revision}
            next_record["last_published"] = pin
            next_record["last_acknowledged"] = pin
            state["installations"][installation] = next_record
            db.execute(
                "INSERT INTO commercial_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("commercial_inventory_state_v1", json.dumps(state, ensure_ascii=True, sort_keys=True)),
            )
        return True

    @property
    def portal_enrollment_enabled(self) -> bool:
        """Whether the independent Portal enrollment is locally usable.

        This is deliberately unrelated to the corporate MCC configuration or
        profile. The Portal lane has its own credential and host binding.
        """
        return bool(self.get("portal_agent_credential")) and self.get("host_fingerprint") == _host_fingerprint()

    @property
    def commercial_enabled(self) -> bool:
        """Backward-compatible alias for the Portal enrollment state."""
        return self.portal_enrollment_enabled

    def remember_nonce(self, nonce: str, now: float) -> bool:
        with sqlite3.connect(self.path) as db:
            try:
                db.execute("INSERT INTO commercial_nonces(nonce,seen_at) VALUES(?,?)", (nonce, now))
                return True
            except sqlite3.IntegrityError:
                return False

    def next_usage_sequence(self, metric: str) -> int:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM commercial_usage_sequences WHERE metric=?", (metric,)).fetchone()
            value = int(row[0]) + 1 if row else 0
            db.execute("INSERT INTO commercial_usage_sequences(metric,value) VALUES(?,?) ON CONFLICT(metric) DO UPDATE SET value=excluded.value", (metric, value))
        return value


class PortalClient:
    def __init__(self, state: CommercialState, *, timeout_sec: int = 35, activation_transport: Any = None):
        self.state = state
        self.timeout_sec = max(1, min(60, int(timeout_sec)))
        self.activation_transport = activation_transport

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, idempotency_key: str | None = None, extra_headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        base = str(self.state.get("portal_api_base") or "").rstrip("/")
        if not base or not self.state.get("portal_agent_credential"):
            raise RuntimeError("commercial_portal_not_activated")
        _require_https(base)
        body = None if payload is None else canonical_json(payload)
        headers = {"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT, "X-Request-Id": _b64(os.urandom(16)), "User-Agent": PORTAL_USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        headers["User-Agent"] = PORTAL_USER_AGENT
        req = request.Request(base + "/" + path.lstrip("/"), data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise _portal_http_error_from_exception(stage="portal.request", exc=exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise _portal_transport_error(stage="portal.request") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise CommercialPortalError(stage="portal.request", code="commercial_portal_response_too_large")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommercialPortalError(stage="portal.request", code="commercial_portal_response_invalid") from exc

    def ack_job(self, *, job_id: str, installation_id: str, status: str, reason: str | None, idempotency_key: str) -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        payload = {"schema": "portal-mcd-job-ack-v1", "contract_version": CONTRACT_VERSION, "job_id": job_id, "installation_id": installation_id, "status": status, "reason": reason, "idempotency_key": idempotency_key}
        return self._request("POST", f"agents/{agent_id}/jobs/{job_id}/ack", payload, idempotency_key=idempotency_key)

    def pull_job(self, *, installation_id: str, cursor: str = "", wait_seconds: int = 25, limit: int = 1) -> dict[str, Any] | None:
        """Poll the exact outbound-only Portal channel and normalize one command.

        ``limit`` remains accepted for the v2 worker API, but the Stage2 poll
        contract intentionally returns a bounded command list whose processing
        and cursor advancement are controlled by MCD.
        """
        agent_id = str(self.state.get("agent_id") or "")
        request_state = self.state.get("commercial_portal_poll_request")
        if not isinstance(request_state, dict) or str(request_state.get("cursor") or "") != str(cursor or ""):
            request_state = {"request_id": _b64(os.urandom(16)), "idempotency_key": _b64(os.urandom(16)), "cursor": str(cursor or "")}
            self.state.put("commercial_portal_poll_request", request_state)
        binding = {
            "agent_id": agent_id,
            "installation_id": str(installation_id),
            "host_fingerprint": str(self.state.get("host_fingerprint") or _host_fingerprint()),
            "commercial_key_id": str(self.state.get("commercial_key_id") or ""),
        }
        transport = self.state.get("commercial_portal_transport_v1")
        retry_after = float(transport.get("retry_after") or 0.0) if isinstance(transport, dict) else 0.0
        backoff_ms = max(0, min(PORTAL_MCD_MAX_BACKOFF_MS, int(max(0.0, retry_after - time.time()) * 1000)))
        payload = build_portal_mcd_poll_request(
            request_id=str(request_state["request_id"]),
            idempotency_key=str(request_state["idempotency_key"]),
            binding=binding,
            cursor=cursor or None,
            wait_ms=max(0, min(PORTAL_MCD_MAX_LONG_POLL_MS, int(wait_seconds) * 1000)),
            backoff_ms=backoff_ms,
        )
        result = self._request("POST", f"agents/{agent_id}/installations/{installation_id}/poll", payload, idempotency_key=str(request_state["idempotency_key"]))
        try:
            verified = verify_portal_mcd_poll_response(
                result,
                expected_request_id=str(request_state["request_id"]),
                expected_idempotency_key=str(request_state["idempotency_key"]),
                expected_binding=binding,
            )
        except ValueError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,120}", code):
                code = "portal_mcd_poll_response_invalid"
            raise CommercialPortalError(stage="poll.response_validation", code=code, retryable=False) from exc
        self.state.put("commercial_portal_poll_request", None)
        retry_after_ms = int(verified["retry_after_ms"])
        self.state.put("commercial_portal_server_retry_after", time.time() + (retry_after_ms / 1000.0))
        commands = verified["commands"]
        if not commands:
            # Poll/lease is observational. The local ACK cursor advances only
            # after the worker has delivered contiguous command receipts.
            return None
        delivery_cursors = [
            int(command["delivery_cursor"])
            for command in commands
            if isinstance(command.get("delivery_cursor"), int) and not isinstance(command.get("delivery_cursor"), bool)
        ]
        cursor_next = str(delivery_cursors[-1]) if len(delivery_cursors) == len(commands) else verified["next_cursor"]
        return {"cursor": cursor_next, "commands": commands}

    def download_artifact(self, *, path: str, grant: str, expected_size: int) -> bytes:
        base = str(self.state.get("portal_api_base") or "").rstrip("/")
        if not base or not self.state.get("portal_agent_credential"):
            raise RuntimeError("commercial_portal_not_activated")
        _require_https(base)
        expected = int(expected_size)
        if expected < 0 or expected > MAX_ARTIFACT_BYTES:
            raise ValueError("commercial_artifact_size_invalid")
        req = request.Request(base + "/" + path.lstrip("/"), headers={"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT, "X-Portal-Artifact-Grant": grant, "User-Agent": PORTAL_USER_AGENT}, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                body = response.read(expected + 1)
        except HTTPError as exc:
            raise _portal_http_error(stage="artifact.download", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise _portal_transport_error(stage="artifact.download") from exc
        if len(body) != expected:
            raise CommercialPortalError(stage="artifact.download", code="commercial_artifact_size_mismatch")
        return body

    def send_receipt(self, payload: dict[str, Any], *, installation_id: str | None = None) -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        payload = dict(payload)
        payload.pop("agent_signature", None)
        installation = str(installation_id or payload.get("installation_id") or self.state.get("commercial_active_installation_id") or "")
        adopt_schema = {"portal-mcd-command-noop-receipt-v1", "portal-mcd-command-receipt-v1", "portal-mcd-connection-state-v1"}
        if str(payload.get("schema") or "") in adopt_schema:
            if not installation:
                raise RuntimeError("commercial_installation_id_missing")
            path = f"agents/{agent_id}/installations/{installation}/adopt-receipts"
        else:
            path = f"agents/{agent_id}/receipts"
        if payload.get("schema") in {"portal-mcd-generic-stage-receipt-v1", "portal-mcd-generic-terminal-receipt-v1"}:
            idempotency_key = f"{payload.get('job_id')}:{payload.get('sequence')}"
        else:
            idempotency_key = str(payload.get("idempotency_key") or payload.get("job_id") or "")
        return self._request("POST", path, payload, idempotency_key=idempotency_key)

    def send_capability_inventory(self, *, installation_id: str, inventory: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the latest declared generic primitive inventory."""
        agent_id = str(self.state.get("agent_id") or "")
        payload = dict(inventory)
        request_id = str(payload.get("request_id") or "")
        if not agent_id or not installation_id or not request_id:
            raise ValueError("commercial_capability_inventory_identity_missing")
        return self._request(
            "POST",
            f"agents/{agent_id}/installations/{installation_id}/capabilities",
            payload,
            idempotency_key=request_id,
        )

    def download_generic_artifact(self, *, digest: str, expected_size: int) -> bytes:
        """Download a Portal generic artifact using the existing commercial key."""
        normalized = str(digest or "").lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
            raise ValueError("commercial_generic_artifact_digest_invalid")
        expected = int(expected_size)
        if expected <= 0 or expected > MAX_ARTIFACT_BYTES:
            raise ValueError("commercial_generic_artifact_size_invalid")
        agent_id = str(self.state.get("agent_id") or "")
        base = str(self.state.get("portal_api_base") or "").rstrip("/")
        if not agent_id or not base or not self.state.get("portal_agent_credential"):
            raise RuntimeError("commercial_portal_not_activated")
        _require_https(base)
        path = f"self-hosted/artifacts/{quote(normalized, safe='')}"
        headers = {"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT, "X-Content-Digest": normalized, "User-Agent": PORTAL_USER_AGENT}
        req = request.Request(base + "/" + path, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                body = response.read(expected + 1)
        except HTTPError as exc:
            raise _portal_http_error(stage="generic_artifact.download", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise _portal_transport_error(stage="generic_artifact.download") from exc
        if len(body) != expected:
            raise CommercialPortalError(stage="generic_artifact.download", code="commercial_generic_artifact_size_mismatch")
        if hashlib.sha256(body).hexdigest() != normalized[7:]:
            raise CommercialPortalError(stage="generic_artifact.download", code="commercial_generic_artifact_digest_mismatch")
        return body

    def _commercial_binding(self, installation_id: str) -> dict[str, str]:
        return {
            "agent_id": str(self.state.get("agent_id") or ""),
            "installation_id": str(installation_id),
            "host_fingerprint": str(self.state.get("host_fingerprint") or _host_fingerprint()),
            "commercial_key_id": str(self.state.get("commercial_key_id") or ""),
        }

    def adopt(self, *, installation_id: str, inventory: Mapping[str, Any], request_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request_id = str(request_id or _b64(os.urandom(16)))
        idempotency_key = str(idempotency_key or _b64(os.urandom(16)))
        unsigned_inventory = dict(inventory)
        # Adopt/inventory are authenticated by the existing commercial API
        # credential over HTTPS. Agent Ed25519 signatures are deliberately not
        # part of this wire envelope.
        unsigned_inventory.pop("agent_signature", None)
        body = {"schema": "portal-mcd-adopt-request-v1", "contract_version": PORTAL_MCD_ADOPT_CONTRACT_VERSION, "request_id": request_id, "idempotency_key": idempotency_key, "binding": self._commercial_binding(installation_id), "inventory": unsigned_inventory}
        response = self._request("POST", f"agents/{body['binding']['agent_id']}/installations/{installation_id}/adopt", body, idempotency_key=idempotency_key)
        return verify_portal_mcd_attach_response(
            response,
            expected_request_id=request_id,
            expected_idempotency_key=idempotency_key,
            expected_binding=body["binding"],
        )

    def send_inventory(self, *, installation_id: str, inventory: Mapping[str, Any], request_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request_id = str(request_id or inventory.get("request_id") or _b64(os.urandom(16)))
        idempotency_key = str(idempotency_key or request_id)
        unsigned_inventory = dict(inventory)
        unsigned_inventory.pop("agent_signature", None)
        body = {"request_id": request_id, "idempotency_key": idempotency_key, "binding": self._commercial_binding(installation_id), "inventory": unsigned_inventory}
        return self._request("POST", f"agents/{body['binding']['agent_id']}/installations/{installation_id}/inventory", body, idempotency_key=idempotency_key)

    def _activation_request(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.activation_transport is not None:
            return dict(self.activation_transport(base_url, payload))
        _require_https(base_url)
        body = canonical_json(payload)
        headers = {"Content-Type": "application/json", "X-Portal-Contract": CONTRACT, "X-Request-Id": _b64(os.urandom(16)), "Idempotency-Key": payload["idempotency_key"], "User-Agent": PORTAL_USER_AGENT}
        req = request.Request(base_url.rstrip("/") + "/api/v1/commercial/activations/redeem", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise _portal_http_error(stage="activation", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise _portal_transport_error(stage="activation") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise CommercialPortalError(stage="activation", code="commercial_activation_response_too_large")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommercialPortalError(stage="activation", code="commercial_activation_response_invalid") from exc

    def activate(self, base_url: str, activation_code: str, agent_version: str) -> dict[str, Any]:
        _require_https(base_url)
        host_fingerprint = _host_fingerprint()
        machine = platform.machine().lower()
        normalized_platform = "linux-arm64" if machine in {"aarch64", "arm64"} else "linux-amd64" if machine in {"x86_64", "amd64"} else "linux-" + machine
        payload = {"schema": "portal-mcd-activation-request-v1", "contract_version": CONTRACT_VERSION, "activation_code": activation_code, "agent": {"host_fingerprint": host_fingerprint, "agent_version": agent_version, "platform": normalized_platform}, "supported_contract_versions": [CONTRACT_VERSION], "idempotency_key": _b64(os.urandom(16))}
        result = self._activation_request(base_url, payload)
        if result.get("schema") != "portal-mcd-activation-response-v1" or result.get("contract_version") != CONTRACT_VERSION:
            raise CommercialPortalError(stage="activation", code="commercial_activation_response_schema_invalid")
        if not result.get("agent_id") or not result.get("agent_credential") or not isinstance(result.get("portal"), dict):
            raise CommercialPortalError(stage="activation", code="commercial_activation_response_fields_invalid")
        # Portal activation is authenticated by the HTTPS API credential and
        # host binding.  Legacy artifact-trust fields, including signing-key
        # material, are intentionally ignored.  Artifact integrity is checked
        # later by exact size/SHA-256 after authenticated HTTPS download.
        commercial_key = result.get("commercial_key") or result.get("commercial_key_metadata")
        updates: dict[str, Any] = {
            "portal_api_base": base_url.rstrip("/") + "/api/v1/commercial",
            "portal_agent_credential": result["agent_credential"],
            "agent_id": result["agent_id"],
            "host_fingerprint": host_fingerprint,
        }
        if isinstance(commercial_key, dict) and commercial_key.get("commercial_key_id"):
            updates["commercial_key_id"] = str(commercial_key["commercial_key_id"])
            updates["commercial_key_metadata"] = _public_commercial_key_metadata(commercial_key)
        elif result.get("commercial_key_id"):
            updates["commercial_key_id"] = str(result["commercial_key_id"])
        self.state.put_many(updates, delete_keys=("portal_job_signing_keys",))
        return {"status": "activated", "agent_id": result["agent_id"], "contract_version": CONTRACT_VERSION}

    def register_installation(self, *, local_identity: str, display_name: str, mautic_version: str, application_root_fingerprint: str, activation_state: str = "passive") -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        if not agent_id or activation_state not in {"passive", "active"}:
            raise ValueError("commercial_installation_request_invalid")
        from mcd_agent.commercial_capabilities import build_capability_manifest
        payload = {"schema": "portal-mcd-installation-register-request-v1", "contract_version": CONTRACT_VERSION, "installation": {"local_identity": local_identity, "display_name": display_name, "mautic_version": mautic_version, "application_root_fingerprint": application_root_fingerprint, "activation_state": activation_state, "capability_manifest": build_capability_manifest(agent_version=str(self.state.get("agent_version") or "runtime"))}}
        request_fingerprint = hashlib.sha256(canonical_json({k: v for k, v in payload.items() if k != "idempotency_key"})).hexdigest()
        requests = self.state.get("installation_requests") or {}
        previous = requests.get(local_identity) if isinstance(requests, dict) else None
        if isinstance(previous, dict) and previous.get("fingerprint") != request_fingerprint:
            raise ValueError("commercial_installation_idempotency_conflict")
        idempotency_key = str(previous.get("idempotency_key")) if isinstance(previous, dict) else _b64(os.urandom(16))
        payload["idempotency_key"] = idempotency_key
        requests = dict(requests) if isinstance(requests, dict) else {}
        requests[local_identity] = {"fingerprint": request_fingerprint, "idempotency_key": idempotency_key}
        self.state.put("installation_requests", requests)
        result = self._request("POST", f"agents/{agent_id}/installations", payload, idempotency_key=idempotency_key)
        if result.get("schema") != "portal-mcd-installation-register-response-v1" or result.get("contract_version") != CONTRACT_VERSION or not result.get("installation_id"):
            raise RuntimeError("commercial_installation_response_invalid")
        installation_ids = self.state.get("installation_ids")
        installation_ids = dict(installation_ids) if isinstance(installation_ids, dict) else {}
        installation_ids[local_identity] = str(result["installation_id"])
        self.state.put("installation_ids", installation_ids)
        # Keep the operator-supplied Portal identity durable. Inventory
        # publication must not reconstruct it from a host-derived instance
        # UID or hostname after a restart.
        metadata = self.state.get("commercial_installation_metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata[str(local_identity)] = {
            "local_identity": str(local_identity),
            "display_name": str(display_name),
            "mautic_version": str(mautic_version),
            "application_root_fingerprint": str(application_root_fingerprint),
        }
        self.state.put("commercial_installation_metadata", metadata)
        self.state.delete("portal_job_signing_keys")
        return result

    def register_host(self, *, local_identity: str, display_name: str) -> dict[str, Any]:
        """Register the host/MCD binding before any Mautic instance exists."""
        identity = _require_contract_string(local_identity, "commercial_host_identity_invalid")
        name = _require_contract_string(display_name, "commercial_host_display_name_invalid")
        agent_id = _require_contract_string(self.state.get("agent_id"), "commercial_host_agent_missing")
        requests = self.state.get("host_registration_requests")
        requests = dict(requests) if isinstance(requests, Mapping) else {}
        previous = requests.get(identity)
        fingerprint = hashlib.sha256(canonical_json({"local_identity": identity, "display_name": name})).hexdigest()
        if isinstance(previous, Mapping) and str(previous.get("fingerprint") or "") != fingerprint:
            raise ValueError("commercial_host_registration_idempotency_conflict")
        idempotency_key = str(previous.get("idempotency_key")) if isinstance(previous, Mapping) else _b64(os.urandom(16))
        payload = {
            "schema": "portal-mcd-host-register-request-v1",
            "contract_version": CONTRACT_VERSION,
            "registration_kind": "host",
            "idempotency_key": idempotency_key,
            "host": {"local_identity": identity, "display_name": name},
        }
        requests[identity] = {"fingerprint": fingerprint, "idempotency_key": idempotency_key}
        self.state.put("host_registration_requests", requests)
        result = self._request("POST", f"agents/{agent_id}/installations", payload, idempotency_key=idempotency_key)
        installation = result.get("installation")
        connection = result.get("connection")
        if (
            result.get("schema") != "portal-mcd-host-register-response-v1"
            or result.get("contract_version") != CONTRACT_VERSION
            or result.get("registration_kind") != "host"
            or not isinstance(installation, Mapping)
            or not isinstance(connection, Mapping)
            or not installation.get("id")
            or str(installation.get("agent_id") or "") != agent_id
            or str(installation.get("local_identity") or "") != identity
            or str(installation.get("display_name") or "") != name
            or installation.get("status") != "passive"
            or connection.get("status") != "active"
            or connection.get("channel_contract") != CONTRACT_VERSION
            or connection.get("last_inventory_revision") is not None
        ):
            raise RuntimeError("commercial_host_registration_response_invalid")
        installation_id = str(installation["id"])
        installation_ids = self.state.get("installation_ids")
        installation_ids = dict(installation_ids) if isinstance(installation_ids, Mapping) else {}
        existing = installation_ids.get(identity)
        if existing and str(existing) != installation_id:
            raise ValueError("commercial_host_registration_identity_conflict")
        installation_ids[identity] = installation_id
        metadata = self.state.get("commercial_installation_metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metadata[identity] = {
            "registration_kind": "host",
            "local_identity": identity,
            "display_name": name,
        }
        self.state.put_many(
            {
                "installation_ids": installation_ids,
                "commercial_installation_metadata": metadata,
                "host_registration_requests": requests,
            },
            delete_keys=("portal_job_signing_keys",),
        )
        return result

    def send_usage(self, *, installation_id: str, metric: str, observed_at: str, raw_value: int,
                   cadence_policy_seconds: int, evidence_ref: str, sequence: int,
                   sample_id: str, reset_type: str = "none") -> dict[str, Any]:
        """Send one immutable usage sample; retries must reuse sample_id/sequence."""
        agent_id = str(self.state.get("agent_id") or "")
        credential = str(self.state.get("portal_agent_credential") or "")
        if not agent_id or not credential or not installation_id:
            raise RuntimeError("commercial_usage_not_activated")
        if metric not in {"contact_count_current", "confirmed_successful_sends_cumulative"}:
            raise ValueError("commercial_usage_metric_invalid")
        if reset_type not in {"none", "counter_reset", "counter_wrap"}:
            raise ValueError("commercial_usage_reset_type_invalid")
        payload = {"schema": "portal-mcd-usage", "contract_version": 1, "agent_id": agent_id,
                   "installation_id": installation_id, "metric": metric, "observed_at": observed_at,
                   "sequence": max(0, int(sequence)), "sample_id": sample_id,
                   "raw_value": max(0, int(raw_value)), "reset_type": reset_type,
                   "cadence_policy_seconds": max(1, int(cadence_policy_seconds)), "evidence_ref": evidence_ref}
        body = canonical_json(payload)
        signature = "hmac-sha256=" + _b64(hmac.new(credential.encode(), body, hashlib.sha256).digest())
        path = f"agents/{agent_id}/installations/{installation_id}/usage"
        return self._request("POST", path, payload, idempotency_key=sample_id,
                             extra_headers={"X-Portal-Usage-Signature": signature})

