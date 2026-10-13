from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from collections import deque
from contextlib import ExitStack
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
    _scheduler_persisted_pending_roots,
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

    def test_fairness_ignores_claimed_only_and_blocked_persisted_cycles(self) -> None:
        root = "/var/www/persisted-fairness/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[101, 102],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state["claimed"] = [101, 102]
        state["running"] = [101, 102]
        store.put_runtime_sync(cycle_key(root), state)
        self.assertEqual(_scheduler_persisted_pending_roots(store, [root]), set())

        state["claimed"] = []
        state["running"] = []
        store.put_runtime_sync(cycle_key(root), state)
        store.put_runtime_sync(
            daemon._segment_dispatch_context_key(root),
            {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "blocked_ids": [101, 102],
                "sql_managed_ids": [],
                "static_configured_ids": [],
                "scheduler_pause": False,
                "backup_pause": False,
                "dispatch_pause": False,
                "db_pause_until": 0.0,
                "cluster_cron_allowed": True,
            },
        )
        self.assertEqual(_scheduler_persisted_pending_roots(store, [root]), set())
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

    def test_persisted_refill_overlaps_main_planner_without_duplicate_launch(self) -> None:
        """An admitted item starts while the main root planner is held busy."""

        class StopMainLoop(BaseException):
            pass

        with tempfile.TemporaryDirectory() as td:
            root = "/var/www/refill-concurrency/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.1\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 1\n"
                "scheduler_instance_max_parallel = 1\n"
                "segment_mode = \"farm\"\n"
                "segment_priority_parallel_idle = 0\n"
                "segment_regular_parallel_idle = 1\n"
                "segment_priority_parallel_throttled = 0\n"
                "segment_regular_parallel_throttled = 1\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)

            cycle = None
            cycle, _added, _started = sync_cycle(
                cycle,
                planned_ids=[501],
                never_built=[],
                running_ids=[],
                now_ts=time.time(),
                important_ids=[],
                remaining_ids=[501],
                lazy_mode=False,
            )
            seed_store = TaskStore(str(state_path), config)
            seed_store.put_runtime_sync(cycle_key(root), cycle)
            seed_store.put_runtime_sync(
                daemon._segment_dispatch_context_key(root),
                {
                    "schema": "mcd-segment-dispatch-context-v1",
                    "root": root,
                    "mautic_major": 6,
                    "updated_at": time.time(),
                    "cycle_id": cycle["cycle_id"],
                    "cycle_status": "active",
                    "blocked_ids": [],
                    "sql_managed_ids": [],
                    "static_configured_ids": [],
                    "static_active_ids": [],
                    "active_import_target_ids": [],
                    "parents_by_child": {},
                    "children_by_parent": {},
                    "dependency_snapshot_authoritative": True,
                    "dependency_snapshot_mode": "live_read",
                    "dependency_snapshot_updated_at": time.time(),
                    "scheduler_pause": False,
                    "backup_pause": False,
                    "dispatch_pause": False,
                    "db_pause_until": 0.0,
                    "cluster_cron_allowed": True,
                    "cluster_import_allowed": False,
                    "throttled_active": False,
                    "priority_pending": False,
                    "segment_slot_limit": 1,
                },
            )
            seed_store.close()

            planner_started = threading.Event()
            launch_seen = threading.Event()
            stop_refill = threading.Event()
            spawned: list[tuple[str, int | None]] = []
            instances = [
                SimpleNamespace(
                    root=root,
                    db=object(),
                    instance_uid="refill-concurrency",
                    name="refill-concurrency",
                    mautic_major=6,
                    mautic_timezone="UTC",
                )
            ]
            real_thread = threading.Thread

            class FixtureInventory:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def count(self) -> int:
                    return len(instances)

                def list_instances(self) -> list[object]:
                    return list(instances)

                def rescan(self, *_args: object, **_kwargs: object) -> int:
                    return len(instances)

            class BlockingTaskStore(TaskStore):
                def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                    if (
                        threading.current_thread().name == "mcd-ordinary-persisted-refill"
                        and key == cycle_key(root)
                        and not planner_started.is_set()
                    ):
                        planner_started.wait(timeout=2.0)
                    return super().get_runtime_sync(key)

            class FakeMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: []

            class DisabledPusher:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def enabled(self) -> bool:
                    return False

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: None

            class FakeProcess:
                _next_pid = 44000

                def __init__(self) -> None:
                    type(self)._next_pid += 1
                    self.pid = type(self)._next_pid

                def poll(self) -> None:
                    return None

                def wait(self) -> int:
                    return 0

            def blocking_dispatch_gate(
                dispatch_installs: list[object],
                *,
                busy_roots: set[str],
                lock: threading.RLock,
            ):
                current = next(iter(dispatch_installs))
                current_root = str(getattr(current, "root") or "")
                with lock:
                    busy_roots.add(current_root)
                planner_started.set()
                try:
                    # The main planner stays inside its root-local section
                    # until refill has observed and launched the durable item.
                    launch_seen.wait(timeout=3.0)
                    yield current
                    stop_refill.set()
                    raise StopMainLoop()
                finally:
                    with lock:
                        busy_roots.discard(current_root)

            def spawn_fixture(**kwargs: object) -> FakeProcess:
                args = list(kwargs.get("args") or [])
                spawned.append((str(kwargs.get("root") or ""), int(args[-1])))
                launch_seen.set()
                return FakeProcess()

            def sync_external(**_kwargs: object) -> dict[str, int]:
                return {"observed": 0, "adopted": 0, "updated": 0, "released": 0}

            class ControlledThread(real_thread):
                def start(self) -> None:
                    # Keep this test focused on the actual persisted refill
                    # worker; other optional daemon loops are not part of the
                    # fixture and would outlive the temporary state DB.
                    if self.name != "mcd-ordinary-persisted-refill":
                        return
                    super().start()

            patches = [
                patch.object(daemon, "TaskStore", BlockingTaskStore),
                patch.object(daemon, "InstanceInventory", FixtureInventory),
                patch.object(daemon, "MauticDB", FakeMauticDB),
                patch.object(daemon, "MCCStatePusher", DisabledPusher),
                patch.object(daemon, "_ensure_mcd_service_kill_mode", return_value=False),
                patch.object(daemon, "ensure_nginx_4xx_scan_safety", return_value={"status": "ok"}),
                patch.object(daemon, "ensure_managed_web_firewall_loopback", return_value={"status": "ok"}),
                patch.object(daemon, "fetch_custom_manifest", return_value=([], "fixture")),
                patch.object(daemon, "ensure_seeded", return_value=1),
                patch.object(daemon, "resolve_agent_identity", return_value={"clone_detected": False}),
                patch.object(daemon, "_load_orphans_from_store", return_value={}),
                patch.object(daemon, "_bootstrap_remote_runtime_before_local_baseline", return_value="none"),
                patch.object(daemon, "_initialize_local_runtime_baseline", return_value=({}, "baseline")),
                patch.object(daemon, "_sync_segment_whitelist_file"),
                patch.object(daemon, "_apply_instance_runtime_guard"),
                patch.object(daemon, "_sync_and_publish_form_embed_status", return_value=False),
                patch.object(daemon, "retire_zabbix_mautic_version_userparameter", return_value={"status": "ok"}),
                patch.object(daemon, "_monitor_running"),
                patch.object(daemon, "_reconcile_running_state", return_value={}),
                patch.object(daemon, "_sync_external_running_tasks", side_effect=sync_external),
                patch.object(daemon, "_iter_instances_with_root_dispatch_gate", side_effect=blocking_dispatch_gate),
                patch.object(
                    daemon,
                    "render_mautic_command",
                    side_effect=lambda **kwargs: ["fixture", "segment", str(kwargs.get("id") or 0)],
                ),
                patch.object(daemon, "_spawn_command", side_effect=spawn_fixture),
                patch.object(daemon, "cluster_route_allows", return_value=True),
                patch.object(daemon, "backup_lock_active", return_value=False),
                patch.object(daemon.threading, "Thread", ControlledThread),
            ]
            try:
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertRaises(StopMainLoop):
                        daemon.run_loop(config, single_cycle=False, _test_stop_event=stop_refill)
            finally:
                stop_refill.set()
                refill_threads = [
                    thread
                    for thread in threading.enumerate()
                    if thread.name == "mcd-ordinary-persisted-refill"
                ]
                for thread in refill_threads:
                    thread.join(timeout=2.0)

            self.assertTrue(planner_started.is_set())
            self.assertTrue(launch_seen.is_set())
            self.assertEqual(spawned, [(root, 501)])
            check_store = TaskStore(str(state_path), config)
            state_after = check_store.get_runtime_sync(cycle_key(root))
            self.assertEqual(state_after["running"], [501])
            self.assertEqual(state_after["claimed"], [501])
            self.assertEqual(_segment_cycle_pending_order(state_after), [])
            self.assertEqual(len(check_store.running_rows()), 1)
            check_store.close()

    def test_persisted_refill_fairness_survives_hot_completion_reorder_and_full_cap(self) -> None:
        """Root-local refill keeps a durable tail out of the hot-root loop.

        This drives the real persisted-cycle claim/submit path while the
        caller order rotates, a new root is added and removed, and the shared
        host cap is initially
        full. The pre-1.5.149 input-order reset would let the first hot root
        claim every simulated completion; the canonical served-root receipt
        must give the tail a turn without opening a global barrier.
        """

        tail_root = "/var/www/maestro/public_html"
        hot_a = "/var/www/agromanija/public_html"
        hot_b = "/var/www/berlitz/public_html"
        added_root = "/var/www/new-root/public_html"
        roots = [hot_a, hot_b, tail_root]
        store = TaskStore(":memory:")
        config = self._config()
        config.scheduler_host_max_parallel = 1
        config.scheduler_instance_max_parallel = 1
        config.segment_mode = "farm"
        config.segment_regular_parallel_idle = 1
        config.segment_regular_parallel_throttled = 1

        for root, first_id in ((hot_a, 100), (hot_b, 200), (tail_root, 900)):
            state, _added, _started = sync_cycle(
                None,
                planned_ids=list(range(first_id, first_id + 40)),
                never_built=[],
                running_ids=[],
                now_ts=0.0,
                remaining_ids=list(range(first_id, first_id + 40)),
                lazy_mode=False,
            )
            store.put_runtime_sync(cycle_key(root), state)

        running: dict[str, daemon.RunningTask | SimpleNamespace] = {}
        popens: dict[str, object] = {}
        spawned: list[tuple[str, int]] = []
        pending_since = {root: 0.0 for root in roots}
        current_key: str | None = None
        current_root: str | None = None
        next_pid = 50000

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def wait(self, timeout: float | None = None) -> int:
                _ = timeout
                return 0

            def poll(self) -> None:
                return None

        def spawn_fixture(**kwargs: object) -> FakeProcess:
            nonlocal next_pid
            next_pid += 1
            args = list(kwargs.get("args") or [])
            root = str(kwargs.get("root") or "")
            entity_id = int(args[-1])
            spawned.append((root, entity_id))
            return FakeProcess(next_pid)

        def complete_previous() -> None:
            nonlocal current_key, current_root
            if not current_key or not current_root:
                return
            task = running.pop(current_key)
            popens.pop(current_key, None)
            store.finish(int(task.row_id), state="done", rc=0, note="fixture_complete")
            daemon._segment_cycle_terminal(
                store,
                root=current_root,
                entity_id=int(task.entity_id or 0),
                success=True,
                note=None,
                now_ts=float(len(spawned)),
            )
            pending_since.pop(current_root, None)
            current_key = None
            current_root = None

        def refill_one(root: str) -> int:
            state = store.get_runtime_sync(cycle_key(root)) or {}
            pending = _segment_cycle_pending_order(state)
            if not pending:
                return 0

            def claim(entity_id: int) -> bool:
                eligible = _segment_cycle_pending_order(store.get_runtime_sync(cycle_key(root)) or {})
                return _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=int(entity_id),
                    eligible_ids=eligible,
                    blocked_ids=set(),
                    running_ids={
                        int(getattr(task, "entity_id", 0) or 0)
                        for task in running.values()
                        if str(getattr(task, "root", "") or "") == root
                    },
                    now_ts=float(len(spawned)),
                )

            def mark_running(entity_id: int) -> None:
                daemon._segment_cycle_running(
                    store,
                    root=root,
                    entity_id=int(entity_id),
                    now_ts=float(len(spawned)),
                )

            return _fill_from_ring(
                ring=deque(pending),
                ring_limit=1,
                total_limit=1,
                root=root,
                task_type="segment",
                running=running,
                ring_entities=set(pending),
                config=config,
                store=store,
                popens=popens,
                build_args=lambda sid: ["php", "segments:update", str(sid)],
                admission_claim=claim,
                admission_running=mark_running,
                admission_release=lambda sid: _segment_cycle_release(
                    store,
                    root=root,
                    entity_id=int(sid),
                    now_ts=float(len(spawned)),
                ),
                bypass_repeat_guard_entities=set(pending),
                bypass_baseline_admission=True,
                max_launches=1,
            )

        with patch.object(daemon, "_spawn_command", side_effect=spawn_fixture), patch.object(
            daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
        ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
            daemon, "_SEGMENT_DISPATCH_WAKEUP", None
        ), patch.object(daemon, "_segment_runtime_marker_write"):
            for tick in range(60):
                complete_previous()

                if tick == 10:
                    added_state, _added, _started = sync_cycle(
                        None,
                        planned_ids=[700, 701],
                        never_built=[],
                        running_ids=[],
                        now_ts=float(tick),
                        remaining_ids=[700, 701],
                        lazy_mode=False,
                    )
                    store.put_runtime_sync(cycle_key(added_root), added_state)
                    roots.append(added_root)
                if tick == 20:
                    roots.remove(added_root)

                # Keep the hot roots at the head of the rotating caller order
                # and move the tail position on every pass. The distribution
                # gate must preserve the prior served receipt rather than
                # treating this as a fresh round.
                raw_order = [hot_a, hot_b, tail_root] if tick % 2 == 0 else [hot_b, hot_a, tail_root]
                if added_root in roots:
                    raw_order.insert(1, added_root)
                pending_roots = _scheduler_persisted_pending_roots(store, roots)
                dispatch, _promoted = daemon._fairness_watchdog_dispatch_installs(
                    [SimpleNamespace(root=root) for root in raw_order],
                    pending_roots=pending_roots,
                    pending_since_by_root=pending_since,
                    now_ts=float(tick),
                    watchdog_sec=300,
                )
                daemon._set_scheduler_distribution_roots(
                    [inst.root for inst in dispatch],
                    worker_slots=1,
                    cursor=0 if tick == 0 else None,
                )

                # At tick zero the shared cap is occupied, so no root may
                # launch, but this is a capacity observation, not a
                # scheduler-wide pause/barrier. The real main-planner-busy
                # hand-off is covered by the run_loop regression below.
                if tick == 0:
                    incumbent_key = f"{hot_a}|segment|9999"
                    running[incumbent_key] = SimpleNamespace(
                        root=hot_a,
                        task_type="segment",
                        entity_id=9999,
                        task_key=incumbent_key,
                    )
                for inst in dispatch:
                    root = str(inst.root)
                    if root not in roots:
                        continue
                    if not _segment_cycle_pending_order(store.get_runtime_sync(cycle_key(root)) or {}):
                        continue
                    launched = refill_one(root)
                    if launched:
                        current_key = next(
                            key for key, task in running.items()
                            if str(getattr(task, "root", "") or "") == root
                        )
                        current_root = root
                        break

                if tick == 0:
                    running.pop(f"{hot_a}|segment|9999", None)

        try:
            self.assertIn(tail_root, [root for root, _entity in spawned])
            tail_launches = [index for index, (root, _entity) in enumerate(spawned) if root == tail_root]
            self.assertTrue(tail_launches)
            self.assertLessEqual(tail_launches[0], 4)
            self.assertEqual(len({(root, entity) for root, entity in spawned}), len(spawned))
            self.assertIn(added_root, {root for root, _entity in spawned})
            self.assertEqual(
                daemon._scheduler_persisted_pending_roots(store, [tail_root]),
                {tail_root},
            )
        finally:
            daemon._set_scheduler_distribution_roots([], worker_slots=1)
            store.close()

    def test_actual_persisted_refill_worker_serves_cold_root_while_hot_planner_is_busy(self) -> None:
        """The real refill thread must use the common fair submit boundary.

        The hot root continuously completes short workers while the main
        planner is held inside that root's dispatch gate. A blocked persisted
        root has pending work but its authoritative import-target guard makes
        that item locally unlaunchable; it must yield the ordinary fair turn
        and let the cold persisted root claim the first eligible free slot
        before the 300-second profile watchdog.
        """

        class StopMainLoop(BaseException):
            pass

        with tempfile.TemporaryDirectory() as td:
            hot_root = "/var/www/hot-root/public_html"
            blocked_root = "/var/www/blocked-root/public_html"
            cold_root = "/var/www/cold-root/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.05\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 1\n"
                "scheduler_instance_max_parallel = 1\n"
                "scheduler_fairness_watchdog_sec = 300\n"
                "segment_mode = \"farm\"\n"
                "segment_priority_parallel_idle = 0\n"
                "segment_regular_parallel_idle = 1\n"
                "segment_priority_parallel_throttled = 0\n"
                "segment_regular_parallel_throttled = 1\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)

            def seed_cycle(
                root: str,
                ids: list[int],
                running_ids: list[int] | None = None,
                active_import_target_ids: list[int] | None = None,
            ) -> None:
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=ids,
                    never_built=[],
                    running_ids=running_ids or [],
                    now_ts=100.0,
                    remaining_ids=ids,
                    lazy_mode=False,
                )
                store.put_runtime_sync(cycle_key(root), state)
                store.put_runtime_sync(
                    daemon._segment_dispatch_context_key(root),
                    {
                        "schema": "mcd-segment-dispatch-context-v1",
                        "root": root,
                        "mautic_major": 6,
                        "updated_at": time.time(),
                        "cycle_id": state["cycle_id"],
                        "cycle_status": "active",
                        "blocked_ids": [],
                        "sql_managed_ids": [],
                        "static_configured_ids": [],
                        "static_active_ids": [],
                        "parents_by_child": {},
                        "children_by_parent": {},
                        "dependency_snapshot_authoritative": True,
                        "dependency_snapshot_mode": "live_read",
                        "dependency_snapshot_updated_at": time.time(),
                        "scheduler_pause": False,
                        "backup_pause": False,
                        "dispatch_pause": False,
                        "db_pause_until": 0.0,
                        "cluster_cron_allowed": True,
                        "cluster_import_allowed": False,
                        "active_import_target_ids": active_import_target_ids or [],
                        "throttled_active": False,
                        "priority_pending": False,
                        "segment_slot_limit": 1,
                    },
                )

            store = TaskStore(str(state_path), config)
            seed_cycle(hot_root, list(range(1000, 1040)), running_ids=[1000])
            # Keep this root in the durable fairness set, but make its only
            # ordinary item locally unlaunchable through the authoritative
            # import-target guard. It must yield once capacity is free.
            seed_cycle(blocked_root, [3000], active_import_target_ids=[3000])
            seed_cycle(cold_root, list(range(2000, 2040)))
            store.close()

            planner_started = threading.Event()
            cold_launch_seen = threading.Event()
            stop_refill = threading.Event()
            spawned: list[tuple[str, int]] = []
            debug_events: list[str] = []
            completion_store: TaskStore | None = None
            instances = [
                SimpleNamespace(
                    root=hot_root,
                    db=object(),
                    instance_uid="hot-root",
                    name="hot-root",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
                SimpleNamespace(
                    root=blocked_root,
                    db=object(),
                    instance_uid="blocked-root",
                    name="blocked-root",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
                SimpleNamespace(
                    root=cold_root,
                    db=object(),
                    instance_uid="cold-root",
                    name="cold-root",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
            ]
            completion_thread: threading.Thread | None = None
            real_thread = threading.Thread

            class FixtureInventory:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def count(self) -> int:
                    return len(instances)

                def list_instances(self) -> list[object]:
                    return instances

                def rescan(self, *_args: object, **_kwargs: object) -> int:
                    return len(instances)

            class BlockingTaskStore(TaskStore):
                def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                    if key in {cycle_key(blocked_root), cycle_key(cold_root)}:
                        debug_events.append(f"read:{key}")
                    if (
                        threading.current_thread().name == "mcd-ordinary-persisted-refill"
                        and key in {cycle_key(blocked_root), cycle_key(cold_root)}
                        and not planner_started.is_set()
                    ):
                        planner_started.wait(timeout=3.0)
                    return super().get_runtime_sync(key)

            class FakeMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: []

            class DisabledPusher:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def enabled(self) -> bool:
                    return False

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: None

            class FakeProcess:
                _next_pid = 61000

                def __init__(self) -> None:
                    type(self)._next_pid += 1
                    self.pid = type(self)._next_pid

                def poll(self) -> None:
                    return None

                def wait(self) -> int:
                    return 0

            def spawn_fixture(**kwargs: object) -> FakeProcess:
                args = list(kwargs.get("args") or [])
                root = str(kwargs.get("root") or "")
                entity_id = int(args[-1])
                spawned.append((root, entity_id))
                debug_events.append(f"spawn:{root}:{entity_id}")
                if root == cold_root:
                    cold_launch_seen.set()
                return FakeProcess()

            def finish_hot_workers(
                running: dict[str, daemon.RunningTask],
                popens: dict[str, object],
                lifecycle_store: TaskStore,
            ) -> None:
                while not stop_refill.is_set():
                    with daemon._SCHEDULER_RUNTIME_LOCK:
                        hot_tasks = [
                            (key, task)
                            for key, task in list(running.items())
                            if str(getattr(task, "root", "") or "") == hot_root
                        ]
                        for key, _task in hot_tasks:
                            running.pop(key, None)
                            popens.pop(key, None)
                    for key, task in hot_tasks:
                        debug_events.append(f"complete:{key}")
                        entity_id = int(getattr(task, "entity_id", 0) or 0)
                        if entity_id > 0:
                            if int(getattr(task, "row_id", 0) or 0) > 0:
                                lifecycle_store.finish(
                                    int(task.row_id),
                                    state="done",
                                    rc=0,
                                    note="hot_fixture_complete",
                                )
                            daemon._segment_cycle_terminal(
                                lifecycle_store,
                                root=hot_root,
                                entity_id=entity_id,
                                success=True,
                                note=None,
                                now_ts=time.time(),
                            )
                    time.sleep(0.02)

            def sync_external(**kwargs: object) -> dict[str, int]:
                nonlocal completion_thread, completion_store
                running = kwargs["running"]
                popens = kwargs["popens"]
                if completion_thread is None:
                    debug_events.append("sync_external:init")
                    completion_store = TaskStore(str(state_path), config)
                    initial_key = f"{hot_root}|segment|1000"
                    running[initial_key] = RunningTask(
                        row_id=0,
                        root=hot_root,
                        task_key=initial_key,
                        task_type="segment",
                        entity_id=1000,
                        command_str="fixture hot segment",
                        timeout_sec=3600,
                        attempts=1,
                        started_at=time.time(),
                        pid=60999,
                    )
                    popens[initial_key] = FakeProcess()
                    completion_thread = real_thread(
                        target=finish_hot_workers,
                        args=(running, popens, completion_store),
                        name="fixture-hot-completion",
                        daemon=True,
                    )
                    completion_thread.start()
                return {"observed": 0, "adopted": 0, "updated": 0, "released": 0}

            real_shared_admission_skip = daemon._mark_scheduler_shared_admission_skip

            def record_shared_admission_skip(root: str, task_type: str) -> None:
                debug_events.append(f"skip:{root}:{task_type}")
                real_shared_admission_skip(root, task_type)

            real_set_distribution_roots = daemon._set_scheduler_distribution_roots

            def seed_prior_ordinary_turn(
                ready_roots: object,
                *,
                worker_slots: int,
                cursor: int | None = None,
            ) -> None:
                real_set_distribution_roots(ready_roots, worker_slots=worker_slots, cursor=cursor)
                debug_events.append(
                    "setroots:" + ",".join(daemon._SCHEDULER_DISTRIBUTION_READY_ROOTS)
                )
                # Recreate the preceding successful ordinary admission. The
                # next refill pass must therefore encounter blocked_root as
                # the only unserved root before cold_root can claim a slot.
                if hot_root in daemon._SCHEDULER_DISTRIBUTION_READY_ROOTS:
                    daemon._mark_scheduler_shared_admission(hot_root, "segment")

            def blocking_dispatch_gate(
                dispatch_installs: list[object],
                *,
                busy_roots: set[str],
                lock: threading.RLock,
            ):
                current = next(inst for inst in dispatch_installs if str(getattr(inst, "root") or "") == hot_root)
                with lock:
                    busy_roots.add(hot_root)
                planner_started.set()
                try:
                    # The main planner is held in the hot root. The refill
                    # thread must still drain cold_root independently.
                    if not cold_launch_seen.wait(timeout=4.0):
                        raise AssertionError(
                            f"cold persisted root was not served while hot planner was busy; spawned={spawned!r}; events={debug_events!r}"
                        )
                    stop_refill.set()
                    raise StopMainLoop()
                finally:
                    with lock:
                        busy_roots.discard(hot_root)
                yield current

            class ControlledThread(real_thread):
                def start(self) -> None:
                    if self.name != "mcd-ordinary-persisted-refill":
                        return
                    super().start()

            patches = [
                patch.object(daemon, "TaskStore", BlockingTaskStore),
                patch.object(daemon, "InstanceInventory", FixtureInventory),
                patch.object(daemon, "MauticDB", FakeMauticDB),
                patch.object(daemon, "MCCStatePusher", DisabledPusher),
                patch.object(daemon, "_ensure_mcd_service_kill_mode", return_value=False),
                patch.object(daemon, "ensure_nginx_4xx_scan_safety", return_value={"status": "ok"}),
                patch.object(daemon, "ensure_managed_web_firewall_loopback", return_value={"status": "ok"}),
                patch.object(daemon, "fetch_custom_manifest", return_value=([], "fixture")),
                patch.object(daemon, "ensure_seeded", return_value=1),
                patch.object(daemon, "resolve_agent_identity", return_value={"clone_detected": False}),
                patch.object(daemon, "_load_orphans_from_store", return_value={}),
                patch.object(daemon, "_bootstrap_remote_runtime_before_local_baseline", return_value="none"),
                patch.object(daemon, "_initialize_local_runtime_baseline", return_value=({}, "baseline")),
                patch.object(daemon, "_sync_segment_whitelist_file"),
                patch.object(daemon, "_apply_instance_runtime_guard"),
                patch.object(daemon, "_sync_and_publish_form_embed_status", return_value=False),
                patch.object(daemon, "retire_zabbix_mautic_version_userparameter", return_value={"status": "ok"}),
                patch.object(daemon, "_monitor_running"),
                patch.object(daemon, "_reconcile_running_state", return_value={}),
                patch.object(daemon, "_is_pid_alive", return_value=True),
                patch.object(daemon, "_sync_external_running_tasks", side_effect=sync_external),
                patch.object(
                    daemon,
                    "_mark_scheduler_shared_admission_skip",
                    side_effect=record_shared_admission_skip,
                ),
                patch.object(
                    daemon,
                    "_set_scheduler_distribution_roots",
                    side_effect=seed_prior_ordinary_turn,
                ),
                patch.object(daemon, "_iter_instances_with_root_dispatch_gate", side_effect=blocking_dispatch_gate),
                patch.object(
                    daemon,
                    "render_mautic_command",
                    side_effect=lambda **kwargs: ["fixture", "segment", str(kwargs.get("id") or 0)],
                ),
                patch.object(daemon, "_spawn_command", side_effect=spawn_fixture),
                patch.object(daemon, "cluster_route_allows", return_value=True),
                patch.object(daemon, "backup_lock_active", return_value=False),
                patch.object(daemon.threading, "Thread", ControlledThread),
            ]
            try:
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertRaises(StopMainLoop):
                        daemon.run_loop(config, single_cycle=False, _test_stop_event=stop_refill)
            finally:
                stop_refill.set()
                if completion_thread is not None:
                    completion_thread.join(timeout=2.0)
                if completion_store is not None:
                    completion_store.close()
                refill_threads = [
                    thread
                    for thread in threading.enumerate()
                    if thread.name == "mcd-ordinary-persisted-refill"
                ]
                for thread in refill_threads:
                    thread.join(timeout=2.0)

            self.assertTrue(planner_started.is_set())
            self.assertTrue(cold_launch_seen.is_set())
            self.assertIn(f"skip:{blocked_root}:segment", debug_events)
            self.assertNotIn(blocked_root, {root for root, _entity in spawned})
            self.assertIn(cold_root, {root for root, _entity in spawned})
            roots_event = next(
                index
                for index, event in enumerate(debug_events)
                if event.startswith("setroots:")
                and blocked_root in event
                and cold_root in event
            )
            skip_event = debug_events.index(f"skip:{blocked_root}:segment")
            cold_spawn_event = next(
                index
                for index, event in enumerate(debug_events)
                if event == f"spawn:{cold_root}:2000"
            )
            self.assertGreater(skip_event, roots_event)
            self.assertGreater(cold_spawn_event, skip_event)
            self.assertGreaterEqual(
                sum(event.startswith(f"complete:{hot_root}|") for event in debug_events),
                1,
                "the pre-existing hot worker must reach terminal completion",
            )
            self.assertEqual(
                len(spawned),
                len(set(spawned)),
                "the shared claim/submit boundary must not duplicate a worker",
            )
            check_store = TaskStore(str(state_path), config)
            cold_state = check_store.get_runtime_sync(cycle_key(cold_root))
            self.assertEqual(cold_state["claimed"], [2000])
            self.assertEqual(cold_state["running"], [2000])
            self.assertEqual(_segment_cycle_pending_order(cold_state), list(range(2001, 2040)))
            check_store.close()

    def test_dependency_refresh_merges_newer_policy_without_overwriting_it(self) -> None:
        """A slow root-local read cannot restore an older pause/capacity snapshot."""

        with tempfile.TemporaryDirectory() as td:
            root = "/var/www/context-merge/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            store = TaskStore(str(state_path), config)
            cycle = {"schema": "mcd-segment-cycle-v1", "cycle_id": "cycle-old", "status": "active"}
            store.put_runtime_sync(cycle_key(root), cycle)
            context = {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "mautic_major": 6,
                "cycle_id": "cycle-old",
                "cycle_status": "active",
                "parents_by_child": {},
                "children_by_parent": {},
                "throttled_active": False,
                "priority_pending": False,
                "segment_slot_limit": 11,
                "dispatch_pause": False,
            }
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), context)
            db_started = threading.Event()
            db_release = threading.Event()

            class SlowMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def fetch_published_segment_filters(self) -> list[dict[str, object]]:
                    db_started.set()
                    self_assert = db_release.wait(timeout=2.0)
                    if not self_assert:
                        raise AssertionError("fixture DB refresh did not release")
                    return [{"id": 1, "filters": "a:0:{}"}]

            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    daemon._rehydrate_segment_dispatch_context(
                        store,
                        root=root,
                        db_config=object(),
                        cycle_state=cycle,
                        context=context,
                        config=config,
                        mautic_major=6,
                    )
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(daemon, "MauticDB", SlowMauticDB):
                thread = threading.Thread(target=refresh, daemon=True)
                thread.start()
                self.assertTrue(db_started.wait(timeout=1.0))
                newer_context = dict(context)
                newer_context["segment_slot_limit"] = 7
                newer_context["dispatch_pause"] = True
                store.put_runtime_sync(daemon._segment_dispatch_context_key(root), newer_context)
                db_release.set()
                thread.join(timeout=2.0)

            self.assertEqual(errors, [])
            merged = store.get_runtime_sync(daemon._segment_dispatch_context_key(root))
            self.assertEqual(merged["segment_slot_limit"], 7)
            self.assertTrue(merged["dispatch_pause"])
            self.assertTrue(merged["dependency_snapshot_authoritative"])
            store.close()

    def test_planner_no_fresh_read_preserves_same_cycle_dependency_snapshot(self) -> None:
        """A no-read planner branch cannot publish an empty default over a valid graph."""

        cycle = {"schema": "mcd-segment-cycle-v1", "cycle_id": "cycle-graph", "status": "active"}
        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "cycle_id": "cycle-graph",
            "parents_by_child": {"154": [146, 149, 152, 153]},
            "children_by_parent": {"146": [154], "149": [154], "152": [154], "153": [154]},
            "dependency_snapshot_authoritative": True,
            "dependency_snapshot_updated_at": 123.0,
            "dependency_snapshot_segment_count": 150,
        }
        selected = daemon._select_segment_dependency_snapshot(
            current_context=context,
            current_cycle=cycle,
            candidate_parents={},
            candidate_children={},
            snapshot_read_ok=False,
            snapshot_updated_at=0.0,
            snapshot_segment_count=0,
        )
        parents, children, authoritative, updated_at, segment_count, mode = selected
        self.assertEqual(parents[154], {146, 149, 152, 153})
        self.assertEqual(children[153], {154})
        self.assertTrue(authoritative)
        self.assertEqual(updated_at, 123.0)
        self.assertEqual(segment_count, 150)
        self.assertEqual(mode, "preserved_previous")

    def test_static_lane_preserves_verified_graph_across_terminal_ordinary_cycle(self) -> None:
        """An independent static tick must not lose its parent graph at cycle rollover."""

        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "cycle_id": "cycle-static-old",
            "parents_by_child": {59: [57]},
            "children_by_parent": {57: [59]},
            "dependency_snapshot_authoritative": True,
            "dependency_snapshot_verified": True,
            "dependency_snapshot_updated_at": 123.0,
            "dependency_snapshot_segment_count": 2,
        }
        selected = daemon._select_segment_dependency_snapshot(
            current_context=context,
            current_cycle={
                "schema": "mcd-segment-cycle-v1",
                "cycle_id": "cycle-ordinary-new",
                "status": "terminal",
            },
            candidate_parents={},
            candidate_children={},
            snapshot_read_ok=False,
            snapshot_updated_at=0.0,
            snapshot_segment_count=0,
            preserve_verified_previous=True,
        )
        parents, children, authoritative, updated_at, segment_count, mode = selected
        self.assertEqual(parents, {59: {57}})
        self.assertEqual(children, {57: {59}})
        self.assertTrue(authoritative)
        self.assertEqual(updated_at, 123.0)
        self.assertEqual(segment_count, 2)
        self.assertEqual(mode, "preserved_previous")

    def test_successful_empty_dependency_snapshot_is_allowed_for_independent_root(self) -> None:
        """A genuinely successful empty definition parse remains authoritative."""

        cycle = {"schema": "mcd-segment-cycle-v1", "cycle_id": "cycle-empty", "status": "active"}
        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "cycle_id": "cycle-empty",
            "parents_by_child": {"154": [146]},
            "children_by_parent": {"146": [154]},
            "dependency_snapshot_authoritative": True,
            "dependency_snapshot_updated_at": 123.0,
            "dependency_snapshot_segment_count": 150,
        }
        parents, children, authoritative, updated_at, segment_count, mode = (
            daemon._select_segment_dependency_snapshot(
                current_context=context,
                current_cycle=cycle,
                candidate_parents={},
                candidate_children={},
                snapshot_read_ok=True,
                snapshot_updated_at=456.0,
                snapshot_segment_count=12,
            )
        )
        self.assertEqual(parents, {})
        self.assertEqual(children, {})
        self.assertTrue(authoritative)
        self.assertEqual(updated_at, 456.0)
        self.assertEqual(segment_count, 12)
        self.assertEqual(mode, "live_read")

    def test_legacy_empty_dependency_context_requires_live_refresh(self) -> None:
        """An old empty graph is refreshed before an active cycle can refill."""

        cycle = {"cycle_id": "cycle-legacy-empty", "status": "active"}
        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "cycle_id": "cycle-legacy-empty",
            "cycle_status": "active",
            "parents_by_child": {},
            "children_by_parent": {},
            "dependency_snapshot_authoritative": True,
            "dependency_snapshot_updated_at": 123.0,
            "dependency_snapshot_segment_count": 150,
            "throttled_active": False,
            "priority_pending": False,
            "segment_slot_limit": 11,
            "native_segment_exclude_supported": True,
        }
        self.assertTrue(
            daemon._segment_dispatch_context_needs_dependency_refresh(
                context,
                cycle,
                config=self._config(),
                mautic_major=7,
                now_ts=200.0,
            )
        )

        live_context = dict(context)
        live_context["dependency_snapshot_mode"] = "live_read"
        self.assertFalse(
            daemon._segment_dispatch_context_needs_dependency_refresh(
                live_context,
                cycle,
                config=self._config(),
                mautic_major=7,
                now_ts=200.0,
            )
        )

        preserved_context = dict(context)
        preserved_context["dependency_snapshot_mode"] = "preserved_previous"
        self.assertTrue(
            daemon._segment_dispatch_context_needs_dependency_refresh(
                preserved_context,
                cycle,
                config=self._config(),
                mautic_major=7,
                now_ts=200.0,
            )
        )

        verified_preserved_context = dict(preserved_context)
        verified_preserved_context["dependency_snapshot_verified"] = True
        self.assertFalse(
            daemon._segment_dispatch_context_needs_dependency_refresh(
                verified_preserved_context,
                cycle,
                config=self._config(),
                mautic_major=7,
                now_ts=200.0,
            )
        )

        selected = daemon._select_segment_dependency_snapshot(
            current_context=context,
            current_cycle=cycle,
            candidate_parents={},
            candidate_children={},
            snapshot_read_ok=False,
            snapshot_updated_at=0.0,
            snapshot_segment_count=0,
        )
        self.assertEqual(selected, ({}, {}, False, 0.0, 0, "read_required"))

    def test_dependency_refresh_final_merge_after_capability_probe_preserves_newer_policy(self) -> None:
        """M7 help latency cannot overwrite a newer same-cycle planner policy."""

        with tempfile.TemporaryDirectory() as td:
            root = "/var/www/capability-merge/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            store = TaskStore(str(state_path), config)
            cycle = {"schema": "mcd-segment-cycle-v1", "cycle_id": "cycle-old", "status": "active"}
            context = {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "mautic_major": 7,
                "cycle_id": "cycle-old",
                "cycle_status": "active",
                "parents_by_child": {},
                "children_by_parent": {},
                "throttled_active": False,
                "priority_pending": False,
                "segment_slot_limit": 11,
                "dispatch_pause": False,
            }
            store.put_runtime_sync(cycle_key(root), cycle)
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), context)
            probe_started = threading.Event()
            probe_release = threading.Event()

            class FastMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def fetch_published_segment_filters(self) -> list[dict[str, object]]:
                    return [{"id": 1, "filters": "a:0:{}"}]

            def blocked_probe(*_args: object, **_kwargs: object) -> bool:
                probe_started.set()
                if not probe_release.wait(timeout=2.0):
                    raise AssertionError("fixture capability probe did not release")
                return True

            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    daemon._rehydrate_segment_dispatch_context(
                        store,
                        root=root,
                        db_config=object(),
                        cycle_state=cycle,
                        context=context,
                        config=config,
                        mautic_major=7,
                    )
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(daemon, "MauticDB", FastMauticDB), patch.object(
                daemon,
                "_segment_native_exclude_capability",
                side_effect=blocked_probe,
            ):
                thread = threading.Thread(target=refresh, daemon=True)
                thread.start()
                self.assertTrue(probe_started.wait(timeout=1.0))
                newer_context = dict(context)
                newer_context["segment_slot_limit"] = 7
                newer_context["dispatch_pause"] = True
                store.put_runtime_sync(daemon._segment_dispatch_context_key(root), newer_context)
                probe_release.set()
                thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            merged = store.get_runtime_sync(daemon._segment_dispatch_context_key(root))
            self.assertEqual(merged["segment_slot_limit"], 7)
            self.assertTrue(merged["dispatch_pause"])
            self.assertTrue(merged["native_segment_exclude_supported"])
            store.close()

    def test_dependency_refresh_discards_changed_generation_after_capability_probe(self) -> None:
        """A cycle replacement during M7 help must cause retry, never a stale write."""

        with tempfile.TemporaryDirectory() as td:
            root = "/var/www/capability-generation/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            store = TaskStore(str(state_path), config)
            cycle = {"schema": "mcd-segment-cycle-v1", "cycle_id": "cycle-old", "status": "active"}
            context = {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "mautic_major": 7,
                "cycle_id": "cycle-old",
                "cycle_status": "active",
                "parents_by_child": {},
                "children_by_parent": {},
                "throttled_active": False,
                "priority_pending": False,
                "segment_slot_limit": 11,
                "dispatch_pause": False,
            }
            store.put_runtime_sync(cycle_key(root), cycle)
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), context)
            probe_started = threading.Event()
            probe_release = threading.Event()

            class FastMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def fetch_published_segment_filters(self) -> list[dict[str, object]]:
                    return [{"id": 1, "filters": "a:0:{}"}]

            def blocked_probe(*_args: object, **_kwargs: object) -> bool:
                probe_started.set()
                if not probe_release.wait(timeout=2.0):
                    raise AssertionError("fixture capability probe did not release")
                return True

            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    daemon._rehydrate_segment_dispatch_context(
                        store,
                        root=root,
                        db_config=object(),
                        cycle_state=cycle,
                        context=context,
                        config=config,
                        mautic_major=7,
                    )
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(daemon, "MauticDB", FastMauticDB), patch.object(
                daemon,
                "_segment_native_exclude_capability",
                side_effect=blocked_probe,
            ):
                thread = threading.Thread(target=refresh, daemon=True)
                thread.start()
                self.assertTrue(probe_started.wait(timeout=1.0))
                replacement_cycle = dict(cycle)
                replacement_cycle["cycle_id"] = "cycle-new"
                store.put_runtime_sync(cycle_key(root), replacement_cycle)
                replacement_context = dict(context)
                replacement_context["cycle_id"] = "cycle-new"
                replacement_context["segment_slot_limit"] = 5
                store.put_runtime_sync(daemon._segment_dispatch_context_key(root), replacement_context)
                probe_release.set()
                thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], daemon._SegmentDispatchContextGenerationChanged)
            self.assertEqual(
                store.get_runtime_sync(cycle_key(root))["cycle_id"],
                "cycle-new",
            )
            preserved = store.get_runtime_sync(daemon._segment_dispatch_context_key(root))
            self.assertEqual(preserved["cycle_id"], "cycle-new")
            self.assertEqual(preserved["segment_slot_limit"], 5)
            self.assertNotIn("dependency_snapshot_authoritative", preserved)
            store.close()

    def test_restart_rehydrates_frozen_full_cycle_without_resetting_claims_or_closure(self) -> None:
        """A stale context must recover the live graph before one M7 worker starts."""

        with tempfile.TemporaryDirectory() as td:
            root = "/var/www/frozen-b2bss/public_html"
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\ndispatch_interval_sec = 0.05\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 11\n"
                "scheduler_instance_max_parallel = 11\n"
                "segment_full_scan_interval_sec = 0\n"
                "segment_mode = \"farm\"\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            store = TaskStore(str(state_path), config)

            # 150 published IDs, with the frozen tail 153/154 present.  The
            # child 154 depends on all four parents, so an empty persisted
            # context would incorrectly make it an independent worker.
            published_ids = [*range(1, 147), 149, 152, 153, 154]
            cycle, _added, _started = sync_cycle(
                None,
                planned_ids=published_ids,
                never_built=[],
                running_ids=[],
                now_ts=100.0,
                important_ids=[],
                remaining_ids=published_ids,
                lazy_mode=False,
            )
            for entity_id in published_ids[:140]:
                cycle = mark_terminal(
                    cycle,
                    entity_id=entity_id,
                    success=True,
                    note=None,
                    now_ts=101.0,
                )
            store.put_runtime_sync(cycle_key(root), cycle)
            stale_context = {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "mautic_major": 7,
                "updated_at": 102.0,
                "cycle_id": cycle["cycle_id"],
                "cycle_status": "active",
                "blocked_ids": [],
                "sql_managed_ids": [],
                "static_configured_ids": [],
                "static_active_ids": [],
                "active_import_target_ids": [],
                "parents_by_child": {},
                "children_by_parent": {},
                "dependency_snapshot_authoritative": True,
                "dependency_snapshot_updated_at": 102.0,
                "dependency_snapshot_segment_count": 150,
                "scheduler_pause": False,
                "backup_pause": False,
                "dispatch_pause": False,
                "db_pause_until": 0.0,
                "cluster_cron_allowed": True,
                "cluster_import_allowed": True,
            }
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), stale_context)
            before_cycle = deepcopy(cycle)

            rows = [
                {
                    "id": entity_id,
                    "filters": "".join(
                        _leadlist_filter(parent_id)
                        for parent_id in ([146, 149, 152, 153] if entity_id == 154 else [])
                    ),
                }
                for entity_id in published_ids
            ]

            class FakeMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def fetch_published_segment_filters(self) -> list[dict[str, object]]:
                    return rows

            with patch.object(daemon, "MauticDB", FakeMauticDB), patch.object(
                daemon,
                "_segment_native_exclude_capability",
                return_value=True,
            ):
                refreshed = daemon._rehydrate_segment_dispatch_context(
                    store,
                    root=root,
                    db_config=object(),
                    cycle_state=cycle,
                    context=stale_context,
                    config=config,
                    mautic_major=7,
                    recovery_policy={
                        "throttled_active": False,
                        "priority_pending": False,
                        "segment_slot_limit": 11,
                        "cluster_cron_allowed": True,
                        "cluster_import_allowed": True,
                    },
                )

            after_cycle = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(
                {key: after_cycle.get(key) for key in ("cycle_id", "items", "done", "claimed", "running")},
                {key: before_cycle.get(key) for key in ("cycle_id", "items", "done", "claimed", "running")},
            )
            self.assertEqual(refreshed["parents_by_child"]["154"], [146, 149, 152, 153])
            self.assertEqual(refreshed["children_by_parent"]["153"], [154])
            self.assertTrue(refreshed["dependency_snapshot_authoritative"])
            self.assertTrue(refreshed["dependency_snapshot_verified"])
            self.assertTrue(refreshed["native_segment_exclude_supported"])
            self.assertEqual(refreshed["segment_slot_limit"], 11)
            self.assertFalse(refreshed["throttled_active"])
            self.assertFalse(refreshed["priority_pending"])
            self.assertFalse(
                daemon._segment_dispatch_context_needs_dependency_refresh(
                    refreshed,
                    after_cycle,
                    config=config,
                    mautic_major=7,
                    now_ts=200.0,
                )
            )

            pending = _segment_cycle_pending_order(after_cycle)
            self.assertIn(153, pending)
            self.assertIn(154, pending)
            self.assertEqual(
                daemon._segment_chain_from_dispatch_context(store, root, pending, 154),
                [154, 146, 149, 152, 153],
            )
            m6_context = dict(refreshed)
            m6_context["mautic_major"] = 6
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), m6_context)
            self.assertEqual(
                daemon._segment_chain_from_dispatch_context(store, root, pending, 154),
                [146, 149, 152, 153, 154],
            )
            store.put_runtime_sync(daemon._segment_dispatch_context_key(root), refreshed)

            class FakeProcess:
                _next_pid = 72000

                def __init__(self) -> None:
                    type(self)._next_pid += 1
                    self.pid = type(self)._next_pid
                    self.finished = False

                def poll(self) -> int | None:
                    return 0 if self.finished else None

                def wait(self) -> int:
                    return 0

            running: dict[str, daemon.RunningTask] = {}
            popens: dict[str, FakeProcess] = {}
            claimed_ids: list[int] = []

            def claim(entity_id: int) -> bool:
                claimed = _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=int(entity_id),
                    eligible_ids={146, 149, 152, 153, 154},
                    blocked_ids=set(),
                    running_ids=set(),
                    now_ts=time.time(),
                )
                if claimed:
                    claimed_ids.append(int(entity_id))
                return claimed

            with patch.object(
                daemon,
                "_spawn_command",
                side_effect=lambda **_kwargs: FakeProcess(),
            ):
                launched = _fill_from_ring(
                    ring=deque([154, 146, 149, 152, 153]),
                    ring_limit=11,
                    total_limit=11,
                    root=root,
                    task_type="segment",
                    running=running,
                    ring_entities={146, 149, 152, 153, 154},
                    config=config,
                    store=store,
                    popens=popens,
                    build_args=lambda sid: ["php", "segments:update", str(sid)],
                    admission_claim=claim,
                    admission_running=lambda sid: _segment_cycle_running(
                        store,
                        root=root,
                        entity_id=int(sid),
                        now_ts=time.time(),
                    ),
                    admission_release=lambda sid: _segment_cycle_release(
                        store,
                        root=root,
                        entity_id=int(sid),
                        now_ts=time.time(),
                    ),
                    remove_on_launch=True,
                    bypass_repeat_guard_entities={146, 149, 152, 153, 154},
                    bypass_baseline_admission=True,
                    max_launches=1,
                )

            self.assertEqual(launched, 1)
            self.assertEqual(claimed_ids, [154, 146, 149, 152, 153])
            self.assertEqual(len(running), 1)
            proc = next(iter(popens.values()))
            proc.finished = True
            daemon._monitor_running(
                config=config,
                store=store,
                running=running,
                popens=popens,
                segment_terminal_callback=lambda task, success, note: daemon._segment_terminal_and_wake(
                    store,
                    threading.Event(),
                    task,
                    success,
                    note,
                ),
            )
            terminal_cycle = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(set(terminal_cycle.get("done") or []) & {146, 149, 152, 153, 154}, {146, 149, 152, 153, 154})
            self.assertEqual(terminal_cycle.get("claimed"), [])
            self.assertEqual(terminal_cycle.get("running"), [])
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

    def test_ordinary_orphan_grace_uses_existing_profile_window(self) -> None:
        self.assertEqual(
            daemon._segment_cycle_orphan_grace_sec(
                SimpleNamespace(segment_sql_orphan_after_sec=777)
            ),
            777,
        )
        self.assertEqual(
            daemon._segment_cycle_orphan_grace_sec(SimpleNamespace()),
            daemon._SEGMENT_ORPHAN_GRACE_SEC,
        )

    def test_marker_pid_reuse_is_not_independent_liveness_without_start_identity(self) -> None:
        marker = {
            "pid": 123,
            "pid_start_time": 456,
            "status": "running",
            "command_str": "",
        }
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=999
        ):
            self.assertFalse(daemon._segment_cycle_marker_process_live(marker))
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=456
        ):
            self.assertTrue(daemon._segment_cycle_marker_process_live(marker))

    def test_live_chain_owner_keeps_all_covered_members_claimed(self) -> None:
        root = "/var/www/alex/chain"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[61, 62],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state["claimed"] = [61, 62]
        state["running"] = [61, 62]
        state["status"] = "active"
        store.put_runtime_sync(cycle_key(root), state)
        chain_marker = {
            "schema": "mcd-segment-execution-chain-v1",
            "root": root,
            "owner_id": 61,
            "entity_ids": [61, 62],
            "status": "running",
            "cycle_id": state["cycle_id"],
            "started_at": 100.1,
            "pid": 123,
            "pid_start_time": 456,
            "command_str": "php bin/console mautic:segments:update -i 61",
        }
        for sid in (61, 62):
            store.put_runtime_sync(daemon._segment_chain_runtime_key(root, sid), chain_marker)
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=456
        ), patch.object(daemon, "_pid_matches_task_command", return_value=True), patch.object(
            daemon, "_refresh_scheduler_important_root"
        ):
            repaired, released = daemon._segment_cycle_reconcile_orphans(
                store,
                root=root,
                running={},
                now_ts=101.0,
                orphan_grace_sec=30,
            )
        self.assertEqual(released, [])
        self.assertEqual(repaired["claimed"], [61, 62])
        self.assertEqual(repaired["running"], [61, 62])
        self.assertEqual(daemon._segment_cycle_pending_order(repaired), [])
        store.close()

    def test_foreign_chain_reverse_index_is_not_loaded_or_reused(self) -> None:
        root = "/var/www/alex/current"
        foreign_root = "/var/www/grubinshowroom/public_html"
        store = TaskStore(":memory:")
        store.put_runtime_sync(
            daemon._segment_chain_runtime_key(root, 61),
            {
                "schema": "mcd-segment-execution-chain-v1",
                "root": foreign_root,
                "owner_id": 61,
                "entity_ids": [61, 62],
                "status": "running",
                "task_key": f"{foreign_root}|segment_priority|61",
                "pid": 123,
                "pid_start_time": 456,
            },
        )
        self.assertEqual(daemon._segment_chain_runtime_load(store, root, 61), {})
        self.assertEqual(daemon._segment_chain_entity_ids(store, root, 61), [61])
        store.close()

    def test_chain_reverse_index_is_scoped_to_current_cycle_generation_and_members(self) -> None:
        root = "/var/www/alex/generation"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[61, 62],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        store.put_runtime_sync(cycle_key(root), state)
        store.put_runtime_sync(
            daemon._segment_chain_runtime_key(root, 61),
            {
                "schema": "mcd-segment-execution-chain-v1",
                "root": root,
                "owner_id": 61,
                "entity_ids": [61, 62, 999],
                "status": "running",
                "cycle_id": state["cycle_id"],
                "task_key": f"{root}|segment|61",
                "pid": 123,
                "pid_start_time": 456,
            },
        )
        self.assertEqual(daemon._segment_chain_entity_ids(store, root, 61), [61, 62])
        stale = dict(store.get_runtime_sync(daemon._segment_chain_runtime_key(root, 61)))
        stale["cycle_id"] = "older-cycle"
        store.put_runtime_sync(daemon._segment_chain_runtime_key(root, 61), stale)
        self.assertEqual(daemon._segment_chain_entity_ids(store, root, 61), [61])
        store.close()

    def test_recycled_marker_pid_enters_grace_then_releases_without_duplicate(self) -> None:
        root = "/var/www/alex/recycled"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[61],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
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
                "status": "running",
                "cycle_id": state["cycle_id"],
                "started_at": 100.1,
                "pid": 123,
                "pid_start_time": 456,
                "command_str": "php bin/console mautic:segments:update -i 61",
            },
        )
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=999
        ), patch.object(daemon, "_pid_matches_task_command", return_value=True):
            held, released = daemon._segment_cycle_reconcile_orphans(
                store,
                root=root,
                running={},
                now_ts=101.0,
                orphan_grace_sec=60,
            )
        self.assertEqual(released, [])
        self.assertEqual(held["claimed"], [61])
        self.assertIn("orphan_since", held["active_meta"]["61"])
        self.assertNotIn("last_independent_at", held["active_meta"]["61"])
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=999
        ), patch.object(daemon, "_pid_matches_task_command", return_value=True), patch.object(
            daemon, "_refresh_scheduler_important_root"
        ):
            released_state, released = daemon._segment_cycle_reconcile_orphans(
                store,
                root=root,
                running={},
                now_ts=161.0,
                orphan_grace_sec=60,
            )
        self.assertEqual(released, [61])
        self.assertEqual(released_state["claimed"], [])
        self.assertEqual(daemon._segment_cycle_pending_order(released_state), [61])
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
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None), patch.object(
            daemon, "_is_pid_alive", return_value=True
        ):
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
            ), patch.object(daemon, "_is_pid_alive", return_value=True):
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
            first_task = running.pop(_task_key(roots[0], "segment", 1))
            store.finish(first_task.row_id, state="done", rc=0, note="fixture_terminal")
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ), patch.object(daemon, "_is_pid_alive", return_value=True):
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
            second_task = running.pop(_task_key(roots[1], "segment", 2))
            store.finish(second_task.row_id, state="done", rc=0, note="fixture_terminal")
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ), patch.object(daemon, "_is_pid_alive", return_value=True):
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
