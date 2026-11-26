from __future__ import annotations

import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.daemon import RunningTask, TaskStore


class ExternalTaskAdmissionTests(unittest.TestCase):
    def test_maintenance_fence_blocks_late_submit_and_external_adoption(self) -> None:
        @contextmanager
        def blocked_fence(*_args, **_kwargs):
            yield False

        config = SimpleNamespace(scheduler_pause_flag_path="/tmp/mcd-test-scheduler.pause")
        with patch.object(daemon, "scheduler_dispatch_fence", blocked_fence), patch.object(
            daemon, "_submit_if_slot_uncoordinated"
        ) as submit:
            launched = daemon._submit_if_slot(
                config=config,
                store=Mock(),
                running={},
                root="/var/www/client",
                task_type="segment",
                entity_id=92,
                args=["true"],
                timeout_sec=1,
                max_parallel_for_type=1,
            )
        self.assertFalse(launched)
        submit.assert_not_called()

        with patch.object(daemon, "scheduler_dispatch_fence", blocked_fence):
            result = daemon._sync_external_running_tasks(
                config=config,
                installs=[],
                running={},
                popens={},
            )
        self.assertEqual(result["fence_blocked"], 1)

    def test_external_console_processes_are_one_logical_task(self) -> None:
        root = "/var/www/client"
        ps_output = (
            "4101 1200 php /var/www/client/bin/console mautic:segments:update -i 92\n"
            "4102 900 php /var/www/client/bin/console mautic:segments:update -i 92\n"
        )
        with patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=ps_output),
        ), patch.object(
            daemon,
            "_pid_cmdline_args",
            side_effect=lambda _pid: [
                "php",
                "/var/www/client/bin/console",
                "mautic:segments:update",
                "-i",
                "92",
            ],
        ):
            rows = daemon.list_external_runtime_task_summaries([root])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 4101)
        self.assertEqual(
            daemon._external_task_key(root, "segment", 92, 4101),
            daemon._external_task_key(root, "segment", 92, 4102),
        )

    def test_three_wrapper_processes_per_segment_are_two_logical_tasks(self) -> None:
        root = "/var/www/client"
        rows = []
        for pid, entity_id in zip(range(4201, 4207), [92, 92, 92, 93, 93, 93]):
            rows.append(f"{pid} {1200 - pid} php /var/www/client/bin/console mautic:segments:update -i {entity_id}")
        ps_output = "\n".join(rows) + "\n"

        def cmdline(pid: int) -> list[str]:
            entity_id = 92 if pid < 4204 else 93
            return [
                "php",
                "/var/www/client/bin/console",
                "mautic:segments:update",
                "-i",
                str(entity_id),
            ]

        with patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=ps_output),
        ), patch.object(daemon, "_pid_cmdline_args", side_effect=cmdline):
            observed = daemon.list_external_runtime_task_summaries([root])

        self.assertEqual(len(observed), 2)
        self.assertEqual([row["entity_id"] for row in observed], [92, 93])

    def test_restart_readopts_only_mcd_owned_child_into_durable_store(self) -> None:
        root = "/var/www/client"
        owned_key = daemon._task_key(root, "campaign_trigger", 110)
        observed = [
            {
                "root": root,
                "task_type": "campaign_trigger",
                "entity_id": 110,
                "pid": 3667,
                "started_at": 100.0,
                "command_str": "php|bin/console|mautic:campaigns:trigger|-i|110",
                "mcd_ownership": {
                    "MCD_TASK_OWNER": "mcd",
                    "MCD_TASK_KEY": owned_key,
                    "MCD_TASK_ROOT": root,
                    "MCD_TASK_TYPE": "campaign_trigger",
                    "MCD_TASK_ENTITY_ID": "110",
                    "MCD_TASK_TIMEOUT_SEC": "0",
                },
            },
            {
                "root": root,
                "task_type": "segment",
                "entity_id": 92,
                "pid": 5001,
                "started_at": 101.0,
                "command_str": "php|bin/console|mautic:segments:update|-i|92",
                "mcd_ownership": {},
            },
        ]
        store = TaskStore(":memory:")
        running: dict[str, RunningTask] = {}
        with patch.object(daemon, "list_external_runtime_task_summaries", return_value=observed):
            result = daemon._sync_external_running_tasks_unfenced(
                installs=[SimpleNamespace(root=root)],
                running=running,
                popens={},
                store=store,
            )

        self.assertEqual(result["adopted"], 2)
        self.assertIn(owned_key, running)
        self.assertFalse(running[owned_key].external)
        self.assertTrue(store.has_running_task_key(owned_key))
        manual = next(task for task in running.values() if task.entity_id == 92)
        self.assertTrue(manual.external)
        self.assertFalse(store.has_running_task_key(manual.task_key))
        store.close()

    @staticmethod
    def _external_segment(root: str, entity_id: int, started_at: float) -> RunningTask:
        return RunningTask(
            row_id=0,
            root=root,
            task_key=daemon._external_task_key(root, "segment", entity_id, 5000 + entity_id),
            task_type="segment",
            entity_id=entity_id,
            command_str=f"php|/var/www/client/bin/console|mautic:segments:update|-i|{entity_id}",
            timeout_sec=0,
            attempts=1,
            started_at=started_at,
            pid=5000 + entity_id,
            external=True,
        )

    def test_external_watchdog_keeps_progressing_worker_and_releases_stuck_worker(self) -> None:
        root = "/var/www/client"
        config = SimpleNamespace(
            segment_sql_orphan_after_sec=30,
            segment_kill_grace_sec=1,
            worker_watchdog_sec=0,
            task_retry_max=1,
            task_retry_delay_sec=0,
        )
        progressing = self._external_segment(root, 92, 90.0)
        stalled = self._external_segment(root, 93, 90.0)
        store = TaskStore(":memory:")
        running = {progressing.task_key: progressing, stalled.task_key: stalled}
        watch: dict[str, dict[str, object]] = {}
        clock = {"now": 100.0}
        evidence = {
            progressing.task_key: {"db_query_progress": "q-1", "process_group_alive": True},
            stalled.task_key: {"db_query_progress": "q-stuck", "process_group_alive": True},
        }

        def probe(task: RunningTask) -> dict[str, object]:
            return dict(evidence[task.task_key])

        def monitor() -> None:
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
                daemon, "_pid_matches_task_command", return_value=True
            ), patch.object(daemon, "_kill_pid") as kill, patch.object(
                daemon.time, "time", return_value=clock["now"]
            ):
                daemon._monitor_running(
                    config=config,
                    store=store,
                    running=running,
                    popens={},
                    segment_liveness_probe=probe,
                    segment_progress_watchdog=watch,
                )
                return kill

        kill = monitor()
        clock["now"] = 105.0
        evidence[progressing.task_key] = {"db_query_progress": "q-2", "process_group_alive": True}
        monitor()
        clock["now"] = 131.0
        evidence[stalled.task_key] = {"process_group_alive": True}
        kill = monitor()
        self.assertIn(stalled.task_key, running)
        clock["now"] = 136.0
        evidence[progressing.task_key] = {"db_query_progress": "q-3", "process_group_alive": True}
        kill = monitor()

        self.assertIn(progressing.task_key, running)
        self.assertNotIn(stalled.task_key, running)
        kill.assert_called_once_with(stalled.pid, 1)
        receipt = store.get_runtime_sync(daemon._SCHEDULER_RECONCILIATION_RECEIPT_KEY)
        self.assertEqual(receipt["state"], "stale")
        self.assertEqual(receipt["reason"], "external_segment_stall")
        store.close()

    def test_external_d_state_watchdog_terminates_exact_group_and_records_reason(self) -> None:
        root = "/var/www/client"
        config = SimpleNamespace(
            external_task_stale_after_sec=30,
            segment_kill_grace_sec=0,
            worker_watchdog_sec=0,
        )
        task = self._external_segment(root, 89, 1.0)
        task.pid_start_time = 1234
        task.process_group_id = 5678
        task.session_id = 5678
        store = TaskStore(":memory:")
        running = {task.task_key: task}
        watchdog: dict[str, dict[str, object]] = {}
        clock = {"now": 40.0}
        process = {
            "pid_alive": True,
            "process_state": "D",
            "process_wchan": "folio_wait_bit_common",
            "process_group_id": 5678,
            "session_id": 5678,
            "pid_start_time": 1234,
            "process_group_alive": True,
            "cpu_progress": (10, 2),
            "io_progress": (("read_bytes", 99), ("write_bytes", 0)),
        }
        kill_calls: list[tuple[int, int]] = []

        def fake_killpg(pgid: int, sig: int) -> None:
            kill_calls.append((pgid, sig))
            if sig == 0:
                raise ProcessLookupError

        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_matches_task_command", return_value=True
        ), patch.object(daemon, "_pid_process_identity", return_value=dict(process)), patch.object(
            daemon, "_external_segment_process_liveness_signal", return_value=dict(process)
        ), patch.object(daemon.os, "killpg", side_effect=fake_killpg), patch.object(
            daemon.time, "time", side_effect=lambda: clock["now"]
        ):
            daemon._monitor_running(
                config=config,
                store=store,
                running=running,
                popens={},
                external_task_watchdog=watchdog,
            )
            clock["now"] = 50.0
            daemon._monitor_running(
                config=config,
                store=store,
                running=running,
                popens={},
                external_task_watchdog=watchdog,
            )

        self.assertNotIn(task.task_key, running)
        self.assertIn((5678, daemon.signal.SIGTERM), kill_calls)
        receipt = store.get_runtime_sync(daemon._SCHEDULER_RECONCILIATION_RECEIPT_KEY)
        self.assertEqual(receipt["reason"], "external_task_d_state_no_progress")
        self.assertEqual(receipt["note"], "stale:external_task_d_state_no_progress")
        self.assertEqual(receipt["evidence"]["process_wchan"], "folio_wait_bit_common")
        store.close()

    def test_external_d_state_watchdog_keeps_recent_or_progressing_task(self) -> None:
        root = "/var/www/client"
        config = SimpleNamespace(external_task_stale_after_sec=30, segment_kill_grace_sec=0)
        task = self._external_segment(root, 90, 100.0)
        task.pid_start_time = 1235
        task.process_group_id = 5679
        task.session_id = 5679
        store = TaskStore(":memory:")
        running = {task.task_key: task}
        process = {
            "pid_alive": True,
            "process_state": "D",
            "process_group_id": 5679,
            "session_id": 5679,
            "pid_start_time": 1235,
            "process_group_alive": True,
            "cpu_progress": (10, 2),
            "io_progress": (("read_bytes", 99),),
        }
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_matches_task_command", return_value=True
        ), patch.object(daemon, "_pid_process_identity", return_value=dict(process)), patch.object(
            daemon, "_external_segment_process_liveness_signal", return_value=dict(process)
        ), patch.object(daemon, "_terminate_external_task_group") as terminate, patch.object(
            daemon.time, "time", return_value=110.0
        ):
            daemon._monitor_running(
                config=config,
                store=store,
                running=running,
                popens={},
                external_task_watchdog={},
            )
        self.assertIn(task.task_key, running)
        terminate.assert_not_called()
        store.close()

