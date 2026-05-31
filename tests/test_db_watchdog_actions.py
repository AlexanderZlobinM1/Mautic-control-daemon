from __future__ import annotations

import unittest
import sys
import types

try:
    import pymysql  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pymysql"] = types.SimpleNamespace(cursors=types.SimpleNamespace(DictCursor=object))

from mcd_agent.db_watchdog import _apply_rule_action, _is_stale_mcd_tmp_segment_query


class _FakeDB:
    def __init__(self) -> None:
        self.killed_queries: list[int] = []
        self.killed_connections: list[int] = []

    def kill_query(self, process_id: int) -> None:
        self.killed_queries.append(process_id)

    def kill_connection(self, process_id: int) -> None:
        self.killed_connections.append(process_id)


class DBWatchdogActionTests(unittest.TestCase):
    def test_kill_query_observe_only_does_not_call_db(self) -> None:
        db = _FakeDB()
        event = _apply_rule_action(
            db,  # type: ignore[arg-type]
            rule={"id": "heavy_page_hits", "action": "kill_query"},
            row={"id": 42, "command": "Query", "time_sec": 901, "info_head": "SELECT ... ss_page_hits"},
            observe_only=True,
        )

        self.assertEqual(event["status"], "observe_only")
        self.assertEqual(db.killed_queries, [])

    def test_kill_query_applies_to_query_pid(self) -> None:
        db = _FakeDB()
        event = _apply_rule_action(
            db,  # type: ignore[arg-type]
            rule={"id": "heavy_page_hits", "action": "kill_query"},
            row={"id": 42, "command": "Query", "time_sec": 901, "info_head": "SELECT ... ss_page_hits"},
            observe_only=False,
        )

        self.assertEqual(event["status"], "applied")
        self.assertEqual(db.killed_queries, [42])

    def test_kill_query_skips_non_query_rows(self) -> None:
        db = _FakeDB()
        event = _apply_rule_action(
            db,  # type: ignore[arg-type]
            rule={"id": "sleep", "action": "kill_query"},
            row={"id": 42, "command": "Sleep", "time_sec": 901, "info_head": ""},
            observe_only=False,
        )

        self.assertEqual(event["status"], "skipped")
        self.assertEqual(event["reason"], "not_query")
        self.assertEqual(db.killed_queries, [])

    def test_stale_mcd_tmp_segment_query_matches_only_old_temp_rebuilds(self) -> None:
        self.assertTrue(
            _is_stale_mcd_tmp_segment_query(
                {
                    "command": "Query",
                    "time_sec": 1800,
                    "info_head": "INSERT IGNORE INTO `mcd_tmp_segment_leads` (`lead_id`) SELECT ...",
                },
                min_time_sec=1800,
            )
        )
        self.assertFalse(
            _is_stale_mcd_tmp_segment_query(
                {
                    "command": "Query",
                    "time_sec": 1799,
                    "info_head": "INSERT IGNORE INTO `mcd_tmp_segment_leads` (`lead_id`) SELECT ...",
                },
                min_time_sec=1800,
            )
        )
        self.assertFalse(
            _is_stale_mcd_tmp_segment_query(
                {"command": "Query", "time_sec": 5000, "info_head": "SELECT * FROM ss_page_hits"},
                min_time_sec=1800,
            )
        )


if __name__ == "__main__":
    unittest.main()
