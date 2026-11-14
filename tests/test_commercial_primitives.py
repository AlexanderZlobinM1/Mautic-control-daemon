import json
from pathlib import Path

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


def test_inventory_adds_upgrade_and_read_only_maintenance_with_complete_dynamic_binding(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    inventory = build_capability_inventory(
        installation_id="installation-1",
        dns_name="mautic.example.com",
        config=object(),
    )
    capability = next(item for item in inventory["capabilities"] if item["capability_id"] == "mautic_upgrade")
    command = capability["commands"][0]
    assert command == {
        "command_id": "apply",
        "version": "1.0",
        "execution": "generic_primitive",
        "mutates_state": True,
        "input_schema_ref": "portal://mcd/input/mautic-upgrade-apply-v1",
        "target_scope": "instance",
        **binding,
    }
    maintenance = next(item for item in inventory["capabilities"] if item["capability_id"] == "maintenance")
    assert maintenance["commands"] == [{
        "command_id": "read_service_status",
        "version": "1.0",
        "execution": "generic_primitive",
        "mutates_state": False,
        "input_schema_ref": "portal://mcd/input/maintenance-service-status-v1",
        "target_scope": "host",
        **binding,
    }]


def test_maintenance_status_fixture_freezes_portal_and_external_dispatch_contract(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    fixture = json.loads((
        Path(__file__).parents[1]
        / "mcd_agent/contracts/fixtures/maintenance-service-status-generic-v1.json"
    ).read_text(encoding="utf-8"))
    command = fixture["capability"]["command"]
    binding = {
        "binding_schema": command["binding_schema"],
        "binding_sha256": command["binding_sha256"],
    }
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    inventory = build_capability_inventory(
        installation_id="installation-1", dns_name="mautic.example.com", config=object(),
    )
    advertised = next(
        item["commands"][0] for item in inventory["capabilities"]
        if item["capability_id"] == fixture["capability"]["capability_id"]
    )
    assert advertised == command
    assert fixture["request_input"] == {"action": "status"}
    assert fixture["external_dispatch"]["operation_input"] == {
        "operation_id": "", "target_php_version": "", "action": "status",
        "owned_stop": "false", "dry_run": "false",
    }


def test_inventory_omits_bound_capabilities_when_upgrade_os_binding_is_unavailable(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: (_ for _ in ()).throw(RuntimeError("missing")))
    inventory = build_capability_inventory(
        installation_id="installation-1",
        dns_name="mautic.example.com",
        config=object(),
    )
    assert {item["capability_id"] for item in inventory["capabilities"]}.isdisjoint({"maintenance", "mautic_upgrade"})


def _maintenance_job(binding):
    return {
        "capability": {"capability_id": "maintenance"},
        "command": {
            "command_id": "read_service_status",
            "binding_schema": binding["binding_schema"],
            "binding_sha256": binding["binding_sha256"],
        },
        "input": {"action": "status"},
    }


def _maintenance_state(*, mutation_started=False):
    return {
        "mode": "off",
        "paused": False,
        "active": False,
        "cron_stopped": False,
        "cron_service_name": "cron",
        "cron_service_active": True,
        "cron_marker_present": False,
        "checked_at_utc": "2026-11-03T12:00:00Z",
        "external_receipt": {
            "component": "service",
            "status": "applied",
            "mutation_started": mutation_started,
            "operation_id": "status",
            "action": "status",
            "active": {"cron": True, "nginx": True, "php8.4-fpm": True},
            "command_attempts": [],
            "rollback_owner": "external_recipe",
        },
    }


def test_maintenance_service_status_generic_uses_existing_caller_and_echoes_binding(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    cfg = object()
    calls = []
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda value: binding if value is cfg else None)
    monkeypatch.setattr(primitives, "collect_maintenance_state", lambda value: calls.append(value) or _maintenance_state())
    result = execute_generic_primitive(_maintenance_job(binding), config=cfg)
    assert calls == [cfg]
    assert result == {
        "status": "succeeded",
        "action": "status",
        "binding": {"schema": binding["binding_schema"], "sha256": binding["binding_sha256"]},
        "maintenance": {key: value for key, value in _maintenance_state().items() if key != "external_receipt"},
        "external_receipt": _maintenance_state()["external_receipt"],
    }


def test_maintenance_service_status_generic_rejects_input_and_binding_before_dispatch(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    cfg = object()
    calls = []
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    monkeypatch.setattr(primitives, "collect_maintenance_state", lambda _cfg: calls.append(True) or _maintenance_state())
    for invalid in ({}, {"action": "reload"}, {"action": "status", "path": "/tmp"}):
        job = _maintenance_job(binding)
        job["input"] = invalid
        assert execute_generic_primitive(job, config=cfg) == {
            "status": "failed", "reason": "commercial_maintenance_status_input_invalid",
        }
    mismatched = _maintenance_job({**binding, "binding_sha256": "sha256:" + "b" * 64})
    assert execute_generic_primitive(mismatched, config=cfg) == {
        "status": "failed", "reason": "commercial_maintenance_status_binding_mismatch",
    }
    assert calls == []


def test_maintenance_service_status_generic_rejects_mutating_external_receipt(monkeypatch):
    from mcd_agent import commercial_primitives as primitives
    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    monkeypatch.setattr(primitives, "collect_maintenance_state", lambda _cfg: _maintenance_state(mutation_started=True))
    assert execute_generic_primitive(_maintenance_job(binding), config=object()) == {
        "status": "failed", "reason": "commercial_maintenance_status_receipt_invalid",
    }


def test_maintenance_state_status_dispatches_exact_read_only_external_operation(monkeypatch, tmp_path):
    from mcd_agent import maintenance_mode
    cfg = type("Config", (), {"scheduler_pause_flag_path": str(tmp_path / "scheduler.pause")})()
    calls = []
    receipt = _maintenance_state()["external_receipt"]
    monkeypatch.setattr(
        maintenance_mode,
        "run_upgrade_os_operation",
        lambda config, **kwargs: calls.append((config, kwargs)) or receipt,
    )
    state = maintenance_mode.collect_maintenance_state(cfg)
    assert calls == [(cfg, {
        "component": "service",
        "operation": "upgrade_service_transition",
        "operation_input": {
            "operation_id": "",
            "target_php_version": "",
            "action": "status",
            "owned_stop": "false",
            "dry_run": "false",
        },
    })]
    assert state["external_receipt"] is receipt


def test_upgrade_generic_caller_normalizes_bare_or_legacy_uid_and_echoes_plan_binding(monkeypatch, tmp_path):
    from mcd_agent import commercial_primitives as primitives
    from mcd_agent import inventory as inventory_module
    from mcd_agent import mautic_upgrade

    install = type("Install", (), {"instance_uid": "demo.example.org", "root": "/srv/demo"})()
    class FakeInventory:
        def __init__(self, _path):
            self.conn = type("Conn", (), {"close": lambda self: None})()
        def list_instances(self):
            return [install]
    monkeypatch.setattr(inventory_module, "InstanceInventory", FakeInventory)
    monkeypatch.setattr(inventory_module, "ensure_seeded", lambda *_args: 1)
    calls = []
    monkeypatch.setattr(mautic_upgrade, "run_upgrade_apply", lambda **kwargs: calls.append(kwargs) or 0)
    cfg = type("Config", (), {
        "state_db_path": str(tmp_path / "state.db"),
        "mcc_host_name": "MauticFarm-02",
    })()
    plan_sha = "sha256:" + "1" * 64
    for selector in ("demo.example.org", "demo.example.org@MauticFarm-02"):
        result = execute_generic_primitive(
            {
                "job_id": "job-1",
                "capability": {"capability_id": "mautic_upgrade"},
                "command": {"command_id": "apply"},
                "input": {
                    "instance_uid": selector,
                    "target_version": "7.2.0",
                    "mode": "composer",
                    "upgrade_plan_ref": "mcc:upgrade-plan:plan-001",
                    "upgrade_plan_sha256": plan_sha,
                },
            },
            config=cfg,
        )
        assert result["status"] == "succeeded"
        assert result["instance_uid"] == "demo.example.org"
        assert result["upgrade_plan"] == {"ref": "mcc:upgrade-plan:plan-001", "sha256": plan_sha}
    assert calls[0]["root"] == "/srv/demo"
    assert calls[0]["operation_id"] == "job-1"
    assert calls[0]["upgrade_plan_ref"] == "mcc:upgrade-plan:plan-001"
    assert calls[0]["upgrade_plan_sha256"] == plan_sha


def test_upgrade_generic_caller_rejects_legacy_uid_for_another_host(tmp_path):
    cfg = type("Config", (), {
        "state_db_path": str(tmp_path / "state.db"),
        "mcc_host_name": "MauticFarm-02",
    })()
    result = execute_generic_primitive(
        {
            "capability": {"capability_id": "mautic_upgrade"},
            "command": {"command_id": "apply"},
            "input": {
                "instance_uid": "demo.example.org@MauticFarm-01",
                "target_version": "7.2.0",
                "mode": "composer",
                "upgrade_plan_ref": "mcc:upgrade-plan:plan-001",
                "upgrade_plan_sha256": "sha256:" + "1" * 64,
            },
        },
        config=cfg,
    )
    assert result == {
        "status": "failed",
        "reason": "commercial_mautic_upgrade_instance_host_mismatch",
    }


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
