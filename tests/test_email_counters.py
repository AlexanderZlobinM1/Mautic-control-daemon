from __future__ import annotations

import unittest
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.email_counters import audit_campaign_email_counters, repair_campaign_email_counters
from mcd_agent.models import DBConfig


class FakeCursor:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db
        self.rows: list[dict[str, Any]] = []
        self.affected = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> int:
        params = params or ()
        self.rows = []
        self.affected = 0
        if query.startswith("SHOW COLUMNS FROM `p_email_stats`"):
            self.rows = [{"Field": name} for name in ["email_id", "lead_id", "date_sent", "is_failed"]]
            return len(self.rows)
        if "FROM `p_campaign_events`" in query and "INNER JOIN" not in query:
            campaign_id = int(params[0])
            self.rows = [row.copy() for row in self.db.events if int(row["campaign_id"]) == campaign_id]
            return len(self.rows)
        if "FROM `p_emails`" in query and query.lstrip().upper().startswith("SELECT"):
            ids = {int(x) for x in params}
            self.rows = [row.copy() for row in self.db.emails.values() if int(row["id"]) in ids]
            return len(self.rows)
        if "FROM `p_email_stats`" in query:
            ids = {int(x) for x in params}
            self.rows = [
                {
                    "email_id": email_id,
                    "actual_sent_count": count,
                    "distinct_leads": count,
                    "first_sent_at": "2026-06-29 13:56:12",
                    "last_sent_at": "2026-06-30 08:27:03",
                    "failed_count": 0,
                }
                for email_id, count in sorted(self.db.stats.items())
                if email_id in ids
            ]
            return len(self.rows)
        if "FROM `p_campaign_lead_event_log`" in query and "GROUP BY `event_id`" in query:
            campaign_id = int(params[0])
            event_ids = {int(x) for x in params[1:]}
            self.rows = [
                row.copy()
                for row in self.db.progress
                if int(row["campaign_id"]) == campaign_id and int(row["event_id"]) in event_ids
            ]
            return len(self.rows)
        if "INNER JOIN `p_campaign_events`" in query and "clel.`date_triggered` IS NULL" in query:
            ids = {int(x) for x in params}
            self.rows = [
                {"email_id": email_id, "pending_event_logs": pending}
                for email_id, pending in sorted(self.db.global_pending.items())
                if email_id in ids
            ]
            return len(self.rows)
        if query.lstrip().upper().startswith("UPDATE `P_EMAILS`"):
            actual, email_id, cached = (int(params[0]), int(params[1]), int(params[2]))
            row = self.db.emails.get(email_id)
            if row and int(row["sent_count"]) == cached:
                row["sent_count"] = actual
                self.affected = 1
                return 1
            return 0
        raise AssertionError(f"Unexpected SQL: {query}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeConnection:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)


class FakeDB:
    _safe_table = staticmethod(MauticDB._safe_table)
    _table_columns = MauticDB._table_columns

    def __init__(self) -> None:
        self.cfg = DBConfig(
            host="localhost",
            port=3306,
            name="mautic",
            user="mautic",
            password="secret",
            table_prefix="p_",
        )
        self.events = [
            {
                "id": 588,
                "campaign_id": 557,
                "name": "Newsletter",
                "type": "email.send",
                "channel": "email",
                "channel_id": 643,
            }
        ]
        self.emails = {
            643: {
                "id": 643,
                "name": "Newsletter email",
                "sent_count": 54214,
                "read_count": 23503,
            }
        }
        self.stats = {643: 158357}
        self.progress = [
            {
                "event_id": 588,
                "campaign_id": 557,
                "total_event_logs": 169709,
                "pending_event_logs": 0,
                "triggered_event_logs": 169709,
                "max_triggered_at": "2026-06-30 08:27:02",
            }
        ]
        self.global_pending: dict[int, int] = {}

    def _connect(self) -> FakeConnection:
        return FakeConnection(self)


class EmailCounterTests(unittest.TestCase):
    def test_audit_marks_completed_undercount_as_repairable(self) -> None:
        db = FakeDB()

        payload = audit_campaign_email_counters(db, 557)  # type: ignore[arg-type]

        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["mismatches"], 1)
        self.assertEqual(payload["repairable"], 1)
        row = payload["emails"][0]
        self.assertEqual(row["email_id"], 643)
        self.assertEqual(row["cached_sent_count"], 54214)
        self.assertEqual(row["actual_sent_count"], 158357)
        self.assertTrue(row["repairable"])

    def test_repair_updates_sent_count_with_compare_and_set(self) -> None:
        db = FakeDB()

        payload = repair_campaign_email_counters(db, 557)  # type: ignore[arg-type]

        self.assertEqual(payload["repaired"], 1)
        self.assertEqual(db.emails[643]["sent_count"], 158357)

    def test_repair_skips_email_with_pending_event_log_work(self) -> None:
        db = FakeDB()
        db.global_pending = {643: 2}

        payload = repair_campaign_email_counters(db, 557)  # type: ignore[arg-type]

        self.assertEqual(payload["repaired"], 0)
        self.assertEqual(db.emails[643]["sent_count"], 54214)
        self.assertFalse(payload["emails"][0]["repairable"])

    def test_repair_never_decreases_cached_sent_count(self) -> None:
        db = FakeDB()
        db.emails[643]["sent_count"] = 160000

        payload = repair_campaign_email_counters(db, 557)  # type: ignore[arg-type]

        self.assertEqual(payload["repaired"], 0)
        self.assertEqual(db.emails[643]["sent_count"], 160000)
        self.assertFalse(payload["emails"][0]["repairable"])


if __name__ == "__main__":
    unittest.main()
