from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.update_admission import scheduler_admission_lock


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
