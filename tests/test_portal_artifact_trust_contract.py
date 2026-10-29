from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mcd_agent.commercial_portal import (
    PORTAL_ARTIFACT_SIGNATURE_DOMAIN,
    CommercialState,
    PortalClient,
    canonical_json,
    portal_artifact_trust_keys,
    validate_portal_artifact_trust,
    validate_portal_artifact_trust_update,
    verify_portal_mcd_poll_response,
    verify_self_hosted_external_artifact,
)
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker, SelfHostedScenarioExecutor


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "portal-artifact-trust-v1.json").read_text())


def _unb64(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _artifact_trust_digest(keys: list[dict[str, object]]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json({
        "schema": "portal-artifact-trust-v1",
        "contract_version": "1.0",
        "trust_domain": "portal_self_host_scenario_artifact",
        "keys": keys,
    })).hexdigest()


def test_portal_fixture_vectors_are_exact_and_domains_are_separate() -> None:
    trust = validate_portal_artifact_trust(FIXTURE["artifact_trust"])
    update = validate_portal_artifact_trust_update(FIXTURE["update_command"])
    assert trust["trust_digest"] == FIXTURE["artifact_trust"]["trust_digest"]
    assert update["trust_digest"] == trust["trust_digest"]
    assert "sha256:" + hashlib.sha256(canonical_json(FIXTURE["artifact_statement"])).hexdigest() == FIXTURE["canonical_sha256"]["artifact_statement"]
    assert "sha256:" + hashlib.sha256(canonical_json(FIXTURE["artifact_trust"])).hexdigest() == FIXTURE["canonical_sha256"]["artifact_trust"]
    assert "sha256:" + hashlib.sha256(canonical_json(FIXTURE["update_command"])).hexdigest() == FIXTURE["canonical_sha256"]["update_command"]
    unsigned_poll = dict(FIXTURE["poll_response"])
    unsigned_poll.pop("portal_signature")
    assert "sha256:" + hashlib.sha256(canonical_json(unsigned_poll)).hexdigest() == FIXTURE["canonical_sha256"]["poll_response_unsigned"]
    statement = canonical_json(FIXTURE["artifact_statement"])
    Ed25519PublicKey.from_public_bytes(_unb64(FIXTURE["artifact_trust"]["keys"][0]["value"])).verify(
        _unb64(FIXTURE["artifact_signature"]["value"]),
        PORTAL_ARTIFACT_SIGNATURE_DOMAIN + statement,
    )
    verified_poll = verify_portal_mcd_poll_response(
        FIXTURE["poll_response"],
        public_keys={FIXTURE["job_signing_public_key"]["key_id"]: FIXTURE["job_signing_public_key"]["value"]},
        expected_request_id="poll-fixture-1",
        expected_idempotency_key="poll-idem-fixture-1",
        expected_binding=FIXTURE["update_command"]["binding"],
    )
    assert verified_poll["commands"][0]["schema"] == "portal-mcd-artifact-trust-update-v1"


def test_activation_ignores_legacy_portal_artifact_trust() -> None:
    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        client = PortalClient(state, activation_transport=lambda *_: {
            "schema": "portal-mcd-activation-response-v1",
            "contract_version": "1.0",
            "agent_id": "agent-fixture-1",
            "agent_credential": "credential-1",
            "portal": {
                "job_signing_keys": [FIXTURE["job_signing_public_key"]],
                "artifact_trust": FIXTURE["artifact_trust"],
                "artifact_signing_keys": FIXTURE["artifact_trust"]["keys"],
            },
        })
        assert client.activate("https://portal.example.test", "activation", "1.5.257")["status"] == "activated"
        assert state.get("portal_artifact_trust_v1") is None
        assert state.get("portal_artifact_signing_keys") is None


def test_trust_update_is_atomic_idempotent_and_rejects_stale_revision() -> None:
    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        command = copy.deepcopy(FIXTURE["update_command"])
        assert state.apply_portal_artifact_trust_update(command)["trust_revision"] == 1
        assert state.apply_portal_artifact_trust_update(command)["idempotent"] is True
        stale = copy.deepcopy(command)
        stale["command_id"] = "stale-command"
        stale["idempotency_key"] = "stale-idempotency"
        stale["keys"][0]["status"] = "revoked"
        stale["trust_digest"] = _artifact_trust_digest(stale["keys"])
        try:
            state.apply_portal_artifact_trust_update(stale)
        except ValueError as exc:
            assert str(exc) == "portal_artifact_trust_revision_conflict"
        else:
            raise AssertionError("same revision with a different digest must fail closed")


def test_worker_applies_signed_poll_update_across_outage_and_restart() -> None:
    class Portal:
        def __init__(self) -> None:
            self.fail_once = True
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_kwargs):
            return {"cursor": "12", "commands": [copy.deepcopy(FIXTURE["update_command"])]}

        def send_receipt(self, receipt, **_kwargs):
            if self.fail_once:
                self.fail_once = False
                raise OSError("outage")
            self.receipts.append(dict(receipt))
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            SelfHostedScenarioExecutor(state),
            installation_id="installation-fixture-1",
            portal_keys={"portal-job-fixture-1": FIXTURE["job_signing_public_key"]["value"]},
            publisher_keys={},
            context={},
            commercial_key_id="commercial-fixture-1",
            agent_id="agent-fixture-1",
            host_fingerprint=FIXTURE["update_command"]["binding"]["host_fingerprint"],
            clock=lambda: 1793059260.0,
        )
        assert worker.run_once()["status"] == "receipt_pending"
        assert state.get("portal_artifact_trust_revision") == 1
        restarted = SelfHostedPortalWorker(
            portal,
            SelfHostedScenarioExecutor(state),
            installation_id="installation-fixture-1",
            portal_keys={"portal-job-fixture-1": FIXTURE["job_signing_public_key"]["value"]},
            publisher_keys={},
            context={},
            commercial_key_id="commercial-fixture-1",
            agent_id="agent-fixture-1",
            host_fingerprint=FIXTURE["update_command"]["binding"]["host_fingerprint"],
            clock=lambda: 1793059260.0,
        )
        result = restarted.run_once()
        assert result["status"] == "succeeded"
        assert state.get("commercial_self_hosted_pending_receipts") == []
        assert state.get("commercial_portal_cursor") == "12"
        assert portal.receipts[-1]["schema"] == "portal-mcd-command-receipt-v1"


def test_trust_update_refreshes_live_worker_and_restart_reloads_for_scenario_verify() -> None:
    from mcd_agent.commercial_portal import _b64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    class Portal:
        def send_receipt(self, receipt, **_kwargs):
            return receipt

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        worker = SelfHostedPortalWorker(
            Portal(),
            SelfHostedScenarioExecutor(state),
            installation_id=FIXTURE["update_command"]["binding"]["installation_id"],
            portal_keys={},
            publisher_keys={},
            context={},
            commercial_key_id=FIXTURE["update_command"]["binding"]["commercial_key_id"],
            agent_id=FIXTURE["update_command"]["binding"]["agent_id"],
            host_fingerprint=FIXTURE["update_command"]["binding"]["host_fingerprint"],
            clock=lambda: 1793059260.0,
        )
        assert worker._run_artifact_trust_update(FIXTURE["update_command"], cursor_next="12")["status"] == "succeeded"
        assert "portal-artifact-fixture-1" in worker.publisher_keys

        private = Ed25519PrivateKey.from_private_bytes(bytes(range(0x40, 0x60)))
        body = b"scenario-after-trust-update"
        digest = hashlib.sha256(body).hexdigest()
        statement = {"kind": "scenario", "revision": "scenario-after-update-r1", "sha256": digest, "media_type": "application/json", "size_bytes": len(body)}
        pin = {"artifact_uri": "https://artifacts.example.test/scenario-after-update-r1", "artifact_digest": "sha256:" + digest, "artifact_size_bytes": len(body), "artifact_media_type": "application/json", "artifact_revision": "scenario-after-update-r1", "signature": {"algorithm": "ed25519", "key_id": "portal-artifact-fixture-1", "value": _b64(private.sign(PORTAL_ARTIFACT_SIGNATURE_DOMAIN + canonical_json(statement)))}}
        assert verify_self_hosted_external_artifact(pin, body, worker.publisher_keys, kind="scenario")["artifact_revision"] == "scenario-after-update-r1"

        restarted_keys = portal_artifact_trust_keys(state.get("portal_artifact_trust_v1"))
        assert verify_self_hosted_external_artifact(pin, body, restarted_keys, kind="scenario")["artifact_revision"] == "scenario-after-update-r1"


def test_portal_artifact_verifier_ignores_legacy_signer_state() -> None:
    body = b"portal-artifact"
    from mcd_agent.commercial_portal import _b64

    private = Ed25519PrivateKey.generate()
    digest = hashlib.sha256(body).hexdigest()
    statement = {"kind": "scenario", "revision": "r1", "sha256": digest, "media_type": "application/json", "size_bytes": len(body)}
    pin = {"artifact_uri": "https://artifacts.example.test/r1", "artifact_digest": "sha256:" + digest, "artifact_size_bytes": len(body), "artifact_media_type": "application/json", "artifact_revision": "r1", "signature": {"algorithm": "ed25519", "key_id": "revoked-1", "value": _b64(private.sign(PORTAL_ARTIFACT_SIGNATURE_DOMAIN + canonical_json(statement)))}}
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    active = {"revoked-1": {"algorithm": "ed25519", "key_id": "revoked-1", "value": _b64(public), "status": "active"}}
    assert verify_self_hosted_external_artifact(pin, body, active, kind="scenario")["artifact_revision"] == "r1"
    assert verify_self_hosted_external_artifact(
        pin,
        body,
        {"revoked-1": {"algorithm": "ed25519", "key_id": "revoked-1", "value": _b64(public), "status": "revoked"}},
        kind="scenario",
    )["artifact_revision"] == "r1"
