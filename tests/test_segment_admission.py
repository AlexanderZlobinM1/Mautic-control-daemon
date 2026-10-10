from __future__ import annotations

import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mcd_agent.daemon as daemon_mod
from mcd_agent.daemon import (
    RunningTask,
    _segment_cycle_claim,
    _segment_cycle_manual_prepare,
    _segment_cycle_manual_terminal,
    _record_segment_admission_rejection,
    _segment_admission_rejections_key,
    _monitor_running,
    _submit_if_slot_uncoordinated,
    _segment_shared_slots_available,
    _segment_terminal_and_wake,
    _sync_external_running_tasks,
)
from mcd_agent.segment_admission import (
    claim_item,
    manual_admission_info,
    manual_claim,
    manual_terminal,
    mark_running,
    mark_terminal,
    never_built_ids,
    normalize_state,
    ordinary_snapshot_order,
    source_baseline,
    sync_cycle,
)
from mcd_agent.segment_admission import cycle_key


class SegmentAdmissionTests(unittest.TestCase):
    class RuntimeStore:
        def __init__(self) -> None:
            self.payloads: dict[str, dict[str, object]] = {}

        def get_runtime_sync(self, key: str) -> dict[str, object] | None:
            return self.payloads.get(key)

        def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
            self.payloads[key] = payload

    def test_never_built_ids_are_bounded_to_positive_deduplicated_ids(self) -> None:
        self.assertEqual(
            never_built_ids(
                [
                    {"id": 151, "last_built_date": None},
                    {"id": "152", "last_built_date": ""},
                    {"id": 151, "last_built_date": None},
                    {"id": 153, "last_built_date": "2026-10-09 12:00:00"},
                ]
            ),
            [151, 152],
        )

    def test_active_cycle_appends_without_reordering_or_reset(self) -> None:
        state, added, started = sync_cycle(
            None,
            planned_ids=[10, 20],
            never_built=[20],
            running_ids=[],
            now_ts=100.0,
        )
        self.assertTrue(started)
        self.assertEqual(added, [10, 20])

        state, added, started = sync_cycle(
            state,
            planned_ids=[20, 30],
            never_built=[20, 30, 30],
            running_ids=[10],
            now_ts=101.0,
        )
        self.assertFalse(started)
        self.assertEqual(added, [30])
        self.assertEqual(state["items"], [10, 20, 30])
        self.assertEqual(state["running"], [10])

    def test_mid_cycle_never_built_is_inserted_before_pending_changed_work(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[1, 2, 3],
            never_built=[1, 2],
            running_ids=[1],
            now_ts=100.0,
        )
        state, added, started = sync_cycle(
            state,
            planned_ids=[1, 2, 3, 4],
            never_built=[1, 2, 4],
            running_ids=[1],
            now_ts=101.0,
        )
        self.assertFalse(started)
        self.assertEqual(added, [4])
        self.assertEqual(state["items"], [1, 2, 4, 3])
        self.assertEqual(state["running"], [1])

    def test_ordinary_snapshot_uses_categories_fifo_and_excludes_static_lanes(self) -> None:
        rows = [
            {"id": 5, "last_built_date": 190, "date_modified": 190, "recent_activity": 9999},
            {"id": 4, "last_built_date": 50, "date_modified": 50, "recent_activity": 1},
            {"id": 2, "last_built_date": 90, "date_modified": 120, "recent_activity": 0},
            {"id": 1, "last_built_date": None, "date_modified": 200, "recent_activity": 0},
            {"id": 6, "last_built_date": None, "date_modified": 100, "recent_activity": 0},
            {"id": 8, "last_built_date": None, "date_modified": 200, "recent_activity": 10000},
        ]
        self.assertEqual(
            ordinary_snapshot_order(
                rows,
                observed_ids=[5, 4, 2, 1, 8],
                static_priority_ids=[8, 9],
                stale_after_sec=100,
                now_ts=200,
            ),
            [1, 6, 2, 4],
        )

    def test_terminal_item_immediately_claims_next_item_without_tick_state(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[10, 20],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state, claimed = claim_item(
            state,
            entity_id=10,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.1,
        )
        self.assertTrue(claimed)
        state = mark_running(state, entity_id=10, now_ts=100.2)
        state = mark_terminal(state, entity_id=10, success=True, now_ts=100.3)

        state, claimed = claim_item(
            state,
            entity_id=20,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.4,
        )
        self.assertTrue(claimed)
        self.assertEqual(state["done"], [10])
        self.assertEqual(state["claimed"], [20])

    def test_completed_baseline_excludes_unchanged_fresh_rows(self) -> None:
        previous_rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 200},
        ]
        current_rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 210},
        ]
        self.assertEqual(
            ordinary_snapshot_order(
                current_rows,
                observed_ids=[1, 2],
                baseline_snapshot=source_baseline(previous_rows),
                stale_after_sec=10_000,
                now_ts=500,
            ),
            [2],
        )

    def test_missing_baseline_entry_fails_open_as_changed(self) -> None:
        rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 100},
        ]
        baseline = source_baseline([rows[0]])
        self.assertEqual(
            ordinary_snapshot_order(
                rows,
                observed_ids=[1, 2],
                baseline_snapshot=baseline,
                stale_after_sec=10_000,
                now_ts=500,
            ),
            [2],
        )

    def test_empty_or_corrupt_baseline_bootstraps_built_candidates(self) -> None:
        rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 100},
        ]
        for baseline in ({}, {"1": "corrupt-fingerprint"}):
            with self.subTest(baseline=baseline):
                self.assertEqual(
                    ordinary_snapshot_order(
                        rows,
                        observed_ids=[1, 2],
                        baseline_snapshot=baseline,
                        static_priority_ids=[2],
                        stale_after_sec=10_000,
                        now_ts=500,
                    ),
                    [1],
                )

    def test_active_cycle_observes_change_without_replacing_cycle_baseline(self) -> None:
        initial_rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 100},
        ]
        changed_rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 200},
        ]
        initial_snapshot = source_baseline(initial_rows)
        changed_snapshot = source_baseline(changed_rows)
        state, _added, started = sync_cycle(
            None,
            planned_ids=[1],
            never_built=[],
            running_ids=[1],
            now_ts=1000.0,
            source_snapshot=initial_snapshot,
        )
        self.assertTrue(started)
        state, added, started = sync_cycle(
            state,
            planned_ids=[1, 2],
            never_built=[],
            running_ids=[1],
            now_ts=1001.0,
            source_snapshot=changed_snapshot,
        )
        self.assertFalse(started)
        self.assertEqual(added, [2])
        self.assertEqual(state["cycle_baseline"], initial_snapshot)
        self.assertEqual(state["observed_snapshot"], changed_snapshot)
        self.assertEqual(state["baseline_generation"], 0)

    def test_manual_pending_claim_replaces_automatic_claim(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[151, 152],
            never_built=[151, 152],
            running_ids=[],
            now_ts=100.0,
        )
        state, claimed, reason = manual_claim(
            state,
            request_id=7,
            entity_ids=[151],
            lane="ordinary",
            observed_snapshot=source_baseline(
                [{"id": 151, "last_built_date": None, "date_modified": 100}]
            ),
            now_ts=100.1,
        )
        self.assertTrue(claimed)
        self.assertEqual(reason, "ordinary_pending")
        state, automatic_claimed = claim_item(
            state,
            entity_id=151,
            eligible_ids=[151, 152],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.2,
        )
        self.assertFalse(automatic_claimed)
        self.assertEqual(manual_admission_info(state, request_id=7)["lane"], "ordinary")

        state = manual_terminal(state, request_id=7, success=True, now_ts=100.3)
        self.assertEqual(state["done"], [151])
        self.assertEqual(state["manual_admissions"]["7"]["status"], "terminal_success")

    def test_daemon_manual_claim_blocks_automatic_and_reconciles_terminal(self) -> None:
        store = self.RuntimeStore()
        root = "/var/www/site"
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[151, 152],
            never_built=[151, 152],
            running_ids=[],
            now_ts=100.0,
        )
        store.put_runtime_sync(cycle_key(root), state)
        prepared, reason = _segment_cycle_manual_prepare(
            store,
            root=root,
            request_id=17,
            entity_ids=[151],
            observed_snapshot=None,
            super_priority_ids=set(),
            priority_ids=set(),
            globally_running_ids=set(),
            now_ts=100.1,
        )
        self.assertTrue(prepared)
        self.assertEqual(reason, "ordinary_pending")
        self.assertFalse(
            _segment_cycle_claim(
                store,
                root=root,
                entity_id=151,
                eligible_ids=[151, 152],
                blocked_ids=[],
                running_ids=[],
                now_ts=100.2,
            )
        )
        _segment_cycle_manual_terminal(
            store,
            root=root,
            request_id=17,
            success=True,
            note=None,
            now_ts=100.3,
        )
        reconciled = store.get_runtime_sync(cycle_key(root))
        self.assertEqual(reconciled["done"], [151])
        self.assertTrue(reconciled["manual_admissions"]["17"]["applied"])

    def test_manual_overcommit_bypasses_host_slot_only_for_manual_request(self) -> None:
        class LaunchStore:
            def __init__(self) -> None:
                self.task = None

            def has_running_task_key(self, _key: str) -> bool:
                return False

            def add_running(self, task: RunningTask) -> int:
                self.task = task
                return 1

        store = LaunchStore()
        running: dict[str, RunningTask] = {}
        popens: dict[str, object] = {}
        proc = SimpleNamespace(pid=5, wait=lambda: 0)
        with patch("mcd_agent.daemon._scheduler_host_slots_available", return_value=0), patch(
            "mcd_agent.daemon._spawn_command", return_value=proc
        ), patch("mcd_agent.daemon._task_locked_args", side_effect=lambda _key, args, **_kwargs: args), patch(
            "mcd_agent.daemon._SEGMENT_DISPATCH_WAKEUP", None
        ):
            launched = _submit_if_slot_uncoordinated(
                config=SimpleNamespace(),
                store=store,
                running=running,
                root="/var/www/site",
                task_type="segment",
                entity_id=151,
                args=["php", "segments:update"],
                timeout_sec=60,
                max_parallel_for_type=1,
                popens=popens,
                ignore_limit=True,
                manual_request_id=17,
            )
        self.assertTrue(launched)
        self.assertIn("/var/www/site|segment|151", running)

    def test_manual_success_records_applied_snapshot_outside_current_cycle(self) -> None:
        previous_rows = [{"id": 151, "last_built_date": 100, "date_modified": 100}]
        current_rows = [{"id": 151, "last_built_date": 110, "date_modified": 100}]
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
            source_snapshot=source_baseline(previous_rows),
        )
        state, claimed, reason = manual_claim(
            state,
            request_id=8,
            entity_ids=[151],
            lane="ordinary",
            observed_snapshot=source_baseline(current_rows),
            now_ts=101.0,
        )
        self.assertTrue(claimed)
        self.assertEqual(reason, "outside_cycle")
        state = manual_terminal(
            state,
            request_id=8,
            success=True,
            now_ts=102.0,
        )
        self.assertEqual(state["baseline"]["151"], source_baseline(current_rows)["151"])
        self.assertTrue(state["manual_admissions"]["8"]["applied"])

    def test_manual_failure_remains_due_for_next_ordinary_snapshot(self) -> None:
        rows = [{"id": 151, "last_built_date": 100, "date_modified": 100}]
        baseline = source_baseline(rows)
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
            source_snapshot=baseline,
        )
        state, claimed, _reason = manual_claim(
            state,
            request_id=9,
            entity_ids=[151],
            lane="ordinary",
            observed_snapshot=baseline,
            now_ts=101.0,
        )
        self.assertTrue(claimed)
        state = manual_terminal(
            state,
            request_id=9,
            success=False,
            note="manual_failed",
            now_ts=102.0,
        )
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["manual_retry_ids"], [151])
        self.assertEqual(
            ordinary_snapshot_order(
                rows,
                observed_ids=[151],
                baseline_snapshot=state["baseline"],
                retry_ids=state["manual_retry_ids"],
                stale_after_sec=10_000,
                now_ts=500,
            ),
            [151],
        )

    def test_manual_dependency_overlap_is_blocked(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[57, 151],
            never_built=[57, 151],
            running_ids=[57],
            now_ts=100.0,
        )
        state, claimed, reason = manual_claim(
            state,
            request_id=10,
            entity_ids=[57, 151],
            lane="ordinary",
            observed_snapshot=None,
            now_ts=101.0,
        )
        self.assertFalse(claimed)
        self.assertEqual(reason, "manual_overlap_active")

    def test_manual_overcommit_capacity_4_to_5_to_4_to_3(self) -> None:
        def task(entity_id: int, manual_request_id: int | None = None) -> RunningTask:
            return RunningTask(
                row_id=entity_id,
                root="/var/www/site",
                task_key=f"/var/www/site|segment|{entity_id}",
                task_type="segment",
                entity_id=entity_id,
                command_str="php mautic segments:update",
                timeout_sec=60,
                attempts=1,
                started_at=100.0,
                pid=entity_id,
                manual_request_id=manual_request_id,
            )

        running = {f"normal-{idx}": task(idx) for idx in range(1, 5)}
        self.assertEqual(_segment_shared_slots_available(running, "/var/www/site", 4), 0)
        running["manual-5"] = task(5, manual_request_id=99)
        self.assertEqual(len(running), 5)
        self.assertEqual(_segment_shared_slots_available(running, "/var/www/site", 4), 0)
        del running["normal-1"]
        self.assertEqual(len(running), 4)
        self.assertEqual(_segment_shared_slots_available(running, "/var/www/site", 4), 0)
        del running["normal-2"]
        self.assertEqual(len(running), 3)
        self.assertEqual(_segment_shared_slots_available(running, "/var/www/site", 4), 1)

    def test_baseline_advances_at_terminal_only_and_error_is_retried(self) -> None:
        previous_rows = [
            {"id": 1, "last_built_date": 100, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 100},
        ]
        current_rows = [
            {"id": 1, "last_built_date": 110, "date_modified": 100},
            {"id": 2, "last_built_date": 100, "date_modified": 100},
        ]
        previous_snapshot = source_baseline(previous_rows)
        current_snapshot = source_baseline(current_rows)
        prior_terminal = {
            "status": "terminal",
            "items": [99],
            "done": [99],
            "baseline": previous_snapshot,
            "baseline_ready": True,
            "baseline_generation": 7,
            "observed_snapshot": previous_snapshot,
        }
        state, _added, started = sync_cycle(
            prior_terminal,
            planned_ids=[1, 2],
            never_built=[],
            running_ids=[1],
            now_ts=2000.0,
            source_snapshot=current_snapshot,
        )
        self.assertTrue(started)
        self.assertEqual(state["baseline_generation"], 7)

        state = mark_terminal(state, entity_id=1, success=True, now_ts=2001.0)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["baseline_generation"], 7)
        self.assertEqual(state["baseline"], previous_snapshot)
        partial_restart = normalize_state(state)
        self.assertEqual(partial_restart["baseline_generation"], 7)
        self.assertEqual(partial_restart["baseline"], previous_snapshot)

        state = mark_terminal(
            state,
            entity_id=2,
            success=False,
            note="bounded_retry_exhausted",
            now_ts=2002.0,
        )
        self.assertEqual(state["status"], "terminal")
        self.assertEqual(state["baseline_generation"], 8)
        self.assertEqual(state["baseline"]["1"], current_snapshot["1"])
        self.assertEqual(state["baseline"]["2"], previous_snapshot["2"])

        restarted = sync_cycle(
            state,
            planned_ids=[2],
            never_built=[],
            running_ids=[],
            now_ts=2003.0,
            source_snapshot=current_snapshot,
        )[0]
        self.assertEqual(restarted["baseline_generation"], 8)
        self.assertEqual(restarted["cycle_baseline"], current_snapshot)
        self.assertEqual(restarted["observed_snapshot"], current_snapshot)
        self.assertEqual(
            ordinary_snapshot_order(
                current_rows,
                observed_ids=[1, 2],
                baseline_snapshot=state["baseline"],
                retry_ids=state["errors"],
                stale_after_sec=10_000,
                now_ts=500,
            ),
            [2],
        )

    def test_concurrent_claim_deduplicates_same_entity(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[151],
            never_built=[151],
            running_ids=[],
            now_ts=100.0,
        )
        state, first = claim_item(
            state,
            entity_id=151,
            eligible_ids=[151],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.1,
        )
        state, second = claim_item(
            state,
            entity_id=151,
            eligible_ids=[151],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.2,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(state["claimed"], [151])

    def test_terminal_error_does_not_block_independent_next_item(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[10, 20],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state, claimed = claim_item(
            state,
            entity_id=10,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.1,
        )
        self.assertTrue(claimed)
        state = mark_terminal(state, entity_id=10, success=False, note="non_zero_exit", now_ts=100.2)
        state, claimed = claim_item(
            state,
            entity_id=20,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.3,
        )
        self.assertTrue(claimed)
        self.assertEqual(state["errors"], [10])

    def test_monitor_terminal_callback_is_called_before_next_dispatch_pass(self) -> None:
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|10",
            task_type="segment",
            entity_id=10,
            command_str="php mautic segments:update --id=10",
            timeout_sec=60,
            attempts=1,
            started_at=100.0,
            pid=123,
        )
        process = Mock()
        process.poll.return_value = 0
        store = Mock()
        running = {task.task_key: task}
        popens = {task.task_key: process}
        callbacks: list[tuple[RunningTask, bool, str | None]] = []

        _monitor_running(
            config=Mock(task_retry_max=1),
            store=store,
            running=running,
            popens=popens,
            segment_terminal_callback=lambda item, success, note: callbacks.append((item, success, note)),
        )

        self.assertEqual(callbacks, [(task, True, None)])
        self.assertEqual(running, {})
        self.assertEqual(popens, {})
        store.finish.assert_called_once()

    def test_mautic_failure_stays_in_finished_error_and_next_item_claims(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[10, 20],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state, claimed = claim_item(
            state,
            entity_id=10,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.1,
        )
        self.assertTrue(claimed)
        state = mark_running(state, entity_id=10, now_ts=100.2)
        state = mark_terminal(state, entity_id=10, success=False, note="mautic_failed", now_ts=100.3)
        self.assertEqual(state["errors"], [10])
        self.assertEqual(state["error_notes"], {"10": "mautic_failed"})
        state, next_claimed = claim_item(
            state,
            entity_id=20,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.4,
        )
        self.assertTrue(next_claimed)
        self.assertEqual(state["errors"], [10])
        self.assertEqual(state["claimed"], [20])

    def test_mautic_failure_is_reevaluated_in_next_ordinary_cycle(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[10],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state = mark_terminal(state, entity_id=10, success=False, note="mautic_failed", now_ts=100.1)
        state, added, started = sync_cycle(
            state,
            planned_ids=[10],
            never_built=[],
            running_ids=[],
            now_ts=101.0,
        )
        self.assertTrue(started)
        self.assertEqual(added, [10])
        self.assertEqual(state["items"], [10])
        self.assertEqual(state["errors"], [])

    def test_lost_taskstore_pid_rebinds_to_surviving_mautic_process(self) -> None:
        with self.subTest("restart adoption"):
            store = self.RuntimeStore()
            task = RunningTask(
                row_id=7,
                root="/var/www/site",
                task_key="/var/www/site|segment|10",
                task_type="segment",
                entity_id=10,
                command_str="php|bin/console|mautic:segments:update|-i|10",
                timeout_sec=60,
                attempts=1,
                started_at=100.0,
                pid=111,
            )
            running = {task.task_key: task}
            with patch.object(daemon_mod, "_is_pid_alive", return_value=False), patch.object(
                daemon_mod,
                "list_external_runtime_task_summaries",
                return_value=[
                    {
                        "root": task.root,
                        "task_type": "segment",
                        "entity_id": 10,
                        "pid": 222,
                        "started_at": 101.0,
                        "command_str": task.command_str,
                    }
                ],
            ):
                stats = _sync_external_running_tasks(
                    installs=[SimpleNamespace(root=task.root)],
                    running=running,
                    popens={},
                    store=store,
                )
            self.assertEqual(stats["rebound"], 1)
            self.assertEqual(task.pid, 222)
            self.assertIs(running[task.task_key], task)

    def test_lost_pid_with_db_evidence_keeps_task_claimed(self) -> None:
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|10",
            task_type="segment",
            entity_id=10,
            command_str="php|bin/console|mautic:segments:update|-i|10",
            timeout_sec=60,
            attempts=1,
            started_at=100.0,
            pid=123,
        )
        store = Mock()
        store.get_runtime_sync.return_value = {}
        running = {task.task_key: task}
        callbacks: list[tuple[RunningTask, bool, str | None]] = []
        with patch.object(daemon_mod, "_is_pid_alive", return_value=False):
            _monitor_running(
                config=SimpleNamespace(task_retry_max=1, worker_watchdog_sec=0, segment_kill_grace_sec=0),
                store=store,
                running=running,
                popens={},
                segment_terminal_callback=lambda item, success, note: callbacks.append((item, success, note)),
                segment_liveness_probe=lambda _task: {"db": True},
            )
        self.assertIn(task.task_key, running)
        self.assertEqual(callbacks, [])
        store.finish.assert_not_called()

    def test_lost_segment_pid_without_runtime_evidence_is_scheduler_error(self) -> None:
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|151",
            task_type="segment",
            entity_id=151,
            command_str="php|bin/console|mautic:segments:update|-i|151",
            timeout_sec=60,
            attempts=1,
            started_at=100.0,
            pid=123,
        )
        store = Mock()
        store.get_runtime_sync.return_value = {}
        callbacks: list[tuple[RunningTask, bool, str | None]] = []
        with patch.object(daemon_mod, "_is_pid_alive", return_value=False):
            _monitor_running(
                config=SimpleNamespace(task_retry_max=1, worker_watchdog_sec=0, segment_kill_grace_sec=0),
                store=store,
                running={task.task_key: task},
                popens={},
                segment_terminal_callback=lambda item, success, note: callbacks.append((item, success, note)),
                segment_liveness_probe=lambda _task: {},
            )
        self.assertEqual(callbacks, [(task, False, "scheduler_error")])
        self.assertEqual(store.finish.call_args.kwargs["note"], "scheduler_error")

    def test_lost_segment_pid_with_fresh_mautic_build_finishes_green(self) -> None:
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|151",
            task_type="segment",
            entity_id=151,
            command_str="php|bin/console|mautic:segments:update|-i|151",
            timeout_sec=60,
            attempts=1,
            started_at=100.0,
            pid=123,
        )
        store = Mock()
        store.get_runtime_sync.return_value = {}
        callbacks: list[tuple[RunningTask, bool, str | None]] = []
        running = {task.task_key: task}
        with patch.object(daemon_mod, "_is_pid_alive", return_value=False):
            _monitor_running(
                config=SimpleNamespace(task_retry_max=1, worker_watchdog_sec=0, segment_kill_grace_sec=0),
                store=store,
                running=running,
                popens={},
                segment_terminal_callback=lambda item, success, note: callbacks.append((item, success, note)),
                segment_liveness_probe=lambda _task: {"completed": True},
            )
        self.assertEqual(callbacks, [(task, True, None)])
        self.assertEqual(running, {})
        store.finish.assert_called_once_with(1, state="done", rc=0, note=None)

    def test_actual_segment_process_failure_is_mautic_failed(self) -> None:
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|151",
            task_type="segment",
            entity_id=151,
            command_str="php|bin/console|mautic:segments:update|-i|151",
            timeout_sec=60,
            attempts=1,
            started_at=100.0,
            pid=123,
        )
        process = Mock()
        process.poll.return_value = 1
        store = Mock()
        running = {task.task_key: task}
        callbacks: list[tuple[RunningTask, bool, str | None]] = []
        _monitor_running(
            config=SimpleNamespace(task_retry_max=1, task_retry_delay_sec=0, worker_watchdog_sec=0),
            store=store,
            running=running,
            popens={task.task_key: process},
            segment_terminal_callback=lambda item, success, note: callbacks.append((item, success, note)),
        )
        self.assertEqual(callbacks, [(task, False, "mautic_failed")])
        self.assertEqual(store.finish.call_args.kwargs["note"], "mautic_failed")

    def test_admission_denial_persists_exact_gate(self) -> None:
        store = self.RuntimeStore()
        _record_segment_admission_rejection(
            store,
            root="/var/www/site",
            entity_id=10,
            reason="host_capacity",
            now_ts=100.0,
        )
        payload = store.get_runtime_sync(_segment_admission_rejections_key("/var/www/site"))
        self.assertEqual(payload["entries"]["10"]["reason"], "host_capacity")
        self.assertEqual(payload["entries"]["10"]["count"], 1)

    def test_terminal_wakes_waiting_dispatcher_and_next_claim_is_immediate(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[10, 20],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state, claimed = claim_item(
            state,
            entity_id=10,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.1,
        )
        self.assertTrue(claimed)
        state = mark_running(state, entity_id=10, now_ts=100.2)

        class RuntimeStore:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload

            def get_runtime_sync(self, _key: str) -> dict[str, object]:
                return self.payload

            def put_runtime_sync(self, _key: str, payload: dict[str, object]) -> None:
                self.payload = payload

        store = RuntimeStore(state)
        wakeup = threading.Event()
        waiter_started = threading.Event()
        waiter_done = threading.Event()

        def wait_for_dispatch() -> None:
            waiter_started.set()
            waiter_done.set() if wakeup.wait(timeout=60.0) else None

        waiter = threading.Thread(target=wait_for_dispatch, daemon=True)
        waiter.start()
        self.assertTrue(waiter_started.wait(timeout=1.0))
        task = RunningTask(
            row_id=1,
            root="/var/www/site",
            task_key="/var/www/site|segment|10",
            task_type="segment",
            entity_id=10,
            command_str="php mautic segments:update --id=10",
            timeout_sec=60,
            attempts=1,
            started_at=time.time(),
            pid=123,
        )
        _segment_terminal_and_wake(store, wakeup, task, True, None)
        waiter.join(timeout=1.0)
        self.assertTrue(waiter_done.is_set())
        next_state, next_claimed = claim_item(
            store.payload,
            entity_id=20,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=100.4,
        )
        self.assertTrue(next_claimed)
        self.assertEqual(next_state["done"], [10])


if __name__ == "__main__":
    unittest.main()
