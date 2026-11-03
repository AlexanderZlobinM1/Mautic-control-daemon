from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcd_agent.commercial_endpoint_cutover import (
    ENDPOINT_PATH,
    RESPONSE_SCHEMA,
    EndpointCutoverError,
    canonical_portal_api_base,
    cutover_portal_endpoint,
    prepare_cutover_request,
)
from mcd_agent.commercial_portal import CommercialState
from mcd_agent.commercial_portal import (
    PORTAL_INVENTORY_LOCAL_PROVENANCE_FIELDS,
    PORTAL_INVENTORY_LOCAL_TOP_LEVEL_FIELDS,
    inventory_durable_hash,
    inventory_semantic_hash,
    portal_inventory_semantic_content,
    portal_inventory_semantic_hash,
)


OLD = "https://127.0.0.1:15443/api/v1/commercial"
TARGET = "https://portal.sales-snap.com/api/v1/commercial"
AGENT = "agent_304d7a4d-ded5-4bbc-bd8c-74e82aa5f609"
INSTALLATION = "installation_3668c563-cafd-4fb6-80ce-dae9c8eb6e5e"
IDENTITY = "mautic.zlobin.com"


def state_at(tmp_path: Path, *, endpoint: str = OLD) -> CommercialState:
    state = CommercialState(str(tmp_path / "state.db"))
    snapshot = {
        "schema": "portal-mcd-inventory-snapshot-v1",
        "snapshot_revision": "2",
        "host": {"mcd_version": "1.7.0.dev0", "profile": "passive"},
        "instances": [
            {
                "installation_id": INSTALLATION,
                "instance_id": IDENTITY,
                "local_identity": IDENTITY,
                "identity_source": "portal_registration",
                "source_instance_id": IDENTITY,
            }
        ],
    }
    durable_hash = inventory_durable_hash(snapshot)
    state.put_many(
        {
            "portal_api_base": endpoint,
            "portal_agent_credential": "fixture-secret-never-in-receipt",
            "account_id": "account-1",
            "agent_id": AGENT,
            "commercial_key_id": "commercial-key-1",
            "identity_kind": "dns",
            "local_identity": IDENTITY,
            "installation_ids": {IDENTITY: INSTALLATION},
            "commercial_portal_connection_state": "active",
            "commercial_portal_cursors_v1": {INSTALLATION: "10"},
            "commercial_portal_poll_requests_v1": {
                INSTALLATION: {"cursor": "10", "request_id": "poll-1", "idempotency_key": "poll-key-1"}
            },
            "commercial_inventory_state_v1": {
                "schema": "mcd-commercial-inventory-state-v1",
                "installations": {
                    INSTALLATION: {
                        "snapshot_revision": 2,
                        "durable_hash": durable_hash,
                        "durable_snapshot": snapshot,
                        "last_published": {"durable_hash": durable_hash, "snapshot_revision": 2},
                        "last_acknowledged": {"durable_hash": durable_hash, "snapshot_revision": 2},
                    }
                },
            },
            "commercial_inventory_revision": 2,
            "commercial_self_hosted_pending_receipts": [],
        }
    )
    with state._connect() as db:
        db.execute("INSERT INTO commercial_nonces(nonce,seen_at) VALUES(?,?)", ("nonce-1", 1.0))
        db.execute("INSERT INTO commercial_receipts(job_id,payload) VALUES(?,?)", ("job-1", '{"status":"succeeded"}'))
        db.execute("INSERT INTO commercial_usage_sequences(metric,value) VALUES(?,?)", ("metric-1", 7))
    return state


def accepted(payload, *, reconciliation: str = "validated", **overrides):
    echoed = {
        key: payload[key]
        for key in (
            "contract_version",
            "request_id",
            "idempotency_key",
            "previous_portal_api_base",
            "target_portal_api_base",
            "pre_state_sha256",
        )
    }
    pre_state = payload["pre_state"]
    cursor_continuity = [
        {
            "installation_id": item["installation_id"],
            **item["connection"],
            "last_inventory_revision": str(item["connection"]["last_inventory_revision"]),
        }
        for item in pre_state["installations"]
    ]
    return {
        "schema": RESPONSE_SCHEMA,
        **echoed,
        "account_id": pre_state["account_id"],
        "commercial_key_id": pre_state["commercial_key_id"],
        "agent_id": pre_state["agent_id"],
        "installation_ids": sorted(item["installation_id"] for item in pre_state["installations"]),
        "cursor_continuity": cursor_continuity,
        "pending_receipt_count": len(pre_state["pending_receipt_ids"]),
        "status": "accepted",
        "reconciliation": reconciliation,
        "no_replay": True,
        "receipt_id": "cutover-receipt-1",
        "post_state_sha256": "sha256:" + "b" * 64,
        "accepted_at": "2026-11-03T05:00:00Z",
        **overrides,
    }


def test_success_changes_only_endpoint_and_probes_twice(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest(exclude_meta_keys=("portal_api_base",))
    calls = []

    def transport(target, path, payload, idempotency_key, timeout_sec):
        calls.append((target, path, payload, idempotency_key, timeout_sec))
        assert target == TARGET
        assert path == ENDPOINT_PATH
        pre_state = payload["pre_state"]
        assert set(payload) == {
            "schema",
            "contract_version",
            "request_id",
            "idempotency_key",
            "previous_portal_api_base",
            "target_portal_api_base",
            "pre_state",
            "pre_state_sha256",
        }
        assert pre_state["agent_id"] == AGENT
        assert pre_state["identity_kind"] == "dns"
        connection = pre_state["installations"][0]["connection"]
        assert pre_state["installations"][0]["installation_id"] == INSTALLATION
        assert connection["cursor"] == 10
        assert connection["next_command_cursor"] == 10
        assert connection["last_inventory_revision"] == "2"
        assert connection["inventory_semantic_sha256"].startswith("sha256:")
        assert pre_state["pending_receipt_ids"] == []
        assert payload["idempotency_key"] == idempotency_key
        return accepted(payload, reconciliation="reconciled" if len(calls) == 1 else "validated")

    receipt = cutover_portal_endpoint(
        state,
        TARGET,
        expected_agent_id=AGENT,
        expected_local_identity=IDENTITY,
        expected_current_url=OLD,
        transport=transport,
    )
    assert receipt["status"] == "succeeded"
    assert receipt["idempotent"] is False
    assert receipt["no_replay"] is True
    assert "fixture-secret" not in str(receipt)
    assert state.get("portal_api_base") == TARGET
    assert state.get("commercial_portal_cursors_v1") == {INSTALLATION: "10"}
    assert state.get("commercial_portal_poll_requests_v1")[INSTALLATION]["request_id"] == "poll-1"
    assert state.continuity_digest(exclude_meta_keys=("portal_api_base",)) == before
    assert len(calls) == 2


def test_prepare_failure_does_not_mutate_state(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest()

    def failed(*args):
        raise RuntimeError("network detail must not escape")

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_prepare_failed"):
        cutover_portal_endpoint(state, TARGET, transport=failed)
    assert state.get("portal_api_base") == OLD
    assert state.continuity_digest() == before


def test_postcheck_failure_rolls_back_exact_state(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest()
    calls = 0

    def transport(target, path, payload, idempotency_key, timeout_sec):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("postcheck unavailable")
        return accepted(payload)

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_postcheck_failed"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD
    assert state.continuity_digest() == before


def test_mismatched_response_rolls_back_without_cursor_change(tmp_path):
    state = state_at(tmp_path)

    def transport(target, path, payload, idempotency_key, timeout_sec):
        return accepted(payload, agent_id="agent-other")

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_response_mismatch"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD
    assert state.get("commercial_portal_cursors_v1") == {INSTALLATION: "10"}


def test_expected_binding_mismatch_fails_before_network(tmp_path):
    state = state_at(tmp_path)
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return {}

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_agent_mismatch"):
        cutover_portal_endpoint(state, TARGET, expected_agent_id="agent-other", transport=transport)
    assert called is False


@pytest.mark.parametrize(
    "value,code",
    [
        ("http://portal.example.com", "https_required"),
        ("https://127.0.0.1:443", "public_target_required"),
        ("https://portal.example.com/other", "path_invalid"),
        ("https://user@portal.example.com", "url_invalid"),
    ],
)
def test_target_rejects_tls_or_identity_downgrade(value, code):
    with pytest.raises(EndpointCutoverError, match=code):
        canonical_portal_api_base(value, public_target=True)


def test_idempotent_repeat_still_performs_authenticated_double_probe(tmp_path):
    state = state_at(tmp_path, endpoint=TARGET)
    calls = 0

    def transport(target, path, payload, idempotency_key, timeout_sec):
        nonlocal calls
        calls += 1
        return accepted(payload, reconciliation="validated")

    receipt = cutover_portal_endpoint(state, TARGET, transport=transport)
    assert receipt["idempotent"] is True
    assert calls == 2
    assert state.get("portal_api_base") == TARGET


def test_pending_receipts_are_bound_into_request_and_preserved(tmp_path):
    state = state_at(tmp_path)
    pending = [{"command_id": "command-1", "status": "receipt_pending"}]
    state.put("commercial_self_hosted_pending_receipts", pending)

    def transport(target, path, payload, idempotency_key, timeout_sec):
        assert payload["pre_state"]["pending_receipt_ids"] == ["command-1"]
        return accepted(payload)

    cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("commercial_self_hosted_pending_receipts") == pending


def test_equal_semantic_inventory_rebases_only_counter_to_server_floor(tmp_path):
    state = state_at(tmp_path)
    before_snapshot = state.get("commercial_inventory_state_v1")["installations"][INSTALLATION]["durable_snapshot"]
    semantic_hash = portal_inventory_semantic_hash(before_snapshot)
    local_semantic_hash = inventory_semantic_hash(before_snapshot)

    def transport(target, path, payload, idempotency_key, timeout_sec):
        connection = payload["pre_state"]["installations"][0]["connection"]
        accepted_state = [
            dict(
                {"installation_id": INSTALLATION, **connection},
                last_inventory_revision="5",
            )
        ]
        return accepted(payload, cursor_continuity=accepted_state)

    receipt = cutover_portal_endpoint(state, TARGET, transport=transport)
    record = state.get("commercial_inventory_state_v1")["installations"][INSTALLATION]
    assert record["snapshot_revision"] == 5
    assert record["durable_snapshot"]["snapshot_revision"] == "5"
    assert inventory_semantic_hash(record["durable_snapshot"]) == local_semantic_hash
    assert portal_inventory_semantic_hash(record["durable_snapshot"]) == semantic_hash
    assert state.get("commercial_inventory_revision") == 5
    assert record["last_published"]["snapshot_revision"] == 5
    assert record["last_acknowledged"]["snapshot_revision"] == 5
    assert receipt["inventory_reconciliation"] == [
        {
            "installation_id": INSTALLATION,
            "inventory_revision_before": 2,
            "inventory_revision_after": 5,
            "inventory_semantic_sha256": semantic_hash,
            "local_inventory_semantic_sha256": local_semantic_hash,
            "rebased": True,
        }
    ]


def test_portal_inventory_semantic_projection_matches_published_fixture():
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "mcd_agent/contracts/fixtures/portal-inventory-semantic-projection-v1.json"
        ).read_text()
    )
    assert portal_inventory_semantic_content(fixture["snapshot"]) == fixture["expected_projection"]
    assert portal_inventory_semantic_hash(fixture["snapshot"]) != inventory_semantic_hash(fixture["snapshot"])
    assert PORTAL_INVENTORY_LOCAL_TOP_LEVEL_FIELDS == {"installation"}
    assert PORTAL_INVENTORY_LOCAL_PROVENANCE_FIELDS == {
        "identity_source",
        "local_identity",
        "source_instance_id",
    }
    assert fixture["dev46_acceptance"]["excluded_paths"] == [
        "/installation",
        "/instances/*/identity_source",
        "/instances/*/local_identity",
        "/instances/*/source_instance_id",
    ]
    assert fixture["dev46_acceptance"]["portal_semantic_sha256"] == (
        "sha256:8236335f85c8f0ccb85af057f46cf5b1833fdba0e68f4ebbfd9b72f9f3027083"
    )


def test_dev46_rev6_projection_parity_produces_portal_accepted_hash():
    path = (
        Path(__file__).parents[1]
        / "mcd_agent/contracts/fixtures/portal-dns-agent-cutover-inventory-projection-v1.json"
    )
    raw = path.read_bytes()
    fixture = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == "868fb8a8678b873aed9677dee2a6634c0ce89f72fa634eee26c53f0972624c9b"
    assert portal_inventory_semantic_hash(fixture) == (
        "sha256:8236335f85c8f0ccb85af057f46cf5b1833fdba0e68f4ebbfd9b72f9f3027083"
    )


def test_prepare_only_payload_uses_normalized_portal_hash_without_mutation(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest()
    _previous, _target, _binding, payload = prepare_cutover_request(state, TARGET)
    snapshot = state.get("commercial_inventory_state_v1")["installations"][INSTALLATION]["durable_snapshot"]
    connection = payload["pre_state"]["installations"][0]["connection"]
    assert connection["inventory_semantic_sha256"] == portal_inventory_semantic_hash(snapshot)
    assert connection["inventory_semantic_sha256"] != inventory_semantic_hash(snapshot)
    assert state.continuity_digest() == before


def test_different_semantic_inventory_fails_closed_before_endpoint_change(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest()

    def transport(target, path, payload, idempotency_key, timeout_sec):
        connection = payload["pre_state"]["installations"][0]["connection"]
        accepted_state = [
            dict(
                {"installation_id": INSTALLATION, **connection},
                last_inventory_revision="5",
                inventory_semantic_sha256="sha256:" + "f" * 64,
            )
        ]
        return accepted(payload, cursor_continuity=accepted_state)

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_inventory_semantic_mismatch"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD
    assert state.get("commercial_inventory_revision") == 2
    assert state.continuity_digest() == before


def test_inventory_rebase_rolls_back_with_endpoint_on_postcheck_failure(tmp_path):
    state = state_at(tmp_path)
    before = state.continuity_digest()
    calls = 0

    def transport(target, path, payload, idempotency_key, timeout_sec):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("postcheck failed")
        connection = payload["pre_state"]["installations"][0]["connection"]
        return accepted(payload, cursor_continuity=[{"installation_id": INSTALLATION, **connection, "last_inventory_revision": "5"}])

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_postcheck_failed"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD
    assert state.get("commercial_inventory_revision") == 2
    assert state.continuity_digest() == before


def test_server_cursor_cannot_advance_or_regress_local_ack(tmp_path):
    state = state_at(tmp_path)

    def transport(target, path, payload, idempotency_key, timeout_sec):
        connection = payload["pre_state"]["installations"][0]["connection"]
        return accepted(payload, cursor_continuity=[{"installation_id": INSTALLATION, **connection, "cursor": 11, "next_command_cursor": 11, "last_inventory_revision": "2"}])

    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_cursor_state_mismatch"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD


def test_compare_and_swap_rejects_concurrent_endpoint_change(tmp_path, monkeypatch):
    state = state_at(tmp_path)

    def transport(target, path, payload, idempotency_key, timeout_sec):
        return accepted(payload)

    monkeypatch.setattr(state, "compare_and_swap_many", lambda *args, **kwargs: False)
    with pytest.raises(EndpointCutoverError, match="portal_endpoint_cutover_state_conflict"):
        cutover_portal_endpoint(state, TARGET, transport=transport)
    assert state.get("portal_api_base") == OLD
