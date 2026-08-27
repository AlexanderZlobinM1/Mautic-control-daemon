import unittest

from mcd_agent.daemon import (
    _MAILRU_POSTMASTER_LATEST_ROWS,
    _MAIL_QUEUE_AVAILABLE_COUNT,
    _MAIL_QUEUE_TOTAL_COUNT,
    _mailru_postmaster_metrics,
)


class _PostmasterDB:
    def __init__(self, rows):
        self.rows = rows

    def fetch_rows(self, _query, limit=5000):
        return self.rows[:limit]


class MailQueueMetricQueryTests(unittest.TestCase):
    def test_total_includes_unsent_pending_and_rescheduled_rows(self) -> None:
        self.assertIn("status IN ('pending', 'rescheduled')", _MAIL_QUEUE_TOTAL_COUNT)
        self.assertIn("success = 0", _MAIL_QUEUE_TOTAL_COUNT)
        self.assertIn("date_sent IS NULL", _MAIL_QUEUE_TOTAL_COUNT)

    def test_available_requires_sendable_pending_row(self) -> None:
        self.assertIn("status = 'pending'", _MAIL_QUEUE_AVAILABLE_COUNT)
        self.assertIn("attempts < max_attempts", _MAIL_QUEUE_AVAILABLE_COUNT)
        self.assertIn("scheduled_date <= '{now_utc}'", _MAIL_QUEUE_AVAILABLE_COUNT)

    def test_postmaster_query_is_optional_and_exposes_latest_domain_state(self) -> None:
        self.assertIn("mailru_postmaster_stats", _MAILRU_POSTMASTER_LATEST_ROWS)
        self.assertIn("probably_spam_percent", _MAILRU_POSTMASTER_LATEST_ROWS)
        metrics = _mailru_postmaster_metrics(
            _PostmasterDB(
                [
                    {
                        "domain": "personal-alex.ru.",
                        "stat_date": "2026-08-18",
                        "messages_sent": 93,
                        "spam_percent": "3.22580645",
                        "probably_spam_percent": "95.69892473",
                        "synced_at": "2026-08-19 18:28:23",
                    },
                    {
                        "domain": "personal-alex.ru",
                        "stat_date": "2026-08-17",
                        "messages_sent": 12,
                        "spam_percent": "0",
                        "probably_spam_percent": "0",
                        "synced_at": "2026-08-18 18:28:23",
                    },
                    {
                        "domain": "alex-personal.ru",
                        "stat_date": "2026-08-18",
                        "messages_sent": 889,
                        "spam_percent": "0",
                        "probably_spam_percent": "0.11248594",
                        "synced_at": "2026-08-19 18:28:23",
                    },
                ]
            ),
            now_ts=123.0,
        )
        by_domain = {row["domain"]: row for row in metrics["domains"]}
        self.assertEqual(metrics["blocked_count"], 1)
        self.assertEqual(by_domain["personal-alex.ru"]["state"], "blocked")
        self.assertEqual(by_domain["alex-personal.ru"]["state"], "eligible")
        self.assertEqual(by_domain["personal-alex.ru"]["stat_date"], "2026-08-18")

    def test_postmaster_failure_is_visible_as_unavailable(self) -> None:
        class BrokenDB:
            def fetch_rows(self, _query, limit=5000):
                raise RuntimeError("table does not exist")

        metrics = _mailru_postmaster_metrics(BrokenDB(), now_ts=123.0)
        self.assertFalse(metrics["available"])
        self.assertIn("does not exist", metrics["error"])


if __name__ == "__main__":
    unittest.main()
