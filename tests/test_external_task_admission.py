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

