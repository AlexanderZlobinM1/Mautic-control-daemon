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
    logical_issue_terminal_segment_notes,
    prune_logical_issue_snapshots,
    remediate_logical_issue,
    remediate_logical_issues,
    scan_install_logical_issues,
)
from mcd_agent.daemon import TaskStore


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
    def test_stale_instance_snapshots_are_pruned_from_primary_and_shadow_contract(self) -> None:
        class RuntimeStore:
            def __init__(self) -> None:
                self.values = {}

            def list_runtime_sync(self, prefix):
                return sorted((key, value) for key, value in self.values.items() if key.startswith(prefix))

            def delete_runtime_sync(self, keys):
                for key in keys:
                    self.values.pop(key, None)

        runtime = RuntimeStore()
        keep_key = LogicalIssueStore._key("/srv/active")
        stale_key = LogicalIssueStore._key("/srv/deleted")
        runtime.values = {keep_key: {"issues": []}, stale_key: {"issues": []}, "other:key": {}}

        removed = prune_logical_issue_snapshots(
            "/unused.db",
            ["/srv/active"],
            runtime_store=runtime,
        )

        self.assertEqual(removed, 1)
        self.assertEqual(set(runtime.values), {keep_key, "other:key"})

    def test_mysql_shadow_prune_keeps_only_bounded_logical_issue_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(str(Path(temp_dir) / "state.db"))
            store.put_runtime_sync("logical_issues:v1:one", {"issues": []})
            store.put_runtime_sync("logical_issues:v1:two", {"issues": []})
            store.put_runtime_sync("unbounded_feature_history:one", {"items": list(range(500))})
            store._mysql_mode = True

            store._sqlite_prune_for_failover()

            keys = {
                str(row["key"])
                for row in store.conn.execute("SELECT key FROM runtime_sync ORDER BY key").fetchall()
            }
            self.assertEqual(keys, {"logical_issues:v1:one", "logical_issues:v1:two"})
            store.close()

    def test_runtime_sync_backend_is_used_and_history_is_bounded(self) -> None:
        class RuntimeStore:
            def __init__(self) -> None:
                self.values = {}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, payload):
                self.values[key] = payload

        runtime = RuntimeStore()
        issue = detect_segment_logical_issues(
            [{"id": 10, "name": "broken", "is_published": 1, "filters": _leadlist_filter(404)}]
        )[0]
        store = LogicalIssueStore("/path/that/must/not/be/created.db", runtime_store=runtime)
        store.sync("/srv/mautic", [issue], now_ts=100.0)
        for index in range(60):
            store.record_action(
                root="/srv/mautic",
                issue_id=issue.issue_id,
                action="disable_segments",
                actor="operator",
                status="success",
                reason=str(index),
                before=[],
                after=[],
                now_ts=101.0 + index,
            )

        snapshot = store.snapshot("/srv/mautic")

        self.assertEqual(snapshot["summary"]["active"], 1)
        self.assertEqual(len(snapshot["actions"]), 50)
        self.assertEqual(len(runtime.values), 1)
        self.assertEqual(snapshot["actions"][0]["reason"], "59")

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

    def test_long_dependency_chain_does_not_use_python_recursion(self) -> None:
        rows = [
            {
                "id": segment_id,
                "name": str(segment_id),
                "is_published": 1,
                "filters": _leadlist_filter(segment_id + 1) if segment_id < 2500 else "a:0:{}",
            }
            for segment_id in range(1, 2501)
        ]

        self.assertEqual(detect_segment_logical_issues(rows), [])

    def test_detail_limit_does_not_drop_scheduler_blocks(self) -> None:
        issues = detect_segment_logical_issues(
            [
                {
                    "id": segment_id,
                    "name": str(segment_id),
                    "is_published": 1,
                    "filters": _leadlist_filter(segment_id),
                }
                for segment_id in range(1, 151)
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogicalIssueStore(str(Path(temp_dir) / "state.db"))
            store.sync("/srv/mautic", issues)
            snapshot = store.snapshot("/srv/mautic")
            store.close()

        self.assertEqual(snapshot["summary"]["active"], 150)
        self.assertTrue(snapshot["summary"]["truncated"])
        self.assertEqual(len(snapshot["issues"]), 100)
        self.assertEqual(len(logical_issue_blocked_segment_ids(snapshot)), 150)

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
        warnings = [issue for issue in issues if issue.severity == "warning"]
        self.assertEqual(
            {issue.code for issue in warnings},
            {"missing_dependency", "unpublished_dependency", "invalid_filter"},
        )
        self.assertTrue(all(issue.blocked_entity_ids == [] for issue in warnings))
        self.assertTrue(all(issue.recommended_action == "" for issue in warnings))
        self.assertEqual(logical_issue_blocked_segment_ids({"issues": [issue.payload() for issue in issues]}), {4})
        self.assertEqual(
            logical_issue_terminal_segment_notes({"issues": [issue.payload() for issue in issues]}),
            {4: "data_error:self_reference"},
        )

    def test_store_resolves_missing_issue_and_keeps_action_history(self) -> None:
        rows = [
            {"id": 10, "name": "broken", "is_published": 1, "filters": _leadlist_filter(10)},
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

            def disable_segments(self, segment_ids, *, issue_id, reason, actor, segment_contexts=None):
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

    def test_guarded_remediation_disables_only_selected_current_segments(self) -> None:
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

            def disable_segments(self, segment_ids, *, issue_id, reason, actor, segment_contexts=None):
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
                    segment_ids=[1],
                )

            self.assertEqual(result["disabled_segment_ids"], [1])
            self.assertEqual(result["snapshot"]["blocked_segment_ids"], [2, 3])
            self.assertEqual(
                [row["is_published"] for row in FakeDB.rows],
                [0, 1, 1],
            )

    def test_guarded_remediation_rejects_segment_outside_current_issue(self) -> None:
        class FakeDB:
            rows = [{"id": 4, "name": "self", "is_published": 1, "filters": _leadlist_filter(4)}]

            def __init__(self, _cfg) -> None:
                pass

            def fetch_all_segment_filters(self):
                return [dict(row) for row in self.rows]

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(state_db_path=str(Path(temp_dir) / "state.db"))
            install = SimpleNamespace(root="/srv/mautic", db=object())
            with patch("mcd_agent.logical_issues.MauticDB", FakeDB):
                initial = scan_install_logical_issues(cfg, install)
                with self.assertRaisesRegex(ValueError, "not part of the current logical issue"):
                    remediate_logical_issue(
                        cfg,
                        install,
                        issue_id=initial["issues"][0]["issue_id"],
                        action="disable_segments",
                        actor="ops.user",
                        segment_ids=[99],
                    )

    def test_batch_remediation_disables_multiple_issues_in_one_database_transaction(self) -> None:
        class FakeDB:
            rows = [
                {"id": 10, "name": "ten", "is_published": 1, "filters": _leadlist_filter(10)},
                {"id": 20, "name": "twenty", "is_published": 1, "filters": _leadlist_filter(20)},
            ]
            disable_calls = 0
            received_contexts = {}

            def __init__(self, _cfg) -> None:
                pass

            def fetch_all_segment_filters(self):
                return [dict(row) for row in self.rows]

            def disable_segments(self, segment_ids, *, issue_id, reason, actor, segment_contexts=None):
                self.__class__.disable_calls += 1
                self.__class__.received_contexts = dict(segment_contexts or {})
                before = [dict(row) for row in self.rows if row["id"] in segment_ids]
                for row in self.rows:
                    if row["id"] in segment_ids:
                        row["is_published"] = 0
                after = [dict(row) for row in self.rows if row["id"] in segment_ids]
                return {"before": before, "after": after}

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(state_db_path=str(Path(temp_dir) / "state.db"))
            install = SimpleNamespace(root="/srv/mautic", db=object())
            with patch("mcd_agent.logical_issues.MauticDB", FakeDB):
                initial = scan_install_logical_issues(cfg, install)
                targets = [
                    {"issue_id": issue["issue_id"], "segment_ids": issue["blocked_entity_ids"]}
                    for issue in initial["issues"]
                ]
                result = remediate_logical_issues(
                    cfg,
                    install,
                    targets=targets,
                    action="disable_segments",
                    actor="ops.user",
                )

        self.assertEqual(FakeDB.disable_calls, 1)
        self.assertEqual(set(FakeDB.received_contexts), {10, 20})
        self.assertEqual(result["disabled_segment_ids"], [10, 20])
        self.assertEqual(result["snapshot"]["summary"]["active"], 0)


if __name__ == "__main__":
    unittest.main()
