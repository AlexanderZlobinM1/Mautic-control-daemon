from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import (
    MCC_ARTIFACT_SIGNATURE_DOMAIN,
    SELF_HOSTED_MCD_JOB_SCHEMA,
    SELF_HOSTED_MCD_CONTRACT_VERSION,
    canonical_json,
    verify_self_hosted_external_artifact,
)
from mcd_agent.commercial_self_hosted import (
    SelfHostedPortalWorker,
    SelfHostedScenarioExecutor,
    validate_self_hosted_stage_receipt,
    validate_self_hosted_terminal_receipt,
)
from mcd_agent.commercial_portal import CommercialState


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _artifact(pin_kind: str, revision: str, body: bytes, key: Ed25519PrivateKey) -> dict[str, object]:
    digest = _digest(body)
    statement = {
        "kind": pin_kind,
        "revision": revision,
        "sha256": digest[7:],
        "media_type": "application/json",
        "size_bytes": len(body),
    }
    return {
        "artifact_uri": f"https://artifacts.example.test/{revision}.json",
        "artifact_digest": digest,
        "artifact_size_bytes": len(body),
        "artifact_media_type": "application/json",
        "artifact_revision": revision,
        "signature": {
            "algorithm": "ed25519",
            "key_id": "publisher-1",
            "value": _b64(key.sign(MCC_ARTIFACT_SIGNATURE_DOMAIN + canonical_json(statement))),
        },
    }


def _job(portal: Ed25519PrivateKey, publisher: Ed25519PrivateKey) -> tuple[dict[str, object], bytes, bytes, dict[str, str]]:
    facts = {
        "os_id": "ubuntu",
        "os_version": "24.04",
        "architecture": "amd64",
        "package_manager": "apt-get",
        "capabilities": ["package_manager.query"],
        "target_kind": "existing_server",
    }
    facts_digest = _digest(canonical_json({**facts, "capabilities": sorted(facts["capabilities"])}))
    bootstrap = b"bootstrap-artifact"
    scenario = b"scenario-artifact"
    scenario_pin = _artifact("scenario", "scenario-r1", scenario, publisher)
    job: dict[str, object] = {
        "schema": SELF_HOSTED_MCD_JOB_SCHEMA,
        "contract_version": SELF_HOSTED_MCD_CONTRACT_VERSION,
        "job_id": "job-1",
        "request_id": "request-1",
        "idempotency_key": "request-1:scenario-r1",
        "account_id": "account-1",
        "commercial_key_id": "commercial-key-1",
        "agent_id": "agent-1",
        "installation_id": "installation-1",
        "host_fingerprint": "sha256:" + "b" * 64,
        "scenario": {
            "scenario_id": "portal-ubuntu-24-04",
            "revision": "scenario-r1",
            "mode": "scan",
            "artifact_digest": scenario_pin["artifact_digest"],
        },
        "applicability": {"facts": facts, "facts_digest": facts_digest},
        "bootstrap_artifact": _artifact("bootstrap", "bootstrap-r1", bootstrap, publisher),
        "scenario_artifact": scenario_pin,
        "execution": {"primitive": "scenario.apply", "version": "1.0"},
        "issued_at": "2026-10-26T10:00:00Z",
        "expires_at": "2099-10-26T11:00:00Z",
        "nonce": "nonce-1",
        "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1", "value": ""},
    }
    unsigned = dict(job)
    unsigned.pop("portal_signature")
    job["portal_signature"] = {"algorithm": "ed25519", "key_id": "portal-1", "value": _b64(portal.sign(canonical_json(unsigned)))}
    return job, bootstrap, scenario, {
        "portal-1": _b64(portal.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        "publisher-1": _b64(publisher.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
    }


def test_v2_external_artifact_execution_has_exact_binding_and_no_handlers() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal, publisher)
    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        events: list[bytes] = []
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **kwargs: events.append(kwargs["artifact"]) or {"status": "succeeded", "steps": 2},
        )
        receipts: list[dict[str, object]] = []
        terminal = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
            receipt_sink=receipts.append,
        )
        assert terminal["status"] == "succeeded"
        assert events == [scenario]
        assert [receipt["stage_id"] for receipt in receipts[:2]] == ["scenario_dispatch", "execution"]
        assert receipts[-1]["schema"] == "mcd-self-hosted-terminal-receipt-v2"
        binding = {key: terminal[key] for key in ("job_id", "request_id", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "facts_digest", "scenario")}
        validate_self_hosted_stage_receipt(receipts[0], binding=binding)
        validate_self_hosted_terminal_receipt(terminal, binding=binding)
        replay = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
        )
        assert replay == terminal
        assert events == [scenario]


def test_v2_external_artifact_requires_publisher_trust_only_at_operation_boundary() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal, publisher)
    pin = job["scenario_artifact"]

    for publisher_keys in ({}, {"other-publisher": keys["publisher-1"]}):
        try:
            verify_self_hosted_external_artifact(
                pin,
                scenario,
                publisher_keys,
                kind="scenario",
            )
        except ValueError as exc:
            assert str(exc) == "self_hosted_mcd_artifact_signer_unknown"
        else:
            raise AssertionError("scenario artifact without its publisher key must fail closed")


def test_v2_worker_emits_typed_failed_terminal_for_missing_publisher_trust_and_keeps_polling(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal_key, publisher_key)

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []
            self.polls = 0

        def pull_job(self, **_: object):
            self.polls += 1
            return {"cursor": "cursor-1", "job": job}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **_: (_ for _ in ()).throw(AssertionError("publisher rejection must precede runner")),
        )
        monkeypatch.setattr(
            "mcd_agent.commercial_self_hosted.download_external_artifact",
            lambda uri, expected_size: bootstrap if "bootstrap" in uri else scenario,
        )
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

        assert result == {"status": "failed", "job_id": "job-1"}
        assert portal.polls == 1
        assert portal.receipts[-1]["status"] == "failed"
        assert portal.receipts[-1]["error"]["type"] == "self_hosted_mcd_artifact_signer_unknown"
        assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_v2_status_and_binding_reject_legacy_passed_receipt() -> None:
    binding = {
        "job_id": "job-1",
        "request_id": "request-1",
        "account_id": "account-1",
        "commercial_key_id": "commercial-key-1",
        "agent_id": "agent-1",
        "installation_id": "installation-1",
        "host_fingerprint": "sha256:" + "b" * 64,
        "facts_digest": "sha256:" + "c" * 64,
        "scenario": {"scenario_id": "s", "revision": "r", "mode": "scan", "artifact_digest": "sha256:" + "d" * 64},
    }
    receipt = {**binding, "schema": "mcd-self-hosted-stage-receipt-v2", "contract_version": "2.0", "stage_id": "execution", "sequence": 1, "idempotency_key": "job-1:execution:1", "status": "passed", "observed_at": "2026-10-26T10:00:00Z"}
    try:
        validate_self_hosted_stage_receipt(receipt, binding=binding)
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_stage_receipt_invalid"
    else:
        raise AssertionError("legacy passed status must be rejected")


def test_v2_receipts_queue_in_order_across_portal_outage_and_restart(monkeypatch) -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal, publisher)

    class FakePortal:
        def __init__(self) -> None:
            self.fail_once = True
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return job

        def send_receipt(self, receipt: dict[str, object]):
            if self.fail_once:
                self.fail_once = False
                raise OSError("portal outage")
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        fake = FakePortal()
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **_: {"status": "succeeded"},
        )
        monkeypatch.setattr(
            "mcd_agent.commercial_self_hosted.download_external_artifact",
            lambda uri, expected_size: bootstrap if "bootstrap" in uri else scenario,
        )
        worker = SelfHostedPortalWorker(
            fake,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        pending = state.get("commercial_self_hosted_pending_receipts")
        assert isinstance(pending, list) and pending[0]["stage_id"] == "scenario_dispatch"

        restarted = SelfHostedPortalWorker(
            fake,
            SelfHostedScenarioExecutor(
                state,
                facts_reader=lambda **_: job["applicability"]["facts"],
                runner=lambda **_: (_ for _ in ()).throw(AssertionError("replay executed runner")),
            ),
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )
        restarted.run_once()
        assert state.get("commercial_self_hosted_pending_receipts") == []
        assert [receipt["sequence"] for receipt in fake.receipts[:2]] == [1, 2]
