from __future__ import annotations

import base64
import socket
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import (
    PORTAL_MCD_POLL_REQUEST_SCHEMA,
    CommercialState,
    PortalClient,
    build_portal_mcd_poll_request,
    canonical_json,
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
        publisher_keys={"publisher-1": "key"},
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
                        "request_id": "request-1",
                        "idempotency_key": "noop-key-1",
                        "expected_standalone_etag": etag,
                    },
                    {
                        "schema": "portal-mcd-command-noop-v1",
                        "command_id": "noop-2",
                        "request_id": "request-2",
                        "idempotency_key": "noop-key-2",
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
    assert receipt["status"] == "succeeded"
