from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_execution import CommercialPortalWorker, CommercialScenarioExecutor
from mcd_agent.commercial_onboarding import build_onboarding_primitive_registry
from mcd_agent.commercial_portal import CommercialState, canonical_json


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_actual_mcc_fixture_fake_portal_restart_replay_and_valid_grant(tmp_path):
    artifact_path = Path("/Users/azlobin/Documents/Projects/mcc/backend/app/catalog/self-hosted-onboarding/self-hosted-onboarding-v1.json")
    fixture_path = Path("/Users/azlobin/Documents/Projects/mcc/backend/tests/fixtures/self-hosted-onboarding-v1.json")
    if not artifact_path.exists() or not fixture_path.exists():
        return
    artifact = artifact_path.read_bytes()
    fixture = json.loads(fixture_path.read_bytes())
    digest = hashlib.sha256(artifact).hexdigest()
    assert digest == fixture["catalog_item"]["artifact"]["sha256"]
    assert len(artifact) == fixture["catalog_item"]["artifact"]["size_bytes"]
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = fixture["test_public_key"]
    package = {"package_id": fixture["catalog_item"]["id"], "revision": digest, "sha256": digest, "size_bytes": len(artifact), "media_type": "application/json", "kind": "scenario", "applicability": {"mcd_contract": ">=1.0 <2.0"}, "download": {"path": "/artifacts/" + digest, "grant": "grant-1"}, "signature": fixture["catalog_item"]["signature"]}
    job = {"job_id": "job-e2e", "idempotency_key": "idem-e2e", "agent_id": "agent-e2e", "installation_id": "inst-e2e", "command": "scenario.apply", "issued_at": "2026-10-24T00:00:00Z", "expires_at": "2099-10-24T00:00:00Z", "nonce": "nonce-e2e", "scope": {"agent_id": "agent-e2e", "installation_id": "inst-e2e", "allowed_commands": ["scenario.apply"], "artifact_sha256": digest}, "package": package, "portal_signature": {"algorithm": "ed25519", "key_id": "portal-e2e"}}
    job["portal_signature"]["value"] = _b64(portal_key.sign(canonical_json({k: v for k, v in job.items() if k != "portal_signature"})))
    delivery = {"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "cursor": "cursor-1", "job": job}

    class FakePortal:
        def __init__(self):
            self.calls = 0
            self.receipts = []
            self.acks = []
            self.fail_terminal_once = True

        def pull_job(self, **_kwargs):
            self.calls += 1
            return delivery

        def download_artifact(self, *, path, grant, expected_size):
            assert path.endswith(digest) and grant == "grant-1" and expected_size == len(artifact)
            return artifact

        def ack_job(self, **kwargs):
            self.acks.append(kwargs)
            return {}

        def send_receipt(self, payload):
            if payload.get("schema") == "mcd-onboarding-terminal-receipt-v1" and self.fail_terminal_once:
                self.fail_terminal_once = False
                raise OSError("Portal outage")
            self.receipts.append(payload)
            return {}

    stages = json.loads(artifact)["stages"]
    events = []
    implementations = {str(stage["kind"]): (lambda envelope, kind=str(stage["kind"]): events.append(kind) or {"status": "passive", "kind": kind}) for stage in stages}
    handlers = build_onboarding_primitive_registry(implementations)
    with_tmp = CommercialState(str(tmp_path / "commercial.sqlite"))
    executor = CommercialScenarioExecutor(with_tmp, stage_handlers=handlers)
    fake = FakePortal()
    worker = CommercialPortalWorker(fake, executor, installation_id="inst-e2e", portal_keys={"portal-e2e": _b64(portal_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))}, publisher_keys={fixture["catalog_item"]["signature"]["key_id"]: publisher_key}, context={"platform": "linux-amd64", "cron_ownership": "none"}, capabilities=set(json.loads(artifact)["compatibility"]["mcd_required_capabilities"]))
    assert worker.run_once()["status"] == "receipt_pending"
    assert worker.run_once()["status"] == "succeeded"
    assert len(events) == len(stages)
    assert len([receipt for receipt in fake.receipts if receipt["schema"] == "mcd-onboarding-stage-receipt-v1"]) == len(stages)
    assert all(ack["status"] == "accepted" for ack in fake.acks)

    bad_delivery = json.loads(json.dumps(delivery))
    del bad_delivery["job"]["package"]["download"]["grant"]

    class BadGrantPortal(FakePortal):
        def pull_job(self, **_kwargs):
            return bad_delivery

    bad_state = CommercialState(str(tmp_path / "bad.sqlite"))
    bad_executor = CommercialScenarioExecutor(bad_state, stage_handlers=handlers)
    bad_fake = BadGrantPortal()
    bad_worker = CommercialPortalWorker(bad_fake, bad_executor, installation_id="inst-e2e", portal_keys={"portal-e2e": _b64(portal_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))}, publisher_keys={fixture["catalog_item"]["signature"]["key_id"]: publisher_key}, context={"platform": "linux-amd64", "cron_ownership": "none"}, capabilities=set(json.loads(artifact)["compatibility"]["mcd_required_capabilities"]))
    assert bad_worker.run_once()["status"] == "rejected"
    assert not bad_executor._load("job-e2e")
