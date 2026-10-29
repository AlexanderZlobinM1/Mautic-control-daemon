import base64
import hashlib
import io
import json
import tempfile
import time
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import shutil
import hmac
from urllib.error import HTTPError, URLError

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from mcd_agent.commercial_portal import CommercialPortalError, CommercialState, PORTAL_USER_AGENT, PortalClient, canonical_json, download_external_artifact, verify_signed_job


def test_signed_job_is_verified_and_replay_state_is_durable():
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    job = {"job_id": "job-1", "idempotency_key": "idem-1", "agent_id": "agent-1", "installation_id": "inst-1", "command": "package.apply", "issued_at": "2026-10-24T00:00:00Z", "expires_at": "2099-10-24T00:00:00Z", "attempt": 1, "nonce": "nonce-1", "scope": {"agent_id": "agent-1", "installation_id": "inst-1"}, "package": {"package_id": "pkg-1", "revision": "rev-1", "sha256": "a" * 64, "size_bytes": 10, "media_type": "application/octet-stream", "applicability": {"mcd_contract": ">=1.0 <2.0"}}, "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1"}}
    job["portal_signature"]["value"] = base64.urlsafe_b64encode(key.sign(canonical_json({k: v for k, v in job.items() if k != "portal_signature"}))).rstrip(b"=").decode()
    envelope = {"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": job}
    assert verify_signed_job(envelope, {"portal-1": public})["job_id"] == "job-1"
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        assert state.remember_nonce("nonce-1", time.time())
        assert not state.remember_nonce("nonce-1", time.time())


def test_unknown_signer_and_scope_mismatch_are_rejected():
    envelope = {"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": {"job_id": "j", "scope": {}, "package": {"digest": "sha256:" + "a" * 64, "size_bytes": 1}}}
    with pytest.raises(ValueError, match="commercial_job_fields_missing"):
        verify_signed_job(envelope, {})


def test_signed_job_time_boundaries_fail_closed():
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    job = {"job_id": "job-time", "idempotency_key": "idem-time", "agent_id": "agent-1", "installation_id": "inst-1", "command": "package.apply", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T00:01:00Z", "nonce": "nonce-time", "scope": {"agent_id": "agent-1", "installation_id": "inst-1"}, "package": {"package_id": "pkg-1", "revision": "rev-1", "sha256": "a" * 64, "size_bytes": 1, "media_type": "application/octet-stream", "applicability": {"mcd_contract": ">=1.0 <2.0"}}, "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1"}}
    job["portal_signature"]["value"] = base64.urlsafe_b64encode(key.sign(canonical_json({k: v for k, v in job.items() if k != "portal_signature"}))).rstrip(b"=").decode()
    envelope = {"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": job}
    with pytest.raises(ValueError, match="commercial_job_expired_or_not_yet_valid"):
        verify_signed_job(envelope, {"portal-1": public}, now=1_767_225_660)


def test_activation_uses_only_exact_unauthenticated_path_and_stores_validated_state():
    received = []
    activation_payload = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, dict(self.headers)))
            body = json.dumps({
                "schema": "portal-mcd-activation-response-v1",
                "contract_version": "1.0",
                "agent_id": "agent-1",
                "agent_credential": "credential-1",
                "portal": {"job_signing_keys": [{"algorithm": "ed25519", "key_id": "portal-1", "value": "a" * 43}]},
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            def activation_transport(_base, payload):
                activation_payload.update(payload)
                return {
                "schema": "portal-mcd-activation-response-v1", "contract_version": "1.0",
                "agent_id": "agent-1", "agent_credential": "credential-1",
                "portal": {"job_signing_keys": [{"algorithm": "ed25519", "key_id": "portal-1", "value": "a" * 43}]},
                }
            client = PortalClient(state, activation_transport=activation_transport)
            result = client.activate(f"https://127.0.0.1:{server.server_port}", "activation", "1.5.200")
            assert result["status"] == "activated"
            assert "public_key" not in activation_payload["agent"]
            assert state.get("agent_private_key") is None
            assert state.get("agent_key_id") is None
            assert state.commercial_enabled
            assert not received
            with pytest.raises(RuntimeError, match="commercial_portal_not_activated"):
                CommercialState(str(Path(td) / "other.sqlite"))
                PortalClient(CommercialState(str(Path(td) / "other.sqlite")))._request("GET", "arbitrary")
    finally:
        server.shutdown()


def test_all_portal_http_requests_use_product_user_agent(monkeypatch, tmp_path):
    requests = []

    class Response:
        def __init__(self, body=b"{}"):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(req, **_kwargs):
        requests.append(req)
        return Response()

    monkeypatch.setattr("mcd_agent.commercial_portal.request.urlopen", fake_urlopen)
    state = CommercialState(str(tmp_path / "state.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("portal_agent_credential", "credential")
    client = PortalClient(state)
    client._request("GET", "jobs", extra_headers={"User-Agent": "spoofed-client"})
    client.download_artifact(path="artifacts/a", grant="grant", expected_size=2)
    client._activation_request("https://portal.example", {"idempotency_key": "activation-id"})
    download_external_artifact("https://portal.example/artifacts/a", expected_size=2)

    assert len(requests) == 4
    assert all(req.get_header("User-agent") == PORTAL_USER_AGENT for req in requests)


def test_authenticated_artifact_download_requires_api_key(tmp_path):
    state = CommercialState(str(tmp_path / "state.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    client = PortalClient(state)
    with pytest.raises(RuntimeError, match="commercial_portal_not_activated"):
        client.download_artifact(path="artifacts/a", grant="grant", expected_size=2)

    state.put("portal_agent_credential", "credential")


def test_inventory_and_adopt_use_api_key_envelope_without_agent_signature(monkeypatch, tmp_path):
    state = CommercialState(str(tmp_path / "state.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("portal_agent_credential", "commercial-api-key")
    state.put("agent_id", "agent-1")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("commercial_key_id", "commercial-key-1")
    captured = []
    client = PortalClient(state)

    def fake_request(method, path, payload, idempotency_key):
        captured.append((method, path, payload, idempotency_key))
        return {"status": "attached"}

    client._request = fake_request
    monkeypatch.setattr("mcd_agent.commercial_portal.verify_portal_mcd_attach_response", lambda response, **_kwargs: response)
    inventory = {
        "schema": "portal-mcd-inventory-snapshot-v1",
        "contract_version": "1.0",
        "agent_signature": {"algorithm": "ed25519", "key_id": "legacy", "value": "legacy"},
        "binding": {"agent_id": "agent-1", "installation_id": "installation-1", "host_fingerprint": "sha256:" + "a" * 64, "commercial_key_id": "commercial-key-1"},
    }

    client.adopt(installation_id="installation-1", inventory=inventory, request_id="request-1", idempotency_key="adopt-1")
    client.send_inventory(installation_id="installation-1", inventory=inventory, request_id="request-1", idempotency_key="inventory-1")

    assert len(captured) == 2
    assert all("agent_signature" not in item[2]["inventory"] for item in captured)
    assert captured[0][2]["binding"] == inventory["binding"]
    assert captured[1][2]["binding"] == inventory["binding"]


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_status", "retryable"),
    [
        (HTTPError("https://portal.example/redeem", 403, "blocked", {}, io.BytesIO(b"activation-secret")), "commercial_activation_http_403", 403, False),
        (HTTPError("https://portal.example/redeem", 502, "bad gateway", {}, io.BytesIO(b"secret-response")), "commercial_activation_http_502", 502, True),
        (URLError("TLS handshake failed with secret-response"), "commercial_portal_transport_error", None, True),
    ],
)
def test_activation_failures_are_typed_and_secret_free(monkeypatch, tmp_path, failure, expected_code, expected_status, retryable):
    def fail_urlopen(_req, **_kwargs):
        raise failure

    monkeypatch.setattr("mcd_agent.commercial_portal.request.urlopen", fail_urlopen)
    state = CommercialState(str(tmp_path / "state.sqlite"))
    client = PortalClient(state)

    with pytest.raises(CommercialPortalError) as raised:
        client._activation_request("https://portal.example", {"idempotency_key": "activation-id"})

    error = raised.value
    assert error.as_dict() == {
        "stage": "activation",
        "code": expected_code,
        "retryable": retryable,
        **({"http_status": expected_status} if expected_status is not None else {}),
    }
    assert "activation-secret" not in str(error)
    assert "secret-response" not in str(error)




def test_https_and_strict_z_timestamps_are_required():
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        state.put("portal_agent_credential", "credential")
        state.put("portal_api_base", "http://portal.invalid/api/v1/commercial")
        with pytest.raises(ValueError, match="commercial_portal_https_required"):
            PortalClient(state)._request("GET", "jobs")

    with pytest.raises(ValueError, match="commercial_job_time_invalid"):
        verify_signed_job({"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": {"job_id": "j", "idempotency_key": "i", "agent_id": "a", "installation_id": "i", "command": "package.apply", "issued_at": "2026-01-01T00:00:00+00:00", "expires_at": "2026-01-01T00:01:00Z", "nonce": "n", "scope": {"x": 1}, "package": {"x": 1}, "portal_signature": {"x": 1}}}, {})


def test_snapshot_restore_on_new_host_cannot_reuse_portal_identity(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "source.sqlite"
        clone = Path(td) / "clone.sqlite"
        original = CommercialState(str(source))
        original.put("portal_agent_credential", "credential-1")
        original.put("agent_id", "agent-1")
        original.put("host_fingerprint", "sha256:" + "a" * 64)
        shutil.copy2(source, clone)
        monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: "sha256:" + "b" * 64)
        restored = CommercialState(str(clone))
        assert not restored.commercial_enabled


def test_installation_registration_defaults_to_passive_and_binds_agent():
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        state.put("portal_api_base", "https://portal.invalid/api/v1/commercial")
        state.put("portal_agent_credential", "credential")
        state.put("agent_id", "agent-1")
        captured = {}
        client = PortalClient(state)
        client._request = lambda method, path, payload, idempotency_key: captured.update({"method": method, "path": path, "payload": payload, "idempotency_key": idempotency_key}) or {"schema": "portal-mcd-installation-register-response-v1", "contract_version": "1.0", "installation_id": "inst-1", "activation_state": "passive"}
        result = client.register_installation(local_identity="local-1", display_name="Tenant", mautic_version="7.2.1", application_root_fingerprint="sha256:" + "a" * 64)
        assert result["installation_id"] == "inst-1"
        assert captured["path"] == "agents/agent-1/installations"
        assert captured["payload"]["installation"]["activation_state"] == "passive"
        assert state.get("commercial_installation_metadata") == {
            "local-1": {
                "local_identity": "local-1",
                "display_name": "Tenant",
                "mautic_version": "7.2.1",
                "application_root_fingerprint": "sha256:" + "a" * 64,
            }
        }
        first_key = captured["idempotency_key"]
        client.register_installation(local_identity="local-1", display_name="Tenant", mautic_version="7.2.1", application_root_fingerprint="sha256:" + "a" * 64)
        assert captured["idempotency_key"] == first_key
        with pytest.raises(ValueError, match="commercial_installation_idempotency_conflict"):
            client.register_installation(local_identity="local-1", display_name="Changed", mautic_version="7.2.1", application_root_fingerprint="sha256:" + "a" * 64)


def test_usage_contract_signs_exact_payload_and_reuses_sample_id():
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        state.put("portal_api_base", "https://portal.invalid/api/v1/commercial")
        state.put("portal_agent_credential", "credential")
        state.put("agent_id", "agent-1")
        captured = {}
        client = PortalClient(state)
        def fake_request(method, path, payload, idempotency_key, extra_headers):
            captured.update(locals())
            assert extra_headers["X-Portal-Usage-Signature"].startswith("hmac-sha256=")
            expected = hmac.new(b"credential", canonical_json(payload), hashlib.sha256).digest()
            actual = extra_headers["X-Portal-Usage-Signature"].split("=", 1)[1]
            assert base64.urlsafe_b64decode(actual + "=" * (-len(actual) % 4)) == expected
            return {"schema": "portal-mcd-usage-ack-v1", "accepted": True}
        client._request = fake_request
        result = client.send_usage(installation_id="inst-1", metric="contact_count_current", observed_at="2026-10-24T12:00:00Z", raw_value=17, cadence_policy_seconds=60, evidence_ref="usage-1", sequence=0, sample_id="sample-1")
        assert result["accepted"] is True
        assert captured["path"] == "agents/agent-1/installations/inst-1/usage"
        assert captured["idempotency_key"] == "sample-1"

