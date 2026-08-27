from __future__ import annotations

import unittest

from mcd_agent.config import _DEFAULT_SQL_SEGMENTS_DUE, _segments_due_sql


class SegmentDueSqlTests(unittest.TestCase):
    def test_segments_are_due_when_any_contact_can_newly_match(self) -> None:
        self.assertIn("{prefix}leads changed_lead", _DEFAULT_SQL_SEGMENTS_DUE)
        self.assertIn("changed_lead.date_added > ll.last_built_date", _DEFAULT_SQL_SEGMENTS_DUE)
        self.assertIn("changed_lead.date_modified > ll.last_built_date", _DEFAULT_SQL_SEGMENTS_DUE)

    def test_dnc_segments_are_due_when_donotcontact_changes(self) -> None:
        self.assertIn("{prefix}lead_donotcontact dnc", _DEFAULT_SQL_SEGMENTS_DUE)
        self.assertIn("ll.filters LIKE '%dnc_%'", _DEFAULT_SQL_SEGMENTS_DUE)
        self.assertIn("dnc.date_added > ll.last_built_date", _DEFAULT_SQL_SEGMENTS_DUE)

    def test_previous_default_without_dnc_tracking_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_SEGMENTS_DUE.replace(
            "  OR EXISTS (    SELECT 1     FROM {prefix}lead_donotcontact dnc     WHERE ll.filters LIKE '%dnc_%'       AND (ll.last_built_date IS NULL OR dnc.date_added > ll.last_built_date)     LIMIT 1  )",
            "",
        )

        self.assertNotIn("lead_donotcontact", previous)
        self.assertEqual(_segments_due_sql({"segments_due": previous}), _DEFAULT_SQL_SEGMENTS_DUE)

    def test_previous_default_without_global_contact_tracking_is_migrated(self) -> None:
        start = _DEFAULT_SQL_SEGMENTS_DUE.index("  OR EXISTS (    SELECT 1     FROM {prefix}leads changed_lead")
        end = _DEFAULT_SQL_SEGMENTS_DUE.index("  OR EXISTS (", start + 1)
        previous = _DEFAULT_SQL_SEGMENTS_DUE[:start] + _DEFAULT_SQL_SEGMENTS_DUE[end:]

        self.assertNotIn("{prefix}leads changed_lead", previous)
        self.assertEqual(_segments_due_sql({"segments_due": previous}), _DEFAULT_SQL_SEGMENTS_DUE)

    def test_custom_segment_due_sql_is_preserved(self) -> None:
        custom = "SELECT id FROM {prefix}lead_lists WHERE is_published = 1 ORDER BY id DESC"
        self.assertEqual(_segments_due_sql({"segments_due": custom}), custom)


if __name__ == "__main__":
    unittest.main()
