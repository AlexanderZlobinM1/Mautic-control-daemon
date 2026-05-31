from __future__ import annotations

import unittest

from types import SimpleNamespace

from mcd_agent.segment_dependencies import (
    dependent_segment_closure,
    dependency_segment_closure,
    extract_leadlist_filter_segment_ids,
    segment_dependency_blocked_ids,
    segment_dependency_maps,
    stale_dependent_segment_closure,
    suppress_mautic_cascade_dependencies,
)


class SegmentDependencyTests(unittest.TestCase):
    def test_extracts_leadlist_filter_ids_from_php_serialized_filters(self) -> None:
        filters = (
            'a:2:{i:0;a:5:{s:5:"field";s:8:"leadlist";s:4:"type";s:8:"leadlist";'
            's:8:"operator";s:2:"in";s:6:"filter";a:1:{i:0;s:1:"6";}}'
            'i:1;a:5:{s:5:"field";s:4:"tags";s:6:"filter";a:1:{i:0;s:1:"9";}}}'
        )

        self.assertEqual(extract_leadlist_filter_segment_ids(filters), {6})

    def test_dependency_maps_and_closure(self) -> None:
        rows = [
            {"id": 6, "filters": "a:0:{}"},
            {
                "id": 8,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:1:"6";}}}'
                ),
            },
            {
                "id": 9,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:1:"8";}}}'
                ),
            },
        ]

        children, parents = segment_dependency_maps(rows)

        self.assertEqual(children, {6: {8}, 8: {9}})
        self.assertEqual(parents, {8: {6}, 9: {8}})
        self.assertEqual(dependent_segment_closure({6}, children), {8, 9})
        self.assertEqual(dependency_segment_closure({9}, parents), {6, 8})

    def test_suppresses_mautic7_dependencies_already_rebuilt_by_child_command(self) -> None:
        planned, suppressed = suppress_mautic_cascade_dependencies([6, 8, 9, 10], {8: {6}, 9: {8}})

        self.assertEqual(planned, [9, 10])
        self.assertEqual(suppressed, {6, 8})

    def test_blocks_child_until_parent_finished_or_not_running(self) -> None:
        running = {
            "k": SimpleNamespace(root="/var/www/app", task_type="segment", entity_id=6)
        }

        blocked = segment_dependency_blocked_ids(
            root="/var/www/app",
            candidate_ids={6, 8},
            parents_by_child={8: {6}},
            running=running,
            recently_finished=set(),
        )
        self.assertEqual(blocked, {8})

        unblocked = segment_dependency_blocked_ids(
            root="/var/www/app",
            candidate_ids={8},
            parents_by_child={8: {6}},
            running={},
            recently_finished={6},
        )
        self.assertEqual(unblocked, set())

    def test_stale_dependency_follow_up_skips_child_already_built_after_parent(self) -> None:
        rows = [
            {
                "id": 6,
                "filters": "a:0:{}",
                "last_built_date": "2026-05-13 06:00:00",
            },
            {
                "id": 8,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:1:"6";}}}'
                ),
                "last_built_date": "2026-05-13 06:05:00",
            },
        ]
        children, _ = segment_dependency_maps(rows)

        self.assertEqual(stale_dependent_segment_closure({6}, rows, children), set())

    def test_stale_dependency_follow_up_keeps_child_older_than_parent(self) -> None:
        rows = [
            {
                "id": 6,
                "filters": "a:0:{}",
                "last_built_date": "2026-05-13 06:00:00",
            },
            {
                "id": 8,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:1:"6";}}}'
                ),
                "last_built_date": "2026-05-13 05:59:59",
            },
        ]
        children, _ = segment_dependency_maps(rows)

        self.assertEqual(stale_dependent_segment_closure({6}, rows, children), {8})


if __name__ == "__main__":
    unittest.main()
