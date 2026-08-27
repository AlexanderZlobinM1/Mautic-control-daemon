from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mcd_agent.sql_time import campaign_sql_time_context


class CampaignSqlTimeContextTests(unittest.TestCase):
    def test_uses_iana_timezone_without_hardcoded_offset(self) -> None:
        now_utc = datetime(2026, 5, 14, 6, 0, 0, tzinfo=timezone.utc)

        for tz_name in (
            "UTC",
            "Europe/Belgrade",
            "Europe/Moscow",
            "America/New_York",
            "Asia/Tokyo",
            "Australia/Sydney",
        ):
            with self.subTest(tz_name=tz_name):
                ctx = campaign_sql_time_context(now_utc, tz_name)
                expected_local = now_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")
                expected_event_log = (
                    now_utc.strftime("%Y-%m-%d %H:%M:%S") if tz_name == "UTC" else expected_local
                )

                self.assertEqual(ctx["now_utc"], "2026-05-14 06:00:00")
                self.assertEqual(ctx["now_local"], expected_local)
                self.assertEqual(ctx["now_event_log"], expected_event_log)

    def test_dst_transition_is_not_fixed_two_hour_shift(self) -> None:
        winter_utc = datetime(2026, 1, 14, 6, 0, 0, tzinfo=timezone.utc)
        summer_utc = datetime(2026, 7, 14, 6, 0, 0, tzinfo=timezone.utc)

        winter = campaign_sql_time_context(winter_utc, "Europe/Belgrade")
        summer = campaign_sql_time_context(summer_utc, "Europe/Belgrade")

        self.assertEqual(winter["now_local"], "2026-01-14 07:00:00")
        self.assertEqual(summer["now_local"], "2026-07-14 08:00:00")
        self.assertEqual(winter["now_utc"], "2026-01-14 06:00:00")
        self.assertEqual(summer["now_utc"], "2026-07-14 06:00:00")

    def test_modern_mautic_event_logs_use_utc_while_mautic_4_keeps_local_time(self) -> None:
        now_utc = datetime(2026, 8, 8, 7, 25, 6, tzinfo=timezone.utc)

        legacy = campaign_sql_time_context(now_utc, "Europe/Belgrade", mautic_major=4)
        modern = campaign_sql_time_context(now_utc, "Europe/Belgrade", mautic_major=6)

        self.assertEqual(legacy["now_event_log"], "2026-08-08 09:25:06")
        self.assertEqual(modern["now_event_log"], "2026-08-08 07:25:06")
        self.assertLess(modern["now_event_log"], "2026-08-08 07:25:54")

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        ctx = campaign_sql_time_context(datetime(2026, 5, 14, 6, 0, 0, tzinfo=timezone.utc), "Bad/Zone")

        self.assertEqual(ctx["now_utc"], "2026-05-14 06:00:00")
        self.assertEqual(ctx["now_local"], "2026-05-14 06:00:00")
        self.assertEqual(ctx["now_event_log"], "2026-05-14 06:00:00")


if __name__ == "__main__":
    unittest.main()
