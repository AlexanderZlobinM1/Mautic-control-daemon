from __future__ import annotations

import unittest

from mcd_agent.config import (
    _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
    _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
    _campaign_rebuilds_due_sql,
    _campaign_triggers_due_sql,
)


class CampaignDueSqlTests(unittest.TestCase):
    def test_trigger_due_catches_date_events_after_publish_down(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]
        self.assertIn("el.date_triggered IS NULL", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertNotIn("el.trigger_date >= '{window_start_utc_7d}'", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_local}'", event_log_branch)
        self.assertNotIn("el.trigger_date >= '{window_start_local_7d}'", event_log_branch)
        self.assertNotIn("c.publish_down", event_log_branch)


    def test_trigger_due_keeps_old_pending_event_logs_visible(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]

        self.assertIn("el.date_triggered IS NULL", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_local}'", event_log_branch)
        self.assertNotIn("window_start_utc_7d", event_log_branch)
        self.assertNotIn("window_start_local_7d", event_log_branch)

    def test_trigger_sql_with_event_log_lower_bound_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "el.trigger_date <= '{now_utc}'         OR el.trigger_date <= '{now_local}'",
            "(el.trigger_date >= '{window_start_utc_7d}' AND el.trigger_date <= '{now_utc}')         OR (el.trigger_date >= '{window_start_local_7d}' AND el.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_due_does_not_require_is_scheduled_for_date_events(self) -> None:
        date_due_pos = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.index("el.trigger_date <= '{now_utc}'")
        scheduled_pos = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.index("el.is_scheduled = 1")

        self.assertLess(date_due_pos, scheduled_pos)
        self.assertIn("OR (el.is_scheduled = 1 AND el.trigger_date IS NULL)", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)

    def test_campaign_due_sql_avoids_deleted_column_for_mautic4(self) -> None:
        self.assertNotIn("c.deleted", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("c.deleted", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)

    def test_trigger_due_catches_root_action_campaign_leads_without_event_log(self) -> None:
        self.assertIn("ce.parent_id IS NULL", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode IN ('immediate', 'interval')", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("el0.rotation <=> cld.rotation", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("el0.event_id = ce.id", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        root_bootstrap_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[1]
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_local}'", root_bootstrap_branch)

    def test_trigger_due_is_strictly_event_log_driven(self) -> None:
        # Scheduled execution is event-log driven, but root actions also need a
        # bootstrap path: Mautic only creates the event log when the campaign is
        # triggered for contacts that already exist in campaign_leads.
        self.assertIn("FROM {prefix}campaign_lead_event_log el", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("FROM {prefix}campaign_events ce", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode IN ('immediate', 'interval')", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)

    def test_rebuild_due_has_publish_down_free_date_action_catchup(self) -> None:
        date_action_branch = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.split("UNION", 2)[1]

        self.assertIn("ce.trigger_mode = 'date'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", date_action_branch)
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_local}'", date_action_branch)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", date_action_branch)
        self.assertNotIn("c.publish_down", date_action_branch)


    def test_campaign_due_has_no_age_lower_bound_for_campaign_events(self) -> None:
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)

    def test_long_running_campaigns_do_not_expire_from_trigger_ring(self) -> None:
        """Welcome/abandoned-cart style campaigns can run for months or years."""
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE

        self.assertIn("el.date_triggered IS NULL", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_local}'", event_log_branch)
        self.assertNotIn("window_start_utc_7d", event_log_branch)
        self.assertNotIn("window_start_local_7d", event_log_branch)

    def test_long_running_campaigns_do_not_expire_from_rebuild_ring(self) -> None:
        """A stale/missing event log in an old active campaign must still seed rebuild."""
        date_action_branch = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.split("UNION", 2)[1]

        self.assertIn("cld.date_last_exited IS NULL", date_action_branch)
        self.assertIn("ce.parent_id IS NOT NULL", date_action_branch)
        self.assertIn("ce.trigger_mode = 'date'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_local}'", date_action_branch)
        self.assertNotIn("window_start_utc_7d", date_action_branch)
        self.assertNotIn("window_start_local_7d", date_action_branch)

    def test_trigger_sql_with_campaign_event_lower_bound_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "ce.trigger_date <= '{now_utc}'             OR ce.trigger_date <= '{now_local}'",
            "(ce.trigger_date >= '{window_start_utc_7d}' AND ce.trigger_date <= '{now_utc}')             OR (ce.trigger_date >= '{window_start_local_7d}' AND ce.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_rebuild_sql_with_campaign_event_lower_bound_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.replace(
            "ce.trigger_date <= '{now_utc}'         OR ce.trigger_date <= '{now_local}'",
            "(ce.trigger_date >= '{window_start_utc_7d}' AND ce.trigger_date <= '{now_utc}')         OR (ce.trigger_date >= '{window_start_local_7d}' AND ce.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )

    def test_rebuild_due_does_not_loop_on_missing_root_action_logs(self) -> None:
        self.assertIn("ce.parent_id IS NOT NULL", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)
        self.assertIn("cld.date_last_exited IS NULL", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)
        self.assertNotIn("el4.event_id = ce.id", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)

    def test_legacy_explicit_trigger_sql_is_migrated(self) -> None:
        legacy = (
            "SELECT DISTINCT q.id FROM ( SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) "
            "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
            "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el "
            "WHERE el.campaign_id = c.id AND el.is_scheduled = 1 "
            "AND el.trigger_date <= '{now_utc}' LIMIT 1 ) UNION SELECT c.id "
            "FROM {prefix}campaigns c INNER JOIN {prefix}campaign_leads cl "
            "ON cl.campaign_id = c.id AND cl.date_added >= '{window_start_local_24h}' "
            "WHERE NOT EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el2 "
            "WHERE el2.campaign_id = cl.campaign_id AND el2.rotation <=> cl.rotation LIMIT 1 ) ) q"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": legacy}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_previous_trigger_only_sql_is_migrated(self) -> None:
        previous = (
            "SELECT DISTINCT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) "
            "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
            "AND EXISTS (SELECT 1 FROM {prefix}campaign_lead_event_log el "
            "WHERE el.campaign_id = c.id AND el.date_triggered IS NULL "
            "AND ((el.trigger_date IS NOT NULL AND el.trigger_date >= '{window_start_utc_7d}' "
            "AND el.trigger_date <= '{now_utc}') OR (el.is_scheduled = 1 AND el.trigger_date IS NULL)) "
            "LIMIT 1) ORDER BY c.id"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaigns_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_without_root_action_branch_is_migrated(self) -> None:
        previous = (
            "SELECT DISTINCT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) "
            "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el "
            "WHERE el.campaign_id = c.id AND el.date_triggered IS NULL "
            "AND ((el.trigger_date IS NOT NULL AND ("
            "(el.trigger_date >= '{window_start_utc_7d}' AND el.trigger_date <= '{now_utc}') "
            "OR (el.trigger_date >= '{window_start_local_7d}' AND el.trigger_date <= '{now_local}'))) "
            "OR (el.is_scheduled = 1 AND el.trigger_date IS NULL)) LIMIT 1) ORDER BY c.id"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_with_old_root_action_date_semantics_is_migrated(self) -> None:
        previous = (
            "SELECT DISTINCT q.id FROM ( SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND EXISTS ( SELECT 1 "
            "FROM {prefix}campaign_lead_event_log el WHERE el.campaign_id = c.id "
            "AND el.date_triggered IS NULL AND el.trigger_date <= '{now_utc}' "
            "AND el.is_scheduled = 1 LIMIT 1 ) UNION SELECT c.id "
            "FROM {prefix}campaigns c WHERE c.is_published = 1 "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_events ce "
            "INNER JOIN {prefix}campaign_leads cld ON cld.campaign_id = c.id "
            "WHERE ce.campaign_id = c.id AND ce.event_type = 'action' "
            "AND ce.parent_id IS NULL AND (ce.trigger_mode IN ('immediate', 'interval') "
            "OR ce.trigger_mode IS NULL OR (ce.trigger_mode = 'date' "
            "AND ce.trigger_date IS NOT NULL AND ce.trigger_date <= '{now_utc}')) "
            "AND NOT EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el0 "
            "WHERE el0.campaign_id = cld.campaign_id AND el0.lead_id = cld.lead_id "
            "AND el0.event_id = ce.id AND el0.rotation <=> cld.rotation LIMIT 1 ) LIMIT 1 ) ) q"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_with_root_action_bootstrap_branch_is_migrated(self) -> None:
        previous = (
            "SELECT DISTINCT q.id FROM ( SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND EXISTS ( SELECT 1 "
            "FROM {prefix}campaign_lead_event_log el WHERE el.campaign_id = c.id "
            "AND el.date_triggered IS NULL AND el.trigger_date <= '{now_utc}' LIMIT 1 ) "
            "UNION SELECT c.id FROM {prefix}campaigns c WHERE c.is_published = 1 "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_events ce "
            "INNER JOIN {prefix}campaign_leads cld ON cld.campaign_id = c.id "
            "WHERE ce.campaign_id = c.id AND ce.event_type = 'action' "
            "AND ce.parent_id IS NULL AND NOT EXISTS ( SELECT 1 "
            "FROM {prefix}campaign_lead_event_log el0 "
            "WHERE el0.campaign_id = cld.campaign_id "
            "AND el0.lead_id = cld.lead_id "
            "AND el0.rotation <=> cld.rotation LIMIT 1 ) LIMIT 1 ) ) q"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_with_deleted_column_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "WHERE c.is_published = 1 ",
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) ",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_with_event_specific_root_action_log_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "AND el0.rotation <=> cld.rotation ",
            "AND el0.event_id = ce.id AND el0.rotation <=> cld.rotation ",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_legacy_explicit_rebuild_sql_is_migrated(self) -> None:
        legacy = (
            "SELECT DISTINCT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 "
            "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_leadlist_xref cx0 "
            "WHERE cx0.campaign_id = c.id LIMIT 1 ) "
            "OR EXISTS ( SELECT 1 FROM {prefix}campaign_events ce "
            "INNER JOIN {prefix}campaign_leads cld ON cld.campaign_id = c.id "
            "WHERE ce.campaign_id = c.id AND ce.trigger_mode = 'date' "
            "AND ce.trigger_date <= '{now_utc}' AND NOT EXISTS ( "
            "SELECT 1 FROM {prefix}campaign_lead_event_log el3 "
            "WHERE el3.campaign_id = cld.campaign_id "
            "AND el3.rotation <=> cld.rotation LIMIT 1 ) LIMIT 1 )"
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": legacy}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )

    def test_previous_rebuild_sql_without_root_action_seed_is_migrated(self) -> None:
        previous = (
            "SELECT DISTINCT q.id FROM ( SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) "
            "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_events ce "
            "INNER JOIN {prefix}campaign_leads cld ON cld.campaign_id = c.id "
            "WHERE ce.campaign_id = c.id AND ce.event_type = 'action' "
            "AND ce.trigger_mode = 'date' AND ce.trigger_date <= '{now_utc}' "
            "AND NOT EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el3 "
            "WHERE el3.campaign_id = cld.campaign_id AND el3.rotation <=> cld.rotation LIMIT 1 ) "
            "LIMIT 1 ) ) q ORDER BY q.id"
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )

    def test_rebuild_sql_with_deleted_column_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.replace(
            "WHERE c.is_published = 1 ",
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) ",
            1,
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )


if __name__ == "__main__":
    unittest.main()
