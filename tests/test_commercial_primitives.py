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
    ]
    assert all(
        command["mutates_state"] == (command["command_id"] == "rescan_inventory")
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


def test_inventory_rescan_does_not_disclose_discovery_errors():
    def fail():
        raise RuntimeError("protected-local-config-content")

    assert execute_generic_primitive(
        {"command": {"command_id": "rescan_inventory"}, "input": {}},
        inventory_rescan=fail,
    ) == {"status": "failed", "reason": "commercial_inventory_rescan_failed"}

