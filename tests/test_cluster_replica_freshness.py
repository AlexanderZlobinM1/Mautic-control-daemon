from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from mcd_agent.config import _normalize_replica_freshness_checks
from mcd_agent.state_push import _collect_replica_freshness, _galera_routing_eligibility


def _cfg(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "cluster_replica_freshness_enabled": True,
        "cluster_replica_freshness_max_age_sec": 3600,
        "cluster_replica_freshness_checks": [
            {
                "database": "baza_ananas",
                "table": "ananas_email_stats",
                "column": "date_sent",
                "order_column": "id",
                "label": "email stats",
            }
        ],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeCursor:
    def __init__(self, latest: object) -> None:
        self.latest = latest
        self.sql = ""

    def execute(self, sql: str) -> None:
        self.sql = sql

    def fetchone(self) -> dict[str, object]:
        return {"latest": self.latest}


class ClusterReplicaFreshnessTests(unittest.TestCase):
    def test_galera_routing_eligibility_requires_primary_synced_ready(self) -> None:
        ok, reason = _galera_routing_eligibility(
            {
                "ready": True,
                "connected": True,
                "cluster_status": "Primary",
                "local_state_comment": "Synced",
            }
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "primary_synced_ready")

        blocked_cases = [
            ({"ready": False, "connected": True, "cluster_status": "Primary", "local_state_comment": "Synced"}, "wsrep_ready_off"),
            ({"ready": True, "connected": False, "cluster_status": "Primary", "local_state_comment": "Synced"}, "wsrep_disconnected"),
            ({"ready": True, "connected": True, "cluster_status": "non-Primary", "local_state_comment": "Synced"}, "cluster_not_primary"),
            ({"ready": True, "connected": True, "cluster_status": "Primary", "local_state_comment": "Donor/Desynced"}, "node_not_synced"),
            ({"ready": True, "connected": True, "cluster_status": "Primary", "local_state_comment": "Joining: receiving State Transfer"}, "node_not_synced"),
        ]
        for payload, expected_reason in blocked_cases:
            with self.subTest(expected_reason=expected_reason):
                ok, reason = _galera_routing_eligibility(payload)
                self.assertFalse(ok)
                self.assertEqual(reason, expected_reason)

    def test_disabled_check_is_na(self) -> None:
        out = _collect_replica_freshness(
            FakeCursor(datetime.now(timezone.utc)),
            _cfg(cluster_replica_freshness_enabled=False),
        )

        self.assertEqual(out["status"], "na")
        self.assertFalse(out["enabled"])

    def test_recent_row_is_ok_and_uses_order_column(self) -> None:
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        cur = FakeCursor(now - timedelta(minutes=10))

        out = _collect_replica_freshness(cur, _cfg(), now_utc_dt=now)

        self.assertEqual(out["status"], "ok")
        self.assertIn("ORDER BY `id` DESC LIMIT 1", cur.sql)
        self.assertEqual(out["checks"][0]["status"], "ok")

    def test_stale_row_degrades_replica_even_when_sql_replica_lag_is_zero(self) -> None:
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        cur = FakeCursor(now - timedelta(days=10))

        out = _collect_replica_freshness(cur, _cfg(), now_utc_dt=now)

        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["checks"][0]["status"], "stale")
        self.assertGreater(out["checks"][0]["age_sec"], 3600)

    def test_invalid_identifier_is_reported_without_running_unsafe_sql(self) -> None:
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        cur = FakeCursor(now)
        cfg = _cfg(
            cluster_replica_freshness_checks=[
                {"database": "baza_ananas;DROP", "table": "x", "column": "date_sent"}
            ]
        )

        out = _collect_replica_freshness(cur, cfg, now_utc_dt=now)

        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["checks"][0]["status"], "error")
        self.assertEqual(cur.sql, "")

    def test_config_accepts_json_list(self) -> None:
        checks = _normalize_replica_freshness_checks(
            '[{"database":"db","table":"stats","column":"created_at","order_column":"id"}]'
        )

        self.assertEqual(checks[0]["database"], "db")
        self.assertEqual(checks[0]["table"], "stats")
        self.assertEqual(checks[0]["column"], "created_at")


if __name__ == "__main__":
    unittest.main()
