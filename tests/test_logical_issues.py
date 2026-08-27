from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.logical_issues import (
    LogicalIssueStore,
    detect_segment_logical_issues,
    logical_issue_blocked_segment_ids,
    remediate_logical_issue,
    scan_install_logical_issues,
)


def _leadlist_filter(*segment_ids: int) -> str:
    items = "".join(
        f'i:{index};s:{len(str(segment_id))}:"{segment_id}";'
        for index, segment_id in enumerate(segment_ids)
    )
    return (
        'a:1:{i:0;a:5:{s:5:"field";s:8:"leadlist";s:4:"type";s:8:"leadlist";'
        f's:8:"operator";s:2:"in";s:6:"filter";a:{len(segment_ids)}:{{{items}}}}}}}'
    )


class LogicalIssueDetectionTests(unittest.TestCase):
    def test_acta_shape_cycle_blocks_cycle_and_published_descendants_only(self) -> None:
        rows = [
            {"id": 632, "name": "632", "is_published": 1, "filters": _leadlist_filter(658)},
            {"id": 658, "name": "658", "is_published": 1, "filters": _leadlist_filter(660)},
            {"id": 660, "name": "660", "is_published": 1, "filters": _leadlist_filter(661)},
            {"id": 661, "name": "661", "is_published": 1, "filters": _leadlist_filter(632)},
            {"id": 644, "name": "dependent", "is_published": 1, "filters": _leadlist_filter(632)},
            {"id": 793, "name": "valid tag segment", "is_published": 1, "filters": "a:0:{}"},
        ]

        issues = detect_segment_logical_issues(rows)

        cycle = next(issue for issue in issues if issue.code == "dependency_cycle")
        self.assertEqual(cycle.entity_ids, [632, 658, 660, 661])
        self.assertEqual(cycle.blocked_entity_ids, [632, 644, 658, 660, 661])
        self.assertNotIn(793, cycle.blocked_entity_ids)

    def test_detects_missing_unpublished_self_and_invalid_filters(self) -> None:
        invalid_date = (
            'a:1:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:20:"user_newsletter_date";s:4:"type";s:4:"date";'
            's:8:"operator";s:3:"gte";s:10:"properties";a:1:{s:6:"filter";s:8:"today -1";}'
            's:6:"filter";s:8:"today -1";s:7:"display";N;}}'
        )
        rows = [
            {"id": 1, "name": "unpublished", "is_published": 0, "filters": "a:0:{}"},
            {"id": 2, "name": "missing", "is_published": 1, "filters": _leadlist_filter(999)},
            {"id": 3, "name": "unpublished dep", "is_published": 1, "filters": _leadlist_filter(1)},
            {"id": 4, "name": "self", "is_published": 1, "filters": _leadlist_filter(4)},
            {"id": 5, "name": "invalid", "is_published": 1, "filters": invalid_date},
        ]

        issues = detect_segment_logical_issues(rows)

        self.assertEqual(
            {issue.code for issue in issues},
            {"missing_dependency", "unpublished_dependency", "self_reference", "invalid_filter"},
        )

    def test_store_resolves_missing_issue_and_keeps_action_history(self) -> None:
        rows = [
            {"id": 10, "name": "broken", "is_published": 1, "filters": _leadlist_filter(404)},
        ]
        issue = detect_segment_logical_issues(rows)[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogicalIssueStore(str(Path(temp_dir) / "state.db"))
            store.sync("/srv/mautic", [issue], now_ts=100.0)
            snapshot = store.snapshot("/srv/mautic")
            self.assertEqual(snapshot["summary"]["active"], 1)
            self.assertEqual(logical_issue_blocked_segment_ids(snapshot), {10})

            store.record_action(
                root="/srv/mautic",
                issue_id=issue.issue_id,
                action="disable_segments",
                actor="operator",
                status="success",
                reason="test",
                before=[{"id": 10, "is_published": 1}],
                after=[{"id": 10, "is_published": 0}],
                now_ts=110.0,
            )
            store.sync("/srv/mautic", [], now_ts=120.0)
            snapshot = store.snapshot("/srv/mautic")
            self.assertEqual(snapshot["summary"]["active"], 0)
            self.assertEqual(snapshot["actions"][0]["actor"], "operator")
            store.close()

    def test_guarded_remediation_disables_full_affected_branch_and_audits(self) -> None:
        class FakeDB:
            rows = [
                {"id": 1, "name": "one", "is_published": 1, "filters": _leadlist_filter(2)},
                {"id": 2, "name": "two", "is_published": 1, "filters": _leadlist_filter(1)},
                {"id": 3, "name": "dependent", "is_published": 1, "filters": _leadlist_filter(1)},
            ]

            def __init__(self, _cfg) -> None:
                pass

            def fetch_all_segment_filters(self):
                return [dict(row) for row in self.rows]

            def disable_segments(self, segment_ids, *, issue_id, reason, actor):
                before = [dict(row) for row in self.rows if row["id"] in segment_ids]
                for row in self.rows:
                    if row["id"] in segment_ids:
                        row["is_published"] = 0
                        row["description"] = f"{issue_id}: {reason}: {actor}"
                after = [dict(row) for row in self.rows if row["id"] in segment_ids]
                return {"before": before, "after": after}

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(state_db_path=str(Path(temp_dir) / "state.db"))
            install = SimpleNamespace(root="/srv/mautic", db=object())
            with patch("mcd_agent.logical_issues.MauticDB", FakeDB):
                initial = scan_install_logical_issues(cfg, install)
                issue_id = initial["issues"][0]["issue_id"]
                result = remediate_logical_issue(
                    cfg,
                    install,
                    issue_id=issue_id,
                    action="disable_segments",
                    actor="ops.user",
                )

            self.assertEqual(result["disabled_segment_ids"], [1, 2, 3])
            self.assertEqual(result["snapshot"]["summary"]["active"], 0)
            self.assertEqual(result["snapshot"]["actions"][0]["actor"], "ops.user")


if __name__ == "__main__":
    unittest.main()
