import json
import inspect
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import self_update
from mcd_agent.source_operation import stable_source_operation
from test_mautic_manual_upgrade import invocation, wire_upgrade


def _operation(state_path, entered, finish):
    @stable_source_operation
    def body(*, config):
        entered.set()
        if not finish.wait(8):
            raise RuntimeError("test synchronization timed out")
    body(config=SimpleNamespace(state_db_path=state_path))


def _cfg(tmp_path):
    return SimpleNamespace(state_db_path=str(tmp_path / "state.db"))


def _plan():
    return {"status": "update", "target": "1.2.99",
            "package_url": "https://example.test/package", "session_id": "test-session"}


def test_python_operation_without_console_defers_update_and_preserves_retry(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    entered, finish = context.Event(), context.Event()
    worker = context.Process(target=_operation, args=(str(tmp_path / "state.db"), entered, finish))
    mutations, releases = [], []
    monkeypatch.setattr(self_update, "installed_agent_version", lambda: "1.2.80")
    monkeypatch.setattr(self_update, "release_session", lambda *a, **kw: releases.append(kw))
    monkeypatch.setattr(self_update, "_apply_update_locked", lambda *a: (mutations.append("update") or True, "updated"))
    worker.start()
    try:
        assert entered.wait(5)
        ok, message = self_update.apply_update(_cfg(tmp_path), _plan())
        assert not ok and "deferred" in message
        assert mutations == []
        assert releases[0]["result_status"] == "deferred"
        state = json.loads((tmp_path / "mcd-self-update.json").read_text())
        assert state["last_status"] == "deferred_source_operation"
    finally:
        finish.set()
        worker.join(8)
    assert worker.exitcode == 0
    assert self_update.apply_update(_cfg(tmp_path), _plan())[0]
    assert mutations == ["update"]


def test_update_exclusive_lease_blocks_new_operation_until_complete(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    entered, finish = context.Event(), context.Event()
    finish.set()
    worker = context.Process(target=_operation, args=(str(tmp_path / "state.db"), entered, finish))
    def mutation(*args):
        worker.start()
        assert not entered.wait(0.25)
        return True, "updated"
    monkeypatch.setattr(self_update, "_apply_update_locked", mutation)
    assert self_update.apply_update(_cfg(tmp_path), _plan())[0]
    assert entered.wait(5)
    worker.join(8)
    assert worker.exitcode == 0


def test_failed_operation_releases_lease_without_stale_busy_state(tmp_path, monkeypatch):
    @stable_source_operation
    def operation(*, config):
        raise RuntimeError("actual operation failed")
    with pytest.raises(RuntimeError, match="actual operation"):
        operation(config=_cfg(tmp_path))
    monkeypatch.setattr(self_update, "_apply_update_locked", lambda *a: (True, "updated"))
    assert self_update.apply_update(_cfg(tmp_path), _plan())[0]


def test_mcd_instance_migration_entrypoints_use_explicit_source_barrier():
    from mcd_agent import instance_migrate

    for name in (
        "stream_source_files",
        "stream_source_db",
        "finalize_target_relay",
        "run_target_pull_migration",
    ):
        function = getattr(instance_migrate, name)
        assert hasattr(function, "__wrapped__"), name
        assert inspect.unwrap(function) is not function


@pytest.mark.parametrize("mode", ["zip", "composer"])
def test_upgrade_entrypoint_holds_lease_during_stage_install_and_verification(invocation, monkeypatch, mode):
    from mcd_agent import mautic_upgrade as upgrade
    args, events = wire_upgrade(invocation, monkeypatch, mode)
    probes = []
    monkeypatch.setattr(self_update, "installed_agent_version", lambda: "1.2.80")
    monkeypatch.setattr(self_update, "release_session", lambda *a, **kw: None)
    def probe():
        ok, message = self_update.apply_update(args["config"], _plan())
        assert not ok and "deferred" in message
        probes.append(True)
    for name in ("_prepare_patch_target_stage", "_apply_zip" if mode == "zip" else "_apply_composer", "_post_upgrade_verify"):
        original = getattr(upgrade, name)
        def wrapped(*a, _original=original, **kw):
            probe()
            return _original(*a, **kw)
        monkeypatch.setattr(upgrade, name, wrapped)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert len(probes) == 3
    monkeypatch.setattr(self_update, "_apply_update_locked", lambda *a: (True, "updated"))
    assert self_update.apply_update(args["config"], _plan())[0]
