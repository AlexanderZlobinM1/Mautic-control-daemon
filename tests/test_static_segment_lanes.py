from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcd_agent.daemon import (
    _manual_static_lane_for_ids,
    _segment_priority_effective_setting,
    _segment_super_priority_effective_setting,
)
from mcd_agent.static_segment_lanes import (
    PRIORITY,
    SUPER_PRIORITY,
    build_lane_plans,
    claim_item,
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


if __name__ == "__main__":
    unittest.main()
