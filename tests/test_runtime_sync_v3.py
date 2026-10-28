from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from mcd_agent import daemon
from mcd_agent.runtime_overrides import push_runtime_sync_v3
from mcd_agent.runtime_sync_v3 import (
    conflict_safe_runtime,
    intent_id,
    runtime_changes,
    three_way_merge,
)


class Store:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_runtime_sync(self, key):
        return self.values.get(key)

    def put_runtime_sync(self, key, value):
        self.values[key] = dict(value)


def test_three_way_merge_matches_mcc_v3_fixtures():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "desired-runtime-sync-v3-cross-component.json").read_text())
    for case in fixture["cases"].values():
        base = case["base"]
        central = {**base, **case["central_changes"]}
        host = {**base, **case["host_changes"]}
        result = three_way_merge(base, central, host, semantic_groups=fixture["semantic_conflict_groups"])
        assert result["status"] == case["status"]
        if result["status"] == "merged":
            assert result["merged"] == case["expected"]


def test_semantic_group_conflict_and_safe_projection_match_mcc():
    groups = [{"id": "campaign-and-trigger", "keys": ["campaign_limit", "campaign_batch"]}]
    conflict = three_way_merge(
        {"campaign_limit": 10, "campaign_batch": 2, "other": 0},
        {"campaign_limit": 20, "campaign_batch": 3, "other": 1},
        {"campaign_limit": 30, "campaign_batch": 2, "other": 0},
        semantic_groups=groups,
    )
    assert conflict["status"] == "conflict"
    safe = conflict_safe_runtime(
        {
            "base": {"campaign_limit": 10, "campaign_batch": 2, "other": 0},
            "central": {"campaign_limit": 20, "campaign_batch": 3, "other": 1},
            "host": {"campaign_limit": 30, "campaign_batch": 2, "other": 0},
            "conflicts": conflict["conflicts"],
            "semantic_conflict_groups": groups,
        },
        {"campaign_limit": 30, "campaign_batch": 2, "other": 0},
    )
    assert safe["other"] == 1
    assert safe["campaign_limit"] == 30
    assert safe["campaign_batch"] == 2


def test_runtime_changes_and_intent_id_are_complete_and_deterministic():
    base = {"a": 1, "removed": 2}
    current = {"a": 3, "added": 4}
    assert runtime_changes(base, current) == {"a": 3, "added": 4, "removed": None}
    first = intent_id(scope_key="host-a", base_revision=7, base=base, changes=runtime_changes(base, current), groups=[])
    second = intent_id(scope_key="host-a", base_revision=7, base=base, changes=runtime_changes(base, current), groups=[])
    assert first == second


def test_runtime_sync_v3_transport_declares_protocol_and_preserves_legacy_boundary(monkeypatch):
    cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token", mcc_host_name="host-a")
    seen = {}

    monkeypatch.setattr("mcd_agent.runtime_overrides.resolve_agent_identity", lambda _: {
        "effective_hostname": "host-a.local",
        "effective_mcc_host_name": "host-a",
        "local_hostname": "host-a.local",
        "configured_host_name": "host-a",
    })
    monkeypatch.setattr(
        "mcd_agent.runtime_overrides._post_json",
        lambda url, payload, token, timeout_sec: seen.update({"url": url, "payload": payload}) or {"status": "applied"},
    )
    result = push_runtime_sync_v3(
        cfg,
        base_revision=4,
        base={"a": 1},
        changes={"a": 2},
        idempotency_key="mcd-v3-test",
        semantic_conflict_groups=[],
    )
    assert result["status"] == "applied"
    assert result["v3_supported"] is True
    assert seen["url"].endswith("/api/v1/agent/runtime-sync")
    assert seen["payload"]["desired_state_sync_protocol"] == 3
    assert seen["payload"]["base_revision"] == 4
    assert seen["payload"]["origin"] == "host"


def test_daemon_v3_attempt_persists_accepted_canonical_base(monkeypatch):
    store = Store({"mcc_runtime": {"a": 1}, "mcc_runtime_desired_state": {"revision": 4}})
    cfg = SimpleNamespace(mcc_host_name="host-a")
    monkeypatch.setattr("mcd_agent.daemon.runtime_remote_allowed_keys", lambda: {"a", "b"})
    monkeypatch.setattr(
        "mcd_agent.daemon.push_runtime_sync_v3",
        lambda *args, **kwargs: {"status": "applied", "desired_state": {"revision": 5, "runtime_overrides": {"a": 2, "b": 3}}},
    )
    result = daemon._runtime_sync_v3_attempt(cfg, store, {"a": 2})
    assert result["handled"] is True
    assert result["status"] == "ok"
    state = store.get_runtime_sync("mcc_runtime_sync_v3")
    assert state["base_revision"] == 5
    assert state["base"] == {"a": 2, "b": 3}


def test_daemon_v3_conflict_is_durable_and_does_not_choose_a_winner(monkeypatch):
    store = Store({"mcc_runtime": {"a": 1}, "mcc_runtime_desired_state": {"revision": 4}})
    cfg = SimpleNamespace(mcc_host_name="host-a")
    monkeypatch.setattr("mcd_agent.daemon.runtime_remote_allowed_keys", lambda: {"a"})
    monkeypatch.setattr(
        "mcd_agent.daemon.push_runtime_sync_v3",
        lambda *args, **kwargs: {
            "status": "conflict",
            "conflict": {
                "base": {"a": 1},
                "central": {"a": 2},
                "host": {"a": 3},
                "conflicts": [{"group_id": "a", "keys": ["a"], "base": {"a": 1}, "central": {"a": 2}, "host": {"a": 3}}],
                "semantic_conflict_groups": [],
            },
        },
    )
    result = daemon._runtime_sync_v3_attempt(cfg, store, {"a": 3})
    assert result["handled"] is True
    assert result["status"] == "conflict"
    assert store.get_runtime_sync("mcc_runtime_sync_v3")["status"] == "conflict"
