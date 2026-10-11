from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon
from mcd_agent.daemon import RunningTask, TaskStore


class ImportLivenessMatrixTests(unittest.TestCase):
    """Disposable M6/M7 import/segment slot invariants.

    These tests model only owner-labelled disposable roots.  They never use a
    production database, host, instance, profile, or command route.
    """

    @staticmethod
    def _config(**overrides: object) -> SimpleNamespace:
        values = {
            "enable_import_polling": True,
            "php_bin": "php",
            "mautic_run_as_user": "www-data",
            "cmd_import_template": "bin/console mautic:import --limit={import_limit}",
            "import_limit": 100,
            "command_timeout_sec": 60,
            "worker_watchdog_sec": 0,
            "worker_stuck_policy": "skip",
            "worker_stuck_restart_limit": 0,
            "segment_kill_grace_sec": 1,
            "task_retry_max": 2,
            "task_retry_delay_sec": 0,
            "segment_retry_count": 1,
            "import_poll_interval_sec": 1,
            "import_stall_timeout_sec": 1800,
            "segment_full_scan_interval_sec": 0,
            "segment_sql_min_repeat_sec": 0,
            "scheduler_host_max_parallel": 4,
            "scheduler_instance_max_parallel": 0,
            "scheduler_elastic_slots_enabled": True,
            "scheduler_emergency_reserved_slots": 0,
            "segment_mode": "id_weighted",
            "segment_priority_parallel_idle": 2,
            "segment_regular_parallel_idle": 2,
            "segment_priority_parallel_throttled": 1,
            "segment_regular_parallel_throttled": 1,
            "segment_throttle_whitelist_only": False,
            "segment_throttle_whitelist_parallel": 1,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _task(root: str, task_type: str, pid: int, *, started_at: float = 1.0) -> RunningTask:
        entity_id = None if task_type == "import" else pid
        return RunningTask(
            row_id=0,
            root=root,
            task_key=daemon._task_key(root, task_type, entity_id),
            task_type=task_type,
            entity_id=entity_id,
            command_str=f"{task_type} {pid}",
            timeout_sec=60,
            attempts=1,
            started_at=started_at,
            pid=pid,
        )

    def _fake_submit(
        self,
        *,
        root: str,
        task_type: str,
        running: dict[str, RunningTask],
        store: TaskStore,
        pid: int,
        config: SimpleNamespace | None = None,
    ) -> bool:
        cfg = config or self._config()
        with patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=pid)):
            return daemon._submit_if_slot_uncoordinated(
                config=cfg,
                store=store,
                running=running,
                root=root,
                task_type=task_type,
                entity_id=None if task_type == "import" else pid,
                args=["fixture", task_type, str(pid)],
                timeout_sec=60,
                max_parallel_for_type=1,
                popens={},
                bypass_repeat_guard=True,
                bypass_baseline_admission=True,
            )

    def test_m6_m7_pending_import_has_one_host_slot_and_fifo_order(self) -> None:
        for major in (6, 7):
            with self.subTest(mautic_major=major):
                roots = [f"/disposable/m{major}/instance-{idx}" for idx in (1, 2, 3)]
                installs = [SimpleNamespace(root=root) for root in roots]
                ordered = daemon._pending_import_first_dispatch_installs(
                    installs,
                    {roots[0]: 1, roots[1]: 1, roots[2]: 1},
                    roots,
                )
                self.assertEqual([inst.root for inst in ordered], roots)
                self.assertEqual(daemon._host_running_import_count({}), 0)

    def test_healthy_running_import_blocks_same_root_but_not_other_root(self) -> None:
        running = {
            "m6": self._task("/disposable/m6/a", "import", 6001),
        }
        self.assertEqual(daemon._host_running_import_count(running), 1)
        with patch.object(daemon, "_submit_if_slot") as submit:
            self.assertFalse(
                daemon._submit_import_if_segment_slot(
                    config=self._config(), store=SimpleNamespace(), running=running, popens={},
                    root="/disposable/m6/a", cluster_import_allowed=True, import_pending_count=1,
                    segment_slot_limit=4, now_ts=100.0,
                )
            )
            submit.assert_not_called()
        with patch.object(daemon, "render_mautic_command", return_value=["fixture", "import"]), patch.object(
            daemon, "_submit_if_slot", return_value=True
        ) as submit:
            self.assertTrue(
                daemon._submit_import_if_segment_slot(
                    config=self._config(), store=SimpleNamespace(), running=running, popens={},
                    root="/disposable/m7/b", cluster_import_allowed=True, import_pending_count=1,
                    segment_slot_limit=4, now_ts=100.0,
                )
            )
            submit.assert_called_once()

    def test_completed_import_releases_slot_without_followup_lock(self) -> None:
        root = "/disposable/m6/completed"
        running: dict[str, RunningTask] = {}
        with patch.object(daemon, "render_mautic_command", return_value=["fixture", "import"]), patch.object(
            daemon, "_submit_if_slot", return_value=True
        ) as submit:
            launched = daemon._submit_import_if_segment_slot(
                config=self._config(),
                store=SimpleNamespace(),
                running=running,
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=1,
                segment_slot_limit=2,
                now_ts=100.0,
            )
        self.assertTrue(launched)
        self.assertEqual(submit.call_args.kwargs["task_type"], "import")
        self.assertEqual(daemon._import_followup_slot_limit({}, root, 2, ordinary_segment_pending=True), 1)

    def test_paused_import_route_does_not_launch(self) -> None:
        with patch.object(daemon, "_submit_if_slot") as submit:
            launched = daemon._submit_import_if_segment_slot(
                config=self._config(),
                store=SimpleNamespace(),
                running={},
                popens={},
                root="/disposable/m7/paused",
                cluster_import_allowed=False,
                import_pending_count=10,
                segment_slot_limit=4,
                now_ts=100.0,
            )
        self.assertFalse(launched)
        submit.assert_not_called()

    def test_cancelled_stopped_import_is_pending_again(self) -> None:
        self.assertEqual(daemon._classify_import_monitor_row({"status": 5}), ("queued", "stopped", "stopped"))
        self.assertEqual(daemon._classify_import_monitor_row({"status": "cancelled"}), ("queued", "stopped", "stopped"))

    def test_terminal_failed_import_does_not_hold_host_slot(self) -> None:
        self.assertEqual(daemon._classify_import_monitor_row({"status": 4}), ("done", "", "error"))
        self.assertEqual(daemon._host_running_import_count({}), 0)
        with patch.object(daemon, "render_mautic_command", return_value=["fixture", "import"]), patch.object(
            daemon, "_submit_if_slot", return_value=True
        ):
            self.assertTrue(
                daemon._submit_import_if_segment_slot(
                    config=self._config(),
                    store=SimpleNamespace(),
                    running={},
                    popens={},
                    root="/disposable/m6/recovery",
                    cluster_import_allowed=True,
                    import_pending_count=1,
                    segment_slot_limit=2,
                    now_ts=100.0,
                )
            )

    def test_hung_import_watchdog_releases_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcd-import-watchdog-") as td:
            root = Path(td)
            proc = subprocess.Popen(["sleep", "5"], cwd=root)
            store = TaskStore(str(root / "tasks.sqlite3"))
            task = RunningTask(
                row_id=0,
                root=str(root),
                task_key=daemon._task_key(str(root), "import", None),
                task_type="import",
                entity_id=None,
                command_str=daemon._CMD_SEP.join(["sleep", "5"]),
                timeout_sec=1,
                attempts=1,
                started_at=time.time() - 2,
                pid=proc.pid,
            )
            task.row_id = store.add_running(task)
            running = {task.task_key: task}
            try:
                daemon._monitor_running(
                    config=self._config(worker_watchdog_sec=1, import_stall_timeout_sec=1),
                    store=store,
                    running=running,
                    popens={task.task_key: proc},
                    import_progress_watchdog={
                        task.task_key: {
                            "last_progress_at": time.time() - 10,
                            "stall_observed_at": time.time() - 10,
                        }
                    },
                    import_liveness_probe=lambda _task: {"marker": True},
                )
                self.assertFalse(running)
                row = store.conn.execute("SELECT state,note FROM tasks WHERE id=?", (task.row_id,)).fetchone()
                self.assertEqual((row["state"], row["note"]), ("timeout", "killed_by_import_stall_watchdog"))
                self.assertEqual(daemon._host_running_import_count(running), 0)
            finally:
                proc.kill()
                proc.wait()
                store.close()

    def test_import_progress_can_run_longer_than_command_timeout(self) -> None:
        store = TaskStore(":memory:")
        proc = Mock()
        proc.poll.return_value = None
        task = self._task("/disposable/m6/long-progress", "import", 6501, started_at=time.time() - 3600)
        task.timeout_sec = 1
        task.row_id = store.add_running(task)
        running = {task.task_key: task}
        try:
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(daemon, "_kill_pid") as kill:
                daemon._monitor_running(
                    config=self._config(worker_watchdog_sec=1, import_stall_timeout_sec=2),
                    store=store,
                    running=running,
                    popens={task.task_key: proc},
                    import_liveness_probe=lambda _task: {"db_progress": "counter-1"},
                )
            self.assertIn(task.task_key, running)
            kill.assert_not_called()
        finally:
            store.close()

    def test_hours_old_import_with_active_db_signal_keeps_host_slot(self) -> None:
        store = TaskStore(":memory:")
        proc = Mock()
        proc.poll.return_value = None
        task = self._task("/disposable/m7/db-active", "import", 7501, started_at=time.time() - 86_400)
        task.timeout_sec = 1
        task.row_id = store.add_running(task)
        running = {task.task_key: task}
        try:
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(daemon, "_kill_pid") as kill:
                daemon._monitor_running(
                    config=self._config(worker_watchdog_sec=1, import_stall_timeout_sec=1),
                    store=store,
                    running=running,
                    popens={task.task_key: proc},
                    import_liveness_probe=lambda _task: {"db_active": True},
                )
            self.assertIn(task.task_key, running)
            kill.assert_not_called()
        finally:
            store.close()

    def test_import_progress_resumes_before_stall_kill(self) -> None:
        store = TaskStore(":memory:")
        proc = Mock()
        proc.poll.return_value = None
        task = self._task("/disposable/m6/progress-resumes", "import", 6601, started_at=time.time() - 10)
        task.row_id = store.add_running(task)
        running = {task.task_key: task}
        watchdog = {
            task.task_key: {
                "last_progress_at": time.time() - 0.5,
                "stall_observed_at": time.time() - 0.5,
                "signal_fingerprint": repr([("db_progress", "old")]),
            }
        }
        try:
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(daemon, "_kill_pid") as kill:
                daemon._monitor_running(
                    config=self._config(import_stall_timeout_sec=2),
                    store=store,
                    running=running,
                    popens={task.task_key: proc},
                    import_progress_watchdog=watchdog,
                    import_liveness_probe=lambda _task: {"db_progress": "new"},
                )
            self.assertIn(task.task_key, running)
            self.assertNotIn("stall_observed_at", watchdog[task.task_key])
            kill.assert_not_called()
        finally:
            store.close()

    def test_terminal_import_exit_releases_immediately(self) -> None:
        store = TaskStore(":memory:")
        proc = Mock()
        proc.poll.return_value = 2
        task = self._task("/disposable/m7/terminal", "import", 7601, started_at=time.time() - 86_400)
        task.row_id = store.add_running(task)
        running = {task.task_key: task}
        watchdog = {task.task_key: {"last_progress_at": time.time() - 86_400}}
        try:
            daemon._monitor_running(
                config=self._config(import_stall_timeout_sec=1),
                store=store,
                running=running,
                popens={task.task_key: proc},
                import_progress_watchdog=watchdog,
            )
            self.assertNotIn(task.task_key, running)
            self.assertNotIn(task.task_key, watchdog)
            row = store.conn.execute("SELECT state,note FROM tasks WHERE id=?", (task.row_id,)).fetchone()
            self.assertEqual((row["state"], row["note"]), ("failed", "mautic_failed"))
        finally:
            store.close()

    def test_stale_marker_never_extends_import_lease(self) -> None:
        store = TaskStore(":memory:")
        proc = Mock()
        proc.poll.return_value = None
        task = self._task("/disposable/m6/stale-marker", "import", 6701, started_at=time.time() - 20)
        task.row_id = store.add_running(task)
        running = {task.task_key: task}
        watchdog = {
            task.task_key: {
                "last_progress_at": time.time() - 10,
                "stall_observed_at": time.time() - 10,
            }
        }
        try:
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(daemon, "_kill_pid") as kill:
                daemon._monitor_running(
                    config=self._config(import_stall_timeout_sec=1),
                    store=store,
                    running=running,
                    popens={task.task_key: proc},
                    import_progress_watchdog=watchdog,
                    import_liveness_probe=lambda _task: {"marker": True},
                )
            self.assertNotIn(task.task_key, running)
            kill.assert_called_once()
            row = store.conn.execute("SELECT state,note FROM tasks WHERE id=?", (task.row_id,)).fetchone()
            self.assertEqual((row["state"], row["note"]), ("timeout", "killed_by_import_stall_watchdog"))
        finally:
            store.close()

    def test_import_retry_is_allowed_when_host_slot_is_free(self) -> None:
        root = "/disposable/m7/retry"
        store = TaskStore(":memory:")
        task = self._task(root, "import", 7001)
        task.command_str = "fixture import"
        task.attempts = 1
        task.row_id = store.add_running(task)
        store.finish(task.row_id, state="failed", rc=1, note="mautic_failed")
        with patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=7002)):
            running: dict[str, RunningTask] = {}
            popens: dict[str, object] = {}
            self.assertTrue(
                daemon._respawn_task(
                    config=self._config(task_retry_max=2),
                    store=store,
                    running=running,
                    popens=popens,
                    task=task,
                )
            )
        self.assertEqual(daemon._host_running_import_count(running), 1)
        store.close()

    def test_multiple_imports_same_instance_are_singleton(self) -> None:
        root = "/disposable/m6/same-instance"
        running = {"import": self._task(root, "import", 6101)}
        self.assertEqual(daemon._host_running_import_count(running), 1)
        self.assertEqual(daemon._segment_shared_slots_available(running, root, 4), 3)

    def test_imports_different_instances_are_concurrent(self) -> None:
        running = {
            "m6": self._task("/disposable/m6/a", "import", 6101),
        }
        with patch.object(daemon, "render_mautic_command", return_value=["fixture", "import"]), patch.object(
            daemon, "_submit_if_slot", return_value=True
        ) as submit:
            self.assertTrue(
                daemon._submit_import_if_segment_slot(
                    config=self._config(), store=SimpleNamespace(), running=running, popens={},
                    root="/disposable/m7/b", cluster_import_allowed=True, import_pending_count=1,
                    segment_slot_limit=4, now_ts=100.0,
                )
            )
        submit.assert_called_once()

    def test_cross_instance_import_uses_real_submit_path_without_baseline_cooldown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcd-import-baseline-") as td:
            store = TaskStore(str(Path(td) / "tasks.sqlite3"))
            running: dict[str, RunningTask] = {}
            with patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_spawn_command", side_effect=[SimpleNamespace(pid=8101), SimpleNamespace(pid=8102)]
            ), patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args):
                self.assertTrue(
                    daemon._submit_if_slot_uncoordinated(
                        config=self._config(), store=store, running=running,
                        root="/disposable/m6/a", task_type="import", entity_id=None,
                        args=["fixture", "import", "8101"], timeout_sec=60,
                        max_parallel_for_type=1, popens={}, bypass_repeat_guard=True,
                    )
                )
                self.assertTrue(
                    daemon._submit_if_slot_uncoordinated(
                        config=self._config(), store=store, running=running,
                        root="/disposable/m7/b", task_type="import", entity_id=None,
                        args=["fixture", "import", "8102"], timeout_sec=60,
                        max_parallel_for_type=1, popens={}, bypass_repeat_guard=True,
                    )
                )
            self.assertEqual(sorted(task.root for task in running.values()), ["/disposable/m6/a", "/disposable/m7/b"])
            store.close()

    def test_manual_requests_are_singleton_per_instance_on_real_submit_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcd-manual-admission-") as td:
            store = TaskStore(str(Path(td) / "tasks.sqlite3"))
            running: dict[str, RunningTask] = {}
            with patch.object(
                daemon, "_spawn_command", side_effect=[SimpleNamespace(pid=8201), SimpleNamespace(pid=8202)]
            ), patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args):
                self.assertTrue(
                    daemon._submit_if_slot_uncoordinated(
                        config=self._config(), store=store, running=running,
                        root="/disposable/m6/a", task_type="segment", entity_id=1,
                        args=["fixture", "segment", "1"], timeout_sec=60,
                        max_parallel_for_type=1, popens={}, manual_request_id=101,
                        bypass_repeat_guard=True, bypass_baseline_admission=True,
                    )
                )
                self.assertFalse(
                    daemon._submit_if_slot_uncoordinated(
                        config=self._config(), store=store, running=running,
                        root="/disposable/m6/a", task_type="segment", entity_id=2,
                        args=["fixture", "segment", "2"], timeout_sec=60,
                        max_parallel_for_type=1, popens={}, manual_request_id=102,
                        bypass_repeat_guard=True, bypass_baseline_admission=True,
                    )
                )
                self.assertTrue(
                    daemon._submit_if_slot_uncoordinated(
                        config=self._config(), store=store, running=running,
                        root="/disposable/m7/b", task_type="segment", entity_id=3,
                        args=["fixture", "segment", "3"], timeout_sec=60,
                        max_parallel_for_type=1, popens={}, manual_request_id=103,
                        bypass_repeat_guard=True, bypass_baseline_admission=True,
                    )
                )
            self.assertEqual(sorted(task.manual_request_id for task in running.values()), [101, 103])
            store.close()

    def test_restart_adopts_live_import_and_keeps_singleton(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcd-import-adopt-") as td:
            root = Path(td)
            proc = subprocess.Popen(["sleep", "5"], cwd=root)
            store = TaskStore(str(root / "tasks.sqlite3"))
            task = RunningTask(
                row_id=0,
                root=str(root),
                task_key=daemon._task_key(str(root), "import", None),
                task_type="import",
                entity_id=None,
                command_str=daemon._CMD_SEP.join(["sleep", "5"]),
                timeout_sec=60,
                attempts=1,
                started_at=time.time(),
                pid=proc.pid,
            )
            store.add_running(task)
            running: dict[str, RunningTask] = {}
            try:
                with patch.object(daemon, "_pid_matches_task_command", return_value=True):
                    stats = daemon._reconcile_running_state(store=store, running=running, popens={})
                self.assertEqual(stats["adopted"], 1)
                self.assertEqual(daemon._host_running_import_count(running), 1)
            finally:
                proc.kill()
                proc.wait()
                store.close()

    def test_lost_import_pid_does_not_hold_slot(self) -> None:
        store = TaskStore(":memory:")
        task = self._task("/disposable/m6/lost", "import", 99999999)
        task.row_id = store.add_running(task)
        running: dict[str, RunningTask] = {}
        stats = daemon._reconcile_running_state(store=store, running=running, popens={})
        self.assertEqual(stats["lost_pid"], 1)
        self.assertEqual(daemon._host_running_import_count(running), 0)
        store.close()

    def test_missing_or_invalid_db_import_rows_fail_closed(self) -> None:
        db = SimpleNamespace(fetch_rows=lambda *_args, **_kwargs: [{"id": 0, "status": 2}, {"id": "bad"}])
        snapshot = daemon._fetch_import_monitor_snapshot(db, "/disposable/m7/invalid", {})
        self.assertEqual(snapshot["running"], [])
        self.assertEqual(snapshot["queued"], [])
        self.assertEqual(snapshot["done"], [])

    def test_stale_completed_followup_reserves_slot_for_unrelated_ordinary_work(self) -> None:
        root = "/disposable/m6/followup"
        self.assertEqual(daemon._import_followup_slot_limit({}, root, 1, ordinary_segment_pending=True), 0)
        self.assertEqual(daemon._import_followup_slot_limit({}, root, 2, ordinary_segment_pending=True), 1)
        self.assertEqual(daemon._import_followup_slot_limit({}, root, 1, ordinary_segment_pending=False), 1)

    def test_pressure_and_capacity_hold_import_without_blocking_other_slot(self) -> None:
        root = "/disposable/m6/pressure"
        running = {"segment": self._task(root, "segment", 6010)}
        with patch.object(daemon, "_submit_if_slot") as submit:
            self.assertFalse(
                daemon._submit_import_if_segment_slot(
                    config=self._config(),
                    store=SimpleNamespace(),
                    running=running,
                    popens={},
                    root=root,
                    cluster_import_allowed=True,
                    import_pending_count=1,
                    segment_slot_limit=1,
                    now_ts=100.0,
                )
            )
        submit.assert_not_called()
        self.assertEqual(daemon._segment_shared_slots_available(running, root, 2), 1)

    def test_manual_segment_n_plus_one_runs_on_other_slot_during_import(self) -> None:
        store = TaskStore(":memory:")
        running: dict[str, RunningTask] = {}
        self.assertTrue(self._fake_submit(root="/disposable/m6/import", task_type="import", running=running, store=store, pid=6201))
        self.assertTrue(self._fake_submit(root="/disposable/m7/manual", task_type="segment", running=running, store=store, pid=7201))
        self.assertEqual(daemon._host_running_import_count(running), 1)
        store.close()

    def test_dependency_closures_use_remaining_slots(self) -> None:
        store = TaskStore(":memory:")
        running: dict[str, RunningTask] = {}
        self.assertTrue(self._fake_submit(root="/disposable/m6/import", task_type="import", running=running, store=store, pid=6301))
        self.assertTrue(self._fake_submit(root="/disposable/m6/closure", task_type="segment", running=running, store=store, pid=6302))
        self.assertTrue(self._fake_submit(root="/disposable/m7/closure", task_type="segment", running=running, store=store, pid=7302))
        self.assertEqual(daemon._host_running_import_count(running), 1)
        self.assertEqual(sum(task.task_type == "segment" for task in running.values()), 2)
        store.close()

    def test_terminal_import_fairness_allows_next_fifo_import_and_segments(self) -> None:
        roots = ["/disposable/m6/fair-a", "/disposable/m7/fair-b"]
        ordered = daemon._pending_import_first_dispatch_installs(
            [SimpleNamespace(root=roots[1]), SimpleNamespace(root=roots[0])],
            {roots[0]: 1, roots[1]: 1},
            roots,
        )
        self.assertEqual([inst.root for inst in ordered], roots)
        store = TaskStore(":memory:")
        running: dict[str, RunningTask] = {}
        self.assertTrue(self._fake_submit(root=roots[0], task_type="import", running=running, store=store, pid=6401))
        running.clear()
        self.assertTrue(self._fake_submit(root=roots[1], task_type="import", running=running, store=store, pid=7401))
        self.assertTrue(self._fake_submit(root="/disposable/m6/fair-segment", task_type="segment", running=running, store=store, pid=6402))
        self.assertEqual(daemon._host_running_import_count(running), 1)
        store.close()


if __name__ == "__main__":
    unittest.main()
