from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcd_agent.daemon import (
    _manual_static_lane_for_ids,
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

    def test_mautic7_sibling_terminals_do_not_schedule_internal_parent(self) -> None:
        plans = build_lane_plans(
            super_priority_ids=[58, 59],
            priority_ids=[],
            parents_by_child={58: {57}, 59: {57}},
            children_by_parent={57: {58, 59}},
            mautic_major=7,
        )
        self.assertEqual(plans[SUPER_PRIORITY], [58, 59])
        self.assertNotIn(57, plans[SUPER_PRIORITY])

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
            (7, [58, 59], []),
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


if __name__ == "__main__":
    unittest.main()
