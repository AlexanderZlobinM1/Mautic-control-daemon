from datetime import datetime
import unittest

from mcd_agent.cleanup_schedule import (
    cleanup_session_key,
    cron_expr_window_due,
    in_hhmm_window,
    select_fair_cleanup_task,
    window_minutes,
)


class EmptyLeadsCleanupWindowTests(unittest.TestCase):
    def test_nightly_window_crosses_midnight(self) -> None:
        self.assertEqual(window_minutes("22:00", "09:00"), 660)
        self.assertTrue(in_hhmm_window(datetime(2026, 5, 15, 22, 0), "22:00", "09:00"))
        self.assertTrue(in_hhmm_window(datetime(2026, 5, 16, 8, 59), "22:00", "09:00"))
        self.assertFalse(in_hhmm_window(datetime(2026, 5, 16, 9, 0), "22:00", "09:00"))
        self.assertFalse(in_hhmm_window(datetime(2026, 5, 15, 21, 59), "22:00", "09:00"))

    def test_nightly_window_respects_start_minutes(self) -> None:
        self.assertEqual(window_minutes("22:30", "09:00"), 630)
        self.assertFalse(in_hhmm_window(datetime(2026, 5, 15, 22, 29), "22:30", "09:00"))
        self.assertTrue(in_hhmm_window(datetime(2026, 5, 15, 22, 30), "22:30", "09:00"))

    def test_cron_start_opens_window_until_end(self) -> None:
        self.assertTrue(cron_expr_window_due("0 22 * * *", datetime(2026, 5, 15, 22, 0), 660))
        self.assertTrue(cron_expr_window_due("0 22 * * *", datetime(2026, 5, 16, 8, 59), 660))
        self.assertFalse(cron_expr_window_due("0 22 * * *", datetime(2026, 5, 16, 9, 0), 660))

    def test_cleanup_session_key_interval_resets_by_interval_slot(self) -> None:
        active, key = cleanup_session_key(
            schedule_type="interval",
            now_local=datetime(2026, 5, 15, 12, 0),
            now_epoch=1800.0,
            interval_sec=900,
            cron_expr="",
            window_min=660,
            window_start="22:00",
            window_end="09:00",
        )
        self.assertTrue(active)
        self.assertEqual(key, "interval:2")

    def test_cleanup_session_key_cron_uses_cron_occurrence(self) -> None:
        active, key = cleanup_session_key(
            schedule_type="cron",
            now_local=datetime(2026, 5, 16, 8, 59),
            now_epoch=0.0,
            interval_sec=900,
            cron_expr="0 22 * * *",
            window_min=660,
            window_start="22:00",
            window_end="09:00",
        )
        self.assertTrue(active)
        self.assertEqual(key, "2026-05-15T22:00")

    def test_cleanup_session_key_nightly_uses_window_start(self) -> None:
        active, key = cleanup_session_key(
            schedule_type="nightly_window",
            now_local=datetime(2026, 5, 16, 8, 59),
            now_epoch=0.0,
            interval_sec=900,
            cron_expr="",
            window_min=660,
            window_start="22:00",
            window_end="09:00",
        )
        self.assertTrue(active)
        self.assertEqual(key, "2026-05-15T22:00")

    def test_select_fair_cleanup_task_round_robins_due_tasks(self) -> None:
        tasks = ["empty_leads_cleanup", "page_hits_orphan_cleanup"]
        selected, cursor = select_fair_cleanup_task(tasks, -1)
        self.assertEqual(selected, "empty_leads_cleanup")
        selected, cursor = select_fair_cleanup_task(tasks, cursor)
        self.assertEqual(selected, "page_hits_orphan_cleanup")
        selected, cursor = select_fair_cleanup_task(["page_hits_orphan_cleanup"], cursor)
        self.assertEqual(selected, "page_hits_orphan_cleanup")


if __name__ == "__main__":
    unittest.main()
