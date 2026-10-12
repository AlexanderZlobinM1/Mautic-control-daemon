from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.config import load_config
from mcd_agent.daemon import (
    TaskStore,
    _segment_cycle_admission_inputs,
    _segment_cycle_claim,
    _segment_cycle_pending_order,
    _segment_cycle_pending_ids,
    _segment_cycle_release,
    _segment_cycle_running,
    _segment_cycle_terminal,
    _terminalize_segment_data_errors,
    _active_persisted_segment_cycle_roots,
    _effective_segment_regular_slot_limit,
    _segment_pending_dispatch_limits,
    _segment_shared_slots_available,
    _segment_lazy_update_effective_setting,
    _segment_full_scan_admission_enabled,
    _fill_from_ring,
    _iter_instances_with_root_dispatch_gate,
    _SchedulerRuntimeRegistry,
    _task_key,
)
from mcd_agent.segment_admission import (
    cycle_key,
    mark_terminal,
    ordinary_snapshot_order,
    sync_cycle,
)
from mcd_agent.logical_issues import detect_segment_logical_issues, logical_issue_terminal_segment_notes
from mcd_agent.daemon import RunningTask


def _leadlist_filter(segment_id: int) -> str:
    value = str(int(segment_id))
    return (
        'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";s:4:"type";s:8:"leadlist";'
        's:8:"operator";s:2:"in";s:6:"filter";a:1:{'
        f'i:0;s:{len(value)}:"{value}";}}}}'
    )


class SegmentLivenessTests(unittest.TestCase):
    """M6/M7 dispatch invariants for persisted ordinary-cycle liveness."""

    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            command_timeout_sec=3600,
            segment_full_scan_interval_sec=300,
            scheduler_host_max_parallel=2,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
        )

    def test_full_cycle_is_default_and_lazy_flag_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcd.toml"
            config_path.write_text("[runtime]\n", encoding="utf-8")
            default_cfg = load_config(str(config_path), allow_recover_from_mcc=False)
            self.assertFalse(default_cfg.segment_lazy_update_enabled)

            config_path.write_text(
                "[runtime]\nsegment_lazy_update_enabled = true\n",
                encoding="utf-8",
            )
            lazy_cfg = load_config(str(config_path), allow_recover_from_mcc=False)
            self.assertTrue(lazy_cfg.segment_lazy_update_enabled)

    def test_mixed_instance_lazy_policy_uses_uid_aliases_and_host_fallback(self) -> None:
        config = SimpleNamespace(
            segment_lazy_update_enabled=False,
            segment_lazy_update_instance_settings={
                "instance-a": True,
                "/var/www/b": {"lazy": False},
                "instance-c@host-a": {"enabled": True},
            },
        )
        installs = [
            SimpleNamespace(instance_uid="instance-a", root="/var/www/a", name="A"),
            SimpleNamespace(instance_uid="instance-b", root="/var/www/b", name="B"),
            SimpleNamespace(instance_uid="instance-c", root="/var/www/c", name="C"),
            SimpleNamespace(instance_uid="instance-d", root="/var/www/d", name="D"),
        ]
        self.assertEqual(
            [_segment_lazy_update_effective_setting(config, inst) for inst in installs],
            [True, False, True, False],
        )

        qualified = SimpleNamespace(instance_uid="instance-c", root="/var/www/c", name="C")
        qualified_config = SimpleNamespace(
            segment_lazy_update_enabled=False,
            segment_lazy_update_instance_settings={"instance-c@host-a": {"enabled": True}},
        )
        self.assertTrue(_segment_lazy_update_effective_setting(qualified_config, qualified))

    def test_forced_full_discovery_never_admits_fresh_tail_for_lazy_instance(self) -> None:
        self.assertFalse(
            _segment_full_scan_admission_enabled(
                lazy_segment_plan=True,
                force_segment_full_scan=True,
                cluster_cron_allowed=True,
            )
        )
        self.assertTrue(
            _segment_full_scan_admission_enabled(
                lazy_segment_plan=False,
                force_segment_full_scan=True,
                cluster_cron_allowed=True,
            )
        )
        self.assertFalse(
            _segment_full_scan_admission_enabled(
                lazy_segment_plan=False,
                force_segment_full_scan=True,
                cluster_cron_allowed=False,
            )
        )

    def test_mixed_lazy_gate_keeps_important_before_full_remaining_and_freezes_toggle(self) -> None:
        rows = [
            {"id": 101, "last_built_date": None, "date_modified": 190},
            {"id": 102, "last_built_date": 195, "date_modified": 195},
        ]
        groups_lazy: dict[str, list[int]] = {}
        lazy_ids = ordinary_snapshot_order(
            rows,
            [101, 102],
            stale_after_sec=100,
            now_ts=200,
            include_fresh_remaining=False,
            groups=groups_lazy,
        )
        groups_full: dict[str, list[int]] = {}
        full_ids = ordinary_snapshot_order(
            rows,
            [101, 102],
            stale_after_sec=100,
            now_ts=200,
            include_fresh_remaining=True,
            groups=groups_full,
        )
        self.assertEqual(lazy_ids, [101])
        self.assertEqual(groups_lazy["important"], [101])
        self.assertEqual(groups_lazy["remaining"], [])
        self.assertEqual(full_ids, [101, 102])
        self.assertEqual(groups_full["important"], [101])
        self.assertEqual(groups_full["remaining"], [102])

        frozen, _added, started = sync_cycle(
            None,
            planned_ids=full_ids,
            never_built=[],
            running_ids=[],
            now_ts=200.0,
            important_ids=groups_full["important"],
            remaining_ids=groups_full["remaining"],
            lazy_mode=False,
        )
        self.assertTrue(started)
        toggled, _added, started = sync_cycle(
            frozen,
            planned_ids=lazy_ids,
            never_built=[],
            running_ids=[],
            now_ts=201.0,
            important_ids=groups_lazy["important"],
            remaining_ids=[],
            lazy_mode=True,
        )
        self.assertFalse(started)
        self.assertEqual(toggled["items"], frozen["items"])
        self.assertEqual(toggled["cycle_id"], frozen["cycle_id"])
        self.assertEqual(toggled["plan_mode"], "full")

    def test_mixed_mode_toggle_uses_dispatcher_and_defers_reverse_tail(self) -> None:
        """UID policy changes never strand or inject fresh work into a live cycle."""

        root_a = "/var/www/mixed-a/public_html"
        root_b = "/var/www/mixed-b/public_html"
        a_ids = [101, 102, 103]
        b_ids = [201, 202, 203]
        store = TaskStore(":memory:")
        running: dict[str, daemon.RunningTask] = {}
        dispatch_config = self._config()
        dispatch_config.scheduler_emergency_reserved_slots = 0

        full_a, _added, started = sync_cycle(
            None,
            planned_ids=a_ids,
            never_built=[101],
            running_ids=[],
            now_ts=100.0,
            important_ids=[101],
            remaining_ids=[102, 103],
            lazy_mode=False,
        )
        full_b, _added, started_b = sync_cycle(
            None,
            planned_ids=b_ids,
            never_built=[201],
            running_ids=[],
            now_ts=100.0,
            important_ids=[201],
            remaining_ids=[202, 203],
            lazy_mode=False,
        )
        self.assertTrue(started)
        self.assertTrue(started_b)
        store.put_runtime_sync(cycle_key(root_a), full_a)
        store.put_runtime_sync(cycle_key(root_b), full_b)

        next_pid = iter(range(8101, 8120))

        def state(root: str) -> dict[str, object]:
            payload = store.get_runtime_sync(cycle_key(root))
            self.assertIsInstance(payload, dict)
            return dict(payload)

        def launch_next(root: str) -> daemon.RunningTask:
            current = state(root)
            pending = _segment_cycle_pending_order(current)
            self.assertTrue(pending)
            entity_id = int(pending[0])
            eligible = [
                *[int(value) for value in current.get("items") or []],
                *[int(value) for value in current.get("follow_up") or []],
            ]
            launched = self._launch_persisted_item(
                store=store,
                root=root,
                entity_id=entity_id,
                eligible_ids=eligible,
                running=running,
                pid=0,
                config=dispatch_config,
            )
            self.assertEqual(launched, 1)
            task = next(
                task
                for task in running.values()
                if task.root == root and int(task.entity_id or 0) == entity_id
            )
            return task

        def finish(task: daemon.RunningTask, *, now_ts: float) -> None:
            store.finish(task.row_id, state="done", rc=0, note=None)
            _segment_cycle_terminal(
                store,
                root=task.root,
                entity_id=int(task.entity_id or 0),
                success=True,
                note=None,
                now_ts=now_ts,
            )
            running.pop(task.task_key, None)

        with patch.object(
            daemon,
            "_spawn_command",
            side_effect=lambda **_kwargs: SimpleNamespace(pid=next(next_pid)),
        ), patch.object(
            daemon,
            "_task_locked_args",
            side_effect=lambda _key, args, **_kwargs: args,
        ), patch.object(
            daemon,
            "_scheduler_baseline_launch_due",
            return_value=False,
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None), patch.object(
            daemon, "_SCHEDULER_IMPORTANT_PENDING_ROOTS", frozenset()
        ), patch.object(daemon, "_SCHEDULER_DISTRIBUTION_READY_ROOTS", ()), patch.object(
            daemon, "_SCHEDULER_DISTRIBUTION_SERVED_ROOTS", set()
        ), patch.object(daemon, "_ENTITY_LAUNCH_GUARD", {}):
            # The important item on each root is actually dispatched through
            # the same ring/TaskStore path used by the scheduler.
            finish(launch_next(root_a), now_ts=101.0)
            finish(launch_next(root_b), now_ts=101.0)

            a_before_toggle = state(root_a)
            b_before_toggle = state(root_b)
            self.assertEqual(a_before_toggle["status"], "active")
            self.assertEqual(a_before_toggle["remaining_items"], [102, 103])
            self.assertEqual(b_before_toggle["plan_mode"], "full")
            self.assertEqual(b_before_toggle["remaining_items"], [202, 203])

            # A -> lazy: the admitted full tail stays assigned to the old
            # cycle and is not erased by the new UID policy.
            a_lazy, added, started_new = sync_cycle(
                a_before_toggle,
                planned_ids=[101],
                never_built=[],
                running_ids=[],
                now_ts=102.0,
                important_ids=[101],
                remaining_ids=[],
                lazy_mode=True,
            )
            full_to_lazy_added = list(added)
            full_to_lazy_started = bool(started_new)
            self.assertFalse(started_new)
            self.assertEqual(added, [])
            self.assertEqual(a_lazy["items"], a_before_toggle["items"])
            self.assertEqual(a_lazy["remaining_items"], [102, 103])
            self.assertEqual(a_lazy["follow_up"], [])
            self.assertEqual(a_lazy["plan_mode"], "full")
            store.put_runtime_sync(cycle_key(root_a), a_lazy)

            # B stays full while A's previously admitted remaining tail is
            # dispatched. Launch one item on each root concurrently to prove
            # the per-root mode toggle is not a shared-host barrier.
            a_second = launch_next(root_a)
            b_second = launch_next(root_b)
            self.assertEqual(a_second.entity_id, 102)
            self.assertEqual(b_second.entity_id, 202)
            finish(a_second, now_ts=103.0)
            finish(b_second, now_ts=103.0)
            finish(launch_next(root_a), now_ts=104.0)
            finish(launch_next(root_b), now_ts=104.0)

            a_terminal = state(root_a)
            b_terminal = state(root_b)
            self.assertEqual(a_terminal["status"], "terminal")
            self.assertEqual(a_terminal["done"], a_ids)
            self.assertEqual(b_terminal["status"], "terminal")
            self.assertEqual(b_terminal["done"], b_ids)

            # The next A cycle uses lazy mode and therefore has no fresh
            # remaining tail.
            a_lazy_next, _added, started_new = sync_cycle(
                a_terminal,
                planned_ids=[101],
                never_built=[],
                running_ids=[],
                now_ts=105.0,
                important_ids=[101],
                remaining_ids=[],
                lazy_mode=True,
            )
            self.assertTrue(started_new)
            self.assertEqual(a_lazy_next["items"], [101])
            self.assertEqual(a_lazy_next["remaining_items"], [])
            self.assertEqual(a_lazy_next["plan_mode"], "lazy")

            # lazy -> full while that lazy cycle is active must not append
            # fresh rows to its follow-up. They appear only after the active
            # lazy cycle reaches terminal and a new full cycle is admitted.
            a_still_lazy, added, started_new = sync_cycle(
                a_lazy_next,
                planned_ids=a_ids,
                never_built=[],
                running_ids=[],
                now_ts=106.0,
                important_ids=[101],
                remaining_ids=[102, 103],
                lazy_mode=False,
            )
            lazy_to_full_added = list(added)
            lazy_to_full_started = bool(started_new)
            self.assertFalse(started_new)
            self.assertEqual(added, [])
            self.assertEqual(a_still_lazy["items"], [101])
            self.assertEqual(a_still_lazy["follow_up"], [])
            self.assertEqual(a_still_lazy["remaining_items"], [])
            self.assertEqual(a_still_lazy["plan_mode"], "lazy")
            store.put_runtime_sync(cycle_key(root_a), a_still_lazy)

            finish(launch_next(root_a), now_ts=107.0)
            a_lazy_terminal = state(root_a)
            self.assertEqual(a_lazy_terminal["status"], "terminal")
            a_full_next, _added, started_new = sync_cycle(
                a_lazy_terminal,
                planned_ids=a_ids,
                never_built=[],
                running_ids=[],
                now_ts=108.0,
                important_ids=[101],
                remaining_ids=[102, 103],
                lazy_mode=False,
            )
            self.assertTrue(started_new)
            self.assertEqual(a_full_next["items"], a_ids)
            self.assertEqual(a_full_next["remaining_items"], [102, 103])
            self.assertEqual(a_full_next["plan_mode"], "full")

            receipt = {
                "schema": "mcd-mixed-mode-dispatch-receipt-v1",
                "dispatcher": "_launch_persisted_item",
                "durable_store": "TaskStore",
                "roots": {
                    "A": {
                        "initial_cycle_id": full_a["cycle_id"],
                        "initial_plan_mode": full_a["plan_mode"],
                        "full_to_lazy": {
                            "cycle_id": a_lazy["cycle_id"],
                            "started_new": full_to_lazy_started,
                            "added": full_to_lazy_added,
                            "active_plan_mode": a_lazy["plan_mode"],
                            "remaining_items_preserved": a_lazy["remaining_items"],
                        },
                        "old_full_cycle_terminal": {
                            "cycle_id": a_terminal["cycle_id"],
                            "status": a_terminal["status"],
                            "done": a_terminal["done"],
                        },
                        "next_lazy_cycle": {
                            "cycle_id": a_lazy_next["cycle_id"],
                            "started_new": True,
                            "plan_mode": a_lazy_next["plan_mode"],
                            "items": a_lazy_next["items"],
                            "remaining_items": a_lazy_next["remaining_items"],
                        },
                        "lazy_to_full_while_active": {
                            "cycle_id": a_still_lazy["cycle_id"],
                            "started_new": lazy_to_full_started,
                            "added": lazy_to_full_added,
                            "plan_mode": a_still_lazy["plan_mode"],
                            "follow_up": a_still_lazy["follow_up"],
                            "remaining_items": a_still_lazy["remaining_items"],
                        },
                        "next_full_cycle": {
                            "cycle_id": a_full_next["cycle_id"],
                            "started_new": True,
                            "plan_mode": a_full_next["plan_mode"],
                            "items": a_full_next["items"],
                            "remaining_items": a_full_next["remaining_items"],
                        },
                    },
                    "B": {
                        "initial_cycle_id": full_b["cycle_id"],
                        "plan_mode": b_before_toggle["plan_mode"],
                        "terminal": {
                            "cycle_id": b_terminal["cycle_id"],
                            "status": b_terminal["status"],
                            "done": b_terminal["done"],
                        },
                    },
                },
                "concurrent_dispatch": {
                    "roots": [root_a, root_b],
                    "entities": [int(a_second.entity_id), int(b_second.entity_id)],
                    "pids": [int(a_second.pid), int(b_second.pid)],
                    "same_host_global_barrier": False,
                },
                "assertions": {
                    "old_full_cycle_terminal_before_next_lazy": True,
                    "active_lazy_toggle_does_not_append_full_tail": True,
                    "new_full_cycle_admits_deferred_tail": True,
                    "queue_claims_and_cycle_ids_are_durable": True,
                },
            }
            print("MIXED_MODE_RECEIPT " + json.dumps(receipt, sort_keys=True))

        store.close()

    def test_main_and_root_local_use_the_same_snapshot_planner(self) -> None:
        self.assertIs(daemon._ordinary_segment_snapshot_order, ordinary_snapshot_order)
        self.assertIn("ll.last_built_date IS NULL", daemon._SQL_SEGMENTS_LATE_DISCOVERY)
        self.assertNotIn("OR ll.date_modified", daemon._SQL_SEGMENTS_LATE_DISCOVERY)

    def test_lazy_snapshot_does_not_materialize_fresh_tail(self) -> None:
        rows = [
            {"id": 101, "last_built_date": 190, "date_modified": 190},
            {"id": 102, "last_built_date": 195, "date_modified": 195},
        ]
        self.assertEqual(
            ordinary_snapshot_order(
                rows,
                [101, 102],
                stale_after_sec=100,
                now_ts=200,
                include_fresh_remaining=False,
            ),
            [],
        )

    def test_root_dispatch_gate_is_independent_and_exception_safe(self) -> None:
        busy_roots: set[str] = set()
        lock = threading.RLock()
        installs = [SimpleNamespace(root="/var/www/root-a"), SimpleNamespace(root="/var/www/root-b")]
        gated = _iter_instances_with_root_dispatch_gate(
            installs,
            busy_roots=busy_roots,
            lock=lock,
        )

        first = next(gated)
        self.assertEqual(first.root, "/var/www/root-a")
        self.assertEqual(busy_roots, {"/var/www/root-a"})

        second = next(gated)
        self.assertEqual(second.root, "/var/www/root-b")
        self.assertEqual(busy_roots, {"/var/www/root-b"})

        gated.close()
        self.assertEqual(busy_roots, set())

        registry = _SchedulerRuntimeRegistry()
        registry["a"] = SimpleNamespace(task_key="a")
        snapshot = registry.values()
        registry["b"] = SimpleNamespace(task_key="b")
        self.assertEqual([item.task_key for item in snapshot], ["a"])

    def test_recurring_priority_leaves_slot_for_admitted_ordinary_cycle(self) -> None:
        root = "/srv/mautic"
        store = SimpleNamespace(
            get_runtime_sync=lambda key: {"status": "active", "items": [151], "done": [], "claimed": [], "running": []},
            list_runtime_sync=lambda prefix: [],
        )
        config = SimpleNamespace(
            segment_mode="farm",
            segment_priority_parallel_idle=1,
            segment_regular_parallel_idle=1,
            segment_priority_parallel_throttled=1,
            segment_regular_parallel_throttled=1,
        )
        entry = SimpleNamespace(segment_id=58, max_interval_sec=60, instance_uid="instance-1")
        executor = SimpleNamespace()
        with patch.object(daemon, "_segment_shared_slots_available", return_value=1), patch.object(
            daemon, "_segment_cycle_key", return_value="segment-cycle:" + root
        ):
            launched = daemon._dispatch_recurring_priority_segments(
                config=config,
                inst=SimpleNamespace(root=root, instance_uid="instance-1"),
                store=store,
                running={},
                priority_executor=executor,
                entries=[entry],
                enabled=True,
            )
        self.assertEqual(launched, 0)

    def test_full_scan_pending_is_admitted_before_claim_guard(self) -> None:
        pending = {151}
        planned = daemon._reconcile_full_scan_pending(
            pending,
            [151],
            full_scan=False,
            published_ids={151},
        )
        state = {"status": "active", "items": [], "done": [], "errors": [], "claimed": [], "running": []}
        cycle_planned, _ = _segment_cycle_admission_inputs(
            state,
            planned,
            [],
            pressure=False,
        )
        self.assertEqual(cycle_planned, [151])

    def test_topology_data_errors_finish_root_and_allow_next_cycle(self) -> None:
        root = "/var/www/topology-data-error/public_html"
        store = TaskStore(":memory:")
        logical_issues = detect_segment_logical_issues(
            [
                {"id": 101, "is_published": 1, "filters": _leadlist_filter(102)},
                {"id": 102, "is_published": 1, "filters": _leadlist_filter(101)},
                {"id": 103, "is_published": 1, "filters": _leadlist_filter(101)},
                {"id": 104, "is_published": 1, "filters": "a:0:{}"},
            ]
        )
        topology_notes = logical_issue_terminal_segment_notes(
            {"issues": [issue.payload() for issue in logical_issues]}
        )
        self.assertEqual(
            topology_notes,
            {
                101: "data_error:dependency_cycle",
                102: "data_error:dependency_cycle",
                103: "data_error:dependency_cycle",
            },
        )
        state, _added, started_new = sync_cycle(
            None,
            planned_ids=[101, 102, 103, 104],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        self.assertTrue(started_new)
        store.put_runtime_sync(cycle_key(root), state)

        updated, terminalized = _terminalize_segment_data_errors(
            store,
            root=root,
            state=state,
            terminal_notes=topology_notes,
            now_ts=101.0,
        )
        self.assertEqual(terminalized, [101, 102, 103])
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["errors"], [101, 102, 103])
        self.assertEqual(_segment_cycle_pending_ids(updated), {104})
        self.assertEqual(updated["error_notes"]["101"], "data_error:dependency_cycle")

        _segment_cycle_terminal(
            store,
            root=root,
            entity_id=104,
            success=True,
            note=None,
            now_ts=102.0,
        )
        terminal = store.get_runtime_sync(cycle_key(root))
        self.assertEqual(terminal["status"], "terminal")
        self.assertEqual(terminal["errors"], [101, 102, 103])
        self.assertEqual(terminal["done"], [104])

        next_cycle, _added, next_started = sync_cycle(
            terminal,
            planned_ids=[101, 102, 103, 104],
            never_built=[],
            running_ids=[],
            now_ts=103.0,
        )
        self.assertTrue(next_started)
        self.assertNotEqual(next_cycle["cycle_id"], terminal["cycle_id"])
        self.assertEqual(next_cycle["items"], [101, 102, 103, 104])
        store.close()

    def test_pressure_held_candidate_never_enters_orphan_ordinary_ring(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[153],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state = mark_terminal(state, entity_id=153, success=True, now_ts=101.0)
        planned, never_built = _segment_cycle_admission_inputs(
            state,
            [154],
            [154],
            pressure=True,
        )
        self.assertEqual(planned, [])
        self.assertEqual(never_built, [])

        next_state, _added, _started = sync_cycle(
            state,
            planned_ids=planned,
            never_built=never_built,
            running_ids=[],
            now_ts=102.0,
        )
        self.assertNotIn(154, next_state["items"])
        self.assertEqual(_segment_cycle_pending_order(next_state), [])

        # A planner candidate that was not durably admitted must not be
        # projected into the ordinary ring merely because it was observed.
        self.assertEqual(
            _segment_cycle_pending_order(
                {
                    "status": "active",
                    "items": [153],
                    "done": [153],
                    "errors": [],
                    "claimed": [],
                    "running": [],
                }
            ),
            [],
        )

    def test_heavy_plan_deferral_tracks_only_nonterminal_persisted_cycles(self) -> None:
        root = "/var/www/active-cycle/public_html"
        other_root = "/var/www/terminal-cycle/public_html"
        store = TaskStore(":memory:")
        active, _added, _started = sync_cycle(
            None,
            planned_ids=[101, 102, 103],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        terminal, _added, _started = sync_cycle(
            None,
            planned_ids=[201],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        terminal = mark_terminal(terminal, entity_id=201, success=True, now_ts=101.0)
        store.put_runtime_sync(cycle_key(root), active)
        store.put_runtime_sync(cycle_key(other_root), terminal)

        self.assertEqual(
            _active_persisted_segment_cycle_roots(store, [root, other_root]),
            {root},
        )

        active = mark_terminal(active, entity_id=101, success=True, now_ts=102.0)
        active = mark_terminal(active, entity_id=102, success=True, now_ts=103.0)
        active = mark_terminal(active, entity_id=103, success=True, now_ts=104.0)
        store.put_runtime_sync(cycle_key(root), active)
        self.assertEqual(_active_persisted_segment_cycle_roots(store, [root, other_root]), set())
        store.close()

    def test_slow_root_liveness_probe_does_not_hold_shared_dispatch_lock(self) -> None:
        """A slow A probe must not prevent an independent B launch."""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            root_a = "/var/www/root-a"
            root_b = "/var/www/root-b"
            task_a = RunningTask(
                row_id=0,
                root=root_a,
                task_key=f"{root_a}|segment|101",
                task_type="segment",
                entity_id=101,
                command_str="php bin/console mautic:segments:update -i 101",
                timeout_sec=3600,
                attempts=1,
                started_at=time.time(),
                pid=9101,
            )
            running = {task_a.task_key: task_a}
            popens: dict[str, object] = {}
            entered = threading.Event()
            release = threading.Event()

            config = SimpleNamespace(
                worker_watchdog_sec=0,
                segment_kill_grace_sec=1,
                scheduler_pause_flag_path=str(Path(td) / "scheduler.pause"),
                scheduler_host_max_parallel=0,
                scheduler_instance_max_parallel=0,
                scheduler_elastic_slots_enabled=True,
                scheduler_emergency_reserved_slots=0,
            )

            def slow_segment_probe(_task: RunningTask) -> dict[str, object]:
                entered.set()
                release.wait(timeout=2.0)
                return {"active": True}

            monitor_thread = threading.Thread(
                target=daemon._monitor_running,
                kwargs={
                    "config": config,
                    "store": store,
                    "running": running,
                    "popens": popens,
                    "segment_liveness_probe": slow_segment_probe,
                },
                daemon=True,
            )
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
                daemon, "_pid_matches_task_command", return_value=False
            ), patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=9201)), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_host_slots_available", return_value=None), patch.object(
                daemon, "_scheduler_instance_slots_available", return_value=None
            ):
                monitor_thread.start()
                self.assertTrue(entered.wait(timeout=1.0))
                started = time.monotonic()
                launched = daemon._submit_if_slot_uncoordinated(
                    config=config,
                    store=store,
                    running=running,
                    root=root_b,
                    task_type="segment",
                    entity_id=202,
                    args=["fixture", "segment", "202"],
                    timeout_sec=60,
                    max_parallel_for_type=1,
                    popens=popens,
                    bypass_repeat_guard=True,
                    bypass_baseline_admission=True,
                )
                elapsed = time.monotonic() - started
                release.set()
                monitor_thread.join(timeout=1.0)

            self.assertTrue(launched)
            self.assertLess(elapsed, 0.5)
            self.assertIn(f"{root_b}|segment|202", running)
            store.close()

    def test_daemon_reconciles_prior_terminal_marker_before_pending_projection(self) -> None:
        root = "/var/www/alex/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[61, 132],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state["done"] = [132]
        state["claimed"] = [61]
        state["running"] = [61]
        state["status"] = "active"
        store.put_runtime_sync(cycle_key(root), state)
        store.put_runtime_sync(
            daemon._segment_runtime_marker_key(root, 61),
            {
                "schema": "mcd-segment-runtime-marker-v1",
                "root": root,
                "entity_id": 61,
                "status": "terminal",
                "cycle_id": "previous-cycle",
                "started_at": 99.0,
                "pid": 0,
                "command_str": "php mautic segments:update --id=61",
            },
        )
        with patch.object(daemon, "_refresh_scheduler_important_root"):
            repaired, released = daemon._segment_cycle_reconcile_orphans(
                store,
                root=root,
                running={},
                now_ts=101.0,
            )
        self.assertEqual(released, [61])
        self.assertEqual(repaired["claimed"], [])
        self.assertEqual(repaired["running"], [])
        self.assertEqual(daemon._segment_cycle_pending_order(repaired), [61])
        self.assertEqual(store.get_runtime_sync(cycle_key(root))["claimed"], [])
        store.close()

    def test_canonical_lifecycle_monitor_is_root_local(self) -> None:
        """A slow A probe cannot delay B terminal cleanup in the same monitor."""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            root_a = "/var/www/root-a"
            root_b = "/var/www/root-b"
            task_a = RunningTask(
                row_id=0,
                root=root_a,
                task_key=f"{root_a}|segment|101",
                task_type="segment",
                entity_id=101,
                command_str="php bin/console mautic:segments:update -i 101",
                timeout_sec=3600,
                attempts=1,
                started_at=time.time(),
                pid=9101,
            )
            task_b = RunningTask(
                row_id=0,
                root=root_b,
                task_key=f"{root_b}|job:ordinary|",
                task_type="job:ordinary",
                entity_id=None,
                command_str="php bin/console mautic:segments:update",
                timeout_sec=3600,
                attempts=1,
                started_at=time.time(),
                pid=9102,
            )
            running = {task_a.task_key: task_a, task_b.task_key: task_b}
            popens = {task_b.task_key: SimpleNamespace(poll=lambda: 0)}
            entered = threading.Event()
            release = threading.Event()
            config = SimpleNamespace(
                worker_watchdog_sec=0,
                segment_kill_grace_sec=1,
                scheduler_pause_flag_path=str(Path(td) / "scheduler.pause"),
                scheduler_host_max_parallel=0,
                scheduler_instance_max_parallel=0,
                scheduler_elastic_slots_enabled=True,
                scheduler_emergency_reserved_slots=0,
                task_retry_max=1,
                task_retry_delay_sec=0,
            )

            def slow_segment_probe(_task: RunningTask) -> dict[str, object]:
                entered.set()
                release.wait(timeout=2.0)
                return {"active": True}

            monitor_a = threading.Thread(
                target=daemon._monitor_running,
                kwargs={
                    "config": config,
                    "store": store,
                    "running": running,
                    "popens": popens,
                    "segment_liveness_probe": slow_segment_probe,
                    "roots": {root_a},
                },
                daemon=True,
            )
            with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
                daemon, "_pid_matches_task_command", return_value=False
            ):
                monitor_a.start()
                self.assertTrue(entered.wait(timeout=1.0))
                started = time.monotonic()
                daemon._monitor_running(
                    config=config,
                    store=store,
                    running=running,
                    popens=popens,
                    roots={root_b},
                )
                elapsed = time.monotonic() - started
                release.set()
                monitor_a.join(timeout=1.0)

            self.assertLess(elapsed, 0.5)
            self.assertNotIn(task_b.task_key, running)
            self.assertIn(task_a.task_key, running)
            store.close()

    def test_persisted_full_scan_item_bypasses_stale_repeat_cadence(self) -> None:
        root = "/var/www/full-scan/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None, planned_ids=[151], never_built=[], running_ids=[], now_ts=100.0
        )
        store.put_runtime_sync(cycle_key(root), state)
        with patch.dict(
            daemon._ENTITY_LAUNCH_GUARD,
            {daemon._entity_launch_guard_key(root, "segment", 151): 200.0},
        ), patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=151)), patch.object(
            daemon, "_segment_runtime_marker_write"
        ):
            launched = _fill_from_ring(
                ring=deque([151]), ring_limit=1, total_limit=1, root=root,
                task_type="segment", running={}, ring_entities={151},
                config=self._config(), store=store, popens={},
                build_args=lambda sid: ["php", "segments:update", str(sid)],
                admission_claim=lambda sid: _segment_cycle_claim(
                    store, root=root, entity_id=sid, eligible_ids=[151],
                    blocked_ids=set(), running_ids=set(), now_ts=201.0,
                ),
                bypass_repeat_guard_entities={151}, bypass_baseline_admission=True,
            )
        self.assertEqual(launched, 1)

    def test_super_priority_does_not_consume_regular_slot_for_persisted_pending_item(self) -> None:
        root = "/var/www/farm02/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[135, 154],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state = mark_terminal(state, entity_id=135, success=True, now_ts=101.0)
        store.put_runtime_sync(cycle_key(root), state)

        config = self._config()
        config.scheduler_host_max_parallel = 0
        config.scheduler_instance_max_parallel = 0
        config.segment_mode = "farm"
        config.segment_priority_parallel_idle = 0
        config.segment_regular_parallel_idle = 1

        class ActiveSuperPriority:
            @staticmethod
            def active_count(active_root: str | None, task_types: set[str]) -> int:
                if active_root == root and task_types == daemon._STATIC_SEGMENT_TASK_TYPES:
                    return 1
                return 0

        running: dict[str, daemon.RunningTask] = {}
        with patch.object(daemon, "_ACTIVE_PRIORITY_EXECUTOR", ActiveSuperPriority()), patch.object(
            daemon, "_spawn_command", return_value=SimpleNamespace(pid=9154)
        ), patch.object(
            daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
        ), patch.object(
            daemon, "_scheduler_baseline_launch_due", return_value=False
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
            pending_slot_limit = min(
                1,
                _segment_shared_slots_available(
                    running,
                    root,
                    1,
                    store=store,
                    config=config,
                    now_ts=102.0,
                ),
            )
            launched = _fill_from_ring(
                ring=deque([154]),
                ring_limit=pending_slot_limit,
                total_limit=pending_slot_limit,
                root=root,
                task_type="segment",
                running=running,
                ring_entities={154},
                config=config,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda sid: _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=sid,
                    eligible_ids=[135, 154],
                    blocked_ids=set(),
                    running_ids=set(),
                    now_ts=102.0,
                ),
                admission_running=lambda sid: _segment_cycle_running(
                    store,
                    root=root,
                    entity_id=sid,
                    now_ts=103.0,
                ),
                admission_release=lambda sid: None,
                bypass_repeat_guard_entities={154},
                bypass_baseline_admission=True,
                max_launches=pending_slot_limit,
            )

        self.assertEqual(pending_slot_limit, 1)
        self.assertEqual(launched, 1)
        self.assertEqual(running[next(iter(running))].entity_id, 154)
        self.assertEqual(store.get_runtime_sync(cycle_key(root))["running"], [154])
        store.close()

    def test_restart_pending_batch_refills_m6_m7_with_independent_super_lane(self) -> None:
        pending_ids = [154, 147, 144, 149, 151]
        for major in (6, 7):
            for throttled in (False, True):
                with self.subTest(mautic_major=major, throttled=throttled), tempfile.TemporaryDirectory() as td:
                    root = f"/var/www/b2bss-m{major}/public_html"
                    store = TaskStore(str(Path(td) / "state.db"))
                    state, _added, started = sync_cycle(
                        None,
                        planned_ids=pending_ids,
                        never_built=[],
                        running_ids=[],
                        now_ts=100.0,
                    )
                    self.assertTrue(started)
                    cycle_id = state["cycle_id"]
                    store.put_runtime_sync(cycle_key(root), state)
                    store.close()
                    store = TaskStore(str(Path(td) / "state.db"))
                    recovered = store.get_runtime_sync(cycle_key(root))
                    self.assertEqual(_segment_cycle_pending_order(recovered), pending_ids)

                    config = self._config()
                    config.scheduler_host_max_parallel = 0
                    config.scheduler_instance_max_parallel = 0
                    config.segment_mode = "farm"
                    config.segment_priority_parallel_idle = 8
                    config.segment_regular_parallel_idle = 3
                    config.segment_priority_parallel_throttled = 2
                    config.segment_regular_parallel_throttled = 0
                    config.segment_throttle_whitelist_only = True
                    config.segment_throttle_whitelist_parallel = 2
                    pending_limit = max(
                        1,
                        _effective_segment_regular_slot_limit(config, throttled),
                    )
                    self.assertEqual(pending_limit, 3 if not throttled else 1)

                    class ActiveSuperPriority:
                        @staticmethod
                        def active_count(active_root: str | None, task_types: set[str]) -> int:
                            if active_root == root and task_types == daemon._STATIC_SEGMENT_TASK_TYPES:
                                return 1
                            return 0

                    running: dict[str, daemon.RunningTask] = {}
                    pids = iter(range(9100 + major * 10 + int(throttled) * 5, 9200))

                    def launch_batch(entity_ids: list[int]) -> int:
                        ring = deque(entity_ids)
                        return _fill_from_ring(
                            ring=ring,
                            ring_limit=pending_limit,
                            total_limit=pending_limit,
                            root=root,
                            task_type="segment",
                            running=running,
                            ring_entities=set(entity_ids),
                            config=config,
                            store=store,
                            popens={},
                            build_args=lambda sid: [
                                "php",
                                "bin/console",
                                "mautic:segments:update",
                                "-i",
                                str(sid),
                            ],
                            admission_claim=lambda sid: _segment_cycle_claim(
                                store,
                                root=root,
                                entity_id=sid,
                                eligible_ids=pending_ids,
                                blocked_ids=set(),
                                running_ids=set(),
                                now_ts=200.0,
                            ),
                            admission_running=lambda sid: _segment_cycle_running(
                                store,
                                root=root,
                                entity_id=sid,
                                now_ts=201.0,
                            ),
                            admission_release=lambda sid: _segment_cycle_release(
                                store,
                                root=root,
                                entity_id=sid,
                                now_ts=202.0,
                            ),
                            bypass_repeat_guard_entities=set(entity_ids),
                            bypass_baseline_admission=True,
                            max_launches=pending_limit,
                        )

                    with patch.object(daemon, "_ACTIVE_PRIORITY_EXECUTOR", ActiveSuperPriority()), patch.object(
                        daemon,
                        "_spawn_command",
                        side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids)),
                    ) as spawn, patch.object(
                        daemon,
                        "_task_locked_args",
                        side_effect=lambda _key, args, **_kwargs: args,
                    ), patch.object(
                        daemon,
                        "_scheduler_baseline_launch_due",
                        return_value=False,
                    ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
                        self.assertEqual(
                            _segment_shared_slots_available(
                                running,
                                root,
                                pending_limit,
                                store=store,
                                config=config,
                                now_ts=203.0,
                            ),
                            pending_limit,
                        )
                        first_batch = pending_ids[:pending_limit]
                        self.assertEqual(launch_batch(pending_ids), pending_limit)
                        self.assertEqual(
                            sorted(task.entity_id for task in running.values()),
                            sorted(first_batch),
                        )
                        self.assertEqual(spawn.call_count, pending_limit)
                        self.assertEqual(store.running_count(), pending_limit)
                        running_state = store.get_runtime_sync(cycle_key(root))
                        self.assertEqual(running_state["cycle_id"], cycle_id)
                        self.assertEqual(
                            running_state["running"],
                            first_batch,
                        )

                        for task in list(running.values()):
                            store.finish(task.row_id, state="done", rc=0, note=None)
                            _segment_cycle_terminal(
                                store,
                                root=root,
                                entity_id=int(task.entity_id),
                                success=True,
                                note=None,
                                now_ts=204.0,
                            )
                            running.pop(task.task_key, None)

                        remaining = pending_ids[pending_limit:]
                        completed = list(first_batch)
                        while remaining:
                            batch_size = min(pending_limit, len(remaining))
                            self.assertEqual(launch_batch(remaining), batch_size)
                            self.assertEqual(spawn.call_count, len(completed) + batch_size)
                            self.assertEqual(store.running_count(), batch_size)
                            refill_state = store.get_runtime_sync(cycle_key(root))
                            self.assertEqual(refill_state["cycle_id"], cycle_id)
                            self.assertEqual(refill_state["done"], completed)
                            self.assertEqual(refill_state["running"], remaining[:batch_size])

                            for task in list(running.values()):
                                store.finish(task.row_id, state="done", rc=0, note=None)
                                _segment_cycle_terminal(
                                    store,
                                    root=root,
                                    entity_id=int(task.entity_id),
                                    success=True,
                                    note=None,
                                    now_ts=205.0,
                                )
                                running.pop(task.task_key, None)
                            completed.extend(remaining[:batch_size])
                            remaining = remaining[batch_size:]

                    terminal = store.get_runtime_sync(cycle_key(root))
                    self.assertEqual(terminal["cycle_id"], cycle_id)
                    self.assertEqual(terminal["status"], "terminal")
                    self.assertEqual(terminal["done"], pending_ids)
                    rows = store.conn.execute(
                        "SELECT entity_id, state, pid FROM tasks WHERE root=? ORDER BY id",
                        (root,),
                    ).fetchall()
                    self.assertEqual([int(row["entity_id"]) for row in rows], pending_ids)
                    self.assertTrue(all(str(row["state"]) == "done" for row in rows))
                    self.assertTrue(all(int(row["pid"]) > 0 for row in rows))
                    store.close()

    def test_pending_refill_keeps_absolute_limit_when_workers_are_already_running(self) -> None:
        """Free capacity must limit launches, not shrink the total cap."""
        root = "/var/www/refill/public_html"
        store = TaskStore(":memory:")
        config = self._config()
        config.scheduler_host_max_parallel = 0
        config.scheduler_instance_max_parallel = 0
        config.segment_mode = "farm"
        config.segment_regular_parallel_idle = 3
        config.segment_regular_parallel_throttled = 0
        config.segment_throttle_whitelist_only = False

        existing = SimpleNamespace(
            root=root,
            task_type="segment",
            entity_id=101,
            task_key=f"{root}|segment|101",
        )
        running: dict[str, daemon.RunningTask] = {existing.task_key: existing}
        total_limit, launch_budget = _segment_pending_dispatch_limits(
            config,
            False,
            running,
            root,
            segment_slot_limit=3,
            priority_pending=True,
            store=store,
            now_ts=200.0,
        )
        self.assertEqual((total_limit, launch_budget), (3, 2))

        pids = iter((9102, 9103))
        with patch.object(
            daemon,
            "_spawn_command",
            side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids)),
        ), patch.object(
            daemon,
            "_task_locked_args",
            side_effect=lambda _key, args, **_kwargs: args,
        ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
            daemon, "_SEGMENT_DISPATCH_WAKEUP", None
        ):
            launched = _fill_from_ring(
                ring=deque([102, 103, 104]),
                ring_limit=total_limit,
                total_limit=total_limit,
                root=root,
                task_type="segment",
                running=running,
                ring_entities={102, 103, 104},
                config=config,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                bypass_repeat_guard_entities={102, 103, 104},
                bypass_baseline_admission=True,
                max_launches=launch_budget,
            )

        self.assertEqual(launched, 2)
        self.assertEqual(
            sorted(int(task.entity_id) for task in running.values()),
            [101, 102, 103],
        )
        self.assertEqual(store.running_count(), 2)
        store.close()

    def test_pending_cycle_borrows_unused_priority_capacity_within_instance_cap(self) -> None:
        root = "/var/www/full-budget/public_html"
        store = TaskStore(":memory:")
        config = self._config()
        config.scheduler_host_max_parallel = 12
        config.scheduler_instance_max_parallel = 6
        config.scheduler_emergency_reserved_slots = 0
        config.segment_mode = "farm"
        config.segment_priority_parallel_idle = 8
        config.segment_regular_parallel_idle = 3
        config.segment_priority_parallel_throttled = 2
        config.segment_regular_parallel_throttled = 0
        config.segment_throttle_whitelist_only = False

        running: dict[str, daemon.RunningTask] = {}
        total_limit, launch_budget = _segment_pending_dispatch_limits(
            config,
            False,
            running,
            root,
            segment_slot_limit=11,
            priority_pending=False,
            store=store,
            now_ts=200.0,
        )
        self.assertEqual(total_limit, 11)
        self.assertEqual(launch_budget, 11)

        pids = iter(range(9200, 9220))
        pending_ids = list(range(201, 221))
        with patch.object(
            daemon,
            "_spawn_command",
            side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids)),
        ), patch.object(
            daemon,
            "_task_locked_args",
            side_effect=lambda _key, args, **_kwargs: args,
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
            launched = _fill_from_ring(
                ring=deque(pending_ids),
                ring_limit=total_limit,
                total_limit=total_limit,
                root=root,
                task_type="segment",
                running=running,
                ring_entities=set(pending_ids),
                config=config,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin", "mautic:segments:update", "-i", str(sid)],
                bypass_repeat_guard_entities=set(pending_ids),
                bypass_baseline_admission=True,
                max_launches=launch_budget,
            )

        # The profile has 8+3=11 segment capacity, but this instance is
        # explicitly capped at 6; the pending lane must use all eligible
        # capacity without bypassing that safety cap.
        self.assertEqual(launched, 6)
        self.assertEqual(len(running), 6)
        self.assertEqual(store.running_count(), 6)
        store.close()

    def _launch_persisted_item(
        self,
        *,
        store: TaskStore,
        root: str,
        entity_id: int,
        eligible_ids: list[int],
        running: dict[str, daemon.RunningTask],
        pid: int,
        config: SimpleNamespace | None = None,
    ) -> int:
        def claim(sid: int) -> bool:
            return _segment_cycle_claim(
                store,
                root=root,
                entity_id=sid,
                eligible_ids=eligible_ids,
                blocked_ids=set(),
                running_ids=set(),
                now_ts=200.0,
            )

        def mark_running(sid: int) -> None:
            _segment_cycle_running(store, root=root, entity_id=sid, now_ts=201.0)

        return _fill_from_ring(
            ring=deque([entity_id]),
            ring_limit=1,
            total_limit=1,
            root=root,
            task_type="segment",
            running=running,
            ring_entities={entity_id},
            config=config or self._config(),
            store=store,
            popens={},
            build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
            admission_claim=claim,
            admission_running=mark_running,
            admission_release=lambda sid: _segment_cycle_release(
                store,
                root=root,
                entity_id=sid,
                now_ts=202.0,
            ),
            bypass_repeat_guard_entities={entity_id},
            bypass_baseline_admission=True,
            max_launches=1,
        )

    def test_m6_and_m7_persisted_last_claims_starts_and_only_then_next_cycle(self) -> None:
        for major in (6, 7):
            with self.subTest(mautic_major=major), tempfile.TemporaryDirectory() as td:
                root = f"/var/www/m{major}/public_html"
                store = TaskStore(str(Path(td) / "state.db"))
                items = [101, 102, 103]
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=items,
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                state = mark_terminal(state, entity_id=101, success=True, now_ts=101.0)
                state = mark_terminal(state, entity_id=102, success=True, now_ts=102.0)
                store.put_runtime_sync(cycle_key(root), state)
                store.close()
                store = TaskStore(str(Path(td) / "state.db"))
                recovered = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(_segment_cycle_pending_ids(recovered), {103})
                self.assertEqual(_segment_cycle_pending_order(recovered), [103])
                cycle_id = recovered["cycle_id"]

                running: dict[str, daemon.RunningTask] = {}
                process = SimpleNamespace(pid=7000 + major)
                with patch.object(daemon, "_spawn_command", return_value=process) as spawn, patch.object(
                    daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
                ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                    daemon, "_SEGMENT_DISPATCH_WAKEUP", None
                ):
                    launched = self._launch_persisted_item(
                        store=store,
                        root=root,
                        entity_id=103,
                        eligible_ids=items,
                        running=running,
                        pid=process.pid,
                    )

                self.assertEqual(launched, 1)
                spawn.assert_called_once()
                task_row = store.conn.execute(
                    "SELECT state, pid, started_at, entity_id FROM tasks WHERE root=? ORDER BY id DESC LIMIT 1",
                    (root,),
                ).fetchone()
                self.assertIsNotNone(task_row)
                self.assertEqual(task_row["state"], "running")
                self.assertEqual(task_row["pid"], process.pid)
                self.assertGreater(float(task_row["started_at"]), 0.0)
                self.assertEqual(task_row["entity_id"], 103)
                running_state = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(running_state["running"], [103])

                # A terminal callback must complete the old cycle before the
                # next planning observation is allowed to create a new one.
                _segment_cycle_terminal(
                    store,
                    root=root,
                    entity_id=103,
                    success=True,
                    note=None,
                    now_ts=202.0,
                )
                terminal = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(terminal["status"], "terminal")
                self.assertEqual(terminal["done"], items)
                self.assertEqual(terminal["cycle_id"], cycle_id)
                restarted, _added, started_new = sync_cycle(
                    terminal,
                    planned_ids=items,
                    never_built=[],
                    running_ids=[],
                    now_ts=203.0,
                )
                self.assertTrue(started_new)
                self.assertNotEqual(restarted["cycle_id"], terminal["cycle_id"])
                store.close()

    def test_new_admission_still_respects_global_baseline_gate(self) -> None:
        root = "/var/www/new-admission"
        store = TaskStore(":memory:")
        with patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
            daemon, "_spawn_command"
        ) as spawn:
            launched = daemon._submit_if_slot_uncoordinated(
                config=self._config(),
                store=store,
                running={},
                root=root,
                task_type="segment",
                entity_id=999,
                args=["fixture"],
                timeout_sec=60,
                max_parallel_for_type=1,
                popens={},
                bypass_baseline_admission=False,
            )
        self.assertFalse(launched)
        spawn.assert_not_called()
        store.close()

    def test_campaign_pressure_holds_new_admissions_but_keeps_m6_m7_pending(self) -> None:
        for major in (6, 7):
            with self.subTest(mautic_major=major):
                initial, _added, _started = sync_cycle(
                    None,
                    planned_ids=[11, 12],
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                initial = mark_terminal(initial, entity_id=11, success=True, now_ts=101.0)
                planned, never_built = _segment_cycle_admission_inputs(
                    initial,
                    [12, 13],
                    [13],
                    pressure=True,
                )
                self.assertEqual(planned, [12])
                self.assertEqual(never_built, [])
                held, added, started_new = sync_cycle(
                    deepcopy(initial),
                    planned_ids=planned,
                    never_built=never_built,
                    running_ids=[],
                    now_ts=102.0,
                )
                self.assertFalse(started_new)
                self.assertEqual(added, [])
                self.assertEqual(held["items"], [11, 12])
                self.assertEqual(_segment_cycle_pending_ids(held), {12})

                released_plan, released_never = _segment_cycle_admission_inputs(
                    held,
                    [12, 13],
                    [13],
                    pressure=False,
                )
                released, added, started_new = sync_cycle(
                    held,
                    planned_ids=released_plan,
                    never_built=released_never,
                    running_ids=[],
                    now_ts=103.0,
                )
                self.assertFalse(started_new)
                self.assertEqual(added, [13])
                self.assertEqual(released["items"], [11, 12])
                self.assertEqual(released["follow_up"], [13])

    def test_new_never_built_arrival_uses_follow_up_fifo_after_restart(self) -> None:
        root = "/var/www/b2bss/public_html"
        cycle_admitted_at = 7 * 3600 + 8 * 60 + 17
        new_segment_at = 11 * 3600 + 51 * 60 + 35
        source_before = {
            "3": {"modified_ts": 100.0, "built_ts": 90.0},
            "144": {"modified_ts": 101.0, "built_ts": 91.0},
        }
        source_after = {
            **source_before,
            "154": {"modified_ts": float(new_segment_at), "built_ts": None},
        }
        state, _added, started = sync_cycle(
            None,
            planned_ids=[3, 144],
            never_built=[],
            running_ids=[],
            now_ts=float(cycle_admitted_at),
            source_snapshot=source_before,
        )
        self.assertTrue(started)
        cycle_id = state["cycle_id"]

        state, added, started = sync_cycle(
            state,
            planned_ids=[3, 144, 154],
            never_built=[154],
            running_ids=[],
            now_ts=float(new_segment_at),
            source_snapshot=source_after,
        )
        self.assertFalse(started)
        self.assertEqual(added, [154])
        self.assertEqual(state["cycle_id"], cycle_id)
        self.assertEqual(state["items"], [3, 144])
        self.assertEqual(state["follow_up"], [154])
        self.assertEqual(_segment_cycle_pending_order(state), [154, 3, 144])

        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            store.put_runtime_sync(cycle_key(root), state)
            store.close()
            store = TaskStore(str(Path(td) / "state.db"))
            recovered = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(_segment_cycle_pending_order(recovered), [154, 3, 144])

            running: dict[str, daemon.RunningTask] = {}
            pending_order = _segment_cycle_pending_order(recovered)
            with patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=9154)) as spawn, patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                launched = _fill_from_ring(
                    ring=deque(pending_order),
                    ring_limit=1,
                    total_limit=1,
                    root=root,
                    task_type="segment",
                    running=running,
                    ring_entities=set(pending_order),
                    config=self._config(),
                    store=store,
                    popens={},
                    build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                    admission_claim=lambda sid: _segment_cycle_claim(
                        store,
                        root=root,
                        entity_id=sid,
                        eligible_ids=pending_order,
                        blocked_ids=set(),
                        running_ids=set(),
                        now_ts=float(new_segment_at),
                    ),
                    admission_running=lambda sid: _segment_cycle_running(
                        store,
                        root=root,
                        entity_id=sid,
                        now_ts=float(new_segment_at + 1),
                    ),
                    admission_release=lambda sid: None,
                    bypass_repeat_guard_entities=set(pending_order),
                    bypass_baseline_admission=True,
                    max_launches=1,
                )

            self.assertEqual(launched, 1)
            spawn.assert_called_once()
            self.assertEqual(running[next(iter(running))].entity_id, 154)
            running_state = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(running_state["items"], [3, 144])
            self.assertEqual(running_state["follow_up"], [154])
            self.assertEqual(running_state["running"], [154])

            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=154,
                success=False,
                note="fixture_failure",
                now_ts=float(new_segment_at + 2),
            )
            failed = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(failed["follow_up"], [154])
            self.assertEqual(failed["errors"], [154])
            self.assertEqual(_segment_cycle_pending_order(failed), [3, 144])
            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=3,
                success=True,
                note=None,
                now_ts=float(new_segment_at + 3),
            )
            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=144,
                success=True,
                note=None,
                now_ts=float(new_segment_at + 4),
            )
            terminal = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(terminal["status"], "terminal")
            retried, _added, started_new = sync_cycle(
                terminal,
                planned_ids=[3, 144, 154],
                never_built=[154],
                running_ids=[],
                now_ts=float(new_segment_at + 5),
            )
            self.assertTrue(started_new)
            self.assertEqual(retried["items"], [3, 144, 154])
            self.assertEqual(retried["follow_up"], [])
            store.close()

    def test_host_wide_free_slots_distribute_persisted_pending_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            running: dict[str, daemon.RunningTask] = {}
            roots = [f"/var/www/instance-{idx}" for idx in range(3)]
            for index, root in enumerate(roots, start=1):
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=[index],
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                store.put_runtime_sync(cycle_key(root), state)

            pids = iter([8101, 8102, 8103])
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                launched = [
                    self._launch_persisted_item(
                        store=store,
                        root=root,
                        entity_id=index,
                        eligible_ids=[index],
                        running=running,
                        pid=8100 + index,
                    )
                    for index, root in enumerate(roots, start=1)
                ]

            self.assertEqual(launched, [1, 0, 0])
            self.assertEqual(len(running), 1)
            # The explicit host cap is hard: the next roots wait while the
            # first real worker occupies the remaining effective slot.
            running.pop(_task_key(roots[0], "segment", 1))
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                self.assertEqual(
                    self._launch_persisted_item(
                        store=store,
                        root=roots[1],
                        entity_id=2,
                        eligible_ids=[2],
                        running=running,
                        pid=8102,
                    ),
                    1,
                )
            running.pop(_task_key(roots[1], "segment", 2))
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                self.assertEqual(
                    self._launch_persisted_item(
                        store=store,
                        root=roots[2],
                        entity_id=3,
                        eligible_ids=[3],
                        running=running,
                        pid=8103,
                    ),
                    1,
                )
            self.assertEqual(len(running), 1)
            self.assertEqual(
                sorted(task.entity_id for task in running.values()),
                [3],
            )
            for root, index in zip(roots, range(1, 4)):
                state = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(state["running"], [index])
            store.close()


if __name__ == "__main__":
    unittest.main()
