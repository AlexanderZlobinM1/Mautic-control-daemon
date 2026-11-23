from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import mautic_composer_executor as executor


def _request(tmp_path: Path, *, steps=None, phases=None) -> dict[str, object]:
    phases = list(phases or [])
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    plan.chmod(0o600)
    pre_cache = {
        "post_source_install",
        "before_campaign_trigger",
        "before_campaign_execution",
        "before_background_imports",
    }
    step_rows = steps or [
        "composer_json_update",
        "composer_update",
        *[
            {"kind": "patch_phase", "phase": phase}
            for phase in phases
            if phase in pre_cache
        ],
        "cache_clear",
        *[
            {"kind": "patch_phase", "phase": phase}
            for phase in phases
            if phase not in pre_cache
        ],
        "mautic_finish",
        "doctrine_migrate",
        "verify",
    ]
    return {
        "schema": executor.SCHEMA,
        "contract_version": executor.CONTRACT_VERSION,
        "run_id": "composer-run-1",
        "instance_uid": "instance-1",
        "root": str(tmp_path / "instance"),
        "source_version": "6.0.1",
        "target_version": "6.0.2",
        "steps": step_rows,
        "patch_plan": {
            "plan_file": str(plan) if phases else "",
            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest() if phases else "",
            "run_id": "patch-run-1" if phases else "",
            "phases": phases,
        },
        "backup": {"required": True},
        "cron_policy": "unchanged",
    }


def test_contract_rejects_shell_and_argv_fields(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["argv"] = ["composer", "update"]
    with pytest.raises(executor.ComposerExecutorError, match="forbidden_request_field"):
        executor.validate_request(request)


def test_contract_rejects_unknown_fields_at_every_boundary(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["unexpected"] = "must-fail"
    with pytest.raises(executor.ComposerExecutorError, match="unknown_request_field:request.unexpected"):
        executor.validate_request(request)
    request = _request(tmp_path)
    request["patch_plan"]["unexpected"] = "must-fail"  # type: ignore[index]
    with pytest.raises(executor.ComposerExecutorError, match="unknown_request_field:patch_plan.unexpected"):
        executor.validate_request(request)


def test_contract_binds_patch_phase_order_and_allows_empty_patch_plan(tmp_path: Path) -> None:
    request = _request(tmp_path, phases=["post_source_install", "before_cache_warmup"])
    normalized = executor.validate_request(request)
    assert normalized["patch_plan"]["phases"] == ["post_source_install", "before_cache_warmup"]

    empty = executor.validate_request(_request(tmp_path))
    assert empty["patch_plan"]["phases"] == []


def test_contract_rejects_patch_phase_in_wrong_position(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        steps=[
            "composer_json_update",
            "composer_update",
            "cache_clear",
            {"kind": "patch_phase", "phase": "post_source_install"},
            "mautic_finish",
            "doctrine_migrate",
            "verify",
        ],
        phases=["post_source_install"],
    )
    with pytest.raises(executor.ComposerExecutorError, match="patch_phase_position_invalid"):
        executor.validate_request(request)


def test_request_file_requires_exact_digest_and_private_mode(tmp_path: Path) -> None:
    request = _request(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert executor.load_request_file(str(path), digest)["run_id"] == "composer-run-1"
    with pytest.raises(executor.ComposerExecutorError, match="request_file_digest_invalid"):
        executor.load_request_file(str(path), "0" * 64)


def test_executor_holds_and_restores_maintenance_before_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "instance"
    root.mkdir()
    (root / "composer.json").write_text('{"require":{"mautic/core":"6.0.1"}}', encoding="utf-8")
    request = _request(tmp_path)
    request["root"] = str(root)
    cfg = SimpleNamespace(
        scheduler_pause_flag_path=str(tmp_path / "scheduler.pause"),
        php_bin="/usr/bin/php",
        mautic_run_as_user="www-data",
        state_db_path="",
    )
    inst = SimpleNamespace(instance_uid="instance-1", root=str(root), console_path=str(root / "bin" / "console"))
    versions = iter(["6.0.1", "6.0.2"])
    exited: list[object] = []
    monkeypatch.setattr(executor.upgrade, "_pick_install_record", lambda _cfg, _root: inst)
    monkeypatch.setattr(executor.upgrade, "_read_current_version", lambda *args: next(versions))
    monkeypatch.setattr(executor.upgrade, "_resolve_composer_project_root", lambda _root: str(root))
    monkeypatch.setattr(executor.upgrade, "_enter_upgrade_maintenance", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(executor.upgrade, "_exit_upgrade_maintenance", lambda _cfg, guard: exited.append(guard) or {"cron_restored": True, "scheduler_resumed": True})
    monkeypatch.setattr(executor.upgrade, "_backup_install", lambda _root: tmp_path / "backup.tar.gz")
    monkeypatch.setattr(executor, "_run_composer_update", lambda *args: [])
    monkeypatch.setattr(executor.upgrade, "_clear_prod_cache_with_fallback", lambda *args: None)
    monkeypatch.setattr(executor.upgrade, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor.upgrade, "_doctrine_migrate_command", lambda *args: "doctrine:migrations:migrate")
    monkeypatch.setattr(executor.upgrade, "_run_doctrine_migrate_with_reconcile", lambda *args: None)
    monkeypatch.setattr(executor.upgrade, "_verify_or_reconcile_doctrine_migrations", lambda *args: None)
    monkeypatch.setattr(executor.upgrade, "_post_upgrade_verify", lambda *args: None)
    monkeypatch.setattr(executor.upgrade, "_write_upgrade_version_cache", lambda *args: 1)

    rc, receipt = executor.execute_request(cfg, request)
    assert rc == 0
    assert receipt["status"] == "success"
    assert receipt["maintenance_guard"] == {"cron_restored": True, "scheduler_resumed": True}
    assert len(exited) == 1
