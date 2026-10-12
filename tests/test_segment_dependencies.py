from __future__ import annotations

import unittest

import phpserialize

from types import SimpleNamespace

from mcd_agent.segment_dependencies import (
    dependent_segment_closure,
    dependency_execution_chains,
    dependency_expanded_segment_plan,
    dependency_segment_closure,
    extract_leadlist_filter_segment_ids,
    mautic7_terminal_segment_plan,
    segment_dependency_blocked_ids,
    segment_dependency_maps,
    segment_related_ids,
    stale_dependent_segment_closure,
    suppress_mautic_cascade_dependencies,
)


class SegmentDependencyTests(unittest.TestCase):
    def test_integer_filter_values_do_not_include_array_keys_or_next_clause_index(self) -> None:
        filters = phpserialize.dumps([
            {"field": "leadlist", "properties": {"filter": [2]}, "filter": [2]},
            {"field": "leadlist", "operator": "!in_all", "properties": {"filter": [5]}},
        ]).decode()
        self.assertEqual(extract_leadlist_filter_segment_ids(filters), {2, 5})

    def test_integer_filter_values_ignore_numeric_tags_and_preserve_mixed_ids(self) -> None:
        filters = phpserialize.dumps([
            {"field": "leadlist", "properties": {"filter": [2, "5"]}},
            {"field": "tags", "properties": {"filter": ["99"]}},
        ]).decode()
        self.assertEqual(extract_leadlist_filter_segment_ids(filters), {2, 5})

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

    def test_dependency_maps_ignore_references_to_missing_segments(self) -> None:
        rows = [
            {
                "id": 8,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:1:"6";}}}'
                ),
            },
        ]

        children, parents = segment_dependency_maps(rows)

        self.assertEqual(children, {})
        self.assertEqual(parents, {})

    def test_suppresses_mautic7_dependencies_already_rebuilt_by_child_command(self) -> None:
        planned, suppressed = suppress_mautic_cascade_dependencies([6, 8, 9, 10], {8: {6}, 9: {8}})

        self.assertEqual(planned, [9, 10])
        self.assertEqual(suppressed, {6, 8})

    def test_mautic7_preserves_visible_due_segments_with_terminal_dependents(self) -> None:
        rows = [
            {"id": 11, "filters": "a:0:{}"},
            {
                "id": 57,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"11";}}}'
                ),
            },
            {
                "id": 106,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"11";}}}'
                ),
            },
            {
                "id": 61,
                "filters": (
                    'a:2:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"57";}}'
                    'i:1;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:3:"106";}}}'
                ),
            },
            {"id": 200, "filters": "a:0:{}"},
        ]
        children, _parents = segment_dependency_maps(rows)

        planned, suppressed = mautic7_terminal_segment_plan([57, 200], children)

        self.assertEqual(planned, [57, 200])
        self.assertEqual(suppressed, set())

    def test_mautic7_dnc_visible_segments_are_not_covered_only_by_children(self) -> None:
        children = {
            3: {28, 30, 34},
            4: {29, 32, 33, 35},
        }

        planned, suppressed = mautic7_terminal_segment_plan([3, 4, 28, 29], children)

        self.assertEqual(planned, [3, 4, 28, 29])
        self.assertEqual(suppressed, set())

    def test_older_mautic_plan_expands_dependencies_before_child(self) -> None:
        rows = [
            {"id": 21, "filters": "a:0:{}"},
            {"id": 22, "filters": "a:0:{}"},
            {
                "id": 11,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:2:{i:0;s:2:"21";i:1;s:2:"22";}}}'
                ),
            },
            {
                "id": 57,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"11";}}}'
                ),
            },
            {
                "id": 106,
                "filters": (
                    'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"11";}}}'
                ),
            },
            {
                "id": 61,
                "filters": (
                    'a:2:{i:0;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:2:"57";}}'
                    'i:1;a:5:{s:5:"field";s:8:"leadlist";'
                    's:6:"filter";a:1:{i:0;s:3:"106";}}}'
                ),
            },
        ]
        _children, parents = segment_dependency_maps(rows)

        planned = dependency_expanded_segment_plan([61], parents)

        self.assertEqual(planned, [21, 22, 11, 57, 106, 61])

    def test_m6_shared_parent_diamond_is_one_topological_worker_chain(self) -> None:
        parents = {11: {21, 22}, 57: {11}, 106: {11}, 61: {57, 106}}
        children = {21: {11}, 22: {11}, 11: {57, 106}, 57: {61}, 106: {61}}

        chains = dependency_execution_chains(
            [21, 22, 11, 57, 106, 61],
            parents,
            children,
            mautic_major=6,
        )

        self.assertEqual(chains, [[21, 22, 11, 57, 106, 61]])

    def test_m6_independent_closures_keep_independent_workers(self) -> None:
        chains = dependency_execution_chains(
            [21, 22, 30],
            {22: {21}},
            {21: {22}},
            mautic_major=6,
        )

        self.assertEqual(chains, [[21, 22], [30]])

    def test_m7_overlapping_native_roots_share_one_durable_closure(self) -> None:
        chains = dependency_execution_chains(
            [57, 61, 21],
            {57: {21}, 61: {21}},
            {21: {57, 61}},
            mautic_major=7,
        )

        self.assertEqual(chains, [[57, 61, 21]])

    def test_m7_independent_native_closures_keep_independent_workers(self) -> None:
        chains = dependency_execution_chains(
            [57, 61, 21, 300],
            {57: {21}, 61: {21}},
            {21: {57, 61}},
            mautic_major=7,
        )

        self.assertEqual(chains, [[57, 61, 21], [300]])

    def test_m6_tier_projection_keeps_fresh_descendant_out_of_important_chain(self) -> None:
        parents = {11: {21, 22}, 57: {11}, 106: {11}, 61: {57, 106}}
        children = {21: {11}, 22: {11}, 11: {57, 106}, 57: {61}, 106: {61}}

        chains = dependency_execution_chains(
            [21, 22, 11, 57, 106, 61],
            parents,
            children,
            mautic_major=6,
            eligible_ids={21, 22, 11, 57, 106},
        )

        self.assertEqual(chains, [[21, 22, 11, 57, 106]])

    def test_related_ids_connect_shared_dependency_chains(self) -> None:
        children = {21: {11}, 22: {11}, 11: {57, 106}, 57: {61}, 106: {61}}
        parents = {11: {21, 22}, 57: {11}, 106: {11}, 61: {57, 106}}

        self.assertTrue(segment_related_ids(21, parents, children) & segment_related_ids(22, parents, children))
        self.assertFalse(segment_related_ids(21, parents, children) & segment_related_ids(300, parents, children))

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
