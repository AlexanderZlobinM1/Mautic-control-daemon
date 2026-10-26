from datetime import datetime, timezone

import pytest

from mcd_agent.commercial_bootstrap_precheck import validate_precheck_receipt, validate_precheck_scenario
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64
from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_bootstrap_precheck import validate_portal_preflight_receipt


def test_precheck_scenario_is_non_mutating_and_bounded():
    value = validate_precheck_scenario({"schema": "mcc-bootstrap-precheck-scenario-v1", "contract_version": "1.0", "ttl_seconds": 900, "checks": ["ssh_auth", "os_support", "dns_exact_server"], "mutates_host": False, "persists_secrets": False, "installs_packages": False, "dns_proof": {"fqdn": "example.test", "nonce": "n1", "cleanup_required": True}})
    assert value["ttl_seconds"] == 900
    with pytest.raises(ValueError, match="mutation_policy"):
        validate_precheck_scenario({**value, "mutates_host": True})


def test_precheck_receipt_requires_binding_ttl_codes_and_cleanup():
    receipt = {"schema": "mcd-bootstrap-precheck-receipt-v1", "contract_version": "1.0", "input_fingerprint": "sha256:input", "host_fingerprint": "sha256:host", "observed_at": "2026-10-25T10:00:00Z", "expires_at": "2026-10-25T10:10:00Z", "status": "PASS", "results": [{"code": "OS_SUPPORTED", "status": "PASS", "remediation": "none"}], "cleanup": {"status": "PASS", "temporary_responder_removed": True}}
    assert validate_precheck_receipt(receipt, input_fingerprint="sha256:input", now=datetime(2026, 10, 25, 10, 1, tzinfo=timezone.utc))["status"] == "PASS"
    with pytest.raises(ValueError, match="cleanup"):
        validate_precheck_receipt({**receipt, "cleanup": {"status": "WARN", "temporary_responder_removed": False}}, input_fingerprint="sha256:input", now=datetime(2026, 10, 25, 10, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="secret"):
        validate_precheck_receipt({**receipt, "results": [{"code": "X", "status": "PASS", "remediation": "password leaked"}]}, input_fingerprint="sha256:input", now=datetime(2026, 10, 25, 10, 1, tzinfo=timezone.utc))


def test_portal_preflight_signature_timestamp_and_policy_are_verified():
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    receipt = {"schema": "portal-self-hosted-preflight-receipt-v1", "contract_version": "1.0", "request_id": "r1", "tuple_key": "t1", "host_fingerprint": "sha256:host", "checked_at": "2026-10-25T10:00:00Z", "observed_at": "2026-10-25T10:00:00Z", "expires_at": "2026-10-25T10:10:00Z", "checks": [{"id": "host", "status": "PASS", "reason": "ok", "remediation": "none"}], "ready": True, "cleanup": "clean", "portal_signature": {"algorithm": "ed25519", "key_id": "k1"}}
    receipt["portal_signature"]["value"] = base64.urlsafe_b64encode(key.sign(canonical_json({k: v for k, v in receipt.items() if k != "portal_signature"}))).rstrip(b"=").decode()
    assert validate_portal_preflight_receipt(receipt, portal_keys={"k1": public}, tuple_key="t1", host_fingerprint="sha256:host", request_id="r1", now=datetime(2026, 10, 25, 10, 1, tzinfo=timezone.utc))["ready"] is True
    with pytest.raises(ValueError, match="signature"):
        validate_portal_preflight_receipt({**receipt, "portal_signature": {**receipt["portal_signature"], "value": "bad"}}, portal_keys={"k1": public}, tuple_key="t1", host_fingerprint="sha256:host", request_id="r1", now=datetime(2026, 10, 25, 10, 1, tzinfo=timezone.utc))
