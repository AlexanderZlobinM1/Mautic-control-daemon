import pytest

from mcd_agent.commercial_owh_auto import (
    OwhAutoContractError,
    OwhAutoStageRunner,
    accept_automatic_install_request,
    build_scenario_request,
    validate_artifact_response,
    validate_install_request,
    validate_install_response,
    validate_manual_enrollment,
    validate_stage_receipt,
    validate_terminal_receipt,
)


def install_request():
    return {
        "schema": "portal-owh-auto-install-request-v1",
        "contract_version": "1.0",
        "request_id": "req-1",
        "idempotency_key": "idem-1",
        "account_id": "account-1",
        "target": {"dns_name": "Mautic.Example.COM", "local_identity": "mautic.example.com"},
        "commercial_access": {"commercial_key_id": "key-1", "commercial_api_key": "secret-value"},
        "activation": {"mode": "automatic", "activate_without_browser": True, "manual_action_required": False},
        "scenario_policy": {"selection": "latest_compatible", "transport": "https", "integrity": "sha256"},
    }


def test_install_request_normalizes_identity_but_keeps_secret_out_of_response_shape():
    parsed = validate_install_request(install_request())
    assert parsed["target"] == {"dns_name": "mautic.example.com", "local_identity": "mautic.example.com"}
    assert parsed["commercial_access"]["commercial_api_key"] == "secret-value"


def test_forbidden_legacy_extensions_are_rejected_recursively():
    payload = install_request()
    payload["target"]["metadata"] = {"agent_signature": "legacy"}
    with pytest.raises(OwhAutoContractError, match="forbidden_extension"):
        validate_install_request(payload)


def test_install_response_and_manual_enrollment_are_separate_modes():
    response = validate_install_response(
        {
            "schema": "portal-owh-auto-install-response-v1",
            "contract_version": "1.0",
            "request_id": "req-1",
            "status": "activated",
            "installation": {"installation_id": "install-1", "dns_name": "mautic.example.com", "local_identity": "mautic.example.com"},
            "activation": {"state": "active", "mode": "automatic", "manual_action_required": False},
        }
    )
    assert response["activation"]["mode"] == "automatic"

    manual = validate_manual_enrollment(
        {
            "schema": "portal-mcd-manual-enrollment-v1",
            "contract_version": "1.0",
            "mode": "manual",
            "installation": {"dns_name": "manual.example.com", "local_identity": "manual.example.com"},
            "activation": {"state": "pending", "manual_action_required": True, "install_scenarios": False},
        }
    )
    assert manual["activation"]["manual_action_required"] is True


def test_scenario_request_uses_observed_target_facts_without_install_policy():
    request = build_scenario_request(
        request_id="scenario-1",
        installation_id="install-1",
        dns_name="mautic.example.com",
        stage_id="database_cache",
        sequence=2,
        target={"os_id": "ubuntu", "os_version": "24.04", "architecture": "amd64", "package_manager": "apt"},
    )
    assert request["stage"] == {"id": "database_cache", "sequence": 2}
    assert request["selection"] == {"mode": "latest_compatible", "transport": "https", "integrity": "sha256"}


def test_artifact_response_requires_https_and_sha256():
    artifact = {
        "schema": "portal-owh-auto-artifact-response-v1",
        "contract_version": "1.0",
        "request_id": "scenario-1",
        "stage": {"id": "database_cache", "sequence": 2},
        "status": "available",
        "scenario": {"scenario_id": "scenario-db", "revision": "7", "compatible_with": {"os_id": "ubuntu", "os_version": "24.04", "architecture": "amd64", "package_manager": "apt"}},
        "artifact": {"uri": "https://portal.example.com/scenario-db-7.tar.gz", "revision": "7", "digest": "sha256:" + "a" * 64, "size_bytes": 10, "media_type": "application/gzip"},
    }
    assert validate_artifact_response(artifact)["artifact"]["digest"].startswith("sha256:")
    artifact["artifact"]["uri"] = "http://portal.example.com/scenario-db-7.tar.gz"
    with pytest.raises(OwhAutoContractError, match="https_required"):
        validate_artifact_response(artifact)


def test_stage_and_terminal_receipts_keep_result_data_scalar_and_bounded():
    stage = validate_stage_receipt(
        {
            "schema": "portal-owh-auto-stage-receipt-v1",
            "contract_version": "1.0",
            "request_id": "scenario-1",
            "installation": {"installation_id": "install-1", "dns_name": "mautic.example.com"},
            "stage": {"id": "base_packages", "sequence": 1},
            "status": "succeeded",
            "observed_at": "2026-10-29T12:00:00Z",
            "result": {"changed": True, "message": "ok"},
        }
    )
    assert stage["result"]["changed"] is True

    terminal = validate_terminal_receipt(
        {
            "schema": "portal-owh-auto-terminal-receipt-v1",
            "contract_version": "1.0",
            "request_id": "req-1",
            "installation": {"installation_id": "install-1", "dns_name": "mautic.example.com"},
            "status": "succeeded",
            "started_at": "2026-10-29T12:00:00Z",
            "completed_at": "2026-10-29T12:01:00Z",
            "stages": [{"id": stage_id, "sequence": sequence, "status": "succeeded"} for sequence, stage_id in enumerate(("base_packages", "database_cache", "web_stack", "mautic"), 1)],
        }
    )
    assert len(terminal["stages"]) == 4

    stage["result"]["nested"] = {"not": "allowed"}
    with pytest.raises(OwhAutoContractError, match="stage_receipt_invalid"):
        validate_stage_receipt(stage)


class _State:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value


def test_automatic_activation_uses_runtime_worker_identity_and_persists_full_binding():
    state = _State({"agent_id": "agent-1", "portal_api_base": "https://portal.example/api/v1/commercial"})
    response = {
        "schema": "portal-owh-auto-install-response-v1",
        "contract_version": "1.0",
        "request_id": "req-1",
        "status": "activated",
        "installation": {"installation_id": "installation-1", "dns_name": "mautic.example.com", "local_identity": "mautic.example.com"},
        "activation": {"state": "active", "mode": "automatic", "manual_action_required": False},
    }
    result = accept_automatic_install_request(state, install_request(), register=lambda request: response)
    assert result["status"] == "activated"
    assert state.get("installation_ids") == {"mautic.example.com": "installation-1"}
    assert state.get("commercial_portal_connection_state") == "active"
    assert state.get("portal_agent_credential") == "secret-value"


def test_automatic_activation_fails_closed_without_runtime_worker_config():
    with pytest.raises(OwhAutoContractError, match="worker_config_missing"):
        accept_automatic_install_request(_State(), install_request(), register=lambda _request: {})


def test_automatic_stage_runner_fetches_all_stages_and_emits_terminal_receipt():
    receipts = []
    body = b"portal-owned-stage"
    import hashlib

    digest = "sha256:" + hashlib.sha256(body).hexdigest()

    def scenario(request):
        return {
            "schema": "portal-owh-auto-artifact-response-v1",
            "contract_version": "1.0",
            "request_id": request["request_id"],
            "stage": dict(request["stage"]),
            "status": "available",
            "scenario": {"scenario_id": "portal-stage", "revision": "1", "compatible_with": dict(request["target"])},
            "artifact": {"uri": "https://portal.example/artifact", "revision": "1", "digest": digest, "size_bytes": len(body), "media_type": "application/octet-stream"},
        }

    runner = OwhAutoStageRunner(
        request_scenario=scenario,
        download_artifact=lambda _uri, _size: body,
        execute_stage=lambda request, _body: {"stage": request["stage"]["id"], "changed": True},
        send_receipt=lambda receipt: receipts.append(dict(receipt)),
    )
    terminal = runner.run(
        request_id="req-1",
        installation_id="installation-1",
        dns_name="mautic.example.com",
        target={"os_id": "ubuntu", "os_version": "24.04", "architecture": "amd64", "package_manager": "apt"},
    )
    assert terminal["status"] == "succeeded"
    assert len(receipts) == 9
    assert [item["stage"]["id"] for item in receipts if item["schema"] == "portal-owh-auto-stage-receipt-v1" and item["status"] == "started"] == ["base_packages", "database_cache", "web_stack", "mautic"]
