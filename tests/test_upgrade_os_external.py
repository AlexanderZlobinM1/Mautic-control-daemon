from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest

from mcd_agent import upgrade_os_external
from mcd_agent.custom_scripts import CustomSelectedTaskError


def _binding_rows() -> list[dict[str, object]]:
    operations = (
        ("apt", "apt_apply"),
        ("certificate", "certificate_material"),
        ("composer", "composer_host_prepare"),
        ("database", "database_prepare_import"),
        ("filesystem", "upgrade_tree_transition"),
        ("nginx", "nginx_service_transition"),
        ("php", "php_runtime_transition"),
        ("service", "upgrade_service_transition"),
    )
    return [
        {
            "component": component,
            "operation": operation,
            "artifact": {"entrypoint": f"{operation}.py", "key": "upgrade-os", "sha256": "a" * 64},
            "profile_source": {"type": "cached-json", "file": "profile.json", "sha256": "b" * 64},
            "args": ["--runtime-input-stdin"],
            "capture_result": True,
            "detach": False,
            "live_output": False,
            "offline_only": True,
            "runtime_input_schema": {
                "type": "string-map",
                "fields": ["operator_profile_json", "cfg_facts_json", "operation_input_json"],
            },
            "runtime_input_mapping": {
                "operator_profile_json": {
                    "encoding": "json",
                    "envelope_key": "profile",
                    "source": "existing_private_operator_profile",
                },
                "cfg_facts_json": {
                    "encoding": "json",
                    "fields": [],
                    "source": "effective_caller_cfg",
                },
                "operation_input_json": {
                    "encoding": "json",
                    "envelope_key": "input",
                    "source": "caller_operation_input",
                },
            },
        }
        for component, operation in operations
    ]


def test_binding_identity_is_derived_from_complete_installed_closure(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"operation_bindings": list(reversed(_binding_rows()))}),
        encoding="utf-8",
    )
    identity = upgrade_os_external.upgrade_os_binding_identity(
        SimpleNamespace(custom_cache_dir=str(tmp_path))
    )
    assert identity["binding_schema"] == "mcd-upgrade-os-binding-v1"
    assert identity["binding_sha256"].startswith("sha256:")
    assert len(identity["binding_sha256"]) == 71


def test_binding_identity_fails_when_one_operation_is_missing(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"operation_bindings": _binding_rows()[:-1]}),
        encoding="utf-8",
    )
    with pytest.raises(
        upgrade_os_external.UpgradeOSExternalError,
        match="upgrade_os_binding_closure_incomplete",
    ):
        upgrade_os_external.upgrade_os_binding_identity(
            SimpleNamespace(custom_cache_dir=str(tmp_path))
        )


def test_binding_identity_rejects_runtime_mapping_drift(tmp_path) -> None:
    rows = _binding_rows()
    rows[0]["runtime_input_mapping"] = {}
    (tmp_path / "manifest.json").write_text(
        json.dumps({"operation_bindings": rows}), encoding="utf-8"
    )
    with pytest.raises(
        upgrade_os_external.UpgradeOSExternalError,
        match="upgrade_os_binding_descriptor_invalid",
    ):
        upgrade_os_external.upgrade_os_binding_identity(
            SimpleNamespace(custom_cache_dir=str(tmp_path))
        )


def test_unbound_operation_fails_closed_without_legacy_fallback() -> None:
    cfg = SimpleNamespace(custom_cache_dir="/nonexistent")
    with patch.object(upgrade_os_external, "load_configured_profile_cache", return_value=None), patch.object(
        upgrade_os_external, "_configured_external_operation", return_value=None
    ) as dispatch:
        with pytest.raises(
            upgrade_os_external.UpgradeOSExternalError,
            match="upgrade_os_binding_not_configured",
        ):
            upgrade_os_external.run_upgrade_os_operation(
                cfg,
                component="php",
                operation="php_runtime_transition",
                operation_input={"action": "apply", "dry_run": "true"},
            )
    dispatch.assert_not_called()


def test_bound_operation_uses_only_pinned_cached_profile() -> None:
    cfg = SimpleNamespace(custom_cache_dir="/cache")
    profile = {"schema": "mcc-upgrade-os-operator-profile-v1", "version": "1.0.0"}
    receipt = {"status": "applied", "operation_id": "upgrade-1"}
    with patch.object(
        upgrade_os_external,
        "load_configured_profile_cache",
        return_value={"profile": profile, "source": {"sha256": "a" * 64}},
    ), patch.object(
        upgrade_os_external, "_configured_external_operation", return_value=receipt
    ) as dispatch:
        assert upgrade_os_external.run_upgrade_os_operation(
            cfg,
            component="filesystem",
            operation="upgrade_tree_transition",
            operation_input={"operation_id": "upgrade-1", "action": "backup"},
        ) == receipt
    dispatch.assert_called_once_with(
        "filesystem",
        profile,
        cfg=cfg,
        operation="upgrade_tree_transition",
        operation_input={"operation_id": "upgrade-1", "action": "backup"},
    )


def test_bound_terminal_error_never_falls_back() -> None:
    with patch.object(
        upgrade_os_external,
        "load_configured_profile_cache",
        return_value={"profile": {}},
    ), patch.object(
        upgrade_os_external,
        "_configured_external_operation",
        return_value={"status": "error", "reason": "profile_rejected"},
    ):
        with pytest.raises(upgrade_os_external.UpgradeOSExternalError, match="profile_rejected"):
            upgrade_os_external.run_upgrade_os_operation(
                SimpleNamespace(),
                component="service",
                operation="upgrade_service_transition",
                operation_input={"action": "quiesce", "dry_run": "false"},
            )


def test_profile_integrity_error_is_fatal_before_dispatch() -> None:
    with patch.object(
        upgrade_os_external,
        "load_configured_profile_cache",
        side_effect=CustomSelectedTaskError("custom_operation_profile_checksum_mismatch"),
    ), patch.object(upgrade_os_external, "_configured_external_operation") as dispatch:
        with pytest.raises(
            upgrade_os_external.UpgradeOSExternalError,
            match="custom_operation_profile_checksum_mismatch",
        ):
            upgrade_os_external.run_upgrade_os_operation(
                SimpleNamespace(),
                component="database",
                operation="database_prepare_import",
                operation_input={"action": "import", "dry_run": "true"},
            )
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "operation_input",
    [
        {"dry_run": True},
        {"": "true"},
        {"dry_run": "true", "facts": {"nested": "forbidden"}},
    ],
)
def test_operation_input_is_an_exact_string_map(operation_input: dict[str, object]) -> None:
    with pytest.raises(
        upgrade_os_external.UpgradeOSExternalError,
        match="upgrade_os_operation_input_invalid",
    ):
        upgrade_os_external.run_upgrade_os_operation(
            SimpleNamespace(),
            component="apt",
            operation="apt_apply",
            operation_input=operation_input,  # type: ignore[arg-type]
        )
