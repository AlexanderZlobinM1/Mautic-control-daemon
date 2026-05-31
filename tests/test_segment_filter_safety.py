from __future__ import annotations

import unittest

from mcd_agent.segment_filter_safety import format_segment_filter_issues, segment_invalid_filter_issues


class SegmentFilterSafetyTests(unittest.TestCase):
    def test_detects_bare_relative_date_expression_without_unit(self) -> None:
        filters = (
            'a:1:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:20:"user_newsletter_date";s:4:"type";s:4:"date";'
            's:8:"operator";s:3:"gte";s:10:"properties";a:1:{s:6:"filter";s:8:"today -1";}'
            's:6:"filter";s:10:"2026-05-20";s:7:"display";N;}}'
        )

        issues = segment_invalid_filter_issues([{"id": 191, "filters": filters}])

        self.assertEqual(set(issues), {191})
        self.assertEqual(issues[191][0].field, "user_newsletter_date")
        self.assertEqual(issues[191][0].value, "today -1")
        self.assertIn("191:user_newsletter_date=today -1", format_segment_filter_issues(issues))

    def test_allows_absolute_date_value(self) -> None:
        filters = (
            'a:1:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:20:"user_newsletter_date";s:4:"type";s:4:"date";'
            's:8:"operator";s:3:"gte";s:10:"properties";a:1:{s:6:"filter";s:10:"2026-05-20";}'
            's:6:"filter";s:10:"2026-05-20";s:7:"display";N;}}'
        )

        self.assertEqual(segment_invalid_filter_issues([{"id": 191, "filters": filters}]), {})


if __name__ == "__main__":
    unittest.main()
