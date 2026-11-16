import json
from types import SimpleNamespace

import pytest

from mcd_agent import daemon


class Store:
    def __init__(self, values):
        self.values = values

    def get_runtime_sync(self, key):
        return self.values.get(key)

    def put_runtime_sync(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


def setup_intent(monkeypatch):
    key = "campaign_super_priority_instance_settings"
    base = {key: {"one": [82]}, "segment_super_priority_instance_settings": {"one": [93, 94]},
            "other": 0}
    current = {**base, "other": 1}
    store = Store({"mcc_runtime": base, "mcc_runtime_desired_state": {"revision": 202},
                   "local_runtime_push_pending": {"previous": base, "current": current}})
    monkeypatch.setattr(daemon, "runtime_remote_allowed_keys", lambda: set(base))
    clock = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    return key, base, current, store, clock


def test_queued_intent_observes_terminal_result_after_restart(monkeypatch):
    key, base, current, store, clock = setup_intent(monkeypatch)
    cfg = SimpleNamespace(mcc_host_name="host", poll_interval_sec=5)
    calls = []
    canonical = {**current, key: {"one": [78, 82]}}

    def send(*_, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"status": "queued"}
        return {"status": "idempotent",
                "intent": {"status": "applied", "idempotency_key": kwargs["idempotency_key"]},
                "desired_state": {"revision": 217, "runtime_overrides": canonical}}

    monkeypatch.setattr(daemon, "push_runtime_sync_v3", send)
    assert daemon._runtime_sync_v3_attempt(cfg, store, current)["status"] == "queued"
    assert daemon._runtime_sync_v3_attempt(cfg, store, current)["status"] == "queued"
    assert len(calls) == 1
    restarted = Store(json.loads(json.dumps(store.values)))
    restarted.values["mcc_runtime_desired_state"] = {"revision": 217}
    clock[0] += 6
    result = daemon._runtime_sync_v3_attempt(cfg, restarted, current)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[1]["base_revision"] == 202
    assert result["status"] == "ok"
    assert result["runtime"][key] == {"one": [78, 82]}
    assert result["runtime"]["segment_super_priority_instance_settings"] == {"one": [93, 94]}
    assert restarted.values["mcc_runtime_sync_v3"]["status"] == "accepted"
    assert restarted.values["mcc_runtime_sync_v3"]["base_revision"] == 217


@pytest.mark.parametrize("terminal", ["queued", "conflict"])
def test_idempotent_status_is_not_always_queued(monkeypatch, terminal):
    _, _, current, store, clock = setup_intent(monkeypatch)
    cfg = SimpleNamespace(mcc_host_name="host")
    calls = []

    def send(*_, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"status": "queued"}
        response = {"status": "idempotent",
                    "intent": {"status": terminal, "idempotency_key": kwargs["idempotency_key"]}}
        if terminal == "conflict":
            response["conflict"] = {"id": "conflict-1", "conflicts": [{"keys": ["other"]}]}
        return response

    monkeypatch.setattr(daemon, "push_runtime_sync_v3", send)
    daemon._runtime_sync_v3_attempt(cfg, store, current)
    clock[0] += 6
    assert daemon._runtime_sync_v3_attempt(cfg, store, current)["status"] == terminal
    assert store.values["mcc_runtime_sync_v3"]["status"] == terminal
    assert "local_runtime_push_pending" in store.values


def test_unknown_idempotent_result_is_rejected_without_clearing_intent(monkeypatch):
    _, _, current, store, _ = setup_intent(monkeypatch)
    monkeypatch.setattr(daemon, "push_runtime_sync_v3", lambda *a, **kw: {"status": "idempotent"})
    result = daemon._runtime_sync_v3_attempt(SimpleNamespace(mcc_host_name="host"), store, current)
    assert result["status"] == "error"
    assert store.values["mcc_runtime_sync_v3"]["reason"] == "runtime_sync_v3_intent_status_missing"
    assert store.values["mcc_runtime_sync_v3"]["changes"]
    assert "local_runtime_push_pending" in store.values


def test_applied_without_canonical_runtime_does_not_accept_empty_defaults(monkeypatch):
    _, _, current, store, _ = setup_intent(monkeypatch)
    monkeypatch.setattr(daemon, "push_runtime_sync_v3", lambda *a, **kw: {
        "status": "applied", "desired_state": {"revision": 217}})
    result = daemon._runtime_sync_v3_attempt(SimpleNamespace(mcc_host_name="host"), store, current)
    assert result["status"] == "error"
    assert store.values["mcc_runtime_sync_v3"]["reason"] == "runtime_sync_v3_terminal_missing_desired"
    assert "runtime" not in result
    assert "local_runtime_push_pending" in store.values
