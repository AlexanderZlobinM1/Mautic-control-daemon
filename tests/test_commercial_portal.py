import base64
import hashlib
import json
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from mcd_agent.commercial_portal import CommercialState, canonical_json, verify_signed_job


def test_signed_job_is_verified_and_replay_state_is_durable():
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    job = {"job_id": "job-1", "idempotency_key": "idem-1", "agent_id": "agent-1", "installation_id": "inst-1", "command": "package.apply", "issued_at": "2026-10-24T00:00:00Z", "expires_at": "2099-10-24T00:00:00Z", "attempt": 1, "nonce": "nonce-1", "scope": {"agent_id": "agent-1", "installation_id": "inst-1"}, "package": {"digest": "sha256:" + "a" * 64, "size_bytes": 10}, "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1"}}
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

