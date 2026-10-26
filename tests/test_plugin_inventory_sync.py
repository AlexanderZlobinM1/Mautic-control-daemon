from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.plugin_inventory import (
    PLUGIN_INVENTORY_PENDING_KEY,
    inventory_sha256,
    runtime_key,
)
from mcd_agent.state_push import (
    MCCStatePusher,
    _ack_plugin_inventory_sync,
    _plugin_inventory_observation,
    _plugin_inventory_sync_payload,
    _queue_plugin_inventory_sync,
    sync_plugin_inventory_after_operation,
    stable_change_payload,
)


class MemoryStore:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_runtime_sync(self, key):
        return self.values.get(key)

    def put_runtime_sync(self, key, value):
        self.values[key] = dict(value)

    def delete_runtime_sync(self, keys):
        for key in keys:
            self.values.pop(key, None)

    def close(self):
        return None


def _install(uid="instance-a", root="/var/www/a/public_html"):
    return SimpleNamespace(instance_uid=uid, root=root)


def test_failed_scan_preserves_last_known_inventory_and_marks_incomplete():
    old = {
        "schema": "mcd-plugin-inventory-observation-v1",
        "instance_uid": "instance-a",
        "root": "/var/www/a/public_html",
        "status": "ok",
        "complete": True,
        "last_known": False,
        "inventory": [{"bundle": "DemoBundle", "version": "1.0.0"}],
        "inventory_sha256": inventory_sha256([{"bundle": "DemoBundle", "version": "1.0.0"}]),
        "observed_at_utc": "2026-10-26T10:00:00Z",
    }
    store = MemoryStore({runtime_key("instance-a"): old})
    with patch("mcd_agent.state_push._scan_installed_plugins", side_effect=PermissionError("denied")):
        observed = _plugin_inventory_observation(_install(), store)

    assert observed["status"] == "error"
    assert observed["complete"] is False
    assert observed["last_known"] is True
    assert observed["inventory"] == old["inventory"]
    assert observed["inventory_sha256"] == old["inventory_sha256"]
    assert observed["scan_error"]["error_type"] == "PermissionError"


def test_successful_scan_replaces_last_known_inventory():
    store = MemoryStore()
    with patch(
        "mcd_agent.state_push._scan_installed_plugins",
        return_value=[{"bundle": "NewBundle", "version": "2.0.0"}],
    ):
        observed = _plugin_inventory_observation(_install(), store)

    assert observed["status"] == "ok"
    assert observed["complete"] is True
    assert observed["inventory"] == [{"bundle": "NewBundle", "version": "2.0.0"}]
    assert store.get_runtime_sync(runtime_key("instance-a"))["inventory"] == observed["inventory"]


def test_pending_inventory_is_multi_instance_and_deduplicated():
    store = MemoryStore()
    first = {"instance_uid": "instance-a", "root": "/a", "inventory_sha256": "sha256:a", "observed_at_utc": "t1"}
    second = {"instance_uid": "instance-b", "root": "/b", "inventory_sha256": "sha256:b", "observed_at_utc": "t1"}
    replacement = {"instance_uid": "instance-a", "root": "/a", "inventory_sha256": "sha256:c", "observed_at_utc": "t2"}
    _queue_plugin_inventory_sync(store, first, operation_id="op-a")
    _queue_plugin_inventory_sync(store, second, operation_id="op-b")
    _queue_plugin_inventory_sync(store, replacement, operation_id="op-c")

    pending = store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY)
    assert set(pending["items"]) == {"instance-a", "instance-b"}
    assert pending["items"]["instance-a"]["inventory_sha256"] == "sha256:c"

    _ack_plugin_inventory_sync(
        store,
        {"plugin_inventory_sync": {"items": [replacement]}},
    )
    pending = store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY)
    assert set(pending["items"]) == {"instance-b"}


def test_successful_operation_persists_before_terminal_sync_and_acks():
    cfg = SimpleNamespace(
        state_db_path="/tmp/not-used.sqlite",
        mcc_push_enabled=True,
        mcc_url="https://mcc.example",
        mcc_token="token",
    )
    store = MemoryStore()
    seen_pending = []

    def fake_push(*_args, **_kwargs):
        seen_pending.append(store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY))
        store.delete_runtime_sync([PLUGIN_INVENTORY_PENDING_KEY])
        return True, "ok"

    with patch(
        "mcd_agent.state_push._scan_installed_plugins",
        return_value=[{"bundle": "DemoBundle", "version": "1.0.0"}],
    ), patch("mcd_agent.state_push.push_state_now", side_effect=fake_push):
        result = sync_plugin_inventory_after_operation(
            cfg,
            _install(),
            operation_id="op-1",
            action="update",
            runtime_store=store,
        )

    assert result["inventory_sync_status"] == "ok"
    assert seen_pending and seen_pending[0]["items"]["instance-a"]["inventory_sha256"]
    assert store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY) is None


def test_failed_push_survives_restart_and_retries_until_ack():
    store = MemoryStore()
    observed = {
        "instance_uid": "instance-a",
        "root": "/a",
        "inventory_sha256": "sha256:a",
        "observed_at_utc": "t1",
    }
    _queue_plugin_inventory_sync(store, observed, operation_id="op-a")
    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example",
        mcc_token="token",
        mcc_push_interval_sec=300,
        mcc_push_on_change=False,
        state_db_path="/tmp/not-used.sqlite",
    )
    restarted = MCCStatePusher(cfg, runtime_store=store)
    assert restarted.should_push(0.0, {"schema": "mcd-state-v1"})[0] is True

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status":"ok"}'

    payload = {
        "schema": "mcd-state-v1",
        "plugin_inventory_sync": {
            "schema": "mcd-plugin-inventory-sync-v1",
            "status": "pending",
            "items": [observed],
        },
    }
    with patch("mcd_agent.state_push.request.urlopen", return_value=Response()), patch(
        "mcd_agent.state_push.mark_state_snapshot_push_result_mysql"
    ):
        assert restarted.send(payload)[0] is True
    assert store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY) is None


def test_inventory_sync_wire_payload_is_versioned_and_compact():
    store = MemoryStore()
    _queue_plugin_inventory_sync(
        store,
        {"instance_uid": "instance-a", "root": "/a", "inventory_sha256": "sha256:a", "observed_at_utc": "t1"},
        operation_id="op-a",
    )
    payload = _plugin_inventory_sync_payload(store)
    assert payload["schema"] == "mcd-plugin-inventory-sync-v1"
    assert payload["status"] == "pending"
    assert payload["items"][0]["instance_uid"] == "instance-a"


def test_observation_timestamps_do_not_trigger_change_only_pushes():
    observation = {
        "schema": "mcd-plugin-inventory-observation-v1",
        "instance_uid": "instance-a",
        "status": "ok",
        "complete": True,
        "last_known": False,
        "inventory": [{"bundle": "DemoBundle", "version": "1.0.0"}],
        "inventory_sha256": "sha256:a",
        "observed_at_utc": "2026-10-26T10:00:00Z",
        "last_known_observed_at_utc": "2026-10-26T10:00:00Z",
    }
    base = {
        "schema": "mcd-state-v1",
        "plugin_inventory_observations": [observation],
        "plugin_inventory_sync": {
            "schema": "mcd-plugin-inventory-sync-v1",
            "status": "ok",
            "items": [],
            "requested_at_utc": "2026-10-26T10:00:00Z",
            "last_attempt_at_utc": "2026-10-26T10:00:00Z",
            "retry_count": 0,
        },
        "instances": [
            {
                "instance_uid": "instance-a",
                "plugin_inventory": observation,
            }
        ],
    }
    changed_only_by_time = {
        **base,
        "plugin_inventory_observations": [
            {**base["plugin_inventory_observations"][0], "observed_at_utc": "2026-10-26T10:01:00Z"}
        ],
        "plugin_inventory_sync": {
            **base["plugin_inventory_sync"],
            "requested_at_utc": "2026-10-26T10:01:00Z",
            "last_attempt_at_utc": "2026-10-26T10:01:00Z",
        },
    }
    changed_only_by_time["instances"] = [
        {
            "instance_uid": "instance-a",
            "plugin_inventory": {
                **base["instances"][0]["plugin_inventory"],
                "observed_at_utc": "2026-10-26T10:01:00Z",
                "last_known_observed_at_utc": "2026-10-26T10:01:00Z",
            },
        }
    ]
    assert stable_change_payload(base) == stable_change_payload(changed_only_by_time)
