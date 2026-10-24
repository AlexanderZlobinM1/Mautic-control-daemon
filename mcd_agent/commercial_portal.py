"""MCD-owned SalesSnap Portal commercial transport boundary (contract v1)."""

from __future__ import annotations

import base64
import hashlib
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
    digest = str(package.get("sha256") or package.get("digest") or "").lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("commercial_package_digest_invalid")
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
    return {"job_id": str(item["job_id"]), "idempotency_key": str(item["idempotency_key"]), "command": str(item["command"]), "package_id": str(package["package_id"]), "revision": str(package["revision"]), "digest": "sha256:" + digest, "size_bytes": size, "media_type": str(package["media_type"])}


class CommercialState:
    """Small durable Portal state store, isolated from corporate MCC state."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS commercial_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commercial_nonces (nonce TEXT PRIMARY KEY, seen_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commercial_receipts (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
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


class PortalClient:
    def __init__(self, state: CommercialState, *, timeout_sec: int = 35, activation_transport: Any = None):
        self.state = state
        self.timeout_sec = max(1, min(60, int(timeout_sec)))
        self.activation_transport = activation_transport

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, idempotency_key: str | None = None) -> dict[str, Any]:
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
        req = request.Request(base + "/" + path.lstrip("/"), data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout_sec, context=ssl.create_default_context()) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"commercial_portal_http_{exc.code}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("commercial_portal_response_too_large")
        return json.loads(raw.decode("utf-8")) if raw else {}

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

