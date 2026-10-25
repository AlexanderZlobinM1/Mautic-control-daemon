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
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

CONTRACT = "com.sales-snap.portal-commercial/1.0"
CONTRACT_VERSION = "1.0"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _host_fingerprint() -> str:
    return "sha256:" + hashlib.sha256(socket.gethostname().encode()).hexdigest()


def _without_signature(job: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result.pop("portal_signature", None)
    return result


def _require_https(url: str) -> None:
    if not str(url).lower().startswith("https://"):
        raise ValueError("commercial_portal_https_required")


def verify_signed_job(job: Mapping[str, Any], public_keys: Mapping[str, str], *, now: float | None = None) -> dict[str, Any]:
    """Validate the immutable Portal job envelope before any executor call."""
    import time

    if job.get("schema") != "portal-mcd-job-delivery-v1" or job.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("commercial_job_schema_unsupported")
    item = job.get("job")
    if not isinstance(item, dict):
        raise ValueError("commercial_job_missing")
    required = ("job_id", "idempotency_key", "agent_id", "installation_id", "command", "issued_at", "expires_at", "nonce", "scope", "package", "portal_signature")
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
    signature = item["portal_signature"]
    if not isinstance(scope, dict) or not isinstance(package, dict) or not isinstance(signature, dict):
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
    key_id = str(signature.get("key_id") or "")
    key = public_keys.get(key_id)
    if not key or signature.get("algorithm") != "ed25519":
        raise ValueError("commercial_job_signer_unknown")
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(key)).verify(_unb64(str(signature["value"])), canonical_json(_without_signature(item)))
    except Exception as exc:
        raise ValueError("commercial_job_signature_invalid") from exc
    applicability = package.get("applicability")
    if not isinstance(applicability, dict):
        raise ValueError("commercial_package_applicability_missing")
    return {"job_id": str(item["job_id"]), "idempotency_key": str(item["idempotency_key"]), "command": str(item["command"]), "package_id": str(package["package_id"]), "revision": str(package["revision"]), "digest": "sha256:" + digest, "size_bytes": size, "media_type": str(package["media_type"]), "applicability": applicability}


def verify_mcc_artifact(job: Mapping[str, Any], artifact: bytes, publisher_keys: Mapping[str, str]) -> dict[str, Any]:
    """Verify exact Portal-delivered bytes against the MCC publisher statement."""
    item = job.get("job") if isinstance(job.get("job"), dict) else job
    package = item.get("package") if isinstance(item, dict) else None
    if not isinstance(package, dict):
        raise ValueError("commercial_package_missing")
    digest = str(package.get("sha256") or "").lower().removeprefix("sha256:")
    if len(digest) != 64 or hashlib.sha256(artifact).hexdigest() != digest:
        raise ValueError("commercial_artifact_digest_invalid")
    if len(artifact) != int(package.get("size_bytes") or -1):
        raise ValueError("commercial_artifact_size_invalid")
    signature = package.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise ValueError("commercial_artifact_signature_missing")
    key = publisher_keys.get(str(signature.get("key_id") or ""))
    kind = str(package.get("kind") or "scenario")
    statement = {"kind": kind, "revision": str(package.get("revision") or ""), "sha256": digest, "media_type": str(package.get("media_type") or ""), "size_bytes": int(package.get("size_bytes") or 0)}
    signed = b"com.sales-snap.portal-mcc/artifact-signature/1.0\n" + canonical_json(statement) + b"\n"
    if not key:
        raise ValueError("commercial_artifact_signer_unknown")
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(key)).verify(_unb64(str(signature.get("value") or "")), signed)
    except Exception as exc:
        raise ValueError("commercial_artifact_signature_invalid") from exc
    return {"kind": kind, "revision": str(package["revision"]), "sha256": "sha256:" + digest, "size_bytes": len(artifact), "media_type": str(package["media_type"])}


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

    @property
    def commercial_enabled(self) -> bool:
        return bool(self.get("portal_agent_credential")) and self.get("host_fingerprint") == _host_fingerprint()

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
        headers = {"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT, "X-Request-Id": _b64(os.urandom(16))}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        req = request.Request(base + "/" + path.lstrip("/"), data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"commercial_portal_http_{exc.code}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("commercial_portal_response_too_large")
        return json.loads(raw.decode("utf-8")) if raw else {}

    def ack_job(self, *, job_id: str, installation_id: str, status: str, reason: str | None, idempotency_key: str) -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        payload = {"schema": "portal-mcd-job-ack-v1", "contract_version": CONTRACT_VERSION, "job_id": job_id, "installation_id": installation_id, "status": status, "reason": reason, "idempotency_key": idempotency_key}
        return self._request("POST", f"agents/{agent_id}/jobs/{job_id}/ack", payload, idempotency_key=idempotency_key)

    def pull_job(self, *, installation_id: str, cursor: str = "", wait_seconds: int = 25, limit: int = 1) -> dict[str, Any] | None:
        agent_id = str(self.state.get("agent_id") or "")
        wait = max(0, min(30, int(wait_seconds)))
        bounded_limit = max(1, min(20, int(limit)))
        query = f"?installation_id={quote(installation_id)}&wait_seconds={wait}&limit={bounded_limit}"
        if cursor:
            query += "&cursor=" + quote(cursor)
        base = str(self.state.get("portal_api_base") or "").rstrip("/")
        if not base:
            raise RuntimeError("commercial_portal_not_activated")
        _require_https(base)
        req = request.Request(base + f"/agents/{agent_id}/jobs" + query, headers={"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT}, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                if int(getattr(response, "status", 200)) == 204:
                    return None
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"commercial_portal_http_{exc.code}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("commercial_portal_response_too_large")
        result = json.loads(raw.decode("utf-8")) if raw else {}
        if result.get("schema") != "portal-mcd-job-delivery-v1" or result.get("contract_version") != CONTRACT_VERSION:
            raise RuntimeError("commercial_job_delivery_schema_invalid")
        return result

    def download_artifact(self, *, path: str, grant: str, expected_size: int) -> bytes:
        base = str(self.state.get("portal_api_base") or "").rstrip("/")
        _require_https(base)
        expected = int(expected_size)
        if expected < 0 or expected > MAX_ARTIFACT_BYTES:
            raise ValueError("commercial_artifact_size_invalid")
        req = request.Request(base + "/" + path.lstrip("/"), headers={"Authorization": "Bearer " + str(self.state.get("portal_agent_credential")), "X-Portal-Contract": CONTRACT, "X-Portal-Artifact-Grant": grant}, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                body = response.read(expected + 1)
        except HTTPError as exc:
            raise RuntimeError(f"commercial_artifact_http_{exc.code}") from exc
        if len(body) != expected:
            raise RuntimeError("commercial_artifact_size_mismatch")
        return body

    def send_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        private = Ed25519PrivateKey.from_private_bytes(_unb64(str(self.state.get("agent_private_key") or "")))
        signed = dict(payload)
        signed.pop("agent_signature", None)
        payload = dict(payload)
        payload["agent_signature"] = {"algorithm": "ed25519", "key_id": str(self.state.get("agent_key_id") or ""), "value": _b64(private.sign(canonical_json(signed)))}
        return self._request("POST", f"agents/{agent_id}/receipts", payload, idempotency_key=str(payload.get("idempotency_key") or payload.get("job_id") or ""))

    def _activation_request(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.activation_transport is not None:
            return dict(self.activation_transport(base_url, payload))
        _require_https(base_url)
        body = canonical_json(payload)
        headers = {"Content-Type": "application/json", "X-Portal-Contract": CONTRACT, "X-Request-Id": _b64(os.urandom(16)), "Idempotency-Key": payload["idempotency_key"]}
        req = request.Request(base_url.rstrip("/") + "/api/v1/commercial/activations/redeem", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"commercial_activation_http_{exc.code}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("commercial_activation_response_too_large")
        return json.loads(raw.decode("utf-8")) if raw else {}

    def activate(self, base_url: str, activation_code: str, agent_version: str) -> dict[str, Any]:
        _require_https(base_url)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        host_fingerprint = _host_fingerprint()
        machine = platform.machine().lower()
        normalized_platform = "linux-arm64" if machine in {"aarch64", "arm64"} else "linux-amd64" if machine in {"x86_64", "amd64"} else "linux-" + machine
        payload = {"schema": "portal-mcd-activation-request-v1", "contract_version": CONTRACT_VERSION, "activation_code": activation_code, "agent": {"public_key": {"algorithm": "ed25519", "key_id": "agent-" + _b64(public)[:22], "value": _b64(public)}, "host_fingerprint": host_fingerprint, "agent_version": agent_version, "platform": normalized_platform}, "supported_contract_versions": [CONTRACT_VERSION], "idempotency_key": _b64(os.urandom(16))}
        result = self._activation_request(base_url, payload)
        if result.get("schema") != "portal-mcd-activation-response-v1" or result.get("contract_version") != CONTRACT_VERSION:
            raise RuntimeError("commercial_activation_response_schema_invalid")
        if not result.get("agent_id") or not result.get("agent_credential") or not isinstance(result.get("portal"), dict):
            raise RuntimeError("commercial_activation_response_fields_invalid")
        signing_keys = result["portal"].get("job_signing_keys")
        if not isinstance(signing_keys, list) or any(not isinstance(item, dict) or not item.get("key_id") or item.get("algorithm") != "ed25519" or not item.get("value") for item in signing_keys):
            raise RuntimeError("commercial_activation_signing_keys_invalid")
        self.state.put("portal_api_base", base_url.rstrip("/") + "/api/v1/commercial")
        self.state.put("portal_agent_credential", result["agent_credential"])
        self.state.put("agent_id", result["agent_id"])
        self.state.put("host_fingerprint", host_fingerprint)
        self.state.put("agent_private_key", _b64(private))
        self.state.put("agent_key_id", payload["agent"]["public_key"]["key_id"])
        self.state.put("portal_job_signing_keys", {x["key_id"]: x["value"] for x in signing_keys})
        return {"status": "activated", "agent_id": result["agent_id"], "contract_version": CONTRACT_VERSION}

    def register_installation(self, *, local_identity: str, display_name: str, mautic_version: str, application_root_fingerprint: str, activation_state: str = "passive") -> dict[str, Any]:
        agent_id = str(self.state.get("agent_id") or "")
        if not agent_id or activation_state not in {"passive", "active"}:
            raise ValueError("commercial_installation_request_invalid")
        payload = {"schema": "portal-mcd-installation-register-request-v1", "contract_version": CONTRACT_VERSION, "installation": {"local_identity": local_identity, "display_name": display_name, "mautic_version": mautic_version, "application_root_fingerprint": application_root_fingerprint, "activation_state": activation_state}}
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

