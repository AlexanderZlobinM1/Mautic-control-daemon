from pathlib import Path

from mcd_agent.commercial_generic import capability_inventory_digest, validate_generic_command_job
from mcd_agent.commercial_portal import CommercialState
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


def test_worker_terminalizes_valid_generic_job_when_capability_inventory_is_missing(tmp_path: Path):
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
    assert portal.receipts[0]["error"]["code"] == "commercial_capability_inventory_unavailable"
    assert calls == []
    assert state.get("commercial_portal_cursor") == "18"
    assert state.get("commercial_self_hosted_pending_delivery_v1") is None
