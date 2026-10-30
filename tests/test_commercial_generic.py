from pathlib import Path
import copy
import pytest

from mcd_agent.commercial_generic import capability_inventory_digest, validate_generic_command_job
from mcd_agent.commercial_portal import CommercialState, CommercialPortalError
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker


def _inventory():
    unsigned = {
        "schema": "portal-mcd-capability-inventory-v1",
        "contract_version": "1.0",
        "request_id": "capability-1",
        "installation": {"installation_id": "installation-1", "dns_name": "mautic.example.com"},
        "observed_at": "2026-10-29T12:00:00Z",
        "capabilities": [
            {
                "capability_id": "web_stack",
                "version": "1.0",
                "commands": [
                    {
                        "command_id": "configure_vhost",
                        "version": "1.0",
                        "execution": "generic_primitive",
                        "mutates_state": True,
                        "input_schema_ref": "portal://mcd/input/web-vhost-v1",
                    }
                ],
            }
        ],
    }
    return {**unsigned, "inventory_sha256": capability_inventory_digest(unsigned)}


def _job():
    return {
        "schema": "portal-mcd-generic-command-job-v1",
        "contract_version": "1.0",
        "job_id": "generic-job-1",
        "request_id": "generic-request-1",
        "idempotency_key": "generic-idem-1",
        "command_id": "portal-command-1",
        "delivery_cursor": 1,
        "installation": {"installation_id": "installation-1", "dns_name": "mautic.example.com"},
        "capability": {"capability_id": "web_stack", "version": "1.0"},
        "command": {"command_id": "configure_vhost", "version": "1.0", "execution": "generic_primitive", "mutates_state": True},
        "input": {"domain": "mautic.example.com", "tls": True},
        "issued_at": "1970-01-01T00:00:00Z",
        "expires_at": "1970-01-01T00:16:40Z",
    }


class _Executor:
    def __init__(self, state):
        self.state = state
        self.runs = 0


class _Clock:
    def __call__(self):
        return 100.0


def test_generic_job_requires_latest_advertised_inventory():
    inventory = _inventory()
    assert validate_generic_command_job(_job(), inventory=inventory)["job_id"] == "generic-job-1"
    job = _job()
    job["command"]["command_id"] = "not_advertised"
    try:
        validate_generic_command_job(job, inventory=inventory)
    except ValueError as exc:
        assert str(exc) == "portal_mcd_generic_command_not_advertised"
    else:
        raise AssertionError("unadvertised command must be rejected")


def test_worker_publishes_inventory_and_durable_generic_receipts(tmp_path: Path):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    executor = _Executor(state)
    inventory = _inventory()

    class Portal:
        def __init__(self):
            self.inventory = []
            self.receipts = []
            self.polled = False

        def send_capability_inventory(self, **kwargs):
            self.inventory.append(kwargs)
            return {}

        def pull_job(self, **kwargs):
            if self.polled:
                return None
            self.polled = True
            return {"cursor": "1", "job": _job()}

        def send_receipt(self, payload, **kwargs):
            self.receipts.append(payload)
            return {}

    portal = Portal()
    calls = []
    worker = SelfHostedPortalWorker(
        portal,
        executor,
        installation_id="installation-1",
        portal_keys={},
        publisher_keys={},
        context={},
        commercial_key_id="commercial-1",
        agent_id="agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        clock=_Clock(),
        capability_inventory_provider=lambda _installation: inventory,
        generic_command_handler=lambda job: calls.append(job) or {"status": "succeeded", "changed": True},
    )

    result = worker.run_once()
    assert result == {"status": "succeeded", "job_id": "generic-job-1"}
    assert portal.inventory[0]["inventory"]["inventory_sha256"] == inventory["inventory_sha256"]
    assert [item["schema"] for item in portal.receipts] == ["portal-mcd-generic-stage-receipt-v1", "portal-mcd-generic-terminal-receipt-v1"]
    assert [item["sequence"] for item in portal.receipts] == [1, 2]
    assert len(calls) == 1
    assert state.get("commercial_portal_cursor") == "1"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_worker_terminalizes_valid_generic_job_when_capability_inventory_is_missing(tmp_path: Path, expired_cached=False, prior_sequence=0):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    executor = _Executor(state)

    class Portal:
        def __init__(self):
            self.receipts = []

        def pull_job(self, **kwargs):
            job = _job()
            job["command"] = {**job["command"], "command_id": "read_host_facts", "version": "1.0", "mutates_state": False}
            job["capability"] = {"capability_id": "host_facts", "version": "1.0"}
            job["input"] = {}
            return {"cursor": "18", "job": job}

        def send_receipt(self, payload, **kwargs):
            self.receipts.append(payload)
            return {}

    portal = Portal()
    if expired_cached:
        delivery = portal.pull_job()
        delivery["job"]["expires_at"] = "1970-01-01T00:01:40Z"
        state.put("commercial_self_hosted_pending_delivery_v1", {
            "installation_id": "installation-1", "cursor": "17",
            "cursor_next": "18", "job": delivery["job"],
        })
        if prior_sequence:
            state.put("commercial_portal_generic_runs_v1", {
                "generic-job-1": {"started_sent": True, "last_sequence": prior_sequence},
            })

        def unexpected_poll(**kwargs):
            raise AssertionError("cached delivery must not poll")

        portal.pull_job = unexpected_poll
    calls = []
    worker = SelfHostedPortalWorker(
        portal,
        executor,
        installation_id="installation-1",
        portal_keys={},
        publisher_keys={},
        context={},
        commercial_key_id="commercial-1",
        agent_id="agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        clock=_Clock(),
        generic_command_handler=lambda job: calls.append(job) or {"status": "succeeded"},
    )

    result = worker.run_once()
    assert result == {"status": "failed", "job_id": "generic-job-1"}
    assert [item["schema"] for item in portal.receipts] == ["portal-mcd-generic-terminal-receipt-v1"]
    assert portal.receipts[0]["status"] == "failed"
    expected = "portal_mcd_generic_command_expired" if expired_cached else "commercial_capability_inventory_unavailable"
    assert portal.receipts[0]["error"]["code"] == expected
    assert portal.receipts[0]["sequence"] == prior_sequence + 1
    if expired_cached:
        assert state.get("commercial_portal_generic_runs_v1")["generic-job-1"]["terminal"] == portal.receipts[0]
    assert calls == []
    assert state.get("commercial_portal_cursor") == "18"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_expired_cached_host_facts_never_invokes_handler(tmp_path: Path):
    test_worker_terminalizes_valid_generic_job_when_capability_inventory_is_missing(tmp_path, expired_cached=True)


def test_expired_cached_host_facts_after_started_uses_next_sequence(tmp_path: Path):
    test_worker_terminalizes_valid_generic_job_when_capability_inventory_is_missing(tmp_path, expired_cached=True, prior_sequence=1)


def test_expired_cached_host_facts_uses_persisted_sequence(tmp_path: Path):
    test_worker_terminalizes_valid_generic_job_when_capability_inventory_is_missing(tmp_path, expired_cached=True, prior_sequence=4)


@pytest.mark.parametrize("invalid", [None, "binding", "command_id", "job_id", "delivery_cursor", "command_status", "expiry_audit", "receipts", "unavailable"])
def test_cancelled_generic_outbox_requires_exact_authenticated_proof(tmp_path: Path, invalid):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    job = _job()
    proof = {
        "schema": "portal-mcd-command-state-v1", "contract_version": "1.0",
        "binding": {"account_id": "account-1", "agent_id": "agent-1", "installation_id": "installation-1"},
        "command_id": job["command_id"], "job_id": job["job_id"], "delivery_cursor": 1,
        "command_status": "cancelled", "job_status": "failed", "last_sequence": 1,
        "expiry_audit": {"reason": "job_ttl_expired", "created_at": "2026-10-30T00:00:00Z"},
        "receipts": [{"sequence": 1, "status": "started", "terminal": False, "payload_sha256": "sha256:" + "a" * 64}],
    }
    if invalid and invalid != "unavailable":
        proof[invalid] = {} if invalid in {"binding", "expiry_audit"} else [] if invalid == "receipts" else "wrong"

    class Portal:
        def send_receipt(self, *args, **kwargs):
            raise CommercialPortalError(stage="receipt", code="conflict", http_status=409)

        def command_state(self, **kwargs):
            assert kwargs == {"installation_id": "installation-1", "command_id": job["command_id"]}
            if invalid == "unavailable":
                raise TimeoutError()
            return proof

    worker = SelfHostedPortalWorker(Portal(), _Executor(state), installation_id="installation-1", portal_keys={}, publisher_keys={}, context={}, commercial_key_id="commercial-1", agent_id="agent-1", host_fingerprint="sha256:" + "a" * 64, clock=_Clock(), generic_command_handler=lambda _: pytest.fail("handler invoked"))
    worker._active_installation_id = "installation-1"
    worker._remember_delivery(installation_id="installation-1", cursor="0", cursor_next="1", job=job)
    receipt = worker._generic_receipt(job, terminal=True, sequence=1, status="failed", error={"code": "portal_mcd_generic_command_expired", "message": "expired"})
    original = copy.deepcopy(receipt)
    worker._queue(receipt)
    assert worker._drain() is (invalid is None)
    if invalid:
        assert worker._pending() == [original]
        assert state.get("commercial_portal_cursor") is None
        assert state.get("commercial_generic_superseded_receipts_v1") is None
    else:
        assert worker._pending() == []
        assert state.get("commercial_portal_cursor") == "1"
        record = state.get("commercial_generic_superseded_receipts_v1")[0]
        assert record["receipt"] == original
        assert record["portal_state"]["receipts"] == proof["receipts"]
