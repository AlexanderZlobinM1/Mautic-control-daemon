from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

import fcntl

from mcd_agent.daemon import RunningTask, TaskStore


class TaskLifecycleTests(unittest.TestCase):
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
