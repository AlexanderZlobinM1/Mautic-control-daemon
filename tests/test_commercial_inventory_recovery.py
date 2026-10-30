"""Recovery after local inventory loss must preserve facts and Portal history."""
import copy
import io
import json
from urllib.error import HTTPError

import pytest

from mcd_agent.commercial_portal import (
    CommercialState, MAX_SAFE_INVENTORY_REVISION,
    _portal_http_error_from_exception, build_standalone_settings_snapshot,
)
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker, SelfHostedScenarioExecutor


def snapshot():
    return {"schema": "portal-mcd-inventory-snapshot-v1", "contract_version": "1.0",
            "complete": True, "binding": {"installation_id": "installation-1"},
            "host": {"settings": build_standalone_settings_snapshot({"profile": "maxi"}, revision=7, sync={})},
            "instances": [{"instance_id": "actual-instance", "mautic_version": "7"}]}


def error(head=12, **overrides):
    body = {"schema": "portal-error-v1", "contract_version": "1.0",
            "code": "inventory_snapshot_revision_conflict", "latest_snapshot_revision": head,
            "private_material": "never-retain-this"}
    status = overrides.pop("status", 409)
    body.update(overrides)
    return _portal_http_error_from_exception(stage="portal.request", exc=HTTPError(
        "https://example.invalid", status, "conflict", {}, io.BytesIO(json.dumps(body).encode())))


@pytest.mark.parametrize("head", [0, 1, 12, MAX_SAFE_INVENTORY_REVISION - 1])
def test_safe_head_is_the_only_extra_error_metadata(head):
    parsed = error(head)
    assert parsed.latest_snapshot_revision == head
    assert parsed.as_dict()["latest_snapshot_revision"] == head
    assert "never-retain-this" not in repr(vars(parsed))


@pytest.mark.parametrize("head", [None, True, False, -1, 1.5, 1.0, "12", {}, [], MAX_SAFE_INVENTORY_REVISION])
def test_invalid_head_is_not_retained(head):
    assert error(head).latest_snapshot_revision is None
    assert "latest_snapshot_revision" not in error(head).as_dict()


@pytest.mark.parametrize("overrides", [{"schema": "other"}, {"contract_version": "2.0"},
                                      {"code": "idempotency_conflict"}, {"status": 400}, {"status": 403}])
def test_other_errors_cannot_authorize_rebase(overrides):
    assert error(**overrides).latest_snapshot_revision is None


def test_durable_cas_preserves_facts_other_installations_and_restart(tmp_path):
    path = str(tmp_path / "state.sqlite")
    state = CommercialState(path)
    rejected, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    other, _ = state.prepare_inventory_snapshot(installation_id="other", inventory=snapshot())
    state.acknowledge_inventory(installation_id="installation-1", inventory=rejected)
    state.put("commercial_portal_cursor", "cursor-keep")
    assert state.rebase_inventory_revision(installation_id="installation-1", inventory=rejected, latest_snapshot_revision=12)
    # A second response for the rejected request cannot advance the ledger again.
    assert not state.rebase_inventory_revision(installation_id="installation-1", inventory=rejected, latest_snapshot_revision=99)
    state = CommercialState(path)
    restored, record = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    assert restored["snapshot_revision"] == "13"
    assert {k: v for k, v in restored.items() if k != "snapshot_revision"} == snapshot()
    assert record["last_published"] is None and record["last_acknowledged"] is None
    assert state.inventory_revision("other") == int(other["snapshot_revision"])
    assert state.get("commercial_portal_cursor") == "cursor-keep"
    assert not state.acknowledge_inventory(installation_id="installation-1", inventory=rejected)


def test_inflight_local_change_wins_over_old_conflict(tmp_path):
    state = CommercialState(str(tmp_path / "state.sqlite"))
    old, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    changed = snapshot()
    changed["instances"].append({"instance_id": "new-actual-instance"})
    current, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=changed)
    assert not state.rebase_inventory_revision(installation_id="installation-1", inventory=old, latest_snapshot_revision=30)
    actual, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=changed)
    assert actual == current


def test_concurrent_conflicts_rebase_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "state.sqlite")
    state = CommercialState(path)
    rejected, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    def rebase(head):
        return CommercialState(path).rebase_inventory_revision(installation_id="installation-1", inventory=rejected, latest_snapshot_revision=head)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rebase, [12, 20]))
    assert sorted(results) == [False, True]
    assert state.inventory_revision("installation-1") in {13, 21}


def test_cold_state_different_facts_get_different_wire_identity(tmp_path):
    def prepare(name, facts):
        state = CommercialState(str(tmp_path / name))
        worker = SelfHostedPortalWorker(object(), SelfHostedScenarioExecutor(state),
            installation_id="installation-1", portal_keys={}, publisher_keys={},
            context={"identity_kind": "dns", "local_identity": "fixture.example.invalid"},
            commercial_key_id="key-record-id", agent_id="agent-1", host_fingerprint="",
            inventory_provider=lambda _: facts)
        return worker._inventory("installation-1")
    empty = snapshot()
    empty["instances"] = []
    before = prepare("before.sqlite", empty)
    after = prepare("after.sqlite", snapshot())
    assert before["snapshot_revision"] == after["snapshot_revision"] == "1"
    assert before["request_id"] != after["request_id"]


@pytest.mark.parametrize("operation", ["adopt", "inventory"])
@pytest.mark.parametrize("code", ["inventory_snapshot_revision_conflict", "inventory_snapshot_revision_stale"])
def test_worker_recovers_both_paths_with_new_request_identity(tmp_path, operation, code):
    state = CommercialState(str(tmp_path / "state.sqlite"))
    observed = []

    class Portal:
        def send(self, **kwargs):
            observed.append(copy.deepcopy(kwargs))
            if len(observed) == 1:
                raise error(12, code=code)
            return {}
        adopt = send
        send_inventory = send

    worker = SelfHostedPortalWorker(Portal(), SelfHostedScenarioExecutor(state),
        installation_id="installation-1", portal_keys={}, publisher_keys={}, context={"identity_kind": "dns", "local_identity": "fixture.example.invalid"},
        commercial_key_id="key-record-id", agent_id="agent-1", host_fingerprint="",
        inventory_provider=lambda _: snapshot())
    call = worker._ensure_adopted if operation == "adopt" else worker._publish_inventory
    assert not call("installation-1")
    assert state.inventory_revision("installation-1") == 13
    assert call("installation-1")
    before, after = [item["inventory"] for item in observed]
    assert before["request_id"] != after["request_id"]
    assert after["snapshot_revision"] == "13"
    assert before["instances"] == after["instances"] == snapshot()["instances"]
    assert before["host"] == after["host"] == snapshot()["host"]
    record = state.get("commercial_inventory_state_v1")["installations"]["installation-1"]
    assert record["last_acknowledged"]["snapshot_revision"] == 13


def test_maximum_safe_counter_cannot_overflow(tmp_path):
    state = CommercialState(str(tmp_path / "state.sqlite"))
    rejected, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    assert state.rebase_inventory_revision(installation_id="installation-1", inventory=rejected, latest_snapshot_revision=MAX_SAFE_INVENTORY_REVISION - 1)
    current, _ = state.prepare_inventory_snapshot(installation_id="installation-1", inventory=snapshot())
    assert current["snapshot_revision"] == str(MAX_SAFE_INVENTORY_REVISION)
    changed = snapshot()
    changed["instances"] = []
    with pytest.raises(ValueError, match="commercial_inventory_revision_exhausted"):
        state.prepare_inventory_snapshot(installation_id="installation-1", inventory=changed)


@pytest.mark.parametrize("wrapped", [True, False])
def test_full_version_comes_from_installed_composer_metadata(tmp_path, wrapped):
    from mcd_agent.mautic_version_cache import read_installed_mautic_version_read_only
    metadata = tmp_path / "vendor/composer/installed.json"
    metadata.parent.mkdir(parents=True)
    packages = [{"name": "unrelated/package", "version": "99.0.0"},
                {"name": "mautic/core-lib", "version": "v7.2.2"}]
    metadata.write_text(json.dumps({"packages": packages} if wrapped else packages))
    original = metadata.read_bytes()
    # A lock can describe a desired upgrade which has not been installed.
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{"name": "mautic/core-lib", "version": "8.0.0"}]}))
    assert read_installed_mautic_version_read_only(tmp_path) == "7.2.2"
    assert metadata.read_bytes() == original


@pytest.mark.parametrize("payload", [{}, {"packages": "invalid"}, {"packages": [{"name": "mautic/core-lib", "version": "dev-main"}]},
    {"packages": [{"name": "mautic/core-lib", "version": "7.2.2"}, {"name": "mautic/core-bundle", "version": "7.1.0"}]}])
def test_unconfirmed_installed_version_is_not_invented(tmp_path, payload):
    from mcd_agent.mautic_version_cache import read_installed_mautic_version_read_only
    metadata = tmp_path / "vendor/composer/installed.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps(payload))
    assert read_installed_mautic_version_read_only(tmp_path) is None
