from mcd_agent.commercial_generic import validate_capability_inventory
from mcd_agent.commercial_primitives import build_capability_inventory, execute_generic_primitive


def test_inventory_advertises_native_rescan_separately_from_read_only_primitives():
    inventory = build_capability_inventory(
        installation_id="installation-1",
        dns_name="mautic.example.com",
    )
    validated = validate_capability_inventory(inventory)
    commands = [
        command["command_id"]
        for capability in validated["capabilities"]
        for command in capability["commands"]
    ]
    assert sorted(commands) == [
        "check_mysql",
        "check_prerequisites",
        "check_redis",
        "check_web_php",
        "read_host_facts",
        "rescan_inventory",
        "sync_binding",
    ]
    assert all(
        command["mutates_state"] == (command["command_id"] in {"rescan_inventory", "sync_binding"})
        for capability in validated["capabilities"]
        for command in capability["commands"]
    )


def test_generic_primitives_reject_non_empty_input():
    result = execute_generic_primitive(
        {"command": {"command_id": "read_host_facts"}, "input": {"path": "/tmp"}}
    )
    assert result == {"status": "failed", "reason": "commercial_generic_input_must_be_empty"}


def test_inventory_rescan_has_no_caller_controlled_input():
    calls = []
    for value in ({"path": "/tmp"}, {"argv": ["anything"]}, {"preserve_manual": False}, None):
        result = execute_generic_primitive(
            {"command": {"command_id": "rescan_inventory"}, "input": value},
            inventory_rescan=lambda: calls.append(True) or 1,
        )
        assert result == {"status": "failed", "reason": "commercial_generic_input_must_be_empty"}
    assert calls == []


def test_inventory_rescan_fails_closed_without_native_handler():
    assert execute_generic_primitive({"command": {"command_id": "rescan_inventory"}, "input": {}}) == {
        "status": "failed", "reason": "commercial_inventory_rescan_unavailable",
    }


def test_inventory_rescan_preserves_canonical_snapshot_reference():
    result = execute_generic_primitive(
        {"command": {"command_id": "rescan_inventory"}, "input": {}},
        inventory_rescan=lambda: {
            "discovered_count": 2,
            "snapshot_reference": {
                "snapshot_revision": 4,
                "durable_hash": "sha256:" + "a" * 64,
            },
        },
    )
    assert result == {
        "status": "succeeded",
        "discovered_count": 2,
        "snapshot_reference": {
            "snapshot_revision": 4,
            "durable_hash": "sha256:" + "a" * 64,
        },
    }


def test_inventory_rescan_does_not_disclose_discovery_errors():
    def fail():
        raise RuntimeError("protected-local-config-content")

    assert execute_generic_primitive(
        {"command": {"command_id": "rescan_inventory"}, "input": {}},
        inventory_rescan=fail,
    ) == {"status": "failed", "reason": "commercial_inventory_rescan_failed"}



def test_host_hardware_observation_preserves_applicability_facts(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    facts = {"schema": "existing", "facts_digest": "unchanged"}
    monkeypatch.setattr(primitives, "read_normalized_local_facts", lambda **kw: facts)
    monkeypatch.setattr(primitives.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(primitives, "read_memory_kib", lambda: 16000000)
    result = execute_generic_primitive({"command": {"command_id": "read_host_facts"}, "input": {}})
    assert result == {"status": "succeeded", "facts": facts,
                      "hardware_observation": {"cpu_count": 8, "memory_kib": 16000000}}
    assert result["facts"] is facts


def test_host_hardware_unavailable_is_explicit_null(monkeypatch, tmp_path):
    from mcd_agent import commercial_primitives as primitives
    from mcd_agent.hardware_profile import read_memory_kib
    monkeypatch.setattr(primitives, "read_normalized_local_facts", lambda **kw: {})
    monkeypatch.setattr(primitives.os, "cpu_count", lambda: None)
    monkeypatch.setattr(primitives, "read_memory_kib", lambda: read_memory_kib(tmp_path / "missing-proc"))
    result = execute_generic_primitive({"command": {"command_id": "read_host_facts"}, "input": {}})
    assert result["hardware_observation"] == {"cpu_count": None, "memory_kib": None}
