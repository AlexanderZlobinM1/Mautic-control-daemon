from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import fcntl

from mcd_agent import daemon
from mcd_agent.update_admission import (
    create_update_admission_barrier,
    recover_stale_update_admission_barrier,
    remove_update_admission_barrier,
    scheduler_admission_lock,
)


def test_priority_admission_rejects_persisted_update_barrier(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    pause.write_text("mcd-update-admission\n", encoding="utf-8")
    config = SimpleNamespace(
        scheduler_pause_flag_path=str(pause),
        command_timeout_sec=1,
        segment_kill_grace_sec=1,
    )
    executor = daemon._PriorityTaskExecutor()

    with patch.object(daemon, "_spawn_command") as spawn:
        assert not executor.launch(
            config,
            root="/var/www/site",
            task_type="segment_super_priority",
            entity_id=58,
            args=["php", "bin/console", "mautic:segments:update", "-i", "58"],
            interval_sec=1,
            max_parallel=1,
        )

    spawn.assert_not_called()


def test_admission_lock_serializes_barrier_with_final_spawn(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    config = SimpleNamespace(scheduler_pause_flag_path=str(pause))
    with scheduler_admission_lock(config):
        assert not pause.exists()
        pause.write_text("mcd-update-admission\n", encoding="utf-8")
    assert pause.exists()


def test_orphaned_update_barrier_is_recovered_when_source_lease_is_free(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    pause.write_text("mcd-update-admission\n", encoding="utf-8")
    config = SimpleNamespace(
        scheduler_pause_flag_path=str(pause),
        state_db_path=str(tmp_path / "mcd-state.db"),
    )

    observed = recover_stale_update_admission_barrier(config)

    assert observed["recovered"] is True
    assert observed["reason"] == "orphaned_mcd_update_barrier"
    assert not pause.exists()


def test_live_update_owner_keeps_barrier_until_explicit_cleanup(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    config = SimpleNamespace(
        scheduler_pause_flag_path=str(pause),
        state_db_path=str(tmp_path / "mcd-state.db"),
    )
    with scheduler_admission_lock(config):
        create_update_admission_barrier(config, target="1.5.139")

    observed = recover_stale_update_admission_barrier(config)

    assert observed["active"] is True
    assert observed["reason"] == "owner_alive"
    assert pause.exists()
    assert remove_update_admission_barrier(config) is True
    assert not pause.exists()


def test_active_source_lease_keeps_legacy_barrier(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    pause.write_text("mcd-update-admission\n", encoding="utf-8")
    config = SimpleNamespace(
        scheduler_pause_flag_path=str(pause),
        state_db_path=str(tmp_path / "mcd-state.db"),
    )
    lease = tmp_path / "mcd-self-update.lock"
    with lease.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        observed = recover_stale_update_admission_barrier(config)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert observed["active"] is True
    assert observed["reason"] == "source_operation_lock"
    assert pause.exists()


def test_manual_pause_is_never_recovered_as_update_barrier(tmp_path: Path) -> None:
    pause = tmp_path / "scheduler.pause"
    pause.write_text("paused\n", encoding="utf-8")
    config = SimpleNamespace(
        scheduler_pause_flag_path=str(pause),
        state_db_path=str(tmp_path / "mcd-state.db"),
    )

    observed = recover_stale_update_admission_barrier(config)

    assert observed["recovered"] is False
    assert observed["reason"] == "not_mcd_update_barrier"
    assert pause.read_text(encoding="utf-8") == "paused\n"
