from __future__ import annotations

import unittest
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.email_activity_report import collect_email_activity_report
from mcd_agent.models import DBConfig


class FakeCursor:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> int:
        self.db.queries.append((query, params or ()))
        if "GROUP BY DATE(es.date_sent), es.email_id" in query:
            self.rows = [
                {
                    "day": "2026-07-07",
                    "email_id": 66,
                    "email_name": "Newsletter",
                    "sent": 5,
                    "read_cnt": 3,
                    "failed_cnt": 1,
                    "clicks": 2,
                    "unsubscribed": 1,
                }
            ]
        else:
            self.rows = [
                {
                    "day": "2026-07-07",
                    "sent": 5,
                    "read_cnt": 3,
                    "failed_cnt": 1,
                    "clicks": 2,
                    "unsubscribed": 1,
                }
            ]
        return len(self.rows)

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

    def __init__(self) -> None:
        self.cfg = DBConfig(
            host="localhost",
            port=3306,
            name="mautic",
            user="mautic",
            password="secret",
            table_prefix="ss",
        )
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def _connect(self) -> FakeConnection:
        return FakeConnection(self)


class EmailActivityReportTests(unittest.TestCase):
    def test_collects_summary_and_extended_using_instance_prefix(self) -> None:
        db = FakeDB()

        payload = collect_email_activity_report(db, days=7, include_summary=True, include_extended=True)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary_totals"]["sent"], 5)
        self.assertEqual(payload["extended_rows"][0]["email_id"], 66)
        rendered = "\n".join(query for query, _params in db.queries)
        self.assertIn("`ssemail_stats`", rendered)
        self.assertIn("`sspage_hits`", rendered)
        self.assertEqual(db.queries[0][1], (6, 6, 6))

    def test_fresh_contact_filter_joins_leads_for_each_source(self) -> None:
        db = FakeDB()

        collect_email_activity_report(db, days=3, include_summary=True, include_extended=False, contact_mode="fresh", contact_age_days=14)

        query, params = db.queries[0]
        self.assertIn("JOIN `ssleads` l ON l.id = es.lead_id", query)
        self.assertIn("JOIN `ssleads` lph ON lph.id = ph.lead_id", query)
        self.assertIn("JOIN `ssleads` ldc ON ldc.id = dnc.lead_id", query)
        self.assertEqual(params, (2, 14, 2, 14, 2, 14))


if __name__ == "__main__":
    unittest.main()
