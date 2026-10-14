from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import socket
import time
from typing import Any, cast
from urllib import request
from urllib.error import HTTPError, URLError
from mcd_agent.fs_permissions import default_guard_paths, normalize_guard_paths

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


CURRENT_CONFIG_SCHEMA_VERSION = 2
_CAMPAIGN_TRIGGER_AUDIT_INTERVAL_SEC = 60
DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN = 24 * 60
LEGACY_RUNTIME_KEYS: tuple[str, ...] = (
    "max_parallel_campaigns",
    "max_parallel_segments_idle",
    "max_parallel_segments_active",
    "max_parallel_segments_non_whitelist_active",
    "segment_non_whitelist_policy",
)

_DEFAULT_CLUSTER_FILE_NODE_PATHS = [
    "/etc/cron.d",
    "/etc/crontab",
    "/etc/fstab",
    "/etc/haproxy",
    "/etc/hostname",
    "/etc/hosts",
    "/etc/mcd",
    "/etc/mysql",
    "/etc/netplan",
    "/etc/nginx",
    "/etc/php",
    "/etc/systemd/system",
    "/etc/timezone",
    "/opt/mcd/etc",
    "/opt/mcd/var/state",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/var/lib/syncthing-mcd/config.xml",
    "/var/lib/syncthing-mcd/cert.pem",
    "/var/lib/syncthing-mcd/key.pem",
]

_DEFAULT_CLUSTER_FILE_SHARED_PATHS = [
    "/var/www/mautic",
]

_DEFAULT_CLUSTER_FILE_EXCLUDES = [
    "var/www/mautic/var/cache/***",
    "var/www/mautic/var/logs/***",
    "var/www/mautic/var/spool/***",
    "var/www/mautic/var/sessions/***",
    "var/www/mautic/app/cache/***",
    "var/www/mautic/app/logs/***",
    "*/var/www/mautic/var/cache/***",
    "*/var/www/mautic/var/logs/***",
    "*/var/www/mautic/var/spool/***",
    "*/var/www/mautic/var/sessions/***",
    "*/var/www/mautic/app/cache/***",
    "*/var/www/mautic/app/logs/***",
]

_LEGACY_SQL_SEGMENTS_DUE_DEFAULT = (
    "SELECT id FROM {prefix}lead_lists WHERE is_published = 1 ORDER BY id"
)
_LEGACY_SQL_SEGMENTS_DUE_DEFAULT_V0822 = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "AND ("
    "  ll.last_built_date IS NULL "
    "  OR ll.last_built_date < '{window_start_utc_24h}' "
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_lists_leads lll "
    "    WHERE lll.leadlist_id = ll.id "
    "      AND lll.manually_removed = 0 "
    "      AND (ll.last_built_date IS NULL OR lll.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_donotcontact dnc "
    "    WHERE ll.filters LIKE '%dnc_%' "
    "      AND (ll.last_built_date IS NULL OR dnc.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    ") "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)
_LEGACY_SQL_SEGMENTS_DUE_DEFAULT_V0192 = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "AND ("
    "  ll.last_built_date IS NULL "
    "  OR ll.last_built_date < '{window_start_utc_24h}' "
    "  OR COALESCE(ll.date_modified, ll.date_added) > COALESCE(ll.last_built_date, '1970-01-01 00:00:00') "
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_lists_leads lll "
    "    WHERE lll.leadlist_id = ll.id "
    "      AND (ll.last_built_date IS NULL OR lll.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_lists_leads lll "
    "    INNER JOIN {prefix}leads l ON l.id = lll.lead_id "
    "    WHERE lll.leadlist_id = ll.id "
    "      AND lll.manually_removed = 0 "
    "      AND ("
    "        ll.last_built_date IS NULL "
    "        OR COALESCE(l.date_modified, l.date_added) > ll.last_built_date"
    "      ) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_donotcontact dnc "
    "    WHERE ll.filters LIKE '%dnc_%' "
    "      AND (ll.last_built_date IS NULL OR dnc.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    ") "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)
_LEGACY_SQL_CAMPAIGNS_DUE_DEFAULT = (
    "SELECT c.id FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "AND (c.deleted IS NULL) "
    "ORDER BY c.id"
)
_LEGACY_SQL_CAMPAIGNS_DUE_DEFAULT_DESC = (
    "SELECT c.id FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "AND (c.deleted IS NULL) "
    "ORDER BY c.id DESC"
)
_LEGACY_SQL_CAMPAIGNS_DUE_NO_DELETED = (
    "SELECT c.id FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "ORDER BY c.id"
)
_LEGACY_SQL_CAMPAIGNS_DUE_NO_DELETED_DESC = (
    "SELECT c.id FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "ORDER BY c.id DESC"
)
_LEGACY_SQL_CAMPAIGNS_DUE_PUBLISHED_WINDOW = (
    "SELECT c.id FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "AND (c.deleted IS NULL) "
    "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
    "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
    "ORDER BY c.id"
)
_DEFAULT_SQL_SEGMENTS_DUE = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "AND ("
    "  ll.last_built_date IS NULL "
    "  OR ll.last_built_date < '{window_start_utc_24h}' "
    "  OR COALESCE(ll.date_modified, ll.date_added) > COALESCE(ll.last_built_date, '1970-01-01 00:00:00') "
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}audit_log al "
    "    WHERE al.bundle = 'lead' "
    "      AND al.object = 'segment' "
    "      AND al.object_id = ll.id "
    "      AND al.action IN ('create', 'update') "
    "      AND (ll.last_built_date IS NULL OR al.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}leads changed_lead "
    "    WHERE ll.last_built_date IS NULL "
    "      OR changed_lead.date_added > ll.last_built_date "
    "      OR changed_lead.date_modified > ll.last_built_date "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_lists_leads lll "
    "    WHERE lll.leadlist_id = ll.id "
    "      AND (ll.last_built_date IS NULL OR lll.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_lists_leads lll "
    "    INNER JOIN {prefix}leads l ON l.id = lll.lead_id "
    "    WHERE lll.leadlist_id = ll.id "
    "      AND lll.manually_removed = 0 "
    "      AND ("
    "        ll.last_built_date IS NULL "
    "        OR COALESCE(l.date_modified, l.date_added) > ll.last_built_date"
    "      ) "
    "    LIMIT 1"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}lead_donotcontact dnc "
    "    WHERE ll.filters LIKE '%dnc_%' "
    "      AND (ll.last_built_date IS NULL OR dnc.date_added > ll.last_built_date) "
    "    LIMIT 1"
    "  )"
    ") "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)
_DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE = (
    "SELECT DISTINCT q.id "
    "FROM ("
    "SELECT c.id "
    "FROM {prefix}campaigns c "
    "  WHERE c.is_published = 1 "
    "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
    "  AND EXISTS ("
    "  SELECT 1 "
    "  FROM {prefix}campaign_lead_event_log el "
    "  WHERE el.campaign_id = c.id "
    "    AND el.is_scheduled = 1 "
    "    AND ("
    "      (el.trigger_date IS NOT NULL AND el.trigger_date <= '{now_utc}') "
    "      OR el.trigger_date IS NULL"
    "    ) "
    "  LIMIT 1"
    "  )"
    "UNION "
    "SELECT c.id "
    "FROM {prefix}campaigns c "
    "  WHERE c.is_published = 1 "
    "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
    "  AND EXISTS ("
    "  SELECT 1 "
    "  FROM {prefix}campaign_events d "
    "  INNER JOIN {prefix}campaign_events ce "
    "    ON ce.parent_id = d.id "
    "   AND ce.decision_path = 'no' "
    "   AND ce.event_type IN ('action', 'condition') "
    "  INNER JOIN {prefix}campaign_lead_event_log parent_log "
    "    ON parent_log.campaign_id = c.id "
    "   AND parent_log.event_id = d.parent_id "
    "   AND parent_log.date_triggered IS NOT NULL "
    "  INNER JOIN {prefix}campaign_leads cld "
    "    ON cld.campaign_id = parent_log.campaign_id "
    "   AND cld.lead_id = parent_log.lead_id "
    "   AND cld.rotation <=> parent_log.rotation "
    "   AND cld.manually_removed = 0 "
    "   AND cld.date_last_exited IS NULL "
    "  WHERE d.campaign_id = c.id "
    "    AND d.event_type = 'decision' "
    "    AND d.parent_id IS NOT NULL "
    "    AND ("
    "      ce.trigger_mode = 'immediate' "
    "      OR ce.trigger_mode IS NULL "
    "      OR ("
    "        ce.trigger_mode = 'date' "
    "        AND (ce.trigger_date IS NULL OR ce.trigger_date <= '{now_utc}')"
    "      ) "
    "      OR ("
    "        ce.trigger_mode = 'interval' "
    "        AND parent_log.date_triggered IS NOT NULL "
    "        AND (CASE LOWER(COALESCE(ce.trigger_interval_unit, 'd')) "
    "          WHEN 'i' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) MINUTE) "
    "          WHEN 'h' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) HOUR) "
    "          WHEN 'd' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) DAY) "
    "          WHEN 'm' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) MONTH) "
    "          WHEN 'y' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) YEAR) "
    "          ELSE DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) DAY) "
    "        END) <= '{now_utc}'"
    "      )"
    "    ) "
    "    AND NOT EXISTS ("
    "      SELECT 1 "
    "      FROM {prefix}campaign_lead_event_log dlog "
    "      WHERE dlog.campaign_id = parent_log.campaign_id "
    "        AND dlog.event_id = d.id "
    "        AND dlog.lead_id = parent_log.lead_id "
    "        AND dlog.rotation <=> parent_log.rotation "
    "      LIMIT 1"
    "    ) "
    "  LIMIT 1"
    "  )"
    "UNION "
    "SELECT c.id "
    "FROM {prefix}campaigns c "
    "  WHERE c.is_published = 1 "
    "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
    "  AND EXISTS ("
    "  SELECT 1 "
    "  FROM {prefix}campaign_events ce "
    "  INNER JOIN {prefix}campaign_leads cld "
    "    ON cld.campaign_id = c.id "
    "   AND cld.manually_removed = 0 "
    "   AND cld.date_last_exited IS NULL "
    "  WHERE ce.campaign_id = c.id "
    "    AND ce.event_type IN ('action', 'condition') "
    "    AND ce.parent_id IS NULL "
    "    AND ("
    "      ce.trigger_mode IN ('immediate', 'interval') "
    "      OR ce.trigger_mode IS NULL "
    "      OR ("
    "        ce.trigger_mode = 'date' "
    "        AND (ce.trigger_date IS NULL OR ce.trigger_date <= '{now_utc}')"
    "      )"
    "    ) "
    "    AND NOT EXISTS ("
    "      SELECT 1 "
    "      FROM {prefix}campaign_lead_event_log el0 "
    "      WHERE el0.campaign_id = cld.campaign_id "
    "        AND el0.lead_id = cld.lead_id "
    "        AND el0.rotation <=> cld.rotation "
    "      LIMIT 1"
    "    ) "
    "  LIMIT 1"
    "  )"
    ") q "
    "ORDER BY q.id"
)
_DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE = (
    "SELECT DISTINCT q.id "
    "FROM ("
    "  SELECT c.id "
    "  FROM {prefix}campaigns c "
    "  WHERE c.is_published = 1 "
    "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
    "  AND EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}campaign_leadlist_xref cx0 "
    "    WHERE cx0.campaign_id = c.id "
    "    LIMIT 1"
    "  ) "
    "  AND ("
    "    EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}campaign_leadlist_xref cx "
    "    INNER JOIN {prefix}lead_lists_leads lll "
    "      ON lll.leadlist_id = cx.leadlist_id "
    "     AND lll.manually_removed = 0 "
    "    LEFT JOIN {prefix}campaign_leads cl "
    "      ON cl.campaign_id = c.id "
    "     AND cl.lead_id = lll.lead_id "
    "    WHERE cx.campaign_id = c.id "
    "      AND cl.lead_id IS NULL "
    "    LIMIT 1"
    "    ) "
    "    OR EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}campaign_leads cl "
    "    WHERE cl.campaign_id = c.id "
    "      AND cl.manually_removed = 0 "
    "      AND cl.date_last_exited IS NULL "
    "      AND NOT EXISTS ("
    "        SELECT 1 "
    "        FROM {prefix}campaign_leadlist_xref cx2 "
    "        INNER JOIN {prefix}lead_lists_leads lll2 "
    "          ON lll2.leadlist_id = cx2.leadlist_id "
    "         AND lll2.lead_id = cl.lead_id "
    "         AND lll2.manually_removed = 0 "
    "        WHERE cx2.campaign_id = c.id "
    "        LIMIT 1"
    "      ) "
    "    LIMIT 1"
    "    )"
    "  ) "
    "  UNION "
    "  SELECT c.id "
    "  FROM {prefix}campaigns c "
    "  WHERE c.is_published = 1 "
    "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
    "  AND EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}campaign_leadlist_xref cx0 "
    "    WHERE cx0.campaign_id = c.id "
    "    LIMIT 1"
    "  ) "
    "  AND EXISTS ("
    "    SELECT 1 "
    "    FROM {prefix}campaign_events ce "
    "    INNER JOIN {prefix}campaign_leads cld "
    "      ON cld.campaign_id = c.id "
    "     AND cld.manually_removed = 0 "
    "     AND cld.date_last_exited IS NULL "
    "    WHERE ce.campaign_id = c.id "
    "      AND ce.event_type = 'action' "
    "      AND ce.parent_id IS NOT NULL "
    "      AND ce.trigger_mode = 'date' "
    "      AND ce.trigger_date IS NOT NULL "
    "      AND ce.trigger_date <= '{now_utc}' "
    "      AND NOT EXISTS ("
    "        SELECT 1 "
    "        FROM {prefix}campaign_lead_event_log el3 "
    "        WHERE el3.campaign_id = cld.campaign_id "
    "          AND el3.lead_id = cld.lead_id "
    "          AND el3.rotation <=> cld.rotation "
    "        LIMIT 1"
    "      ) "
    "    LIMIT 1"
    "  )"
    ") q "
    "ORDER BY q.id"
)
_DEFAULT_SQL_CAMPAIGNS_DUE = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE
_DEFAULT_SQL_IMPORT_PENDING_COUNT = (
    "SELECT COUNT(*) AS cnt FROM {prefix}imports "
    "WHERE is_published = 1 "
    "AND (status IN (1,7) "
    "OR LOWER(CAST(status AS CHAR)) IN ('queued','pending','delayed')) "
    "AND (date_ended IS NULL OR date_ended >= '{import_pending_cutoff_utc}') "
    "AND (date_started IS NULL "
    "OR CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(properties, '$.line')), '0') AS UNSIGNED) < line_count)"
)
_LEGACY_CAMPAIGNS_DUE_SQLS = {
    _LEGACY_SQL_CAMPAIGNS_DUE_DEFAULT,
    _LEGACY_SQL_CAMPAIGNS_DUE_DEFAULT_DESC,
    _LEGACY_SQL_CAMPAIGNS_DUE_NO_DELETED,
    _LEGACY_SQL_CAMPAIGNS_DUE_NO_DELETED_DESC,
    _LEGACY_SQL_CAMPAIGNS_DUE_PUBLISHED_WINDOW,
}
_DEFAULT_SQL_SEGMENTS_ALL_PUBLISHED = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)


@dataclass(frozen=True)
class ManualInstanceConfig:
    name: str
    root: str
    mautic_major: int | None
    console_path: str | None
    local_php_path: str | None
    db_host: str | None
    db_port: int | None
    db_name: str | None
    db_user: str | None
    db_password: str | None
    db_table_prefix: str | None


@dataclass(frozen=True)
class ScheduledJobConfig:
    name: str
    enabled: bool
    interval_sec: int
    command_template: str
    timeout_sec: int | None
    quiet_hour: int | None
    quiet_window_min: int


@dataclass(frozen=True)
class AgentConfig:
    config_file_path: str
    config_schema_version: int
    config_customized: bool
    config_sha256: str
    poll_interval_sec: int
    dispatch_interval_sec: float
    discovery_roots: list[str]
    exclude_path_contains: list[str]
    supported_mautic_majors: list[int]
    custom_instances: list[ManualInstanceConfig]
    scheduled_jobs: list[ScheduledJobConfig]
    php_bin: str
    mautic_run_as_user: str | None
    command_timeout_sec: int
    state_db_path: str
    state_backend: str
    state_mysql_host: str | None
    state_mysql_unix_socket: str | None
    state_mysql_port: int
    state_mysql_database: str | None
    state_mysql_user: str | None
    state_mysql_password: str | None
    state_mysql_table_prefix: str
    state_mysql_connect_timeout_sec: int
    state_mysql_read_timeout_sec: int
    state_mysql_write_timeout_sec: int
    state_mysql_snapshot_enabled: bool
    tasks_history_keep_days: int
    tasks_history_max_rows: int
    tasks_compact_enabled: bool
    tasks_compact_interval_sec: int
    tasks_compact_quiet_hour: int
    tasks_compact_quiet_window_min: int
    tasks_compact_vacuum: bool
    tasks_archive_enabled: bool
    tasks_archive_dir: str
    tasks_archive_keep_days: int
    scheduler_pause_flag_path: str
    weights_recalc_interval_sec: int
    task_retry_max: int
    task_retry_delay_sec: int
    worker_watchdog_sec: int
    worker_stuck_policy: str
    worker_stuck_restart_limit: int
    jobs_max_workers: int
    scheduler_host_max_parallel: int
    scheduler_elastic_slots_enabled: bool
    scheduler_emergency_reserved_slots: int
    scheduler_instance_max_parallel: int
    scheduler_fairness_watchdog_sec: int
    segment_whitelist: list[int]
    segment_whitelist_file: str | None
    segment_whitelist_instance_settings: dict[str, Any]
    segment_recurring_priority_v1: dict[str, Any]
    campaign_whitelist: list[int]
    campaign_whitelist_file: str | None
    campaign_whitelist_instance_settings: dict[str, Any]
    segment_priority_weight_threshold: float
    segment_priority_size: int
    segment_mode: str
    segment_priority_parallel_idle: int
    segment_regular_parallel_idle: int
    segment_cycles_per_tick: int
    segment_full_scan_interval_sec: int
    segment_periodic_full_scan_enabled: bool
    segment_lazy_update_enabled: bool
    segment_lazy_update_instance_settings: dict[str, Any]
    segment_priority_parallel_throttled: int
    segment_regular_parallel_throttled: int
    segment_sql_ring_enabled: bool
    segment_sql_auto_enabled: bool
    segment_sql_auto_problem_threshold: int
    segment_sql_auto_max_clauses: int
    segment_sql_auto_long_native_enabled: bool
    segment_sql_auto_long_native_min_duration_sec: int
    segment_sql_auto_long_native_history_sec: int
    segment_sql_auto_long_native_min_successes: int
    segment_sql_long_ring_max_per_tick: int
    segment_sql_ring_max_per_tick: int
    segment_sql_min_repeat_sec: int
    segment_sql_lock_heartbeat_sec: int
    segment_sql_statement_timeout_sec: int
    segment_sql_orphan_policy: str
    segment_sql_orphan_after_sec: int
    segment_sql_page_hits_quiet_only: bool
    segment_sql_page_hits_quiet_hour: int
    segment_sql_page_hits_quiet_window_min: int
    segment_sql_ring_rules: dict[str, Any]
    segment_kill_mode: str
    segment_kill_grace_sec: int
    priority_segment_timeout_sec: int
    campaign_priority_parallel: int
    campaign_regular_parallel: int
    campaign_total_parallel: int
    campaign_priority_size: int
    campaign_latest_priority_count: int
    enable_campaign_rebuild: bool
    campaign_rebuild_poll_interval_sec: int
    campaign_rebuild_max_cycles_per_tick: int
    campaign_rebuild_priority_parallel: int
    campaign_rebuild_regular_parallel: int
    campaign_native_fallback_enabled: bool
    campaign_native_fallback_interval_sec: int
    enable_contacts_cleanup: bool
    contacts_cleanup_interval_sec: int
    contacts_cleanup_quiet_hour: int
    contacts_cleanup_quiet_window_min: int
    contacts_cleanup_email_field: str
    contacts_cleanup_phone_field: str
    contacts_cleanup_mode: str
    contacts_cleanup_max_delete_per_run: int
    enable_page_hits_orphan_cleanup: bool
    page_hits_orphan_cleanup_interval_sec: int
    page_hits_orphan_cleanup_quiet_hour: int
    page_hits_orphan_cleanup_quiet_window_min: int
    page_hits_orphan_cleanup_batch_size: int
    page_hits_orphan_cleanup_batches_per_run: int
    page_hits_orphan_cleanup_sleep_sec: float
    page_hits_orphan_cleanup_grace_min: int
    page_hits_orphan_cleanup_max_run_sec: int
    page_hits_orphan_cleanup_instance_settings: dict[str, Any]
    enable_mautic_lock_cleanup: bool
    mautic_lock_cleanup_interval_sec: int
    mautic_lock_cleanup_quiet_hour: int
    mautic_lock_cleanup_quiet_window_min: int
    mautic_lock_cleanup_min_age_sec: int
    mautic_lock_cleanup_max_rows_per_run: int
    enable_cache_clear: bool
    cache_clear_interval_sec: int
    cache_clear_quiet_hour: int
    cache_clear_quiet_window_min: int
    enable_cache_warm: bool
    cache_warm_interval_sec: int
    cache_warm_quiet_hour: int
    cache_warm_quiet_window_min: int
    plugin_operations: dict[str, Any]
    plugin_operation_instance_settings: dict[str, Any]
    plugin_operation_legacy_runtime: dict[str, Any]
    message_queue_enabled: bool
    message_queue_interval_sec: int
    message_queue_instance_settings: dict[str, Any]
    monitored_email_parser_enabled: bool
    monitored_email_parser_interval_sec: int
    monitored_email_parser_batch_size: int
    monitored_email_parser_force_seen: bool
    monitored_email_parser_delete_processed: bool
    monitored_email_parser_disable_mautic_fetch: bool
    monitored_email_parser_types: list[str]
    monitored_email_parser_whitelist: list[str]
    monitored_email_parser_instance_settings: dict[str, Any]
    form_embed_instance_settings: dict[str, Any]
    empty_leads_cleanup_enabled: bool
    empty_leads_cleanup_interval_sec: int
    empty_leads_cleanup_batch_size: int
    empty_leads_cleanup_max_batches_per_run: int
    empty_leads_cleanup_quiet_window_min: int
    empty_leads_cleanup_max_runs_per_window: int
    empty_leads_cleanup_instance_settings: dict[str, Any]
    cluster_assets_enabled: bool
    cluster_assets_interval_sec: int
    cluster_assets_state_file: str
    cluster_assets_fix_permissions: bool
    cluster_assets_reload_on_change: bool
    cluster_assets_cache_clear_on_change: bool
    cluster_assets_cache_warm_on_change: bool
    cluster_assets_fpm_reload_on_change: bool
    cluster_assets_reload_timeout_sec: int
    fs_permissions_guard_enabled: bool
    fs_permissions_guard_interval_sec: int
    fs_permissions_guard_paths: list[str]
    fs_permissions_guard_fix_console_exec: bool
    fs_permissions_guard_console_relpath: str
    scheduler_reconcile_interval_sec: int
    php_console_stuck_sec: int
    host_pressure_pause_enabled: bool
    host_pressure_php_stuck_pause_threshold: int
    host_pressure_swap_level_pause_threshold: int
    db_watchdog: dict[str, Any]
    segment_batch_limit: int
    campaign_batch_limit: int
    campaign_limit: int
    import_limit: int
    enable_import_polling: bool
    import_poll_interval_sec: int
    import_stall_timeout_sec: int
    queue_throttle_threshold: int
    queue_throttle_window_min: int
    sql_mail_queue_count: str
    sql_segments_due: str
    sql_segment_weights: str
    sql_campaigns_due: str
    sql_campaign_triggers_due: str
    sql_campaign_rebuilds_due: str
    sql_campaign_weights: str
    sql_import_pending_count: str
    cmd_segment_update_template: str
    cmd_segment_full_update_template: str
    cmd_campaign_update_template: str
    cmd_campaign_trigger_template: str
    cmd_campaign_rebuild_template: str
    cmd_import_template: str
    cmd_cache_clear_template: str
    cmd_cache_warm_template: str
    mcc_url: str | None
    mcc_token: str | None
    mcc_push_enabled: bool
    mcc_push_interval_sec: int
    mcc_push_on_change: bool
    mcc_push_alert_poll_interval_sec: int
    mcc_push_alert_window_min: int
    mcc_push_apt_state_interval_sec: int
    mcc_runtime_overrides_poll_enabled: bool
    mcc_profile_guard_enabled: bool
    inventory_auto_rescan_enabled: bool
    inventory_auto_rescan_interval_sec: int
    outbound_events_sent_keep_days: int
    mcc_host_name: str | None
    cluster_id: str | None
    cluster_name: str | None
    cluster_node_role: str
    cluster_node_index: int | None
    cluster_routing_enabled: bool
    cluster_route_cron_host: str | None
    cluster_route_import_host: str | None
    cluster_route_backup_host: str | None
    cluster_route_cache_hosts: list[str]
    cluster_replica_freshness_enabled: bool
    cluster_replica_freshness_max_age_sec: int
    cluster_replica_freshness_checks: list[dict[str, Any]]
    host_template: bool
    template_autopromote_on_clone: bool
    mcc_mcd_manifest_url: str | None
    plugins_repo_base_url: str | None
    plugins_repo_fallback_ip: str | None
    plugins_manifest_path_template: str
    plugins_post_cache_clear: bool
    plugins_post_install: bool
    plugins_state_filename: str
    plugins_cluster_file_sync_wait_sec: int
    plugins_cluster_cache_clear_wait_sec: int
    custom_repo_base_url: str | None
    custom_manifest_path: str
    custom_cache_dir: str
    custom_run_mode_default: str
    custom_prefer_tmux: bool
    custom_prefer_screen: bool
    custom_tmux_session_prefix: str
    custom_cache_cleanup_enabled: bool
    custom_cache_cleanup_interval_sec: int
    custom_cache_cleanup_quiet_hour: int
    custom_cache_cleanup_quiet_window_min: int
    custom_logs_keep_days: int
    custom_logs_max_files: int
    custom_downloads_keep_days: int
    custom_downloads_max_entries: int
    backup_enabled: bool
    backup_method: str
    backup_auto_install_packages: bool
    backup_state_dir: str
    backup_lock_dir: str
    backup_mount_base_dir: str
    backup_storage_kind: str
    backup_local_path: str
    backup_local_require_mount: bool
    backup_remote_root_dir: str
    backup_host_name: str | None
    backup_instance_name: str | None
    backup_retention_copies: int
    backup_mount_timeout_sec: int
    backup_unmount_timeout_sec: int
    backup_dump_timeout_sec: int
    backup_archive_enabled: bool
    backup_archive_name: str
    backup_archive_paths: list[str]
    backup_secrets_key_path: str
    backup_ssh_password_ref: str | None
    backup_ssh_host: str
    backup_ssh_port: int
    backup_ssh_user: str
    backup_ssh_remote_path: str
    backup_ssh_key_file: str | None
    backup_ssh_password: str | None
    backup_mysql_host: str | None
    backup_mysql_port: int | None
    backup_mysql_user: str | None
    backup_mysql_password: str | None
    backup_mysql_password_ref: str | None
    backup_mysql_database: str | None
    backup_sshfs_package: str
    backup_mydumper_package: str
    backup_mydumper_bin: str
    backup_mydumper_threads: int
    backup_mydumper_verbose: int
    backup_mydumper_compress: bool
    backup_mydumper_long_query_guard: int
    backup_mydumper_kill_long_queries: bool
    backup_mydumper_extra_args: list[str]
    backup_mydumper_use_nice: bool
    backup_mydumper_nice_level: int
    backup_mydumper_use_ionice: bool
    backup_mydumper_ionice_class: int
    backup_mydumper_ionice_level: int
    backup_myloader_bin: str
    backup_myloader_threads: int
    backup_xtrabackup_package: str
    backup_xtrabackup_bin: str
    backup_xtrabackup_parallel: int
    backup_xtrabackup_extra_args: list[str]
    backup_xtrabackup_incremental_enabled: bool
    backup_xtrabackup_full_interval_days: int
    backup_xtrabackup_retention_full_copies: int
    backup_xtrabackup_retention_incremental_days: int
    backup_cluster_enabled: bool
    backup_cluster_storage_mode: str
    backup_cluster_local_xtrabackup_enabled: bool
    backup_cluster_local_root_dir: str
    backup_cluster_full_hour: int
    backup_cluster_full_minute: int
    backup_cluster_offsite_not_before_hour: int
    backup_cluster_offsite_not_before_minute: int
    backup_cluster_incremental_start_hour: int
    backup_cluster_incremental_end_hour: int
    backup_cluster_incremental_interval_sec: int
    backup_cluster_incremental_min_free_bytes: int
    backup_cluster_offsite_source: str
    backup_cluster_files_snapshot_enabled: bool
    backup_cluster_files_snapshot_paths: list[str]
    backup_cluster_files_snapshot_exclude: list[str]
    backup_cluster_files_transport: str
    backup_cluster_files_sync_dir: str
    backup_cluster_files_node_paths: list[str]
    backup_cluster_files_shared_paths: list[str]
    backup_cluster_files_shared_producer_host: str | None
    backup_cluster_files_expected_nodes: list[str]
    backup_cluster_files_layer_max_age_sec: int
    backup_cluster_files_produce_interval_sec: int
    backup_cluster_remote_enabled: bool
    backup_cluster_remote_retention_daily: int
    backup_cluster_remote_retention_weekly: int
    backup_cluster_authority_role: str
    backup_cluster_authority_host: str | None
    backup_restore_apply_files: bool
    backup_restore_apply_databases: bool
    backup_schedule_enabled: bool
    backup_schedule_interval_sec: int
    backup_schedule_quiet_hour: int
    backup_schedule_quiet_minute: int
    backup_schedule_quiet_window_min: int
    backup_schedule_pre_pause_sec: int
    mcd_update_notify: bool
    mcd_auto_update_enabled: bool
    mcd_update_check_interval_sec: int
    mcd_update_channel: str
    mcd_update_policy: str
    mcd_update_allow_test_build: bool
    mcd_update_wait_retry_sec: int
    # Legacy compatibility only. Ordinary Mautic/PHP tasks never gate MCD
    # self-update; MCD-owned critical operation locks are the authority.
    mcd_update_defer_during_campaigns: bool
    mcd_update_cleanup_enabled: bool
    mcd_update_cleanup_interval_sec: int
    mcd_update_keep_archives: int
    mcd_update_keep_preupdate_backups: int
    mcd_update_artifacts_max_age_days: int
    mcd_config_history_limit: int
    service_profiles_enabled: bool
    service_profiles_auto_apply: bool
    service_profiles_poll_interval_sec: int
    service_profiles_components: list[str]
    mautic6_core_patch_policy: str
    mautic6_core_patch_version_min: str | None
    mautic6_core_patch_version_max: str | None
    mautic6_core_patch_apply_if_version_unknown: bool
    pagehit_cascade_patch_policy: str
    profile_name: str
    hardware_profile_name: str
    ring_mode: str
    disable_throttle: bool
    disable_whitelist: bool
    segment_throttle_during_campaigns: bool
    campaign_pressure_min_running_sec: int
    campaign_pressure_min_running_count: int
    segment_throttle_whitelist_only: bool
    segment_throttle_whitelist_parallel: int
    segment_throttle_kill_non_whitelist: bool
    campaign_update_priority_parallel: int
    campaign_update_regular_parallel: int
    campaign_trigger_priority_parallel: int
    campaign_trigger_regular_parallel: int
    campaign_trigger_min_repeat_sec: int
    campaign_trigger_audit_interval_sec: int
    campaign_trigger_due_guard_enabled: bool
    campaign_trigger_progress_watchdog_enabled: bool
    campaign_trigger_progress_watchdog_grace_sec: int
    campaign_trigger_progress_watchdog_interval_sec: int
    campaign_trigger_progress_watchdog_stable_checks: int
    campaign_rebuild_min_repeat_sec: int
    # Canonical static segment lanes. ``segment_whitelist`` remains a
    # migration alias for priority and is resolved by the daemon; it is not a
    # third execution lane.
    segment_priority: list[int] = field(default_factory=list)
    segment_super_priority: list[int] = field(default_factory=list)
    segment_priority_instance_settings: dict[str, Any] = field(default_factory=dict)
    segment_super_priority_instance_settings: dict[str, Any] = field(default_factory=dict)
    segment_stale_after_sec: int = 24 * 60 * 60
    # Historical wire/config name retained for compatibility. The value is
    # the maximum total starts for one automatic segment item, not an
    # additional-retry count. Values below zero are normalized to zero at
    # load time and become one safe effective start at admission.
    segment_retry_count: int = 2


def _apply_profile(cfg: AgentConfig) -> AgentConfig:
    p = (cfg.profile_name or "custom").strip().lower()
    if p in {"", "custom"}:
        return cfg

    # Shared defaults for named profiles.
    base = replace(
        cfg,
        profile_name=p,
        command_timeout_sec=0,
        worker_watchdog_sec=0,
    )

    if p == "tiny":
        return replace(
            base,
            ring_mode="single",
            disable_throttle=True,
            disable_whitelist=True,
            enable_campaign_rebuild=True,
            segment_priority_weight_threshold=999999,
            segment_priority_size=0,
            segment_priority_parallel_idle=0,
            segment_regular_parallel_idle=1,
            scheduler_host_max_parallel=1,
            scheduler_instance_max_parallel=1,
            segment_full_scan_interval_sec=60,
            segment_periodic_full_scan_enabled=True,
            segment_priority_parallel_throttled=0,
            segment_regular_parallel_throttled=1,
            campaign_priority_size=0,
            campaign_latest_priority_count=0,
            campaign_total_parallel=1,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=0,
            campaign_trigger_regular_parallel=1,
            campaign_rebuild_priority_parallel=0,
            campaign_rebuild_regular_parallel=1,
        )
    if p == "mini":
        return replace(
            base,
            ring_mode="single",
            disable_throttle=True,
            disable_whitelist=True,
            enable_campaign_rebuild=True,
            segment_priority_weight_threshold=999999,
            segment_priority_size=0,
            segment_priority_parallel_idle=0,
            segment_regular_parallel_idle=4,
            scheduler_host_max_parallel=4,
            scheduler_instance_max_parallel=1,
            segment_full_scan_interval_sec=120,
            segment_periodic_full_scan_enabled=True,
            segment_priority_parallel_throttled=0,
            segment_regular_parallel_throttled=4,
            campaign_priority_size=0,
            campaign_latest_priority_count=0,
            campaign_total_parallel=1,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=0,
            campaign_trigger_regular_parallel=2,
            campaign_rebuild_priority_parallel=0,
            campaign_rebuild_regular_parallel=1,
        )
    if p == "passive":
        return replace(
            base,
            ring_mode="single",
            disable_throttle=True,
            disable_whitelist=True,
            enable_import_polling=False,
            enable_campaign_rebuild=False,
            segment_priority_weight_threshold=999999,
            segment_priority_size=0,
            campaign_priority_size=0,
            campaign_latest_priority_count=0,
            segment_priority_parallel_idle=0,
            segment_regular_parallel_idle=0,
            scheduler_host_max_parallel=0,
            scheduler_instance_max_parallel=0,
            segment_full_scan_interval_sec=300,
            segment_priority_parallel_throttled=0,
            segment_regular_parallel_throttled=0,
            campaign_total_parallel=0,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=0,
            campaign_trigger_regular_parallel=0,
            campaign_rebuild_priority_parallel=0,
            campaign_rebuild_regular_parallel=0,
        )
    if p == "midi":
        return replace(
            base,
            ring_mode="dual",
            disable_throttle=True,
            disable_whitelist=False,
            enable_campaign_rebuild=True,
            segment_priority_size=10,
            campaign_priority_size=10,
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            scheduler_host_max_parallel=4,
            scheduler_instance_max_parallel=2,
            segment_full_scan_interval_sec=300,
            segment_periodic_full_scan_enabled=True,
            segment_priority_parallel_throttled=3,
            segment_regular_parallel_throttled=1,
            campaign_total_parallel=0,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=3,
            campaign_trigger_regular_parallel=1,
            campaign_rebuild_priority_parallel=3,
            campaign_rebuild_regular_parallel=1,
        )
    if p == "maxi":
        return replace(
            base,
            ring_mode="dual",
            disable_throttle=False,
            disable_whitelist=False,
            enable_campaign_rebuild=True,
            queue_throttle_threshold=200,
            queue_throttle_window_min=5,
            segment_priority_size=10,
            campaign_priority_size=10,
            segment_priority_parallel_idle=5,
            segment_regular_parallel_idle=1,
            scheduler_host_max_parallel=8,
            scheduler_instance_max_parallel=4,
            segment_full_scan_interval_sec=300,
            segment_periodic_full_scan_enabled=True,
            segment_priority_parallel_throttled=1,
            segment_regular_parallel_throttled=0,
            segment_throttle_whitelist_only=True,
            segment_throttle_whitelist_parallel=1,
            segment_throttle_kill_non_whitelist=False,
            campaign_total_parallel=0,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=3,
            campaign_trigger_regular_parallel=1,
            campaign_rebuild_priority_parallel=2,
            campaign_rebuild_regular_parallel=1,
        )
    farm_defaults = {
        "farm-tiny": (1, 1, 1, 1, 1, 1),
        "farm-mini": (1, 1, 1, 1, 2, 1),
        "farm-midi": (2, 1, 2, 1, 4, 2),
        "farm-maxi": (5, 2, 3, 1, 8, 4),
        "farm-hiload": (8, 3, 4, 2, 12, 6),
        "farm-ultra": (16, 7, 8, 4, 24, 12),
        # Compatibility for hosts assigned before the farm profile split.
        "farm": (8, 3, 4, 2, 12, 6),
    }
    if p in {"hiload", "ultra"} or p in farm_defaults:
        if p in farm_defaults:
            (
                segment_priority,
                segment_regular,
                campaign_priority,
                campaign_regular,
                scheduler_host_limit,
                scheduler_instance_limit,
            ) = farm_defaults[p]
        else:
            segment_priority = 10 if p == "ultra" else 6
            segment_regular = 4 if p == "ultra" else 2
            campaign_priority = 8 if p == "ultra" else 4
            campaign_regular = 4 if p == "ultra" else 2
            scheduler_host_limit, scheduler_instance_limit = {
                "hiload": (12, 6),
                "ultra": (24, 12),
            }[p]
        return replace(
            base,
            ring_mode="dual",
            disable_throttle=False,
            disable_whitelist=False,
            enable_campaign_rebuild=True,
            queue_throttle_threshold=200,
            queue_throttle_window_min=5,
            segment_priority_size=10,
            campaign_priority_size=10,
            segment_priority_parallel_idle=segment_priority,
            segment_regular_parallel_idle=segment_regular,
            segment_full_scan_interval_sec=300,
            segment_periodic_full_scan_enabled=True,
            segment_priority_parallel_throttled=2,
            segment_regular_parallel_throttled=0,
            segment_throttle_whitelist_only=True,
            segment_throttle_whitelist_parallel=2,
            segment_throttle_kill_non_whitelist=True,
            campaign_total_parallel=0,
            campaign_update_priority_parallel=0,
            campaign_update_regular_parallel=0,
            campaign_trigger_priority_parallel=campaign_priority,
            campaign_trigger_regular_parallel=campaign_regular,
            campaign_rebuild_priority_parallel=max(1, campaign_priority - 1),
            campaign_rebuild_regular_parallel=max(1, campaign_regular - 1),
            scheduler_host_max_parallel=scheduler_host_limit,
            scheduler_instance_max_parallel=scheduler_instance_limit,
        )
    return base


def reapply_profile_authority(cfg: AgentConfig, profile_name: str | None) -> AgentConfig:
    """Rebase an effective config onto the selected workload profile.

    Runtime overrides are allowed to change tunables, but they are not a
    second source of workload-profile identity.  A fresh config load during a
    sync can otherwise reintroduce a stale ``passive`` fallback before the
    canonical runtime payload is applied.  Rebase only the profile-owned
    capacity fields here; the caller applies current runtime overrides after
    this function returns.
    """

    selected = str(profile_name or "").strip().lower()
    if not selected or selected == str(getattr(cfg, "profile_name", "") or "").strip().lower():
        return cfg
    return _apply_profile(replace(cfg, profile_name=selected))


def _normalize_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _normalize_service_profile_components(value: object) -> list[str]:
    components = _normalize_list(value)
    if not components:
        components = ["php_fpm", "mysql", "apt", "wazuh", "mautic_db_indexes"]
    seen = {str(x).strip().lower().replace("-", "_") for x in components if str(x).strip()}
    # MCC runtime overrides created before apt profiles existed often contain
    # exactly ["php_fpm", "mysql"]. Treat that as the old default, not as an
    # operator decision to disable apt profile convergence.
    legacy_defaults = (
        {"php_fpm", "mysql"},
        {"php_fpm", "mysql", "apt"},
        {"php_fpm", "mysql", "apt", "mautic_db_indexes"},
    )
    if seen in legacy_defaults:
        for name in ("apt", "wazuh", "mautic_db_indexes"):
            if name not in seen:
                components.append(name)
                seen.add(name)
    return components


def _normalize_int_list(value: object) -> list[int]:
    items = _normalize_list(value)
    out: list[int] = []
    for raw in items:
        try:
            out.append(int(raw))
        except ValueError:
            continue
    return out


def _normalize_runtime_limit(value: object, *, default: int) -> int:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return int(default) if value else 0
    text = str(value).strip().lower()
    if text in {"", "none", "null"}:
        return int(default)
    if text in {"0", "false", "off", "disabled", "unlimited", "all"}:
        return 0
    try:
        return max(0, int(text))
    except (TypeError, ValueError):
        return int(default)


def _normalize_campaign_limit(value: object) -> int:
    limit = _normalize_runtime_limit(value, default=0)
    # 60000 was the historical package default. Treat it as unlimited during
    # config/runtime migration so large campaigns are not silently truncated on
    # hosts that never intentionally customized the value.
    return 0 if limit == 60000 else limit


def _normalize_campaign_trigger_template(value: object) -> str:
    default = "mautic:campaigns:trigger -i {id}{campaign_limit_arg} --batch-limit={batch_limit}"
    text = str(value or default).strip() or default
    legacy_limit = "--campaign-limit={campaign_limit}"
    if legacy_limit in text and "{campaign_limit_arg}" not in text:
        text = text.replace(f" {legacy_limit}", "{campaign_limit_arg}")
        text = text.replace(legacy_limit, "{campaign_limit_arg}")
    return " ".join(text.split())


def _campaign_trigger_slots_enabled(cfg: AgentConfig) -> bool:
    return (
        max(0, int(cfg.campaign_trigger_priority_parallel))
        + max(0, int(cfg.campaign_trigger_regular_parallel))
    ) > 0


def _campaign_rebuild_slots_enabled(cfg: AgentConfig) -> bool:
    return (
        max(0, int(cfg.campaign_rebuild_priority_parallel))
        + max(0, int(cfg.campaign_rebuild_regular_parallel))
    ) > 0


def _enforce_campaign_rebuild_guard(cfg: AgentConfig) -> AgentConfig:
    profile = (cfg.profile_name or "").strip().lower()
    if profile == "passive" or not _campaign_trigger_slots_enabled(cfg):
        return cfg
    updates: dict[str, Any] = {}
    if not cfg.enable_campaign_rebuild:
        updates["enable_campaign_rebuild"] = True
    if not _campaign_rebuild_slots_enabled(cfg):
        updates["campaign_rebuild_regular_parallel"] = 1
    if not updates:
        return cfg
    return replace(cfg, **updates)


def _enforce_campaign_trigger_audit_policy(cfg: AgentConfig) -> AgentConfig:
    """Keep active MCD campaign safety audits on a one-minute cadence."""
    profile = (cfg.profile_name or "").strip().lower()
    if profile == "passive" or not _campaign_trigger_slots_enabled(cfg):
        return cfg
    if cfg.campaign_trigger_audit_interval_sec == _CAMPAIGN_TRIGGER_AUDIT_INTERVAL_SEC:
        return cfg
    return replace(cfg, campaign_trigger_audit_interval_sec=_CAMPAIGN_TRIGGER_AUDIT_INTERVAL_SEC)


def _enforce_profile_guards(cfg: AgentConfig) -> AgentConfig:
    profile = (cfg.profile_name or "").strip().lower()
    if (
        profile == "midi"
        and cfg.ring_mode == "single"
        and cfg.disable_whitelist
        and cfg.segment_priority_size == 0
        and cfg.segment_priority_parallel_idle == 0
        and cfg.segment_regular_parallel_idle == 1
        and cfg.segment_priority_parallel_throttled == 0
        and cfg.segment_regular_parallel_throttled == 1
    ):
        # Early multi-instance installs persisted the old single-ring defaults
        # into runtime overrides. Do not let that exact legacy snapshot replace
        # the selected midi profile's dual 3+1 scheduler topology.
        return _apply_profile(cfg)
    if (
        cfg.ring_mode == "single"
        and not cfg.disable_whitelist
        and (
            cfg.segment_whitelist
            or cfg.campaign_whitelist
            or cfg.segment_whitelist_instance_settings
            or cfg.campaign_whitelist_instance_settings
        )
    ):
        # Whitelists only change the ordering of due Mautic work. A single
        # queue silently discards that distinction, so persisted legacy
        # overrides must not disable the priority ring.
        return replace(cfg, ring_mode="dual")
    if profile == "tiny" and not cfg.segment_periodic_full_scan_enabled:
        return replace(cfg, segment_periodic_full_scan_enabled=True)
    return cfg


def _normalize_json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_replica_freshness_checks(value: object) -> list[dict[str, Any]]:
    raw = value
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = []
        raw = parsed
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        database = str(item.get("database", item.get("schema", "")) or "").strip()
        table = str(item.get("table", "") or "").strip()
        column = str(item.get("column", item.get("date_column", "")) or "").strip()
        if not database or not table or not column:
            continue
        order_column = str(item.get("order_column", "id") or "").strip()
        row: dict[str, Any] = {
            "database": database,
            "table": table,
            "column": column,
            "order_column": order_column,
            "label": str(item.get("label", f"{database}.{table}.{column}") or "").strip(),
        }
        if item.get("max_age_sec") is not None:
            try:
                row["max_age_sec"] = max(60, int(item.get("max_age_sec") or 0))
            except Exception:
                pass
        out.append(row)
    return out


def _normalize_backup_dump_timeout(value: object) -> int:
    # Backup timeout must always be bounded in daemon mode.
    # Infinite wait would keep backup guard active and pause dispatch forever.
    try:
        raw = int(value)
    except Exception:
        raw = 0
    return raw if raw > 0 else 10_800


def _normalize_sql_signature(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _sql_branch_missing_publish_down(normalized: str, signatures: tuple[str, ...]) -> bool:
    for branch in normalized.split(" union "):
        if all(sig in branch for sig in signatures):
            if "c.publish_down is null or c.publish_down >= '{now_utc}'" not in branch:
                return True
    return False


def _is_legacy_campaigns_due_sql(value: object) -> bool:
    raw = str(value or "").strip()
    if raw in _LEGACY_CAMPAIGNS_DUE_SQLS:
        return True
    normalized = _normalize_sql_signature(raw)
    if not normalized:
        return False
    if "{prefix}campaigns c" in normalized and "c.deleted is null" in normalized:
        # Mautic campaign schema is not stable across supported majors; Mautic
        # 4 installations do not have campaigns.deleted. Any persisted due SQL
        # referencing it is unsafe and must be regenerated from the packaged
        # compatibility-safe default.
        return True
    event_log_active_window_signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_lead_event_log el",
        "el.is_scheduled = 1",
        "el.trigger_date <= '{now_utc}'",
    )
    if _sql_branch_missing_publish_down(normalized, event_log_active_window_signatures):
        return True
    # MCC used to persist the generated campaign trigger SQL under the legacy
    # campaigns_due key. Match that shape structurally so hosts with old
    # variants migrate instead of keeping stale trigger-date semantics forever.
    signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_lead_event_log el",
        "el.is_scheduled = 1",
        "el.trigger_date",
        "{prefix}campaign_leads cl",
        "window_start_local_24h",
        "el2.rotation <=> cl.rotation",
    )
    if all(sig in normalized for sig in signatures):
        return True
    old_trigger_only_signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_lead_event_log el",
        "el.date_triggered is null",
        "el.trigger_date <= '{now_utc}'",
        "el.is_scheduled = 1",
    )
    if all(sig in normalized for sig in old_trigger_only_signatures):
        if "campaign_events ce" not in normalized or "el0.event_id = ce.id" not in normalized:
            return True
    old_event_log_lower_bound = (
        "{prefix}campaign_lead_event_log el",
        "el.date_triggered is null",
        "el.trigger_date >= '{window_start_utc_7d}'",
        "el.trigger_date >= '{window_start_local_7d}'",
    )
    if all(sig in normalized for sig in old_event_log_lower_bound):
        return True
    dual_clock_event_log_semantics = (
        "{prefix}campaign_lead_event_log el",
        "el.trigger_date <= '{now_utc}'",
        "el.trigger_date <= '{now_local}'",
    )
    if all(sig in normalized for sig in dual_clock_event_log_semantics):
        return True
    old_campaign_event_lower_bound = (
        "{prefix}campaign_events ce",
        "ce.trigger_date >= '{window_start_utc_7d}'",
        "ce.trigger_date >= '{window_start_local_7d}'",
    )
    if all(sig in normalized for sig in old_campaign_event_lower_bound):
        return True
    old_root_action_date_semantics = (
        "campaign_events ce",
        "el0.event_id = ce.id",
        "ce.parent_id is null",
        "ce.trigger_mode in ('immediate', 'interval')",
        "ce.trigger_mode = 'date'",
    )
    if all(sig in normalized for sig in old_root_action_date_semantics):
        if "ce.trigger_date is null" not in normalized:
            return True
    old_root_action_event_specific_log = (
        "campaign_events ce",
        "el0.event_id = ce.id",
        "ce.parent_id is null",
        "el0.rotation <=> cld.rotation",
    )
    if all(sig in normalized for sig in old_root_action_event_specific_log):
        return True
    old_root_action_bootstrap_branch = (
        "campaign_events ce",
        "campaign_leads cld",
        "ce.parent_id is null",
        "campaign_lead_event_log el0",
        "el0.rotation <=> cld.rotation",
    )
    if all(sig in normalized for sig in old_root_action_bootstrap_branch):
        return True
    return False


def _is_legacy_segments_due_sql(value: object) -> bool:
    normalized = _normalize_sql_signature(value)
    if not normalized:
        return False
    if normalized in {_normalize_sql_signature(v) for v in {
        _LEGACY_SQL_SEGMENTS_DUE_DEFAULT,
        _LEGACY_SQL_SEGMENTS_DUE_DEFAULT_V0822,
        _LEGACY_SQL_SEGMENTS_DUE_DEFAULT_V0192,
    }}:
        return True
    default_shape = (
        "{prefix}lead_lists ll",
        "ll.last_built_date",
        "{prefix}lead_lists_leads lll",
        "{prefix}leads l",
    )
    if all(sig in normalized for sig in default_shape) and "{prefix}lead_donotcontact dnc" not in normalized:
        # DNC-backed segments such as dnc_unsubscribed depend on
        # lead_donotcontact, not only leads/lead_lists_leads. Old generated
        # defaults can mark those segments built while fresh unsubscribe rows
        # remain invisible until the next 24h full scan.
        return True
    if all(sig in normalized for sig in default_shape) and "{prefix}leads changed_lead" not in normalized:
        # Looking only at contacts already present in a segment misses newly
        # created or modified contacts that have just started matching it.
        return True
    return False


def _is_legacy_campaign_rebuilds_due_sql(value: object) -> bool:
    normalized = _normalize_sql_signature(value)
    if not normalized:
        return False
    if "{prefix}campaigns c" in normalized and "c.deleted is null" in normalized:
        return True
    date_action_active_window_signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_events ce",
        "ce.parent_id is not null",
        "ce.trigger_mode = 'date'",
        "ce.trigger_date <= '{now_utc}'",
        "el3.rotation <=> cld.rotation",
    )
    if _sql_branch_missing_publish_down(normalized, date_action_active_window_signatures):
        return True
    signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_leadlist_xref",
        "{prefix}campaign_events ce",
        "ce.trigger_mode = 'date'",
        "ce.trigger_date <= '{now_utc}'",
        "c.publish_down is null or c.publish_down >= '{now_utc}'",
        "el3.rotation <=> cld.rotation",
    )
    if all(sig in normalized for sig in signatures) and "ce.trigger_date <= '{now_local}'" in normalized:
        return True
    old_rebuild_event_lower_bound = (
        "{prefix}campaign_events ce",
        "ce.trigger_mode = 'date'",
        "ce.trigger_date >= '{window_start_utc_7d}'",
        "ce.trigger_date >= '{window_start_local_7d}'",
    )
    if all(sig in normalized for sig in old_rebuild_event_lower_bound):
        return True
    old_rebuild_signatures = (
        "{prefix}campaigns c",
        "{prefix}campaign_events ce",
        "ce.trigger_mode = 'date'",
        "ce.trigger_date <= '{now_utc}'",
        "el3.rotation <=> cld.rotation",
    )
    return (
        all(sig in normalized for sig in old_rebuild_signatures)
        and "ce.parent_id is not null" not in normalized
    )


def _is_legacy_import_pending_count_sql(value: object) -> bool:
    normalized = _normalize_sql_signature(value)
    if not normalized:
        return False
    if "{prefix}imports" not in normalized:
        return False
    # Pre-cutoff queries can resurrect historical partial rows after the
    # Mautic partial-batch date_ended correction. Migrate them to the
    # baseline-aware default instead of accepting an unbounded custom shape.
    if "date_ended" not in normalized:
        return True
    if "date_ended is null" in normalized and "import_pending_cutoff_utc" not in normalized:
        return True
    if "in_progress" in normalized:
        return True
    if "status in (1,2,7)" in normalized or "status in (1, 2, 7)" in normalized:
        return True
    if "<= line_count" in normalized:
        return True
    return False


def _campaign_triggers_due_sql(sql_section: dict[str, Any]) -> str:
    explicit = sql_section.get("campaign_triggers_due")
    if explicit is not None:
        if _is_legacy_campaigns_due_sql(explicit):
            return _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE
        return str(explicit)
    legacy = sql_section.get("campaigns_due")
    if legacy is not None and not _is_legacy_campaigns_due_sql(legacy):
        # Preserve intentional custom SQL while migrating known all-published defaults.
        return str(legacy)
    return _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE


def _campaign_rebuilds_due_sql(sql_section: dict[str, Any]) -> str:
    explicit = sql_section.get("campaign_rebuilds_due")
    if explicit is not None:
        if _is_legacy_campaign_rebuilds_due_sql(explicit):
            return _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE
        return str(explicit)
    return _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE


def _segments_due_sql(sql_section: dict[str, Any]) -> str:
    explicit = sql_section.get("segments_due")
    if explicit is not None:
        if _is_legacy_segments_due_sql(explicit):
            return _DEFAULT_SQL_SEGMENTS_DUE
        return str(explicit)
    return _DEFAULT_SQL_SEGMENTS_DUE


def _import_pending_count_sql(sql_section: dict[str, Any]) -> str:
    explicit = sql_section.get("import_pending_count")
    if explicit is not None:
        if _is_legacy_import_pending_count_sql(explicit):
            return _DEFAULT_SQL_IMPORT_PENDING_COUNT
        return str(explicit)
    return _DEFAULT_SQL_IMPORT_PENDING_COUNT


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[index]
        else:
            out[k] = v
    return out


def _load_toml_with_includes(path: str) -> dict[str, Any]:
    cfg_path = Path(path)
    root = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    include = root.get("include", {})
    files_raw = include.get("files", []) if isinstance(include, dict) else []
    files = _normalize_list(files_raw)

    merged: dict[str, Any] = {}
    for raw in files:
        p = Path(raw)
        if not p.is_absolute():
            p = (cfg_path.parent / p).resolve()
        if not p.exists():
            continue
        child = tomllib.loads(p.read_text(encoding="utf-8"))
        merged = _deep_merge(merged, child)

    root_wo_include = dict(root)
    root_wo_include.pop("include", None)
    return _deep_merge(merged, root_wo_include)


def _resolve_mutable_config_path(path: str) -> Path:
    """
    Resolve effective mutable config path.

    In split-layout mode, runtime overrides are expected in the last existing
    include file (usually /opt/mcd/etc/mcd.local.toml).
    """
    p = Path(path)
    if not p.exists():
        return p
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return p
    if "MCD_CONFIG_ENTRYPOINT v1" not in text:
        return p
    try:
        root = tomllib.loads(text)
    except Exception:
        return p
    include = root.get("include", {})
    files_raw = include.get("files", []) if isinstance(include, dict) else []
    files = _normalize_list(files_raw)
    resolved: list[Path] = []
    for raw in files:
        fp = Path(raw)
        if not fp.is_absolute():
            fp = (p.parent / fp).resolve()
        if fp != p:
            resolved.append(fp)
    for fp in reversed(resolved):
        if fp.exists():
            return fp
    if resolved:
        return resolved[-1]
    return p


def resolve_mutable_config_path(path: str) -> Path:
    return _resolve_mutable_config_path(path)


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for k, v in sorted(value.items(), key=lambda item: str(item[0])):
            key = json.dumps(str(k), ensure_ascii=True)
            parts.append(f"{key} = {_toml_literal(v)}")
        return "{ " + ", ".join(parts) + " }"
    return json.dumps("" if value is None else str(value), ensure_ascii=True)


def upsert_section_values(config_path: str, section_name: str, updates: dict[str, object]) -> tuple[str, bool]:
    """
    Upsert plain key-value entries in mutable config section and return
    (mutable_path, changed).
    """
    p = _resolve_mutable_config_path(config_path)
    if not updates:
        return str(p), False
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    section_lines = [f"{k} = {_toml_literal(v)}" for k, v in updates.items()]
    section = f"[{section_name}]\n" + "\n".join(section_lines) + "\n\n"
    m = re.search(rf"(?ms)^(\[{re.escape(section_name)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        text2 = section + text
        if text2 == text:
            return str(p), False
        p.write_text(text2, encoding="utf-8")
        return str(p), True
    body = m.group(2)
    key_set = set(updates.keys())
    out_lines: list[str] = []
    key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for raw in body.splitlines():
        km = key_re.match(raw)
        if km and km.group(1) in key_set:
            continue
        out_lines.append(raw)
    while out_lines and not out_lines[0].strip():
        out_lines.pop(0)
    merged: list[str] = list(section_lines)
    if out_lines:
        merged.append("")
        merged.extend(out_lines)
    new_body = "\n".join(merged).rstrip("\n")
    tail = text[m.end(2) :]
    # Keep section boundary valid even when original body had no trailing EOL
    # right before next section header.
    if tail and not tail.startswith("\n") and not new_body.endswith("\n"):
        new_body = new_body + "\n"
    text2 = text[: m.start(2)] + new_body + tail
    if text2 == text:
        return str(p), False
    p.write_text(text2, encoding="utf-8")
    return str(p), True


def upsert_runtime_values(config_path: str, updates: dict[str, object]) -> tuple[str, bool]:
    return upsert_section_values(config_path, "runtime", updates)


def persist_profile_authority(config_path: str, profile_name: str) -> tuple[str, bool]:
    """Persist the canonical workload-profile identity in the mutable config."""

    selected = str(profile_name or "").strip().lower()
    if not selected:
        return str(_resolve_mutable_config_path(config_path)), False
    return upsert_section_values(config_path, "profile", {"name": selected})


def remove_runtime_values(config_path: str, keys: set[str]) -> tuple[str, bool]:
    path = _resolve_mutable_config_path(config_path)
    if not path.exists() or not keys:
        return str(path), False
    text = path.read_text(encoding="utf-8")
    updated, removed = _remove_section_keys_text(text, "runtime", set(keys))
    if removed <= 0:
        return str(path), False
    path.write_text(updated, encoding="utf-8")
    return str(path), True


def _remove_section_keys_text(text: str, section: str, keys: set[str]) -> tuple[str, int]:
    if not keys:
        return text, 0
    m = re.search(rf"(?ms)^(\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return text, 0
    header = m.group(1)
    body = m.group(2)
    removed = 0
    out_lines: list[str] = []
    key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in body.splitlines(keepends=True):
        km = key_re.match(line)
        if km and km.group(1) in keys:
            removed += 1
            continue
        out_lines.append(line)
    if removed == 0:
        return text, 0
    text2 = text[: m.start()] + header + "".join(out_lines) + text[m.end() :]
    return text2, removed


def _replace_section_string_defaults_text(
    text: str,
    section: str,
    replacements: dict[str, tuple[set[str] | Callable[[str], bool], str]],
) -> tuple[str, int]:
    if not replacements:
        return text, 0
    m = re.search(rf"(?ms)^(\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return text, 0
    header = m.group(1)
    body = m.group(2)
    changed = 0
    out_lines: list[str] = []
    line_re = re.compile(
        r'^(\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)"((?:[^"\\]|\\.)*)"(\s*(?:#.*)?)$'
    )
    for line in body.splitlines(keepends=True):
        base_line = line[:-1] if line.endswith("\n") else line
        suffix_nl = "\n" if line.endswith("\n") else ""
        lm = line_re.match(base_line)
        if not lm:
            out_lines.append(line)
            continue
        key = lm.group(2)
        spec = replacements.get(key)
        if not spec:
            out_lines.append(line)
            continue
        expected_values, new_value = spec
        current_value = lm.group(3)
        matches = (
            expected_values(current_value)
            if callable(expected_values)
            else current_value in expected_values
        )
        if not matches:
            out_lines.append(line)
            continue
        escaped = new_value.replace("\\", "\\\\").replace('"', '\\"')
        out_lines.append(f'{lm.group(1)}"{escaped}"{lm.group(4)}{suffix_nl}')
        changed += 1
    if changed <= 0:
        return text, 0
    text2 = text[: m.start()] + header + "".join(out_lines) + text[m.end() :]
    return text2, changed


def _auto_migrate_legacy_runtime_keys(config_path: str) -> int:
    """
    Remove legacy runtime keys from mutable config file before parsing.
    This prevents old parallel/segment knobs from leaking into new releases.
    """
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return 0
    text2, removed = _remove_section_keys_text(text, "runtime", set(LEGACY_RUNTIME_KEYS))
    if removed <= 0:
        return 0
    p.write_text(text2, encoding="utf-8")
    return removed


def _auto_migrate_legacy_sql_defaults(config_path: str) -> int:
    """
    Upgrade legacy SQL defaults in mutable config to the current SQL shape.
    Planner mode is selected separately by ``segment_lazy_update_enabled``.
    """
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return 0
    text2, changed = _replace_section_string_defaults_text(
        text,
        "sql",
        {
            "segments_due": (
                _is_legacy_segments_due_sql,
                _DEFAULT_SQL_SEGMENTS_DUE,
            ),
            "campaigns_due": (
                _is_legacy_campaigns_due_sql,
                _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
            ),
            "campaign_triggers_due": (
                _is_legacy_campaigns_due_sql,
                _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
            ),
            "campaign_rebuilds_due": (
                _is_legacy_campaign_rebuilds_due_sql,
                _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
            ),
            "import_pending_count": (
                _is_legacy_import_pending_count_sql,
                _DEFAULT_SQL_IMPORT_PENDING_COUNT,
            ),
        },
    )
    if changed <= 0:
        return 0
    p.write_text(text2, encoding="utf-8")
    return changed


def _canonicalize_config_text_for_drift_check(text: str) -> str:
    """
    Apply the same nonsemantic config migrations used during load_config.

    MCC desired config can lag behind an agent release. A newer agent may
    rewrite old defaults on disk during load_config, after which raw SHA
    comparison would otherwise trigger an endless recover/rewrite loop.
    """
    out, _removed = _remove_section_keys_text(text, "runtime", set(LEGACY_RUNTIME_KEYS))
    out, _changed = _replace_section_string_defaults_text(
        out,
        "sql",
        {
            "segments_due": (
                _is_legacy_segments_due_sql,
                _DEFAULT_SQL_SEGMENTS_DUE,
            ),
            "campaigns_due": (
                _is_legacy_campaigns_due_sql,
                _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
            ),
            "campaign_triggers_due": (
                _is_legacy_campaigns_due_sql,
                _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
            ),
            "campaign_rebuilds_due": (
                _is_legacy_campaign_rebuilds_due_sql,
                _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
            ),
            "import_pending_count": (
                _is_legacy_import_pending_count_sql,
                _DEFAULT_SQL_IMPORT_PENDING_COUNT,
            ),
        },
    )
    return out


def _strip_legacy_runtime_keys(runtime: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = dict(runtime)
    removed: list[str] = []
    for key in LEGACY_RUNTIME_KEYS:
        if key in out:
            out.pop(key, None)
            removed.append(key)
    return out, removed


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _extract_mcc_bootstrap(raw_text: str) -> tuple[str | None, str | None, str | None]:
    # Preferred path: TOML parse if file is still syntactically valid.
    try:
        parsed = tomllib.loads(raw_text)
        mcc = parsed.get("mcc", {})
        if isinstance(mcc, dict):
            url = str(mcc.get("url", "")).strip() or None
            token = str(mcc.get("token", "")).strip() or None
            host_name = str(mcc.get("host_name", "")).strip() or None
            if url and token:
                return url, token, host_name
    except Exception:
        pass

    # Fallback: section-level regex extraction for broken TOML.
    sec = re.search(r"(?ms)^\[mcc\]\s*\n(.*?)(?=^\[|\Z)", raw_text)
    body = sec.group(1) if sec else raw_text
    m_url = re.search(r'(?m)^\s*url\s*=\s*"([^"]+)"\s*$', body)
    m_token = re.search(r'(?m)^\s*token\s*=\s*"([^"]+)"\s*$', body)
    m_host = re.search(r'(?m)^\s*host_name\s*=\s*"([^"]+)"\s*$', body)
    url = m_url.group(1).strip() if m_url else None
    token = m_token.group(1).strip() if m_token else None
    host_name = m_host.group(1).strip() if m_host else None
    return url, token, host_name


def _attempt_recover_config_from_mcc(config_path: str, *, load_error: Exception) -> tuple[bool, str]:
    p = Path(config_path)
    if not p.exists():
        return False, f"config file not found: {config_path}"
    try:
        raw_text = p.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"failed to read broken config: {e}"

    url, token, host_name = _extract_mcc_bootstrap(raw_text)
    if not url or not token:
        return False, "mcc bootstrap (url/token) unavailable in broken config"

    payload = {
        "hostname": socket.gethostname(),
        "mcc_host_name": host_name or "",
    }
    endpoint = url.rstrip("/") + "/api/v1/agent/config-desired"
    try:
        res = _post_json(endpoint, payload, token, timeout_sec=12)
    except HTTPError as e:
        return False, f"mcc config fetch http_{e.code}"
    except URLError as e:
        return False, f"mcc config fetch urlerror:{e.reason}"
    except Exception as e:
        return False, f"mcc config fetch failed: {e}"

    if str(res.get("status", "")).strip().lower() != "ok":
        return False, f"mcc config fetch status={res.get('status')} reason={res.get('reason')}"

    cfg_text = res.get("desired_config_toml")
    if not isinstance(cfg_text, str) or not cfg_text.strip():
        return False, "mcc returned empty desired_config_toml"

    try:
        tomllib.loads(cfg_text)
    except Exception as e:
        return False, f"mcc desired_config_toml is invalid: {e}"

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = p.with_name(f"{p.name}.broken-{ts}")
    try:
        backup_path.write_text(raw_text, encoding="utf-8")
    except Exception:
        # Non-fatal, still try to recover config.
        pass

    p.write_text(cfg_text, encoding="utf-8")
    return True, f"recovered from MCC desired config (backup={backup_path}, reason={load_error})"


def _fetch_desired_config_payload_from_mcc(config_path: str) -> tuple[bool, dict[str, Any] | str]:
    p = Path(config_path)
    if not p.exists():
        return False, f"config file not found: {config_path}"
    try:
        raw_text = p.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"failed to read config: {e}"

    url, token, host_name = _extract_mcc_bootstrap(raw_text)
    if not url or not token:
        return False, "mcc bootstrap (url/token) unavailable in local config"

    payload = {
        "hostname": socket.gethostname(),
        "mcc_host_name": host_name or "",
    }
    endpoint = url.rstrip("/") + "/api/v1/agent/config-desired"
    try:
        res = _post_json(endpoint, payload, token, timeout_sec=12)
    except HTTPError as e:
        return False, f"mcc config fetch http_{e.code}"
    except URLError as e:
        return False, f"mcc config fetch urlerror:{e.reason}"
    except Exception as e:
        return False, f"mcc config fetch failed: {e}"
    if not isinstance(res, dict):
        return False, "mcc config payload is not an object"
    return True, res


def check_profile_drift_with_mcc(
    config_path: str,
    *,
    current_profile: str,
    current_config_sha: str | None = None,
) -> dict[str, Any]:
    ok, res = _fetch_desired_config_payload_from_mcc(config_path)
    if not ok:
        return {"status": "disabled", "reason": str(res)}
    payload = cast(dict[str, Any], res)
    status = str(payload.get("status", "")).strip().lower()
    if status != "ok":
        return {
            "status": "error",
            "reason": f"mcc_status={status or '-'}",
            "mcc_reason": str(payload.get("reason", "")).strip() or None,
        }
    desired_profile = str(payload.get("desired_profile", "")).strip().lower()
    desired_cfg = payload.get("desired_config_toml")
    desired_cfg_text = desired_cfg if isinstance(desired_cfg, str) else ""
    desired_cfg_sha = str(payload.get("desired_config_sha256", "")).strip()
    if not desired_cfg_sha and desired_cfg_text:
        desired_cfg_sha = hashlib.sha256(desired_cfg_text.encode("utf-8")).hexdigest()
    current_profile_n = (current_profile or "").strip().lower()
    current_cfg_sha = str(current_config_sha or "").strip()
    out: dict[str, Any] = {
        "status": "ok",
        "desired_profile": desired_profile or None,
        "current_profile": current_profile_n or None,
        "config_source": str(payload.get("config_source", "")).strip() or None,
        "desired_config_sha256": desired_cfg_sha or None,
    }
    if current_cfg_sha:
        out["current_config_sha256"] = current_cfg_sha or None
    profile_mismatch = bool(desired_profile and current_profile_n and desired_profile != current_profile_n)
    config_sha_mismatch = bool(desired_cfg_sha and current_cfg_sha and desired_cfg_sha != current_cfg_sha)
    if profile_mismatch:
        out["status"] = "drift"
        out["reason"] = "profile_mismatch"

    if config_sha_mismatch and desired_cfg_text:
        canonical_desired_cfg = _canonicalize_config_text_for_drift_check(desired_cfg_text)
        canonical_desired_sha = hashlib.sha256(canonical_desired_cfg.encode("utf-8")).hexdigest()
        out["canonical_desired_config_sha256"] = canonical_desired_sha
        if canonical_desired_sha == current_cfg_sha:
            out["config_sha_mismatch_ignored"] = "local_auto_migration"
            return out

    if config_sha_mismatch:
        out["status"] = "drift"
        out["reason"] = "config_sha_mismatch"
    return out


def recover_config_from_mcc(config_path: str, *, reason: str) -> tuple[bool, str]:
    return _attempt_recover_config_from_mcc(
        config_path,
        load_error=RuntimeError(reason or "manual_recover_request"),
    )


def runtime_overrides_from_config_file(path: str) -> dict[str, Any]:
    """
    Read MCC-manageable operator runtime overrides from config file.

    The runtime section also contains daemon bootstrap/static settings such as
    ``dispatch_interval_sec`` and ``poll_interval_sec``.  Those values are
    intentionally not part of the MCC runtime contract.  Returning them here
    made a local baseline push them into the desired-state API, where a
    newer MCC correctly persisted them but rejected the agent acknowledgement
    as unsupported.  Project only the keys that can participate in the
    bidirectional runtime contract; static/local settings remain in the
    config file and continue to be loaded by ``load_config``.
    """
    def managed(values: dict[str, Any]) -> dict[str, Any]:
        allowed = runtime_remote_allowed_keys()
        return {key: value for key, value in values.items() if key in allowed}

    try:
        cfg = _load_toml_with_includes(path)
        rt = cfg.get("runtime", {})
        if isinstance(rt, dict):
            cleaned, _ = _strip_legacy_runtime_keys(dict(rt))
            return managed(cleaned)
    except Exception:
        pass
    # Fallback to mutable file only if include parser failed.
    p = _resolve_mutable_config_path(path)
    if not p.exists():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rt2 = raw.get("runtime", {})
    if isinstance(rt2, dict):
        cleaned2, _ = _strip_legacy_runtime_keys(dict(rt2))
        return managed(cleaned2)
    return {}


def runtime_effective_map(cfg: AgentConfig) -> dict[str, Any]:
    """
    Effective runtime map derived from loaded AgentConfig.
    """
    out: dict[str, Any] = {}
    for key, attr in _RUNTIME_TO_ATTR.items():
        if hasattr(cfg, attr):
            out[key] = getattr(cfg, attr)
    legacy = out.get("segment_whitelist_instance_settings")
    priority = out.get("segment_priority_instance_settings")
    if isinstance(legacy, dict) and isinstance(priority, dict):
        merged = dict(priority)
        legacy_projection: dict[str, Any] = {}
        for uid, raw in legacy.items():
            legacy_value = raw
            if isinstance(legacy_value, dict) and "segment_whitelist" in legacy_value:
                legacy_value = {"segments": _normalize_int_list(legacy_value.get("segment_whitelist"))}
            legacy_projection[str(uid)] = legacy_value
            if str(uid) in merged:
                # Explicit modern priority rows, including empty tombstones,
                # have precedence over the legacy alias.
                continue
            if isinstance(raw, dict) and "segment_whitelist" in raw:
                raw = {"segments": _normalize_int_list(raw.get("segment_whitelist"))}
            merged[str(uid)] = raw
        out["segment_priority_instance_settings"] = merged
        out["segment_whitelist_instance_settings"] = legacy_projection or merged
    return out


_RUNTIME_TO_ATTR: dict[str, str] = {
    "php_bin": "php_bin",
    "mautic_run_as_user": "mautic_run_as_user",
    "command_timeout_sec": "command_timeout_sec",
    "state_db_path": "state_db_path",
    "state_backend": "state_backend",
    "state_mysql_host": "state_mysql_host",
    "state_mysql_unix_socket": "state_mysql_unix_socket",
    "state_mysql_port": "state_mysql_port",
    "state_mysql_database": "state_mysql_database",
    "state_mysql_user": "state_mysql_user",
    "state_mysql_password": "state_mysql_password",
    "state_mysql_table_prefix": "state_mysql_table_prefix",
    "state_mysql_connect_timeout_sec": "state_mysql_connect_timeout_sec",
    "state_mysql_read_timeout_sec": "state_mysql_read_timeout_sec",
    "state_mysql_write_timeout_sec": "state_mysql_write_timeout_sec",
    "state_mysql_snapshot_enabled": "state_mysql_snapshot_enabled",
    "tasks_history_keep_days": "tasks_history_keep_days",
    "tasks_history_max_rows": "tasks_history_max_rows",
    "tasks_compact_enabled": "tasks_compact_enabled",
    "tasks_compact_interval_sec": "tasks_compact_interval_sec",
    "tasks_compact_quiet_hour": "tasks_compact_quiet_hour",
    "tasks_compact_quiet_window_min": "tasks_compact_quiet_window_min",
    "tasks_compact_vacuum": "tasks_compact_vacuum",
    "tasks_archive_enabled": "tasks_archive_enabled",
    "tasks_archive_dir": "tasks_archive_dir",
    "tasks_archive_keep_days": "tasks_archive_keep_days",
    "scheduler_pause_flag_path": "scheduler_pause_flag_path",
    "weights_recalc_interval_sec": "weights_recalc_interval_sec",
    "task_retry_max": "task_retry_max",
    "task_retry_delay_sec": "task_retry_delay_sec",
    "segment_retry_count": "segment_retry_count",
    "worker_watchdog_sec": "worker_watchdog_sec",
    "worker_stuck_policy": "worker_stuck_policy",
    "worker_stuck_restart_limit": "worker_stuck_restart_limit",
    "jobs_max_workers": "jobs_max_workers",
    "scheduler_host_max_parallel": "scheduler_host_max_parallel",
    "scheduler_elastic_slots_enabled": "scheduler_elastic_slots_enabled",
    "scheduler_emergency_reserved_slots": "scheduler_emergency_reserved_slots",
    "scheduler_instance_max_parallel": "scheduler_instance_max_parallel",
    "scheduler_fairness_watchdog_sec": "scheduler_fairness_watchdog_sec",
    "segment_whitelist": "segment_whitelist",
    "segment_whitelist_file": "segment_whitelist_file",
    "segment_whitelist_instance_settings": "segment_whitelist_instance_settings",
    "segment_priority": "segment_priority",
    "segment_super_priority": "segment_super_priority",
    "segment_priority_instance_settings": "segment_priority_instance_settings",
    "segment_super_priority_instance_settings": "segment_super_priority_instance_settings",
    "segment_stale_after_sec": "segment_stale_after_sec",
    "segment_recurring_priority_v1": "segment_recurring_priority_v1",
    "campaign_whitelist": "campaign_whitelist",
    "campaign_whitelist_file": "campaign_whitelist_file",
    "campaign_whitelist_instance_settings": "campaign_whitelist_instance_settings",
    "segment_priority_weight_threshold": "segment_priority_weight_threshold",
    "segment_priority_size": "segment_priority_size",
    "segment_mode": "segment_mode",
    "segment_priority_parallel_idle": "segment_priority_parallel_idle",
    "segment_regular_parallel_idle": "segment_regular_parallel_idle",
    "segment_full_scan_interval_sec": "segment_full_scan_interval_sec",
    "segment_periodic_full_scan_enabled": "segment_periodic_full_scan_enabled",
    "segment_lazy_update_enabled": "segment_lazy_update_enabled",
    "segment_lazy_update_instance_settings": "segment_lazy_update_instance_settings",
    "segment_cycles_per_tick": "segment_cycles_per_tick",
    "segment_priority_parallel_throttled": "segment_priority_parallel_throttled",
    "segment_regular_parallel_throttled": "segment_regular_parallel_throttled",
    "segment_sql_ring_enabled": "segment_sql_ring_enabled",
    "segment_sql_auto_enabled": "segment_sql_auto_enabled",
    "segment_sql_auto_problem_threshold": "segment_sql_auto_problem_threshold",
    "segment_sql_auto_max_clauses": "segment_sql_auto_max_clauses",
    "segment_sql_auto_long_native_enabled": "segment_sql_auto_long_native_enabled",
    "segment_sql_auto_long_native_min_duration_sec": "segment_sql_auto_long_native_min_duration_sec",
    "segment_sql_auto_long_native_history_sec": "segment_sql_auto_long_native_history_sec",
    "segment_sql_auto_long_native_min_successes": "segment_sql_auto_long_native_min_successes",
    "segment_sql_long_ring_max_per_tick": "segment_sql_long_ring_max_per_tick",
    "segment_sql_ring_max_per_tick": "segment_sql_ring_max_per_tick",
    "segment_sql_min_repeat_sec": "segment_sql_min_repeat_sec",
    "segment_sql_lock_heartbeat_sec": "segment_sql_lock_heartbeat_sec",
    "segment_sql_statement_timeout_sec": "segment_sql_statement_timeout_sec",
    "segment_sql_orphan_policy": "segment_sql_orphan_policy",
    "segment_sql_orphan_after_sec": "segment_sql_orphan_after_sec",
    "segment_sql_page_hits_quiet_only": "segment_sql_page_hits_quiet_only",
    "segment_sql_page_hits_quiet_hour": "segment_sql_page_hits_quiet_hour",
    "segment_sql_page_hits_quiet_window_min": "segment_sql_page_hits_quiet_window_min",
    "segment_sql_ring_rules": "segment_sql_ring_rules",
    "segment_kill_mode": "segment_kill_mode",
    "segment_kill_grace_sec": "segment_kill_grace_sec",
    "priority_segment_timeout_sec": "priority_segment_timeout_sec",
    "campaign_priority_parallel": "campaign_priority_parallel",
    "campaign_regular_parallel": "campaign_regular_parallel",
    "campaign_total_parallel": "campaign_total_parallel",
    "campaign_priority_size": "campaign_priority_size",
    "campaign_latest_priority_count": "campaign_latest_priority_count",
    "campaign_update_priority_parallel": "campaign_update_priority_parallel",
    "campaign_update_regular_parallel": "campaign_update_regular_parallel",
    "campaign_trigger_priority_parallel": "campaign_trigger_priority_parallel",
    "campaign_trigger_regular_parallel": "campaign_trigger_regular_parallel",
    "campaign_trigger_min_repeat_sec": "campaign_trigger_min_repeat_sec",
    "campaign_trigger_audit_interval_sec": "campaign_trigger_audit_interval_sec",
    "campaign_trigger_due_guard_enabled": "campaign_trigger_due_guard_enabled",
    "campaign_trigger_progress_watchdog_enabled": "campaign_trigger_progress_watchdog_enabled",
    "campaign_trigger_progress_watchdog_grace_sec": "campaign_trigger_progress_watchdog_grace_sec",
    "campaign_trigger_progress_watchdog_interval_sec": "campaign_trigger_progress_watchdog_interval_sec",
    "campaign_trigger_progress_watchdog_stable_checks": "campaign_trigger_progress_watchdog_stable_checks",
    "campaign_rebuild_min_repeat_sec": "campaign_rebuild_min_repeat_sec",
    "enable_campaign_rebuild": "enable_campaign_rebuild",
    "campaign_rebuild_poll_interval_sec": "campaign_rebuild_poll_interval_sec",
    "campaign_rebuild_max_cycles_per_tick": "campaign_rebuild_max_cycles_per_tick",
    "campaign_rebuild_priority_parallel": "campaign_rebuild_priority_parallel",
    "campaign_rebuild_regular_parallel": "campaign_rebuild_regular_parallel",
    "campaign_native_fallback_enabled": "campaign_native_fallback_enabled",
    "campaign_native_fallback_interval_sec": "campaign_native_fallback_interval_sec",
    "enable_contacts_cleanup": "enable_contacts_cleanup",
    "contacts_cleanup_interval_sec": "contacts_cleanup_interval_sec",
    "contacts_cleanup_quiet_hour": "contacts_cleanup_quiet_hour",
    "contacts_cleanup_quiet_window_min": "contacts_cleanup_quiet_window_min",
    "contacts_cleanup_email_field": "contacts_cleanup_email_field",
    "contacts_cleanup_phone_field": "contacts_cleanup_phone_field",
    "contacts_cleanup_mode": "contacts_cleanup_mode",
    "contacts_cleanup_max_delete_per_run": "contacts_cleanup_max_delete_per_run",
    "enable_page_hits_orphan_cleanup": "enable_page_hits_orphan_cleanup",
    "page_hits_orphan_cleanup_interval_sec": "page_hits_orphan_cleanup_interval_sec",
    "page_hits_orphan_cleanup_quiet_hour": "page_hits_orphan_cleanup_quiet_hour",
    "page_hits_orphan_cleanup_quiet_window_min": "page_hits_orphan_cleanup_quiet_window_min",
    "page_hits_orphan_cleanup_batch_size": "page_hits_orphan_cleanup_batch_size",
    "page_hits_orphan_cleanup_batches_per_run": "page_hits_orphan_cleanup_batches_per_run",
    "page_hits_orphan_cleanup_sleep_sec": "page_hits_orphan_cleanup_sleep_sec",
    "page_hits_orphan_cleanup_grace_min": "page_hits_orphan_cleanup_grace_min",
    "page_hits_orphan_cleanup_max_run_sec": "page_hits_orphan_cleanup_max_run_sec",
    "page_hits_orphan_cleanup_instance_settings": "page_hits_orphan_cleanup_instance_settings",
    "enable_mautic_lock_cleanup": "enable_mautic_lock_cleanup",
    "mautic_lock_cleanup_interval_sec": "mautic_lock_cleanup_interval_sec",
    "mautic_lock_cleanup_quiet_hour": "mautic_lock_cleanup_quiet_hour",
    "mautic_lock_cleanup_quiet_window_min": "mautic_lock_cleanup_quiet_window_min",
    "mautic_lock_cleanup_min_age_sec": "mautic_lock_cleanup_min_age_sec",
    "mautic_lock_cleanup_max_rows_per_run": "mautic_lock_cleanup_max_rows_per_run",
    "enable_cache_clear": "enable_cache_clear",
    "cache_clear_interval_sec": "cache_clear_interval_sec",
    "cache_clear_quiet_hour": "cache_clear_quiet_hour",
    "cache_clear_quiet_window_min": "cache_clear_quiet_window_min",
    "enable_cache_warm": "enable_cache_warm",
    "cache_warm_interval_sec": "cache_warm_interval_sec",
    "cache_warm_quiet_hour": "cache_warm_quiet_hour",
    "cache_warm_quiet_window_min": "cache_warm_quiet_window_min",
    "plugin_operations": "plugin_operations",
    "plugin_operation_instance_settings": "plugin_operation_instance_settings",
    "message_queue_enabled": "message_queue_enabled",
    "message_queue_interval_sec": "message_queue_interval_sec",
    "message_queue_instance_settings": "message_queue_instance_settings",
    "monitored_email_parser_enabled": "monitored_email_parser_enabled",
    "monitored_email_parser_interval_sec": "monitored_email_parser_interval_sec",
    "monitored_email_parser_batch_size": "monitored_email_parser_batch_size",
    "monitored_email_parser_force_seen": "monitored_email_parser_force_seen",
    "monitored_email_parser_delete_processed": "monitored_email_parser_delete_processed",
    "monitored_email_parser_disable_mautic_fetch": "monitored_email_parser_disable_mautic_fetch",
    "monitored_email_parser_types": "monitored_email_parser_types",
    "monitored_email_parser_whitelist": "monitored_email_parser_whitelist",
    "monitored_email_parser_instance_settings": "monitored_email_parser_instance_settings",
    "form_embed_instance_settings": "form_embed_instance_settings",
    "inventory_auto_rescan_enabled": "inventory_auto_rescan_enabled",
    "inventory_auto_rescan_interval_sec": "inventory_auto_rescan_interval_sec",
    "empty_leads_cleanup_enabled": "empty_leads_cleanup_enabled",
    "empty_leads_cleanup_interval_sec": "empty_leads_cleanup_interval_sec",
    "empty_leads_cleanup_batch_size": "empty_leads_cleanup_batch_size",
    "empty_leads_cleanup_max_batches_per_run": "empty_leads_cleanup_max_batches_per_run",
    "empty_leads_cleanup_quiet_window_min": "empty_leads_cleanup_quiet_window_min",
    "empty_leads_cleanup_max_runs_per_window": "empty_leads_cleanup_max_runs_per_window",
    "empty_leads_cleanup_instance_settings": "empty_leads_cleanup_instance_settings",
    "cluster_assets_enabled": "cluster_assets_enabled",
    "cluster_assets_interval_sec": "cluster_assets_interval_sec",
    "cluster_assets_state_file": "cluster_assets_state_file",
    "cluster_assets_fix_permissions": "cluster_assets_fix_permissions",
    "cluster_assets_reload_on_change": "cluster_assets_reload_on_change",
    "cluster_assets_cache_clear_on_change": "cluster_assets_cache_clear_on_change",
    "cluster_assets_cache_warm_on_change": "cluster_assets_cache_warm_on_change",
    "cluster_assets_fpm_reload_on_change": "cluster_assets_fpm_reload_on_change",
    "cluster_assets_reload_timeout_sec": "cluster_assets_reload_timeout_sec",
    "fs_permissions_guard_enabled": "fs_permissions_guard_enabled",
    "fs_permissions_guard_interval_sec": "fs_permissions_guard_interval_sec",
    "fs_permissions_guard_paths": "fs_permissions_guard_paths",
    "fs_permissions_guard_fix_console_exec": "fs_permissions_guard_fix_console_exec",
    "fs_permissions_guard_console_relpath": "fs_permissions_guard_console_relpath",
    "scheduler_reconcile_interval_sec": "scheduler_reconcile_interval_sec",
    "php_console_stuck_sec": "php_console_stuck_sec",
    "host_pressure_pause_enabled": "host_pressure_pause_enabled",
    "host_pressure_php_stuck_pause_threshold": "host_pressure_php_stuck_pause_threshold",
    "host_pressure_swap_level_pause_threshold": "host_pressure_swap_level_pause_threshold",
    "db_watchdog": "db_watchdog",
    "segment_batch_limit": "segment_batch_limit",
    "campaign_batch_limit": "campaign_batch_limit",
    "campaign_limit": "campaign_limit",
    "import_limit": "import_limit",
    "enable_import_polling": "enable_import_polling",
    "import_poll_interval_sec": "import_poll_interval_sec",
    "import_stall_timeout_sec": "import_stall_timeout_sec",
    "queue_throttle_threshold": "queue_throttle_threshold",
    "queue_throttle_window_min": "queue_throttle_window_min",
    "mcd_update_notify": "mcd_update_notify",
    "mcd_auto_update_enabled": "mcd_auto_update_enabled",
    "mcd_update_check_interval_sec": "mcd_update_check_interval_sec",
    "mcd_update_channel": "mcd_update_channel",
    "mcd_update_policy": "mcd_update_policy",
    "mcd_update_allow_test_build": "mcd_update_allow_test_build",
    "mcd_update_wait_retry_sec": "mcd_update_wait_retry_sec",
    "mcd_update_defer_during_campaigns": "mcd_update_defer_during_campaigns",
    "segment_throttle_during_campaigns": "segment_throttle_during_campaigns",
    "campaign_pressure_min_running_sec": "campaign_pressure_min_running_sec",
    "campaign_pressure_min_running_count": "campaign_pressure_min_running_count",
    "mcd_update_cleanup_enabled": "mcd_update_cleanup_enabled",
    "mcd_update_cleanup_interval_sec": "mcd_update_cleanup_interval_sec",
    "mcd_update_keep_archives": "mcd_update_keep_archives",
    "mcd_update_keep_preupdate_backups": "mcd_update_keep_preupdate_backups",
    "mcd_update_artifacts_max_age_days": "mcd_update_artifacts_max_age_days",
    "mcd_config_history_limit": "mcd_config_history_limit",
    "cluster_id": "cluster_id",
    "cluster_name": "cluster_name",
    "cluster_node_role": "cluster_node_role",
    "cluster_node_index": "cluster_node_index",
    "cluster_routing_enabled": "cluster_routing_enabled",
    "cluster_route_cron_host": "cluster_route_cron_host",
    "cluster_route_import_host": "cluster_route_import_host",
    "cluster_route_backup_host": "cluster_route_backup_host",
    "cluster_route_cache_hosts": "cluster_route_cache_hosts",
    "cluster_replica_freshness_enabled": "cluster_replica_freshness_enabled",
    "cluster_replica_freshness_max_age_sec": "cluster_replica_freshness_max_age_sec",
    "cluster_replica_freshness_checks": "cluster_replica_freshness_checks",
    "plugins_repo_base_url": "plugins_repo_base_url",
    "plugins_repo_fallback_ip": "plugins_repo_fallback_ip",
    "outbound_events_sent_keep_days": "outbound_events_sent_keep_days",
    "custom_cache_cleanup_enabled": "custom_cache_cleanup_enabled",
    "custom_cache_cleanup_interval_sec": "custom_cache_cleanup_interval_sec",
    "custom_cache_cleanup_quiet_hour": "custom_cache_cleanup_quiet_hour",
    "custom_cache_cleanup_quiet_window_min": "custom_cache_cleanup_quiet_window_min",
    "custom_logs_keep_days": "custom_logs_keep_days",
    "custom_logs_max_files": "custom_logs_max_files",
    "custom_downloads_keep_days": "custom_downloads_keep_days",
    "custom_downloads_max_entries": "custom_downloads_max_entries",
    "host_template": "host_template",
    "template_autopromote_on_clone": "template_autopromote_on_clone",
    "service_profiles_enabled": "service_profiles_enabled",
    "service_profiles_auto_apply": "service_profiles_auto_apply",
    "service_profiles_poll_interval_sec": "service_profiles_poll_interval_sec",
    "service_profiles_components": "service_profiles_components",
    "backup_enabled": "backup_enabled",
    "backup_method": "backup_method",
    "backup_auto_install_packages": "backup_auto_install_packages",
    "backup_dump_timeout_sec": "backup_dump_timeout_sec",
    "backup_schedule_enabled": "backup_schedule_enabled",
    "backup_schedule_interval_sec": "backup_schedule_interval_sec",
    "backup_schedule_quiet_hour": "backup_schedule_quiet_hour",
    "backup_schedule_quiet_minute": "backup_schedule_quiet_minute",
    "backup_schedule_quiet_window_min": "backup_schedule_quiet_window_min",
    "backup_schedule_pre_pause_sec": "backup_schedule_pre_pause_sec",
    "backup_mydumper_threads": "backup_mydumper_threads",
    "backup_mydumper_long_query_guard": "backup_mydumper_long_query_guard",
    "backup_mydumper_kill_long_queries": "backup_mydumper_kill_long_queries",
    "backup_mydumper_extra_args": "backup_mydumper_extra_args",
    "backup_mydumper_use_nice": "backup_mydumper_use_nice",
    "backup_mydumper_nice_level": "backup_mydumper_nice_level",
    "backup_mydumper_use_ionice": "backup_mydumper_use_ionice",
    "backup_mydumper_ionice_class": "backup_mydumper_ionice_class",
    "backup_mydumper_ionice_level": "backup_mydumper_ionice_level",
    "backup_xtrabackup_parallel": "backup_xtrabackup_parallel",
    "backup_xtrabackup_extra_args": "backup_xtrabackup_extra_args",
    "backup_xtrabackup_incremental_enabled": "backup_xtrabackup_incremental_enabled",
    "backup_xtrabackup_full_interval_days": "backup_xtrabackup_full_interval_days",
    "backup_xtrabackup_retention_full_copies": "backup_xtrabackup_retention_full_copies",
    "backup_xtrabackup_retention_incremental_days": "backup_xtrabackup_retention_incremental_days",
    "backup_cluster_enabled": "backup_cluster_enabled",
    "backup_cluster_local_root_dir": "backup_cluster_local_root_dir",
    "backup_cluster_full_hour": "backup_cluster_full_hour",
    "backup_cluster_full_minute": "backup_cluster_full_minute",
    "backup_cluster_offsite_not_before_hour": "backup_cluster_offsite_not_before_hour",
    "backup_cluster_offsite_not_before_minute": "backup_cluster_offsite_not_before_minute",
    "backup_cluster_incremental_start_hour": "backup_cluster_incremental_start_hour",
    "backup_cluster_incremental_end_hour": "backup_cluster_incremental_end_hour",
    "backup_cluster_incremental_interval_sec": "backup_cluster_incremental_interval_sec",
    "backup_cluster_incremental_min_free_bytes": "backup_cluster_incremental_min_free_bytes",
    "backup_cluster_offsite_source": "backup_cluster_offsite_source",
    "backup_cluster_files_snapshot_enabled": "backup_cluster_files_snapshot_enabled",
    "backup_cluster_files_snapshot_paths": "backup_cluster_files_snapshot_paths",
    "backup_cluster_files_snapshot_exclude": "backup_cluster_files_snapshot_exclude",
    "backup_cluster_files_transport": "backup_cluster_files_transport",
    "backup_cluster_files_sync_dir": "backup_cluster_files_sync_dir",
    "backup_cluster_files_node_paths": "backup_cluster_files_node_paths",
    "backup_cluster_files_shared_paths": "backup_cluster_files_shared_paths",
    "backup_cluster_files_shared_producer_host": "backup_cluster_files_shared_producer_host",
    "backup_cluster_files_expected_nodes": "backup_cluster_files_expected_nodes",
    "backup_cluster_files_layer_max_age_sec": "backup_cluster_files_layer_max_age_sec",
    "backup_cluster_files_produce_interval_sec": "backup_cluster_files_produce_interval_sec",
    "backup_cluster_remote_enabled": "backup_cluster_remote_enabled",
    "backup_cluster_remote_retention_daily": "backup_cluster_remote_retention_daily",
    "backup_cluster_remote_retention_weekly": "backup_cluster_remote_retention_weekly",
    "backup_cluster_authority_role": "backup_cluster_authority_role",
    "backup_cluster_authority_host": "backup_cluster_authority_host",
    "mautic6_core_patch_policy": "mautic6_core_patch_policy",
    "mautic6_core_patch_version_min": "mautic6_core_patch_version_min",
    "mautic6_core_patch_version_max": "mautic6_core_patch_version_max",
    "mautic6_core_patch_apply_if_version_unknown": "mautic6_core_patch_apply_if_version_unknown",
    "pagehit_cascade_patch_policy": "pagehit_cascade_patch_policy",
    "ring_mode": "ring_mode",
    "disable_throttle": "disable_throttle",
    "disable_whitelist": "disable_whitelist",
    "segment_throttle_during_campaigns": "segment_throttle_during_campaigns",
    "campaign_pressure_min_running_sec": "campaign_pressure_min_running_sec",
    "campaign_pressure_min_running_count": "campaign_pressure_min_running_count",
    "segment_throttle_whitelist_only": "segment_throttle_whitelist_only",
    "segment_throttle_whitelist_parallel": "segment_throttle_whitelist_parallel",
    "segment_throttle_kill_non_whitelist": "segment_throttle_kill_non_whitelist",
}

_RUNTIME_REMOTE_BLOCKED_KEYS: set[str] = {
    # Changing runtime DB path mid-process is unsafe because stores are
    # opened at daemon startup and should stay stable.
    "state_db_path",
    # Paused-flag file path is process-level and should remain static for
    # current daemon run.
    "scheduler_pause_flag_path",
    # CLI executor identity/path are process bootstrap settings.
    "php_bin",
    "mautic_run_as_user",
}


_PROFILE_LOCKED_RUNTIME_KEYS = {
    "command_timeout_sec",
    "worker_watchdog_sec",
    "ring_mode",
    "disable_throttle",
    "disable_whitelist",
    "enable_import_polling",
    "import_stall_timeout_sec",
    "enable_campaign_rebuild",
    "segment_priority_weight_threshold",
    "segment_priority_size",
    "campaign_priority_size",
    "campaign_latest_priority_count",
    "segment_priority_parallel_idle",
    "segment_regular_parallel_idle",
    "segment_priority_parallel_throttled",
    "segment_regular_parallel_throttled",
    "segment_throttle_whitelist_only",
    "segment_throttle_whitelist_parallel",
    "segment_throttle_kill_non_whitelist",
    "queue_throttle_threshold",
    "queue_throttle_window_min",
    "campaign_total_parallel",
    "campaign_update_priority_parallel",
    "campaign_update_regular_parallel",
    "campaign_trigger_priority_parallel",
    "campaign_trigger_regular_parallel",
    "campaign_rebuild_priority_parallel",
    "campaign_rebuild_regular_parallel",
    "scheduler_host_max_parallel",
    "scheduler_elastic_slots_enabled",
    "scheduler_emergency_reserved_slots",
    "scheduler_instance_max_parallel",
    "scheduler_fairness_watchdog_sec",
}


def _reapply_manual_runtime_overrides(cfg: AgentConfig, runtime: dict[str, Any]) -> AgentConfig:
    updates: dict[str, Any] = {}
    for raw_key, raw_value in runtime.items():
        if (
            raw_key in _PROFILE_LOCKED_RUNTIME_KEYS
            and str(getattr(cfg, "profile_name", "") or "").strip().lower() not in {"", "custom"}
        ):
            continue
        attr = _RUNTIME_TO_ATTR.get(raw_key)
        if not attr:
            continue
        if not hasattr(cfg, attr):
            continue
        current = getattr(cfg, attr)
        if attr in {"state_mysql_unix_socket"} and str(raw_value or "").strip() == "" and str(current or "").strip():
            # Remote runtime payloads may omit local socket details or send an
            # empty value. Do not erase a working local socket in-memory: the
            # daemon would fall back to PyMySQL's default localhost socket and
            # lose the Galera-backed MCD state backend until restart.
            continue
        if isinstance(current, bool):
            updates[attr] = bool(raw_value)
        elif isinstance(current, int):
            if attr == "campaign_limit":
                updates[attr] = _normalize_campaign_limit(raw_value)
            elif attr == "campaign_native_fallback_interval_sec":
                updates[attr] = max(300, int(raw_value or 3600))
            else:
                updates[attr] = int(raw_value)
        elif isinstance(current, float):
            updates[attr] = float(raw_value)
        elif isinstance(current, list):
            if attr in {
                "segment_whitelist",
                "campaign_whitelist",
                "segment_priority",
                "segment_super_priority",
            }:
                updates[attr] = _normalize_int_list(raw_value)
            else:
                updates[attr] = _normalize_list(raw_value)
        elif isinstance(current, dict):
            updates[attr] = dict(raw_value) if isinstance(raw_value, dict) else dict(current)
        elif current is None:
            if attr == "cluster_node_index":
                try:
                    updates[attr] = int(raw_value) if raw_value is not None and str(raw_value).strip() else None
                except Exception:
                    updates[attr] = None
            else:
                updates[attr] = str(raw_value).strip() if raw_value is not None and str(raw_value).strip() else None
        else:
            updates[attr] = str(raw_value)
    if not updates:
        return cfg
    return replace(cfg, **updates)


def _is_runtime_customized_against_profile(profiled: AgentConfig, merged: AgentConfig) -> bool:
    # "customized" means effective runtime differs from profile baseline.
    attrs = set(_RUNTIME_TO_ATTR.values())
    for attr in attrs:
        if not hasattr(profiled, attr) or not hasattr(merged, attr):
            continue
        if getattr(profiled, attr) != getattr(merged, attr):
            return True
    return False


def runtime_supported_keys() -> set[str]:
    return set(_RUNTIME_TO_ATTR.keys())


def runtime_remote_allowed_keys() -> set[str]:
    return runtime_supported_keys() - set(_RUNTIME_REMOTE_BLOCKED_KEYS)


def normalize_runtime_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize runtime override payload into a flat runtime-key map.
    Supported forms:
      - {"segment_regular_parallel_idle": 2}
      - {"runtime.segment_regular_parallel_idle": 2}
      - {"runtime": {"segment_regular_parallel_idle": 2}}
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        if key == "runtime" and isinstance(v, dict):
            for rk, rv in v.items():
                rkey = str(rk).strip()
                if rkey:
                    out[rkey] = rv
            continue
        if key.startswith("runtime."):
            key = key[len("runtime.") :].strip()
            if not key:
                continue
        out[key] = v
    return out


def apply_runtime_overrides(
    cfg: AgentConfig,
    runtime: dict[str, Any],
    *,
    allowed_keys: set[str] | None = None,
) -> tuple[AgentConfig, list[str], list[str], list[str]]:
    """
    Apply runtime overrides onto existing AgentConfig and return:
      (new_cfg, applied_keys, unsupported_keys, blocked_keys)
    """
    normalized = normalize_runtime_overrides(runtime)
    applicable: dict[str, Any] = {}
    unsupported: list[str] = []
    blocked: list[str] = []
    for raw_key, raw_val in normalized.items():
        if raw_key not in _RUNTIME_TO_ATTR:
            unsupported.append(raw_key)
            continue
        if allowed_keys is not None and raw_key not in allowed_keys:
            blocked.append(raw_key)
            continue
        applicable[raw_key] = raw_val
    merged = _reapply_manual_runtime_overrides(cfg, applicable)
    return merged, sorted(applicable.keys()), sorted(set(unsupported)), sorted(set(blocked))


def _parse_manual_instances(data: object) -> list[ManualInstanceConfig]:
    if not isinstance(data, list):
        return []

    out: list[ManualInstanceConfig] = []
    for idx, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            continue
        out.append(
            ManualInstanceConfig(
                name=str(row.get("name", f"manual-{idx}")),
                root=str(row.get("root", "")).strip(),
                mautic_major=int(row["mautic_major"]) if row.get("mautic_major") else None,
                console_path=str(row.get("console_path")).strip() if row.get("console_path") else None,
                local_php_path=str(row.get("local_php_path")).strip() if row.get("local_php_path") else None,
                db_host=str(row.get("db_host")).strip() if row.get("db_host") else None,
                db_port=int(row["db_port"]) if row.get("db_port") else None,
                db_name=str(row.get("db_name")).strip() if row.get("db_name") else None,
                db_user=str(row.get("db_user")).strip() if row.get("db_user") else None,
                db_password=str(row.get("db_password")) if row.get("db_password") else None,
                db_table_prefix=str(row.get("db_table_prefix")).strip() if row.get("db_table_prefix") else None,
            )
        )
    return [x for x in out if x.root]


def _parse_scheduled_jobs(data: object) -> list[ScheduledJobConfig]:
    if not isinstance(data, list):
        return []
    out: list[ScheduledJobConfig] = []
    for idx, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", f"job-{idx}")).strip() or f"job-{idx}"
        enabled = bool(row.get("enabled", True))
        interval_sec = int(row.get("interval_sec", 60))
        command_template = str(row.get("command_template", "")).strip()
        timeout_sec = int(row["timeout_sec"]) if row.get("timeout_sec") else None
        quiet_hour_raw = row.get("quiet_hour")
        quiet_hour = int(quiet_hour_raw) if quiet_hour_raw is not None else None
        quiet_window_min = int(row.get("quiet_window_min", 60))
        if not command_template:
            continue
        out.append(
            ScheduledJobConfig(
                name=name,
                enabled=enabled,
                interval_sec=max(1, interval_sec),
                command_template=command_template,
                timeout_sec=timeout_sec,
                quiet_hour=None if quiet_hour is None else max(0, min(23, quiet_hour)),
                quiet_window_min=max(1, min(180, quiet_window_min)),
            )
        )
    return out


def _record_config_history(cfg: AgentConfig) -> None:
    limit = max(1, int(cfg.mcd_config_history_limit))
    hist_path = Path(cfg.state_db_path).parent / "config-history.json"
    row: dict[str, Any] = {
        "ts": int(time.time()),
        "config_path": cfg.config_file_path,
        "schema_version": int(cfg.config_schema_version),
        "customized": bool(cfg.config_customized),
        "sha256": cfg.config_sha256,
    }
    try:
        row["toml"] = Path(cfg.config_file_path).read_text(encoding="utf-8")
    except Exception:
        row["toml"] = None

    existing: list[dict[str, Any]] = []
    if hist_path.exists():
        try:
            raw = json.loads(hist_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("history"), list):
                existing = [x for x in raw["history"] if isinstance(x, dict)]
        except Exception:
            existing = []

    new_hist: list[dict[str, Any]] = []
    if existing and str(existing[0].get("sha256", "")) == cfg.config_sha256:
        head = dict(existing[0])
        head["ts"] = row["ts"]
        if head.get("toml") is None and row.get("toml") is not None:
            head["toml"] = row.get("toml")
        new_hist.append(head)
        new_hist.extend(existing[1:])
    else:
        new_hist.append(row)
        new_hist.extend(existing)

    payload = {"history": new_hist[:limit]}
    try:
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        return


def _load_config_inner(path: str) -> AgentConfig:
    data = _load_toml_with_includes(path)
    raw_text = Path(path).read_text(encoding="utf-8")
    cfg_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    config_sec = data.get("config", {})
    schema_version = CURRENT_CONFIG_SCHEMA_VERSION
    if isinstance(config_sec, dict) and config_sec.get("schema_version") is not None:
        try:
            schema_version = max(1, int(config_sec.get("schema_version")))
        except Exception:
            schema_version = CURRENT_CONFIG_SCHEMA_VERSION
    if schema_version < CURRENT_CONFIG_SCHEMA_VERSION:
        schema_version = CURRENT_CONFIG_SCHEMA_VERSION

    daemon = data.get("daemon", {})
    discovery = data.get("discovery", {})
    runtime_raw = data.get("runtime", {})
    runtime = dict(runtime_raw) if isinstance(runtime_raw, dict) else {}
    runtime, _ = _strip_legacy_runtime_keys(runtime)
    sql = data.get("sql", {})
    commands = data.get("commands", {})
    mcc = data.get("mcc", {})
    plugins = data.get("plugins", {})
    custom = data.get("custom", {}) if isinstance(data.get("custom", {}), dict) else {}
    state = data.get("state", {}) if isinstance(data.get("state", {}), dict) else {}
    backup = data.get("backup", {})
    backup_storage = backup.get("storage", {}) if isinstance(backup, dict) else {}
    backup_packages = backup.get("packages", {}) if isinstance(backup, dict) else {}
    backup_mydumper = backup.get("mydumper", {}) if isinstance(backup, dict) else {}
    backup_xtrabackup = backup.get("xtrabackup", {}) if isinstance(backup, dict) else {}
    backup_cluster = backup.get("cluster", {}) if isinstance(backup, dict) else {}
    backup_archive = backup.get("archive", {}) if isinstance(backup, dict) else {}
    backup_mysql = backup.get("mysql", {}) if isinstance(backup, dict) else {}
    backup_schedule = backup.get("schedule", {}) if isinstance(backup, dict) else {}
    backup_secrets = backup.get("secrets", {}) if isinstance(backup, dict) else {}
    cluster_cfg = data.get("cluster", {}) if isinstance(data.get("cluster", {}), dict) else {}
    profile = data.get("profile", {})
    hardware = data.get("hardware", {}) if isinstance(data.get("hardware", {}), dict) else {}

    profile_name = str(profile.get("name", runtime.get("profile", "custom"))).strip().lower() or "custom"
    hardware_profile_name = str(
        hardware.get("profile", hardware.get("profile_name", runtime.get("hardware_profile", "auto")))
    ).strip().lower() or "auto"
    state_backend = str(runtime.get("state_backend", state.get("backend", "sqlite"))).strip().lower() or "sqlite"
    if state_backend in {"mysql", "mariadb", "hybrid"}:
        state_backend = "mysql_hybrid"
    if state_backend not in {"sqlite", "mysql_hybrid"}:
        state_backend = "sqlite"
    ring_mode = str(runtime.get("ring_mode", "dual")).strip().lower()
    disable_throttle = bool(runtime.get("disable_throttle", False))
    disable_whitelist = bool(runtime.get("disable_whitelist", False))
    segment_throttle_whitelist_only = bool(runtime.get("segment_throttle_whitelist_only", False))
    segment_throttle_whitelist_parallel = int(runtime.get("segment_throttle_whitelist_parallel", 1))
    segment_throttle_kill_non_whitelist = bool(runtime.get("segment_throttle_kill_non_whitelist", False))
    segment_throttle_during_campaigns = bool(runtime.get("segment_throttle_during_campaigns", True))
    campaign_pressure_min_running_sec = max(0, int(runtime.get("campaign_pressure_min_running_sec", 120) or 0))
    campaign_pressure_min_running_count = max(0, int(runtime.get("campaign_pressure_min_running_count", 2) or 0))
    campaign_priority_parallel = int(runtime.get("campaign_priority_parallel", 4))
    campaign_regular_parallel = int(runtime.get("campaign_regular_parallel", 1))
    campaign_total_parallel = int(runtime.get("campaign_total_parallel", 0))
    campaign_update_priority_parallel = int(runtime.get("campaign_update_priority_parallel", campaign_priority_parallel))
    campaign_update_regular_parallel = int(runtime.get("campaign_update_regular_parallel", campaign_regular_parallel))
    campaign_trigger_priority_parallel = int(runtime.get("campaign_trigger_priority_parallel", campaign_priority_parallel))
    campaign_trigger_regular_parallel = int(runtime.get("campaign_trigger_regular_parallel", campaign_regular_parallel))
    campaign_trigger_min_repeat_sec = int(runtime.get("campaign_trigger_min_repeat_sec", 10))
    campaign_trigger_audit_interval_sec = int(
        runtime.get("campaign_trigger_audit_interval_sec", _CAMPAIGN_TRIGGER_AUDIT_INTERVAL_SEC)
    )
    campaign_trigger_due_guard_enabled = bool(runtime.get("campaign_trigger_due_guard_enabled", True))
    campaign_trigger_progress_watchdog_enabled = bool(
        runtime.get("campaign_trigger_progress_watchdog_enabled", True)
    )
    campaign_trigger_progress_watchdog_grace_sec = max(
        0,
        int(runtime.get("campaign_trigger_progress_watchdog_grace_sec", 180) or 180),
    )
    campaign_trigger_progress_watchdog_interval_sec = max(
        10,
        int(runtime.get("campaign_trigger_progress_watchdog_interval_sec", 60) or 60),
    )
    campaign_trigger_progress_watchdog_stable_checks = max(
        1,
        int(runtime.get("campaign_trigger_progress_watchdog_stable_checks", 2) or 2),
    )
    campaign_rebuild_min_repeat_sec = int(runtime.get("campaign_rebuild_min_repeat_sec", 15))
    segment_sql_orphan_policy = str(runtime.get("segment_sql_orphan_policy", "reclaim_stale")).strip().lower() or "reclaim_stale"
    if segment_sql_orphan_policy not in {"manual", "reclaim_stale"}:
        segment_sql_orphan_policy = "reclaim_stale"
    mcd_update_policy = str(runtime.get("mcd_update_policy", "")).strip().lower()
    if not mcd_update_policy:
        # Backward compatibility: old channel names.
        ch = str(runtime.get("mcd_update_channel", "approved")).strip().lower()
        if ch in {"stable", "approved"}:
            mcd_update_policy = "approved"
        elif ch == "lts":
            mcd_update_policy = "lts"
        elif ch in {"rc", "test"}:
            mcd_update_policy = "test"
        elif ch == "cluster":
            mcd_update_policy = "cluster"
        elif ch == "off":
            mcd_update_policy = "off"
        else:
            mcd_update_policy = "approved"
    if mcd_update_policy not in {"off", "lts", "approved", "test", "cluster"}:
        mcd_update_policy = "approved"
    cluster_id = str(runtime.get("cluster_id", cluster_cfg.get("id", "")) or "").strip() or None
    cluster_name = str(runtime.get("cluster_name", cluster_cfg.get("name", "")) or "").strip() or None
    cluster_node_role = str(runtime.get("cluster_node_role", cluster_cfg.get("node_role", "")) or "").strip().lower()
    if cluster_node_role not in {"node", "replica", "arbiter", "other"}:
        cluster_node_role = ""
    cluster_node_index: int | None = None
    raw_cluster_node_index = runtime.get("cluster_node_index", cluster_cfg.get("node_index"))
    if raw_cluster_node_index is not None and str(raw_cluster_node_index).strip():
        try:
            cluster_node_index = int(raw_cluster_node_index)
        except Exception:
            cluster_node_index = None
    cluster_routing = cluster_cfg.get("routing", {}) if isinstance(cluster_cfg.get("routing", {}), dict) else {}
    cluster_routing_enabled = bool(runtime.get("cluster_routing_enabled", cluster_routing.get("enabled", True)))
    cluster_route_cron_host = (
        str(
            runtime.get(
                "cluster_route_cron_host",
                cluster_routing.get("cron_host", cluster_routing.get("cron_node", "")),
            )
            or ""
        ).strip()
        or None
    )
    cluster_route_import_host = (
        str(
            runtime.get(
                "cluster_route_import_host",
                cluster_routing.get("import_host", cluster_routing.get("import_node", "")),
            )
            or ""
        ).strip()
        or None
    )
    cluster_route_backup_host = (
        str(
            runtime.get(
                "cluster_route_backup_host",
                cluster_routing.get("backup_host", cluster_routing.get("backup_node", "")),
            )
            or ""
        ).strip()
        or None
    )
    cluster_route_cache_hosts = _normalize_list(
        runtime.get(
            "cluster_route_cache_hosts",
            cluster_routing.get("cache_hosts", []),
        )
    )
    cluster_replica_freshness_checks = _normalize_replica_freshness_checks(
        runtime.get("cluster_replica_freshness_checks", [])
    )
    backup_cluster_authority_role = str(
        runtime.get("backup_cluster_authority_role", backup_cluster.get("authority_role", "replica")) or "replica"
    ).strip().lower()
    if backup_cluster_authority_role in {"*", "all"}:
        backup_cluster_authority_role = "any"
    if backup_cluster_authority_role not in {"replica", "node", "arbiter", "other", "any"}:
        backup_cluster_authority_role = "replica"
    backup_cluster_authority_host = (
        str(
            runtime.get(
                "backup_cluster_authority_host",
                backup_cluster.get("authority_host", cluster_route_backup_host or ""),
            )
            or ""
        ).strip()
        or None
    )
    m6_patch_policy = str(runtime.get("mautic6_core_patch_policy", "required")).strip().lower() or "required"
    if m6_patch_policy not in {"required", "off"}:
        m6_patch_policy = "required"
    m6_patch_min = str(runtime.get("mautic6_core_patch_version_min", "")).strip() or None
    m6_patch_max = str(runtime.get("mautic6_core_patch_version_max", "")).strip() or None
    m6_patch_unknown = bool(runtime.get("mautic6_core_patch_apply_if_version_unknown", True))
    pagehit_patch_policy = str(runtime.get("pagehit_cascade_patch_policy", "off")).strip().lower() or "off"
    if pagehit_patch_policy not in {"required", "off"}:
        pagehit_patch_policy = "off"

    cleanup_mode = str(runtime.get("contacts_cleanup_mode", "email_and_mobile")).strip().lower()
    if cleanup_mode in {"email_only", "email"}:
        cleanup_mode = "email_only"
    else:
        cleanup_mode = "email_and_mobile"
    db_watchdog_cfg = runtime.get("db_watchdog", {})
    if not isinstance(db_watchdog_cfg, dict):
        db_watchdog_cfg = {}
    fs_guard_paths = normalize_guard_paths(runtime.get("fs_permissions_guard_paths", default_guard_paths()))
    raw_profile_guard = mcc.get("profile_guard_enabled", True)
    if isinstance(raw_profile_guard, str) and raw_profile_guard.strip().lower() in {"0", "false", "no", "off", "disabled"}:
        profile_guard_enabled = False
    elif raw_profile_guard is False and not (mcc.get("url") and mcc.get("token")):
        profile_guard_enabled = False
    else:
        # Old managed configs were generated with boolean false when the guard
        # was optional. MCC-managed hosts now need config self-healing by
        # default; use string "off" for an intentional hard-disable.
        profile_guard_enabled = True

    cfg = AgentConfig(
        config_file_path=str(Path(path).resolve()),
        config_schema_version=schema_version,
        config_customized=bool(isinstance(runtime, dict) and len(runtime.keys()) > 0),
        config_sha256=cfg_sha,
        poll_interval_sec=int(daemon.get("poll_interval_sec", 30)),
        dispatch_interval_sec=float(daemon.get("dispatch_interval_sec", 1)),
        discovery_roots=_normalize_list(discovery.get("roots", ["/var/www"])),
        exclude_path_contains=_normalize_list(discovery.get("exclude_path_contains", [])),
        supported_mautic_majors=_normalize_int_list(discovery.get("supported_mautic_majors", [4, 5, 6, 7])),
        custom_instances=_parse_manual_instances(data.get("instances", [])),
        scheduled_jobs=_parse_scheduled_jobs(data.get("jobs", [])),
        php_bin=str(runtime.get("php_bin", "/usr/bin/php")),
        mautic_run_as_user=str(runtime.get("mautic_run_as_user")).strip() if runtime.get("mautic_run_as_user") else "www-data",
        command_timeout_sec=int(runtime.get("command_timeout_sec", 0)),
        state_db_path=str(runtime.get("state_db_path", "/opt/mcd/var/mcd-state.db")),
        state_backend=state_backend,
        state_mysql_host=(
            str(runtime.get("state_mysql_host", state.get("mysql_host", ""))).strip()
            or None
        ),
        state_mysql_unix_socket=(
            str(runtime.get("state_mysql_unix_socket", state.get("mysql_unix_socket", ""))).strip()
            or None
        ),
        state_mysql_port=int(runtime.get("state_mysql_port", state.get("mysql_port", 3306))),
        state_mysql_database=(
            str(runtime.get("state_mysql_database", state.get("mysql_database", "mcd_state"))).strip()
            or None
        ),
        state_mysql_user=(
            str(runtime.get("state_mysql_user", state.get("mysql_user", ""))).strip()
            or None
        ),
        state_mysql_password=(
            str(runtime.get("state_mysql_password", state.get("mysql_password", "")))
            if (runtime.get("state_mysql_password") is not None or state.get("mysql_password") is not None)
            else None
        ),
        state_mysql_table_prefix=(
            str(runtime.get("state_mysql_table_prefix", state.get("mysql_table_prefix", "mcd_"))).strip()
            or "mcd_"
        ),
        state_mysql_connect_timeout_sec=int(
            runtime.get("state_mysql_connect_timeout_sec", state.get("mysql_connect_timeout_sec", 5))
        ),
        state_mysql_read_timeout_sec=int(
            runtime.get("state_mysql_read_timeout_sec", state.get("mysql_read_timeout_sec", 15))
        ),
        state_mysql_write_timeout_sec=int(
            runtime.get("state_mysql_write_timeout_sec", state.get("mysql_write_timeout_sec", 15))
        ),
        state_mysql_snapshot_enabled=bool(
            runtime.get("state_mysql_snapshot_enabled", state.get("mysql_snapshot_enabled", True))
        ),
        tasks_history_keep_days=int(runtime.get("tasks_history_keep_days", 2)),
        tasks_history_max_rows=int(runtime.get("tasks_history_max_rows", 25000)),
        tasks_compact_enabled=bool(runtime.get("tasks_compact_enabled", True)),
        tasks_compact_interval_sec=int(runtime.get("tasks_compact_interval_sec", 3600)),
        tasks_compact_quiet_hour=int(runtime.get("tasks_compact_quiet_hour", 3)),
        tasks_compact_quiet_window_min=int(runtime.get("tasks_compact_quiet_window_min", 60)),
        tasks_compact_vacuum=bool(runtime.get("tasks_compact_vacuum", True)),
        tasks_archive_enabled=bool(runtime.get("tasks_archive_enabled", True)),
        tasks_archive_dir=str(runtime.get("tasks_archive_dir", "/opt/mcd/var/task-history")),
        tasks_archive_keep_days=int(runtime.get("tasks_archive_keep_days", 14)),
        scheduler_pause_flag_path=str(runtime.get("scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause")),
        weights_recalc_interval_sec=int(runtime.get("weights_recalc_interval_sec", 86_400)),
        task_retry_max=int(runtime.get("task_retry_max", 1)),
        task_retry_delay_sec=int(runtime.get("task_retry_delay_sec", 2)),
        segment_retry_count=max(0, int(runtime.get("segment_retry_count", 2) or 0)),
        worker_watchdog_sec=int(runtime.get("worker_watchdog_sec", 0)),
        worker_stuck_policy=str(runtime.get("worker_stuck_policy", "skip")),
        worker_stuck_restart_limit=int(runtime.get("worker_stuck_restart_limit", 1)),
        jobs_max_workers=int(runtime.get("jobs_max_workers", 2)),
        scheduler_host_max_parallel=max(0, int(runtime.get("scheduler_host_max_parallel", 0))),
        scheduler_elastic_slots_enabled=bool(runtime.get("scheduler_elastic_slots_enabled", True)),
        scheduler_emergency_reserved_slots=max(
            0,
            int(runtime.get("scheduler_emergency_reserved_slots", 1) or 0),
        ),
        scheduler_instance_max_parallel=max(
            0,
            int(runtime.get("scheduler_instance_max_parallel", 0) or 0),
        ),
        scheduler_fairness_watchdog_sec=max(
            30,
            int(runtime.get("scheduler_fairness_watchdog_sec", 300) or 300),
        ),
        segment_whitelist=_normalize_int_list(runtime.get("segment_whitelist", [])),
        segment_whitelist_file=str(runtime.get("segment_whitelist_file")).strip() if runtime.get("segment_whitelist_file") else None,
        segment_whitelist_instance_settings=(
            dict(runtime.get("segment_whitelist_instance_settings", {}))
            if isinstance(runtime.get("segment_whitelist_instance_settings", {}), dict)
            else {}
        ),
        segment_priority=_normalize_int_list(runtime.get("segment_priority", [])),
        segment_super_priority=_normalize_int_list(runtime.get("segment_super_priority", [])),
        segment_priority_instance_settings=(
            dict(runtime.get("segment_priority_instance_settings", {}))
            if isinstance(runtime.get("segment_priority_instance_settings", {}), dict)
            else {}
        ),
        segment_super_priority_instance_settings=(
            dict(runtime.get("segment_super_priority_instance_settings", {}))
            if isinstance(runtime.get("segment_super_priority_instance_settings", {}), dict)
            else {}
        ),
        segment_stale_after_sec=max(
            0,
            int(runtime.get("segment_stale_after_sec", 24 * 60 * 60) or 0),
        ),
        segment_recurring_priority_v1=(
            dict(runtime.get("segment_recurring_priority_v1", {}))
            if isinstance(runtime.get("segment_recurring_priority_v1", {}), dict)
            else {}
        ),
        campaign_whitelist=_normalize_int_list(runtime.get("campaign_whitelist", [])),
        campaign_whitelist_file=str(runtime.get("campaign_whitelist_file")).strip() if runtime.get("campaign_whitelist_file") else None,
        campaign_whitelist_instance_settings=(
            dict(runtime.get("campaign_whitelist_instance_settings", {}))
            if isinstance(runtime.get("campaign_whitelist_instance_settings", {}), dict)
            else {}
        ),
        segment_priority_weight_threshold=float(runtime.get("segment_priority_weight_threshold", 60)),
        segment_priority_size=int(runtime.get("segment_priority_size", 5)),
        segment_mode=str(runtime.get("segment_mode", "id_weighted")),
        segment_priority_parallel_idle=int(runtime.get("segment_priority_parallel_idle", 4)),
        segment_regular_parallel_idle=int(runtime.get("segment_regular_parallel_idle", 1)),
        segment_full_scan_interval_sec=int(runtime.get("segment_full_scan_interval_sec", 300)),
        segment_periodic_full_scan_enabled=bool(runtime.get("segment_periodic_full_scan_enabled", False)),
        # Full published-segment cycles are the safe default.  The explicit
        # lazy flag opts into due-only planning and must not erase durable
        # remaining work already admitted in TaskStore.
        segment_lazy_update_enabled=bool(runtime.get("segment_lazy_update_enabled", False)),
        segment_lazy_update_instance_settings=(
            dict(runtime.get("segment_lazy_update_instance_settings", {}))
            if isinstance(runtime.get("segment_lazy_update_instance_settings", {}), dict)
            else {}
        ),
        segment_cycles_per_tick=int(runtime.get("segment_cycles_per_tick", 1)),
        segment_priority_parallel_throttled=int(runtime.get("segment_priority_parallel_throttled", 2)),
        segment_regular_parallel_throttled=int(runtime.get("segment_regular_parallel_throttled", 0)),
        segment_sql_ring_enabled=bool(runtime.get("segment_sql_ring_enabled", False)),
        segment_sql_auto_enabled=bool(runtime.get("segment_sql_auto_enabled", True)),
        segment_sql_auto_problem_threshold=max(1, int(runtime.get("segment_sql_auto_problem_threshold", 2) or 2)),
        segment_sql_auto_max_clauses=max(1, int(runtime.get("segment_sql_auto_max_clauses", 24) or 24)),
        segment_sql_auto_long_native_enabled=bool(runtime.get("segment_sql_auto_long_native_enabled", True)),
        segment_sql_auto_long_native_min_duration_sec=max(
            60,
            int(runtime.get("segment_sql_auto_long_native_min_duration_sec", 600) or 600),
        ),
        segment_sql_auto_long_native_history_sec=max(
            3600,
            int(runtime.get("segment_sql_auto_long_native_history_sec", 7 * 86400) or (7 * 86400)),
        ),
        segment_sql_auto_long_native_min_successes=max(
            1,
            int(runtime.get("segment_sql_auto_long_native_min_successes", 1) or 1),
        ),
        segment_sql_long_ring_max_per_tick=max(0, int(runtime.get("segment_sql_long_ring_max_per_tick", 1) or 0)),
        segment_sql_ring_max_per_tick=int(runtime.get("segment_sql_ring_max_per_tick", 1)),
        segment_sql_min_repeat_sec=max(0, int(runtime.get("segment_sql_min_repeat_sec", 3600) or 3600)),
        segment_sql_lock_heartbeat_sec=max(5, int(runtime.get("segment_sql_lock_heartbeat_sec", 15) or 15)),
        segment_sql_statement_timeout_sec=max(60, int(runtime.get("segment_sql_statement_timeout_sec", 1800) or 1800)),
        segment_sql_orphan_policy=segment_sql_orphan_policy,
        segment_sql_orphan_after_sec=max(30, int(runtime.get("segment_sql_orphan_after_sec", 900) or 900)),
        segment_sql_page_hits_quiet_only=bool(runtime.get("segment_sql_page_hits_quiet_only", True)),
        segment_sql_page_hits_quiet_hour=max(0, min(23, int(runtime.get("segment_sql_page_hits_quiet_hour", 2) or 2))),
        segment_sql_page_hits_quiet_window_min=max(
            1,
            min(720, int(runtime.get("segment_sql_page_hits_quiet_window_min", 180) or 180)),
        ),
        segment_sql_ring_rules=_normalize_json_dict(runtime.get("segment_sql_ring_rules", {})),
        segment_kill_mode=str(runtime.get("segment_kill_mode", "graceful")),
        segment_kill_grace_sec=int(runtime.get("segment_kill_grace_sec", 10)),
        priority_segment_timeout_sec=max(
            0,
            int(runtime.get("priority_segment_timeout_sec", 3600) or 0),
        ),
        campaign_priority_parallel=campaign_priority_parallel,
        campaign_regular_parallel=campaign_regular_parallel,
        campaign_total_parallel=campaign_total_parallel,
        campaign_priority_size=int(runtime.get("campaign_priority_size", 5)),
        campaign_latest_priority_count=int(runtime.get("campaign_latest_priority_count", 2)),
        enable_campaign_rebuild=bool(runtime.get("enable_campaign_rebuild", True)),
        campaign_rebuild_poll_interval_sec=int(runtime.get("campaign_rebuild_poll_interval_sec", 300)),
        campaign_rebuild_max_cycles_per_tick=int(runtime.get("campaign_rebuild_max_cycles_per_tick", 4)),
        campaign_rebuild_priority_parallel=int(runtime.get("campaign_rebuild_priority_parallel", 4)),
        campaign_rebuild_regular_parallel=int(runtime.get("campaign_rebuild_regular_parallel", 1)),
        campaign_native_fallback_enabled=bool(runtime.get("campaign_native_fallback_enabled", False)),
        campaign_native_fallback_interval_sec=max(
            300,
            int(runtime.get("campaign_native_fallback_interval_sec", 3600) or 3600),
        ),
        enable_contacts_cleanup=bool(runtime.get("enable_contacts_cleanup", False)),
        contacts_cleanup_interval_sec=int(runtime.get("contacts_cleanup_interval_sec", 86_400)),
        contacts_cleanup_quiet_hour=int(runtime.get("contacts_cleanup_quiet_hour", 2)),
        contacts_cleanup_quiet_window_min=int(runtime.get("contacts_cleanup_quiet_window_min", 60)),
        contacts_cleanup_email_field=str(runtime.get("contacts_cleanup_email_field", "email")),
        contacts_cleanup_phone_field=str(runtime.get("contacts_cleanup_phone_field", "mobile")),
        contacts_cleanup_mode=cleanup_mode,
        contacts_cleanup_max_delete_per_run=int(runtime.get("contacts_cleanup_max_delete_per_run", 10000)),
        enable_page_hits_orphan_cleanup=bool(runtime.get("enable_page_hits_orphan_cleanup", False)),
        page_hits_orphan_cleanup_interval_sec=max(
            60,
            int(runtime.get("page_hits_orphan_cleanup_interval_sec", 3600) or 3600),
        ),
        page_hits_orphan_cleanup_quiet_hour=max(
            0,
            min(23, int(runtime.get("page_hits_orphan_cleanup_quiet_hour", 2) or 2)),
        ),
        page_hits_orphan_cleanup_quiet_window_min=max(
            1,
            min(720, int(runtime.get("page_hits_orphan_cleanup_quiet_window_min", 180) or 180)),
        ),
        page_hits_orphan_cleanup_batch_size=max(
            100,
            int(runtime.get("page_hits_orphan_cleanup_batch_size", 5000) or 5000),
        ),
        page_hits_orphan_cleanup_batches_per_run=max(
            1,
            int(runtime.get("page_hits_orphan_cleanup_batches_per_run", 12) or 12),
        ),
        page_hits_orphan_cleanup_sleep_sec=max(
            0.0,
            float(runtime.get("page_hits_orphan_cleanup_sleep_sec", 0.2) or 0.2),
        ),
        page_hits_orphan_cleanup_grace_min=max(
            5,
            int(
                runtime.get(
                    "page_hits_orphan_cleanup_grace_min",
                    DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN,
                )
                or DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN
            ),
        ),
        page_hits_orphan_cleanup_max_run_sec=max(
            30,
            int(runtime.get("page_hits_orphan_cleanup_max_run_sec", 180) or 180),
        ),
        page_hits_orphan_cleanup_instance_settings=(
            dict(runtime.get("page_hits_orphan_cleanup_instance_settings", {}))
            if isinstance(runtime.get("page_hits_orphan_cleanup_instance_settings", {}), dict)
            else {}
        ),
        enable_mautic_lock_cleanup=bool(runtime.get("enable_mautic_lock_cleanup", True)),
        mautic_lock_cleanup_interval_sec=max(
            300,
            int(runtime.get("mautic_lock_cleanup_interval_sec", 3600) or 3600),
        ),
        mautic_lock_cleanup_quiet_hour=max(
            0,
            min(23, int(runtime.get("mautic_lock_cleanup_quiet_hour", 0) or 0)),
        ),
        mautic_lock_cleanup_quiet_window_min=max(
            1,
            min(1440, int(runtime.get("mautic_lock_cleanup_quiet_window_min", 1440) or 1440)),
        ),
        mautic_lock_cleanup_min_age_sec=max(
            1800,
            int(runtime.get("mautic_lock_cleanup_min_age_sec", 21600) or 21600),
        ),
        mautic_lock_cleanup_max_rows_per_run=max(
            1,
            int(runtime.get("mautic_lock_cleanup_max_rows_per_run", 20) or 20),
        ),
        enable_cache_clear=bool(runtime.get("enable_cache_clear", False)),
        cache_clear_interval_sec=int(runtime.get("cache_clear_interval_sec", 86_400)),
        cache_clear_quiet_hour=int(runtime.get("cache_clear_quiet_hour", 7)),
        cache_clear_quiet_window_min=int(runtime.get("cache_clear_quiet_window_min", 60)),
        enable_cache_warm=bool(runtime.get("enable_cache_warm", False)),
        cache_warm_interval_sec=int(runtime.get("cache_warm_interval_sec", 86_400)),
        cache_warm_quiet_hour=int(runtime.get("cache_warm_quiet_hour", 8)),
        cache_warm_quiet_window_min=int(runtime.get("cache_warm_quiet_window_min", 60)),
        plugin_operations=(
            dict(runtime.get("plugin_operations", {}))
            if isinstance(runtime.get("plugin_operations", {}), dict)
            else {}
        ),
        plugin_operation_instance_settings=(
            dict(runtime.get("plugin_operation_instance_settings", {}))
            if isinstance(runtime.get("plugin_operation_instance_settings", {}), dict)
            else {}
        ),
        # Internal migration input: catalog legacy_runtime mappings decide
        # which keys matter. This preserves old local plugin settings without
        # teaching the agent any plugin-specific names.
        plugin_operation_legacy_runtime=dict(runtime),
        message_queue_enabled=bool(runtime.get("message_queue_enabled", False)),
        message_queue_interval_sec=max(
            60,
            min(86_400, int(runtime.get("message_queue_interval_sec", 3600) or 3600)),
        ),
        message_queue_instance_settings=(
            dict(runtime.get("message_queue_instance_settings", {}))
            if isinstance(runtime.get("message_queue_instance_settings", {}), dict)
            else {}
        ),
        monitored_email_parser_enabled=bool(runtime.get("monitored_email_parser_enabled", False)),
        monitored_email_parser_interval_sec=max(60, int(runtime.get("monitored_email_parser_interval_sec", 900) or 900)),
        monitored_email_parser_batch_size=min(
            5000,
            max(1, int(runtime.get("monitored_email_parser_batch_size", 100) or 100)),
        ),
        monitored_email_parser_force_seen=bool(runtime.get("monitored_email_parser_force_seen", False)),
        monitored_email_parser_delete_processed=bool(runtime.get("monitored_email_parser_delete_processed", False)),
        monitored_email_parser_disable_mautic_fetch=bool(
            runtime.get("monitored_email_parser_disable_mautic_fetch", True)
        ),
        monitored_email_parser_types=(
            [
                str(x).strip()
                for x in runtime.get("monitored_email_parser_types", ["feedback_loop"])
                if str(x).strip()
            ]
            if isinstance(runtime.get("monitored_email_parser_types", ["feedback_loop"]), list)
            else ["feedback_loop"]
        ),
        monitored_email_parser_whitelist=(
            [
                str(x).strip().lower()
                for x in runtime.get("monitored_email_parser_whitelist", [])
                if str(x).strip()
            ]
            if isinstance(runtime.get("monitored_email_parser_whitelist", []), list)
            else []
        ),
        monitored_email_parser_instance_settings=(
            dict(runtime.get("monitored_email_parser_instance_settings", {}))
            if isinstance(runtime.get("monitored_email_parser_instance_settings", {}), dict)
            else {}
        ),
        form_embed_instance_settings=(
            dict(runtime.get("form_embed_instance_settings", {}))
            if isinstance(runtime.get("form_embed_instance_settings", {}), dict)
            else {}
        ),
        empty_leads_cleanup_enabled=bool(runtime.get("empty_leads_cleanup_enabled", False)),
        empty_leads_cleanup_interval_sec=max(60, int(runtime.get("empty_leads_cleanup_interval_sec", 900) or 900)),
        empty_leads_cleanup_batch_size=max(1, int(runtime.get("empty_leads_cleanup_batch_size", 5000) or 5000)),
        empty_leads_cleanup_max_batches_per_run=max(
            1,
            int(runtime.get("empty_leads_cleanup_max_batches_per_run", 10) or 10),
        ),
        empty_leads_cleanup_quiet_window_min=max(
            1,
            min(1440, int(runtime.get("empty_leads_cleanup_quiet_window_min", 660) or 660)),
        ),
        empty_leads_cleanup_max_runs_per_window=max(
            0,
            int(runtime.get("empty_leads_cleanup_max_runs_per_window", 0) or 0),
        ),
        empty_leads_cleanup_instance_settings=(
            dict(runtime.get("empty_leads_cleanup_instance_settings", {}))
            if isinstance(runtime.get("empty_leads_cleanup_instance_settings", {}), dict)
            else {}
        ),
        cluster_assets_enabled=bool(runtime.get("cluster_assets_enabled", False)),
        cluster_assets_interval_sec=max(60, int(runtime.get("cluster_assets_interval_sec", 600) or 600)),
        cluster_assets_state_file=str(
            runtime.get("cluster_assets_state_file", "/opt/mcd/var/cluster-assets-state.json")
        ),
        cluster_assets_fix_permissions=bool(runtime.get("cluster_assets_fix_permissions", True)),
        cluster_assets_reload_on_change=bool(runtime.get("cluster_assets_reload_on_change", True)),
        cluster_assets_cache_clear_on_change=bool(runtime.get("cluster_assets_cache_clear_on_change", True)),
        cluster_assets_cache_warm_on_change=bool(runtime.get("cluster_assets_cache_warm_on_change", True)),
        cluster_assets_fpm_reload_on_change=bool(runtime.get("cluster_assets_fpm_reload_on_change", True)),
        cluster_assets_reload_timeout_sec=max(60, int(runtime.get("cluster_assets_reload_timeout_sec", 300) or 300)),
        fs_permissions_guard_enabled=bool(runtime.get("fs_permissions_guard_enabled", True)),
        fs_permissions_guard_interval_sec=int(runtime.get("fs_permissions_guard_interval_sec", 300)),
        fs_permissions_guard_paths=fs_guard_paths,
        fs_permissions_guard_fix_console_exec=bool(runtime.get("fs_permissions_guard_fix_console_exec", True)),
        fs_permissions_guard_console_relpath=str(
            runtime.get("fs_permissions_guard_console_relpath", "bin/console")
        ).strip()
        or "bin/console",
        scheduler_reconcile_interval_sec=max(15, int(runtime.get("scheduler_reconcile_interval_sec", 60) or 60)),
        php_console_stuck_sec=max(60, int(runtime.get("php_console_stuck_sec", 1800) or 1800)),
        host_pressure_pause_enabled=bool(runtime.get("host_pressure_pause_enabled", True)),
        host_pressure_php_stuck_pause_threshold=max(
            0,
            int(runtime.get("host_pressure_php_stuck_pause_threshold", 8) or 8),
        ),
        host_pressure_swap_level_pause_threshold=max(
            0,
            min(2, int(runtime.get("host_pressure_swap_level_pause_threshold", 0) or 0)),
        ),
        db_watchdog=dict(db_watchdog_cfg),
        segment_batch_limit=int(runtime.get("segment_batch_limit", 1000)),
        campaign_batch_limit=int(runtime.get("campaign_batch_limit", 1000)),
        campaign_limit=_normalize_campaign_limit(runtime.get("campaign_limit", 0)),
        import_limit=int(runtime.get("import_limit", 1000)),
        enable_import_polling=bool(runtime.get("enable_import_polling", True)),
        import_poll_interval_sec=int(runtime.get("import_poll_interval_sec", 15)),
        import_stall_timeout_sec=max(60, int(runtime.get("import_stall_timeout_sec", 1800) or 1800)),
        queue_throttle_threshold=int(runtime.get("queue_throttle_threshold", 200)),
        queue_throttle_window_min=int(runtime.get("queue_throttle_window_min", 5)),
        sql_mail_queue_count=str(sql.get("mail_queue_count", "SELECT COUNT(*) AS cnt FROM {prefix}message_queue WHERE status = 'pending'")),
        sql_segments_due=_segments_due_sql(sql),
        sql_segment_weights=str(
            sql.get(
                "segment_weights",
                "SELECT ll.id, "
                "UNIX_TIMESTAMP(ll.date_added) AS created_ts, "
                "UNIX_TIMESTAMP(ll.date_modified) AS modified_ts, "
                "UNIX_TIMESTAMP(ll.last_built_date) AS built_ts, "
                "COALESCE(a.recent_cnt, 0) AS recent_activity "
                "FROM {prefix}lead_lists ll "
                "LEFT JOIN ("
                "  SELECT leadlist_id, COUNT(*) AS recent_cnt "
                "  FROM {prefix}lead_lists_leads "
                "  WHERE manually_removed = 0 "
                "    AND date_added >= '{window_start_utc_24h}' "
                "  GROUP BY leadlist_id"
                ") a ON a.leadlist_id = ll.id "
                "WHERE ll.is_published = 1",
            )
        ),
        sql_campaigns_due=str(
            sql.get(
                "campaigns_due",
                _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
            )
        ),
        sql_campaign_triggers_due=_campaign_triggers_due_sql(sql),
        sql_campaign_rebuilds_due=_campaign_rebuilds_due_sql(sql),
        sql_campaign_weights=str(
            sql.get(
                "campaign_weights",
                "SELECT c.id, UNIX_TIMESTAMP(COALESCE(c.publish_up, c.date_added)) AS publish_ts, "
                "COALESCE(w.pending_cnt, 0) AS pending_cnt, "
                "COALESCE(w.recent_cnt, 0) AS recent_activity "
                "FROM {prefix}campaigns c "
                "LEFT JOIN ("
                "  SELECT campaign_id, "
                "         SUM(CASE WHEN manually_removed = 0 AND date_last_exited IS NULL THEN 1 ELSE 0 END) AS pending_cnt, "
                "         SUM(CASE WHEN manually_removed = 0 AND date_added >= '{window_start_local_24h}' THEN 1 ELSE 0 END) AS recent_cnt "
                "  FROM {prefix}campaign_leads "
                "  GROUP BY campaign_id"
                ") w ON w.campaign_id = c.id "
                "WHERE c.is_published = 1",
            )
        ),
        sql_import_pending_count=_import_pending_count_sql(sql),
        cmd_segment_update_template=str(
            commands.get(
                "segment_update_template",
                "mautic:segments:update -i {id} --batch-limit={batch_limit}",
            )
        ),
        cmd_segment_full_update_template=str(
            commands.get(
                "segment_full_update_template",
                "mautic:segments:update --batch-limit={batch_limit}",
            )
        ),
        cmd_campaign_update_template=str(
            commands.get(
                "campaign_update_template",
                "mautic:campaigns:update -i {id}",
            )
        ),
        cmd_campaign_trigger_template=_normalize_campaign_trigger_template(
            commands.get(
                "campaign_trigger_template",
                "mautic:campaigns:trigger -i {id}{campaign_limit_arg} --batch-limit={batch_limit}",
            )
        ),
        cmd_campaign_rebuild_template=str(
            commands.get(
                "campaign_rebuild_template",
                "mautic:campaigns:rebuild -i {id}",
            )
        ),
        cmd_import_template=str(
            commands.get(
                "import_template",
                "mautic:import --limit={import_limit}",
            )
        ),
        cmd_cache_clear_template=str(commands.get("cache_clear_template", "cache:clear")),
        cmd_cache_warm_template=str(commands.get("cache_warm_template", "cache:warmup")),
        mcc_url=str(mcc.get("url")) if mcc.get("url") else None,
        mcc_token=str(mcc.get("token")) if mcc.get("token") else None,
        mcc_push_enabled=bool(mcc.get("push_enabled", True)),
        mcc_push_interval_sec=int(mcc.get("push_interval_sec", 300)),
        mcc_push_on_change=bool(mcc.get("push_on_change", False)),
        mcc_push_alert_poll_interval_sec=int(mcc.get("push_alert_poll_interval_sec", 60)),
        mcc_push_alert_window_min=int(mcc.get("push_alert_window_min", 5)),
        mcc_push_apt_state_interval_sec=int(mcc.get("push_apt_state_interval_sec", 120)),
        mcc_runtime_overrides_poll_enabled=bool(mcc.get("runtime_overrides_poll_enabled", True)),
        mcc_profile_guard_enabled=profile_guard_enabled,
        inventory_auto_rescan_enabled=bool(runtime.get("inventory_auto_rescan_enabled", True)),
        inventory_auto_rescan_interval_sec=max(
            300,
            int(runtime.get("inventory_auto_rescan_interval_sec", 3600) or 3600),
        ),
        outbound_events_sent_keep_days=int(runtime.get("outbound_events_sent_keep_days", 14)),
        mcc_host_name=str(mcc.get("host_name")).strip() if mcc.get("host_name") else None,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        cluster_node_role=cluster_node_role,
        cluster_node_index=cluster_node_index,
        cluster_routing_enabled=cluster_routing_enabled,
        cluster_route_cron_host=cluster_route_cron_host,
        cluster_route_import_host=cluster_route_import_host,
        cluster_route_backup_host=cluster_route_backup_host,
        cluster_route_cache_hosts=cluster_route_cache_hosts,
        cluster_replica_freshness_enabled=bool(runtime.get("cluster_replica_freshness_enabled", False)),
        cluster_replica_freshness_max_age_sec=max(
            300,
            int(runtime.get("cluster_replica_freshness_max_age_sec", 172800) or 172800),
        ),
        cluster_replica_freshness_checks=cluster_replica_freshness_checks,
        host_template=bool(runtime.get("host_template", False)),
        template_autopromote_on_clone=bool(runtime.get("template_autopromote_on_clone", True)),
        mcc_mcd_manifest_url=str(mcc.get("mcd_manifest_url")) if mcc.get("mcd_manifest_url") else None,
        plugins_repo_base_url=str(plugins.get("repo_base_url", "https://servercontrol.sales-snap.com")).rstrip("/"),
        plugins_repo_fallback_ip=str(plugins.get("repo_fallback_ip")).strip() if plugins.get("repo_fallback_ip") else None,
        plugins_manifest_path_template=str(
            plugins.get("manifest_path_template", "/mauticctl/packages/mautic{major}/manifest.json")
        ),
        plugins_post_cache_clear=bool(plugins.get("post_cache_clear", True)),
        plugins_post_install=bool(plugins.get("post_plugin_install", True)),
        plugins_state_filename=str(plugins.get("state_filename", ".mcd-plugin.json")),
        plugins_cluster_file_sync_wait_sec=max(
            30,
            int(plugins.get("cluster_file_sync_wait_sec", 900) or 900),
        ),
        plugins_cluster_cache_clear_wait_sec=max(
            60,
            int(plugins.get("cluster_cache_clear_wait_sec", 600) or 600),
        ),
        custom_repo_base_url=str(custom.get("repo_base_url")).rstrip("/") if custom.get("repo_base_url") else (
            str(mcc.get("url")).rstrip("/") if mcc.get("url") else None
        ),
        custom_manifest_path=str(custom.get("manifest_path", "/mauticctl/custom/manifest.json")),
        custom_cache_dir=str(custom.get("cache_dir", "/opt/mcd/var/custom")),
        custom_run_mode_default=str(custom.get("run_mode_default", "auto")).strip().lower() or "auto",
        custom_prefer_tmux=bool(custom.get("prefer_tmux", True)),
        custom_prefer_screen=bool(custom.get("prefer_screen", True)),
        custom_tmux_session_prefix=str(custom.get("tmux_session_prefix", "mcd-custom")),
        custom_cache_cleanup_enabled=bool(runtime.get("custom_cache_cleanup_enabled", custom.get("cleanup_enabled", True))),
        custom_cache_cleanup_interval_sec=int(
            runtime.get("custom_cache_cleanup_interval_sec", custom.get("cleanup_interval_sec", 86_400))
        ),
        custom_cache_cleanup_quiet_hour=int(
            runtime.get("custom_cache_cleanup_quiet_hour", custom.get("cleanup_quiet_hour", 3))
        ),
        custom_cache_cleanup_quiet_window_min=int(
            runtime.get("custom_cache_cleanup_quiet_window_min", custom.get("cleanup_quiet_window_min", 60))
        ),
        custom_logs_keep_days=int(runtime.get("custom_logs_keep_days", custom.get("logs_keep_days", 14))),
        custom_logs_max_files=int(runtime.get("custom_logs_max_files", custom.get("logs_max_files", 200))),
        custom_downloads_keep_days=int(
            runtime.get("custom_downloads_keep_days", custom.get("downloads_keep_days", 30))
        ),
        custom_downloads_max_entries=int(
            runtime.get("custom_downloads_max_entries", custom.get("downloads_max_entries", 200))
        ),
        backup_enabled=bool(backup.get("enabled", False)),
        backup_method=str(backup.get("method", "mydumper")).strip().lower() or "mydumper",
        backup_auto_install_packages=bool(backup_packages.get("auto_install", backup.get("auto_install_packages", True))),
        backup_state_dir=str(backup.get("state_dir", "/opt/mcd/var/state/backup")),
        backup_lock_dir=str(backup.get("lock_dir", "/opt/mcd/var/locks")),
        backup_mount_base_dir=str(backup.get("mount_base_dir", "/opt/mcd/var/backup/mounts")),
        backup_storage_kind=str(backup_storage.get("kind", "sftp")).strip().lower() or "sftp",
        backup_local_path=str(backup_storage.get("local_path", "")).strip(),
        backup_local_require_mount=bool(backup_storage.get("require_mount", True)),
        backup_remote_root_dir=str(backup.get("remote_root_dir", "backup")),
        backup_host_name=str(backup.get("host_name")).strip() if backup.get("host_name") else None,
        backup_instance_name=str(backup.get("instance_name")).strip() if backup.get("instance_name") else None,
        backup_retention_copies=int(backup.get("retention_copies", 10)),
        backup_mount_timeout_sec=int(backup.get("mount_timeout_sec", 15)),
        backup_unmount_timeout_sec=int(backup.get("unmount_timeout_sec", 10)),
        backup_dump_timeout_sec=_normalize_backup_dump_timeout(backup.get("dump_timeout_sec", 10_800)),
        backup_archive_enabled=bool(backup_archive.get("enabled", True)),
        backup_archive_name=str(backup_archive.get("name", "files.tar.gz")),
        backup_archive_paths=_normalize_list(
            backup_archive.get(
                "paths",
                [
                    "/etc/mysql",
                    "/etc/nginx",
                    "/etc/php",
                    "/etc/systemd/system",
                    "/etc/cron.d",
                    "/var/www",
                    "/opt/mcd/etc",
                    "/var/spool/cron",
                ],
            )
        ),
        backup_secrets_key_path=str(backup_secrets.get("key_path", "/opt/mcd/var/keys/backup-secrets.key")),
        backup_ssh_password_ref=str(backup_storage.get("password_ref")).strip() if backup_storage.get("password_ref") else None,
        backup_ssh_host=str(backup_storage.get("host", "")).strip(),
        backup_ssh_port=int(backup_storage.get("port", 22)),
        backup_ssh_user=str(backup_storage.get("user", "")).strip(),
        backup_ssh_remote_path=str(backup_storage.get("remote_path", "/")).strip() or "/",
        backup_ssh_key_file=str(backup_storage.get("key_file")).strip() if backup_storage.get("key_file") else None,
        backup_ssh_password=str(backup_storage.get("password")) if backup_storage.get("password") else None,
        backup_mysql_host=str(backup_mysql.get("host")).strip() if backup_mysql.get("host") else None,
        backup_mysql_port=int(backup_mysql["port"]) if backup_mysql.get("port") is not None else None,
        backup_mysql_user=str(backup_mysql.get("user")).strip() if backup_mysql.get("user") else None,
        backup_mysql_password=str(backup_mysql.get("password")) if backup_mysql.get("password") else None,
        backup_mysql_password_ref=str(backup_mysql.get("password_ref")).strip() if backup_mysql.get("password_ref") else None,
        backup_mysql_database=str(backup_mysql.get("database")).strip() if backup_mysql.get("database") else None,
        backup_sshfs_package=str(backup_packages.get("sshfs", "sshfs")).strip() or "sshfs",
        backup_mydumper_package=str(backup_packages.get("mydumper", "mydumper")).strip() or "mydumper",
        backup_mydumper_bin=str(backup_mydumper.get("bin", "/usr/bin/mydumper")),
        backup_mydumper_threads=int(backup_mydumper.get("threads", 6)),
        backup_mydumper_verbose=int(backup_mydumper.get("verbose", 3)),
        backup_mydumper_compress=bool(backup_mydumper.get("compress", True)),
        backup_mydumper_long_query_guard=int(backup_mydumper.get("long_query_guard", 0)),
        backup_mydumper_kill_long_queries=bool(backup_mydumper.get("kill_long_queries", False)),
        backup_mydumper_extra_args=_normalize_list(backup_mydumper.get("extra_args", [])),
        backup_mydumper_use_nice=bool(backup_mydumper.get("use_nice", True)),
        backup_mydumper_nice_level=int(backup_mydumper.get("nice_level", 15)),
        backup_mydumper_use_ionice=bool(backup_mydumper.get("use_ionice", True)),
        backup_mydumper_ionice_class=int(backup_mydumper.get("ionice_class", 2)),
        backup_mydumper_ionice_level=int(backup_mydumper.get("ionice_level", 7)),
        backup_myloader_bin=str(backup_mydumper.get("myloader_bin", "/usr/bin/myloader")),
        backup_myloader_threads=int(backup_mydumper.get("myloader_threads", 4)),
        backup_xtrabackup_package=str(backup_packages.get("xtrabackup", backup_xtrabackup.get("package", "percona-xtrabackup-80"))).strip()
        or "percona-xtrabackup-80",
        backup_xtrabackup_bin=str(backup_xtrabackup.get("bin", "/usr/bin/xtrabackup")),
        backup_xtrabackup_parallel=int(backup_xtrabackup.get("parallel", 4)),
        backup_xtrabackup_extra_args=_normalize_list(backup_xtrabackup.get("extra_args", [])),
        backup_xtrabackup_incremental_enabled=bool(backup_xtrabackup.get("incremental_enabled", True)),
        backup_xtrabackup_full_interval_days=max(1, int(backup_xtrabackup.get("full_interval_days", 7))),
        backup_xtrabackup_retention_full_copies=max(1, int(backup_xtrabackup.get("retention_full_copies", 3))),
        backup_xtrabackup_retention_incremental_days=max(
            1,
            int(backup_xtrabackup.get("retention_incremental_days", 7)),
        ),
        backup_cluster_enabled=bool(backup_cluster.get("enabled", False)),
        backup_cluster_storage_mode=(
            str(backup_cluster.get("storage_mode", "local")).strip().lower() or "local"
        ),
        backup_cluster_local_xtrabackup_enabled=bool(
            backup_cluster.get("local_xtrabackup_enabled", True)
        ),
        backup_cluster_local_root_dir=str(
            backup_cluster.get("local_root_dir", "/mnt/data/backup/local/ananasrs")
        ).strip()
        or "/mnt/data/backup/local/ananasrs",
        backup_cluster_full_hour=max(0, min(23, int(backup_cluster.get("full_hour", 1)))),
        backup_cluster_full_minute=max(0, min(59, int(backup_cluster.get("full_minute", 0)))),
        backup_cluster_offsite_not_before_hour=max(
            0,
            min(23, int(backup_cluster.get("offsite_not_before_hour", 2))),
        ),
        backup_cluster_offsite_not_before_minute=max(
            0,
            min(59, int(backup_cluster.get("offsite_not_before_minute", 0))),
        ),
        backup_cluster_incremental_start_hour=max(
            0,
            min(23, int(backup_cluster.get("incremental_start_hour", 8))),
        ),
        backup_cluster_incremental_end_hour=max(
            0,
            min(23, int(backup_cluster.get("incremental_end_hour", 20))),
        ),
        backup_cluster_incremental_interval_sec=max(
            300,
            int(backup_cluster.get("incremental_interval_sec", 7200)),
        ),
        backup_cluster_incremental_min_free_bytes=max(
            0,
            int(backup_cluster.get("incremental_min_free_bytes", 300 * 1024 * 1024 * 1024)),
        ),
        backup_cluster_offsite_source=(
            str(backup_cluster.get("offsite_source", "xtrabackup")).strip().lower() or "xtrabackup"
        ),
        backup_cluster_files_snapshot_enabled=bool(backup_cluster.get("files_snapshot_enabled", True)),
        backup_cluster_files_snapshot_paths=_normalize_list(
            backup_cluster.get(
                "files_snapshot_paths",
                backup_archive.get("paths", []),
            )
        ),
        backup_cluster_files_snapshot_exclude=_normalize_list(
            backup_cluster.get(
                "files_snapshot_exclude",
                _DEFAULT_CLUSTER_FILE_EXCLUDES,
            )
        ),
        backup_cluster_files_transport=str(backup_cluster.get("files_transport", "syncthing")).strip().lower()
        or "syncthing",
        backup_cluster_files_sync_dir=str(
            backup_cluster.get("files_sync_dir", "/opt/mcd/var/cluster-backup-files")
        ).strip()
        or "/opt/mcd/var/cluster-backup-files",
        backup_cluster_files_node_paths=_normalize_list(
            backup_cluster.get(
                "files_node_paths",
                _DEFAULT_CLUSTER_FILE_NODE_PATHS,
            )
        ),
        backup_cluster_files_shared_paths=_normalize_list(
            backup_cluster.get("files_shared_paths", _DEFAULT_CLUSTER_FILE_SHARED_PATHS)
        ),
        backup_cluster_files_shared_producer_host=(
            str(backup_cluster.get("files_shared_producer_host")).strip()
            if backup_cluster.get("files_shared_producer_host")
            else None
        ),
        backup_cluster_files_expected_nodes=_normalize_list(backup_cluster.get("files_expected_nodes", [])),
        backup_cluster_files_layer_max_age_sec=max(
            300,
            int(backup_cluster.get("files_layer_max_age_sec", 86400) or 86400),
        ),
        backup_cluster_files_produce_interval_sec=max(
            300,
            int(backup_cluster.get("files_produce_interval_sec", 3600) or 3600),
        ),
        backup_cluster_remote_enabled=bool(backup_cluster.get("remote_enabled", True)),
        backup_cluster_remote_retention_daily=max(1, int(backup_cluster.get("remote_retention_daily", 7))),
        backup_cluster_remote_retention_weekly=max(0, int(backup_cluster.get("remote_retention_weekly", 4))),
        backup_cluster_authority_role=backup_cluster_authority_role,
        backup_cluster_authority_host=backup_cluster_authority_host,
        backup_restore_apply_files=bool(backup.get("restore_apply_files", True)),
        backup_restore_apply_databases=bool(backup.get("restore_apply_databases", True)),
        backup_schedule_enabled=bool(backup_schedule.get("enabled", False)),
        backup_schedule_interval_sec=int(backup_schedule.get("interval_sec", 86_400)),
        backup_schedule_quiet_hour=int(backup_schedule.get("quiet_hour", 2)),
        backup_schedule_quiet_minute=int(backup_schedule.get("quiet_minute", 0)),
        backup_schedule_quiet_window_min=int(backup_schedule.get("quiet_window_min", 60)),
        backup_schedule_pre_pause_sec=int(backup_schedule.get("pre_pause_sec", 3600)),
        mcd_update_notify=bool(runtime.get("mcd_update_notify", True)),
        mcd_auto_update_enabled=bool(runtime.get("mcd_auto_update_enabled", True)),
        mcd_update_check_interval_sec=int(runtime.get("mcd_update_check_interval_sec", 3600)),
        mcd_update_channel=str(runtime.get("mcd_update_channel", "approved")).strip() or "approved",
        mcd_update_policy=mcd_update_policy,
        mcd_update_allow_test_build=bool(runtime.get("mcd_update_allow_test_build", False)),
        mcd_update_wait_retry_sec=int(runtime.get("mcd_update_wait_retry_sec", 60)),
        mcd_update_defer_during_campaigns=bool(runtime.get("mcd_update_defer_during_campaigns", True)),
        mcd_update_cleanup_enabled=bool(runtime.get("mcd_update_cleanup_enabled", True)),
        mcd_update_cleanup_interval_sec=int(runtime.get("mcd_update_cleanup_interval_sec", 86_400)),
        mcd_update_keep_archives=int(runtime.get("mcd_update_keep_archives", 0)),
        mcd_update_keep_preupdate_backups=int(runtime.get("mcd_update_keep_preupdate_backups", 0)),
        mcd_update_artifacts_max_age_days=int(runtime.get("mcd_update_artifacts_max_age_days", 30)),
        mcd_config_history_limit=int(runtime.get("mcd_config_history_limit", 10)),
        service_profiles_enabled=bool(runtime.get("service_profiles_enabled", True)),
        service_profiles_auto_apply=bool(runtime.get("service_profiles_auto_apply", True)),
        service_profiles_poll_interval_sec=int(runtime.get("service_profiles_poll_interval_sec", 3600)),
        service_profiles_components=_normalize_service_profile_components(
            runtime.get("service_profiles_components", ["php_fpm", "mysql", "apt", "wazuh", "mautic_db_indexes"])
        ),
        mautic6_core_patch_policy=m6_patch_policy,
        mautic6_core_patch_version_min=m6_patch_min,
        mautic6_core_patch_version_max=m6_patch_max,
        mautic6_core_patch_apply_if_version_unknown=m6_patch_unknown,
        pagehit_cascade_patch_policy=pagehit_patch_policy,
        profile_name=profile_name,
        hardware_profile_name=hardware_profile_name,
        ring_mode=ring_mode,
        disable_throttle=disable_throttle,
        disable_whitelist=disable_whitelist,
        segment_throttle_during_campaigns=segment_throttle_during_campaigns,
        campaign_pressure_min_running_sec=campaign_pressure_min_running_sec,
        campaign_pressure_min_running_count=campaign_pressure_min_running_count,
        segment_throttle_whitelist_only=segment_throttle_whitelist_only,
        segment_throttle_whitelist_parallel=segment_throttle_whitelist_parallel,
        segment_throttle_kill_non_whitelist=segment_throttle_kill_non_whitelist,
        campaign_update_priority_parallel=campaign_update_priority_parallel,
        campaign_update_regular_parallel=campaign_update_regular_parallel,
        campaign_trigger_priority_parallel=campaign_trigger_priority_parallel,
        campaign_trigger_regular_parallel=campaign_trigger_regular_parallel,
        campaign_trigger_min_repeat_sec=max(0, campaign_trigger_min_repeat_sec),
        campaign_trigger_audit_interval_sec=max(0, campaign_trigger_audit_interval_sec),
        campaign_trigger_due_guard_enabled=campaign_trigger_due_guard_enabled,
        campaign_trigger_progress_watchdog_enabled=campaign_trigger_progress_watchdog_enabled,
        campaign_trigger_progress_watchdog_grace_sec=campaign_trigger_progress_watchdog_grace_sec,
        campaign_trigger_progress_watchdog_interval_sec=campaign_trigger_progress_watchdog_interval_sec,
        campaign_trigger_progress_watchdog_stable_checks=campaign_trigger_progress_watchdog_stable_checks,
        campaign_rebuild_min_repeat_sec=max(0, campaign_rebuild_min_repeat_sec),
    )
    if cfg.disable_whitelist:
        cfg = replace(
            cfg,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_priority=[],
            segment_super_priority=[],
            segment_priority_instance_settings={},
            segment_super_priority_instance_settings={},
            campaign_whitelist=[],
            campaign_whitelist_file=None,
        )
    profiled = _apply_profile(cfg)
    merged = _reapply_manual_runtime_overrides(profiled, runtime)
    merged = _enforce_campaign_rebuild_guard(merged)
    merged = _enforce_campaign_trigger_audit_policy(merged)
    merged = _enforce_profile_guards(merged)
    if merged.disable_whitelist:
        merged = replace(
            merged,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_priority=[],
            segment_super_priority=[],
            segment_priority_instance_settings={},
            segment_super_priority_instance_settings={},
            campaign_whitelist=[],
            campaign_whitelist_file=None,
        )
    # Runtime customization flag is not "runtime section exists"; it is
    # "effective config diverges from selected profile baseline".
    profile_lower = (profile_name or "").strip().lower()
    if profile_lower in {"", "custom"}:
        customized = bool(isinstance(runtime, dict) and len(runtime.keys()) > 0)
    else:
        customized = _is_runtime_customized_against_profile(profiled, merged)
    merged = replace(merged, config_customized=customized)
    _record_config_history(merged)
    return merged


def load_config(path: str, *, allow_recover_from_mcc: bool = True) -> AgentConfig:
    # Always sanitize legacy runtime keys on disk before parse to avoid
    # re-applying stale pre-profile settings after upgrades.
    try:
        _auto_migrate_legacy_runtime_keys(path)
    except Exception:
        pass
    try:
        _auto_migrate_legacy_sql_defaults(path)
    except Exception:
        pass

    try:
        return _load_config_inner(path)
    except Exception as first_error:
        if not allow_recover_from_mcc:
            raise
        ok, note = _attempt_recover_config_from_mcc(path, load_error=first_error)
        if not ok:
            raise RuntimeError(f"config load failed: {first_error}; mcc recover failed: {note}") from first_error
        return _load_config_inner(path)
