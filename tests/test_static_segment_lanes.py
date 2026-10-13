from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.daemon import (
    _manual_static_lane_for_ids,
    _segment_static_closure_conflict,
    _segment_static_closure_lock_key,
    _segment_native_chain_command_plan,
    _segment_priority_effective_setting,
    _segment_super_priority_effective_setting,
    _static_lane_runtime_evidence,
)
from mcd_agent.static_segment_lanes import (
    ORPHAN_GRACE_SEC,
    PRIORITY,
    SUPER_PRIORITY,
    build_lane_plans,
    claim_item,
    mark_running,
    mark_terminal,
    pending_items,
    reconcile_state,
)


class StaticSegmentLaneTests(unittest.TestCase):
    def test_delayed_static_callbacks_keep_a_context_after_b_is_created(self) -> None:
        """A delayed A callback must never write B's root or command markers."""
        store = daemon.TaskStore(":memory:")
        wakeup = threading.Event()
        root_a = "/var/www/a/public_html"
        root_b = "/var/www/b/public_html"
        start_a, complete_a = daemon._make_static_segment_callbacks(
            store=store,
            root=root_a,
            task_type="segment_priority",
            lane_name=PRIORITY,
            segment_id=10,
            chain_ids=(10, 11),
            started_at=10.0,
            command_str="A-command",
            timeout_sec=60,
            segment_dispatch_wakeup=wakeup,
            max_attempts=3,
        )
        start_b, _complete_b = daemon._make_static_segment_callbacks(
            store=store,
            root=root_b,
            task_type="segment_priority",
            lane_name=PRIORITY,
            segment_id=20,
            chain_ids=(20, 21),
            started_at=20.0,
            command_str="B-command",
            timeout_sec=60,
            segment_dispatch_wakeup=wakeup,
            max_attempts=3,
        )

        with patch.object(daemon, "_pid_start_time", return_value=None):
            start_b(202)
            start_a(101)
            complete_a(0)

        for root, ids, command in (
            (root_a, (10, 11), "A-command"),
            (root_b, (20, 21), "B-command"),
        ):
            for entity_id in ids:
                marker = store.get_runtime_sync(daemon._segment_runtime_marker_key(root, entity_id))
                self.assertEqual(marker.get("root"), root)
                self.assertEqual(marker.get("command_str"), command)
                chain = store.get_runtime_sync(daemon._segment_chain_runtime_key(root, entity_id))
                self.assertEqual(chain.get("root"), root)
                self.assertEqual(
                    chain.get("task_key"),
                    daemon._task_key(root, "segment_priority", 10 if root == root_a else 20),
                )
                self.assertEqual(chain.get("command_str"), command)

        self.assertIsNone(store.get_runtime_sync(daemon._segment_runtime_marker_key(root_b, 10)))
        self.assertIsNone(store.get_runtime_sync(daemon._segment_chain_runtime_key(root_b, 10)))
        self.assertTrue(wakeup.is_set())
        store.close()

    def test_whitelist_is_only_priority_migration_alias(self) -> None:
        config = SimpleNamespace(
            disable_whitelist=False,
            segment_priority=[58],
            segment_whitelist=[59],
            segment_whitelist_file=None,
            segment_priority_instance_settings={},
            segment_whitelist_instance_settings={},
            segment_super_priority=[60],
            segment_super_priority_instance_settings={},
        )
        inst = SimpleNamespace(root="/var/www/site")
        self.assertEqual(_segment_priority_effective_setting(config, inst), {58, 59})
        self.assertEqual(_segment_super_priority_effective_setting(config, inst), {60})

    def test_legacy_priority_and_super_priority_are_mutually_exclusive(self) -> None:
        plans = build_lane_plans(
            super_priority_ids=[58, 58],
            priority_ids=[58, 59],
            parents_by_child={},
            children_by_parent={},
            mautic_major=6,
        )
        self.assertEqual(plans[SUPER_PRIORITY], [58])
        self.assertEqual(plans[PRIORITY], [59])

    def test_legacy_common_parent_is_planned_once_for_both_static_lanes(self) -> None:
        plans = build_lane_plans(
            super_priority_ids=[58],
            priority_ids=[59],
            parents_by_child={58: {57}, 59: {57}},
            children_by_parent={57: {58, 59}},
            mautic_major=6,
        )
        self.assertEqual(plans[SUPER_PRIORITY], [57, 58])
        self.assertEqual(plans[PRIORITY], [59])
        self.assertEqual(sum(item == 57 for lane in plans.values() for item in lane), 1)

    def test_mautic7_sibling_terminals_own_internal_parent_without_scheduling_it_alone(self) -> None:
        plans = build_lane_plans(
            super_priority_ids=[58, 59],
            priority_ids=[],
            parents_by_child={58: {57}, 59: {57}},
            children_by_parent={57: {58, 59}},
            mautic_major=7,
        )
        self.assertEqual(plans[SUPER_PRIORITY], [57, 58, 59])
        self.assertNotIn(57, plans[PRIORITY])

    def test_mautic7_shared_parent_is_owned_by_super_over_priority(self) -> None:
        plans = build_lane_plans(
            super_priority_ids=[58],
            priority_ids=[59],
            parents_by_child={58: {57}, 59: {57}},
            children_by_parent={57: {58, 59}},
            mautic_major=7,
        )
        self.assertEqual(plans[SUPER_PRIORITY], [57, 58])
        self.assertEqual(plans[PRIORITY], [59])
        self.assertEqual(sum(item == 57 for lane in plans.values() for item in lane), 1)

    def test_mautic7_full_ownership_keeps_native_commands_on_leaf_roots(self) -> None:
        command_ids, excludes = _segment_native_chain_command_plan(
            [57, 58, 59],
            {58: {57}, 59: {57}},
            {57: {58, 59}},
        )
        self.assertEqual(command_ids, [58, 59])
        self.assertEqual(excludes, {58: [], 59: [57]})

    def test_static_success_reconciles_preexisting_frozen_ordinary_parent(self) -> None:
        store = daemon.TaskStore(":memory:")
        root = "/var/www/m7/public_html"
        cycle, _added, _started = daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=100.0,
        )
        self.assertIn(57, daemon._segment_cycle_pending_order(cycle))
        _on_start, on_complete = daemon._make_static_segment_callbacks(
            store=store,
            root=root,
            task_type="segment_super_priority",
            lane_name=SUPER_PRIORITY,
            segment_id=58,
            chain_ids=(58, 59, 57),
            started_at=100.0,
            command_str="native-super-closure",
            timeout_sec=60,
            segment_dispatch_wakeup=threading.Event(),
            max_attempts=3,
        )
        on_complete(0)
        reconciled = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, reconciled["done"])
        self.assertNotIn(58, reconciled["done"])
        self.assertNotIn(59, reconciled["done"])
        self.assertNotIn(57, daemon._segment_cycle_pending_order(reconciled, excluded_ids=[57]))
        store.close()

    def test_static_callback_does_not_reconcile_a_new_ordinary_cycle(self) -> None:
        store = daemon.TaskStore(":memory:")
        root = "/var/www/m7/public_html"
        first_cycle, _added, _started = daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57, 100],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=100.0,
        )
        _on_start, on_complete = daemon._make_static_segment_callbacks(
            store=store,
            root=root,
            task_type="segment_super_priority",
            lane_name=SUPER_PRIORITY,
            segment_id=58,
            chain_ids=(58, 59, 57),
            started_at=100.0,
            command_str="native-super-closure",
            timeout_sec=60,
            segment_dispatch_wakeup=threading.Event(),
            max_attempts=3,
        )
        old_cycle = dict(first_cycle)
        old_cycle.update({"status": "terminal", "done": [57, 100], "claimed": [], "running": []})
        store.put_runtime_sync(daemon._segment_cycle_key(root), old_cycle)
        new_cycle, _added, started_new = daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57, 100],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=101.0,
        )
        self.assertTrue(started_new)
        self.assertNotEqual(new_cycle["cycle_id"], first_cycle["cycle_id"])
        on_complete(0)
        current = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertEqual(current["cycle_id"], new_cycle["cycle_id"])
        self.assertNotIn(57, current.get("done", []))
        self.assertNotIn(58, current.get("done", []))
        self.assertNotIn(59, current.get("done", []))
        store.close()

    def test_static_lock_busy_releases_frozen_parent_without_terminalizing_it(self) -> None:
        store = daemon.TaskStore(":memory:")
        root = "/var/www/m7/public_html"
        cycle, _added, _started = daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=100.0,
        )
        _on_start, on_complete = daemon._make_static_segment_callbacks(
            store=store,
            root=root,
            task_type="segment_super_priority",
            lane_name=SUPER_PRIORITY,
            segment_id=58,
            chain_ids=(58, 59, 57),
            started_at=100.0,
            command_str="native-super-closure",
            timeout_sec=60,
            segment_dispatch_wakeup=threading.Event(),
            max_attempts=3,
        )
        on_complete(daemon._TASK_LOCK_BUSY_RC)
        released = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, daemon._segment_cycle_pending_order(released))
        self.assertNotIn(57, released.get("done", []))
        self.assertNotIn(57, released.get("errors", []))
        chain = store.get_runtime_sync(daemon._segment_chain_runtime_key(root, 58))
        self.assertEqual(chain.get("status"), "released")
        store.close()

    def test_static_failure_retries_then_success_closes_frozen_parent(self) -> None:
        store = daemon.TaskStore(":memory:")
        root = "/var/www/m7/public_html"
        _cycle, _added, _started = daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=100.0,
        )
        static_state, _added, _started = reconcile_state(
            None,
            plans={SUPER_PRIORITY: [57, 58, 59], PRIORITY: []},
            now_ts=100.0,
        )
        store.put_runtime_sync(daemon._static_lane_state_key(root), static_state)
        _on_start, on_complete = daemon._make_static_segment_callbacks(
            store=store,
            root=root,
            task_type="segment_super_priority",
            lane_name=SUPER_PRIORITY,
            segment_id=58,
            chain_ids=(58, 59, 57),
            started_at=100.0,
            command_str="native-super-closure",
            timeout_sec=60,
            segment_dispatch_wakeup=threading.Event(),
            max_attempts=3,
        )
        on_complete(1)
        retry_one = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, daemon._segment_cycle_pending_order(retry_one))
        self.assertNotIn(57, retry_one.get("errors", []))
        self.assertEqual(
            store.get_runtime_sync(daemon._static_lane_state_key(root))["lanes"][SUPER_PRIORITY]["attempts"]["57"],
            1,
        )
        on_complete(1)
        retry_two = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, daemon._segment_cycle_pending_order(retry_two))
        on_complete(0)
        succeeded = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, succeeded.get("done", []))
        self.assertNotIn(57, succeeded.get("errors", []))
        store.close()

    def test_static_failure_exhaustion_is_red_and_next_static_cycle_is_fresh(self) -> None:
        store = daemon.TaskStore(":memory:")
        root = "/var/www/m7/public_html"
        daemon._segment_cycle_sync(
            store,
            root=root,
            planned_ids=[57],
            never_built_ids=[],
            running_ids=[],
            excluded_ids=[],
            now_ts=100.0,
        )
        static_state, _added, _started = reconcile_state(
            None,
            plans={SUPER_PRIORITY: [57, 58, 59], PRIORITY: []},
            now_ts=100.0,
        )
        store.put_runtime_sync(daemon._static_lane_state_key(root), static_state)
        _on_start, on_complete = daemon._make_static_segment_callbacks(
            store=store,
            root=root,
            task_type="segment_super_priority",
            lane_name=SUPER_PRIORITY,
            segment_id=58,
            chain_ids=(58, 59, 57),
            started_at=100.0,
            command_str="native-super-closure",
            timeout_sec=60,
            segment_dispatch_wakeup=threading.Event(),
            max_attempts=3,
        )
        on_complete(1)
        on_complete(1)
        on_complete(1)
        failed_cycle = store.get_runtime_sync(daemon._segment_cycle_key(root))
        self.assertIn(57, failed_cycle.get("errors", []))
        self.assertNotIn(57, failed_cycle.get("done", []))
        failed_static = store.get_runtime_sync(daemon._static_lane_state_key(root))
        self.assertIn(57, failed_static["lanes"][SUPER_PRIORITY]["errors"])
        next_static, _added, started_new = reconcile_state(
            failed_static,
            plans={SUPER_PRIORITY: [57, 58, 59], PRIORITY: []},
            now_ts=102.0,
        )
        self.assertTrue(started_new)
        self.assertEqual(pending_items(next_static, SUPER_PRIORITY), [57, 58, 59])
        store.close()

    def test_mautic7_sibling_terminals_share_native_closure_execution_lock(self) -> None:
        parents = {58: {57}, 59: {57}}
        children = {57: {58, 59}}
        self.assertTrue(_segment_static_closure_conflict(59, {58}, parents, children))
        self.assertEqual(
            _segment_static_closure_lock_key("/var/www/m7", 58, parents, children),
            _segment_static_closure_lock_key("/var/www/m7", 59, parents, children),
        )
        self.assertFalse(_segment_static_closure_conflict(60, {58}, parents, children))

    def test_persisted_lanes_keep_priority_behind_super_and_allow_independent_claim(self) -> None:
        plans = {
            SUPER_PRIORITY: [57, 58],
            PRIORITY: [100],
        }
        state, added, started = reconcile_state(None, plans=plans, now_ts=100.0)
        self.assertTrue(started)
        self.assertEqual(added[SUPER_PRIORITY], [57, 58])
        self.assertEqual(pending_items(state, SUPER_PRIORITY), [57, 58])

        state, super_claimed = claim_item(
            state,
            lane=SUPER_PRIORITY,
            entity_id=57,
            blocked_ids=set(),
            globally_running_ids=set(),
            now_ts=100.1,
        )
        self.assertTrue(super_claimed)
        state, priority_claimed = claim_item(
            state,
            lane=PRIORITY,
            entity_id=100,
            blocked_ids=set(),
            globally_running_ids={57},
            now_ts=100.2,
        )
        self.assertTrue(priority_claimed)

    def test_manual_dependency_overlap_is_blocked_across_static_lanes(self) -> None:
        lane, reason = _manual_static_lane_for_ids(
            {
                "lanes": {
                    SUPER_PRIORITY: {"items": [58]},
                    PRIORITY: {"items": [59]},
                }
            },
            [58, 59],
            super_priority_ids={58},
            priority_ids={59},
        )
        self.assertIsNone(lane)
        self.assertEqual(reason, "manual_dependency_overlap_static_lanes")

    def test_manual_super_owner_absorbs_configured_lower_parent(self) -> None:
        lane, reason = _manual_static_lane_for_ids(
            {"lanes": {}},
            [57, 58],
            super_priority_ids={58},
            priority_ids={57},
        )
        self.assertEqual(lane, SUPER_PRIORITY)
        self.assertIsNone(reason)

    @staticmethod
    def _claimed_running_state(lane: str, entity_id: int = 59) -> dict[str, object]:
        plans = {SUPER_PRIORITY: [], PRIORITY: []}
        plans[lane] = [entity_id]
        state, _added, _started = reconcile_state(None, plans=plans, now_ts=100.0)
        state, claimed = claim_item(
            state,
            lane=lane,
            entity_id=entity_id,
            blocked_ids=set(),
            globally_running_ids=set(),
            now_ts=101.0,
        )
        assert claimed
        return mark_running(state, lane=lane, entity_id=entity_id, now_ts=102.0)

    def test_restart_with_live_executor_evidence_adopts_both_static_lanes(self) -> None:
        for lane in (SUPER_PRIORITY, PRIORITY):
            with self.subTest(lane=lane):
                state = self._claimed_running_state(lane)
                reconciled, _added, started_new = reconcile_state(
                    state,
                    plans={SUPER_PRIORITY: [59] if lane == SUPER_PRIORITY else [], PRIORITY: [59] if lane == PRIORITY else []},
                    now_ts=110.0,
                    active_evidence={(lane, 59): {"executor_active": True, "independent_at": 109.5}},
                    orphan_grace_sec=10,
                )
                self.assertFalse(started_new)
                self.assertEqual(reconciled["lanes"][lane]["running"], [59])
                self.assertEqual(pending_items(reconciled, lane), [])
                self.assertEqual(
                    reconciled["active_meta"][lane]["59"]["last_independent_at"],
                    109.5,
                )
                self.assertNotIn("orphan_since", reconciled["active_meta"][lane]["59"])

    def test_phantom_claim_has_bounded_grace_then_returns_to_same_pending_ring(self) -> None:
        for lane in (SUPER_PRIORITY, PRIORITY):
            with self.subTest(lane=lane):
                state = self._claimed_running_state(lane)
                within_grace, _added, _started = reconcile_state(
                    state,
                    plans={SUPER_PRIORITY: [59] if lane == SUPER_PRIORITY else [], PRIORITY: [59] if lane == PRIORITY else []},
                    now_ts=102.0 + ORPHAN_GRACE_SEC - 1,
                    active_evidence={},
                    orphan_grace_sec=ORPHAN_GRACE_SEC,
                )
                self.assertEqual(pending_items(within_grace, lane), [])
                within_grace, claimed = claim_item(
                    within_grace,
                    lane=lane,
                    entity_id=59,
                    blocked_ids=set(),
                    globally_running_ids=set(),
                    now_ts=102.5 + ORPHAN_GRACE_SEC - 1,
                )
                self.assertFalse(claimed)

                released, _added, _started = reconcile_state(
                    within_grace,
                    plans={SUPER_PRIORITY: [59] if lane == SUPER_PRIORITY else [], PRIORITY: [59] if lane == PRIORITY else []},
                    now_ts=102.0 + ORPHAN_GRACE_SEC,
                    active_evidence={},
                    orphan_grace_sec=ORPHAN_GRACE_SEC,
                )
                self.assertEqual(pending_items(released, lane), [59])
                released, claimed = claim_item(
                    released,
                    lane=lane,
                    entity_id=59,
                    blocked_ids=set(),
                    globally_running_ids=set(),
                    now_ts=103.0 + ORPHAN_GRACE_SEC,
                )
                self.assertTrue(claimed)
                restarted = mark_running(released, lane=lane, entity_id=59, now_ts=104.0 + ORPHAN_GRACE_SEC)
                self.assertEqual(restarted["lanes"][lane]["running"], [59])

    def test_marker_only_observation_does_not_extend_orphan_grace(self) -> None:
        state = self._claimed_running_state(SUPER_PRIORITY)
        marker_only = {(SUPER_PRIORITY, 59): {"started_at": 102.0, "last_independent_at": 102.0}}
        within_grace, _added, _started = reconcile_state(
            state,
            plans={SUPER_PRIORITY: [59], PRIORITY: []},
            now_ts=102.0 + ORPHAN_GRACE_SEC - 1,
            active_evidence=marker_only,
            orphan_grace_sec=ORPHAN_GRACE_SEC,
        )
        self.assertEqual(pending_items(within_grace, SUPER_PRIORITY), [])
        released, _added, _started = reconcile_state(
            within_grace,
            plans={SUPER_PRIORITY: [59], PRIORITY: []},
            now_ts=102.0 + ORPHAN_GRACE_SEC,
            active_evidence=marker_only,
            orphan_grace_sec=ORPHAN_GRACE_SEC,
        )
        self.assertEqual(pending_items(released, SUPER_PRIORITY), [59])

    def test_terminal_done_and_error_items_survive_orphan_reconciliation(self) -> None:
        plans = {SUPER_PRIORITY: [57, 58, 59], PRIORITY: []}
        state, _added, _started = reconcile_state(None, plans=plans, now_ts=100.0)
        state = mark_terminal(state, lane=SUPER_PRIORITY, entity_id=57, success=True, now_ts=101.0)
        state = mark_terminal(state, lane=SUPER_PRIORITY, entity_id=58, success=False, note="fixture", now_ts=102.0)
        state, claimed = claim_item(
            state,
            lane=SUPER_PRIORITY,
            entity_id=59,
            blocked_ids=set(),
            globally_running_ids=set(),
            now_ts=103.0,
        )
        self.assertTrue(claimed)
        state = mark_running(state, lane=SUPER_PRIORITY, entity_id=59, now_ts=104.0)
        reconciled, _added, _started = reconcile_state(
            state,
            plans=plans,
            now_ts=104.0 + ORPHAN_GRACE_SEC,
            active_evidence={},
            orphan_grace_sec=ORPHAN_GRACE_SEC,
        )
        lane = reconciled["lanes"][SUPER_PRIORITY]
        self.assertEqual(lane["items"], [57, 58, 59])
        self.assertEqual(lane["done"], [57])
        self.assertEqual(lane["errors"], [58])
        self.assertEqual(pending_items(reconciled, SUPER_PRIORITY), [59])

    def test_m6_m7_closure_plans_remain_deduplicated_after_orphan_release(self) -> None:
        for major, expected_super, expected_priority in (
            (6, [57, 58], [59]),
            (7, [57, 58, 59], []),
        ):
            with self.subTest(mautic_major=major):
                plans = build_lane_plans(
                    super_priority_ids=[58, 59] if major >= 7 else [58],
                    priority_ids=[] if major >= 7 else [59],
                    parents_by_child={58: {57}, 59: {57}},
                    children_by_parent={57: {58, 59}},
                    mautic_major=major,
                )
                state, _added, _started = reconcile_state(None, plans=plans, now_ts=100.0)
                state, claimed = claim_item(
                    state,
                    lane=SUPER_PRIORITY,
                    entity_id=expected_super[0],
                    blocked_ids=set(),
                    globally_running_ids=set(),
                    now_ts=101.0,
                )
                self.assertTrue(claimed)
                state = mark_running(state, lane=SUPER_PRIORITY, entity_id=expected_super[0], now_ts=102.0)
                released, _added, _started = reconcile_state(
                    state,
                    plans=plans,
                    now_ts=102.0 + ORPHAN_GRACE_SEC,
                    active_evidence={},
                    orphan_grace_sec=ORPHAN_GRACE_SEC,
                )
                self.assertEqual(released["lanes"][SUPER_PRIORITY]["items"], expected_super)
                self.assertEqual(released["lanes"][PRIORITY]["items"], expected_priority)
                all_items = expected_super + expected_priority
                self.assertEqual(len(all_items), len(set(all_items)))

    def test_runtime_evidence_uses_actual_priority_executor_not_marker_only(self) -> None:
        class Store:
            @staticmethod
            def get_runtime_sync(_key: str) -> dict[str, object] | None:
                return None

            @staticmethod
            def running_rows() -> list[dict[str, object]]:
                return []

        class Executor:
            @staticmethod
            def is_active(_root: str, task_type: str, _entity_id: int) -> bool:
                return task_type == "segment_super_priority"

        state = self._claimed_running_state(SUPER_PRIORITY)
        evidence = _static_lane_runtime_evidence(
            store=Store(),
            root="/var/www/mcd",
            static_state=state,
            running={},
            priority_executor=Executor(),
            now_ts=110.0,
        )
        self.assertTrue(evidence[(SUPER_PRIORITY, 59)]["executor_active"])
        self.assertNotIn((PRIORITY, 59), evidence)

    def test_runtime_evidence_adopts_independent_process_or_db_probe_without_marker(self) -> None:
        class Store:
            @staticmethod
            def get_runtime_sync(_key: str) -> dict[str, object] | None:
                return None

            @staticmethod
            def running_rows() -> list[dict[str, object]]:
                return []

        state = self._claimed_running_state(PRIORITY)
        evidence = _static_lane_runtime_evidence(
            store=Store(),
            root="/var/www/mcd",
            static_state=state,
            running={},
            priority_executor=None,
            liveness_probe=lambda _task: {"db": True},
            now_ts=110.0,
        )
        self.assertTrue(evidence[(PRIORITY, 59)]["db"])
        reconciled, _added, _started = reconcile_state(
            state,
            plans={SUPER_PRIORITY: [], PRIORITY: [59]},
            now_ts=110.0,
            active_evidence=evidence,
            orphan_grace_sec=1,
        )
        self.assertEqual(reconciled["lanes"][PRIORITY]["running"], [59])

    def test_foreign_chain_marker_does_not_block_static_root(self) -> None:
        root = "/var/www/current"
        foreign_root = "/var/www/grubinshowroom/public_html"
        chain_key = daemon._segment_chain_runtime_key(root, 59)

        class Store:
            @staticmethod
            def get_runtime_sync(key: str) -> dict[str, object] | None:
                if key == chain_key:
                    return {
                        "schema": "mcd-segment-execution-chain-v1",
                        "root": foreign_root,
                        "owner_id": 59,
                        "entity_ids": [59],
                        "status": "running",
                        "task_key": f"{foreign_root}|segment_priority|59",
                        "pid": 123,
                        "pid_start_time": 456,
                    }
                return None

            @staticmethod
            def running_rows() -> list[dict[str, object]]:
                return []

        state = self._claimed_running_state(PRIORITY)
        evidence = _static_lane_runtime_evidence(
            store=Store(),
            root=root,
            static_state=state,
            running={},
            priority_executor=None,
            now_ts=110.0,
        )
        self.assertNotIn("taskstore_active", evidence[(PRIORITY, 59)])

    def test_live_chain_owner_still_protects_non_owner_static_member(self) -> None:
        root = "/var/www/current"
        chain_key = daemon._segment_chain_runtime_key(root, 59)

        class Store:
            @staticmethod
            def get_runtime_sync(key: str) -> dict[str, object] | None:
                if key == chain_key:
                    return {
                        "schema": "mcd-segment-execution-chain-v1",
                        "root": root,
                        "owner_id": 59,
                        "entity_ids": [59],
                        "status": "running",
                        "task_key": f"{root}|segment_priority|59",
                        "pid": 123,
                        "pid_start_time": 456,
                        "command_str": "php bin/console mautic:segments:update -i 59",
                    }
                return None

            @staticmethod
            def running_rows() -> list[dict[str, object]]:
                return []

        state = self._claimed_running_state(PRIORITY)
        with patch.object(daemon, "_is_pid_alive", return_value=True), patch.object(
            daemon, "_pid_start_time", return_value=456
        ), patch.object(daemon, "_pid_matches_task_command", return_value=True):
            evidence = _static_lane_runtime_evidence(
                store=Store(),
                root=root,
                static_state=state,
                running={},
                priority_executor=None,
                now_ts=110.0,
            )
        self.assertTrue(evidence[(PRIORITY, 59)]["taskstore_active"])


if __name__ == "__main__":
    unittest.main()
