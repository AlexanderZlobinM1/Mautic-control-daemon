from pathlib import Path
import copy
import json
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


def test_generic_job_requires_exact_advertised_external_binding():
    inventory = _inventory()
    command = inventory["capabilities"][0]["commands"][0]
    command["binding_schema"] = "mcd-connected-host-lifecycle-binding-v1"
    command["binding_sha256"] = "sha256:" + "b" * 64
    inventory["inventory_sha256"] = capability_inventory_digest(
        {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    )
    job = _job()
    job["command"]["binding_schema"] = command["binding_schema"]
    job["command"]["binding_sha256"] = command["binding_sha256"]

    validated = validate_generic_command_job(job, inventory=inventory)
    assert validated["command"]["binding_schema"] == command["binding_schema"]
    assert validated["command"]["binding_sha256"] == command["binding_sha256"]

    for field, value in (
        ("binding_schema", "mcd-connected-host-lifecycle-binding-v2"),
        ("binding_sha256", "sha256:" + "c" * 64),
    ):
        changed = copy.deepcopy(job)
        changed["command"][field] = value
        with pytest.raises(ValueError, match="portal_mcd_generic_command_not_advertised"):
            validate_generic_command_job(changed, inventory=inventory)


def test_generic_binding_fields_are_an_atomic_pair():
    inventory = _inventory()
    inventory["capabilities"][0]["commands"][0]["binding_schema"] = (
        "mcd-connected-host-lifecycle-binding-v1"
    )
    inventory["inventory_sha256"] = capability_inventory_digest(
        {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    )
    from mcd_agent.commercial_generic import validate_capability_inventory

    with pytest.raises(ValueError, match="portal_mcd_capability_inventory_invalid"):
        validate_capability_inventory(inventory)

    job = _job()
    job["command"]["binding_schema"] = "mcd-connected-host-lifecycle-binding-v1"
    with pytest.raises(ValueError, match="portal_mcd_generic_command_job_invalid"):
        validate_generic_command_job(job)


def test_capability_inventory_digest_excludes_managed_tenant_coordinate():
    from mcd_agent.commercial_generic import validate_capability_inventory

    inventory = _inventory()
    inventory["installation"]["account_id"] = "tenant-must-not-be-public"
    inventory["inventory_sha256"] = capability_inventory_digest(
        {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    )
    with pytest.raises(ValueError, match="portal_mcd_capability_inventory_digest_invalid"):
        validate_capability_inventory(inventory)

    inventory["installation"].pop("account_id")
    inventory["inventory_sha256"] = capability_inventory_digest(
        {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    )
    assert "account_id" not in validate_capability_inventory(inventory)["installation"]


def test_inventory_rescan_terminal_survives_restart_without_rescanning(tmp_path):
    from mcd_agent.commercial_primitives import build_capability_inventory, execute_generic_primitive

    state_path = str(tmp_path / "commercial.sqlite")
    inventory = build_capability_inventory(installation_id="installation-1", dns_name="mautic.example.com")
    job = _job()
    job["capability"]["capability_id"] = "inventory"
    job["command"].update(command_id="rescan_inventory", mutates_state=True)
    job["input"] = {}
    assert validate_generic_command_job(job, inventory=inventory)["command"]["mutates_state"]
    calls = []
    now = [100.0]

    class Portal:
        fail_terminal = True
        polled = False

        def __init__(self):
            self.receipts = []

        def send_capability_inventory(self, **kwargs):
            return {}

        def pull_job(self, **kwargs):
            if self.polled:
                return None
            self.polled = True
            return {"cursor": "1", "job": job}

        def send_receipt(self, payload, **kwargs):
            self.receipts.append(copy.deepcopy(payload))
            if self.fail_terminal and payload["sequence"] == 2:
                raise OSError("temporary transport failure")
            return {}

    portal = Portal()

    def worker():
        return SelfHostedPortalWorker(
            portal, _Executor(CommercialState(state_path)), installation_id="installation-1",
            portal_keys={}, publisher_keys={}, context={}, commercial_key_id="commercial-1",
            agent_id="agent-1", host_fingerprint="sha256:" + "a" * 64, clock=lambda: now[0],
            capability_inventory_provider=lambda _installation: inventory,
            generic_command_handler=lambda job: execute_generic_primitive(
                job, inventory_rescan=lambda: calls.append(True) or 1,
            ),
        )

    assert worker().run_once() == {"status": "receipt_pending", "job_id": "generic-job-1"}
    pending = CommercialState(state_path).get("commercial_self_hosted_pending_receipts")
    assert pending[0]["result"] == {"status": "succeeded", "discovered_count": 1}
    assert CommercialState(state_path).get("commercial_portal_cursor") is None
    portal.fail_terminal = False
    now[0] = 200.0
    assert worker().run_once() == {"status": "succeeded", "job_id": "generic-job-1"}
    assert calls == [True]
    assert portal.receipts[-1] == pending[0]
    assert CommercialState(state_path).get("commercial_portal_cursor") == "1"
    assert CommercialState(state_path).get("commercial_self_hosted_pending_receipts") == []


def test_transient_artifact_grant_is_consumed_before_durable_delivery(tmp_path):
    from mcd_agent.commercial_primitives import build_capability_inventory

    state_path = str(tmp_path / "commercial.sqlite")
    inventory = build_capability_inventory(
        installation_id="installation-1", dns_name="mautic.example.com",
    )
    job = _job()
    job["capability"] = {"capability_id": "external_operation_cache", "version": "1.0"}
    job["command"].update(
        command_id="sync_binding", mutates_state=True, target_scope="host",
    )
    job["input"] = {
        "catalog_item_id": "mcc.scenario.debian-family-self-hosted",
        "catalog_item_revision": "3.2.1",
        "closure_digest": "sha256:" + "a" * 64,
        "closure_size_bytes": 123,
        "closure_media_type": "application/gzip",
        "binding_schema": "mcd-mautic-connected-host-lifecycle-binding-v1",
        "binding_sha256": "sha256:" + "b" * 64,
    }
    job["artifact_delivery"] = {
        "schema": "portal-mcd-transient-artifact-delivery-v1",
        "contract_version": "1.0",
        "artifacts": [{"artifact_grant": "one-time-grant-must-not-persist"}],
    }

    class Portal:
        polled = False
        receipts = []

        def send_capability_inventory(self, **kwargs):
            return {}

        def pull_job(self, **kwargs):
            if self.polled:
                return None
            self.polled = True
            return {"cursor": "1", "job": job}

        def send_receipt(self, payload, **kwargs):
            self.receipts.append(copy.deepcopy(payload))
            return {}

    staged = []

    def stager(command):
        staged.append(copy.deepcopy(command["artifact_delivery"]))
        sanitized = dict(command)
        sanitized.pop("artifact_delivery")
        return sanitized

    portal = Portal()
    worker = SelfHostedPortalWorker(
        portal, _Executor(CommercialState(state_path)), installation_id="installation-1",
        portal_keys={}, publisher_keys={}, context={}, commercial_key_id="commercial-1",
        agent_id="agent-1", host_fingerprint="sha256:" + "a" * 64, clock=_Clock(),
        capability_inventory_provider=lambda _installation: inventory,
        generic_command_handler=lambda command: {"status": "succeeded", "synced": True},
        generic_artifact_stager=stager,
    )
    assert worker.run_once() == {"status": "succeeded", "job_id": "generic-job-1"}
    assert staged[0]["artifacts"][0]["artifact_grant"] == "one-time-grant-must-not-persist"
    assert b"one-time-grant-must-not-persist" not in Path(state_path).read_bytes()
    assert all("artifact_delivery" not in json.dumps(receipt) for receipt in portal.receipts)


@pytest.mark.parametrize("host_result", [None, "facts", "invalid"])
def test_worker_publishes_inventory_and_durable_generic_receipts(tmp_path: Path, monkeypatch, host_result):
    state = CommercialState(str(tmp_path / "commercial.sqlite"))
    executor = _Executor(state)
    inventory = _inventory()
    job = _job()
    handler = None
    if host_result:
        from mcd_agent import commercial_primitives, commercial_detector
        job["capability"]["capability_id"] = "host_facts"
        job["command"].update(command_id="read_host_facts", mutates_state=False)
        job["input"] = {}
        capability = inventory["capabilities"][0]
        capability["capability_id"] = "host_facts"
        capability["commands"][0].update(command_id="read_host_facts", mutates_state=False)
        inventory["inventory_sha256"] = capability_inventory_digest({key: value for key, value in inventory.items() if key != "inventory_sha256"})
        os_release = tmp_path / "os-release"
        os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        monkeypatch.setattr(commercial_detector.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
        probes = {name: {"installed": True, "active": False, "markers": ["path:/etc/php"]} for name in ("mautic_installation", "web_server", "database", "cache", "php_runtime")}
        facts = commercial_detector.read_normalized_local_facts(os_release_path=str(os_release), machine="x86_64", classification_probes=probes)
        from mcd_agent.commercial_generic import _scalar, GenericCommandContractError
        with pytest.raises(GenericCommandContractError, match="portal_mcd_generic_terminal_receipt_invalid"):
            _scalar({"status": "succeeded", "facts": facts}, "portal_mcd_generic_terminal_receipt_invalid")
        monkeypatch.setattr(commercial_primitives, "read_normalized_local_facts", lambda **kwargs: facts if host_result == "facts" else {"shell": "forbidden"})
        handler = commercial_primitives.execute_generic_primitive

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
            return {"cursor": "1", "job": job}

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
        generic_command_handler=lambda job: calls.append(job) or (handler(job) if handler else {"status": "succeeded", "changed": True}),
    )

    result = worker.run_once()
    assert result == {"status": "failed" if host_result == "invalid" else "succeeded", "job_id": "generic-job-1"}
    if host_result == "facts":
        assert portal.receipts[-1]["result"]["facts"]["classification_evidence"]["probes"]["php_runtime"]["markers"] == ["path:/etc/php"]
    if host_result == "invalid":
        assert portal.receipts[-1]["error"]["code"] == "commercial_generic_terminal_construction_failed"
    assert state.get("commercial_portal_generic_runs_v1")["generic-job-1"]["terminal"] == portal.receipts[-1]
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
