from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from mcd_agent import mautic_manual_upgrade as manual
from mcd_agent import mautic_upgrade as upgrade


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    root = (tmp_path / "project")
    root.mkdir()
    root = root.resolve()
    metadata = root / "docroot/app/bundles/CoreBundle/release_metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"version":"7.1.3"}')
    (root / "composer.json").write_text('{"name":"mautic/recommended-project","require":{"mautic/core-lib":"7.1.3"}}')
    (root / "composer.lock").write_text('{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}')
    fixture = Path(__file__).parents[1] / "mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json"
    plan = json.loads(fixture.read_text())["resolve_response"]["plan"]
    plan.update(source_version="7.1.3", target_version="7.1.4", trigger="upgrade_lifecycle", phase="dependency_update_preflight", run_id="manual-job-72")
    plan["patches"][0]["triggers"] = ["upgrade_lifecycle"]
    plan["patches"][0]["phases"] = ["post_source_install"]
    monkeypatch.setattr(manual.os, "geteuid", lambda: 0)
    return dict(root=str(root), install_root=str(root), current="7.1.3", target="7.1.4",
                mode="composer", raw_plan=json.dumps(plan), run_id="manual-job-72",
                yes=True, allow_minor=True, allow_major=False, do_backup=True, with_system_upgrade=False)


def test_exact_manual_invocation_is_valid(invocation):
    manual.validate_preflighted_single_instance(**invocation)


def test_manual_forward_same_major_latest_without_applicable_patch_is_valid(invocation):
    invocation.update(target="7.1.5", raw_plan=None, run_id=None)
    manual.validate_preflighted_single_instance(**invocation)


@pytest.mark.parametrize("target", ["7.0.9", "8.0.0"])
def test_manual_downgrade_or_cross_major_is_rejected(invocation, target):
    invocation.update(target=target, raw_plan=None, run_id=None)
    with pytest.raises(manual.ManualUpgradePreflightError, match="newer|major-version"):
        manual.validate_preflighted_single_instance(**invocation)


def test_run_id_without_selected_patch_plan_is_rejected(invocation):
    invocation.update(raw_plan=None)
    with pytest.raises(manual.ManualUpgradePreflightError, match="patch-run-id"):
        manual.validate_preflighted_single_instance(**invocation)


@pytest.mark.parametrize("key,value", [
    ("root", None), ("root", "project"), ("root", "/"),
    ("current", "7.1.2"), ("current", "7.1.4"), ("target", None),
    ("target", "7.1.5"), ("target", "8.0.0"), ("mode", "auto"),
    ("mode", "zip"), ("raw_plan", None), ("raw_plan", "{}"),
    ("raw_plan", " " * 16385), ("run_id", None), ("run_id", ""),
    ("run_id", "../other"), ("run_id", "x" * 97), ("yes", False),
    ("allow_minor", False), ("allow_major", True),
])
def test_manual_input_mismatch_rejected(invocation, key, value):
    invocation[key] = value
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


def test_non_root_rejected(invocation, monkeypatch):
    monkeypatch.setattr(manual.os, "geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="root execution"):
        manual.validate_preflighted_single_instance(**invocation)


def test_manual_php84_requires_7x_backup_and_immutable_plan(invocation):
    invocation["with_system_upgrade"] = True
    manual.validate_preflighted_single_instance(**invocation)
    with pytest.raises(RuntimeError, match="requires --backup"):
        manual.validate_preflighted_single_instance(**{**invocation, "do_backup": False})
    with pytest.raises(RuntimeError, match="immutable selected patch plan"):
        manual.validate_preflighted_single_instance(**{**invocation, "raw_plan": None, "run_id": None})
    with pytest.raises(RuntimeError, match="same-major Mautic 7"):
        manual.validate_preflighted_single_instance(**{**invocation, "current": "6.0.9", "target": "6.0.10"})


def test_manual_php84_capability_is_only_forward_7x():
    assert manual.manual_system_upgrade_capability("7.1.3", "7.1.4")["supported"] is True
    assert manual.manual_system_upgrade_capability("7.1.4", "7.1.3")["supported"] is False
    assert manual.manual_system_upgrade_capability("6.0.9", "6.0.10")["supported"] is False


def test_same_version_has_stable_no_mutation_reason(invocation):
    invocation.update(current="7.1.4", target="7.1.4")
    with pytest.raises(manual.ManualUpgradePreflightError) as caught:
        manual.validate_preflighted_single_instance(**invocation)
    assert caught.value.reason == "target_already_installed"


def test_noncanonical_alias_and_wrong_instance_rejected(invocation, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(invocation["root"], target_is_directory=True)
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**{**invocation, "root": str(alias)})
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**{**invocation, "install_root": str(tmp_path)})


@pytest.mark.parametrize("mutation", ["duplicate", "float", "unknown", "revision", "source", "mode", "order"])
def test_strict_patch_plan_rejected(invocation, mutation):
    plan = json.loads(invocation["raw_plan"])
    if mutation == "duplicate":
        invocation["raw_plan"] = '{"schema":"mcd-mautic-patch-plan-v1",' + invocation["raw_plan"].lstrip()[1:]
    else:
        if mutation == "float":
            plan["patches"][0]["phase_order"] = 10.0
        elif mutation == "unknown":
            plan["commands"] = []
        elif mutation == "revision":
            plan["registry_revision"] = "unknown"
        elif mutation == "source":
            plan["source_version"] = "7.1.2"
        elif mutation == "mode":
            plan["install_type"] = "zip"
        else:
            plan["patches"].append(dict(plan["patches"][0]))
        invocation["raw_plan"] = json.dumps(plan)
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


@pytest.mark.parametrize("missing", [False, True])
def test_source_metadata_ambiguity_or_absence_rejected(invocation, missing):
    root = Path(invocation["root"])
    metadata = root / "docroot/app/bundles/CoreBundle/release_metadata.json"
    if missing:
        metadata.unlink()
        (root / "composer.lock").unlink()
    else:
        metadata.write_text('{"version":"7.1.2"}')
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


@dataclass
class Install:
    root: str
    instance_uid: str = "fixture-instance"
    console_path: str = "bin/console"
    runtime: str = "host"
    mautic_major: int = 7


def wire_upgrade(invocation, monkeypatch, kind="composer"):
    root = Path(invocation["root"])
    if kind == "zip":
        (root / "composer.json").write_text('{"name":"mautic/core","require":{}}')
        (root / "docroot/app").rename(root / "app")
        (root / "docroot").rmdir()
        plan = json.loads(invocation["raw_plan"])
        plan["install_type"] = "zip"
        invocation.update(mode="zip", raw_plan=json.dumps(plan))
    events = []
    monkeypatch.setattr("mcd_agent.mautic_patch_stage.application_root", lambda root: Path(root))
    monkeypatch.setattr("mcd_agent.mautic_patch_plan_v3.verify_applied", lambda *a, **kw: {"status": "success"})
    monkeypatch.setattr(upgrade, "_prepare_patch_target_stage", lambda *a, **kw: events.append("stage") or SimpleNamespace(close=lambda: None, verify_original=lambda *a: None))
    installed = [False]
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data",
                          state_db_path=str(root / "state.db"),
                          mcc_url="https://mcc.example.test", mcc_token="test-shared-token")
    monkeypatch.setattr(upgrade, "_pick_install_record", lambda *a: Install(str(root)))
    monkeypatch.setattr(upgrade, "_read_current_version", lambda *a: invocation["target"] if installed[0] else "7.1.3")

    def deny(request, **kwargs):
        events.append("global_authorization")
        raise HTTPError(request.full_url, 409, "release blocked", {}, None)

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", deny)
    monkeypatch.setattr(upgrade, "_enter_upgrade_maintenance", lambda *a, **kw: events.append("maintenance") or object())
    monkeypatch.setattr(upgrade, "_exit_upgrade_maintenance", lambda *a: events.append("cleanup"))
    monkeypatch.setattr(upgrade, "_pre_upgrade_permissions_check", lambda *a: events.append("permissions"))
    monkeypatch.setattr(upgrade, "revert_mautic713_import_tag_patch", lambda *a: events.append("revert") or {"status": "clean"})
    monkeypatch.setattr(upgrade, "_prepare_composer_host", lambda *a, **kw: "/usr/bin/composer")

    def external(_config, *, component, operation, operation_input):
        if (component, operation, operation_input.get("action")) == (
            "filesystem", "upgrade_tree_transition", "backup"
        ):
            events.append("baseline_backup")
            return {"status": "ok", "backup_ref": "verified-backup"}
        return {"status": "ok"}

    monkeypatch.setattr(upgrade, "run_upgrade_os_operation", external)

    def install(*args, **kwargs):
        events.append("install")
        installed[0] = True

    monkeypatch.setattr(upgrade, "_apply_zip", install)
    monkeypatch.setattr(upgrade, "_apply_composer", install)
    for name in ("ensure_mailer_packages_for_sender_config", "ensure_amazon_mailer_for_bundles", "_post_upgrade_verify", "_verify_assetmapper_upgrade"):
        monkeypatch.setattr(upgrade, name, lambda *a, **kw: None)
    monkeypatch.setattr(upgrade, "installed_required_bundles", lambda *a: [])
    monkeypatch.setattr(upgrade, "ensure_import_tag_patch", lambda *a: {"status": "already"})
    monkeypatch.setattr(upgrade, "_write_upgrade_version_cache", lambda *a: 0)
    args = dict(config=cfg, root=str(root), mode=kind, yes=True, do_backup=False,
                with_system_upgrade=False, target_override=invocation["target"], allow_minor=True,
                patch_plan_json=invocation["raw_plan"], patch_run_id=invocation["run_id"])
    return args, events


@pytest.mark.parametrize("kind", ["zip", "composer"])
def test_explicit_manual_flow_skips_both_callbacks(invocation, monkeypatch, kind):
    args, events = wire_upgrade(invocation, monkeypatch, kind)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events == ["stage", "maintenance", "permissions", "install", "cleanup"]


def test_manual_latest_without_patch_plan_uses_regular_upgrade_path(invocation, monkeypatch):
    invocation.update(target="7.1.5", raw_plan=None, run_id=None)
    args, events = wire_upgrade(invocation, monkeypatch)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events == ["maintenance", "permissions", "install", "cleanup"]


def test_same_major_upgrade_keeps_requested_baseline_backup(invocation, monkeypatch):
    invocation.update(target="7.1.5", raw_plan=None, run_id=None)
    args, events = wire_upgrade(invocation, monkeypatch)
    args["do_backup"] = True
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events.index("baseline_backup") < events.index("install")


def test_manual_php84_runs_after_verified_mautic_update(invocation, monkeypatch, capsys):
    args, events = wire_upgrade(invocation, monkeypatch)
    args.update(do_backup=True, with_system_upgrade=True)
    monkeypatch.setattr(upgrade, "_assert_php84_host_safe", lambda *a: events.append("host_preflight"))
    monkeypatch.setattr(upgrade, "_apply_system_upgrade", lambda *a, **kw: events.append("system"))
    monkeypatch.setattr(upgrade, "rebind_php_after_system_upgrade", lambda value: value)
    monkeypatch.setattr(upgrade, "replace", lambda config, **kw: config)
    monkeypatch.setattr(upgrade, "_verify_nginx_and_php_services", lambda *a, **kw: {"cli_version": "8.4.1", "fpm_version": "8.4", "nginx_socket": ["/run/php/php8.4-fpm.sock"]})
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events == ["host_preflight", "stage", "maintenance", "permissions", "baseline_backup", "install", "system", "cleanup"]
    evidence = [
        json.loads(line.split("=", 1)[1])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("MCD_MANUAL_SYSTEM_UPGRADE_EVIDENCE=")
    ]
    assert evidence == [
        {
            "schema": "mcd-mautic-manual-system-upgrade-execution-v1",
            "status": "success",
            "requested": True,
            "target_php": "8.4",
            "binding": {
                "instance_uid": "fixture-instance", "root": invocation["root"],
                "source_version": "7.1.3", "target_version": "7.1.4",
                "patch_run_id": "manual-job-72", "plan_sha256": evidence[0]["binding"]["plan_sha256"],
                "backup_path": "verified-backup",
            },
            "host_compatibility": {"ok": True, "reason": "all discovered host instances are PHP 8.4 compatible"},
            "observed": {"cli_version": "8.4.1", "fpm_version": "8.4", "nginx_socket": ["/run/php/php8.4-fpm.sock"]},
            "rollback": {"attempted": False, "succeeded": False},
        }
    ]


@pytest.mark.parametrize("kind", ["zip", "composer"])
def test_ordinary_blocked_release_never_mutates_source(invocation, monkeypatch, kind):
    args, events = wire_upgrade(invocation, monkeypatch, kind)
    with pytest.raises(RuntimeError, match="not authorized"):
        upgrade.run_upgrade_apply(**args)
    assert events == ["global_authorization"]


def test_ordinary_latest_above_pin_still_requires_release_authorization(invocation, monkeypatch):
    invocation.update(target="7.1.5", raw_plan=None, run_id=None)
    args, events = wire_upgrade(invocation, monkeypatch)
    with pytest.raises(RuntimeError, match="not authorized"):
        upgrade.run_upgrade_apply(**args)
    assert events == ["global_authorization"]


def test_invalid_manual_plan_rejected_before_maintenance(invocation, monkeypatch, capsys):
    args, events = wire_upgrade(invocation, monkeypatch)
    args["patch_plan_json"] = "{}"
    with pytest.raises(RuntimeError):
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True)
    assert events == []
    evidence = [
        json.loads(line.split("=", 1)[1])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("MCD_PATCH_PLAN_EVIDENCE=")
    ]
    assert len(evidence) == 1
    assert evidence[0]["schema"] == "mcd-mautic-patch-preflight-v1"
    assert evidence[0]["operation"] == "patch_preflight"
    assert evidence[0]["run_id"] == invocation["run_id"]
    assert evidence[0]["status"] == "error"
    assert evidence[0]["reason"] == "invalid_manual_preflight"
    assert evidence[0]["upgrade_started"] is False
    assert evidence[0]["selected"] == evidence[0]["applied"] == evidence[0]["phases"] == []
    assert evidence[0]["rollback_attempted"] is False


def test_same_version_manual_job_emits_terminal_rejection_before_maintenance(
    invocation, monkeypatch, capsys
):
    args, events = wire_upgrade(invocation, monkeypatch)
    monkeypatch.setattr(upgrade, "_read_current_version", lambda *a: "7.1.4")

    with pytest.raises(manual.ManualUpgradePreflightError) as caught:
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True)

    assert caught.value.reason == "target_already_installed"
    assert events == []
    evidence = [
        json.loads(line.split("=", 1)[1])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("MCD_PATCH_PLAN_EVIDENCE=")
    ]
    assert len(evidence) == 1
    assert evidence[0]["reason"] == "target_already_installed"
    assert evidence[0]["status"] == "error"
    assert evidence[0]["upgrade_started"] is False
    assert evidence[0]["rollback_attempted"] is False
    assert evidence[0]["rollback_succeeded"] is False
    assert evidence[0]["hard_incident"] is False


def test_source_change_during_maintenance_aborts_before_first_mutation(invocation, monkeypatch):
    args, events = wire_upgrade(invocation, monkeypatch)

    def changed(*a, **kw):
        events.append("maintenance")
        (Path(invocation["root"]) / "composer.lock").write_text('{"packages":[{"name":"mautic/core-lib","version":"7.1.2"}]}')
        return object()

    monkeypatch.setattr(upgrade, "_enter_upgrade_maintenance", changed)
    with pytest.raises(RuntimeError):
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True)
    assert events == ["stage", "maintenance", "cleanup"]


@pytest.mark.parametrize("operation", ["check", "interactive"])
def test_cli_cannot_ignore_manual_flag_on_other_operations(monkeypatch, operation):
    from mcd_agent import cli
    monkeypatch.setattr("sys.argv", ["mcd-cli", "mautic-upgrade", operation, "--mcc-preflighted-single-instance"])
    assert cli.main() == 2


def test_cli_forwards_manual_flag_and_exact_job_fields(invocation, monkeypatch):
    from mcd_agent import cli
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda *a: object())
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *a: None)
    monkeypatch.setattr(cli, "run_upgrade_apply", lambda **kw: captured.update(kw) or 2)
    monkeypatch.setattr("sys.argv", ["mcd-cli", "mautic-upgrade", "apply", "--root", invocation["root"],
        "--mode", "composer", "--target", "7.1.4", "--allow-minor", "--yes",
        "--patch-plan-json", invocation["raw_plan"], "--patch-run-id", invocation["run_id"],
        "--mcc-preflighted-single-instance"])
    assert cli.main() == 2
    assert captured["mcc_preflighted_single_instance"] is True
    assert captured["root"] == invocation["root"]
    assert captured["patch_plan_json"] == invocation["raw_plan"]
    assert captured["patch_run_id"] == invocation["run_id"]


@pytest.mark.parametrize("preflighted", [False, True])
def test_cross_line_never_uses_minor_flag_or_preflight_as_authorization(invocation, monkeypatch, preflighted):
    args, events = wire_upgrade(invocation, monkeypatch)
    args["target_override"] = "7.2.0"
    with pytest.raises(RuntimeError):
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=preflighted)
    assert events == []


def test_cross_line_live_authorization_repeated_before_mutation(invocation, monkeypatch):
    from mcd_agent import mautic_release_authorization as auth
    args, events = wire_upgrade(invocation, monkeypatch)
    plan = json.loads(args["patch_plan_json"])
    plan["target_version"] = "7.2.0"
    args.update(target_override="7.2.0", patch_plan_json=json.dumps(plan),
                allow_release_transition=True, mcc_release_authorization_context_file="private-file",
                mcc_release_authorization_context_sha256="a" * 64)
    invocation["target"] = "7.2.0"
    requirements = {"minimum_agent_version": "1.2.67", "install_types": ["composer"], "phases": ["upgrade"],
                    "requires_backup": False, "requires_json_repair": False, "system_upgrade_supported": False,
                    "requires_latest_source": True, "database_compatibility": ""}
    monkeypatch.setattr(auth, "read_context", lambda *a: {"transition_requirements": requirements})
    expected = []
    def authorize(config, context, sha, binding):
        expected.append(binding)
        events.append("transition_authorization")
    monkeypatch.setattr(auth, "authorize", authorize)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events.count("transition_authorization") >= 4
    assert events.index("transition_authorization") < events.index("maintenance")
    assert events[events.index("permissions") - 1] == "transition_authorization"
    assert events[events.index("install") - 1] == "transition_authorization"
    assert all(row["phase"] == "upgrade" and row["patch_run_id"] == args["patch_run_id"] for row in expected)
    assert "global_authorization" not in events
