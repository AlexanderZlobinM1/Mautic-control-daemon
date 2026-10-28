from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import maintenance_mode


class MaintenanceFenceTests(unittest.TestCase):
    def _config(self, root: str) -> SimpleNamespace:
        return SimpleNamespace(
            scheduler_pause_flag_path=str(Path(root) / "scheduler.pause"),
            state_db_path=str(Path(root) / "state.db"),
        )

    def test_exclusive_fence_blocks_shared_admission_and_replays_after_release(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td)
            with maintenance_mode.scheduler_dispatch_fence(cfg, exclusive=True):
                with maintenance_mode.scheduler_dispatch_fence(cfg, exclusive=False, blocking=False) as admitted:
                    self.assertFalse(admitted)
            with maintenance_mode.scheduler_dispatch_fence(cfg, exclusive=False, blocking=False) as admitted:
                self.assertTrue(admitted)

    def test_drain_rechecks_and_stops_task_appearing_during_window(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td)
            first = [{"root": "/var/www/a", "task_type": "segment", "entity_id": 1, "pid": 101}]
            second = [{"root": "/var/www/a", "task_type": "campaign_trigger", "entity_id": 2, "pid": 202}]
            snapshots = iter([first, [], second, [], [], []])
            with (
                patch.object(maintenance_mode, "observed_running_tasks", side_effect=lambda _cfg: (next(snapshots), [])),
                patch.object(maintenance_mode, "_reconcile_exited_tracked_tasks", return_value=[]),
                patch.object(maintenance_mode, "list_mautic_console_processes", return_value=[]),
                patch.object(maintenance_mode, "_kill_pid", side_effect=["terminated", "terminated"]) as kill,
                patch.object(maintenance_mode.time, "sleep"),
            ):
                result = maintenance_mode.stop_running_tasks_for_maintenance(
                    cfg, grace_sec=0, stable_interval_sec=0, drain_timeout_sec=30
                )
            self.assertEqual(result["managed_running"], 0)
            self.assertEqual(result["stable_snapshots"], 3)
            self.assertEqual(result["drain_attempts"], 3)
            self.assertEqual([call.args[0] for call in kill.call_args_list], [101, 202])

    def test_dead_tracked_row_is_reconciled_before_quiescence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td)
            stale = [{"root": "/var/www/a", "task_type": "segment", "entity_id": 1, "pid": 303}]
            snapshots = iter([stale, [], [], [], [], [], [], [], []])
            released = [{"root": "/var/www/a", "task_type": "segment", "entity_id": 1, "pid": 303, "reason": "pid_not_alive"}]
            with (
                patch.object(maintenance_mode, "observed_running_tasks", side_effect=lambda _cfg: (next(snapshots), [])),
                patch.object(maintenance_mode, "_reconcile_exited_tracked_tasks", side_effect=[released, [], []]),
                patch.object(maintenance_mode, "list_mautic_console_processes", return_value=[]),
                patch.object(maintenance_mode, "_kill_pid") as kill,
                patch.object(maintenance_mode.time, "sleep"),
            ):
                result = maintenance_mode.stop_running_tasks_for_maintenance(
                    cfg, grace_sec=0, stable_interval_sec=0, drain_timeout_sec=30
                )
            self.assertEqual(result["managed_running"], 0)
            self.assertEqual(result["released_stale_tasks"], released)
            kill.assert_not_called()

    def test_unstoppable_pid_is_bounded_and_reported(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td)
            row = [{"root": "/var/www/a", "task_type": "segment", "entity_id": 1, "pid": 404}]
            with (
                patch.object(maintenance_mode, "observed_running_tasks", return_value=(row, [])),
                patch.object(maintenance_mode, "_reconcile_exited_tracked_tasks", return_value=[]),
                patch.object(maintenance_mode, "list_mautic_console_processes", return_value=[]),
                patch.object(maintenance_mode, "_kill_pid", return_value="failed"),
                patch.object(maintenance_mode.time, "monotonic", side_effect=[0.0, 2.0, 2.0]),
            ):
                result = maintenance_mode.stop_running_tasks_for_maintenance(
                    cfg, grace_sec=0, stable_interval_sec=0, drain_timeout_sec=0.1
                )
            self.assertEqual(result["stop_failed"], 1)
            self.assertEqual(result["managed_running"], 1)
            self.assertTrue(result["drain_timed_out"])


if __name__ == "__main__":
    unittest.main()
