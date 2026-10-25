from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from mcd_agent.commercial_execution import CommercialPortalWorker, CommercialScenarioExecutor, validate_onboarding_stage_receipt, validate_onboarding_terminal_receipt
from mcd_agent.commercial_portal import CommercialState, canonical_json


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_generic_scenario_checkpoints_and_emits_signed_receipt_payload():
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    portal_public = _b64(portal.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    publisher_public = _b64(publisher.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    artifact = json.dumps({"schema": "portal-mcd-scenario-v1", "contract_version": "1.0", "stages": [{"stage_id": "one", "handler": "fixture"}, {"stage_id": "two", "handler": "fixture"}]} , separators=(",", ":")).encode()
    digest = hashlib.sha256(artifact).hexdigest()
    statement = {"kind": "scenario", "revision": "r1", "sha256": digest, "media_type": "application/json", "size_bytes": len(artifact)}
    package = {"package_id": "scenario-1", "revision": "r1", "sha256": digest, "size_bytes": len(artifact), "media_type": "application/json", "kind": "scenario", "applicability": {"mcd_contract": ">=1.0 <2.0"}, "signature": {"algorithm": "ed25519", "key_id": "mcc-1", "value": _b64(publisher.sign(b"com.sales-snap.portal-mcc/artifact-signature/1.0\n" + canonical_json(statement) + b"\n"))}}
    job = {"job_id": "j1", "idempotency_key": "i1", "agent_id": "a1", "installation_id": "in1", "command": "scenario.apply", "issued_at": "2026-10-24T00:00:00Z", "expires_at": "2099-10-24T00:00:00Z", "nonce": "n1", "scope": {"agent_id": "a1", "installation_id": "in1"}, "package": package, "portal_signature": {"algorithm": "ed25519", "key_id": "p1"}}
    job["portal_signature"]["value"] = _b64(portal.sign(canonical_json({k: v for k, v in job.items() if k != "portal_signature"})))
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        events = []
        runner = CommercialScenarioExecutor(state, stage_handlers={"fixture": lambda value: events.append(value["stage"]["stage_id"]) or "ok"})
        receipt = runner.run({"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": job}, portal_keys={"p1": portal_public}, publisher_keys={"mcc-1": publisher_public}, artifact=artifact)
        assert receipt["status"] == "succeeded"
        assert events == ["one", "two"]
        assert runner.run({"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": job}, portal_keys={"p1": portal_public}, publisher_keys={"mcc-1": publisher_public}, artifact=artifact)["status"] == "succeeded"
        assert events == ["one", "two"]


def test_unknown_stage_fails_closed_before_handler():
    with tempfile.TemporaryDirectory() as td:
        state = CommercialState(str(Path(td) / "state.sqlite"))
        runner = CommercialScenarioExecutor(state, stage_handlers={})
        with pytest.raises(ValueError, match="commercial_job_fields_missing"):
            runner.run({"schema": "portal-mcd-job-delivery-v1", "contract_version": "1.0", "job": {}}, portal_keys={}, publisher_keys={}, artifact=b"{}")


def test_receipt_schema_validation_is_strict():
    validate_onboarding_stage_receipt({"schema": "mcd-onboarding-stage-receipt-v1", "contract_version": "1.0", "job_id": "j", "stage_id": "s", "sequence": 1, "status": "succeeded"}, job_id="j", stage_id="s", sequence=1)
    validate_onboarding_terminal_receipt({"schema": "mcd-onboarding-terminal-receipt-v1", "contract_version": "1.0", "job_id": "j", "status": "succeeded"}, job_id="j")
    with pytest.raises(ValueError, match="commercial_stage_receipt_schema_invalid"):
        validate_onboarding_stage_receipt({"schema": "wrong"}, job_id="j", stage_id="s", sequence=1)
