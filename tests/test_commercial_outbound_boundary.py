from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import socket
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import (
    PORTAL_MCD_POLL_REQUEST_SCHEMA,
    CommercialPortalError,
    CommercialState,
    PortalClient,
    _IPv4HTTPSConnection,
    _create_ipv4_connection,
    build_standalone_settings_snapshot,
    build_portal_mcd_poll_request,
    canonical_json,
    inventory_durable_hash,
    standalone_settings_etag,
    verify_portal_mcd_attach_response,
    verify_portal_mcd_poll_response,
)
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class _Executor:
    def __init__(self, state: CommercialState) -> None:
        self.state = state
        self.runs = 0

    def run(self, job, **kwargs):
        self.runs += 1
        return {"status": "succeeded", "job_id": str(job["job_id"])}


def _worker(tmp_path: Path, client: object, clock: _Clock) -> tuple[SelfHostedPortalWorker, CommercialState, _Executor]:
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    executor = _Executor(state)
    worker = SelfHostedPortalWorker(
        client,
        executor,
        installation_id="installation-1",
        portal_keys={"portal-1": "key"},
        # Portal transport and noop/settings commands do not need MCC
        # artifact trust; that trust is checked only at scenario execution.
        publisher_keys={},
        context={},
        commercial_key_id="commercial-key-1",
        agent_id="agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        poll_wait_seconds=25,
        backoff_base_seconds=1,
        backoff_max_seconds=8,
        clock=clock,
    )
    return worker, state, executor


def test_commercial_worker_is_outbound_only_and_uses_long_poll(monkeypatch, tmp_path):
    calls = []

    class Portal:
        def pull_job(self, **kwargs):
            calls.append(kwargs)
            return None

    def forbidden_socket(*_args, **_kwargs):
        raise AssertionError("commercial worker must not create an inbound socket")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    worker, _state, _executor = _worker(tmp_path, Portal(), _Clock())

    assert worker.run_once() == {"status": "idle"}
    assert calls and calls[0]["wait_seconds"] == 25
    assert calls[0]["cursor"] == ""


def test_portal_job_stays_queued_during_outage_and_is_consumed_on_next_outbound_cycle(monkeypatch, tmp_path):
    clock = _Clock()
    job = {"job_id": "job-queued", "installation_id": "installation-1"}

    class Portal:
        def __init__(self):
            self.polls = 0
            self.queued = True
            self.receipts = []

        def pull_job(self, **kwargs):
            self.polls += 1
            if self.polls == 1:
                raise OSError("Portal offline")
            assert kwargs["cursor"] == ""
            self.queued = False
            return {"cursor": "cursor-1", "job": job}

        def send_receipt(self, payload):
            self.receipts.append(payload)
            return {}

    portal = Portal()
    worker, state, executor = _worker(tmp_path, portal, clock)
    monkeypatch.setattr(
        "mcd_agent.commercial_self_hosted.verify_self_hosted_mcd_job",
        lambda *_args, **_kwargs: {
            "job": job,
            "facts": {},
            "bootstrap_artifact": {"artifact_uri": "https://portal.test/bootstrap", "artifact_size_bytes": 1},
            "scenario_artifact": {"artifact_uri": "https://portal.test/scenario", "artifact_size_bytes": 1},
        },
    )
    monkeypatch.setattr("mcd_agent.commercial_self_hosted.download_external_artifact", lambda *_args, **_kwargs: b"x")

    first = worker.run_once()
    assert first["status"] == "backoff"
    assert portal.queued is True
    assert portal.polls == 1

    # A persistent retry deadline prevents a tight loop and survives worker
    # recreation; no second Portal request is made while the host is offline.
    second = worker.run_once()
    assert second["status"] == "backoff"
    assert portal.polls == 1
    assert state.get("commercial_portal_transport_v1")["failures"] == 1

    clock.value = 101.0
    third = worker.run_once()
    assert third == {"status": "succeeded", "job_id": "job-queued"}
    assert portal.queued is False
    assert portal.polls == 2
    assert executor.runs == 1
    assert state.get("commercial_portal_cursor") == "cursor-1"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_saved_delivery_is_replayed_after_worker_restart_without_a_second_pull(monkeypatch, tmp_path):
    clock = _Clock()
    job = {"job_id": "job-restart", "installation_id": "installation-1"}

    class Portal:
        def __init__(self):
            self.polls = 0

        def pull_job(self, **_kwargs):
            self.polls += 1
            raise AssertionError("saved delivery must be used before polling again")

    portal = Portal()
    worker, state, executor = _worker(tmp_path, portal, clock)
    state.put("commercial_self_hosted_pending_delivery_v1", {"installation_id": "installation-1", "cursor": "before", "cursor_next": "after", "job": job})
    monkeypatch.setattr(
        "mcd_agent.commercial_self_hosted.verify_self_hosted_mcd_job",
        lambda *_args, **_kwargs: {
            "job": job,
            "facts": {},
            "bootstrap_artifact": {"artifact_uri": "https://portal.test/bootstrap", "artifact_size_bytes": 1},
            "scenario_artifact": {"artifact_uri": "https://portal.test/scenario", "artifact_size_bytes": 1},
        },
    )
    monkeypatch.setattr("mcd_agent.commercial_self_hosted.download_external_artifact", lambda *_args, **_kwargs: b"x")

    assert worker.run_once() == {"status": "succeeded", "job_id": "job-restart"}
    assert portal.polls == 0
    assert executor.runs == 1
    assert state.get("commercial_portal_cursor") == "after"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_exact_poll_envelope_is_mcd_to_portal_and_response_is_signed():
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    binding = {"agent_id": "agent-1", "installation_id": "installation-1", "host_fingerprint": "sha256:" + "a" * 64, "commercial_key_id": "commercial-key-1"}
    request = build_portal_mcd_poll_request(request_id="request-1", idempotency_key="poll-1", binding=binding, cursor=None, wait_ms=30_000, backoff_ms=1_000)
    assert request["schema"] == PORTAL_MCD_POLL_REQUEST_SCHEMA
    assert request["direction"] == "mcd_to_portal"
    response = {
        "schema": "portal-mcd-poll-response-v1",
        "contract_version": "1.0",
        "request_id": "request-1",
        "idempotency_key": "poll-1",
        "binding": binding,
        "next_cursor": "cursor-1",
        "commands": [{"schema": "portal-mcd-command-noop-v1", "command_id": "noop-1"}],
        "retry_after_ms": 0,
        "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1", "value": ""},
    }
    unsigned = dict(response)
    unsigned.pop("portal_signature")
    response["portal_signature"]["value"] = base64.urlsafe_b64encode(private.sign(canonical_json(unsigned))).rstrip(b"=").decode()
    parsed = verify_portal_mcd_poll_response(response, public_keys={"portal-1": public}, expected_request_id="request-1", expected_idempotency_key="poll-1", expected_binding=binding)
    assert parsed["next_cursor"] == "cursor-1"
    assert parsed["commands"][0]["command_id"] == "noop-1"


def test_unsigned_poll_response_is_accepted_without_portal_signing_keys():
    binding = {"agent_id": "agent-1", "installation_id": "installation-1", "host_fingerprint": "sha256:" + "a" * 64, "commercial_key_id": "commercial-key-1"}
    response = {
        "schema": "portal-mcd-poll-response-v1",
        "contract_version": "1.0",
        "request_id": "request-unsigned",
        "idempotency_key": "poll-unsigned",
        "binding": binding,
        "next_cursor": "cursor-unsigned",
        "commands": [],
        "retry_after_ms": 0,
    }
    parsed = verify_portal_mcd_poll_response(
        response,
        public_keys={},
        expected_request_id="request-unsigned",
        expected_idempotency_key="poll-unsigned",
        expected_binding=binding,
    )
    assert parsed == {"next_cursor": "cursor-unsigned", "commands": [], "retry_after_ms": 0}


def test_unsigned_attach_response_is_accepted_without_portal_signing_keys():
    binding = {"agent_id": "agent-1", "installation_id": "installation-1", "host_fingerprint": "sha256:" + "a" * 64, "commercial_key_id": "commercial-key-1"}
    response = {
        "schema": "portal-mcd-attach-v1",
        "contract_version": "1.0",
        "request_id": "request-unsigned",
        "idempotency_key": "adopt-unsigned",
        "binding": binding,
        "status": "attached",
        "connection": {"status": "active", "channel_contract": "1.0"},
        "inventory": {"schema": "portal-mcd-inventory-snapshot-v1", "complete": True},
    }
    assert verify_portal_mcd_attach_response(
        response,
        public_keys={},
        expected_request_id="request-unsigned",
        expected_idempotency_key="adopt-unsigned",
        expected_binding=binding,
    ) == response


def test_portal_client_posts_poll_envelope_and_keeps_cursor_until_worker_commit(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("portal_agent_credential", "credential")
    state.put("agent_id", "agent-1")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("commercial_key_id", "commercial-key-1")
    state.put("portal_job_signing_keys", {"portal-1": public})
    captured = {}
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    client = PortalClient(state)

    def fake_request(method, path, payload, idempotency_key):
        captured.update({"method": method, "path": path, "payload": payload, "idempotency_key": idempotency_key})
        response = {
            "schema": "portal-mcd-poll-response-v1",
            "contract_version": "1.0",
            "request_id": payload["request_id"],
            "idempotency_key": payload["idempotency_key"],
            "binding": payload["binding"],
            "next_cursor": "cursor-1",
            "commands": [],
            "retry_after_ms": 0,
            "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1", "value": ""},
        }
        unsigned = dict(response)
        unsigned.pop("portal_signature")
        response["portal_signature"]["value"] = base64.urlsafe_b64encode(private.sign(canonical_json(unsigned))).rstrip(b"=").decode()
        return response

    client._request = fake_request
    assert client.pull_job(installation_id="installation-1", cursor="", wait_seconds=30) is None
    assert captured["method"] == "POST"
    assert captured["path"] == "agents/agent-1/installations/installation-1/poll"
    assert captured["payload"]["direction"] == "mcd_to_portal"
    assert captured["payload"]["wait_ms"] == 30_000
    assert state.get("commercial_portal_cursor") is None


def test_portal_client_accepts_unsigned_poll_and_does_not_read_signing_state(tmp_path):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("portal_agent_credential", "credential")
    state.put("agent_id", "agent-1")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("commercial_key_id", "commercial-key-1")
    state.put("portal_job_signing_keys", {"legacy": "must-be-scrubbed"})
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    client = PortalClient(state)

    def fake_request(_method, _path, payload, idempotency_key):
        assert idempotency_key == payload["idempotency_key"]
        return {
            "schema": "portal-mcd-poll-response-v1",
            "contract_version": "1.0",
            "request_id": payload["request_id"],
            "idempotency_key": payload["idempotency_key"],
            "binding": payload["binding"],
            "next_cursor": "cursor-unsigned",
            "commands": [],
            "retry_after_ms": 0,
        }

    client._request = fake_request
    assert client.pull_job(installation_id="installation-1", cursor="", wait_seconds=30) is None
    assert state.get("portal_job_signing_keys") is None


def test_poll_response_validation_is_typed_and_bounded(tmp_path):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("portal_agent_credential", "credential")
    state.put("agent_id", "agent-1")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("commercial_key_id", "commercial-key-1")
    client = PortalClient(state)

    def fake_request(_method, _path, payload, idempotency_key):
        return {
            "schema": "portal-mcd-poll-response-v1",
            "contract_version": "1.0",
            "request_id": payload["request_id"],
            "idempotency_key": idempotency_key,
            "binding": {**payload["binding"], "installation_id": "wrong-installation"},
            "next_cursor": "cursor-10",
            "commands": [],
            "retry_after_ms": 0,
        }

    client._request = fake_request
    with pytest.raises(CommercialPortalError) as raised:
        client.pull_job(installation_id="installation-1", cursor="9", wait_seconds=30)
    assert raised.value.stage == "poll.response_validation"
    assert raised.value.code == "portal_mcd_poll_response_binding_mismatch"
    assert raised.value.retryable is False
    assert state.get("commercial_portal_poll_request")["cursor"] == "9"


def test_worker_persists_typed_poll_diagnostic(tmp_path):
    class Portal:
        def pull_job(self, **_kwargs):
            raise CommercialPortalError(
                stage="poll.response_validation",
                code="portal_mcd_poll_response_binding_mismatch",
                retryable=False,
            )

    worker, state, _executor = _worker(tmp_path, Portal(), _Clock())
    assert worker.run_once() == {"status": "backoff", "retry_after": 101.0}
    assert state.get("commercial_portal_transport_v1")["last_error"] == "job_poll:portal_mcd_poll_response_binding_mismatch"


def test_portal_https_transport_forces_ipv4_and_keeps_hostname_for_sni(monkeypatch):
    seen = {}

    class FakeSocket:
        def settimeout(self, value):
            seen["timeout"] = value

        def connect(self, sockaddr):
            seen["sockaddr"] = sockaddr

        def close(self):
            seen["closed"] = True

    def fake_getaddrinfo(host, port, family, socktype):
        seen["resolve"] = (host, port, family, socktype)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", port))]

    monkeypatch.setattr("mcd_agent.commercial_portal.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("mcd_agent.commercial_portal.socket.socket", lambda family, socktype, proto: FakeSocket())
    sock = _create_ipv4_connection(("portal.example", 443), timeout=5)
    assert seen["resolve"] == ("portal.example", 443, socket.AF_INET, socket.SOCK_STREAM)
    assert seen["sockaddr"] == ("192.0.2.10", 443)
    connection = _IPv4HTTPSConnection("portal.example", 443)
    assert connection.host == "portal.example"
    assert connection._create_connection is _create_ipv4_connection


def test_stage2_adoption_and_batch_commands_are_durable_and_ordered(tmp_path):
    clock = _Clock()
    etag = "sha256:" + "a" * 64

    class Portal:
        def __init__(self):
            self.polls = 0
            self.adoptions = []
            self.receipts = []

        def adopt(self, **kwargs):
            self.adoptions.append(kwargs)
            return {"status": "adopted"}

        def pull_job(self, **kwargs):
            self.polls += 1
            assert kwargs["installation_id"] == "installation-1"
            assert kwargs["cursor"] == "" if self.polls == 1 else False
            if self.polls > 1:
                raise AssertionError("the next command must come from the saved batch")
            return {
                "cursor": "cursor-batch-1",
                "commands": [
                    {
                        "schema": "portal-mcd-command-noop-v1",
                        "command_id": "noop-1",
                        "delivery_cursor": 1,
                        "request_id": "request-1",
                        "idempotency_key": "noop-key-1",
                        "issued_at": "1970-01-01T00:00:00Z",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "expected_standalone_etag": etag,
                    },
                    {
                        "schema": "portal-mcd-command-noop-v1",
                        "command_id": "noop-2",
                        "delivery_cursor": 2,
                        "request_id": "request-2",
                        "idempotency_key": "noop-key-2",
                        "issued_at": "1970-01-01T00:00:00Z",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "expected_standalone_etag": etag,
                    },
                ],
            }

        def send_receipt(self, payload, **kwargs):
            self.receipts.append((payload, kwargs))
            return {}

    portal = Portal()
    worker, state, _executor = _worker(tmp_path, portal, clock)
    worker.context["standalone_etag"] = etag

    first = worker.run_once()
    assert first["status"] == "succeeded"
    assert state.get("commercial_portal_cursor") is None
    assert state.get("commercial_self_hosted_pending_delivery_v1")["job"]["commands"][0]["command_id"] == "noop-2"
    assert portal.adoptions and portal.adoptions[0]["installation_id"] == "installation-1"

    second = worker.run_once()
    assert second["status"] == "succeeded"
    assert state.get("commercial_portal_cursor") == "cursor-batch-1"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None
    assert [item[0]["command_id"] for item in portal.receipts if item[0].get("schema") == "portal-mcd-command-noop-receipt-v1"] == ["noop-1", "noop-2"]
    assert [item[0]["delivery_cursor"] for item in portal.receipts if item[0].get("schema") == "portal-mcd-command-noop-receipt-v1"] == [1, 2]


def test_stage2_v3_command_is_handled_and_receipted(tmp_path):
    class Portal:
        def __init__(self):
            self.receipts = []

        def pull_job(self, **_kwargs):
            return {
                "cursor": "cursor-v3-1",
                "commands": [{
                    "schema": "mcc-desired-runtime-sync-v3",
                    "desired_state_sync_protocol": 3,
                    "command_id": "sync-1",
                    "delivery_cursor": 3,
                    "request_id": "sync-request-1",
                    "idempotency_key": "sync-key-1",
                    "runtime_overrides": {"scheduler_host_max_parallel": 8},
                }],
            }

        def send_receipt(self, payload, **_kwargs):
            self.receipts.append(payload)
            return {}

    portal = Portal()
    worker, state, _executor = _worker(tmp_path, portal, _Clock())
    worker.runtime_command_handler = lambda command: {"status": "succeeded", "applied_keys": sorted(command["runtime_overrides"])}

    result = worker.run_once()

    assert result["status"] == "succeeded"
    assert state.get("commercial_portal_cursor") == "cursor-v3-1"
    receipt = next(item for item in portal.receipts if item.get("schema") == "portal-mcd-command-receipt-v1")
    assert receipt["command_id"] == "sync-1"
    assert receipt["delivery_cursor"] == 3
    assert receipt["status"] == "succeeded"


def test_standalone_settings_etag_is_shared_and_durable_across_worker_restart(tmp_path):
    values = {"jobs_max_workers": 2, "scheduler_paused": False}
    expected = standalone_settings_etag(values)
    snapshot = build_standalone_settings_snapshot(values, revision=7, sync={"desired_state_sync_protocol": 3, "pending_intent": None, "conflict": None})
    assert snapshot["etag"] == expected

    class Portal:
        def pull_job(self, **_kwargs):
            return None

    worker, state, _executor = _worker(tmp_path, Portal(), _Clock())
    worker.context["runtime_overrides"] = values
    inventory = worker._inventory("installation-1")
    assert inventory["host"]["settings"]["etag"] == expected
    assert state.get("commercial_standalone_etag") == expected
    assert state.get("commercial_standalone_settings_snapshot_v1")["etag"] == expected
    assert state.get("commercial_standalone_settings_snapshot_v1")["schema"] == "mcd-standalone-settings-snapshot-v1"

    restarted = CommercialState(str(tmp_path / "commercial.sqlite"))
    assert restarted.get("commercial_standalone_etag") == expected
    assert restarted.get("commercial_standalone_settings_snapshot_v1")["etag"] == expected


@pytest.mark.parametrize(
    ("case", "expected", "actual", "expires_at", "reason"),
    [
        ("unavailable", "sha256:" + "a" * 64, "", "2099-01-01T00:00:00Z", "commercial_standalone_etag_unavailable"),
        ("mismatch", "sha256:" + "a" * 64, "sha256:" + "b" * 64, "2099-01-01T00:00:00Z", "commercial_standalone_etag_mismatch"),
        ("malformed", "not-an-etag", "sha256:" + "b" * 64, "2099-01-01T00:00:00Z", "commercial_noop_command_invalid"),
        ("expired", "sha256:" + "a" * 64, "sha256:" + "a" * 64, "1970-01-01T00:00:00Z", "commercial_noop_command_expired"),
    ],
)
def test_noop_failure_always_emits_terminal_receipt_and_advances_delivery(tmp_path, case, expected, actual, expires_at, reason):
    class Portal:
        def __init__(self):
            self.receipts = []

        def pull_job(self, **_kwargs):
            return {
                "cursor": "cursor-1",
                "commands": [{
                    "schema": "portal-mcd-command-noop-v1",
                    "command_id": f"noop-{case}",
                    "delivery_cursor": 1,
                    "request_id": f"request-{case}",
                    "idempotency_key": f"noop-key-{case}",
                    "issued_at": "1970-01-01T00:00:00Z",
                    "expires_at": expires_at,
                    "expected_standalone_etag": expected,
                    "command": {"primitive": "connection.noop", "version": "1.0"},
                }],
            }

        def send_receipt(self, payload, **_kwargs):
            self.receipts.append(payload)
            return {}

    portal = Portal()
    worker, state, executor = _worker(tmp_path / case, portal, _Clock())
    if actual:
        worker.context["standalone_etag"] = actual

    result = worker.run_once()

    assert result["status"] == "failed"
    assert result["reason"] == reason
    assert executor.runs == 0
    assert state.get("commercial_portal_cursor") == "cursor-1"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None
    assert len(portal.receipts) == 1
    receipt = portal.receipts[0]
    assert receipt["schema"] == "portal-mcd-command-receipt-v1"
    assert receipt["status"] == "failed"
    assert receipt["payload"]["reason"] == reason


def test_noop_terminal_receipt_survives_delivery_outage_and_restart(tmp_path):
    expected = "sha256:" + "a" * 64

    class Portal:
        def __init__(self):
            self.receipts = []
            self.receipt_calls = 0
            self.fail_once = True

        def pull_job(self, **_kwargs):
            return {
                "cursor": "cursor-1",
                "commands": [{
                    "schema": "portal-mcd-command-noop-v1",
                    "command_id": "noop-outage",
                    "delivery_cursor": 1,
                    "request_id": "request-outage",
                    "idempotency_key": "noop-key-outage",
                    "issued_at": "1970-01-01T00:00:00Z",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "expected_standalone_etag": expected,
                    "command": {"primitive": "connection.noop", "version": "1.0"},
                }],
            }

        def send_receipt(self, payload, **_kwargs):
            self.receipt_calls += 1
            if self.fail_once:
                self.fail_once = False
                raise OSError("Portal offline")
            if not any(item["idempotency_key"] == payload["idempotency_key"] for item in self.receipts):
                self.receipts.append(payload)
            return {}

    portal = Portal()
    worker, state, _executor = _worker(tmp_path, portal, _Clock())
    first = worker.run_once()
    assert first["status"] == "receipt_pending"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is not None
    assert state.get("commercial_self_hosted_pending_receipts")[0]["idempotency_key"] == "noop-key-outage"

    restarted = _worker(tmp_path, portal, _Clock())[0]
    second = restarted.run_once()
    assert second["status"] == "failed"
    assert state.get("commercial_portal_cursor") == "cursor-1"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None
    assert state.get("commercial_self_hosted_pending_receipts") == []
    assert portal.receipt_calls == 3
    assert len(portal.receipts) == 1
    assert portal.receipts[0]["idempotency_key"] == "noop-key-outage"


def _inventory_fixture(*, version="1.5.255", settings=None, instances=None, request_id="volatile-1", observed_at="2026-10-27T00:00:00Z"):
    values = settings or {"jobs_max_workers": 2, "scheduler_paused": False}
    return {
        "schema": "portal-mcd-inventory-snapshot-v1",
        "contract_version": "1.0",
        "request_id": request_id,
        "observed_at": observed_at,
        "binding": {
            "agent_id": "agent-1",
            "installation_id": "installation-1",
            "host_fingerprint": "sha256:" + "a" * 64,
            "commercial_key_id": "commercial-key-1",
        },
        "snapshot_revision": "0",
        "complete": True,
        "host": {
            "mcd_version": version,
            "profile": "commercial",
            "service_status": "active",
            "scheduler_paused": False,
            "settings": build_standalone_settings_snapshot(values, revision=0, sync={}),
        },
        "instances": instances or [],
    }


def test_inventory_revision_ignores_transport_fields_and_bumps_once_per_durable_change(tmp_path):
    class Portal:
        def __init__(self):
            self.inventories = []

        def send_inventory(self, **kwargs):
            self.inventories.append(kwargs["inventory"])
            return {"status": "accepted"}

        def pull_job(self, **_kwargs):
            return None

    current = {"value": _inventory_fixture()}
    portal = Portal()
    clock = _Clock()
    worker, state, _executor = _worker(tmp_path, portal, clock)
    worker.inventory_provider = lambda _installation: dict(current["value"])

    assert worker.run_once()["status"] == "idle"
    first = portal.inventories[-1]
    assert first["snapshot_revision"] == "1"
    assert state.get("commercial_inventory_state_v1")["installations"]["installation-1"]["snapshot_revision"] == 1

    current["value"] = _inventory_fixture(request_id="volatile-2", observed_at="2026-10-27T00:01:00Z")
    current["value"]["agent_signature"] = {"algorithm": "ed25519", "key_id": "volatile-2", "value": "sig-2"}
    assert worker.run_once()["status"] == "idle"
    assert len(portal.inventories) == 1

    current["value"] = _inventory_fixture(version="1.5.256", request_id="volatile-3")
    assert worker.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "2"
    assert len(portal.inventories) == 2

    restarted, restarted_state, _ = _worker(tmp_path, portal, _Clock())
    restarted.inventory_provider = lambda _installation: dict(current["value"])
    assert restarted.run_once()["status"] == "idle"
    assert restarted_state.get("commercial_inventory_state_v1")["installations"]["installation-1"]["snapshot_revision"] == 2
    assert len(portal.inventories) == 3
    assert portal.inventories[-1]["snapshot_revision"] == "2"


def test_inventory_revision_changes_for_settings_and_instances_and_replays_same_revision_after_outage(tmp_path):
    class Portal:
        def __init__(self):
            self.inventories = []
            self.fail_once = True

        def send_inventory(self, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise OSError("Portal offline")
            self.inventories.append(kwargs["inventory"])
            return {"status": "accepted"}

        def pull_job(self, **_kwargs):
            return None

    current = {"value": _inventory_fixture()}
    portal = Portal()
    clock = _Clock()
    worker, state, _executor = _worker(tmp_path, portal, clock)
    worker.inventory_provider = lambda _installation: dict(current["value"])
    assert worker.run_once()["status"] == "backoff"
    assert state.get("commercial_inventory_state_v1")["installations"]["installation-1"]["snapshot_revision"] == 1

    clock.value = 102.0
    replay, _, _ = _worker(tmp_path, portal, clock)
    replay.inventory_provider = lambda _installation: dict(current["value"])
    assert replay.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "1"
    record = state.get("commercial_inventory_state_v1")["installations"]["installation-1"]
    assert record["last_published"] == {"durable_hash": record["durable_hash"], "snapshot_revision": 1}
    assert record["last_acknowledged"] == record["last_published"]

    current["value"] = _inventory_fixture(settings={"jobs_max_workers": 4, "scheduler_paused": False})
    assert replay.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "2"

    current["value"] = _inventory_fixture(instances=[{"instance_id": "site-1", "status": "active"}])
    assert replay.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "3"


def test_inventory_revision_assignment_is_atomic_across_concurrent_workers(tmp_path):
    class Portal:
        def send_inventory(self, **_kwargs):
            return {"status": "accepted"}

        def pull_job(self, **_kwargs):
            return None

    portal = Portal()
    worker, state, _executor = _worker(tmp_path, portal, _Clock())
    inventory = _inventory_fixture()
    worker.inventory_provider = lambda _installation: dict(inventory)

    def prepare():
        return worker._inventory("installation-1")["snapshot_revision"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = sorted(pool.map(lambda _unused: prepare(), range(2)))
    assert revisions == ["1", "1"]
    record = state.get("commercial_inventory_state_v1")["installations"]["installation-1"]
    assert record["snapshot_revision"] == 1
    assert record["durable_hash"] == inventory_durable_hash({**inventory, "snapshot_revision": "1"})


def test_inventory_state_corruption_fails_closed_without_revision_rollback(tmp_path):
    class Portal:
        def pull_job(self, **_kwargs):
            return None

    worker, state, _executor = _worker(tmp_path, Portal(), _Clock())
    state.put(
        "commercial_inventory_state_v1",
        {"schema": "mcd-commercial-inventory-state-v1", "installations": {"installation-1": {"snapshot_revision": "corrupt"}}},
    )
    worker.inventory_provider = lambda _installation: _inventory_fixture()
    with pytest.raises(ValueError, match="commercial_inventory_state_invalid"):
        worker._inventory("installation-1")


def test_inventory_revision_never_rolls_back_from_stale_durable_state(tmp_path):
    class Portal:
        def pull_job(self, **_kwargs):
            return None

    worker, state, _executor = _worker(tmp_path, Portal(), _Clock())
    inventory = _inventory_fixture()
    stale = {**inventory, "snapshot_revision": "5"}
    state.put(
        "commercial_inventory_state_v1",
        {
            "schema": "mcd-commercial-inventory-state-v1",
            "installations": {
                "installation-1": {
                    "snapshot_revision": 5,
                    "durable_hash": inventory_durable_hash(stale),
                    "durable_snapshot": {key: value for key, value in stale.items() if key not in {"request_id", "observed_at", "agent_signature"}},
                }
            },
        },
    )
    worker.inventory_provider = lambda _installation: _inventory_fixture(version="1.5.256")
    prepared = worker._inventory("installation-1")
    assert prepared["snapshot_revision"] == "6"
