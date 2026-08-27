from __future__ import annotations

import unittest

from mcd_agent.config import (
    _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
    _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
    _DEFAULT_SQL_IMPORT_PENDING_COUNT,
    _campaign_rebuilds_due_sql,
    _campaign_triggers_due_sql,
    _import_pending_count_sql,
)
from mcd_agent.daemon import (
    _SQL_CAMPAIGNS_ALL_PUBLISHED,
    _campaign_trigger_event_log_due_exists_sql,
    _campaign_trigger_no_action_due_exists_sql,
    _campaign_trigger_root_action_due_exists_sql,
)


class CampaignDueSqlTests(unittest.TestCase):
    def test_campaign_audit_ring_uses_mautic_utc_publish_window(self) -> None:
        # Regression: at 14:14 UTC / 16:14 local, a campaign with publish_up
        # 14:20 must not enter the audit ring before native Mautic sees it.
        self.assertIn("c.publish_up <= '{now_utc}'", _SQL_CAMPAIGNS_ALL_PUBLISHED)
        self.assertIn("c.publish_down >= '{now_utc}'", _SQL_CAMPAIGNS_ALL_PUBLISHED)
        self.assertNotIn("{now_local}", _SQL_CAMPAIGNS_ALL_PUBLISHED)

    def test_import_pending_sql_only_counts_launchable_statuses(self) -> None:
        self.assertIn("status IN (1,7)", _DEFAULT_SQL_IMPORT_PENDING_COUNT)
        self.assertIn("'queued','pending','delayed'", _DEFAULT_SQL_IMPORT_PENDING_COUNT)
        self.assertIn("< line_count", _DEFAULT_SQL_IMPORT_PENDING_COUNT)
        self.assertNotIn("status IN (1,2,7)", _DEFAULT_SQL_IMPORT_PENDING_COUNT)
        self.assertNotIn("in_progress", _DEFAULT_SQL_IMPORT_PENDING_COUNT)
        self.assertNotIn("<= line_count", _DEFAULT_SQL_IMPORT_PENDING_COUNT)

    def test_legacy_import_pending_sql_with_in_progress_is_migrated(self) -> None:
        previous = (
            "SELECT COUNT(*) AS cnt FROM {prefix}imports "
            "WHERE is_published = 1 "
            "AND (status IN (1,2,7) "
            "OR CAST(status AS CHAR) IN ('pending','in_progress','delayed'))"
        )

        self.assertEqual(
            _import_pending_count_sql({"import_pending_count": previous}),
            _DEFAULT_SQL_IMPORT_PENDING_COUNT,
        )

    def test_legacy_import_pending_sql_with_inclusive_final_line_is_migrated(self) -> None:
        previous = (
            "SELECT COUNT(*) AS cnt FROM {prefix}imports "
            "WHERE is_published = 1 "
            "AND (status IN (1,7) "
            "OR LOWER(CAST(status AS CHAR)) IN ('queued','pending','delayed')) "
            "AND (date_started IS NULL "
            "OR CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(properties, '$.line')), '1') AS UNSIGNED) <= line_count)"
        )

        self.assertEqual(
            _import_pending_count_sql({"import_pending_count": previous}),
            _DEFAULT_SQL_IMPORT_PENDING_COUNT,
        )

    def test_trigger_due_respects_publish_down_for_date_events(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]
        self.assertIn("el.is_scheduled = 1", event_log_branch)
        self.assertNotIn("el.date_triggered", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertNotIn("{now_local}", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("el.trigger_date >= '{window_start_utc_7d}'", event_log_branch)
        self.assertNotIn("el.trigger_date >= '{window_start_local_7d}'", event_log_branch)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", event_log_branch)

    def test_trigger_due_keeps_old_pending_event_logs_visible(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]

        self.assertIn("el.is_scheduled = 1", event_log_branch)
        self.assertNotIn("el.date_triggered", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertNotIn("{now_local}", event_log_branch)
        self.assertNotIn("window_start_utc_7d", event_log_branch)
        self.assertNotIn("window_start_local_7d", event_log_branch)

    def test_trigger_sql_with_event_log_lower_bound_is_migrated(self) -> None:
        previous = (
            "SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND EXISTS ("
            "SELECT 1 FROM {prefix}campaign_lead_event_log el "
            "WHERE el.campaign_id = c.id AND el.date_triggered IS NULL "
            "AND ((el.trigger_date >= '{window_start_utc_7d}' AND el.trigger_date <= '{now_utc}') "
            "OR (el.trigger_date >= '{window_start_local_7d}' AND el.trigger_date <= '{now_local}')) "
            "LIMIT 1)"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_with_dual_clock_event_log_semantics_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "el.trigger_date <= '{now_utc}'",
            "(el.trigger_date <= '{now_utc}' OR el.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_sql_without_event_log_publish_down_guard_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') ",
            "",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_trigger_due_does_not_require_is_scheduled_for_date_events(self) -> None:
        due_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("AND (", 1)[1]
        self.assertIn("el.trigger_date IS NOT NULL AND el.trigger_date <= '{now_utc}'", due_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", due_branch)
        self.assertIn("OR el.trigger_date IS NULL", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)

    def test_trigger_due_catches_mautic_prescheduled_rows(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]

        self.assertIn("el.is_scheduled = 1", event_log_branch)
        self.assertIn("el.trigger_date IS NOT NULL", event_log_branch)
        self.assertNotIn("el.date_triggered", event_log_branch)

    def test_trigger_due_keeps_scheduled_rows_due_after_attempt_timestamp(self) -> None:
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]

        self.assertIn("el.is_scheduled = 1", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertNotIn("date_triggered IS NULL", event_log_branch)
        self.assertNotIn("date_triggered < el.trigger_date", event_log_branch)

    def test_campaign_due_sql_avoids_deleted_column_for_mautic4(self) -> None:
        self.assertNotIn("c.deleted", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("c.deleted", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)

    def test_trigger_due_catches_root_action_campaign_leads_without_event_log(self) -> None:
        self.assertIn("ce.parent_id IS NULL", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.event_type IN ('action', 'condition')", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode IN ('immediate', 'interval')", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("el0.rotation <=> cld.rotation", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("el0.event_id = ce.id", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        root_bootstrap_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[1]
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", root_bootstrap_branch)

    def test_trigger_due_catches_due_root_date_action_without_event_log(self) -> None:
        # Regression: Neodent campaign 315 had existing campaign_leads and a
        # root Viber date action, so native Mautic trigger could execute it
        # even before an event-log row existed.
        root_bootstrap_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.rsplit("UNION", 1)[1]

        self.assertIn("ce.parent_id IS NULL", root_bootstrap_branch)
        self.assertIn("ce.trigger_mode = 'date'", root_bootstrap_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", root_bootstrap_branch)
        self.assertIn("NOT EXISTS (", root_bootstrap_branch)
        self.assertIn("{prefix}campaign_lead_event_log el0", root_bootstrap_branch)

    def test_trigger_due_catches_root_condition_campaign_leads_without_event_log(self) -> None:
        root_bootstrap_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[1]

        self.assertIn("ce.event_type IN ('action', 'condition')", root_bootstrap_branch)
        self.assertIn("ce.parent_id IS NULL", root_bootstrap_branch)
        self.assertIn("INNER JOIN {prefix}campaign_leads cld", root_bootstrap_branch)
        self.assertIn("cld.date_last_exited IS NULL", root_bootstrap_branch)
        self.assertIn("el0.rotation <=> cld.rotation", root_bootstrap_branch)
        self.assertNotIn("el0.event_id = ce.id", root_bootstrap_branch)

    def test_trigger_due_guard_catches_root_condition_campaign_leads_without_event_log(self) -> None:
        guard_sql = _campaign_trigger_root_action_due_exists_sql(162)

        self.assertIn("ce.event_type IN ('action', 'condition')", guard_sql)
        self.assertIn("ce.parent_id IS NULL", guard_sql)
        self.assertIn("INNER JOIN {prefix}campaign_leads cld", guard_sql)
        self.assertIn("el0.rotation <=> cld.rotation", guard_sql)
        self.assertNotIn("el0.event_id = ce.id", guard_sql)

    def test_trigger_due_catches_decision_no_path_without_event_log(self) -> None:
        self.assertIn("d.event_type = 'decision'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.decision_path = 'no'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("parent_log.event_id = d.parent_id", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("dlog.event_id = d.id", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode = 'date'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_date <= '{now_utc}'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("{now_local}", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode = 'interval'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("DATE_ADD(parent_log.date_triggered", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)

    def test_trigger_due_guard_catches_decision_no_path_without_event_log(self) -> None:
        guard_sql = _campaign_trigger_no_action_due_exists_sql(162)

        self.assertIn("d.event_type = 'decision'", guard_sql)
        self.assertIn("ce.decision_path = 'no'", guard_sql)
        self.assertIn("parent_log.event_id = d.parent_id", guard_sql)
        self.assertIn("dlog.event_id = d.id", guard_sql)
        self.assertIn("dlog.rotation <=> parent_log.rotation", guard_sql)
        self.assertIn("<= '{now_utc}'", guard_sql)
        self.assertNotIn("{now_local}", guard_sql)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", guard_sql)

    def test_trigger_due_event_log_guard_respects_publish_down(self) -> None:
        guard_sql = _campaign_trigger_event_log_due_exists_sql(162)

        self.assertIn("FROM {prefix}campaign_lead_event_log el", guard_sql)
        self.assertIn("el.trigger_date <= '{now_event_log}'", guard_sql)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", guard_sql)

    def test_trigger_due_is_strictly_event_log_driven(self) -> None:
        # Scheduled execution is event-log driven, but root actions also need a
        # bootstrap path: Mautic only creates the event log when the campaign is
        # triggered for contacts that already exist in campaign_leads. Root
        # conditions need the same bootstrap so Mautic can evaluate the branch
        # and execute downstream channel actions.
        self.assertIn("FROM {prefix}campaign_lead_event_log el", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("FROM {prefix}campaign_events ce", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertIn("ce.trigger_mode IN ('immediate', 'interval')", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)

    def test_rebuild_due_date_action_catchup_respects_publish_down(self) -> None:
        date_action_branch = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.split("UNION", 2)[1]

        self.assertIn("ce.trigger_mode = 'date'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", date_action_branch)
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", date_action_branch)
        self.assertNotIn("{now_local}", date_action_branch)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", date_action_branch)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", date_action_branch)

    def test_campaign_due_has_no_age_lower_bound_for_campaign_events(self) -> None:
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_utc_7d}'", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)
        self.assertNotIn("ce.trigger_date >= '{window_start_local_7d}'", _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE)

    def test_long_running_campaigns_do_not_expire_from_trigger_ring(self) -> None:
        """Welcome/abandoned-cart style campaigns can run for months or years."""
        event_log_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE

        self.assertIn("el.is_scheduled = 1", event_log_branch)
        self.assertNotIn("el.date_triggered IS NULL", event_log_branch)
        self.assertNotIn("el.date_triggered < el.trigger_date", event_log_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", event_log_branch)
        self.assertNotIn("window_start_utc_7d", event_log_branch)
        self.assertNotIn("window_start_local_7d", event_log_branch)

    def test_long_running_campaigns_do_not_expire_from_rebuild_ring(self) -> None:
        """A stale/missing event log in an old active campaign must still seed rebuild."""
        date_action_branch = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.split("UNION", 2)[1]

        self.assertIn("cld.date_last_exited IS NULL", date_action_branch)
        self.assertIn("ce.parent_id IS NOT NULL", date_action_branch)
        self.assertIn("ce.trigger_mode = 'date'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", date_action_branch)
        self.assertNotIn("{now_local}", date_action_branch)
        self.assertNotIn("window_start_utc_7d", date_action_branch)
        self.assertNotIn("window_start_local_7d", date_action_branch)

    def test_trigger_sql_with_campaign_event_lower_bound_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.replace(
            "ce.trigger_date <= '{now_utc}'",
            "(ce.trigger_date >= '{window_start_utc_7d}' AND ce.trigger_date <= '{now_utc}')             OR (ce.trigger_date >= '{window_start_local_7d}' AND ce.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_rebuild_sql_with_campaign_event_lower_bound_is_migrated(self) -> None:
        previous = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.replace(
            "ce.trigger_date <= '{now_utc}'",
            "(ce.trigger_date >= '{window_start_utc_7d}' AND ce.trigger_date <= '{now_utc}')         OR (ce.trigger_date >= '{window_start_local_7d}' AND ce.trigger_date <= '{now_local}')",
            1,
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": previous}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )

    def test_rebuild_sql_without_date_action_publish_down_guard_is_migrated(self) -> None:
        guard = "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
        first_pos = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.index(guard)
        second_pos = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.index(guard, first_pos + len(guard))
        previous = (
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE[:second_pos]
            + _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE[second_pos + len(guard) :]
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
