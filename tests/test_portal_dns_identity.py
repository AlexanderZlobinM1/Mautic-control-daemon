import copy
import hashlib
from pathlib import Path

import pytest

from mcd_agent.commercial_portal import CommercialState, PortalClient, canonical_json, verify_self_hosted_mcd_job
from mcd_agent.commercial_registration import import_activation_material
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker, SelfHostedScenarioExecutor
from mcd_agent.portal_identity import identity_coordinate


DNS = "portable.example.invalid"


def material():
    return {"schema": "mcd-portal-enrollment-material-v1", "contract_version": "1.0", "identity_kind": "dns", "local_identity": DNS, "account_id": "account-1", "agent_id": "agent-1", "commercial_key_id": "key-1", "portal_agent_credential": "fixture-existing-key", "portal_api_base": "https://portal.example.com/api/v1/commercial", "installation_ids": {DNS: "installation-1"}}


def test_dns_import_and_replay_never_compute_host_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("mcd_agent.commercial_registration._host_fingerprint", lambda: pytest.fail("host hash called"))
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: pytest.fail("host hash called"))
    state = CommercialState(str(tmp_path / "state.db"))
    first = import_activation_material(state, material())
    assert first["idempotent"] is False
    assert state.portal_enrollment_enabled
    assert state.get("host_fingerprint") is None
    assert "host_fingerprint" not in first and "material_digest" not in first
    assert import_activation_material(state, material())["idempotent"] is True
    wrong = {**material(), "local_identity": "other.example.invalid"}
    with pytest.raises(ValueError):
        import_activation_material(state, wrong)
    assert state.get("local_identity") == DNS


@pytest.mark.parametrize("extra", [{"host_fingerprint": "sha256:" + "a" * 64}, {"identity_kind": "unknown"}, {"local_identity": "name@host"}])
def test_dns_identity_rejects_mixed_or_unknown_coordinates(extra):
    with pytest.raises(ValueError):
        identity_coordinate({"identity_kind": "dns", "local_identity": DNS, **extra})


def test_new_import_does_not_convert_legacy_state(tmp_path):
    state = CommercialState(str(tmp_path / "state.db"))
    state.put("agent_id", "legacy-agent")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="legacy_binding_preserved"):
        import_activation_material(state, material())
    assert state.get("agent_id") == "legacy-agent"
    assert state.get("identity_kind") is None


def test_dns_poll_inventory_and_connection_receipts_have_no_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: pytest.fail("host hash called"))
    state = CommercialState(str(tmp_path / "state.db"))
    import_activation_material(state, material())
    client = PortalClient(state)
    sent = []
    def request(method, path, payload=None, **kwargs):
        sent.append((path, payload))
        if path.endswith("/poll"):
            assert payload["binding"]["identity_kind"] == "dns"
            assert "host_fingerprint" not in payload["binding"]
            return {"schema": "portal-mcd-poll-response-v1", "contract_version": "1.0", "request_id": payload["request_id"], "idempotency_key": payload["idempotency_key"], "binding": payload["binding"], "next_command_cursor": 0, "retry_after_ms": 0, "commands": []}
        return {}
    monkeypatch.setattr(client, "_request", request)
    assert client.pull_job(installation_id="installation-1", wait_seconds=0) is None
    worker = SelfHostedPortalWorker(client, SelfHostedScenarioExecutor(state), installation_id="installation-1", portal_keys={}, publisher_keys={}, context={"identity_kind": "dns", "local_identity": DNS}, commercial_key_id="key-1", agent_id="agent-1", host_fingerprint="")
    inventory = worker._minimal_inventory("installation-1")
    assert inventory["binding"] == client._commercial_binding("installation-1")
    assert "host_fingerprint" not in inventory["binding"]
    worker._publish_active_state()
    assert all("host_fingerprint" not in payload.get("binding", {}) for _, payload in sent)


def dns_job():
    artifact = b"#!/bin/sh\nexit 0\n"
    digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
    facts = {"os_id": "ubuntu", "os_version": "24.04", "architecture": "amd64", "package_manager": "apt-get", "capabilities": ["package_manager.query"], "target_kind": "existing_server"}
    stage = {"stage_id": "base_packages", "sequence": 1, "operation": "ensure_supported_base_packages", "depends_on": []}
    job = {"schema": "portal-self-hosted-mcd-job-v2", "contract_version": "2.1", "job_id": "job-1", "request_id": "request-1", "idempotency_key": "request-1:stage", "account_id": "account-1", "agent_id": "agent-1", "installation_id": "installation-1", "commercial_key_id": "key-1", "identity_kind": "dns", "local_identity": DNS, "scenario": {"scenario_id": "scenario", "revision": "r1", "mode": "bootstrap", "artifact_digest": digest}, "scenario_artifact": {"artifact_uri": "https://portal.example.com/artifact", "artifact_digest": digest, "artifact_size_bytes": len(artifact), "artifact_media_type": "application/x-sh", "artifact_revision": "r1"}, "applicability": {"facts": facts, "facts_digest": "sha256:" + hashlib.sha256(canonical_json(facts)).hexdigest()}, "stage_selection": stage, "execution": {"primitive": "scenario.apply", "version": "1.0", "stage": {key: stage[key] for key in ("stage_id", "sequence", "operation")}}, "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z", "nonce": "nonce-1"}
    return job, artifact, facts


def test_dns_scenario_and_durable_terminal_use_plain_identity(tmp_path):
    job, artifact, facts = dns_job()
    assert verify_self_hosted_mcd_job(job, {}, expected_local_identity=DNS)["local_identity"] == DNS
    wrong = {**job, "local_identity": "other.example.invalid"}
    with pytest.raises(ValueError, match="binding_mismatch"):
        verify_self_hosted_mcd_job(wrong, {}, expected_local_identity=DNS)
    state = CommercialState(str(tmp_path / "state.db"))
    calls = []
    executor = SelfHostedScenarioExecutor(state, facts_reader=lambda **kwargs: facts, runner=lambda **kwargs: calls.append(kwargs) or {"status": "succeeded"})
    terminal = executor.run(job, portal_keys={}, publisher_keys={}, context={"identity_kind": "dns", "local_identity": DNS}, expected_agent_id="agent-1", expected_installation_id="installation-1", expected_commercial_key_id="key-1", expected_host_fingerprint="", bootstrap_artifact=None, scenario_artifact=artifact)
    assert terminal["status"] == "succeeded"
    assert terminal["identity_kind"] == "dns" and terminal["local_identity"] == DNS
    assert "host_fingerprint" not in terminal
    assert len(calls) == 1


@pytest.mark.parametrize("expired", [False, True])
def test_dns_worker_terminalizes_delivery_without_host_hash(tmp_path, monkeypatch, expired):
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: pytest.fail("host hash called"))
    state = CommercialState(str(tmp_path / "state.db"))
    import_activation_material(state, material())
    job, artifact, facts = dns_job()
    if expired:
        job["expires_at"] = "2026-01-01T00:01:00Z"
    calls = []
    receipts = []
    class Portal:
        def pull_job(self, **kwargs):
            return {"cursor": "1", "job": job}
        def send_receipt(self, payload, **kwargs):
            receipts.append(payload)
            return {}
    executor = SelfHostedScenarioExecutor(state, facts_reader=lambda **kwargs: facts, runner=lambda **kwargs: calls.append(kwargs) or {"status": "succeeded"})
    worker = SelfHostedPortalWorker(Portal(), executor, installation_id="installation-1", portal_keys={}, publisher_keys={}, context={"identity_kind": "dns", "local_identity": DNS}, commercial_key_id="key-1", agent_id="agent-1", host_fingerprint="")
    monkeypatch.setattr(worker, "_download_scenario_artifact", lambda pin: artifact)
    result = worker.run_once()
    assert result["status"] == ("failed" if expired else "succeeded"), receipts[-1].get("error")
    assert len(calls) == (0 if expired else 1)
    terminal = receipts[-1]
    assert terminal["local_identity"] == DNS
    assert "host_fingerprint" not in terminal
    assert state.get("commercial_portal_cursor") == "1"


def test_dns_enrollment_eligible_with_passive_profile(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mcd_agent.daemon import _commercial_state_eligibility
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: pytest.fail("host hash called"))
    path = str(tmp_path / "state.db")
    import_activation_material(CommercialState(path), material())
    eligible, reason = _commercial_state_eligibility(SimpleNamespace(state_db_path=path, profile_name="passive"))
    assert eligible, reason


def test_dns_primitive_context_never_uses_host_digest():
    from test_scenario_launcher import _primitive_job_context
    from mcd_agent.scenario_launcher import encode_request_frame
    artifact = b"#!/bin/sh\nexit 0\n"
    context = _primitive_job_context(artifact)
    context["job"].update(identity_kind="dns", local_identity=DNS, agent_id="agent-1", installation_id="installation-1")
    binding = context["primitive_context"]["binding"]
    binding.pop("host_fingerprint")
    binding.update(identity_kind="dns", local_identity=DNS)
    assert encode_request_frame(artifact, context)
    binding["host_fingerprint"] = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="fingerprint_forbidden"):
        encode_request_frame(artifact, context)
