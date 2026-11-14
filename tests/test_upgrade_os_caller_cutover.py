from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import amazon_mailer_dep, backup, maintenance_mode, mautic_upgrade
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


def test_backup_mydumper_package_role_uses_required_external_apt() -> None:
    cfg = SimpleNamespace(
        backup_mydumper_bin="mydumper",
        backup_mydumper_package="mydumper",
        backup_auto_install_packages=True,
    )
    with patch.object(backup, "run_upgrade_os_operation", return_value={"status": "ok"}) as dispatch:
        assert backup._install_missing_mydumper_only(cfg, [("mydumper", "mydumper")]) == ["mydumper"]
    dispatch.assert_called_once_with(
        cfg,
        component="apt",
        operation="apt_apply",
        operation_input={
            "package_role": "backup_myloader",
            "package_state": "present",
            "force_repo_rescan": "true",
            "dry_run": "false",
        },
    )


def test_backup_does_not_claim_sshfs_or_xtrabackup_package_policy() -> None:
    cfg = SimpleNamespace(
        backup_mydumper_bin="mydumper",
        backup_mydumper_package="mydumper",
        backup_auto_install_packages=True,
    )
    with patch.object(backup, "run_upgrade_os_operation") as dispatch:
        with pytest.raises(RuntimeError, match="xtrabackup"):
            backup._install_missing_mydumper_only(cfg, [("xtrabackup", "percona-xtrabackup")])
    dispatch.assert_not_called()


def test_mailer_node_prerequisite_uses_required_external_apt() -> None:
    cfg = SimpleNamespace()
    with patch.object(amazon_mailer_dep, "run_upgrade_os_operation", return_value={"status": "ok"}) as dispatch:
        amazon_mailer_dep._prepare_node_runtime(cfg)
    dispatch.assert_called_once_with(
        cfg,
        component="apt",
        operation="apt_apply",
        operation_input={
            "package_role": "node_prerequisite",
            "package_state": "present",
            "force_repo_rescan": "true",
            "dry_run": "false",
        },
    )


def test_php_runtime_transition_has_exact_string_input() -> None:
    cfg = SimpleNamespace()
    receipt = {"status": "applied"}
    with patch.object(mautic_upgrade, "_assert_php84_host_safe"), patch.object(
        mautic_upgrade, "run_upgrade_os_operation", return_value=receipt
    ) as dispatch:
        assert mautic_upgrade._apply_system_upgrade(
            "7.1.3", "7.2.0", config=cfg, upgraded_root="/srv/mautic", operation_id="op-php"
        ) == receipt
    dispatch.assert_called_once_with(
        cfg,
        component="php",
        operation="php_runtime_transition",
        operation_input={
            "operation_id": "op-php",
            "action": "apply",
            "target_php_version": "8.4",
            "previous_php_version": "8.3",
            "dry_run": "false",
        },
    )


def test_live_upgrade_plan_claim_matches_target_mode_and_digest() -> None:
    plan = {"target_version": "7.2.0", "install_type": "composer", "run_id": "plan-1"}
    mautic_upgrade._verify_upgrade_plan_claim(
        plan,
        target_version="7.2.0",
        mode="composer",
        expected_sha256="sha256:" + canonical_json_sha256(plan),
    )


@pytest.mark.parametrize("field,value", [
    ("target_version", "7.2.1"),
    ("install_type", "zip"),
    ("sha256", "sha256:" + "0" * 64),
])
def test_live_upgrade_plan_claim_mismatch_fails_closed(field: str, value: str) -> None:
    plan = {"target_version": "7.2.0", "install_type": "composer", "run_id": "plan-1"}
    target = value if field == "target_version" else "7.2.0"
    mode = value if field == "install_type" else "composer"
    digest = value if field == "sha256" else "sha256:" + canonical_json_sha256(plan)
    with pytest.raises(RuntimeError, match="upgrade_plan_claim_mismatch"):
        mautic_upgrade._verify_upgrade_plan_claim(
            plan,
            target_version=target,
            mode=mode,
            expected_sha256=digest,
        )


def test_composer_host_prepare_returns_only_external_binary() -> None:
    cfg = SimpleNamespace()
    with patch.object(
        mautic_upgrade,
        "run_upgrade_os_operation",
        return_value={
            "status": "ok",
            "composer_binary": "/opt/mcd/runtime/composer",
            "composer_dependency_commands_owned_by_mcd": True,
        },
    ) as dispatch:
        assert mautic_upgrade._prepare_composer_host(
            cfg, working_root="/srv/mautic", operation_id="op-composer"
        ) == "/opt/mcd/runtime/composer"
    dispatch.assert_called_once_with(
        cfg,
        component="composer",
        operation="composer_host_prepare",
        operation_input={
            "operation_id": "op-composer",
            "working_root": "/srv/mautic",
            "dry_run": "false",
        },
    )


def test_prepared_target_activation_uses_external_tree_transition() -> None:
    cfg = SimpleNamespace()
    prepared = SimpleNamespace(
        root=Path("/opt/mcd/runtime/prepared"),
        input={"target_version": "7.2.0", "prepared_target_id": "prepared-1"},
        artifact_sha256="sha256:" + "a" * 64,
    )
    with patch.object(mautic_upgrade, "run_upgrade_os_operation", return_value={"status": "applied"}) as dispatch, patch.object(
        mautic_upgrade, "_run"
    ), patch.object(
        mautic_upgrade, "_resolve_composer_project_root", return_value="/srv/mautic"
    ), patch.object(
        mautic_upgrade, "_doctrine_migrate_command", return_value="doctrine:migrations:migrate"
    ), patch.object(
        mautic_upgrade, "_run_doctrine_migrate_with_reconcile"
    ), patch.object(
        mautic_upgrade, "_verify_or_reconcile_doctrine_migrations"
    ):
        mautic_upgrade._apply_prepared_target(
            cfg,
            "/srv/mautic",
            "bin/console",
            "/usr/bin/php8.4",
            "7.2.0",
            prepared,
            operation_id="op-tree",
            backup_id="baseline",
        )
    dispatch.assert_called_once_with(
        cfg,
        component="filesystem",
        operation="upgrade_tree_transition",
        operation_input={
            "operation_id": "op-tree",
            "instance_root": "/srv/mautic",
            "prepared_root": "/opt/mcd/runtime/prepared",
            "backup_id": "baseline",
            "action": "activate",
            "dry_run": "false",
        },
    )


def test_nginx_status_uses_external_service_transition() -> None:
    cfg = SimpleNamespace()
    with patch.object(
        mautic_upgrade,
        "run_upgrade_os_operation",
        return_value={"status": "ok", "required_runtime_active": True},
    ) as dispatch:
        mautic_upgrade._verify_nginx_and_php_services(cfg, target_php_version="8.4")
    dispatch.assert_called_once_with(
        cfg,
        component="nginx",
        operation="nginx_service_transition",
        operation_input={
            "operation_id": "",
            "target_php_version": "8.4",
            "action": "status",
            "dry_run": "false",
        },
    )


def test_maintenance_quiesce_uses_owned_external_service_receipt(tmp_path: Path) -> None:
    cfg = SimpleNamespace(scheduler_pause_flag_path=str(tmp_path / "scheduler.pause"))
    with patch.object(maintenance_mode, "run_upgrade_os_operation", return_value={
        "status": "applied", "cron_service": "cron", "was_active": True, "owned_stop": True,
    }) as dispatch:
        result = maintenance_mode.stop_cron_service(
            cfg, operation_id="op-service", target_php_version="8.4"
        )
    assert result["cron_stopped"] is True
    dispatch.assert_called_once_with(
        cfg,
        component="service",
        operation="upgrade_service_transition",
        operation_input={
            "operation_id": "op-service",
            "target_php_version": "8.4",
            "action": "quiesce",
            "owned_stop": "false",
            "dry_run": "false",
        },
    )


def test_cutover_sources_contain_no_concrete_package_service_or_certificate_fallback() -> None:
    root = Path(__file__).parents[1]
    sources = {
        relative: (root / relative).read_text(encoding="utf-8")
        for relative in (
            "mcd_agent/mautic_upgrade.py",
            "mcd_agent/mautic_image_install.py",
            "mcd_agent/amazon_mailer_dep.py",
            "mcd_agent/maintenance_mode.py",
            "mcd_agent/backup.py",
        )
    }
    for relative, source in sources.items():
        for forbidden in ("apt-get", "deb.nodesource.com", "/etc/letsencrypt", "_run_certbot"):
            assert forbidden not in source, f"{relative}: {forbidden}"
    for relative in (
        "mcd_agent/mautic_upgrade.py",
        "mcd_agent/mautic_image_install.py",
        "mcd_agent/maintenance_mode.py",
    ):
        assert "systemctl" not in sources[relative]
