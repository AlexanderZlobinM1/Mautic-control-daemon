from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.state_push import (
    MCCStatePusher,
    _read_campaign_native_fallback_events,
    _read_campaign_native_fallback_stats,
)


class CampaignNativeFallbackStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(state_db_path=str(Path(self.tmp.name) / "state.sqlite"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stats_accumulate_and_survive_pusher_restart(self) -> None:
        pusher = MCCStatePusher(self.cfg)  # type: ignore[arg-type]
        pusher.add_campaign_native_fallback(
            {
                "root": "/var/www/example/public_html",
                "status": "ok",
                "operation_rc": 0,
                "metrics_status": "ok",
                "pending_before": 4,
                "pending_after": 0,
                "email_stats_before": 10,
                "email_stats_after": 13,
                "duration_sec": 75,
                "schedule_delay_sec": 9,
            }
        )
        pusher.add_campaign_native_fallback(
            {
                "root": "/var/www/example/public_html",
                "status": "error",
                "failed_stage": "trigger",
                "operation_rc": 101,
                "metrics_status": "error",
                "metrics_error": "email_stats: unavailable",
                "pending_before": None,
                "pending_after": None,
                "email_stats_before": None,
                "email_stats_after": None,
            }
        )

        stats = _read_campaign_native_fallback_stats(self.cfg)  # type: ignore[arg-type]
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["runs"], 2)
        self.assertEqual(stats[0]["native_errors"], 1)
        self.assertEqual(stats[0]["metric_errors"], 1)
        self.assertEqual(stats[0]["recovered_runs"], 1)
        self.assertEqual(stats[0]["recovered_pending"], 4)
        self.assertEqual(stats[0]["recovered_email_stats"], 3)

        restarted = MCCStatePusher(self.cfg)  # type: ignore[arg-type]
        self.assertGreater(restarted.campaign_native_fallback_last_run_ts("/var/www/example/public_html"), 0)
        signals = restarted._signals_payload()
        self.assertEqual(signals["totals"]["campaign_native_fallback_runs"], 2)
        self.assertEqual(signals["totals"]["campaign_native_fallback_recovered_runs"], 1)
        self.assertEqual(signals["totals"]["campaign_native_fallback_recovered_email_stats"], 3)
        self.assertEqual(len(signals["details"]["campaign_native_fallback_recent"]), 2)
        self.assertTrue(signals["details"]["campaign_native_fallback_recent"][0]["event_id"])
        self.assertEqual(signals["details"]["campaign_native_fallback_retention"]["days"], 30)
        self.assertEqual(signals["details"]["campaign_native_fallback_recent"][0]["duration_sec"], 75)

        restarted.set_campaign_native_fallback_runtime(
            "/var/www/example/public_html",
            {
                "status": "running",
                "pid": 123,
                "started_at": "2026-08-06T10:00:00Z",
            },
        )
        runtime_signals = restarted._signals_payload()
        self.assertEqual(
            runtime_signals["details"]["campaign_native_fallback_runtime"][0]["status"],
            "running",
        )
        restarted.set_campaign_native_fallback_runtime("/var/www/example/public_html", None)
        self.assertNotIn("campaign_native_fallback_runtime", restarted._signals_payload()["details"])

    def test_event_retention_removes_rows_older_than_thirty_days(self) -> None:
        pusher = MCCStatePusher(self.cfg)  # type: ignore[arg-type]
        pusher.add_campaign_native_fallback(
            {
                "root": "/var/www/example/public_html",
                "status": "ok",
                "operation_rc": 0,
                "metrics_status": "ok",
                "pending_before": 0,
                "pending_after": 0,
                "email_stats_before": 0,
                "email_stats_after": 0,
            }
        )
        old_payload = json.dumps({"root": "/var/www/old", "status": "ok"})
        with sqlite3.connect(self.cfg.state_db_path) as conn:
            conn.execute(
                "INSERT INTO campaign_native_fallback_events(root, created_at, payload_json) VALUES (?, ?, ?)",
                ("/var/www/old", time.time() - 31 * 86400, old_payload),
            )
        pusher.add_campaign_native_fallback(
            {
                "root": "/var/www/example/public_html",
                "status": "ok",
                "operation_rc": 0,
                "metrics_status": "ok",
                "pending_before": 0,
                "pending_after": 0,
                "email_stats_before": 0,
                "email_stats_after": 0,
            }
        )

        events = _read_campaign_native_fallback_events(self.cfg, limit=50)  # type: ignore[arg-type]
        self.assertEqual([row["root"] for row in events], [
            "/var/www/example/public_html",
            "/var/www/example/public_html",
        ])

    def test_existing_stats_table_is_migrated_for_recovered_run_counter(self) -> None:
        with sqlite3.connect(self.cfg.state_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE campaign_native_fallback_stats (
                  root TEXT PRIMARY KEY,
                  runs INTEGER NOT NULL DEFAULT 0,
                  native_errors INTEGER NOT NULL DEFAULT 0,
                  metric_errors INTEGER NOT NULL DEFAULT 0,
                  recovered_pending INTEGER NOT NULL DEFAULT 0,
                  recovered_email_stats INTEGER NOT NULL DEFAULT 0,
                  first_run_at REAL NOT NULL,
                  last_run_at REAL NOT NULL,
                  last_status TEXT NOT NULL,
                  last_failed_stage TEXT NOT NULL DEFAULT '',
                  last_operation_rc INTEGER,
                  last_metrics_status TEXT NOT NULL DEFAULT 'unknown',
                  last_metrics_error TEXT NOT NULL DEFAULT '',
                  last_pending_before INTEGER,
                  last_pending_after INTEGER,
                  last_email_stats_before INTEGER,
                  last_email_stats_after INTEGER
                )
                """
            )

        pusher = MCCStatePusher(self.cfg)  # type: ignore[arg-type]
        pusher.add_campaign_native_fallback(
            {
                "root": "/var/www/example/public_html",
                "status": "ok",
                "operation_rc": 0,
                "metrics_status": "ok",
                "pending_before": 1,
                "pending_after": 0,
                "email_stats_before": 3,
                "email_stats_after": 3,
            }
        )

        stats = _read_campaign_native_fallback_stats(self.cfg)  # type: ignore[arg-type]
        self.assertEqual(stats[0]["recovered_runs"], 1)

    def test_existing_098284_counter_is_backfilled_from_retained_events(self) -> None:
        root = "/var/www/example/public_html"
        recovered = {
            "root": root,
            "status": "ok",
            "operation_rc": 0,
            "metrics_status": "ok",
            "pending_before": 0,
            "pending_after": 0,
            "email_stats_before": 10,
            "email_stats_after": 16,
        }
        with sqlite3.connect(self.cfg.state_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE campaign_native_fallback_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  root TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE campaign_native_fallback_stats (
                  root TEXT PRIMARY KEY,
                  runs INTEGER NOT NULL DEFAULT 0,
                  native_errors INTEGER NOT NULL DEFAULT 0,
                  metric_errors INTEGER NOT NULL DEFAULT 0,
                  recovered_runs INTEGER NOT NULL DEFAULT 0,
                  recovered_pending INTEGER NOT NULL DEFAULT 0,
                  recovered_email_stats INTEGER NOT NULL DEFAULT 0,
                  first_run_at REAL NOT NULL,
                  last_run_at REAL NOT NULL,
                  last_status TEXT NOT NULL,
                  last_failed_stage TEXT NOT NULL DEFAULT '',
                  last_operation_rc INTEGER,
                  last_metrics_status TEXT NOT NULL DEFAULT 'unknown',
                  last_metrics_error TEXT NOT NULL DEFAULT '',
                  last_pending_before INTEGER,
                  last_pending_after INTEGER,
                  last_email_stats_before INTEGER,
                  last_email_stats_after INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO campaign_native_fallback_events(root, created_at, payload_json) VALUES (?, ?, ?)",
                (root, time.time(), json.dumps(recovered)),
            )
            conn.execute(
                """
                INSERT INTO campaign_native_fallback_stats (
                  root, runs, recovered_runs, recovered_email_stats,
                  first_run_at, last_run_at, last_status
                ) VALUES (?, 1, 0, 6, ?, ?, 'ok')
                """,
                (root, time.time(), time.time()),
            )

        first = _read_campaign_native_fallback_stats(self.cfg)  # type: ignore[arg-type]
        second = _read_campaign_native_fallback_stats(self.cfg)  # type: ignore[arg-type]
        self.assertEqual(first[0]["recovered_runs"], 1)
        self.assertEqual(second[0]["recovered_runs"], 1)


if __name__ == "__main__":
    unittest.main()
