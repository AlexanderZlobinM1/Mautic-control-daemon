from mcd_agent.commercial_generic import validate_capability_inventory
from mcd_agent.commercial_primitives import build_capability_inventory, execute_generic_primitive


def test_inventory_advertises_only_safe_read_only_primitives():
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
    ]
    assert all(
        not command["mutates_state"]
        for capability in validated["capabilities"]
        for command in capability["commands"]
    )


def test_generic_primitives_reject_non_empty_input():
    result = execute_generic_primitive(
        {"command": {"command_id": "read_host_facts"}, "input": {"path": "/tmp"}}
    )
    assert result == {"status": "failed", "reason": "commercial_generic_input_must_be_empty"}

