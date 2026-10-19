from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

import fcntl
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.daemon import (
    RunningTask,
    TaskStore,
    _SCHEDULER_RECONCILIATION_RECEIPT_KEY,
    _reconcile_running_state,
    _scheduler_host_slots_available,
)


class TaskLifecycleTests(unittest.TestCase):
    def _running_task(self, root: Path, *, task_type: str = "segment", pid: int = 999) -> tuple[TaskStore, RunningTask]:
        store = TaskStore(str(root / "state.db"))
        task = RunningTask(
            row_id=0,
            root="/var/www/site",
            task_key=f"/var/www/site|{task_type}|30",
            task_type=task_type,
            entity_id=30,
            command_str=f"php mautic:{'segments:update' if task_type == 'segment' else 'campaigns:rebuild'} -i 30",
            timeout_sec=0,
            attempts=1,
            started_at=time.time() - 86_400,
            pid=pid,
        )
        task.row_id = store.add_running(task)
        return store, task

    def test_reconcile_dead_pid_detaches_memory_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, task = self._running_task(Path(temp_dir))
            running = {task.task_key: task}
            with patch("mcd_agent.daemon._is_pid_alive", return_value=False):
                stats = _reconcile_running_state(store=store, running=running, popens={})

            self.assertEqual(stats["lost_pid"], 1)
            self.assertEqual(stats["detached_memory"], 1)
            self.assertEqual(running, {})
            self.assertEqual(store.running_rows(), [])
            receipt = store.get_runtime_sync(_SCHEDULER_RECONCILIATION_RECEIPT_KEY)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual(receipt["reason"], "pid_missing")
            self.assertEqual(receipt["task_key"], task.task_key)
            store.close()

    def test_reconcile_keeps_legitimate_long_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, task = self._running_task(Path(temp_dir), task_type="campaign_rebuild", pid=1000)
            running = {task.task_key: task}
            with (
                patch("mcd_agent.daemon._is_pid_alive", return_value=True),
                patch("mcd_agent.daemon._pid_matches_task_command", return_value=True),
            ):
                stats = _reconcile_running_state(store=store, running=running, popens={})

            self.assertEqual(stats["lost_pid"], 0)
            self.assertEqual(stats["lost_cmd"], 0)
            self.assertIn(task.task_key, running)
            self.assertEqual(len(store.running_rows()), 1)
            store.close()

    def test_campaign_capacity_remains_available_while_segment_runs(self) -> None:
        config = SimpleNamespace(
            scheduler_host_max_parallel=4,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "segment": SimpleNamespace(root="/var/www/site", task_type="segment", entity_id=30),
        }
        self.assertEqual(
            _scheduler_host_slots_available(
                config,
                running,
                "campaign_rebuild",
                root="/var/www/site",
            ),
            3,
        )

    def test_dry_run_and_apply_archive_only_old_terminal_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = TaskStore(str(root / "state.db"))
            failed = RunningTask(
                row_id=0,
                root="/var/www/site",
                task_key="/var/www/site|segment|10",
                task_type="segment",
                entity_id=10,
                command_str="php mautic segments:update -i 10",
                timeout_sec=60,
                attempts=4,
                started_at=1.0,
                pid=123,
            )
            failed.row_id = store.add_running(failed)
            store.finish(failed.row_id, "failed", 23, "non_zero_exit")
            running = RunningTask(
                row_id=0,
                root="/var/www/site",
                task_key="/var/www/site|segment|20",
                task_type="segment",
                entity_id=20,
                command_str="php mautic segments:update -i 20",
                timeout_sec=60,
                attempts=1,
                started_at=1.0,
                pid=124,
            )
            running.row_id = store.add_running(running)

            lifecycle_now = time.time() + 10_000.0
            preview = store.task_lifecycle(
                now_ts=lifecycle_now,
                min_age_sec=0,
                archive_dir=str(root / "archive"),
                admission_dir=str(root / "locks"),
                dry_run=True,
            )
            self.assertEqual(preview["phase"], "dry_run")
            self.assertEqual(preview["candidate_ids"], [failed.row_id])
            self.assertEqual(preview["running_ids"], [running.row_id])

            applied = store.task_lifecycle(
                now_ts=lifecycle_now,
                min_age_sec=0,
                archive_dir=str(root / "archive"),
                admission_dir=str(root / "locks"),
                dry_run=False,
            )
            self.assertEqual(applied["deleted_ids"], [failed.row_id])
            self.assertTrue(Path(str(applied["archive_path"])).exists())
            self.assertEqual(store.running_rows()[0]["id"], running.row_id)
            store.close()

    def test_active_execution_lock_is_protected_from_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_dir = root / "locks"
            lock_dir.mkdir()
            store = TaskStore(str(root / "state.db"))
            task = RunningTask(
                row_id=0,
                root="/var/www/site",
                task_key="/var/www/site|segment|30",
                task_type="segment",
                entity_id=30,
                command_str="php mautic segments:update -i 30",
                timeout_sec=60,
                attempts=4,
                started_at=1.0,
                pid=125,
            )
            task.row_id = store.add_running(task)
            store.finish(task.row_id, "failed", 23, "non_zero_exit")
            lock_path = lock_dir / (hashlib.sha256(task.task_key.encode("utf-8")).hexdigest() + ".lock")
            handle = lock_path.open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                preview = store.task_lifecycle(
                    now_ts=time.time() + 10_000.0,
                    min_age_sec=0,
                    archive_dir=str(root / "archive"),
                    admission_dir=str(lock_dir),
                    dry_run=True,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

            self.assertEqual(preview["candidate_ids"], [])
            self.assertEqual(preview["protected_ids"], [task.row_id])
            store.close()


if __name__ == "__main__":
    unittest.main()
