from __future__ import annotations

import json
import tempfile
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent.daemon import (
    RunningTask,
    TaskStore,
    _segment_cycle_claim,
    _segment_cycle_manual_prepare,
    _segment_cycle_manual_terminal,
    _monitor_running,
    _segment_runtime_marker_write,
    _submit_if_slot_uncoordinated,
    _segment_shared_slots_available,
    _segment_terminal_and_wake,
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

    def test_persisted_cycle_survives_several_minute_ticks_before_reset(self) -> None:
        store = self.RuntimeStore()
        root = "/var/www/site"
        key = cycle_key(root)
        planned = [101, 102, 103]
        source = {
            "101": {"modified_ts": 10, "built_ts": 1},
            "102": {"modified_ts": 20, "built_ts": 2},
            "103": {"modified_ts": 30, "built_ts": 3},
        }

        def tick(now_ts: float, running_ids: list[int]) -> tuple[dict[str, object], bool]:
            raw = store.get_runtime_sync(key)
            state, _added, started = sync_cycle(
                raw,
                planned_ids=planned,
                never_built=[],
                running_ids=running_ids,
                now_ts=now_ts,
                source_snapshot=source,
            )
            # Mirror TaskStore's JSON boundary so every minute tick reads a
            # fresh persisted state rather than an in-memory object alias.
            store.put_runtime_sync(key, json.loads(json.dumps(state)))
            return state, started

        state, started = tick(0.0, [])
        self.assertTrue(started)
        self.assertEqual(state["items"], planned)

        state, claimed = claim_item(
            state,
            entity_id=101,
            eligible_ids=planned,
            blocked_ids=[],
            running_ids=[],
            now_ts=1.0,
        )
        self.assertTrue(claimed)
        store.put_runtime_sync(key, json.loads(json.dumps(mark_running(state, entity_id=101, now_ts=2.0))))

        state, started = tick(60.0, [101])
        self.assertFalse(started)
        self.assertEqual(state["items"], planned)
        self.assertEqual(state["running"], [101])

        state = mark_terminal(state, entity_id=101, success=True, now_ts=61.0)
        store.put_runtime_sync(key, json.loads(json.dumps(state)))
        state, started = tick(120.0, [])
        self.assertFalse(started)
        self.assertEqual(state["done"], [101])

        state, claimed = claim_item(
            state,
            entity_id=102,
            eligible_ids=planned,
            blocked_ids=[],
            running_ids=[],
            now_ts=121.0,
        )
        self.assertTrue(claimed)
        store.put_runtime_sync(key, json.loads(json.dumps(mark_running(state, entity_id=102, now_ts=122.0))))
        state, started = tick(180.0, [102])
        self.assertFalse(started)
        self.assertEqual(state["items"], planned)

        state = mark_terminal(state, entity_id=102, success=True, now_ts=181.0)
        store.put_runtime_sync(key, json.loads(json.dumps(state)))
        state, started = tick(240.0, [])
        self.assertFalse(started)
        self.assertEqual(state["done"], [101, 102])

        state, claimed = claim_item(
            state,
            entity_id=103,
            eligible_ids=planned,
            blocked_ids=[],
            running_ids=[],
            now_ts=241.0,
        )
        self.assertTrue(claimed)
        store.put_runtime_sync(key, json.loads(json.dumps(mark_running(state, entity_id=103, now_ts=242.0))))
        state, started = tick(300.0, [103])
        self.assertFalse(started)
        self.assertEqual(state["items"], planned)

        state = mark_terminal(state, entity_id=103, success=True, now_ts=301.0)
        store.put_runtime_sync(key, json.loads(json.dumps(state)))
        self.assertEqual(state["status"], "terminal")
        self.assertEqual(state["done"], planned)

        restarted, started = tick(360.0, [])
        self.assertTrue(started)
        self.assertEqual(restarted["items"], planned)
        self.assertEqual(len(restarted["items"]), len(set(restarted["items"])))

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

    def test_lost_segment_marker_expires_without_self_refreshing(self) -> None:
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
        marker = {
            "schema": "mcd-segment-runtime-marker-v1",
            "status": "running",
            "started_at": 100.0,
            "heartbeat_at": 100.0,
            "last_independent_at": 100.0,
        }
        store = Mock()
        persisted = {"value": marker}
        store.get_runtime_sync.side_effect = lambda _key: persisted["value"]
        store.put_runtime_sync.side_effect = lambda _key, payload: persisted.__setitem__("value", dict(payload))
        _segment_runtime_marker_write(store, task, status="running", now_ts=100.0)
        running = {task.task_key: task}
        popens: dict[str, Mock] = {}
        callbacks: list[tuple[bool, str | None]] = []

        config = SimpleNamespace(
            task_retry_max=1,
            segment_retry_count=0,
            task_retry_delay_sec=0,
            worker_watchdog_sec=0,
        )
        with patch("mcd_agent.daemon._is_pid_alive", return_value=False):
            clock = {"now": 150.0}
            with patch("mcd_agent.daemon.time", SimpleNamespace(time=lambda: clock["now"])):
                _monitor_running(
                    config=config,
                    store=store,
                    running=running,
                    popens=popens,
                    segment_liveness_probe=lambda _task: {},
                    segment_terminal_callback=lambda _task, success, note: callbacks.append((success, note)),
                )
                self.assertIn(task.task_key, running)
                self.assertEqual(persisted["value"]["heartbeat_at"], 100.0)
                self.assertEqual(persisted["value"]["last_independent_at"], 100.0)
                self.assertEqual(persisted["value"]["lost_at"], 100.0)

                clock["now"] = 161.0
                _monitor_running(
                    config=config,
                    store=store,
                    running=running,
                    popens=popens,
                    segment_liveness_probe=lambda _task: {},
                    segment_terminal_callback=lambda _task, success, note: callbacks.append((success, note)),
                )

        self.assertNotIn(task.task_key, running)
        self.assertEqual(callbacks, [(False, "scheduler_error")])
        self.assertEqual(persisted["value"]["terminal_note"], "scheduler_error")

    def test_segment_retry_policy_runs_three_attempts_then_continues_next_item(self) -> None:
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
                self.next_row_id = 2
                self.finished: list[tuple[int, str, int | None, str | None]] = []

            def get_runtime_sync(self, _key: str) -> dict[str, object]:
                return self.payload

            def put_runtime_sync(self, _key: str, payload: dict[str, object]) -> None:
                self.payload = payload

            def recent_task_problem_counts(self, **_kwargs: object) -> dict[int, int]:
                return {}

            def add_running(self, _task: RunningTask) -> int:
                row_id = self.next_row_id
                self.next_row_id += 1
                return row_id

            def finish(self, row_id: int, state: str, rc: int | None, note: str | None) -> None:
                self.finished.append((row_id, state, rc, note))

        store = RuntimeStore(state)
        wakeup = threading.Event()
        starts: list[int] = [1]
        callbacks: list[tuple[int | None, bool, str | None]] = []

        def spawn(**_kwargs: object) -> Mock:
            process = Mock()
            process.pid = 100 + len(starts)
            process.poll.return_value = 23
            starts.append(process.pid)
            return process

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
            pid=100,
        )
        process = Mock()
        process.poll.return_value = 23
        running = {task.task_key: task}
        popens = {task.task_key: process}

        def terminal_callback(item: RunningTask, success: bool, note: str | None) -> None:
            callbacks.append((item.entity_id, success, note))
            _segment_terminal_and_wake(store, wakeup, item, success, note)

        with patch("mcd_agent.daemon._spawn_command", side_effect=spawn):
            for _ in range(3):
                _monitor_running(
                    config=SimpleNamespace(
                        task_retry_max=1,
                        segment_retry_count=2,
                        task_retry_delay_sec=0,
                    ),
                    store=store,
                    running=running,
                    popens=popens,
                    segment_terminal_callback=terminal_callback,
                )

        self.assertEqual(len(starts), 3)
        self.assertEqual(callbacks, [(10, False, "mautic_failed")])
        self.assertTrue(wakeup.is_set())
        self.assertEqual(store.finished[-1], (3, "failed", 23, "mautic_failed"))
        next_state, next_claimed = claim_item(
            store.payload,
            entity_id=20,
            eligible_ids=[10, 20],
            blocked_ids=[],
            running_ids=[],
            now_ts=101.0,
        )
        self.assertTrue(next_claimed)
        self.assertEqual(next_state["errors"], [10])

    def test_real_task_history_does_not_truncate_three_segment_starts(self) -> None:
        root = "/var/www/site"
        task_key = f"{root}|segment|10"
        callbacks: list[tuple[int | None, bool, str | None]] = []

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(f"{directory}/state.db")
            try:
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
                store.put_runtime_sync(cycle_key(root), state)

                task = RunningTask(
                    row_id=0,
                    root=root,
                    task_key=task_key,
                    task_type="segment",
                    entity_id=10,
                    command_str="php mautic segments:update --id=10",
                    timeout_sec=60,
                    attempts=1,
                    started_at=100.0,
                    pid=100,
                )
                task.row_id = store.add_running(task)
                running = {task_key: task}
                initial_process = Mock()
                initial_process.poll.return_value = 23
                popens = {task_key: initial_process}
                spawned: list[Mock] = []

                def spawn(**_kwargs: object) -> Mock:
                    process = Mock()
                    process.pid = 200 + len(spawned)
                    process.poll.return_value = 23
                    spawned.append(process)
                    return process

                def terminal_callback(item: RunningTask, success: bool, note: str | None) -> None:
                    callbacks.append((item.entity_id, success, note))
                    _segment_terminal_and_wake(store, threading.Event(), item, success, note)

                config = SimpleNamespace(
                    task_retry_max=1,
                    segment_retry_count=2,
                    task_retry_delay_sec=0,
                )
                with patch("mcd_agent.daemon._spawn_command", side_effect=spawn):
                    for _ in range(3):
                        _monitor_running(
                            config=config,
                            store=store,
                            running=running,
                            popens=popens,
                            segment_terminal_callback=terminal_callback,
                        )

                self.assertEqual(len(spawned), 2)
                self.assertEqual(
                    store.recent_task_problem_counts(
                        root=root,
                        task_type="segment",
                        since_sec=3600,
                    ),
                    {10: 3},
                )
                rows = store.conn.execute(
                    "SELECT attempts, state, rc FROM tasks WHERE task_key=? ORDER BY attempts",
                    (task_key,),
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1], row[2]) for row in rows],
                    [(1, "failed", 23), (2, "failed", 23), (3, "failed", 23)],
                )
                self.assertEqual(callbacks, [(10, False, "mautic_failed")])

                terminal_state = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(terminal_state["errors"], [10])
                terminal_state, next_claimed = claim_item(
                    terminal_state,
                    entity_id=20,
                    eligible_ids=[10, 20],
                    blocked_ids=[],
                    running_ids=[],
                    now_ts=101.0,
                )
                self.assertTrue(next_claimed)
                self.assertEqual(terminal_state["claimed"], [20])
            finally:
                store.close()

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
