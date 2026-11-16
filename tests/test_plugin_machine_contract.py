from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from mcd_agent import plugins


def _terminal_v1(operation_id: str) -> dict[str, object]:
    return {
        "schema": "mcd-plugin-apply-result-v1",
        "operation": "plugin_apply",
        "operation_id": operation_id,
        "root": "/srv/tenant-one",
        "action": "auto",
        "event": "completed",
        "terminal": True,
        "selected": [{
            "plugin_uid": "vendor.demo:7",
            "bundle": "VendorDemoBundle",
            "install_bundle": "VendorDemoBundle",
            "expected_version": "1.2.3",
        }],
        "files_applied": True,
        "post_step_status": "success",
        "cache_inventory_confirmed": True,
        "overall_status": "success",
        "rc": 0,
        "inventory": [{"bundle": "VendorDemoBundle", "installed_version": "1.2.3"}],
        "error": "",
    }


def _read_machine_result(capsys: pytest.CaptureFixture[str]) -> tuple[str, dict[str, object]]:
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert len(lines) == 1
    prefix = "MCD_PLUGIN_OPERATION_RESULT="
    assert lines[0].startswith(prefix)
    return output, json.loads(lines[0][len(prefix):])


def test_machine_contract_emits_one_terminal_host_independent_result(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        plugins,
        "_select_install_with_db",
        lambda _config, selector: SimpleNamespace(instance_uid=selector, root="/srv/tenant-one"),
    )

    def run(**kwargs):
        calls.append(kwargs)
        print("human progress containing /srv/tenant-one and host-a")
        print("MCD_PLUGIN_APPLY_RESULT=" + json.dumps(_terminal_v1(kwargs["operation_id"])))
        return 0

    monkeypatch.setattr(plugins, "run_plugins_interactive", run)

    rc = plugins.run_plugins_machine(
        config=SimpleNamespace(),
        instance_uid="tenant-one",
        plugin_uids=["vendor.demo:7"],
        action="auto",
        operation_id="mcc-op-123",
    )

    output, result = _read_machine_result(capsys)
    assert rc == 0
    assert result["schema"] == "mcd-plugin-operation-result-v2"
    assert result["operation_id"] == "mcc-op-123"
    assert result["instance_uid"] == "tenant-one"
    assert result["plugin_uids"] == ["vendor.demo:7"]
    assert result["status"] == "success"
    assert result["terminal"] is True
    assert "/srv/tenant-one" not in output
    assert "host-a" not in output
    assert "root" not in result
    assert "host" not in result
    assert calls[0]["root"] == "tenant-one"
    assert calls[0]["operation_id"] == "mcc-op-123"

    receipt_sha = result.pop("receipt_sha256")
    canonical = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert receipt_sha == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("instance_uid", "legacy_root", "error_code"),
    [
        ("tenant-one@host-a", None, "plugin_instance_uid_must_be_bare"),
        ("tenant-one", "/srv/tenant-one", "plugin_machine_root_forbidden"),
    ],
)
def test_machine_contract_rejects_host_qualified_or_root_identity(
    monkeypatch,
    capsys,
    instance_uid,
    legacy_root,
    error_code,
):
    monkeypatch.setattr(
        plugins,
        "run_plugins_interactive",
        lambda **_kwargs: pytest.fail("plugin transaction must not start"),
    )

    rc = plugins.run_plugins_machine(
        config=SimpleNamespace(),
        instance_uid=instance_uid,
        plugin_uids=["vendor.demo:7"],
        action="auto",
        operation_id="mcc-op-123",
        legacy_root=legacy_root,
    )

    output, result = _read_machine_result(capsys)
    assert rc == 1
    assert result["status"] == "failed"
    assert result["error_code"] == error_code
    assert result["instance_uid"] == "tenant-one"
    assert "@host-a" not in output
    assert "/srv/tenant-one" not in output


def test_bare_uid_selects_local_instance_and_legacy_same_host_remains_read_compatible(monkeypatch):
    install = SimpleNamespace(instance_uid="tenant-one", root="/srv/tenant-one")
    monkeypatch.setattr(plugins, "discover_mautic", lambda *_args, **_kwargs: [install])
    monkeypatch.setattr(
        plugins,
        "resolve_agent_identity",
        lambda _config: {"effective_mcc_host_name": "host-a"},
    )
    config = SimpleNamespace(
        discovery_roots=[],
        exclude_path_contains=[],
        supported_mautic_majors=[7],
        custom_instances=[],
    )

    assert plugins._select_install_with_db(config, "tenant-one") is install
    assert plugins._select_install_with_db(config, "tenant-one@host-a") is install
    with pytest.raises(RuntimeError, match="not found"):
        plugins._select_install_with_db(config, "tenant-one@host-b")


def test_plugin_apply_selection_carries_stable_uid():
    selected = [{
        "plugin_uid": "vendor.demo:7",
        "bundle": "VendorDemoBundle",
        "install_bundle": "VendorDemoBundle",
        "item": {"version": "1.2.3"},
    }]
    assert plugins._plugin_apply_selection(selected) == [{
        "plugin_uid": "vendor.demo:7",
        "bundle": "VendorDemoBundle",
        "install_bundle": "VendorDemoBundle",
        "expected_version": "1.2.3",
    }]


def test_plugin_filesystem_closure_fails_before_transaction(monkeypatch):
    present = {"plugin_filesystem_status"}
    monkeypatch.setattr(
        plugins,
        "_plugin_filesystem_binding_present",
        lambda _config, operation: operation in present,
    )

    with pytest.raises(RuntimeError, match="plugin_filesystem_bindings_missing"):
        plugins._require_plugin_filesystem_bindings(
            SimpleNamespace(),
            plugins._PLUGIN_FILESYSTEM_REQUIRED_OPERATIONS,
        )


def test_machine_contract_same_operation_replays_deterministic_terminal_receipt(monkeypatch, capsys):
    monkeypatch.setattr(
        plugins,
        "_select_install_with_db",
        lambda _config, selector: SimpleNamespace(instance_uid=selector, root="/srv/tenant-one"),
    )

    def run(**kwargs):
        print("MCD_PLUGIN_APPLY_RESULT=" + json.dumps(_terminal_v1(kwargs["operation_id"])))
        return 0

    monkeypatch.setattr(plugins, "run_plugins_interactive", run)
    arguments = {
        "config": SimpleNamespace(),
        "instance_uid": "tenant-one",
        "plugin_uids": ["vendor.demo:7"],
        "action": "auto",
        "operation_id": "mcc-op-replay",
    }

    assert plugins.run_plugins_machine(**arguments) == 0
    _, first = _read_machine_result(capsys)
    assert plugins.run_plugins_machine(**arguments) == 0
    _, second = _read_machine_result(capsys)
    assert first == second


def test_machine_contract_preserves_terminal_failure_evidence(monkeypatch, capsys):
    monkeypatch.setattr(
        plugins,
        "_select_install_with_db",
        lambda _config, selector: SimpleNamespace(instance_uid=selector, root="/srv/tenant-one"),
    )

    def run(**kwargs):
        terminal = _terminal_v1(kwargs["operation_id"])
        terminal.update({
            "overall_status": "failed",
            "post_step_status": "failed",
            "cache_inventory_confirmed": False,
            "rc": 7,
            "inventory": [],
            "error": "native_plugin_registration_failed",
        })
        print("MCD_PLUGIN_APPLY_RESULT=" + json.dumps(terminal))
        raise RuntimeError("native_plugin_registration_failed")

    monkeypatch.setattr(plugins, "run_plugins_interactive", run)

    rc = plugins.run_plugins_machine(
        config=SimpleNamespace(),
        instance_uid="tenant-one",
        plugin_uids=["vendor.demo:7"],
        action="auto",
        operation_id="mcc-op-failed",
    )

    _, result = _read_machine_result(capsys)
    assert rc == 1
    assert result["status"] == "failed"
    assert result["terminal"] is True
    assert result["rc"] == 7
    assert result["error_code"] == "plugin_operation_failed"
    assert result["error"] == "native_plugin_registration_failed"
    assert result["selected"][0]["plugin_uid"] == "vendor.demo:7"
    assert result["inventory"] == []
