from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mcd_agent.config import (
    AgentConfig,
    check_profile_drift_with_mcc,
    load_config,
    recover_config_from_mcc,
    runtime_effective_map,
    upsert_runtime_values,
)
from mcd_agent.backup import (
    backup_lock_active,
    backup_profile_sync_from_config,
    backup_run,
    backup_status,
    backup_storage_probe,
    cluster_backup_authority_status,
    cluster_backup_files_produce,
    cluster_backup_files_snapshot,
    cluster_backup_local_full,
    cluster_backup_local_incremental,
    cluster_backup_offsite,
    cluster_backup_status,
)
from mcd_agent.cluster_routing import (
    cluster_local_identity_values,
    cluster_route_allows,
    cluster_route_authority_status,
)
from mcd_agent.cleanup_schedule import (
    cleanup_session_key as _cleanup_session_key,
    select_fair_cleanup_task as _select_fair_cleanup_task,
    window_minutes as _window_minutes,
)
from mcd_agent.cluster_assets import guard_cluster_assets
from mcd_agent.custom_scripts import cached_custom_manifest_keys, cleanup_custom_cache, fetch_custom_manifest
from mcd_agent.db import MauticDB
from mcd_agent.db_watchdog import collect_db_watchdog_snapshot, effective_db_watchdog_config
from mcd_agent.executor import render_mautic_command
from mcd_agent.fs_permissions import ensure_instance_permissions
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.mautic6_core_patch import ensure_m6_plugin_update_metadata_patch, should_apply_m6_plugin_update_metadata_patch
from mcd_agent.mautic_core_restore import restore_retired_mcd_core_patches
from mcd_agent.mautic_version_cache import (
    install_zabbix_mautic_version_userparameter,
    refresh_mautic_version_cache,
)
from mcd_agent.mode import (
    reconcile_empty_leads_cleanup_cron,
    reconcile_mautic_email_fetch_cron,
    reconcile_viber_stats_cron,
)
from mcd_agent.monitored_email import (
    ALLOWED_TYPES as MONITORED_EMAIL_ALLOWED_TYPES,
    MonitoredEmailParserSettings,
    monitored_email_state_key,
    process_monitored_email,
)
from mcd_agent.runtime_overrides import (
    apply_remote_overrides,
    consume_poll_trigger,
    fetch_runtime_overrides,
    local_runtime_overrides,
    overrides_fingerprint,
    push_runtime_overrides,
)
from mcd_agent.ring_utils import advance_ring_after_launch as _advance_ring_after_launch
from mcd_agent.ring_utils import mark_ring_entity_executed as _mark_ring_entity_executed
from mcd_agent.ring_utils import reconcile_ring as _reconcile_ring
from mcd_agent.service_profiles import service_profiles_apply_once
from mcd_agent.self_update import maybe_auto_update
from mcd_agent.segment_filter_safety import format_segment_filter_issues, segment_invalid_filter_issues
from mcd_agent.segment_sql_auto import DetectedSQLSegmentRule, detect_auto_sql_segment_rules
from mcd_agent.segment_dependencies import (
    segment_dependency_blocked_ids,
    segment_dependency_maps,
    suppress_mautic_cascade_dependencies,
    stale_dependent_segment_closure,
)
from mcd_agent.signals import collect_signals
from mcd_agent.state_push import (
    MCCStatePusher,
    clear_pending_profile_event,
    log_push_result,
    prune_sent_profile_events,
    queue_profile_event,
    read_pending_profile_event,
    should_poll_alert,
)
from mcd_agent.sql_time import campaign_sql_time_context, mautic_local_datetime
from mcd_agent.state_backend import (
    ensure_mysql_state_schema,
    mysql_state_connection,
    mysql_state_enabled,
    mysql_state_table_names,
    normalized_state_backend,
)

_CMD_SEP = "\x1f"
_M4_CAMPAIGN_DELETED_CLAUSE_RE = re.compile(r"\s+AND\s*\(?\s*c\.deleted\s+IS\s+NULL\s*\)?", re.IGNORECASE)
_SEGMENT_STALE_PRIORITY_SEC = 24 * 3600
_SEGMENT_STUCK_SPILLOVER_SEC = 2 * 3600
_SEGMENT_FAILURE_COOLDOWN_SEC = 3600
_SEGMENT_FAILURE_COOLDOWN_THRESHOLD = 3
_SEGMENT_AUTOMATIC_RETRY_CAP = 3
_DB_DISPATCH_PAUSE_SEC = 120
_DB_WATCHDOG_LONG_QUERIES_PAUSE_THRESHOLD = 50
_DB_WATCHDOG_METADATA_LOCKS_PAUSE_THRESHOLD = 10
_SEGMENT_DEPENDENCY_FINISH_WINDOW_SEC = 2 * 3600
_ENTITY_LAUNCH_GUARD: dict[str, float] = {}
_SEGMENT_FINISHED_AT: dict[tuple[str, int], float] = {}
_SEGMENT_FILTER_WARN_TS: dict[tuple[str, str], float] = {}
_SEGMENT_FAILURE_WARN_TS: dict[tuple[str, int], float] = {}
_IMPORT_SETTLE_UNTIL: dict[str, float] = {}
_IMPORT_PENDING_OVERRIDE_WARN_TS: dict[str, float] = {}
_SQL_IMPORT_PENDING_STATUS_COUNT = (
    "SELECT COUNT(*) AS cnt FROM {prefix}imports "
    "WHERE is_published = 1 "
    "AND (status IN (1,2,7) "
    "OR CAST(status AS CHAR) IN ('pending','in_progress','delayed')) "
    "AND (date_started IS NULL "
    "OR CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(properties, '$.line')), '1') AS UNSIGNED) <= line_count)"
)
_SQL_SEGMENTS_ALL_PUBLISHED = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)
_SQL_CAMPAIGNS_ALL_PUBLISHED = (
    "SELECT c.id "
    "FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
    "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
    "ORDER BY c.id"
)

_BACKUP_STABLE_RUNTIME_KEYS = {
    "backup_enabled",
    "backup_method",
    "backup_auto_install_packages",
    "backup_dump_timeout_sec",
    "backup_schedule_enabled",
    "backup_schedule_interval_sec",
    "backup_schedule_quiet_hour",
    "backup_schedule_quiet_minute",
    "backup_schedule_quiet_window_min",
    "backup_schedule_pre_pause_sec",
    "backup_mydumper_threads",
    "backup_mydumper_long_query_guard",
    "backup_mydumper_kill_long_queries",
    "backup_mydumper_extra_args",
    "backup_mydumper_use_nice",
    "backup_mydumper_nice_level",
    "backup_mydumper_use_ionice",
    "backup_mydumper_ionice_class",
    "backup_mydumper_ionice_level",
    "backup_xtrabackup_parallel",
    "backup_xtrabackup_extra_args",
    "backup_xtrabackup_incremental_enabled",
    "backup_xtrabackup_full_interval_days",
    "backup_xtrabackup_retention_full_copies",
    "backup_xtrabackup_retention_incremental_days",
    "backup_cluster_enabled",
    "backup_cluster_local_root_dir",
    "backup_cluster_full_hour",
    "backup_cluster_full_minute",
    "backup_cluster_offsite_not_before_hour",
    "backup_cluster_offsite_not_before_minute",
    "backup_cluster_incremental_start_hour",
    "backup_cluster_incremental_end_hour",
    "backup_cluster_incremental_interval_sec",
    "backup_cluster_incremental_min_free_bytes",
    "backup_cluster_files_snapshot_enabled",
    "backup_cluster_files_snapshot_paths",
    "backup_cluster_files_snapshot_exclude",
    "backup_cluster_files_transport",
    "backup_cluster_files_sync_dir",
    "backup_cluster_files_node_paths",
    "backup_cluster_files_shared_paths",
    "backup_cluster_files_shared_producer_host",
    "backup_cluster_files_expected_nodes",
    "backup_cluster_files_layer_max_age_sec",
    "backup_cluster_files_produce_interval_sec",
    "backup_cluster_remote_enabled",
    "backup_cluster_remote_retention_daily",
    "backup_cluster_remote_retention_weekly",
    "backup_cluster_authority_role",
    "backup_cluster_authority_host",
}
_VIBER_STATS_STABLE_RUNTIME_KEYS = {
    "viber_stats_enabled",
    "viber_stats_interval_sec",
    "viber_stats_instance_settings",
}
_SEGMENT_WHITELIST_STABLE_RUNTIME_KEYS = {
    "segment_whitelist_instance_settings",
}
_EMPTY_LEADS_CLEANUP_STABLE_RUNTIME_KEYS = {
    "empty_leads_cleanup_enabled",
    "empty_leads_cleanup_interval_sec",
    "empty_leads_cleanup_batch_size",
    "empty_leads_cleanup_max_batches_per_run",
    "empty_leads_cleanup_quiet_window_min",
    "empty_leads_cleanup_max_runs_per_window",
    "empty_leads_cleanup_instance_settings",
}
_PAGE_HITS_ORPHAN_CLEANUP_STABLE_RUNTIME_KEYS = {
    "enable_page_hits_orphan_cleanup",
    "page_hits_orphan_cleanup_interval_sec",
    "page_hits_orphan_cleanup_quiet_hour",
    "page_hits_orphan_cleanup_quiet_window_min",
    "page_hits_orphan_cleanup_batch_size",
    "page_hits_orphan_cleanup_batches_per_run",
    "page_hits_orphan_cleanup_sleep_sec",
    "page_hits_orphan_cleanup_grace_min",
    "page_hits_orphan_cleanup_max_run_sec",
    "page_hits_orphan_cleanup_instance_settings",
}
_HOUSEKEEPING_PLUGIN_STABLE_RUNTIME_KEYS = {
    "housekeeping_plugin_enabled",
    "housekeeping_plugin_interval_sec",
    "housekeeping_plugin_quiet_hour",
    "housekeeping_plugin_quiet_window_min",
    "housekeeping_plugin_days_old",
    "housekeeping_plugin_flags",
    "housekeeping_plugin_optimize_tables",
    "housekeeping_plugin_dry_run",
    "housekeeping_plugin_instance_settings",
}
_MONITORED_EMAIL_PARSER_STABLE_RUNTIME_KEYS = {
    "monitored_email_parser_enabled",
    "monitored_email_parser_interval_sec",
    "monitored_email_parser_batch_size",
    "monitored_email_parser_force_seen",
    "monitored_email_parser_delete_processed",
    "monitored_email_parser_disable_mautic_fetch",
    "monitored_email_parser_types",
    "monitored_email_parser_whitelist",
    "monitored_email_parser_instance_settings",
}
_CLUSTER_STABLE_RUNTIME_KEYS = {
    "cluster_id",
    "cluster_name",
    "cluster_node_role",
    "cluster_node_index",
    "cluster_routing_enabled",
    "cluster_route_cron_host",
    "cluster_route_import_host",
    "cluster_route_backup_host",
    "cluster_route_cache_hosts",
}
_STABLE_RUNTIME_KEYS = (
    _BACKUP_STABLE_RUNTIME_KEYS
    | _VIBER_STATS_STABLE_RUNTIME_KEYS
    | _SEGMENT_WHITELIST_STABLE_RUNTIME_KEYS
    | _EMPTY_LEADS_CLEANUP_STABLE_RUNTIME_KEYS
    | _PAGE_HITS_ORPHAN_CLEANUP_STABLE_RUNTIME_KEYS
    | _HOUSEKEEPING_PLUGIN_STABLE_RUNTIME_KEYS
    | _MONITORED_EMAIL_PARSER_STABLE_RUNTIME_KEYS
    | _CLUSTER_STABLE_RUNTIME_KEYS
)
_SERVICE_CLEANUP_RUNTIME_KEYS = (
    _EMPTY_LEADS_CLEANUP_STABLE_RUNTIME_KEYS
    | _PAGE_HITS_ORPHAN_CLEANUP_STABLE_RUNTIME_KEYS
    | _HOUSEKEEPING_PLUGIN_STABLE_RUNTIME_KEYS
    | _MONITORED_EMAIL_PARSER_STABLE_RUNTIME_KEYS
)

_DB_DISPATCH_PAUSE_ERROR_RE = re.compile(
    r"(too many connections|lost connection to mysql|mysql server has gone away|"
    r"lock wait timeout|deadlock found|metadata lock|\(1040,|\(1205,|\(1213,|\(2006,|\(2013,)",
    re.IGNORECASE,
)
_EXTERNAL_TASK_TYPES = {
    "mautic:segments:update": "segment",
    "mautic:campaign:trigger": "campaign_trigger",
    "mautic:campaigns:trigger": "campaign_trigger",
    "mautic:campaign:rebuild": "campaign_rebuild",
    "mautic:campaigns:rebuild": "campaign_rebuild",
    "mautic:campaigns:update": "campaign_rebuild",
}
_EXTERNAL_PROCESS_WRAPPERS = {"sudo", "timeout", "bash", "sh", "setsid", "nohup"}
_ZABBIX_VERSION_CACHE_GUARD_INTERVAL_SEC = 3600


@dataclass(frozen=True)
class SQLSegmentRule:
    segment_id: int
    select_sql: str
    depends_on: tuple[int, ...]


def _recent_finished_segment_ids(root: str, now_ts: float) -> set[int]:
    cutoff = float(now_ts) - float(_SEGMENT_DEPENDENCY_FINISH_WINDOW_SEC)
    out: set[int] = set()
    for (row_root, sid), ts in list(_SEGMENT_FINISHED_AT.items()):
        if ts < cutoff:
            _SEGMENT_FINISHED_AT.pop((row_root, sid), None)
            continue
        if row_root == root:
            out.add(int(sid))
    return out


def _mark_segment_finished(root: str, segment_id: int | None, now_ts: float | None = None) -> None:
    if segment_id is None:
        return
    try:
        sid = int(segment_id)
    except Exception:
        return
    if sid <= 0:
        return
    _SEGMENT_FINISHED_AT[(str(root), sid)] = float(now_ts or time.time())


def _segment_failure_blocked_ids(store: "TaskStore", root: str) -> set[int]:
    counts = store.recent_task_problem_counts(
        root=root,
        task_type="segment",
        since_sec=_SEGMENT_FAILURE_COOLDOWN_SEC,
    )
    return {
        int(segment_id)
        for segment_id, count in counts.items()
        if int(segment_id) > 0 and int(count) >= _SEGMENT_FAILURE_COOLDOWN_THRESHOLD
    }


def _log_segment_failure_cooldown(root: str, segment_id: int, count: int | None = None) -> None:
    key = (str(root), int(segment_id))
    now_ts = time.time()
    last = float(_SEGMENT_FAILURE_WARN_TS.get(key, 0.0) or 0.0)
    if now_ts - last < 300.0:
        return
    _SEGMENT_FAILURE_WARN_TS[key] = now_ts
    detail = f" failures={count}" if count is not None else ""
    logging.warning(
        "[%s] segment entity=%s cooldown active after repeated failures%s cooldown=%ss",
        root,
        int(segment_id),
        detail,
        _SEGMENT_FAILURE_COOLDOWN_SEC,
    )


def _log_invalid_segment_filters(root: str, details: str) -> None:
    if not details:
        return
    key = (str(root), details)
    now_ts = time.time()
    last = float(_SEGMENT_FILTER_WARN_TS.get(key, 0.0) or 0.0)
    if now_ts - last < 300.0:
        return
    _SEGMENT_FILTER_WARN_TS[key] = now_ts
    logging.warning("[%s] segment skipped due invalid date filter: %s", root, details)



def _to_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _to_boolish(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(default)


def _runtime_slug(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-").lower()


def _cluster_files_producer_allowed(config: AgentConfig) -> bool:
    if not bool(getattr(config, "backup_cluster_enabled", False)):
        return False
    if not bool(getattr(config, "backup_cluster_files_snapshot_enabled", True)):
        return False
    transport = str(getattr(config, "backup_cluster_files_transport", "syncthing") or "syncthing").strip().lower()
    if transport != "syncthing":
        return False
    expected = {
        _runtime_slug(item)
        for item in list(getattr(config, "backup_cluster_files_expected_nodes", []) or [])
        if _runtime_slug(item)
    }
    if not expected:
        return True
    local = {_runtime_slug(item) for item in cluster_local_identity_values(config) if _runtime_slug(item)}
    return bool(local & expected)


def _instance_has_viber_plugin(inst: object) -> bool:
    root = str(getattr(inst, "root", "") or "").strip()
    if not root:
        return False
    base = Path(root)
    candidates = [
        base / "plugins",
        base / "docroot" / "plugins",
        base / "public" / "plugins",
    ]
    for plugins_dir in candidates:
        if not plugins_dir.exists() or not plugins_dir.is_dir():
            continue
        try:
            for row in plugins_dir.iterdir():
                if row.name.startswith(".") or not row.is_dir():
                    continue
                if "viber" in row.name.lower():
                    return True
        except Exception:
            continue
    return False


def _viber_stats_setting_keys(inst: object) -> list[str]:
    raw = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
    ]
    domains = getattr(inst, "domains", None)
    if isinstance(domains, list):
        raw.extend(domains)
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _viber_stats_effective_setting(config: AgentConfig, inst: object) -> tuple[bool, int]:
    enabled = bool(getattr(config, "viber_stats_enabled", True))
    interval = max(60, int(getattr(config, "viber_stats_interval_sec", 600) or 600))
    settings = getattr(config, "viber_stats_instance_settings", {})
    if not isinstance(settings, dict):
        return enabled, interval
    for key in _viber_stats_setting_keys(inst):
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            if "enabled" in raw:
                enabled = _to_boolish(raw.get("enabled"), enabled)
            if "interval_sec" in raw:
                try:
                    interval = max(60, int(raw.get("interval_sec") or interval))
                except Exception:
                    pass
            return enabled, interval
        if isinstance(raw, bool):
            return bool(raw), interval
        try:
            return enabled, max(60, int(raw or interval))
        except Exception:
            return enabled, interval
    return enabled, interval


def _segment_whitelist_effective_setting(config: AgentConfig, inst: object) -> set[int]:
    if bool(getattr(config, "disable_whitelist", False)):
        return set()

    def global_segment_ids() -> set[int]:
        return set(getattr(config, "segment_whitelist", []) or []) | _load_segment_whitelist_file(
            getattr(config, "segment_whitelist_file", None),
            inst,
        )

    settings = getattr(config, "segment_whitelist_instance_settings", {})
    if not isinstance(settings, dict):
        return global_segment_ids()

    for key in _viber_stats_setting_keys(inst) + ["default"]:
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            if "enabled" in raw and not _to_boolish(raw.get("enabled"), True):
                return set()
            if _to_boolish(raw.get("disable_whitelist"), False):
                return set()
            ids_raw = raw.get("segment_whitelist", raw.get("segments", raw.get("ids", raw.get("whitelist", []))))
            ids = set(_to_int_list(ids_raw))
            file_raw = str(raw.get("segment_whitelist_file", "") or "").strip()
            if file_raw:
                ids |= _load_segment_whitelist_file(file_raw, inst)
            return ids
        if isinstance(raw, bool):
            return global_segment_ids() if raw else set()
        return set(_to_int_list(raw))

    return global_segment_ids()


def _segment_whitelist_file_key_for_instance(inst: object) -> str:
    for attr in ("instance_uid", "primary_domain", "name", "root"):
        value = str(getattr(inst, attr, "") or "").strip()
        if value:
            return value
    keys = _viber_stats_setting_keys(inst)
    return keys[0] if keys else "default"


def _parse_segment_whitelist_tokens(value: object) -> set[int]:
    ids: set[int] = set()
    if isinstance(value, (list, tuple, set)):
        for item in value:
            ids.update(_parse_segment_whitelist_tokens(item))
        return ids
    if value is None:
        return ids
    for token in re.split(r"[\s,;]+", str(value).strip()):
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return ids


def _parse_segment_whitelist_file(path: str | None) -> tuple[dict[str, set[int]], set[int]]:
    scoped: dict[str, set[int]] = {}
    legacy: set[int] = set()
    if not path:
        return scoped, legacy
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                if ":" in line:
                    key, raw_ids = line.split(":", 1)
                    key = key.strip()
                    if not key:
                        legacy.update(_parse_segment_whitelist_tokens(raw_ids))
                        continue
                    scoped.setdefault(key, set()).update(_parse_segment_whitelist_tokens(raw_ids))
                    continue
                legacy.update(_parse_segment_whitelist_tokens(line))
    except FileNotFoundError:
        logging.debug("segment whitelist file not found: %s", path)
    return scoped, legacy


def _load_segment_whitelist_file(path: str | None, inst: object) -> set[int]:
    scoped, legacy = _parse_segment_whitelist_file(path)
    if not scoped:
        return legacy
    keys = set(_viber_stats_setting_keys(inst))
    exact_ids: set[int] = set()
    exact_seen = False
    for key, ids in scoped.items():
        if key in keys:
            exact_seen = True
            exact_ids.update(ids)
    if exact_seen:
        return exact_ids
    if "default" in scoped:
        return set(scoped["default"])
    return legacy


def _published_segment_whitelist_ids(
    db: MauticDB,
    whitelist: set[int],
    sql_ctx: dict[str, str],
) -> list[int]:
    ids = sorted({int(x) for x in whitelist if int(x) > 0})
    if not ids:
        return []
    id_sql = ",".join(str(x) for x in ids)
    query = (
        "SELECT ll.id "
        "FROM {prefix}lead_lists ll "
        f"WHERE ll.is_published = 1 AND ll.id IN ({id_sql}) "
        "ORDER BY ll.id ASC"
    )
    return db.fetch_ids(query, limit=len(ids), context=sql_ctx)


def _ids_from_segment_whitelist_setting(raw: object) -> set[int]:
    if isinstance(raw, dict):
        if "enabled" in raw and not _to_boolish(raw.get("enabled"), True):
            return set()
        if _to_boolish(raw.get("disable_whitelist"), False):
            return set()
        return set(
            _to_int_list(raw.get("segment_whitelist", raw.get("segments", raw.get("ids", raw.get("whitelist", [])))))
        )
    if isinstance(raw, bool):
        return set()
    return set(_to_int_list(raw))


def _format_segment_whitelist_file(entries: dict[str, set[int]]) -> str:
    lines = [
        "# Managed by MCD.",
        "# Format: <instance-key>: <segment ids>",
        "# Instance key can be instance_uid, root, name, or domain.",
    ]
    for key in sorted(entries):
        ids = sorted(entries[key])
        if not ids:
            lines.append(f"{key}:")
            continue
        lines.append(f"{key}: {' '.join(str(x) for x in ids)}")
    return "\n".join(lines) + "\n"


def _sync_segment_whitelist_file(config: AgentConfig, installs: list[object] | None = None) -> bool:
    path_raw = str(getattr(config, "segment_whitelist_file", "") or "").strip()
    if not path_raw:
        return False
    path = Path(path_raw)
    settings = getattr(config, "segment_whitelist_instance_settings", {})
    entries: dict[str, set[int]] = {}

    if isinstance(settings, dict):
        for raw_key, raw_value in settings.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            entries[key] = _ids_from_segment_whitelist_setting(raw_value)

    scoped_from_file, legacy_from_file = _parse_segment_whitelist_file(path_raw)
    if not entries:
        entries.update(scoped_from_file)

    legacy_ids = set(legacy_from_file) | set(getattr(config, "segment_whitelist", []) or [])
    if legacy_ids:
        target_key = "default"
        if installs and len(installs) == 1:
            target_key = _segment_whitelist_file_key_for_instance(installs[0])
        entries.setdefault(target_key, set()).update(legacy_ids)

    if not entries:
        return False

    text = _format_segment_whitelist_file(entries)
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        logging.info("segment whitelist file synced: %s", path)
        return True
    except Exception as e:
        logging.warning("segment whitelist file sync failed for %s: %s", path, e)
        return False


def _monitored_email_parser_effective_setting(config: AgentConfig, inst: object) -> MonitoredEmailParserSettings:
    enabled = bool(getattr(config, "monitored_email_parser_enabled", False))
    interval = max(60, int(getattr(config, "monitored_email_parser_interval_sec", 900) or 900))
    batch_size = min(5000, max(1, int(getattr(config, "monitored_email_parser_batch_size", 100) or 100)))
    force_seen = bool(getattr(config, "monitored_email_parser_force_seen", False))
    delete_processed = bool(getattr(config, "monitored_email_parser_delete_processed", False))
    disable_mautic_fetch = bool(getattr(config, "monitored_email_parser_disable_mautic_fetch", True))
    raw_types = getattr(config, "monitored_email_parser_types", ["feedback_loop"]) or ["feedback_loop"]
    types = _normalize_monitored_email_types(raw_types)
    whitelist = _normalize_monitored_email_whitelist(getattr(config, "monitored_email_parser_whitelist", []))
    settings = getattr(config, "monitored_email_parser_instance_settings", {})
    if isinstance(settings, dict):
        for key in _viber_stats_setting_keys(inst) + ["default"]:
            if key not in settings:
                continue
            raw = settings.get(key)
            if isinstance(raw, dict):
                if "enabled" in raw:
                    enabled = _to_boolish(raw.get("enabled"), enabled)
                if "interval_sec" in raw:
                    try:
                        interval = max(60, int(raw.get("interval_sec") or interval))
                    except Exception:
                        pass
                if "batch_size" in raw:
                    try:
                        batch_size = min(5000, max(1, int(raw.get("batch_size") or batch_size)))
                    except Exception:
                        pass
                if "force_seen" in raw:
                    force_seen = _to_boolish(raw.get("force_seen"), force_seen)
                if "delete_processed" in raw:
                    delete_processed = _to_boolish(raw.get("delete_processed"), delete_processed)
                if "disable_mautic_fetch" in raw:
                    disable_mautic_fetch = _to_boolish(raw.get("disable_mautic_fetch"), disable_mautic_fetch)
                if "types" in raw:
                    types = _normalize_monitored_email_types(raw.get("types"))
                if "whitelist" in raw:
                    whitelist = _normalize_monitored_email_whitelist(raw.get("whitelist"))
                break
            if isinstance(raw, bool):
                enabled = bool(raw)
                break
    return MonitoredEmailParserSettings(
        enabled=enabled,
        interval_sec=interval,
        batch_size=batch_size,
        force_seen=force_seen,
        delete_processed=delete_processed,
        disable_mautic_fetch=disable_mautic_fetch,
        types=types,
        whitelist=whitelist,
    )


def _normalize_monitored_email_types(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in {"fbl", "feedback", "spam_complaint"}:
            key = "feedback_loop"
        if key not in MONITORED_EMAIL_ALLOWED_TYPES or key in out:
            continue
        out.append(key)
    return tuple(out or ["feedback_loop"])


def _normalize_monitored_email_whitelist(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = re.split(r"[\s,;]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    out: list[str] = []
    for item in items:
        email = str(item or "").strip().strip("<>.,;:()[]{}'\"").lower()
        if not email or "@" not in email or email in out:
            continue
        out.append(email)
    return tuple(out[:200])


def _is_mautic_email_fetch_template(template: str) -> bool:
    return bool(re.search(r"\bmautic:emails?:fetch\b", str(template or "")))


def _monitored_email_fetch_replaces_mautic(config: AgentConfig, inst: object) -> bool:
    setting = _monitored_email_parser_effective_setting(config, inst)
    return bool(setting.enabled and setting.disable_mautic_fetch)


def _any_monitored_email_fetch_replaces_mautic(config: AgentConfig, installs: list[object]) -> bool:
    return any(_monitored_email_fetch_replaces_mautic(config, inst) for inst in installs)


def _empty_leads_cleanup_effective_setting(
    config: AgentConfig,
    inst: object,
) -> tuple[bool, int, str, str, str, int, int, str, str, int]:
    enabled = bool(getattr(config, "empty_leads_cleanup_enabled", False))
    interval = max(60, int(getattr(config, "empty_leads_cleanup_interval_sec", 900) or 900))
    batch_size = max(1, int(getattr(config, "empty_leads_cleanup_batch_size", 5000) or 5000))
    max_runs_per_window = max(0, int(getattr(config, "empty_leads_cleanup_max_runs_per_window", 0) or 0))
    window_start = "22:00"
    window_end = "09:00"
    window_min = max(1, min(1440, int(getattr(config, "empty_leads_cleanup_quiet_window_min", 660) or 660)))
    mode = "both_null"
    schedule_type = "interval"
    cron_expr = ""
    settings = getattr(config, "empty_leads_cleanup_instance_settings", {})
    if not isinstance(settings, dict):
        return (
            enabled,
            interval,
            mode,
            schedule_type,
            cron_expr,
            batch_size,
            window_min,
            window_start,
            window_end,
            max_runs_per_window,
        )
    for key in _viber_stats_setting_keys(inst) + ["default"]:
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            if "enabled" in raw:
                enabled = _to_boolish(raw.get("enabled"), enabled)
            if "interval_sec" in raw:
                try:
                    interval = max(60, int(raw.get("interval_sec") or interval))
                except Exception:
                    pass
            if "batch_size" in raw:
                try:
                    batch_size = max(1, int(raw.get("batch_size") or batch_size))
                except Exception:
                    pass
            if "max_runs_per_window" in raw:
                try:
                    max_runs_per_window = max(0, int(raw.get("max_runs_per_window") or 0))
                except Exception:
                    pass
            if "window_start" in raw:
                window_start = str(raw.get("window_start") or window_start).strip() or window_start
            if "window_end" in raw:
                window_end = str(raw.get("window_end") or window_end).strip() or window_end
            if "window_min" in raw or "quiet_window_min" in raw:
                try:
                    window_min = max(1, min(1440, int(raw.get("window_min", raw.get("quiet_window_min")) or window_min)))
                except Exception:
                    pass
            raw_schedule_type = str(raw.get("schedule_type", schedule_type) or schedule_type).strip().lower()
            raw_cron_expr = str(raw.get("cron_expr", cron_expr) or "").strip()
            if raw_schedule_type in {"nightly", "nightly_window", "window"}:
                schedule_type = "nightly_window"
                cron_expr = ""
                window_min = _window_minutes(window_start, window_end)
            elif raw_schedule_type == "cron_window" and raw_cron_expr:
                schedule_type = "cron"
                cron_expr = raw_cron_expr
            if raw_schedule_type == "cron" and raw_cron_expr:
                schedule_type = "cron"
                cron_expr = raw_cron_expr
            elif schedule_type not in {"nightly_window", "cron"}:
                schedule_type = "interval"
                cron_expr = ""
            raw_mode = str(raw.get("mode", mode) or mode).strip().lower()
            if raw_mode == "email_or_mobile_null":
                raw_mode = "both_null"
            if raw_mode in {"both_null", "email_null", "mobile_null"}:
                mode = raw_mode
            return (
                enabled,
                interval,
                mode,
                schedule_type,
                cron_expr,
                batch_size,
                window_min,
                window_start,
                window_end,
                max_runs_per_window,
            )
        if isinstance(raw, bool):
            return (
                bool(raw),
                interval,
                mode,
                schedule_type,
                cron_expr,
                batch_size,
                window_min,
                window_start,
                window_end,
                max_runs_per_window,
            )
    return (
        enabled,
        interval,
        mode,
        schedule_type,
        cron_expr,
        batch_size,
        window_min,
        window_start,
        window_end,
        max_runs_per_window,
    )


def _page_hits_orphan_cleanup_effective_setting(
    config: AgentConfig,
    inst: object,
) -> tuple[bool, int, str, str, int, str, str, int, int, float, int, int]:
    enabled = bool(getattr(config, "enable_page_hits_orphan_cleanup", False))
    interval = max(60, int(getattr(config, "page_hits_orphan_cleanup_interval_sec", 3600) or 3600))
    quiet_hour = max(0, min(23, int(getattr(config, "page_hits_orphan_cleanup_quiet_hour", 2) or 2)))
    window_min = max(1, min(720, int(getattr(config, "page_hits_orphan_cleanup_quiet_window_min", 180) or 180)))
    window_start = f"{quiet_hour:02d}:00"
    end_min = quiet_hour * 60 + window_min
    window_end = f"{(end_min // 60) % 24:02d}:{end_min % 60:02d}"
    schedule_type = "nightly_window"
    cron_expr = ""
    batch_size = max(100, int(getattr(config, "page_hits_orphan_cleanup_batch_size", 5000) or 5000))
    batches = max(1, int(getattr(config, "page_hits_orphan_cleanup_batches_per_run", 12) or 12))
    sleep_sec = max(0.0, float(getattr(config, "page_hits_orphan_cleanup_sleep_sec", 0.2) or 0.2))
    grace_min = max(5, int(getattr(config, "page_hits_orphan_cleanup_grace_min", 60) or 60))
    max_run_sec = max(30, int(getattr(config, "page_hits_orphan_cleanup_max_run_sec", 180) or 180))
    settings = getattr(config, "page_hits_orphan_cleanup_instance_settings", {})
    if not isinstance(settings, dict):
        return enabled, interval, schedule_type, cron_expr, window_min, window_start, window_end, batch_size, batches, sleep_sec, grace_min, max_run_sec
    for key in _viber_stats_setting_keys(inst) + ["default"]:
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            if "enabled" in raw:
                enabled = _to_boolish(raw.get("enabled"), enabled)
            for field, assign in (
                ("interval_sec", "interval"),
                ("quiet_hour", "quiet_hour"),
                ("quiet_window_min", "window_min"),
                ("batch_size", "batch_size"),
                ("batches_per_run", "batches"),
            ):
                if field not in raw:
                    continue
                try:
                    value = int(raw.get(field))
                except Exception:
                    continue
                if assign == "interval":
                    interval = max(60, value)
                elif assign == "quiet_hour":
                    quiet_hour = max(0, min(23, value))
                elif assign == "window_min":
                    window_min = max(1, min(720, value))
                elif assign == "batch_size":
                    batch_size = max(100, value)
                elif assign == "batches":
                    batches = max(1, value)
            if "max_runs_per_window" in raw:
                try:
                    batches = max(0, int(raw.get("max_runs_per_window") or 0))
                except Exception:
                    pass
            raw_schedule_type = str(raw.get("schedule_type", schedule_type) or schedule_type).strip().lower()
            raw_cron_expr = str(raw.get("cron_expr", "") or "").strip()
            if "window_start" in raw:
                window_start = str(raw.get("window_start") or window_start).strip() or window_start
            if "window_end" in raw:
                window_end = str(raw.get("window_end") or window_end).strip() or window_end
            if raw_schedule_type in {"nightly", "nightly_window", "window"}:
                schedule_type = "nightly_window"
                cron_expr = ""
                window_min = _window_minutes(window_start, window_end)
            elif raw_schedule_type == "cron" and raw_cron_expr:
                schedule_type = "cron"
                cron_expr = raw_cron_expr
            else:
                schedule_type = "interval"
                cron_expr = ""
            return enabled, interval, schedule_type, cron_expr, window_min, window_start, window_end, batch_size, batches, sleep_sec, grace_min, max_run_sec
        if isinstance(raw, bool):
            return bool(raw), interval, schedule_type, cron_expr, window_min, window_start, window_end, batch_size, batches, sleep_sec, grace_min, max_run_sec
    return enabled, interval, schedule_type, cron_expr, window_min, window_start, window_end, batch_size, batches, sleep_sec, grace_min, max_run_sec


_HOUSEKEEPING_ALLOWED_FLAGS = {
    "campaign_lead": "--campaign-lead",
    "email_stats": "--email-stats",
    "email_stats_tokens": "--email-stats-tokens",
    "lead": "--lead",
    "page_hits": "--page-hits",
}


def _housekeeping_plugin_installed(root: str) -> bool:
    base = Path(root)
    for p in (base / "plugins", base / "docroot" / "plugins", base / "public" / "plugins"):
        if (p / "LeuchtfeuerHousekeepingBundle").is_dir():
            return True
    return False


def _housekeeping_plugin_effective_setting(config: AgentConfig, inst: object) -> tuple[bool, int, int, int, int, list[str], bool, bool]:
    enabled = bool(getattr(config, "housekeeping_plugin_enabled", False))
    interval = max(60, int(getattr(config, "housekeeping_plugin_interval_sec", 86400) or 86400))
    quiet_hour = max(0, min(23, int(getattr(config, "housekeeping_plugin_quiet_hour", 3) or 3)))
    window_min = max(1, min(720, int(getattr(config, "housekeeping_plugin_quiet_window_min", 120) or 120)))
    days_old = max(1, int(getattr(config, "housekeeping_plugin_days_old", 365) or 365))
    flags = [str(x).strip() for x in (getattr(config, "housekeeping_plugin_flags", []) or []) if str(x).strip()]
    optimize = bool(getattr(config, "housekeeping_plugin_optimize_tables", False))
    dry_run = bool(getattr(config, "housekeeping_plugin_dry_run", True))
    settings = getattr(config, "housekeeping_plugin_instance_settings", {})
    if not isinstance(settings, dict):
        return enabled, interval, quiet_hour, window_min, days_old, flags, optimize, dry_run
    for key in _viber_stats_setting_keys(inst) + ["default"]:
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            if "enabled" in raw:
                enabled = _to_boolish(raw.get("enabled"), enabled)
            for field in ("interval_sec", "quiet_hour", "quiet_window_min", "days_old"):
                if field not in raw:
                    continue
                try:
                    value = int(raw.get(field))
                except Exception:
                    continue
                if field == "interval_sec":
                    interval = max(60, value)
                elif field == "quiet_hour":
                    quiet_hour = max(0, min(23, value))
                elif field == "quiet_window_min":
                    window_min = max(1, min(720, value))
                elif field == "days_old":
                    days_old = max(1, value)
            if isinstance(raw.get("flags"), list):
                flags = [str(x).strip() for x in raw.get("flags", []) if str(x).strip()]
            if "optimize_tables" in raw:
                optimize = _to_boolish(raw.get("optimize_tables"), optimize)
            if "dry_run" in raw:
                dry_run = _to_boolish(raw.get("dry_run"), dry_run)
            return enabled, interval, quiet_hour, window_min, days_old, flags, optimize, dry_run
        if isinstance(raw, bool):
            return bool(raw), interval, quiet_hour, window_min, days_old, flags, optimize, dry_run
    return enabled, interval, quiet_hour, window_min, days_old, flags, optimize, dry_run


def _migrate_empty_leads_cleanup_runtime(config: AgentConfig, lines: list[str]) -> bool:
    interval_sec = 0
    cron_expr = ""
    for line in lines:
        m = re.search(r"MCD_EMPTY_LEADS_MIGRATE\s+interval_sec=(\d+)", str(line or ""))
        if m:
            interval_sec = max(interval_sec, int(m.group(1)))
        cm = re.search(r"MCD_EMPTY_LEADS_MIGRATE\s+.*cron_expr='([^']+)'", str(line or ""))
        if cm:
            cron_expr = cm.group(1).strip()
    if interval_sec <= 0 and not cron_expr:
        return False
    current = local_runtime_overrides(config)
    schedule_type = "cron" if cron_expr else "interval"
    interval_sec = max(60, interval_sec or int(getattr(config, "empty_leads_cleanup_interval_sec", 900) or 900))
    default_setting = {
        "enabled": True,
        "interval_sec": interval_sec,
        "mode": "both_null",
        "schedule_type": schedule_type,
    }
    if cron_expr:
        default_setting["cron_expr"] = cron_expr
    current_settings = current.get("empty_leads_cleanup_instance_settings")
    next_settings: dict[str, object] = {}
    if isinstance(current_settings, dict) and current_settings:
        changed_any = False
        for key, value in current_settings.items():
            if isinstance(value, dict):
                merged = dict(value)
                if not merged.get("schedule_type"):
                    merged["schedule_type"] = schedule_type
                    merged["interval_sec"] = int(merged.get("interval_sec") or interval_sec)
                    if cron_expr:
                        merged["cron_expr"] = cron_expr
                    changed_any = True
                next_settings[str(key)] = merged
            else:
                next_settings[str(key)] = value
        if not changed_any:
            return False
    else:
        next_settings = {"default": default_setting}
    updates = {
        "empty_leads_cleanup_enabled": True,
        "empty_leads_cleanup_interval_sec": interval_sec,
        "empty_leads_cleanup_instance_settings": next_settings,
    }
    path, changed = upsert_runtime_values(config.config_file_path, updates)
    if changed:
        logging.info("empty leads cleanup runtime migrated from cron into %s", path)
    return bool(changed)


def _persist_stable_backup_runtime_to_config(
    config: AgentConfig,
    applied_keys: list[str],
    installs: list[object] | None = None,
) -> None:
    stable_keys = sorted(set(applied_keys) & _STABLE_RUNTIME_KEYS)
    if not stable_keys:
        return
    eff_runtime = runtime_effective_map(config)
    updates: dict[str, object] = {}
    for key in stable_keys:
        if key in eff_runtime:
            updates[key] = eff_runtime[key]
    if not updates:
        return
    try:
        path, changed = upsert_runtime_values(config.config_file_path, updates)
        if changed:
            logging.info(
                "runtime-overrides persisted to config (%s): keys=%s",
                path,
                ",".join(stable_keys),
            )
    except Exception as e:
        logging.warning("runtime-overrides persist to config failed: %s", e)
    if (
        "segment_whitelist_instance_settings" in stable_keys
        or "segment_whitelist" in stable_keys
        or "segment_whitelist_file" in stable_keys
    ):
        _sync_segment_whitelist_file(config, installs)


def _to_int_list(value: object) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            iv = _to_int(item)
            if iv is not None:
                out.append(iv)
        return out
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                return _to_int_list(parsed)
            except Exception:
                pass
        out2: list[int] = []
        for part in raw.split(","):
            iv = _to_int(part)
            if iv is not None:
                out2.append(iv)
        return out2
    iv = _to_int(value)
    return [iv] if iv is not None else []


def _parse_sql_segment_rules(raw: object) -> dict[int, SQLSegmentRule]:
    items: list[tuple[int | None, dict[str, object]]] = []
    if isinstance(raw, dict):
        wrapped = raw.get("rules")
        if isinstance(wrapped, list):
            for item in wrapped:
                if isinstance(item, dict):
                    items.append((None, item))
        else:
            for rk, rv in raw.items():
                if not isinstance(rv, dict):
                    continue
                items.append((_to_int(rk), rv))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                items.append((None, item))

    out: dict[int, SQLSegmentRule] = {}
    for key_sid, item in items:
        enabled_raw = item.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() not in {"0", "false", "off", "no"}
        else:
            enabled = bool(enabled_raw)
        if not enabled:
            continue

        sid = _to_int(item.get("segment_id"))
        if sid is None:
            sid = _to_int(item.get("id"))
        if sid is None:
            sid = key_sid
        if sid is None or sid <= 0:
            continue

        sql_text = (
            str(item.get("select_sql") or item.get("sql") or item.get("query") or "").strip()
        )
        if not sql_text:
            continue

        deps = _to_int_list(
            item.get("depends_on", item.get("dependencies", item.get("deps", item.get("requires"))))
        )
        deps = [x for x in deps if x > 0 and x != sid]
        out[sid] = SQLSegmentRule(
            segment_id=sid,
            select_sql=sql_text,
            depends_on=tuple(dict.fromkeys(deps)),
        )
    return out


def _toposort_sql_segment_ids(ids: list[int], rules: dict[int, SQLSegmentRule]) -> list[int]:
    uniq = list(dict.fromkeys([x for x in ids if x in rules]))
    if not uniq:
        return []
    active = set(uniq)
    indeg: dict[int, int] = {sid: 0 for sid in uniq}
    outgoing: dict[int, set[int]] = {sid: set() for sid in uniq}
    for sid in uniq:
        for dep in rules[sid].depends_on:
            if dep not in active:
                continue
            # dep -> sid
            if sid not in outgoing[dep]:
                outgoing[dep].add(sid)
                indeg[sid] += 1
    queue = sorted([sid for sid in uniq if indeg[sid] == 0])
    ordered: list[int] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing.get(current, set())):
            indeg[nxt] = max(0, indeg[nxt] - 1)
            if indeg[nxt] == 0 and nxt not in queue and nxt not in ordered:
                queue.append(nxt)
    if len(ordered) == len(uniq):
        return ordered
    # Keep deterministic order on dependency cycles/malformed graphs.
    tail = [sid for sid in sorted(uniq) if sid not in set(ordered)]
    return ordered + tail


def _plan_sql_segment_ring(due_ids: list[int], rules: dict[int, SQLSegmentRule]) -> list[int]:
    due_set = set(dict.fromkeys(due_ids))
    managed_due = [sid for sid in due_ids if sid in rules]
    if not managed_due:
        return []
    planned: set[int] = set(managed_due)
    queue = deque(managed_due)
    while queue:
        sid = queue.popleft()
        rule = rules.get(sid)
        if rule is None:
            continue
        for dep in rule.depends_on:
            if dep not in rules:
                continue
            # Always include SQL-managed dependencies for deterministic order.
            if dep not in planned:
                planned.add(dep)
                queue.append(dep)
    ordered = _toposort_sql_segment_ids(list(planned), rules)
    # Keep due IDs first where possible; dependencies not due are still kept.
    ordered_due = [sid for sid in ordered if sid in due_set]
    ordered_deps = [sid for sid in ordered if sid not in due_set]
    return ordered_due + ordered_deps


def _segment_sql_state_key(root: str, segment_id: int) -> str:
    root_hash = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:24]
    return f"segment_sql_state:{root_hash}:{int(segment_id)}"


def _segment_sql_state_load(store: "TaskStore", root: str, segment_id: int) -> dict[str, object]:
    payload = store.get_runtime_sync(_segment_sql_state_key(root, segment_id))
    return dict(payload) if isinstance(payload, dict) else {}


def _segment_sql_rule_kind(rule: SQLSegmentRule) -> str:
    sql = str(rule.select_sql or "")
    if re.search(r"\bpage_hits\b", sql, flags=re.IGNORECASE):
        return "page_hits"
    return "generic"


def _in_daily_quiet_window(now_local: datetime, quiet_hour: int, quiet_window_min: int) -> bool:
    start_hour = max(0, min(23, int(quiet_hour)))
    window_min = max(1, int(quiet_window_min))
    start_today = now_local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_today = start_today + timedelta(minutes=window_min)
    if start_today <= now_local < end_today:
        return True
    if end_today.date() != start_today.date():
        start_prev = start_today - timedelta(days=1)
        end_prev = start_prev + timedelta(minutes=window_min)
        if start_prev <= now_local < end_prev:
            return True
    return False


def _segment_sql_try_acquire(
    *,
    store: "TaskStore",
    config: AgentConfig,
    root: str,
    segment_id: int,
    owner: str,
    rule_kind: str,
    now_ts: float,
) -> tuple[bool, dict[str, object]]:
    payload = _segment_sql_state_load(store, root, segment_id)
    status = str(payload.get("status") or "").strip().lower()
    prev_owner = str(payload.get("owner") or "").strip()
    last_started_at = float(payload.get("last_started_at") or 0.0)
    last_heartbeat_at = float(payload.get("heartbeat_at") or payload.get("started_at") or 0.0)
    min_repeat = max(0, int(getattr(config, "segment_sql_min_repeat_sec", 0) or 0))
    orphan_after_sec = max(30, int(getattr(config, "segment_sql_orphan_after_sec", 900) or 900))
    orphan_policy = str(getattr(config, "segment_sql_orphan_policy", "reclaim_stale") or "reclaim_stale").strip().lower()
    stale_running = (
        status == "running"
        and prev_owner
        and prev_owner != owner
        and last_heartbeat_at > 0
        and (float(now_ts) - float(last_heartbeat_at)) >= float(orphan_after_sec)
    )
    if status == "running" and prev_owner and prev_owner != owner:
        if not stale_running or orphan_policy != "reclaim_stale":
            retry_after = 0
            if stale_running:
                retry_after = 0
            elif last_heartbeat_at > 0:
                retry_after = max(1, int(orphan_after_sec - (float(now_ts) - float(last_heartbeat_at))))
            return False, {
                **payload,
                "reason": "running_locked",
                "retry_after_sec": retry_after,
            }

    if last_started_at > 0 and min_repeat > 0 and (float(now_ts) - float(last_started_at)) < float(min_repeat):
        return False, {
            **payload,
            "reason": "cooldown",
            "retry_after_sec": max(1, int(float(min_repeat) - (float(now_ts) - float(last_started_at)))),
        }

    next_payload = dict(payload)
    next_payload.update(
        {
            "root": str(root),
            "segment_id": int(segment_id),
            "status": "running",
            "owner": str(owner),
            "rule_kind": str(rule_kind),
            "started_at": float(now_ts),
            "last_started_at": float(now_ts),
            "heartbeat_at": float(now_ts),
            "orphan_policy": orphan_policy,
            "orphan_after_sec": int(orphan_after_sec),
            "cooldown_sec": int(min_repeat),
        }
    )
    if stale_running:
        next_payload["reclaimed_from_owner"] = prev_owner
        next_payload["reclaimed_at"] = float(now_ts)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), next_payload)
    return True, next_payload


def _segment_sql_heartbeat(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    now_ts: float,
) -> None:
    payload = _segment_sql_state_load(store, root, segment_id)
    if str(payload.get("status") or "").strip().lower() != "running":
        return
    if str(payload.get("owner") or "").strip() != str(owner):
        return
    payload["heartbeat_at"] = float(now_ts)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), payload)


def _segment_sql_finish(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    now_ts: float,
    result: str,
    note: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload = _segment_sql_state_load(store, root, segment_id)
    if str(payload.get("owner") or "").strip() != str(owner) and payload:
        return
    payload.update(
        {
            "root": str(root),
            "segment_id": int(segment_id),
            "status": "idle",
            "owner": "",
            "heartbeat_at": float(now_ts),
            "last_finished_at": float(now_ts),
            "last_result": str(result),
            "last_note": str(note or "")[:1000],
        }
    )
    if extra:
        payload.update(extra)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), payload)


def _segment_sql_start_heartbeat(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    interval_sec: int,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _worker() -> None:
        while not stop_event.wait(max(5, int(interval_sec))):
            try:
                _segment_sql_heartbeat(
                    store=store,
                    root=root,
                    segment_id=segment_id,
                    owner=owner,
                    now_ts=time.time(),
                )
            except Exception as e:
                logging.warning("[%s] segment_sql heartbeat failed id=%s: %s", root, segment_id, e)

    thread = threading.Thread(
        target=_worker,
        name=f"segment-sql-heartbeat-{segment_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _run_sql_segment_ring(
    *,
    config: AgentConfig,
    store: "TaskStore",
    db: MauticDB,
    root: str,
    ring: deque[int],
    rules: dict[int, SQLSegmentRule],
    active_set: set[int],
    done_set: set[int],
    running: dict[str, "RunningTask"],
    sql_ctx: dict[str, str],
    now_ts: float,
    now_local: datetime,
) -> int:
    if not config.segment_sql_ring_enabled:
        return 0
    limit = max(0, int(getattr(config, "segment_sql_ring_max_per_tick", 1) or 0))
    if limit <= 0 or not ring or not rules:
        return 0
    launched = 0
    scans = len(ring)
    while scans > 0 and launched < limit:
        sid = int(ring[0])
        scans -= 1
        rule = rules.get(sid)
        if rule is None:
            ring.popleft()
            active_set.discard(sid)
            done_set.discard(sid)
            continue
        rule_kind = _segment_sql_rule_kind(rule)
        if (
            rule_kind == "page_hits"
            and bool(getattr(config, "segment_sql_page_hits_quiet_only", False))
            and not _in_daily_quiet_window(
                now_local,
                int(getattr(config, "segment_sql_page_hits_quiet_hour", 2) or 2),
                int(getattr(config, "segment_sql_page_hits_quiet_window_min", 180) or 180),
            )
        ):
            ring.rotate(-1)
            continue
        if _is_running(running, root, "segment", sid):
            ring.rotate(-1)
            continue
        if not _launch_allowed(config, root, "segment_sql", sid, now_ts=now_ts):
            ring.rotate(-1)
            continue
        dep_wait = [dep for dep in rule.depends_on if dep in active_set and dep not in done_set]
        if dep_wait:
            ring.rotate(-1)
            continue
        owner = f"{_state_node_id(config)}:{os.getpid()}:{sid}:{int(now_ts)}"
        lock_ok, _lock_state = _segment_sql_try_acquire(
            store=store,
            config=config,
            root=root,
            segment_id=sid,
            owner=owner,
            rule_kind=rule_kind,
            now_ts=now_ts,
        )
        if not lock_ok:
            ring.rotate(-1)
            continue
        hb_stop, hb_thread = _segment_sql_start_heartbeat(
            store=store,
            root=root,
            segment_id=sid,
            owner=owner,
            interval_sec=int(getattr(config, "segment_sql_lock_heartbeat_sec", 15) or 15),
        )
        try:
            res = db.rebuild_segment_membership(
                segment_id=sid,
                select_query_template=rule.select_sql,
                context=sql_ctx,
                statement_timeout_sec=int(getattr(config, "segment_sql_statement_timeout_sec", 1800) or 1800),
            )
            _mark_segment_finished(root, sid, now_ts=now_ts)
            done_set.add(sid)
            _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment_sql", sid)] = float(now_ts)
            # Keep shared cooldown in sync with regular `segment` task type.
            _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment", sid)] = float(now_ts)
            _segment_sql_finish(
                store=store,
                root=root,
                segment_id=sid,
                owner=owner,
                now_ts=time.time(),
                result="ok",
                note="rebuilt",
                extra={
                    "selected_count": int(res.get("selected_count", 0) or 0),
                    "deleted_count": int(res.get("deleted_count", 0) or 0),
                    "inserted_count": int(res.get("inserted_count", 0) or 0),
                    "duration_sec": float(res.get("duration_sec", 0.0) or 0.0),
                },
            )
            ring.rotate(-1)
            launched += 1
            logging.info(
                "[%s] segment_sql rebuilt id=%s selected=%s inserted=%s deleted=%s duration=%.2fs",
                root,
                sid,
                int(res.get("selected_count", 0) or 0),
                int(res.get("inserted_count", 0) or 0),
                int(res.get("deleted_count", 0) or 0),
                float(res.get("duration_sec", 0.0) or 0.0),
            )
        except Exception as e:
            ring.rotate(-1)
            _segment_sql_finish(
                store=store,
                root=root,
                segment_id=sid,
                owner=owner,
                now_ts=time.time(),
                result="error",
                note=str(e),
            )
            logging.warning("[%s] segment_sql rebuild failed id=%s: %s", root, sid, e)
        finally:
            hb_stop.set()
            hb_thread.join(timeout=2.0)
    return launched


def _backup_done_for_local_date(config: AgentConfig, local_dt: datetime) -> bool:
    today = local_dt.strftime("%Y-%m-%d")
    try:
        st = backup_status(config)
    except Exception:
        return False

    if str(st.get("last_status") or "").strip().lower() != "ok":
        return False

    # Preferred source of truth for daily success.
    last_success_at = str(st.get("last_success_at") or "").strip()
    if last_success_at:
        try:
            ts = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone().date() == local_dt.date():
                return True
        except Exception:
            pass

    # Backward-compatible fallback: final path suffix contains date.
    last_path = str(st.get("last_backup_path") or "").strip().rstrip("/")
    return bool(last_path) and last_path.endswith(f"/{today}")


def _backup_attempted_for_local_date(config: AgentConfig, local_dt: datetime) -> bool:
    today = local_dt.strftime("%Y-%m-%d")
    try:
        st = backup_status(config)
    except Exception:
        return False

    # A finished run (ok/failed) for today counts as "attempted" so dispatch
    # can resume after backup completion.
    last_run_at = str(st.get("last_run_at") or "").strip()
    if last_run_at:
        try:
            ts = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone().date() == local_dt.date():
                return True
        except Exception:
            pass

    # Fallback for older states where only success path/timestamp may exist.
    if str(st.get("last_status") or "").strip().lower() != "ok":
        return False
    last_path = str(st.get("last_backup_path") or "").strip().rstrip("/")
    if last_path.endswith(f"/{today}"):
        return True
    last_success_at = str(st.get("last_success_at") or "").strip()
    if not last_success_at:
        return False
    try:
        ts = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone().date() == local_dt.date()
    except Exception:
        return False


def _backup_storage_probe_allowed(config: AgentConfig) -> tuple[bool, str]:
    if not bool(getattr(config, "backup_enabled", False)):
        return False, "backup disabled"
    if not bool(getattr(config, "backup_cluster_enabled", False)):
        return True, ""

    authority = cluster_backup_authority_status(config)
    if not bool(authority.get("allowed")):
        reason = str(authority.get("reason") or "not cluster backup authority").strip()
        return False, f"cluster backup non-authority: {reason}"
    if not bool(getattr(config, "backup_cluster_remote_enabled", True)):
        return False, "cluster remote backup disabled"
    return True, ""


def _cluster_state_ts_local_date(value: str, local_dt: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if local_dt.tzinfo is not None:
            return ts.astimezone(local_dt.tzinfo).date() == local_dt.date()
        return ts.astimezone().date() == local_dt.date()
    except Exception:
        return False


def _cluster_state_ts_age_sec(value: str, *, now: datetime | None = None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return max(0.0, (ref.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _cluster_local_full_done_for_date(config: AgentConfig, local_dt: datetime) -> bool:
    try:
        st = cluster_backup_status(config)
    except Exception:
        return False
    last_full_at = str(st.get("last_local_full_at") or "")
    # Cluster full backups are calendar-window driven. A late full from the
    # previous day must not push the next day's 01:00 full forward; otherwise a
    # single incident causes permanent schedule drift and the offsite tier misses
    # its expected 02:00 chain. Same-day protection is still enough to prevent
    # duplicate full runs after daemon restarts.
    return _cluster_state_ts_local_date(last_full_at, local_dt)


def _cluster_local_full_ready_for_offsite(config: AgentConfig, local_dt: datetime) -> bool:
    try:
        st = cluster_backup_status(config)
    except Exception:
        return False
    raw = str(st.get("last_local_full_at") or "").strip()
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        full_local = ts.astimezone()
    except Exception:
        return False
    if full_local.date() != local_dt.date():
        return False
    # Offsite belongs to the latest successful local full for the same
    # business date. Do not reject late local full backups here: after incidents,
    # restarts, or long full runs the backup can complete after the daytime
    # incremental window starts, but it is still the correct offsite source.
    return True


def _cluster_offsite_done_for_date(config: AgentConfig, local_dt: datetime) -> bool:
    try:
        st = cluster_backup_status(config)
    except Exception:
        return False
    if str(st.get("last_backup_kind") or "") != "cluster_offsite":
        return False
    return _cluster_state_ts_local_date(str(st.get("last_success_at") or ""), local_dt)


def _cluster_incremental_recent(config: AgentConfig, now_ts: float) -> bool:
    try:
        st = cluster_backup_status(config)
    except Exception:
        return False
    raw = str(st.get("last_local_incremental_at") or "").strip()
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        interval = max(300, int(config.backup_cluster_incremental_interval_sec or 7200))
        return now_ts - ts.timestamp() < interval
    except Exception:
        return False


def _local_time_reached(dt_local: datetime, hour: int, minute: int) -> bool:
    target = dt_local.replace(hour=max(0, min(23, int(hour))), minute=max(0, min(59, int(minute))), second=0, microsecond=0)
    return dt_local >= target


def _local_hour_in_closed_window(dt_local: datetime, start_hour: int, end_hour: int) -> bool:
    start = max(0, min(23, int(start_hour)))
    end = max(0, min(23, int(end_hour)))
    hour = dt_local.hour
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def _backup_dispatch_pause_state(
    config: AgentConfig,
    *,
    backup_running: bool,
    now_local: datetime | None = None,
) -> tuple[bool, str]:
    if not config.backup_enabled:
        return False, ""
    if backup_running:
        return True, "backup_running"
    if not config.backup_schedule_enabled:
        return False, ""

    pre_pause_sec = max(0, int(config.backup_schedule_pre_pause_sec))
    if pre_pause_sec <= 0:
        return False, ""

    quiet_hour = max(0, min(23, int(config.backup_schedule_quiet_hour)))
    quiet_minute = max(0, min(59, int(getattr(config, "backup_schedule_quiet_minute", 0))))
    quiet_window_min = max(1, min(180, int(config.backup_schedule_quiet_window_min)))
    dt_local = now_local if now_local is not None else datetime.now()
    start_today = dt_local.replace(hour=quiet_hour, minute=quiet_minute, second=0, microsecond=0)
    done_today = _backup_attempted_for_local_date(config, dt_local)
    in_window = start_today <= dt_local < (start_today + timedelta(minutes=quiet_window_min))

    # If today's backup is still pending and we are already in backup slot,
    # block new task launches until backup run starts/finishes.
    if in_window and not done_today:
        return True, "backup_window_pending"

    if dt_local < start_today:
        next_start = start_today
    else:
        next_start = start_today + timedelta(days=1)

    sec_to_next = int((next_start - dt_local).total_seconds())
    if 0 < sec_to_next <= pre_pause_sec:
        return True, f"pre_backup_window_{sec_to_next}s"
    return False, ""


@dataclass
class RunningTask:
    row_id: int
    root: str
    task_key: str
    task_type: str
    entity_id: int | None
    command_str: str
    timeout_sec: int
    attempts: int
    started_at: float
    pid: int
    manual_request_id: int | None = None
    external: bool = False


def _load_id_file(path: str | None) -> set[int]:
    out: set[int] = set()
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    out.add(int(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        logging.warning("ID file not found: %s", path)
    return out


def _extract_int(row: dict[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_node_id(cfg: AgentConfig | None) -> str:
    if cfg is None:
        return "local-node"
    ident = resolve_agent_identity(cfg)
    for key in (
        "local_hostname",
        "effective_hostname",
        "configured_host_name",
        "effective_mcc_host_name",
    ):
        raw = str(ident.get(key) or "").strip()
        if raw:
            return raw[:191]
    return "unknown-host"


def _segment_weights(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    whitelist: set[int],
    now_ts: int,
) -> dict[int, float]:
    by_id: dict[int, dict[str, object]] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is not None:
            by_id[rid] = row

    weights: dict[int, float] = {}
    for sid in ids:
        row = by_id.get(sid, {})
        created_ts = _extract_int(row, "created_ts")
        modified_ts = _extract_int(row, "modified_ts")
        built_ts = _extract_int(row, "built_ts")
        recent_activity = _extract_int(row, "recent_activity")
        w = 0.0
        if created_ts is not None:
            created_age_h = max(0.0, (now_ts - created_ts) / 3600.0)
            w += max(0.0, 400.0 - created_age_h)
        if built_ts is None:
            w += 80.0
        else:
            staleness_h = max(0.0, (now_ts - built_ts) / 3600.0)
            w += min(40.0, staleness_h)

        if modified_ts is not None and (built_ts is None or modified_ts > built_ts):
            w += 120.0
        if recent_activity is not None and recent_activity > 0:
            w += min(600.0, float(recent_activity) * 2.0)

        if sid in whitelist:
            w += 1000.0

        weights[sid] = w
    return weights


def _campaign_weights(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    whitelist: set[int],
    now_ts: int,
) -> dict[int, float]:
    by_id: dict[int, dict[str, object]] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is not None:
            by_id[rid] = row

    out: dict[int, float] = {}
    for cid in ids:
        row = by_id.get(cid, {})
        publish_ts = _extract_int(row, "publish_ts")
        pending_cnt = _extract_int(row, "pending_cnt")
        recent_activity = _extract_int(row, "recent_activity")
        if publish_ts is None:
            w = 0.0
        else:
            age_h = max(0.0, (now_ts - publish_ts) / 3600.0)
            w = max(0.0, 500.0 - (age_h * 2.0))
        if pending_cnt is not None and pending_cnt > 0:
            w += min(800.0, float(pending_cnt) * 0.5)
        if recent_activity is not None and recent_activity > 0:
            w += min(600.0, float(recent_activity) * 2.0)
        if cid in whitelist:
            w += 1000.0
        out[cid] = w
    return out


def _latest_campaign_ids(weight_rows: list[dict[str, object]], count: int) -> list[int]:
    if count <= 0:
        return []
    pairs: list[tuple[int, int]] = []
    for row in weight_rows:
        rid = _extract_int(row, "id")
        publish_ts = _extract_int(row, "publish_ts")
        if rid is None or publish_ts is None:
            continue
        pairs.append((rid, publish_ts))
    pairs.sort(key=lambda x: (-x[1], -x[0]))
    return [rid for rid, _ in pairs[:count]]


def _latest_campaign_ids_from_ids(ids: list[int], count: int) -> list[int]:
    if count <= 0:
        return []
    uniq = list(dict.fromkeys(ids))
    return sorted(uniq, reverse=True)[:count]


def _split_segment_circles(
    ids: list[int],
    weights: dict[int, float],
    whitelist: set[int],
    threshold: float,
    priority_size: int,
    stale_priority_ids: set[int] | None = None,
) -> tuple[list[int], list[int]]:
    uniq = list(dict.fromkeys(ids))
    stale_set = set(stale_priority_ids or set())
    priority_set: set[int] = set(whitelist)
    if stale_priority_ids:
        priority_set.update(stale_priority_ids)
    ranked = sorted(uniq, key=lambda x: (-weights.get(x, 0.0), x))
    for sid in ranked[: max(0, priority_size)]:
        priority_set.add(sid)
    for sid in uniq:
        if weights.get(sid, 0.0) >= threshold:
            priority_set.add(sid)

    def _prio_key(sid: int) -> tuple[int, int, float, int]:
        # Priority order inside ring:
        # 1) whitelist (forced),
        # 2) stale (>24h or never built),
        # 3) computed weight,
        # 4) id for deterministic tie-break.
        return (
            0 if sid in whitelist else 1,
            0 if sid in stale_set else 1,
            -weights.get(sid, 0.0),
            sid,
        )

    priority = sorted([x for x in uniq if x in priority_set], key=_prio_key)
    regular = sorted([x for x in uniq if x not in priority_set], key=lambda x: (-weights.get(x, 0.0), x))
    return priority, regular


def _stale_segment_priority_ids(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    now_ts: int,
    stale_sec: int = _SEGMENT_STALE_PRIORITY_SEC,
) -> set[int]:
    uniq = list(dict.fromkeys(ids))
    by_id_built_ts: dict[int, int | None] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is None:
            continue
        by_id_built_ts[rid] = _extract_int(row, "built_ts")

    out: set[int] = set()
    limit_sec = max(60, int(stale_sec))
    for sid in uniq:
        built_ts = by_id_built_ts.get(sid)
        # Never built / unknown built date => treat as stale.
        if built_ts is None:
            out.add(sid)
            continue
        if now_ts - built_ts >= limit_sec:
            out.add(sid)
    return out


def _split_campaign_circles(
    ids: list[int],
    weights: dict[int, float],
    whitelist: set[int],
    priority_size: int,
    latest_priority_ids: list[int],
) -> tuple[list[int], list[int]]:
    uniq = list(dict.fromkeys(ids))
    latest_set = set(latest_priority_ids)
    # For equal weights, prefer newer campaign IDs first.
    wl_sorted = sorted([x for x in uniq if x in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))
    latest_sorted = sorted([x for x in uniq if x in latest_set and x not in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))
    non_wl = sorted([x for x in uniq if x not in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))

    forced = wl_sorted + latest_sorted
    forced_set = set(forced)
    need = max(0, priority_size - len(forced))
    picked_non_wl = [x for x in non_wl if x not in forced_set][:need]
    priority_set = set(forced + picked_non_wl)

    priority = [x for x in (forced + picked_non_wl) if x in uniq]
    regular = [x for x in non_wl if x not in priority_set]
    return priority, regular


def _needs_weight_recalc(ids: list[int], cached: dict[int, float]) -> bool:
    if not cached:
        return True
    return set(ids) != set(cached.keys())


def _partition_complete(ids: list[int], p1: list[int], p2: list[int]) -> bool:
    base = set(ids)
    return base == (set(p1) | set(p2))


def _campaign_sql_for_major(query_template: str, mautic_major: int | None) -> str:
    if mautic_major != 4:
        return query_template
    # Mautic 4 schemas may not have campaigns.deleted; strip this predicate safely.
    patched = _M4_CAMPAIGN_DELETED_CLAUSE_RE.sub("", query_template)
    if patched == query_template:
        return query_template
    return re.sub(r"\s{2,}", " ", patched).strip()


def _is_db_dispatch_pause_error(exc: Exception) -> bool:
    return bool(_DB_DISPATCH_PAUSE_ERROR_RE.search(str(exc or "")))


def _mark_db_dispatch_pause(
    *,
    root: str,
    reason: str,
    now_ts: float,
    pause_until: dict[str, float],
    pause_reasons: dict[str, str],
    pause_sec: int = _DB_DISPATCH_PAUSE_SEC,
) -> None:
    until = now_ts + float(max(30, int(pause_sec)))
    prev_until = float(pause_until.get(root, 0.0))
    prev_reason = pause_reasons.get(root, "")
    pause_until[root] = max(prev_until, until)
    pause_reasons[root] = reason
    if prev_until <= now_ts or prev_reason != reason:
        logging.warning(
            "[%s] db dispatch circuit-breaker active for %ss: %s",
            root,
            int(max(30, int(pause_sec))),
            reason,
        )


def _clear_campaign_rings(
    *,
    root: str,
    trigger_prio_rings: dict[str, deque[int]],
    trigger_reg_rings: dict[str, deque[int]],
    rebuild_prio_rings: dict[str, deque[int]],
    rebuild_reg_rings: dict[str, deque[int]],
    trigger_prio_sets: dict[str, set[int]],
    trigger_reg_sets: dict[str, set[int]],
    rebuild_prio_sets: dict[str, set[int]],
    rebuild_reg_sets: dict[str, set[int]],
) -> None:
    trigger_prio_rings[root] = deque()
    trigger_reg_rings[root] = deque()
    rebuild_prio_rings[root] = deque()
    rebuild_reg_rings[root] = deque()
    trigger_prio_sets[root] = set()
    trigger_reg_sets[root] = set()
    rebuild_prio_sets[root] = set()
    rebuild_reg_sets[root] = set()


def _fill_from_ring(
    *,
    ring: deque[int],
    ring_limit: int,
    total_limit: int,
    root: str,
    task_type: str,
    running: dict[str, RunningTask],
    ring_entities: set[int] | None,
    config: AgentConfig,
    store: TaskStore,
    popens: dict[str, subprocess.Popen[bytes]],
    build_args,
    blocked_entities: set[int] | None = None,
    remove_on_launch: bool = False,
) -> None:
    if not ring or ring_limit <= 0 or total_limit <= 0:
        return
    scans = len(ring)
    while _running_count_for_entities(running, root, task_type, ring_entities) < ring_limit and scans > 0:
        eid = ring[0]
        scans -= 1
        if blocked_entities and eid in blocked_entities:
            ring.rotate(-1)
            continue
        if _is_running(running, root, task_type, eid):
            ring.rotate(-1)
            continue
        if not _launch_allowed(config, root, task_type, eid):
            ring.rotate(-1)
            continue
        args = build_args(eid)
        launched = _submit_if_slot(
            config=config,
            store=store,
            running=running,
            root=root,
            task_type=task_type,
            entity_id=eid,
            args=args,
            timeout_sec=config.command_timeout_sec,
            max_parallel_for_type=max(1, total_limit),
            popens=popens,
        )
        if launched:
            _advance_ring_after_launch(ring, eid, remove_on_launch=remove_on_launch)
        else:
            break


def _compute_throttle_active(samples: deque[tuple[float, int]], threshold: int, window_min: int) -> bool:
    if not samples:
        return False
    now = time.time()
    window_sec = max(60, window_min * 60)
    while samples and now - samples[0][0] > window_sec:
        samples.popleft()
    if not samples:
        return False
    if now - samples[0][0] < window_sec:
        return False
    return min(v for _, v in samples) >= threshold


class TaskStore:
    def __init__(self, path: str, cfg: AgentConfig | None = None) -> None:
        self.path = path
        self.cfg = cfg
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        try:
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        self._init_sqlite_schema()
        self._node_id = _state_node_id(cfg)
        aliases = {str(self._node_id or "").strip()}
        if cfg is not None:
            try:
                aliases.update(cluster_local_identity_values(cfg))
            except Exception:
                pass
        self._node_aliases = {x.strip() for x in aliases if str(x or "").strip()}
        self._node_aliases_lc = {x.lower() for x in self._node_aliases}

        self._mysql_mode = bool(
            cfg is not None
            and normalized_state_backend(cfg) == "mysql_hybrid"
            and mysql_state_enabled(cfg)
        )
        self._mysql_tables = mysql_state_table_names(cfg) if self._mysql_mode and cfg is not None else {}
        self._mysql_conn = None
        self._mysql_retry_after_ts = 0.0
        if self._mysql_mode:
            if self.cfg is not None:
                try:
                    ensure_mysql_state_schema(self.cfg)
                except Exception:
                    self._mysql_mark_error()
            if self._mysql_available(force_probe=True) and self._migrate_sqlite_to_mysql_once():
                self._sqlite_prune_for_failover()

    def _is_local_host_alias(self, host_name: str | None) -> bool:
        host_lc = str(host_name or "").strip().lower()
        return bool(host_lc and host_lc in self._node_aliases_lc)

    def _mysql_alias_where(self) -> tuple[str, tuple[str, ...]]:
        aliases = tuple(sorted(self._node_aliases or {self._node_id}))
        placeholders = ",".join(["%s"] * len(aliases))
        return placeholders, aliases

    def _init_sqlite_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              task_key TEXT NOT NULL,
              task_type TEXT NOT NULL,
              entity_id INTEGER,
              command_str TEXT NOT NULL,
              pid INTEGER NOT NULL,
              timeout_sec INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 1,
              manual_request_id INTEGER,
              state TEXT NOT NULL,
              note TEXT,
              started_at REAL NOT NULL,
              finished_at REAL,
              rc INTEGER
            )
            """
        )
        self._ensure_sqlite_column("tasks", "manual_request_id", "ALTER TABLE tasks ADD COLUMN manual_request_id INTEGER")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_running ON tasks(state, root, task_type)")
        self.conn.execute("DROP INDEX IF EXISTS idx_tasks_task_key_running")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_task_key_started ON tasks(task_key, started_at)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weight_cache (
              kind TEXT NOT NULL,
              root TEXT NOT NULL,
              entity_id INTEGER NOT NULL,
              weight REAL NOT NULL,
              computed_at REAL NOT NULL,
              PRIMARY KEY(kind, root, entity_id)
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_weight_cache_lookup ON weight_cache(kind, root, computed_at)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_sync (
              key TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              task_type TEXT NOT NULL,
              entity_id INTEGER,
              command_str TEXT NOT NULL,
              timeout_sec INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              note TEXT,
              task_key TEXT,
              requested_at REAL NOT NULL,
              launched_at REAL,
              finished_at REAL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_requests_pending ON manual_requests(status, root, requested_at)"
        )
        self.conn.commit()

    def _ensure_sqlite_column(self, table: str, column: str, ddl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        cols = {str(r["name"]) for r in cur.fetchall()}
        if column not in cols:
            self.conn.execute(ddl)

    def _mysql_mark_error(self) -> None:
        self._mysql_retry_after_ts = time.time() + 15.0
        try:
            if self._mysql_conn is not None:
                self._mysql_conn.close()
        except Exception:
            pass
        self._mysql_conn = None

    def _mysql_available(self, *, force_probe: bool = False) -> bool:
        if not self._mysql_mode or self.cfg is None:
            return False
        now = time.time()
        if not force_probe and now < self._mysql_retry_after_ts:
            return False
        if self._mysql_conn is not None:
            try:
                self._mysql_conn.ping(reconnect=True)
                return True
            except Exception:
                self._mysql_mark_error()
        try:
            self._mysql_conn = mysql_state_connection(self.cfg)
            self._mysql_retry_after_ts = 0.0
            return True
        except Exception:
            self._mysql_mark_error()
            return False

    def _mysql_query(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
                return [dict(r) for r in rows if isinstance(r, dict)]
        except Exception:
            self._mysql_mark_error()
            raise

    def _mysql_exec(self, sql: str, params: tuple[object, ...] = ()) -> tuple[int, int]:
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.lastrowid or 0), int(cur.rowcount or 0)
        except Exception:
            self._mysql_mark_error()
            raise

    def _mysql_exec_many(self, sql: str, params: list[tuple[object, ...]]) -> int:
        if not params:
            return 0
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.executemany(sql, params)
                return int(cur.rowcount or 0)
        except Exception:
            self._mysql_mark_error()
            raise

    def _sqlite_fetchall_dicts(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _sqlite_prune_for_failover(self) -> None:
        """In mysql mode keep only minimum local fallback footprint."""
        if not self._mysql_mode:
            return
        try:
            self.conn.execute("DELETE FROM tasks WHERE state!='running'")
            self.conn.execute("DELETE FROM manual_requests WHERE status NOT IN ('pending','launched')")
            self.conn.execute("DELETE FROM weight_cache")
            # Keep runtime_sync only for migration marker and local runtime hints.
            self.conn.execute(
                """
                DELETE FROM runtime_sync
                WHERE key NOT IN ('taskstore_mysql_migrated_v1', 'local_runtime', 'mcc_runtime')
                """
            )
            self.conn.commit()
        except Exception:
            pass

    def sync_sqlite_running_shadow(self) -> dict[str, int]:
        """
        Keep the local SQLite failover shadow aligned with MySQL running rows.

        Signals and some local diagnostics read the SQLite shadow directly, so in
        mysql_hybrid mode we periodically resync it from the authoritative MySQL
        task table and drop orphaned/stale local rows.
        """
        stats = {"mysql_rows": 0, "sqlite_before": 0, "sqlite_after": 0, "replaced": 0}
        if not self._mysql_mode:
            return stats
        tasks_table = self._mysql_tables.get("tasks", "")
        if not tasks_table or not self._mysql_available():
            return stats
        try:
            rows = self._mysql_query(
                f"""
                SELECT id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                FROM `{tasks_table}`
                WHERE host_name=%s AND state='running'
                ORDER BY id ASC
                """,
                (self._node_id,),
            )
        except Exception:
            return stats

        try:
            row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks").fetchone()
            stats["sqlite_before"] = int(row["cnt"] if row else 0)
            self.conn.execute("DELETE FROM tasks")
            if rows:
                self.conn.executemany(
                    """
                    INSERT OR REPLACE INTO tasks(
                      id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            int(r.get("id") or 0),
                            str(r.get("root") or ""),
                            str(r.get("task_key") or ""),
                            str(r.get("task_type") or ""),
                            r.get("entity_id"),
                            str(r.get("command_str") or ""),
                            int(r.get("pid") or 0),
                            int(r.get("timeout_sec") or 0),
                            int(r.get("attempts") or 1),
                            r.get("manual_request_id"),
                            "running",
                            float(r.get("started_at") or 0.0),
                        )
                        for r in rows
                    ],
                )
            self.conn.commit()
            row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks").fetchone()
            stats["mysql_rows"] = len(rows)
            stats["sqlite_after"] = int(row["cnt"] if row else 0)
            stats["replaced"] = max(stats["sqlite_before"], stats["sqlite_after"])
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
        return stats

    def _migrate_sqlite_to_mysql_once(self) -> bool:
        if not self._mysql_mode:
            return False
        marker = self.conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key='taskstore_mysql_migrated_v1' LIMIT 1"
        ).fetchone()
        if marker:
            raw = str(marker["payload_json"] or "")
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            if str(payload.get("status") or "").strip().lower() == "ok":
                return True
        if not self._mysql_available():
            return False

        tasks_table = self._mysql_tables.get("tasks", "")
        weight_table = self._mysql_tables.get("weight_cache", "")
        runtime_table = self._mysql_tables.get("runtime_sync", "")
        req_table = self._mysql_tables.get("manual_requests", "")
        if not all([tasks_table, weight_table, runtime_table, req_table]):
            return False

        migrated = {"tasks": 0, "weights": 0, "runtime_sync": 0, "manual_requests": 0}
        try:
            # tasks
            rows = self._sqlite_fetchall_dicts(
                """
                SELECT id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec,
                       attempts, manual_request_id, state, note, started_at, finished_at, rc
                FROM tasks
                ORDER BY id ASC
                """
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{tasks_table}`(
                      host_name, root, task_key, task_type, entity_id, command_str, pid, timeout_sec,
                      attempts, manual_request_id, state, note, started_at, finished_at, rc
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      host_name=VALUES(host_name),
                      root=VALUES(root),
                      task_key=VALUES(task_key),
                      task_type=VALUES(task_type),
                      entity_id=VALUES(entity_id),
                      command_str=VALUES(command_str),
                      pid=VALUES(pid),
                      timeout_sec=VALUES(timeout_sec),
                      attempts=VALUES(attempts),
                      manual_request_id=VALUES(manual_request_id),
                      state=VALUES(state),
                      note=VALUES(note),
                      started_at=VALUES(started_at),
                      finished_at=VALUES(finished_at),
                      rc=VALUES(rc)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["root"] or ""),
                            str(r["task_key"] or ""),
                            str(r["task_type"] or ""),
                            r["entity_id"],
                            str(r["command_str"] or ""),
                            int(r["pid"] or 0),
                            int(r["timeout_sec"] or 0),
                            int(r["attempts"] or 1),
                            r["manual_request_id"],
                            str(r["state"] or ""),
                            r.get("note"),
                            float(r["started_at"] or 0.0),
                            (float(r["finished_at"]) if r.get("finished_at") is not None else None),
                            r.get("rc"),
                        )
                        for r in rows
                    ],
                )
                migrated["tasks"] = len(rows)

            # weight_cache
            rows = self._sqlite_fetchall_dicts(
                "SELECT kind, root, entity_id, weight, computed_at FROM weight_cache"
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{weight_table}`(host_name, kind, root, entity_id, weight, computed_at)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      weight=VALUES(weight),
                      computed_at=VALUES(computed_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["kind"] or ""),
                            str(r["root"] or ""),
                            int(r["entity_id"] or 0),
                            float(r["weight"] or 0.0),
                            float(r["computed_at"] or 0.0),
                        )
                        for r in rows
                    ],
                )
                migrated["weights"] = len(rows)

            # runtime_sync
            rows = self._sqlite_fetchall_dicts("SELECT key, payload_json, updated_at FROM runtime_sync")
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{runtime_table}`(host_name, `key`, payload_json, updated_at)
                    VALUES(%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      payload_json=VALUES(payload_json),
                      updated_at=VALUES(updated_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["key"] or ""),
                            str(r["payload_json"] or "{}"),
                            float(r["updated_at"] or time.time()),
                        )
                        for r in rows
                    ],
                )
                migrated["runtime_sync"] = len(rows)

            # manual_requests
            rows = self._sqlite_fetchall_dicts(
                """
                SELECT id, root, task_type, entity_id, command_str, timeout_sec, status,
                       note, task_key, requested_at, launched_at, finished_at
                FROM manual_requests
                ORDER BY id ASC
                """
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{req_table}`(
                      host_name, root, task_type, entity_id, command_str, timeout_sec, status,
                      note, task_key, requested_at, launched_at, finished_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      host_name=VALUES(host_name),
                      root=VALUES(root),
                      task_type=VALUES(task_type),
                      entity_id=VALUES(entity_id),
                      command_str=VALUES(command_str),
                      timeout_sec=VALUES(timeout_sec),
                      status=VALUES(status),
                      note=VALUES(note),
                      task_key=VALUES(task_key),
                      requested_at=VALUES(requested_at),
                      launched_at=VALUES(launched_at),
                      finished_at=VALUES(finished_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["root"] or ""),
                            str(r["task_type"] or ""),
                            r["entity_id"],
                            str(r["command_str"] or ""),
                            int(r["timeout_sec"] or 0),
                            str(r["status"] or "pending"),
                            r.get("note"),
                            r.get("task_key"),
                            float(r["requested_at"] or 0.0),
                            (float(r["launched_at"]) if r.get("launched_at") is not None else None),
                            (float(r["finished_at"]) if r.get("finished_at") is not None else None),
                        )
                        for r in rows
                    ],
                )
                migrated["manual_requests"] = len(rows)

            self.conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    "taskstore_mysql_migrated_v1",
                    json.dumps(
                        {"status": "ok", "migrated": migrated, "at": int(time.time())},
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )
            self.conn.commit()
        except Exception as e:
            self.conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    "taskstore_mysql_migrated_v1",
                    json.dumps(
                        {"status": "error", "error": str(e), "at": int(time.time())},
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )
            self.conn.commit()
            return False
        return True

    def mark_old_running_lost(self) -> None:
        now = time.time()
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        UPDATE `{tasks_table}`
                        SET state='lost', note=%s, finished_at=%s
                        WHERE host_name=%s AND state='running'
                        """,
                        ("daemon_restart", now, self._node_id),
                    )
                except Exception:
                    pass
        self.conn.execute(
            "UPDATE tasks SET state='lost', note='daemon_restart', finished_at=? WHERE state='running'",
            (now,),
        )
        self.conn.commit()

    def add_running(self, task: RunningTask) -> int:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    row_id, _ = self._mysql_exec(
                        f"""
                        INSERT INTO `{tasks_table}`(
                          host_name, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            self._node_id,
                            task.root,
                            task.task_key,
                            task.task_type,
                            task.entity_id,
                            task.command_str,
                            task.pid,
                            task.timeout_sec,
                            task.attempts,
                            task.manual_request_id,
                            "running",
                            task.started_at,
                        ),
                    )
                    # Minimal failover shadow (running rows only).
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO tasks(
                          id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(row_id),
                            task.root,
                            task.task_key,
                            task.task_type,
                            task.entity_id,
                            task.command_str,
                            task.pid,
                            task.timeout_sec,
                            task.attempts,
                            task.manual_request_id,
                            "running",
                            task.started_at,
                        ),
                    )
                    self.conn.commit()
                    return int(row_id)
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            INSERT INTO tasks(
              root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.root,
                task.task_key,
                task.task_type,
                task.entity_id,
                task.command_str,
                task.pid,
                task.timeout_sec,
                task.attempts,
                task.manual_request_id,
                "running",
                task.started_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish(self, row_id: int, state: str, rc: int | None, note: str | None) -> None:
        now = time.time()
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    _, changed = self._mysql_exec(
                        f"""
                        UPDATE `{tasks_table}`
                        SET state=%s, rc=%s, note=%s, finished_at=%s
                        WHERE id=%s AND host_name=%s
                        """,
                        (state, rc, note, now, int(row_id), self._node_id),
                    )
                    if int(changed) > 0:
                        # Keep SQLite tiny in mysql mode: running failover only.
                        self.conn.execute("DELETE FROM tasks WHERE id=?", (int(row_id),))
                        self.conn.commit()
                        return
                except Exception:
                    pass

        self.conn.execute(
            "UPDATE tasks SET state=?, rc=?, note=?, finished_at=? WHERE id=?",
            (state, rc, note, now, int(row_id)),
        )
        self.conn.commit()

    def running_rows(self) -> list[dict[str, object]]:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    return self._mysql_query(
                        f"SELECT * FROM `{tasks_table}` WHERE host_name=%s AND state='running' ORDER BY id ASC",
                        (self._node_id,),
                    )
                except Exception:
                    pass
        return self._sqlite_fetchall_dicts("SELECT * FROM tasks WHERE state='running' ORDER BY id ASC")

    def running_count(self) -> int:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state='running'",
                        (self._node_id,),
                    )
                    return int((rows[0].get("cnt") if rows else 0) or 0)
                except Exception:
                    pass
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state='running'").fetchone()
        return int(row["cnt"] if row else 0)

    def running_task_summaries(self) -> list[dict[str, object]]:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    return self._mysql_query(
                        f"""
                        SELECT id, root, task_type, entity_id, pid, command_str
                        FROM `{tasks_table}`
                        WHERE host_name=%s AND state='running'
                        ORDER BY id ASC
                        """,
                        (self._node_id,),
                    )
                except Exception:
                    pass
        return self._sqlite_fetchall_dicts(
            "SELECT id, root, task_type, entity_id, pid, command_str FROM tasks WHERE state='running' ORDER BY id ASC"
        )

    def has_running_task_key(self, task_key: str) -> bool:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT 1 AS present
                        FROM `{tasks_table}`
                        WHERE host_name=%s AND state='running' AND task_key=%s
                        LIMIT 1
                        """,
                        (self._node_id, str(task_key)),
                    )
                    return bool(rows)
                except Exception:
                    pass
        row = self.conn.execute(
            "SELECT 1 AS present FROM tasks WHERE state='running' AND task_key=? LIMIT 1",
            (str(task_key),),
        ).fetchone()
        return bool(row)

    def last_task_started_at(self, task_key: str) -> float:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT MAX(started_at) AS started_at
                        FROM `{tasks_table}`
                        WHERE host_name=%s AND task_key=%s
                        """,
                        (self._node_id, str(task_key)),
                    )
                    return float((rows[0].get("started_at") if rows else 0.0) or 0.0)
                except Exception:
                    pass
        row = self.conn.execute(
            "SELECT MAX(started_at) AS started_at FROM tasks WHERE task_key=?",
            (str(task_key),),
        ).fetchone()
        return float((row["started_at"] if row else 0.0) or 0.0)

    def recent_task_problem_counts(
        self,
        *,
        root: str,
        task_type: str,
        since_sec: int,
        states: tuple[str, ...] = ("failed", "timeout", "lost"),
    ) -> dict[int, int]:
        cutoff = time.time() - float(max(60, int(since_sec)))
        wanted_states = tuple(str(s or "").strip().lower() for s in states if str(s or "").strip())
        if not wanted_states:
            return {}

        out: dict[int, int] = {}
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    placeholders = ",".join(["%s"] * len(wanted_states))
                    rows = self._mysql_query(
                        f"""
                        SELECT entity_id, COUNT(*) AS cnt
                        FROM `{tasks_table}`
                        WHERE host_name=%s
                          AND root=%s
                          AND task_type=%s
                          AND entity_id IS NOT NULL
                          AND state IN ({placeholders})
                          AND COALESCE(finished_at, started_at) >= %s
                        GROUP BY entity_id
                        """,
                        (self._node_id, str(root), str(task_type), *wanted_states, cutoff),
                    )
                    for row in rows:
                        try:
                            eid = int(row.get("entity_id") or 0)
                            cnt = int(row.get("cnt") or 0)
                        except Exception:
                            continue
                        if eid > 0 and cnt > 0:
                            out[eid] = cnt
                    return out
                except Exception:
                    pass

        placeholders = ",".join(["?"] * len(wanted_states))
        rows = self._sqlite_fetchall_dicts(
            f"""
            SELECT entity_id, COUNT(*) AS cnt
            FROM tasks
            WHERE root=?
              AND task_type=?
              AND entity_id IS NOT NULL
              AND state IN ({placeholders})
              AND COALESCE(finished_at, started_at) >= ?
            GROUP BY entity_id
            """,
            (str(root), str(task_type), *wanted_states, cutoff),
        )
        for row in rows:
            try:
                eid = int(row.get("entity_id") or 0)
                cnt = int(row.get("cnt") or 0)
            except Exception:
                continue
            if eid > 0 and cnt > 0:
                out[eid] = cnt
        return out

    def get_weights(self, kind: str, root: str, max_age_sec: int) -> dict[int, float]:
        min_ts = time.time() - max(1, max_age_sec)
        out: dict[int, float] = {}
        if self._mysql_mode:
            table = self._mysql_tables.get("weight_cache", "")
            if table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT entity_id, weight
                        FROM `{table}`
                        WHERE host_name=%s AND kind=%s AND root=%s AND computed_at>=%s
                        """,
                        (self._node_id, kind, root, min_ts),
                    )
                    for row in rows:
                        out[int(row["entity_id"])] = float(row["weight"])
                    return out
                except Exception:
                    pass

        cur = self.conn.execute(
            "SELECT entity_id, weight FROM weight_cache WHERE kind=? AND root=? AND computed_at>=?",
            (kind, root, min_ts),
        )
        for row in cur.fetchall():
            out[int(row["entity_id"])] = float(row["weight"])
        return out

    def put_weights(self, kind: str, root: str, weights: dict[int, float]) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("weight_cache", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"DELETE FROM `{table}` WHERE host_name=%s AND kind=%s AND root=%s",
                        (self._node_id, kind, root),
                    )
                    params = [(self._node_id, kind, root, int(eid), float(w), now) for eid, w in weights.items()]
                    if params:
                        self._mysql_exec_many(
                            f"""
                            INSERT INTO `{table}`(host_name, kind, root, entity_id, weight, computed_at)
                            VALUES(%s,%s,%s,%s,%s,%s)
                            """,
                            params,
                        )
                    # Not required in mysql mode; keep sqlite cache small.
                    self.conn.execute("DELETE FROM weight_cache WHERE kind=? AND root=?", (kind, root))
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute("DELETE FROM weight_cache WHERE kind=? AND root=?", (kind, root))
        self.conn.executemany(
            "INSERT INTO weight_cache(kind, root, entity_id, weight, computed_at) VALUES(?,?,?,?,?)",
            [(kind, root, int(eid), float(w), now) for eid, w in weights.items()],
        )
        self.conn.commit()

    def compact_tasks(
        self,
        *,
        now_ts: float,
        keep_days: int,
        max_rows: int,
        run_vacuum: bool,
        archive_enabled: bool = True,
        archive_dir: str = "/opt/mcd/var/task-history",
        archive_keep_days: int = 14,
    ) -> tuple[int, int, bool]:
        """Compact historical task rows (non-running only).

        Returns: (deleted_rows, remaining_non_running_rows, vacuum_done)
        """
        deleted_rows = 0
        keep_days = max(0, int(keep_days))
        max_rows = max(0, int(max_rows))
        archive_keep_days = max(1, int(archive_keep_days))

        def _prune_task_archives() -> None:
            if not archive_enabled:
                return
            base = Path(archive_dir)
            if not base.exists():
                return
            cutoff = float(now_ts) - (float(archive_keep_days) * 86400.0)
            for item in base.glob("tasks-*.jsonl.gz"):
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink()
                except Exception:
                    continue

        def _archive_rows(rows_iter: object) -> int:
            if not archive_enabled:
                return 0
            try:
                base = Path(archive_dir)
                base.mkdir(parents=True, exist_ok=True)
                stamp = datetime.fromtimestamp(float(now_ts), tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
                target = base / f"tasks-{stamp}.jsonl.gz"
                written = 0
                with gzip.open(target, "at", encoding="utf-8") as fh:
                    for row in rows_iter:  # type: ignore[union-attr]
                        fh.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
                        written += 1
                if written == 0:
                    try:
                        target.unlink()
                    except Exception:
                        pass
                return written
            except Exception as e:
                logging.warning("tasks archive failed: %s", e)
                return 0

        def _archive_sqlite_rows(where_sql: str, params: tuple[object, ...]) -> int:
            if not archive_enabled:
                return 0
            rows = self.conn.execute(
                f"""
                SELECT id, root, task_key, task_type, entity_id, command_str,
                       pid, timeout_sec, attempts, state, note, started_at,
                       finished_at, rc, manual_request_id
                FROM tasks
                WHERE {where_sql}
                ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                """,
                params,
            )
            return _archive_rows(rows)

        def _archive_mysql_rows(tasks_table: str, where_sql: str, params: tuple[object, ...]) -> int:
            if not archive_enabled or not tasks_table:
                return 0
            try:
                rows = self._mysql_query(
                    f"""
                    SELECT id, host_name, root, task_key, task_type, entity_id,
                           command_str, pid, timeout_sec, attempts, state, note,
                           started_at, finished_at, rc, manual_request_id
                    FROM `{tasks_table}`
                    WHERE host_name=%s AND {where_sql}
                    ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                    """,
                    (self._node_id, *params),
                )
                return _archive_rows(rows)
            except Exception as e:
                logging.warning("tasks mysql archive failed: %s", e)
                return 0

        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    if keep_days > 0:
                        cutoff = float(now_ts) - (float(keep_days) * 86400.0)
                        _archive_mysql_rows(
                            tasks_table,
                            "state!='running' AND COALESCE(finished_at, started_at) < %s",
                            (cutoff,),
                        )
                        _, cnt = self._mysql_exec(
                            f"""
                            DELETE FROM `{tasks_table}`
                            WHERE host_name=%s AND state!='running' AND COALESCE(finished_at, started_at) < %s
                            """,
                            (self._node_id, cutoff),
                        )
                        deleted_rows += int(cnt)

                    rows = self._mysql_query(
                        f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state!='running'",
                        (self._node_id,),
                    )
                    non_running = int((rows[0].get("cnt") if rows else 0) or 0)
                    if max_rows > 0 and non_running > max_rows:
                        overflow = non_running - max_rows
                        _archive_mysql_rows(
                            tasks_table,
                            """
                            id IN (
                              SELECT id FROM (
                                SELECT id
                                FROM `{tasks_table}`
                                WHERE host_name=%s AND state!='running'
                                ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                                LIMIT %s
                              ) t
                            )
                            """.replace("{tasks_table}", tasks_table),
                            (self._node_id, overflow),
                        )
                        _, cnt = self._mysql_exec(
                            f"""
                            DELETE FROM `{tasks_table}`
                            WHERE id IN (
                              SELECT id FROM (
                                SELECT id
                                FROM `{tasks_table}`
                                WHERE host_name=%s AND state!='running'
                                ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                                LIMIT %s
                              ) t
                            )
                            """,
                            (self._node_id, overflow),
                        )
                        deleted_rows += int(cnt)
                        rows = self._mysql_query(
                            f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state!='running'",
                            (self._node_id,),
                        )
                        non_running = int((rows[0].get("cnt") if rows else 0) or 0)

                    # Keep sqlite fallback minimal (running only) in mysql mode.
                    _archive_sqlite_rows("state!='running'", ())
                    self.conn.execute("DELETE FROM tasks WHERE state!='running'")
                    self.conn.commit()
                    _prune_task_archives()
                    return deleted_rows, non_running, False
                except Exception:
                    pass

        if keep_days > 0:
            cutoff = float(now_ts) - (float(keep_days) * 86400.0)
            _archive_sqlite_rows("state!='running' AND COALESCE(finished_at, started_at) < ?", (cutoff,))
            cur = self.conn.execute(
                "DELETE FROM tasks WHERE state!='running' AND COALESCE(finished_at, started_at) < ?",
                (cutoff,),
            )
            deleted_rows += int(cur.rowcount or 0)

        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state!='running'").fetchone()
        non_running = int(row["cnt"] if row else 0)
        if max_rows > 0 and non_running > max_rows:
            overflow = non_running - max_rows
            _archive_sqlite_rows(
                """
                id IN (
                  SELECT id
                  FROM tasks
                  WHERE state!='running'
                  ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                  LIMIT ?
                )
                """,
                (overflow,),
            )
            cur = self.conn.execute(
                """
                DELETE FROM tasks
                WHERE id IN (
                  SELECT id
                  FROM tasks
                  WHERE state!='running'
                  ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                  LIMIT ?
                )
                """,
                (overflow,),
            )
            deleted_rows += int(cur.rowcount or 0)
            row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state!='running'").fetchone()
            non_running = int(row["cnt"] if row else 0)

        self.conn.commit()

        vacuum_done = False
        if run_vacuum and deleted_rows > 0:
            self.conn.execute("VACUUM")
            vacuum_done = True

        _prune_task_archives()

        return deleted_rows, non_running, vacuum_done

    def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        INSERT INTO `{table}`(host_name, `key`, payload_json, updated_at)
                        VALUES(%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json), updated_at=VALUES(updated_at)
                        """,
                        (self._node_id, str(key), json.dumps(payload, ensure_ascii=False), now),
                    )
                except Exception:
                    pass
        self.conn.execute(
            """
            INSERT INTO runtime_sync(key, payload_json, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (str(key), json.dumps(payload, ensure_ascii=False), now),
        )
        self.conn.commit()

    def get_runtime_sync(self, key: str) -> dict[str, object] | None:
        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"SELECT payload_json FROM `{table}` WHERE host_name=%s AND `key`=%s LIMIT 1",
                        (self._node_id, str(key)),
                    )
                    if rows:
                        raw = str(rows[0].get("payload_json") or "").strip()
                        if raw:
                            payload = json.loads(raw)
                            if isinstance(payload, dict):
                                return payload
                except Exception:
                    pass
        row = self.conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key=? LIMIT 1",
            (str(key),),
        ).fetchone()
        if not row:
            return None
        raw = str(row["payload_json"] or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def enqueue_manual_request(
        self,
        *,
        root: str,
        task_type: str,
        entity_id: int | None,
        command_str: str,
        timeout_sec: int,
        target_host_name: str | None = None,
    ) -> int:
        now = time.time()
        target_host = str(target_host_name or self._node_id).strip() or self._node_id
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    req_id, _ = self._mysql_exec(
                        f"""
                        INSERT INTO `{table}`(
                          host_name, root, task_type, entity_id, command_str, timeout_sec, status, requested_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,'pending',%s)
                        """,
                        (target_host, str(root), str(task_type), entity_id, str(command_str), int(timeout_sec), now),
                    )
                    if self._is_local_host_alias(target_host):
                        self.conn.execute(
                            """
                            INSERT OR REPLACE INTO manual_requests(
                              id, root, task_type, entity_id, command_str, timeout_sec, status, requested_at
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            (
                                int(req_id),
                                str(root),
                                str(task_type),
                                entity_id,
                                str(command_str),
                                int(timeout_sec),
                                "pending",
                                now,
                            ),
                        )
                        self.conn.commit()
                    return int(req_id)
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            INSERT INTO manual_requests(
              root, task_type, entity_id, command_str, timeout_sec, status, requested_at
            ) VALUES(?,?,?,?,?,'pending',?)
            """,
            (
                str(root),
                str(task_type),
                entity_id,
                str(command_str),
                int(timeout_sec),
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending_manual_requests(self, root: str, limit: int = 32) -> list[dict[str, object]]:
        lim = max(1, int(limit))
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    placeholders, aliases = self._mysql_alias_where()
                    return self._mysql_query(
                        f"""
                        SELECT *
                        FROM `{table}`
                        WHERE host_name IN ({placeholders}) AND status='pending' AND root=%s
                        ORDER BY requested_at ASC, id ASC
                        LIMIT {lim}
                        """,
                        (*aliases, str(root)),
                    )
                except Exception:
                    pass

        return self._sqlite_fetchall_dicts(
            """
            SELECT *
            FROM manual_requests
            WHERE status='pending' AND root=?
            ORDER BY requested_at ASC, id ASC
            LIMIT ?
            """,
            (str(root), lim),
        )

    def get_manual_request_status(self, req_id: int) -> str | None:
        return self.get_manual_request_status_for_host(req_id, None)

    def get_manual_request_status_for_host(self, req_id: int, host_name: str | None = None) -> str | None:
        target_host = str(host_name or self._node_id).strip() or self._node_id
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    if host_name is None:
                        placeholders, aliases = self._mysql_alias_where()
                        rows = self._mysql_query(
                            f"SELECT status FROM `{table}` WHERE id=%s AND host_name IN ({placeholders}) LIMIT 1",
                            (int(req_id), *aliases),
                        )
                    else:
                        rows = self._mysql_query(
                            f"SELECT status FROM `{table}` WHERE id=%s AND host_name=%s LIMIT 1",
                            (int(req_id), target_host),
                        )
                    if rows:
                        return str(rows[0].get("status") or "")
                except Exception:
                    pass
        row = self.conn.execute("SELECT status FROM manual_requests WHERE id=?", (int(req_id),)).fetchone()
        if not row:
            return None
        return str(row["status"])

    def cancel_manual_request(self, req_id: int, note: str = "fallback_direct_exec") -> bool:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    placeholders, aliases = self._mysql_alias_where()
                    _, cnt = self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status='cancelled', note=%s, finished_at=%s
                        WHERE id=%s AND host_name IN ({placeholders}) AND status='pending'
                        """,
                        (str(note), now, int(req_id), *aliases),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status='cancelled', note=?, finished_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (str(note), now, int(req_id)),
                    )
                    self.conn.commit()
                    return int(cnt) > 0
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            UPDATE manual_requests
            SET status='cancelled', note=?, finished_at=?
            WHERE id=? AND status='pending'
            """,
            (str(note), now, int(req_id)),
        )
        self.conn.commit()
        return int(cur.rowcount or 0) > 0

    def mark_manual_request_launched(self, req_id: int, task_key: str) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    placeholders, aliases = self._mysql_alias_where()
                    self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status='launched', task_key=%s, launched_at=%s, note=NULL
                        WHERE id=%s AND host_name IN ({placeholders}) AND status='pending'
                        """,
                        (str(task_key), now, int(req_id), *aliases),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status='launched', task_key=?, launched_at=?, note=NULL
                        WHERE id=? AND status='pending'
                        """,
                        (str(task_key), now, int(req_id)),
                    )
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute(
            """
            UPDATE manual_requests
            SET status='launched', task_key=?, launched_at=?, note=NULL
            WHERE id=? AND status='pending'
            """,
            (str(task_key), now, int(req_id)),
        )
        self.conn.commit()

    def finish_manual_request(self, req_id: int, status: str, note: str | None = None) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    placeholders, aliases = self._mysql_alias_where()
                    self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status=%s, note=%s, finished_at=%s
                        WHERE id=%s AND host_name IN ({placeholders}) AND status IN ('pending','launched')
                        """,
                        (str(status), note, now, int(req_id), *aliases),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status=?, note=?, finished_at=?
                        WHERE id=? AND status IN ('pending','launched')
                        """,
                        (str(status), note, now, int(req_id)),
                    )
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute(
            """
            UPDATE manual_requests
            SET status=?, note=?, finished_at=?
            WHERE id=? AND status IN ('pending','launched')
            """,
            (str(status), note, now, int(req_id)),
        )
        self.conn.commit()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_cmdline_args(pid: int) -> list[str]:
    if pid <= 0:
        return []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return []
    if not raw:
        return []
    args: list[str] = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            args.append(chunk.decode("utf-8", errors="replace"))
        except Exception:
            continue
    return args


def _task_signature_tokens(command_str: str) -> list[str]:
    args = [x for x in str(command_str or "").split(_CMD_SEP) if x]
    signature = [x for x in args if x.startswith("mautic:") or x.endswith("/bin/console") or x == "bin/console"]
    id_opt_names = {"-i", "--id", "--list-id", "--campaign-id", "--segment-id"}
    id_opt_prefixes = ("--id=", "--list-id=", "--campaign-id=", "--segment-id=")
    identity: list[str] = []
    for idx, arg in enumerate(args):
        if arg in id_opt_names and idx + 1 < len(args):
            identity.extend([arg, args[idx + 1]])
            continue
        if any(arg.startswith(prefix) for prefix in id_opt_prefixes):
            identity.append(arg)
    if identity:
        signature.extend(identity)
    if signature:
        return signature
    # Fallback for unexpected/custom templates: use short tail.
    return args[-2:] if len(args) >= 2 else args


def _cmdline_has_token(cmdline_args: list[str], token: str) -> bool:
    if token in cmdline_args:
        return True
    if "/" in token:
        token_base = os.path.basename(token.rstrip("/"))
        if not token_base:
            return False
        for arg in cmdline_args:
            if os.path.basename(str(arg).rstrip("/")) == token_base:
                return True
    return False


def _pid_matches_task_command(pid: int, command_str: str) -> bool:
    cmdline_args = _pid_cmdline_args(pid)
    if not cmdline_args:
        return False
    signature = _task_signature_tokens(command_str)
    if not signature:
        return False
    return all(_cmdline_has_token(cmdline_args, tok) for tok in signature)


def _normalize_root_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve())
    except Exception:
        try:
            return str(Path(raw).absolute())
        except Exception:
            return raw


def _extract_mautic_task_entity_id(args: list[str], start_idx: int) -> int | None:
    id_names = {"-i", "--id", "--list-id", "--campaign-id", "--segment-id"}
    id_prefixes = ("--id=", "--list-id=", "--campaign-id=", "--segment-id=")
    for idx in range(max(0, int(start_idx)), len(args)):
        arg = str(args[idx] or "").strip()
        if not arg:
            continue
        if arg in id_names and idx + 1 < len(args):
            try:
                return int(str(args[idx + 1]).strip())
            except Exception:
                continue
        if arg.startswith("-i") and len(arg) > 2:
            try:
                return int(arg[2:])
            except Exception:
                continue
        for prefix in id_prefixes:
            if arg.startswith(prefix):
                try:
                    return int(arg[len(prefix) :].strip())
                except Exception:
                    break
    return None


def list_external_runtime_task_summaries(
    known_roots: list[str] | set[str],
    *,
    tracked_tasks: list[dict[str, object]] | None = None,
    tracked_pids: set[int] | None = None,
    timeout_sec: int = 4,
) -> list[dict[str, object]]:
    roots_map: dict[str, str] = {}
    for root in known_roots:
        root_raw = str(root or "").strip()
        if not root_raw:
            continue
        roots_map[_normalize_root_path(root_raw)] = root_raw
    if not roots_map:
        return []

    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,etimes=,args="],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    tracked = {int(x) for x in (tracked_pids or set()) if int(x) > 0}
    tracked_keys: set[tuple[str, str, int | None]] = set()
    for row in tracked_tasks or []:
        tracked_root = str(row.get("root") or "").strip()
        tracked_type = str(row.get("task_type") or "").strip()
        tracked_entity = row.get("entity_id")
        tracked_entity_id = int(tracked_entity) if tracked_entity is not None else None
        if tracked_root and tracked_type:
            tracked_keys.add((tracked_root, tracked_type, tracked_entity_id))
    rows: list[dict[str, object]] = []
    seen_pids: set[int] = set()
    now_ts = time.time()
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            elapsed_sec = int(parts[1])
        except Exception:
            continue
        if pid <= 0 or pid in seen_pids or pid in tracked:
            continue
        cmdline_args = _pid_cmdline_args(pid)
        if not cmdline_args:
            try:
                cmdline_args = shlex.split(str(parts[2] or "").strip())
            except Exception:
                cmdline_args = []
        if not cmdline_args:
            continue
        first_base = os.path.basename(str(cmdline_args[0] or "").strip())
        if first_base in _EXTERNAL_PROCESS_WRAPPERS:
            continue

        console_idx = -1
        console_path = ""
        for idx, arg in enumerate(cmdline_args):
            val = str(arg or "").strip()
            if val.endswith("/bin/console") or val.endswith("/app/console"):
                console_idx = idx
                console_path = val
                break
        if console_idx < 0 or not console_path:
            continue
        command_idx = -1
        command_name = ""
        for idx in range(console_idx + 1, len(cmdline_args)):
            val = str(cmdline_args[idx] or "").strip()
            if val.startswith("mautic:"):
                command_idx = idx
                command_name = val
                break
        task_type = _EXTERNAL_TASK_TYPES.get(command_name)
        if command_idx < 0 or not task_type:
            continue

        console_parent = Path(console_path).parent
        if console_parent.name not in {"bin", "app"}:
            continue
        root_norm = _normalize_root_path(str(console_parent.parent))
        root = roots_map.get(root_norm)
        if not root:
            continue

        entity_id = _extract_mautic_task_entity_id(cmdline_args, command_idx + 1)
        if (root, task_type, entity_id) in tracked_keys:
            continue
        rows.append(
            {
                "root": root,
                "task_type": task_type,
                "entity_id": entity_id,
                "pid": pid,
                "elapsed_sec": elapsed_sec,
                "started_at": float(now_ts - max(0, elapsed_sec)),
                "command_str": _CMD_SEP.join(str(x) for x in cmdline_args if str(x or "").strip()),
                "external": True,
            }
        )
        seen_pids.add(pid)
    rows.sort(key=lambda row: (str(row.get("root") or ""), str(row.get("task_type") or ""), int(row.get("pid") or 0)))
    return rows


def _external_task_key(root: str, task_type: str, entity_id: int | None, pid: int) -> str:
    entity = "-" if entity_id is None else str(int(entity_id))
    return f"external:{root}:{task_type}:{entity}:{int(pid)}"


def _sync_external_running_tasks(
    *,
    installs: list[object],
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
) -> dict[str, int]:
    stats = {"observed": 0, "adopted": 0, "updated": 0, "released": 0}
    roots = [str(getattr(inst, "root", "") or "").strip() for inst in installs]
    internal_pids = {int(t.pid) for t in running.values() if not bool(getattr(t, "external", False)) and int(t.pid or 0) > 0}
    internal_tasks = [
        {
            "root": t.root,
            "task_type": t.task_type,
            "entity_id": t.entity_id,
        }
        for t in running.values()
        if not bool(getattr(t, "external", False))
    ]
    observed_rows = list_external_runtime_task_summaries(roots, tracked_tasks=internal_tasks, tracked_pids=internal_pids)
    stats["observed"] = len(observed_rows)
    observed_by_key: dict[str, RunningTask] = {}
    for row in observed_rows:
        pid = int(row.get("pid") or 0)
        root = str(row.get("root") or "").strip()
        task_type = str(row.get("task_type") or "").strip()
        entity_id = int(row["entity_id"]) if row.get("entity_id") is not None else None
        key = _external_task_key(root, task_type, entity_id, pid)
        observed_by_key[key] = RunningTask(
            row_id=0,
            root=root,
            task_key=key,
            task_type=task_type,
            entity_id=entity_id,
            command_str=str(row.get("command_str") or ""),
            timeout_sec=0,
            attempts=1,
            started_at=float(row.get("started_at") or time.time()),
            pid=pid,
            manual_request_id=None,
            external=True,
        )

    for key, task in list(running.items()):
        if not bool(getattr(task, "external", False)):
            continue
        if key not in observed_by_key:
            running.pop(key, None)
            popens.pop(key, None)
            stats["released"] += 1

    for key, task in observed_by_key.items():
        cur = running.get(key)
        if cur is None:
            running[key] = task
            stats["adopted"] += 1
            continue
        if (
            not bool(getattr(cur, "external", False))
            or cur.pid != task.pid
            or cur.entity_id != task.entity_id
            or cur.task_type != task.task_type
            or cur.command_str != task.command_str
        ):
            running[key] = task
            stats["updated"] += 1
    return stats


def _kill_pid(pid: int, grace_sec: int) -> None:
    if not _is_pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + max(0, grace_sec)
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return
        time.sleep(0.5)
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _spawn_command(*, root: str, args: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _task_key(root: str, task_type: str, entity_id: int | None) -> str:
    eid = "-" if entity_id is None else str(entity_id)
    return f"{root}|{task_type}|{eid}"


def _task_repeat_interval_sec(config: AgentConfig, task_type: str) -> int:
    if task_type == "segment":
        return max(0, int(getattr(config, "segment_full_scan_interval_sec", 0) or 0))
    if task_type == "segment_sql":
        return max(0, int(getattr(config, "segment_sql_min_repeat_sec", 0)))
    if task_type == "import":
        return max(0, int(getattr(config, "import_poll_interval_sec", 0) or 0))
    if task_type == "campaign_trigger":
        min_repeat = max(0, int(getattr(config, "campaign_trigger_min_repeat_sec", 0)))
        audit_interval = max(0, int(getattr(config, "campaign_trigger_audit_interval_sec", 0) or 0))
        # One per-campaign trigger pass processes all currently available
        # batches. If SQL still sees the campaign as due immediately after
        # that pass, relaunching the same ID every daemon tick creates a
        # no-op storm. Keep retries aligned with the audit/planner cadence.
        return max(min_repeat, audit_interval)
    if task_type in {"campaign_rebuild", "campaign_update"}:
        return max(0, int(getattr(config, "campaign_rebuild_min_repeat_sec", 0)))
    return 0


def _entity_launch_guard_key(root: str, task_type: str, entity_id: int | None) -> str:
    return f"{_task_key(root, task_type, entity_id)}|launch"


def _launch_allowed(config: AgentConfig, root: str, task_type: str, entity_id: int | None, now_ts: float | None = None) -> bool:
    min_repeat = _task_repeat_interval_sec(config, task_type)
    if min_repeat <= 0:
        return True
    ts = float(now_ts if now_ts is not None else time.time())
    key = _entity_launch_guard_key(root, task_type, entity_id)
    prev = _ENTITY_LAUNCH_GUARD.get(key, 0.0)
    return (ts - prev) >= float(min_repeat)


def _prune_entity_launch_guard(max_age_sec: int = 3600, now_ts: float | None = None) -> None:
    if not _ENTITY_LAUNCH_GUARD:
        return
    ts = float(now_ts if now_ts is not None else time.time())
    stale = [k for k, v in _ENTITY_LAUNCH_GUARD.items() if ts - float(v) > float(max_age_sec)]
    for k in stale:
        _ENTITY_LAUNCH_GUARD.pop(k, None)


def _mark_import_settle(
    config: AgentConfig,
    root: str,
    now_ts: float | None = None,
    elapsed_sec: float | None = None,
) -> None:
    ts = float(now_ts if now_ts is not None else time.time())
    base_settle_sec = max(5, int(getattr(config, "import_poll_interval_sec", 15) or 15))
    # A near-instant import usually means no durable work was available.
    # Give Mautic time to settle status fields before polling again, otherwise
    # the daemon can catch its own transient in_progress row and relaunch a
    # no-op import forever.
    if elapsed_sec is not None and float(elapsed_sec) < 2.0:
        settle_sec = max(60, base_settle_sec * 4)
    else:
        settle_sec = base_settle_sec
    _IMPORT_SETTLE_UNTIL[str(root)] = ts + float(settle_sec)
    logging.info("[%s] import settle active for %ss", root, int(settle_sec))


def _import_in_settle(root: str, now_ts: float | None = None) -> bool:
    ts = float(now_ts if now_ts is not None else time.time())
    until = float(_IMPORT_SETTLE_UNTIL.get(str(root), 0.0) or 0.0)
    if until <= 0:
        return False
    if ts < until:
        return True
    _IMPORT_SETTLE_UNTIL.pop(str(root), None)
    return False


def _fetch_import_pending_count(
    db: MauticDB,
    config: AgentConfig,
    root: str,
    sql_ctx: dict[str, str],
) -> int:
    configured_count = db.fetch_count(config.sql_import_pending_count, context=sql_ctx)
    status_count = db.fetch_count(_SQL_IMPORT_PENDING_STATUS_COUNT, context=sql_ctx)
    if configured_count > 0 and status_count <= 0:
        now_ts = time.time()
        last_warn = float(_IMPORT_PENDING_OVERRIDE_WARN_TS.get(str(root), 0.0) or 0.0)
        if now_ts - last_warn >= 300.0:
            _IMPORT_PENDING_OVERRIDE_WARN_TS[str(root)] = now_ts
            logging.warning(
                "[%s] import pending override returned %s but status query returned 0; treating import queue as empty",
                root,
                configured_count,
            )
        return 0
    return max(0, int(status_count or configured_count or 0))


def _running_count(running: dict[str, RunningTask], root: str, task_type_prefix: str) -> int:
    return sum(1 for t in running.values() if t.root == root and t.task_type.startswith(task_type_prefix))


def _running_campaign_total(running: dict[str, RunningTask], root: str) -> int:
    return sum(
        1
        for t in running.values()
        if t.root == root and t.task_type in {"campaign_update", "campaign_trigger", "campaign_rebuild"}
    )


def _running_count_for_entities(
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
    entity_ids: set[int] | None,
) -> int:
    if entity_ids is None:
        return _running_count(running, root, task_type)
    return sum(
        1
        for t in running.values()
        if t.root == root and t.task_type == task_type and t.entity_id is not None and t.entity_id in entity_ids
    )


def _effective_segment_slot_limit(config: AgentConfig, throttled_active: bool) -> int:
    if config.segment_mode == "classic_loop":
        return 1
    if throttled_active:
        if config.segment_throttle_whitelist_only:
            return max(0, int(config.segment_throttle_whitelist_parallel or 0))
        return max(0, int(config.segment_priority_parallel_throttled or 0)) + max(
            0,
            int(config.segment_regular_parallel_throttled or 0),
        )
    return max(0, int(config.segment_priority_parallel_idle or 0)) + max(
        0,
        int(config.segment_regular_parallel_idle or 0),
    )


def _segment_shared_slots_used(running: dict[str, RunningTask], root: str) -> int:
    return _running_count(running, root, "segment") + _running_count(running, root, "import")


def _segment_shared_slots_available(
    running: dict[str, RunningTask],
    root: str,
    segment_slot_limit: int,
) -> int:
    return max(0, max(0, int(segment_slot_limit or 0)) - _segment_shared_slots_used(running, root))


def _segment_task_limit_after_import(
    running: dict[str, RunningTask],
    root: str,
    segment_slot_limit: int,
) -> int:
    return max(0, max(0, int(segment_slot_limit or 0)) - _running_count(running, root, "import"))


def _submit_import_if_segment_slot(
    *,
    config: AgentConfig,
    store: "TaskStore",
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    root: str,
    cluster_import_allowed: bool,
    import_pending_count: int,
    segment_slot_limit: int,
    now_ts: float,
) -> bool:
    if (
        not cluster_import_allowed
        or not config.enable_import_polling
        or int(import_pending_count or 0) <= 0
        or _import_in_settle(root, now_ts)
        or _running_count(running, root, "import") > 0
    ):
        return False
    if _segment_shared_slots_available(running, root, segment_slot_limit) <= 0:
        return False
    args = render_mautic_command(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=root,
        template=config.cmd_import_template,
        import_limit=config.import_limit,
    )
    launched = _submit_if_slot(
        config=config,
        store=store,
        running=running,
        root=root,
        task_type="import",
        entity_id=None,
        args=args,
        timeout_sec=config.command_timeout_sec,
        max_parallel_for_type=1,
        popens=popens,
    )
    if launched:
        logging.info(
            "[%s] import claimed shared segment slot pending=%s slot_limit=%s",
            root,
            int(import_pending_count or 0),
            max(0, int(segment_slot_limit or 0)),
        )
    return launched


def _is_running(running: dict[str, RunningTask], root: str, task_type: str, entity_id: int | None) -> bool:
    for t in running.values():
        if t.root == root and t.task_type == task_type and t.entity_id == entity_id:
            return True
    return False


def _ring_has_launchable(
    ring: deque[int],
    *,
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
) -> bool:
    for eid in ring:
        if not _is_running(running, root, task_type, eid):
            return True
    return False


def _running_entities_stuck_for(
    running: dict[str, RunningTask],
    *,
    root: str,
    task_type: str,
    entity_ids: set[int],
    now_ts: float,
    stuck_sec: int,
) -> int:
    if stuck_sec <= 0:
        return 0
    count = 0
    for t in running.values():
        if t.root != root or t.task_type != task_type or t.entity_id is None:
            continue
        if t.entity_id not in entity_ids:
            continue
        if now_ts - float(t.started_at) >= float(stuck_sec):
            count += 1
    return count


def _submit_if_slot(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
    entity_id: int | None,
    args: list[str],
    timeout_sec: int,
    max_parallel_for_type: int,
    popens: dict[str, subprocess.Popen[bytes]] | None = None,
    ignore_limit: bool = False,
    manual_request_id: int | None = None,
) -> bool:
    key = _task_key(root, task_type, entity_id)
    if key in running:
        return False
    if store.has_running_task_key(key):
        return False
    if manual_request_id is None and not _launch_allowed(config, root, task_type, entity_id):
        return False
    if manual_request_id is None:
        min_repeat = _task_repeat_interval_sec(config, task_type)
        if min_repeat > 0:
            prev = store.last_task_started_at(key)
            if prev > 0 and time.time() - float(prev) < float(min_repeat):
                return False
    if not ignore_limit and _running_count(running, root, task_type) >= max_parallel_for_type:
        return False

    proc = _spawn_command(root=root, args=args)
    task = RunningTask(
        row_id=0,
        root=root,
        task_key=key,
        task_type=task_type,
        entity_id=entity_id,
        command_str=_CMD_SEP.join(args),
        timeout_sec=timeout_sec,
        attempts=1,
        started_at=time.time(),
        pid=proc.pid,
        manual_request_id=manual_request_id,
    )
    task.row_id = store.add_running(task)
    running[key] = task
    if popens is not None:
        popens[key] = proc
    _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, task_type, entity_id)] = task.started_at
    logging.info("[%s] spawned %s pid=%s entity=%s", root, task_type, task.pid, entity_id)
    return True


def _respawn_task(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    task: RunningTask,
) -> bool:
    # task_retry_max semantics:
    # - <= 0 : unlimited retries
    # - 1    : no retry (initial run only)
    # - > 1  : bounded retries up to configured attempt cap
    retry_max = int(config.task_retry_max)
    if task.manual_request_id is None and task.task_type == "segment" and task.entity_id is not None:
        if retry_max <= 0:
            retry_max = _SEGMENT_AUTOMATIC_RETRY_CAP
        problem_counts = store.recent_task_problem_counts(
            root=task.root,
            task_type="segment",
            since_sec=_SEGMENT_FAILURE_COOLDOWN_SEC,
        )
        count = int(problem_counts.get(int(task.entity_id), 0) or 0)
        if count >= _SEGMENT_FAILURE_COOLDOWN_THRESHOLD:
            _log_segment_failure_cooldown(task.root, int(task.entity_id), count)
            return False
    if retry_max > 0 and task.attempts >= retry_max:
        return False
    try:
        args = task.command_str.split(_CMD_SEP)
        proc = _spawn_command(root=task.root, args=args)
    except Exception as e:  # pragma: no cover - defensive
        logging.warning("[%s] respawn failed %s: %s", task.root, task.task_type, e)
        return False

    next_task = RunningTask(
        row_id=0,
        root=task.root,
        task_key=task.task_key,
        task_type=task.task_type,
        entity_id=task.entity_id,
        command_str=task.command_str,
        timeout_sec=task.timeout_sec,
        attempts=task.attempts + 1,
        started_at=time.time(),
        pid=proc.pid,
        manual_request_id=task.manual_request_id,
    )
    next_task.row_id = store.add_running(next_task)
    running[next_task.task_key] = next_task
    popens[next_task.task_key] = proc
    logging.warning(
        "[%s] retry %s entity=%s attempt=%s pid=%s",
        task.root,
        task.task_type,
        task.entity_id,
        next_task.attempts,
        next_task.pid,
    )
    return True


def _monitor_running(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
) -> None:
    now = time.time()
    for key, task in list(running.items()):
        if bool(getattr(task, "external", False)):
            alive = _is_pid_alive(task.pid)
            if alive and _pid_matches_task_command(task.pid, task.command_str):
                continue
            running.pop(key, None)
            popens.pop(key, None)
            if task.task_type == "segment":
                _mark_segment_finished(task.root, task.entity_id, now_ts=now)
            logging.info(
                "[%s] external %s entity=%s pid=%s released",
                task.root,
                task.task_type,
                task.entity_id,
                task.pid,
            )
            continue

        proc = popens.get(key)
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                state = "done" if rc == 0 else "failed"
                note = None if rc == 0 else "non_zero_exit"
                store.finish(task.row_id, state=state, rc=rc, note=note)
                if rc == 0 and task.task_type == "segment":
                    _mark_segment_finished(task.root, task.entity_id, now_ts=now)
                if rc == 0 and task.task_type == "import":
                    _mark_import_settle(config, task.root, now, elapsed_sec=now - float(task.started_at))
                running.pop(key, None)
                popens.pop(key, None)
                respawned = False
                if rc != 0:
                    logging.warning("[%s] %s entity=%s failed rc=%s", task.root, task.task_type, task.entity_id, rc)
                    retry_enabled = (config.task_retry_max <= 0) or (config.task_retry_max > 1)
                    if retry_enabled:
                        if config.task_retry_delay_sec > 0:
                            time.sleep(config.task_retry_delay_sec)
                        respawned = _respawn_task(config=config, store=store, running=running, popens=popens, task=task)
                if task.manual_request_id is not None and not respawned:
                    store.finish_manual_request(task.manual_request_id, state, note)
                continue

        alive = _is_pid_alive(task.pid)
        if alive and proc is None and not _pid_matches_task_command(task.pid, task.command_str):
            store.finish(task.row_id, state="lost", rc=None, note="pid_cmd_mismatch")
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", "pid_cmd_mismatch")
            logging.warning(
                "[%s] %s entity=%s pid=%s command-mismatch detected, marking lost",
                task.root,
                task.task_type,
                task.entity_id,
                task.pid,
            )
            continue

        elapsed = now - task.started_at
        timeout_threshold: int | None = None
        if task.timeout_sec > 0 and config.worker_watchdog_sec > 0:
            timeout_threshold = min(task.timeout_sec, config.worker_watchdog_sec)
        elif task.timeout_sec > 0:
            timeout_threshold = task.timeout_sec
        elif config.worker_watchdog_sec > 0:
            timeout_threshold = config.worker_watchdog_sec

        if alive and (timeout_threshold is None or elapsed <= timeout_threshold):
            continue

        if alive and timeout_threshold is not None and elapsed > timeout_threshold:
            _kill_pid(task.pid, config.segment_kill_grace_sec)
            store.finish(task.row_id, state="timeout", rc=None, note="killed_by_timeout")
            running.pop(key, None)
            popens.pop(key, None)
            logging.warning("[%s] %s entity=%s timeout=%ss", task.root, task.task_type, task.entity_id, int(timeout_threshold))
            allow_restart = (config.worker_stuck_policy or "skip").lower() == "restart"
            restart_cap = max(0, config.worker_stuck_restart_limit)
            respawned = False
            if allow_restart and task.attempts <= restart_cap:
                if config.task_retry_delay_sec > 0:
                    time.sleep(config.task_retry_delay_sec)
                respawned = _respawn_task(config=config, store=store, running=running, popens=popens, task=task)
            if task.manual_request_id is not None and not respawned:
                store.finish_manual_request(task.manual_request_id, "timeout", "killed_by_timeout")
            continue

        if not alive:
            store.finish(task.row_id, state="lost", rc=None, note="pid_not_alive")
            if task.task_type == "import":
                _mark_import_settle(config, task.root, now, elapsed_sec=now - float(task.started_at))
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", "pid_not_alive")
            logging.warning("[%s] %s entity=%s pid_lost=%s", task.root, task.task_type, task.entity_id, task.pid)


def _running_task_from_row(row: dict[str, object]) -> RunningTask:
    return RunningTask(
        row_id=int(row["id"]),
        root=str(row["root"]),
        task_key=str(row["task_key"]),
        task_type=str(row["task_type"]),
        entity_id=int(row["entity_id"]) if row["entity_id"] is not None else None,
        command_str=str(row["command_str"]),
        timeout_sec=int(row["timeout_sec"]),
        attempts=int(row["attempts"]),
        started_at=float(row["started_at"]),
        pid=int(row["pid"]),
        manual_request_id=int(row["manual_request_id"]) if row["manual_request_id"] is not None else None,
    )


def _reconcile_running_state(
    *,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
) -> dict[str, int]:
    stats = {
        "tracked_total": 0,
        "kept": 0,
        "adopted": 0,
        "lost_pid": 0,
        "lost_cmd": 0,
        "duplicate": 0,
        "shadow_replaced": 0,
    }
    try:
        shadow = store.sync_sqlite_running_shadow()
        stats["shadow_replaced"] = int(shadow.get("replaced", 0) or 0)
    except Exception:
        pass
    rows = store.running_rows()
    stats["tracked_total"] = len(rows)
    if not rows:
        return stats

    valid_by_key: dict[str, RunningTask] = {}
    for row in rows:
        task = _running_task_from_row(row)
        if not _is_pid_alive(task.pid):
            store.finish(task.row_id, state="lost", rc=None, note="pid_not_alive")
            stats["lost_pid"] += 1
            continue
        if not _pid_matches_task_command(task.pid, task.command_str):
            store.finish(task.row_id, state="lost", rc=None, note="pid_cmd_mismatch")
            stats["lost_cmd"] += 1
            continue

        prev = valid_by_key.get(task.task_key)
        if prev is None:
            valid_by_key[task.task_key] = task
            continue

        keep = prev
        current = running.get(task.task_key)
        if current is not None:
            if task.pid == current.pid:
                keep = task
            elif prev.pid == current.pid:
                keep = prev
            elif (task.started_at, task.row_id) >= (prev.started_at, prev.row_id):
                keep = task
        elif (task.started_at, task.row_id) >= (prev.started_at, prev.row_id):
            keep = task

        loser = prev if keep is task else task
        store.finish(loser.row_id, state="lost", rc=None, note="duplicate_task_key")
        stats["duplicate"] += 1
        valid_by_key[task.task_key] = keep

    for key, task in valid_by_key.items():
        cur = running.get(key)
        if cur is None:
            running[key] = task
            proc = popens.get(key)
            if proc is not None and proc.pid != task.pid:
                popens.pop(key, None)
            stats["adopted"] += 1
            continue
        if (
            cur.row_id != task.row_id
            or cur.pid != task.pid
            or cur.attempts != task.attempts
            or abs(cur.started_at - task.started_at) > 0.001
        ):
            running[key] = task
            proc = popens.get(key)
            if proc is not None and proc.pid != task.pid:
                popens.pop(key, None)
            stats["adopted"] += 1
        else:
            stats["kept"] += 1
    return stats


def _mark_external_entities_executed(
    *,
    running: dict[str, RunningTask],
    root: str,
    seg_sql_done: set[int],
    seg_sql_ring: deque[int],
    seg_prio_ring: deque[int],
    seg_reg_ring: deque[int],
    trg_prio_ring: deque[int],
    trg_reg_ring: deque[int],
    reb_prio_ring: deque[int],
    reb_reg_ring: deque[int],
) -> None:
    for task in running.values():
        if not bool(getattr(task, "external", False)):
            continue
        if task.root != root or task.entity_id is None:
            continue
        entity_id = int(task.entity_id)
        if task.task_type == "segment":
            seg_sql_done.add(entity_id)
            _mark_ring_entity_executed(seg_sql_ring, entity_id)
            _mark_ring_entity_executed(seg_prio_ring, entity_id)
            _mark_ring_entity_executed(seg_reg_ring, entity_id)
        elif task.task_type == "campaign_trigger":
            _mark_ring_entity_executed(trg_prio_ring, entity_id)
            _mark_ring_entity_executed(trg_reg_ring, entity_id)
        elif task.task_type == "campaign_rebuild":
            _mark_ring_entity_executed(reb_prio_ring, entity_id)
            _mark_ring_entity_executed(reb_reg_ring, entity_id)


def _load_orphans_from_store(store: TaskStore) -> dict[str, RunningTask]:
    out: dict[str, RunningTask] = {}
    for row in store.running_rows():
        t = _running_task_from_row(row)
        out[t.task_key] = t
    return out


def _dispatch_manual_requests_for_root(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    root: str,
    seg_sql_ring: deque[int],
    seg_prio_ring: deque[int],
    seg_reg_ring: deque[int],
    trg_prio_ring: deque[int],
    trg_reg_ring: deque[int],
    reb_prio_ring: deque[int],
    reb_reg_ring: deque[int],
) -> None:
    pending = store.pending_manual_requests(root, limit=64)
    if not pending:
        return
    for row in pending:
        req_id = int(row["id"])
        task_type = str(row["task_type"] or "").strip()
        entity_id = int(row["entity_id"]) if row["entity_id"] is not None else None
        raw_cmd = str(row["command_str"] or "").strip()
        timeout_sec = int(row["timeout_sec"] or config.command_timeout_sec)
        if not task_type or not raw_cmd:
            store.finish_manual_request(req_id, "failed", "invalid_request_payload")
            continue
        args = raw_cmd.split(_CMD_SEP)
        if not args:
            store.finish_manual_request(req_id, "failed", "empty_command")
            continue
        if task_type == "segment" and entity_id is not None:
            seg_sql_state = _segment_sql_state_load(store, root, entity_id)
            if str(seg_sql_state.get("status") or "").strip().lower() == "running":
                store.finish_manual_request(req_id, "skipped", "managed_by_segment_sql_running")
                continue

        launched = _submit_if_slot(
            config=config,
            store=store,
            running=running,
            root=root,
            task_type=task_type,
            entity_id=entity_id,
            args=args,
            timeout_sec=timeout_sec,
            max_parallel_for_type=1,
            popens=popens,
            ignore_limit=True,
            manual_request_id=req_id,
        )
        if not launched:
            store.finish_manual_request(req_id, "skipped", "already_running")
            continue

        task_key = _task_key(root, task_type, entity_id)
        store.mark_manual_request_launched(req_id, task_key)

        if task_type == "segment":
            _mark_ring_entity_executed(seg_sql_ring, entity_id)
            _mark_ring_entity_executed(seg_prio_ring, entity_id)
            _mark_ring_entity_executed(seg_reg_ring, entity_id)
        elif task_type == "campaign_trigger":
            _mark_ring_entity_executed(trg_prio_ring, entity_id)
            _mark_ring_entity_executed(trg_reg_ring, entity_id)
        elif task_type == "campaign_rebuild":
            _mark_ring_entity_executed(reb_prio_ring, entity_id)
            _mark_ring_entity_executed(reb_reg_ring, entity_id)

        logging.info(
            "[%s] manual request launched id=%s task=%s entity=%s",
            root,
            req_id,
            task_type,
            entity_id,
        )


def _zabbix_agent2_present() -> bool:
    return (
        Path("/etc/zabbix/zabbix_agent2.conf").exists()
        or Path("/etc/zabbix/zabbix_agent2.d").exists()
        or Path("/usr/sbin/zabbix_agent2").exists()
        or Path("/usr/bin/zabbix_agent2").exists()
    )


def _ensure_zabbix_mautic_version_cache_guard(config: AgentConfig, installs: list[object]) -> None:
    if not _zabbix_agent2_present():
        return
    install_res = install_zabbix_mautic_version_userparameter(restart_service=False)
    actions_raw = install_res.get("actions") if isinstance(install_res, dict) else []
    actions = [str(x) for x in actions_raw] if isinstance(actions_raw, list) else []
    changed = any(action.startswith("userparameter:") for action in actions)
    refresh_res = refresh_mautic_version_cache(installs, config.php_bin, run_as_user=config.mautic_run_as_user)
    if changed:
        try:
            proc = subprocess.run(
                ["systemctl", "restart", "zabbix-agent2"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                logging.info(
                    "zabbix mautic.version cache guard repaired UserParameter; caches_updated=%s",
                    int(refresh_res.get("updated", 0) or 0) if isinstance(refresh_res, dict) else 0,
                )
            else:
                msg = (proc.stderr or proc.stdout or "restart failed").strip()
                logging.warning("zabbix mautic.version cache guard restart failed: %s", msg[:500])
        except Exception as e:
            logging.warning("zabbix mautic.version cache guard restart failed: %s", e)
    else:
        updated = int(refresh_res.get("updated", 0) or 0) if isinstance(refresh_res, dict) else 0
        if updated:
            logging.debug("zabbix mautic.version cache guard refreshed caches=%s", updated)


def run_loop(config: AgentConfig, single_cycle: bool = False) -> None:
    logging.info("MCD loop started")
    base_config = config

    campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
    if config.disable_whitelist:
        campaign_whitelist = set()

    store = TaskStore(config.state_db_path, config)
    store.put_runtime_sync("local_runtime", local_runtime_overrides(config))
    inventory = InstanceInventory(config.state_db_path)
    seeded = ensure_seeded(inventory, config)
    logging.info("Instance inventory loaded: %s", seeded)
    try:
        rows, source = fetch_custom_manifest(config, use_cache_on_error=True)
        logging.info("Custom scripts manifest loaded: source=%s count=%s", source, len(rows))
    except Exception as e:
        logging.warning("Custom scripts manifest prefetch failed: %s", e)

    running = _load_orphans_from_store(store)
    popens: dict[str, subprocess.Popen[bytes]] = {}

    identity = resolve_agent_identity(config)
    if bool(identity.get("clone_detected", False)):
        try:
            count = inventory.rescan(config)
            installs = inventory.list_instances()
            logging.info(
                "template clone detected: source=%s local=%s; inventory rescan applied (%s instances)",
                str(identity.get("source_host_name") or "-"),
                str(identity.get("local_hostname") or "-"),
                count,
            )
        except Exception as e:
            logging.warning("template clone inventory rescan failed; fallback to cached inventory: %s", e)
            installs = inventory.list_instances()
    else:
        installs = inventory.list_instances()
    _sync_segment_whitelist_file(config, installs)
    segment_sql_rings: dict[str, deque[int]] = {}
    segment_sql_rules_by_root: dict[str, dict[int, SQLSegmentRule]] = {}
    segment_sql_active_sets: dict[str, set[int]] = {}
    segment_sql_done_sets: dict[str, set[int]] = {}
    segment_sql_auto_signatures: dict[str, tuple[int, ...]] = {}
    segment_dependencies_by_root: dict[str, dict[int, set[int]]] = {}
    segment_parents_by_root: dict[str, dict[int, set[int]]] = {}
    segment_invalid_filter_sets: dict[str, set[int]] = {}
    segment_failure_blocked_sets: dict[str, set[int]] = {}
    segment_prio_rings: dict[str, deque[int]] = {}
    segment_reg_rings: dict[str, deque[int]] = {}
    campaign_trigger_prio_rings: dict[str, deque[int]] = {}
    campaign_trigger_reg_rings: dict[str, deque[int]] = {}
    campaign_rebuild_prio_rings: dict[str, deque[int]] = {}
    campaign_rebuild_reg_rings: dict[str, deque[int]] = {}
    segment_prio_sets: dict[str, set[int]] = {}
    segment_reg_sets: dict[str, set[int]] = {}
    campaign_trigger_prio_sets: dict[str, set[int]] = {}
    campaign_trigger_reg_sets: dict[str, set[int]] = {}
    campaign_rebuild_prio_sets: dict[str, set[int]] = {}
    campaign_rebuild_reg_sets: dict[str, set[int]] = {}
    campaign_round_robin: dict[str, int] = {}
    segment_resume_rings: dict[str, deque[int]] = {}
    queue_samples: dict[str, deque[tuple[float, int]]] = {}
    throttled: dict[str, bool] = {}
    last_import_poll_ts: dict[str, float] = {}
    import_pending_cache: dict[str, int] = {}
    segment_last_full_scan_ts: dict[str, float] = {}
    segment_force_full_scan_until: dict[str, float] = {}
    last_cleanup_ts: dict[str, float] = {}
    last_mautic_lock_cleanup_ts: dict[str, float] = {}
    last_page_hits_orphan_cleanup_ts: dict[str, float] = {}
    page_hits_orphan_cleanup_active: dict[str, bool] = {}
    page_hits_orphan_cleanup_window_counts: dict[str, int] = {}
    page_hits_orphan_cleanup_window_keys: dict[str, str] = {}
    page_hits_orphan_cleanup_done_window_keys: dict[str, str] = {}
    last_housekeeping_plugin_ts: dict[str, float] = {}
    last_empty_leads_cleanup_ts: dict[str, float] = {}
    last_empty_leads_cleanup_idle_ts: dict[str, float] = {}
    last_empty_leads_cleanup_skip_ts: dict[str, float] = {}
    last_campaign_trigger_audit_ts: dict[str, float] = {}
    last_empty_leads_cleanup_cron_minute: dict[str, str] = {}
    empty_leads_cleanup_window_counts: dict[str, int] = {}
    empty_leads_cleanup_window_keys: dict[str, str] = {}
    empty_leads_cleanup_done_window_keys: dict[str, str] = {}
    maintenance_cleanup_rr_index: dict[str, int] = {}
    last_cache_clear_ts: dict[str, float] = {}
    last_cache_warm_ts: dict[str, float] = {}
    last_monitored_email_parser_ts: dict[str, float] = {}
    last_fs_permissions_guard_ts: dict[str, float] = {}
    last_db_watchdog_ts: dict[str, float] = {}
    last_tasks_compact_ts = 0.0
    last_outbound_events_prune_ts = 0.0
    last_custom_cache_cleanup_ts = 0.0
    last_launch_guard_prune_ts = 0.0
    next_scheduler_reconcile_at = 0.0
    db_dispatch_pause_until: dict[str, float] = {}
    db_dispatch_pause_reasons: dict[str, str] = {}
    jobs_last_run: dict[tuple[str, str], float] = {}
    last_backup_schedule_ts = 0.0
    last_backup_schedule_day = ""
    backup_thread: threading.Thread | None = None
    cluster_backup_thread: threading.Thread | None = None
    cluster_files_thread: threading.Thread | None = None
    next_cluster_files_produce_at = 0.0
    last_cluster_full_day = ""
    last_cluster_offsite_day = ""
    next_cluster_full_retry_at = 0.0
    next_cluster_offsite_retry_at = 0.0
    last_cluster_backup_suppressed_reason = ""
    backup_dispatch_pause_active = False
    scheduler_dispatch_pause_active = False
    next_plan_refresh_at = 0.0
    next_passive_notice_at = 0.0
    next_update_check_at = 0.0
    update_deferred_by_backup = False
    last_campaign_console_activity_ts = 0.0
    next_service_profile_apply_at = 0.0
    next_backup_profile_sync_at = 0.0
    next_backup_storage_probe_at = 0.0
    next_zabbix_version_cache_guard_at = 0.0
    next_runtime_overrides_poll_at = 0.0
    next_viber_cron_reconcile_at = 0.0
    next_email_fetch_cron_reconcile_at = 0.0
    next_empty_leads_cleanup_cron_reconcile_at = 0.0
    next_cluster_assets_guard_at = 0.0
    next_inventory_auto_rescan_at = time.time() + min(300, max(60, int(getattr(config, "poll_interval_sec", 60) or 60)))
    last_inventory_signature = ""
    runtime_overrides_sync_requested = False
    # Always perform an initial runtime-overrides sync after daemon start/restart.
    # This is required even when periodic polling is disabled to avoid running
    # with stale local-only runtime after self-update/service restart.
    startup_runtime_sync_pending = bool(config.mcc_url and config.mcc_token)
    next_profile_guard_at = 0.0
    last_runtime_overrides_fp = ""
    last_runtime_overrides_error = ""
    last_local_runtime_fp = overrides_fingerprint(local_runtime_overrides(config))
    pusher = MCCStatePusher(config)

    while True:
        _monitor_running(config=config, store=store, running=running, popens=popens)
        now = time.time()
        if now >= next_scheduler_reconcile_at:
            rec = _reconcile_running_state(store=store, running=running, popens=popens)
            if any(int(rec.get(k, 0) or 0) > 0 for k in ("adopted", "lost_pid", "lost_cmd", "duplicate", "shadow_replaced")):
                logging.warning(
                    "scheduler reconcile: tracked=%s kept=%s adopted=%s lost_pid=%s lost_cmd=%s duplicate=%s shadow_replaced=%s",
                    int(rec.get("tracked_total", 0) or 0),
                    int(rec.get("kept", 0) or 0),
                    int(rec.get("adopted", 0) or 0),
                    int(rec.get("lost_pid", 0) or 0),
                    int(rec.get("lost_cmd", 0) or 0),
                    int(rec.get("duplicate", 0) or 0),
                    int(rec.get("shadow_replaced", 0) or 0),
                )
            next_scheduler_reconcile_at = now + max(15, int(config.scheduler_reconcile_interval_sec or 60))

        if bool(getattr(config, "inventory_auto_rescan_enabled", True)) and now >= next_inventory_auto_rescan_at:
            interval = max(300, int(getattr(config, "inventory_auto_rescan_interval_sec", 3600) or 3600))
            try:
                before = inventory.list_instances()
                before_sig = "|".join(
                    sorted(f"{getattr(inst, 'instance_uid', '')}:{getattr(inst, 'root', '')}" for inst in before)
                )
                count = inventory.rescan(config, preserve_manual=True)
                installs = inventory.list_instances()
                after_sig = "|".join(
                    sorted(f"{getattr(inst, 'instance_uid', '')}:{getattr(inst, 'root', '')}" for inst in installs)
                )
                if after_sig != before_sig and after_sig != last_inventory_signature:
                    logging.info(
                        "inventory auto-rescan updated local instances: before=%s after=%s",
                        len(before),
                        count,
                    )
                    # Force next MCC state push to carry the refreshed inventory immediately.
                    pusher.last_hash = ""
                    pusher.last_push_ts = 0.0
                last_inventory_signature = after_sig
            except Exception as e:
                logging.warning("inventory auto-rescan failed: %s", e)
            next_inventory_auto_rescan_at = now + interval

        ext = _sync_external_running_tasks(installs=installs, running=running, popens=popens)
        if any(int(ext.get(k, 0) or 0) > 0 for k in ("adopted", "updated", "released")):
            logging.info(
                "external task sync: observed=%s adopted=%s updated=%s released=%s",
                int(ext.get("observed", 0) or 0),
                int(ext.get("adopted", 0) or 0),
                int(ext.get("updated", 0) or 0),
                int(ext.get("released", 0) or 0),
            )
        if any(t.task_type in {"campaign_trigger", "campaign_rebuild", "campaign_update"} for t in running.values()):
            last_campaign_console_activity_ts = now
        if now - last_launch_guard_prune_ts >= 300:
            _prune_entity_launch_guard(max_age_sec=3600, now_ts=now)
            last_launch_guard_prune_ts = now

        local_runtime = local_runtime_overrides(config)
        local_fp = overrides_fingerprint(local_runtime)
        if local_fp != last_local_runtime_fp:
            store.put_runtime_sync("local_runtime", local_runtime)
            last_local_runtime_fp = local_fp
            if config.mcc_url and config.mcc_token:
                pushed = push_runtime_overrides(config, local_runtime, merge=False)
                p_status = str(pushed.get("status", "")).strip().lower()
                if p_status == "ok":
                    logging.info(
                        "runtime-overrides local change pushed to MCC (keys=%s)",
                        ",".join(sorted(local_runtime.keys())) if local_runtime else "-",
                    )
                elif p_status != "disabled":
                    logging.warning("runtime-overrides local push failed: %s", pushed.get("reason", "unknown"))

        if consume_poll_trigger(config):
            runtime_overrides_sync_requested = True
            next_runtime_overrides_poll_at = 0.0
            logging.info("runtime-overrides poll trigger consumed: immediate MCC sync requested")

        runtime_poll_enabled = bool(getattr(config, "mcc_runtime_overrides_poll_enabled", False))
        should_runtime_poll = runtime_poll_enabled and now >= next_runtime_overrides_poll_at
        if config.mcc_url and config.mcc_token and (
            startup_runtime_sync_pending or runtime_overrides_sync_requested or should_runtime_poll
        ):
            ro = fetch_runtime_overrides(config)
            status = str(ro.get("status", "")).strip().lower()
            poll_interval = max(15, min(300, int(config.mcc_push_interval_sec or config.poll_interval_sec or 60)))
            if status == "ok":
                overrides_raw = ro.get("runtime_overrides")
                overrides = overrides_raw if isinstance(overrides_raw, dict) else {}
                store.put_runtime_sync("mcc_runtime", overrides)
                fp = overrides_fingerprint(overrides)
                if fp != last_runtime_overrides_fp:
                    # Apply new MCC runtime over the currently effective
                    # daemon config. Using the startup baseline here makes
                    # operator saves look persisted on disk while scheduler
                    # decisions can continue from stale in-memory values until
                    # a restart.
                    applied = apply_remote_overrides(config, overrides)
                    next_cfg = applied["config"]
                    applied_keys = list(applied.get("applied_keys", []))
                    unsupported_keys = list(applied.get("unsupported_keys", []))
                    blocked_keys = list(applied.get("blocked_keys", []))
                    if unsupported_keys:
                        logging.warning(
                            "runtime-overrides ignored unsupported keys: %s",
                            ",".join(str(x) for x in unsupported_keys),
                        )
                    if blocked_keys:
                        logging.warning(
                            "runtime-overrides ignored blocked keys (restart/static-only): %s",
                            ",".join(str(x) for x in blocked_keys),
                        )
                    if next_cfg != config:
                        old_profile = (config.profile_name or "").strip().lower()
                        config = next_cfg
                        base_config = config
                        pusher.cfg = config
                        next_plan_refresh_at = 0.0
                        next_update_check_at = 0.0
                        next_service_profile_apply_at = 0.0
                        campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
                        if config.disable_whitelist:
                            campaign_whitelist = set()
                        new_profile = (config.profile_name or "").strip().lower()
                        logging.info(
                            "runtime-overrides applied from MCC: keys=%s profile=%s->%s",
                            ",".join(applied_keys) if applied_keys else "-",
                            old_profile or "-",
                            new_profile or "-",
                        )
                    else:
                        logging.info(
                            "runtime-overrides synced from MCC: no effective change (keys=%s)",
                            ",".join(applied_keys) if applied_keys else "-",
                        )
                    if set(applied_keys) & _SERVICE_CLEANUP_RUNTIME_KEYS:
                        # A Save in MCC must be visible to the next scheduler
                        # tick. Drop per-process cleanup cursors so newly
                        # enabled/changed service cleanups do not wait for an
                        # old interval/window key or a previous idle marker.
                        last_page_hits_orphan_cleanup_ts.clear()
                        page_hits_orphan_cleanup_window_counts.clear()
                        page_hits_orphan_cleanup_window_keys.clear()
                        page_hits_orphan_cleanup_done_window_keys.clear()
                        last_empty_leads_cleanup_ts.clear()
                        last_empty_leads_cleanup_idle_ts.clear()
                        last_empty_leads_cleanup_skip_ts.clear()
                        empty_leads_cleanup_window_counts.clear()
                        empty_leads_cleanup_window_keys.clear()
                        empty_leads_cleanup_done_window_keys.clear()
                        last_housekeeping_plugin_ts.clear()
                        last_monitored_email_parser_ts.clear()
                        maintenance_cleanup_rr_index.clear()
                        logging.info("runtime-overrides cleanup schedules refreshed for immediate scheduler apply")
                    _persist_stable_backup_runtime_to_config(
                        next_cfg if isinstance(next_cfg, AgentConfig) else config,
                        applied_keys,
                        installs,
                    )
                    base_config = config
                    last_runtime_overrides_fp = fp
                    store.put_runtime_sync(
                        "active_runtime",
                        {
                            "profile": (config.profile_name or "").strip().lower(),
                            "applied_keys": applied_keys,
                            "blocked_keys": blocked_keys,
                            "unsupported_keys": unsupported_keys,
                        },
                    )
                startup_runtime_sync_pending = False
                last_runtime_overrides_error = ""
                next_runtime_overrides_poll_at = now + poll_interval
            elif status == "disabled":
                startup_runtime_sync_pending = False
                last_runtime_overrides_error = ""
                next_runtime_overrides_poll_at = now + poll_interval
            else:
                reason = str(ro.get("reason", "unknown")).strip() or "unknown"
                if reason != last_runtime_overrides_error:
                    logging.warning("runtime-overrides fetch failed: %s", reason)
                last_runtime_overrides_error = reason
                next_runtime_overrides_poll_at = now + max(30, poll_interval)
            runtime_overrides_sync_requested = False

        if now >= next_backup_profile_sync_at:
            try:
                sync_res = backup_profile_sync_from_config(config)
                if bool(sync_res.get("changed")):
                    keys_raw = sync_res.get("keys")
                    keys = keys_raw if isinstance(keys_raw, list) else []
                    logging.info(
                        "backup-profile synced from config to state DB: keys=%s",
                        ",".join(str(x) for x in keys) if keys else "-",
                    )
            except Exception as e:
                logging.warning("backup-profile sync from config failed: %s", e)
            next_backup_profile_sync_at = now + 60.0

        if now >= next_zabbix_version_cache_guard_at:
            try:
                _ensure_zabbix_mautic_version_cache_guard(config, installs)
            except Exception as e:
                logging.warning("zabbix mautic.version cache guard failed: %s", e)
            next_zabbix_version_cache_guard_at = now + _ZABBIX_VERSION_CACHE_GUARD_INTERVAL_SEC

        if config.backup_enabled and now >= next_backup_storage_probe_at:
            probe_allowed, probe_skip_reason = _backup_storage_probe_allowed(config)
            if not probe_allowed:
                logging.info("backup storage probe skipped: %s", probe_skip_reason)
            else:
                probe_backup_locked = False
                try:
                    probe_backup_locked = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(config)
                except Exception as e:
                    logging.warning("backup storage probe lock check failed: %s", e)
                if probe_backup_locked:
                    logging.info("backup storage probe skipped: backup lock active")
                else:
                    try:
                        probe_res = backup_storage_probe(config)
                        probe_status = str(probe_res.get("status", "")).strip().lower()
                        if probe_status == "ok":
                            logging.info("backup storage probe ok")
                        elif probe_status == "failed":
                            logging.warning("backup storage probe failed: %s", probe_res.get("error", "unknown"))
                        elif probe_status == "skipped":
                            logging.info("backup storage probe skipped: %s", probe_res.get("reason", "-"))
                    except Exception as e:
                        logging.warning("backup storage probe failed: %s", e)
            next_backup_storage_probe_at = now + 7200.0

        if config.tasks_compact_enabled:
            quiet_hour = max(0, min(23, int(config.tasks_compact_quiet_hour)))
            quiet_window_min = max(1, min(180, int(config.tasks_compact_quiet_window_min)))
            interval_sec = max(300, int(config.tasks_compact_interval_sec))
            dt = datetime.now()
            in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
            if in_quiet and (last_tasks_compact_ts == 0.0 or now - last_tasks_compact_ts >= interval_sec):
                can_vacuum = bool(config.tasks_compact_vacuum and not running)
                try:
                    deleted, remaining, vacuum_done = store.compact_tasks(
                        now_ts=now,
                        keep_days=config.tasks_history_keep_days,
                        max_rows=config.tasks_history_max_rows,
                        run_vacuum=can_vacuum,
                        archive_enabled=config.tasks_archive_enabled,
                        archive_dir=config.tasks_archive_dir,
                        archive_keep_days=config.tasks_archive_keep_days,
                    )
                    if deleted > 0 or vacuum_done:
                        logging.info(
                            "tasks compaction: deleted=%s remaining_non_running=%s vacuum=%s",
                            deleted,
                            remaining,
                            "on" if vacuum_done else "off",
                        )
                    elif config.tasks_compact_vacuum and running:
                        logging.info("tasks compaction: vacuum postponed (running tasks=%s)", len(running))
                except Exception as e:
                    logging.warning("tasks compaction failed: %s", e)
                last_tasks_compact_ts = now

        # Keep outbound profile-event queue bounded; old delivered events do
        # not have operational value after retention window.
        quiet_hour = max(0, min(23, int(config.tasks_compact_quiet_hour)))
        quiet_window_min = max(1, min(180, int(config.tasks_compact_quiet_window_min)))
        interval_sec = max(300, int(config.tasks_compact_interval_sec))
        dt = datetime.now()
        in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
        if in_quiet and (
            last_outbound_events_prune_ts == 0.0
            or now - last_outbound_events_prune_ts >= interval_sec
        ):
            try:
                removed = prune_sent_profile_events(
                    config,
                    keep_days=max(1, int(config.outbound_events_sent_keep_days)),
                )
                if removed > 0:
                    logging.info("outbound events prune: removed=%s", removed)
            except Exception as e:
                logging.warning("outbound events prune failed: %s", e)
            last_outbound_events_prune_ts = now

        if config.custom_cache_cleanup_enabled:
            quiet_hour = max(0, min(23, int(config.custom_cache_cleanup_quiet_hour)))
            quiet_window_min = max(1, min(180, int(config.custom_cache_cleanup_quiet_window_min)))
            interval_sec = max(300, int(config.custom_cache_cleanup_interval_sec))
            dt = datetime.now()
            in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
            if in_quiet and (
                last_custom_cache_cleanup_ts == 0.0
                or now - last_custom_cache_cleanup_ts >= interval_sec
            ):
                try:
                    stats = cleanup_custom_cache(config, known_keys=cached_custom_manifest_keys(config))
                    if int(stats.get("logs_removed", 0) or 0) > 0 or int(stats.get("downloads_removed", 0) or 0) > 0:
                        logging.info(
                            "custom cache cleanup: logs_removed=%s downloads_removed=%s errors=%s",
                            int(stats.get("logs_removed", 0) or 0),
                            int(stats.get("downloads_removed", 0) or 0),
                            int(stats.get("errors", 0) or 0),
                        )
                except Exception as e:
                    logging.warning("custom cache cleanup failed: %s", e)
                last_custom_cache_cleanup_ts = now

        if bool(getattr(config, "cluster_assets_enabled", False)) and now >= next_cluster_assets_guard_at:
            try:
                guard_res = guard_cluster_assets(
                    config,
                    installs=installs,
                    fix_permissions=bool(getattr(config, "cluster_assets_fix_permissions", True)),
                    reload_on_change=bool(getattr(config, "cluster_assets_reload_on_change", True)),
                )
                pusher.latest_cluster_assets_state = guard_res.get("assets") if isinstance(guard_res.get("assets"), dict) else None
                pusher.latest_cluster_assets_state_ts = now if pusher.latest_cluster_assets_state else 0.0
                changed_roots = guard_res.get("changed") if isinstance(guard_res.get("changed"), list) else []
                if changed_roots or str(guard_res.get("status", "")).lower() not in {"ok", "disabled"}:
                    logging.warning(
                        "cluster-assets guard: status=%s changed=%s",
                        str(guard_res.get("status", "unknown")),
                        ",".join(str(x) for x in changed_roots) if changed_roots else "-",
                    )
            except Exception as e:
                logging.warning("cluster-assets guard failed: %s", e)
            next_cluster_assets_guard_at = now + max(60, int(getattr(config, "cluster_assets_interval_sec", 600) or 600))

        if now >= next_update_check_at:
            update_backup_locked = False
            if config.backup_enabled:
                try:
                    update_backup_locked = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(config)
                except Exception as e:
                    logging.warning("auto-update backup lock check failed: %s", e)
                    update_backup_locked = False

            if update_backup_locked:
                if not update_deferred_by_backup:
                    logging.info("auto-update deferred: backup lock active; retry on next cycle")
                update_deferred_by_backup = True
                next_update_check_at = now + max(5, int(config.poll_interval_sec or 10))
            else:
                if update_deferred_by_backup:
                    logging.info("auto-update defer cleared: backup lock released")
                update_deferred_by_backup = False
                try:
                    note, wait_sec = maybe_auto_update(config)
                    if note:
                        logging.info("%s", note)
                    next_update_check_at = now + max(60, int(wait_sec or config.mcd_update_check_interval_sec))
                except Exception as e:
                    logging.warning("auto-update check failed: %s", e)
                    next_update_check_at = now + max(120, int(config.mcd_update_check_interval_sec))

        if (
            config.service_profiles_enabled
            and config.service_profiles_auto_apply
            and now >= next_service_profile_apply_at
        ):
            try:
                components = [c.strip().lower() for c in (config.service_profiles_components or []) if str(c).strip()]
                if not components:
                    components = ["php_fpm", "mysql", "apt"]
                for comp in components:
                    if comp not in {"php_fpm", "php-fpm", "mysql", "apt", "mautic_db_indexes", "mautic-db-indexes", "db_indexes", "db-indexes"}:
                        continue
                    res = service_profiles_apply_once(config, component=comp, dry_run=False)
                    status = str(res.get("status", "")).strip().lower()
                    if status == "ok":
                        applied = res.get("apply")
                        if isinstance(applied, dict):
                            logging.info("service-profile %s apply: %s", comp, applied.get("status", "ok"))
                    elif status == "skipped":
                        logging.info("service-profile %s skipped: %s", comp, res.get("reason", "-"))
                    else:
                        logging.warning("service-profile %s apply failed: %s", comp, res.get("reason", "unknown"))
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))
            except Exception as e:
                logging.warning("service-profile auto-apply failed: %s", e)
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))

        if _cluster_files_producer_allowed(config) and now >= next_cluster_files_produce_at:
            interval = max(300, int(getattr(config, "backup_cluster_files_produce_interval_sec", 3600) or 3600))
            if cluster_files_thread is not None and cluster_files_thread.is_alive():
                logging.info("cluster files producer: previous layer build still active, skip this tick")
            else:
                def _cluster_files_producer_worker() -> None:
                    try:
                        res = cluster_backup_files_produce(config)
                        if res.ok:
                            logging.info("cluster files producer: %s", res.message)
                        else:
                            logging.warning("cluster files producer failed: %s", res.message)
                    except Exception as e:
                        logging.warning("cluster files producer failed: %s", e)

                cluster_files_thread = threading.Thread(
                    target=_cluster_files_producer_worker,
                    name="mcd-cluster-files-producer",
                    daemon=True,
                )
                cluster_files_thread.start()
            next_cluster_files_produce_at = now + interval

        if bool(getattr(config, "backup_cluster_enabled", False)):
            authority = cluster_backup_authority_status(config)
            if not bool(authority.get("allowed")):
                reason = str(authority.get("reason") or "not authority").strip()
                if reason != last_cluster_backup_suppressed_reason:
                    logging.info(
                        "cluster backup suppressed on this node: %s (cluster=%s role=%s authority_role=%s authority_host=%s)",
                        reason,
                        authority.get("cluster_id") or authority.get("cluster_name") or "-",
                        authority.get("node_role") or "-",
                        authority.get("authority_role") or "-",
                        authority.get("authority_host") or "-",
                    )
                    last_cluster_backup_suppressed_reason = reason
            else:
                last_cluster_backup_suppressed_reason = ""
                dt_local = datetime.now()
                run_day = dt_local.strftime("%Y-%m-%d")
                cluster_busy = cluster_backup_thread is not None and cluster_backup_thread.is_alive()
                if not cluster_busy:
                    try:
                        cluster_busy = backup_lock_active(config)
                    except Exception as e:
                        logging.warning("cluster backup lock check failed: %s", e)
                if not cluster_busy:
                    cluster_job = ""
                    if (
                        run_day != last_cluster_full_day
                        and now >= next_cluster_full_retry_at
                        and _local_time_reached(
                            dt_local,
                            int(getattr(config, "backup_cluster_full_hour", 1) or 1),
                            int(getattr(config, "backup_cluster_full_minute", 0) or 0),
                        )
                        and not _cluster_local_full_done_for_date(config, dt_local)
                    ):
                        cluster_job = "local-full"
                    elif (
                        run_day != last_cluster_offsite_day
                        and now >= next_cluster_offsite_retry_at
                        and bool(getattr(config, "backup_cluster_remote_enabled", True))
                        and _cluster_local_full_ready_for_offsite(config, dt_local)
                        and not _cluster_offsite_done_for_date(config, dt_local)
                    ):
                        cluster_job = "offsite"
                    elif (
                        _cluster_local_full_done_for_date(config, dt_local)
                        and _local_hour_in_closed_window(
                            dt_local,
                            int(getattr(config, "backup_cluster_incremental_start_hour", 8) or 8),
                            int(getattr(config, "backup_cluster_incremental_end_hour", 20) or 20),
                        )
                        and not _cluster_incremental_recent(config, now)
                    ):
                        cluster_job = "incremental"

                    if cluster_job:
                        def _cluster_backup_worker(job: str = cluster_job, job_day: str = run_day) -> None:
                            nonlocal last_cluster_full_day
                            nonlocal last_cluster_offsite_day
                            nonlocal next_cluster_full_retry_at
                            nonlocal next_cluster_offsite_retry_at
                            try:
                                if job == "local-full":
                                    res = cluster_backup_local_full(config)
                                    files_ok = True
                                    if res.ok and bool(getattr(config, "backup_cluster_files_snapshot_enabled", True)):
                                        files_res = cluster_backup_files_snapshot(config)
                                        files_ok = bool(files_res.ok)
                                        if not files_res.ok:
                                            logging.warning("cluster files snapshot failed after local full: %s", files_res.message)
                                    if res.ok:
                                        last_cluster_full_day = job_day
                                        next_cluster_full_retry_at = 0.0
                                        if (
                                            files_ok
                                            and bool(getattr(config, "backup_cluster_remote_enabled", True))
                                            and not _cluster_offsite_done_for_date(config, dt_local)
                                        ):
                                            offsite_res = cluster_backup_offsite(config)
                                            if offsite_res.ok:
                                                last_cluster_offsite_day = job_day
                                                next_cluster_offsite_retry_at = 0.0
                                                logging.info("cluster offsite: %s", offsite_res.message)
                                            else:
                                                next_cluster_offsite_retry_at = time.monotonic() + 600
                                                logging.warning("cluster offsite failed: %s", offsite_res.message)
                                    else:
                                        next_cluster_full_retry_at = time.monotonic() + 900
                                    logging.info("cluster local full: %s", res.message)
                                elif job == "offsite":
                                    res = cluster_backup_offsite(config)
                                    if res.ok:
                                        last_cluster_offsite_day = job_day
                                        next_cluster_offsite_retry_at = 0.0
                                        logging.info("cluster offsite: %s", res.message)
                                    else:
                                        next_cluster_offsite_retry_at = time.monotonic() + 600
                                        logging.warning("cluster offsite failed: %s", res.message)
                                else:
                                    res = cluster_backup_local_incremental(config)
                                    if res.ok:
                                        logging.info("cluster incremental: %s", res.message)
                                    else:
                                        logging.warning("cluster incremental failed: %s", res.message)
                            except Exception as e:
                                logging.warning("cluster backup %s failed: %s", job, e)

                        cluster_backup_thread = threading.Thread(
                            target=_cluster_backup_worker,
                            name=f"mcd-cluster-backup-{cluster_job}",
                            daemon=True,
                        )
                        cluster_backup_thread.start()

        if config.backup_enabled and config.backup_schedule_enabled and not bool(
            getattr(config, "backup_cluster_enabled", False)
        ):
            quiet_hour = max(0, min(23, int(config.backup_schedule_quiet_hour)))
            quiet_minute = max(0, min(59, int(getattr(config, "backup_schedule_quiet_minute", 0))))
            quiet_window_min = max(1, min(180, int(config.backup_schedule_quiet_window_min)))
            interval_sec = max(300, int(config.backup_schedule_interval_sec))
            dt_local = datetime.now()
            start_today = dt_local.replace(hour=quiet_hour, minute=quiet_minute, second=0, microsecond=0)
            in_quiet = start_today <= dt_local < (start_today + timedelta(minutes=quiet_window_min))
            if in_quiet and (last_backup_schedule_ts == 0.0 or now - last_backup_schedule_ts >= interval_sec):
                run_day = dt_local.strftime("%Y-%m-%d")
                if run_day == last_backup_schedule_day:
                    # Already executed (or skipped by completed state) for current daily slot.
                    pass
                elif backup_thread is not None and backup_thread.is_alive():
                    logging.info("backup schedule: previous run still active, skip this slot")
                elif _backup_done_for_local_date(config, dt_local):
                    logging.info("backup schedule: successful backup for %s already exists, skip", run_day)
                    last_backup_schedule_day = run_day
                    last_backup_schedule_ts = now
                else:
                    def _backup_worker() -> None:
                        try:
                            res = backup_run(config, None)
                            if res.ok:
                                logging.info("backup schedule: %s", res.message)
                            else:
                                logging.warning("backup schedule failed: %s", res.message)
                        except Exception as e:
                            logging.warning("backup schedule failed: %s", e)
                    backup_thread = threading.Thread(target=_backup_worker, name="mcd-backup", daemon=True)
                    backup_thread.start()
                    last_backup_schedule_ts = now
                    last_backup_schedule_day = run_day

        if pusher.enabled() and should_poll_alert(now, pusher.last_alert_poll_ts, config.mcc_push_alert_poll_interval_sec):
            try:
                signals_payload = collect_signals(window_min=config.mcc_push_alert_window_min, cfg=config)
                pusher.set_signals(signals_payload, now)
            except Exception as e:
                logging.warning("signals collect failed: %s", e)
            pusher.last_alert_poll_ts = now

        if config.host_pressure_pause_enabled and installs:
            sig = pusher.latest_signals if isinstance(pusher.latest_signals, dict) else {}
            totals = sig.get("totals") if isinstance(sig.get("totals"), dict) else {}
            php_stuck = int(totals.get("php_console_stuck", 0) or 0) if isinstance(totals, dict) else 0
            swap_level = int(totals.get("swap_pressure_level", 0) or 0) if isinstance(totals, dict) else 0
            pressure_reasons: list[str] = []
            if (
                config.host_pressure_php_stuck_pause_threshold > 0
                and php_stuck >= int(config.host_pressure_php_stuck_pause_threshold or 0)
            ):
                pressure_reasons.append(f"php_console_stuck={php_stuck}")
            if (
                config.host_pressure_swap_level_pause_threshold > 0
                and swap_level >= int(config.host_pressure_swap_level_pause_threshold or 0)
            ):
                pressure_reasons.append(f"swap_pressure_level={swap_level}")
            if pressure_reasons:
                pause_sec = int(
                    effective_db_watchdog_config(config).get("dispatch_pause_sec", _DB_DISPATCH_PAUSE_SEC)
                    or _DB_DISPATCH_PAUSE_SEC
                )
                reason = "host pressure: " + ", ".join(pressure_reasons)
                for inst in installs:
                    root = str(getattr(inst, "root", "") or "").strip()
                    if not root:
                        continue
                    _mark_db_dispatch_pause(
                        root=root,
                        reason=reason,
                        now_ts=now,
                        pause_until=db_dispatch_pause_until,
                        pause_reasons=db_dispatch_pause_reasons,
                        pause_sec=pause_sec,
                    )

        if now >= next_plan_refresh_at:
            installs = inventory.list_instances()
            logging.info("Instance inventory count: %d", len(installs))
            if (config.profile_name or "").strip().lower() == "passive":
                for inst in installs:
                    root = str(getattr(inst, "root", "") or "").strip()
                    if not root:
                        continue
                    _dispatch_manual_requests_for_root(
                        config=config,
                        store=store,
                        running=running,
                        popens=popens,
                        root=root,
                        seg_sql_ring=segment_sql_rings.setdefault(root, deque()),
                        seg_prio_ring=segment_prio_rings.setdefault(root, deque()),
                        seg_reg_ring=segment_reg_rings.setdefault(root, deque()),
                        trg_prio_ring=campaign_trigger_prio_rings.setdefault(root, deque()),
                        trg_reg_ring=campaign_trigger_reg_rings.setdefault(root, deque()),
                        reb_prio_ring=campaign_rebuild_prio_rings.setdefault(root, deque()),
                        reb_reg_ring=campaign_rebuild_reg_rings.setdefault(root, deque()),
                    )
                if now >= next_passive_notice_at:
                    logging.info("Passive profile active: automatic planning disabled; manual requests still accepted")
                    next_passive_notice_at = now + 60
                # Do not let a low poll_interval spin the passive branch every
                # loop: MCC state push, config guard and self-update live after
                # planning and must get regular cycles too.
                next_plan_refresh_at = now + max(5, config.poll_interval_sec)
                if single_cycle:
                    return
                time.sleep(max(0.1, float(config.dispatch_interval_sec)))
                continue
            now_utc = datetime.now(timezone.utc)
            now_ts = int(now_utc.timestamp())
            sql_segment_rules_cfg = _parse_sql_segment_rules(getattr(config, "segment_sql_ring_rules", {}))
            if config.segment_sql_ring_enabled and config.segment_mode == "classic_loop" and sql_segment_rules_cfg:
                logging.warning("segment_sql_ring ignored because segment_mode=classic_loop")
            cluster_cron_allowed = cluster_route_allows(config, "cron")
            cluster_import_allowed = cluster_route_allows(config, "import")
            cluster_cache_allowed = cluster_route_allows(config, "cache")
            for inst in installs:
                # Mautic 6 core hotfix: ReloadHelper may pass null metadata to
                # PluginUpdateEvent (strict array in M6), which breaks
                # plugin install/reload. Keep this check idempotent and always
                # active so version upgrades that overwrite the file are
                # auto-patched again on next planning cycle.
                try:
                    patch_gate = should_apply_m6_plugin_update_metadata_patch(
                        inst,
                        policy=config.mautic6_core_patch_policy,
                        version_min=config.mautic6_core_patch_version_min,
                        version_max=config.mautic6_core_patch_version_max,
                        apply_if_version_unknown=bool(config.mautic6_core_patch_apply_if_version_unknown),
                    )
                    if bool(patch_gate.get("apply", False)):
                        patch_res = ensure_m6_plugin_update_metadata_patch(inst)
                        p_status = str(patch_res.get("status", "")).strip().lower()
                        if p_status == "patched":
                            logging.info("[%s] mautic6 core patch applied: %s", inst.root, patch_res.get("path", "-"))
                        elif p_status == "error":
                            logging.warning("[%s] mautic6 core patch error: %s", inst.root, patch_res.get("reason", "unknown"))
                    else:
                        reason = str(patch_gate.get("reason", "")).strip()
                        version = str(patch_gate.get("version", "")).strip() or "-"
                        if reason not in {"policy_off", "not_mautic_6"}:
                            logging.info("[%s] mautic6 core patch skipped: reason=%s version=%s", inst.root, reason or "n/a", version)
                except Exception as e:
                    logging.warning("[%s] mautic6 core patch check failed: %s", inst.root, e)

                try:
                    restore_res = restore_retired_mcd_core_patches(inst)
                    if str(restore_res.get("status", "")).strip().lower() == "error":
                        logging.warning("[%s] retired core patch restore error: %s", inst.root, restore_res.get("errors", []))
                except Exception as e:
                    logging.warning("[%s] retired core patch restore check failed: %s", inst.root, e)

                if not inst.db:
                    continue
                root = inst.root
                segment_whitelist_for_inst = _segment_whitelist_effective_setting(config, inst)
                db = MauticDB(inst.db)
                sql_ctx = campaign_sql_time_context(now_utc, inst.mautic_timezone)
                sql_ring_enabled_for_root = bool(
                    config.segment_sql_ring_enabled and config.segment_mode != "classic_loop"
                )
                sql_rules_for_root: dict[int, SQLSegmentRule] = {}

                segment_ids: list[int] | None = None
                campaign_trigger_ids: list[int] | None = None
                campaign_rebuild_ids: list[int] | None = None
                import_settling = _import_in_settle(root, now)
                if import_settling:
                    import_pending_cache[root] = 0
                elif cluster_import_allowed and now - last_import_poll_ts.get(root, 0.0) >= max(1, config.import_poll_interval_sec):
                    try:
                        import_pending_cache[root] = _fetch_import_pending_count(db, config, root, sql_ctx)
                    except Exception as e:
                        logging.warning("[%s] import query failed: %s", root, e)
                        if _is_db_dispatch_pause_error(e):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=f"import planning db error: {e}",
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                            )
                        import_pending_cache[root] = 0
                    last_import_poll_ts[root] = now
                import_pending_now = max(0, int(import_pending_cache.get(root, 0)))
                import_force_until = float(segment_force_full_scan_until.get(root, 0.0))
                import_hold_sec = max(120, int(config.import_poll_interval_sec) * 8)
                if import_pending_now > 0:
                    import_force_until = max(import_force_until, now + float(import_hold_sec))
                    segment_force_full_scan_until[root] = import_force_until
                force_segment_full_scan = now < import_force_until
                periodic_full_scan_enabled = bool(getattr(config, "segment_periodic_full_scan_enabled", False))
                full_scan_interval_sec = max(0, int(getattr(config, "segment_full_scan_interval_sec", 300) or 0))
                if (
                    periodic_full_scan_enabled
                    and not force_segment_full_scan
                    and full_scan_interval_sec > 0
                    and now - float(segment_last_full_scan_ts.get(root, 0.0)) >= float(full_scan_interval_sec)
                ):
                    force_segment_full_scan = True
                if cluster_cron_allowed:
                    try:
                        if force_segment_full_scan:
                            segment_ids = db.fetch_ids(_SQL_SEGMENTS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                            segment_last_full_scan_ts[root] = now
                            logging.info(
                                "[%s] segment full-scan planned (reason=%s pending_imports=%s interval=%ss)",
                                root,
                                "import_activity" if now < import_force_until else "periodic_full_scan",
                                import_pending_now,
                                full_scan_interval_sec,
                            )
                        else:
                            segment_ids = db.fetch_ids(config.sql_segments_due, limit=5000, context=sql_ctx)
                    except Exception as e:
                        logging.warning("[%s] segment query failed: %s", root, e)
                        if _is_db_dispatch_pause_error(e):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=f"segment planning db error: {e}",
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                            )
                else:
                    segment_ids = []

                campaign_query_error: Exception | None = None
                if cluster_cron_allowed:
                    try:
                        campaign_triggers_due_sql = _campaign_sql_for_major(
                            config.sql_campaign_triggers_due,
                            inst.mautic_major,
                        )
                        campaign_trigger_ids = db.fetch_ids(campaign_triggers_due_sql, limit=5000, context=sql_ctx)
                        audit_interval = max(0, int(getattr(config, "campaign_trigger_audit_interval_sec", 0) or 0))
                        if audit_interval > 0 and now - float(last_campaign_trigger_audit_ts.get(root, 0.0)) >= float(audit_interval):
                            audit_ids = db.fetch_ids(_SQL_CAMPAIGNS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                            if audit_ids:
                                before = set(campaign_trigger_ids)
                                campaign_trigger_ids = list(dict.fromkeys(campaign_trigger_ids + audit_ids))
                                added = sorted(set(campaign_trigger_ids) - before)
                                logging.info(
                                    "[%s] campaign trigger audit planned ids=%s added=%s interval=%ss",
                                    root,
                                    len(audit_ids),
                                    ",".join(str(x) for x in added[:50]) if added else "-",
                                    audit_interval,
                                )
                            last_campaign_trigger_audit_ts[root] = now
                    except Exception as e:
                        campaign_query_error = e
                        logging.warning("[%s] campaign trigger query failed: %s", root, e)
                    if config.enable_campaign_rebuild:
                        try:
                            campaign_rebuilds_due_sql = _campaign_sql_for_major(
                                config.sql_campaign_rebuilds_due,
                                inst.mautic_major,
                            )
                            campaign_rebuild_ids = db.fetch_ids(campaign_rebuilds_due_sql, limit=5000, context=sql_ctx)
                        except Exception as e:
                            campaign_query_error = e
                            logging.warning("[%s] campaign rebuild query failed: %s", root, e)
                    else:
                        campaign_rebuild_ids = []
                else:
                    campaign_trigger_ids = []
                    campaign_rebuild_ids = []

                if campaign_query_error is not None and _is_db_dispatch_pause_error(campaign_query_error):
                    _mark_db_dispatch_pause(
                        root=root,
                        reason=f"campaign planning db error: {campaign_query_error}",
                        now_ts=now,
                        pause_until=db_dispatch_pause_until,
                        pause_reasons=db_dispatch_pause_reasons,
                    )

                if campaign_trigger_ids is not None and (config.profile_name or "").strip().lower() == "tiny":
                    # Tiny mode: newest published campaigns first.
                    campaign_trigger_ids = sorted(list(dict.fromkeys(campaign_trigger_ids)), reverse=True)
                if campaign_rebuild_ids is not None and (config.profile_name or "").strip().lower() == "tiny":
                    campaign_rebuild_ids = sorted(list(dict.fromkeys(campaign_rebuild_ids)), reverse=True)

                if segment_ids is not None:
                    standard_segment_ids = list(dict.fromkeys(segment_ids))
                    if segment_whitelist_for_inst:
                        try:
                            published_whitelist_ids = _published_segment_whitelist_ids(
                                db,
                                segment_whitelist_for_inst,
                                sql_ctx,
                            )
                            if published_whitelist_ids:
                                before = set(standard_segment_ids)
                                standard_segment_ids = list(dict.fromkeys(standard_segment_ids + published_whitelist_ids))
                                added = sorted(set(standard_segment_ids) - before)
                                if added:
                                    logging.info(
                                        "[%s] segment whitelist planned ids=%s",
                                        root,
                                        ",".join(str(x) for x in added[:50]),
                                    )
                        except Exception as e:
                            logging.warning("[%s] segment whitelist publish check failed: %s", root, e)
                    dependency_children: dict[int, set[int]] = {}
                    dependency_parents: dict[int, set[int]] = {}
                    try:
                        dep_rows = db.fetch_published_segment_filters()
                        dependency_children, dependency_parents = segment_dependency_maps(dep_rows)
                        segment_dependencies_by_root[root] = dependency_children
                        segment_parents_by_root[root] = dependency_parents
                        if int(getattr(inst, "mautic_major", 0) or 0) >= 7:
                            standard_segment_ids, suppressed_ids = suppress_mautic_cascade_dependencies(
                                standard_segment_ids,
                                dependency_parents,
                            )
                            if suppressed_ids:
                                logging.info(
                                    "[%s] mautic7 segment dependency cascade covered ids=%s",
                                    root,
                                    ",".join(str(x) for x in sorted(suppressed_ids)[:50]),
                                )
                        else:
                            recently_finished = _recent_finished_segment_ids(root, now)
                            dependent_ids = stale_dependent_segment_closure(
                                recently_finished,
                                dep_rows,
                                dependency_children,
                            )
                            if dependent_ids:
                                before = set(standard_segment_ids)
                                standard_segment_ids = list(dict.fromkeys(standard_segment_ids + sorted(dependent_ids)))
                                added = sorted(set(standard_segment_ids) - before)
                                if added:
                                    logging.info(
                                        "[%s] segment dependency follow-up planned parents=%s children=%s",
                                        root,
                                        ",".join(str(x) for x in sorted(recently_finished)[:30]),
                                        ",".join(str(x) for x in added[:50]),
                                    )
                    except Exception as e:
                        dependency_children = segment_dependencies_by_root.get(root, {})
                        dependency_parents = segment_parents_by_root.get(root, {})
                        logging.warning("[%s] segment dependency planning failed: %s", root, e)
                    segment_definition_rows: list[dict[str, object]] = []
                    invalid_filter_ids: set[int] = set()
                    if standard_segment_ids:
                        try:
                            segment_definition_rows = db.fetch_segment_definitions(standard_segment_ids)
                            invalid_issues = segment_invalid_filter_issues(segment_definition_rows)
                            invalid_filter_ids = set(invalid_issues)
                            if invalid_filter_ids:
                                _log_invalid_segment_filters(
                                    root,
                                    format_segment_filter_issues(invalid_issues),
                                )
                                standard_segment_ids = [
                                    sid for sid in standard_segment_ids if sid not in invalid_filter_ids
                                ]
                                segment_definition_rows = [
                                    row
                                    for row in segment_definition_rows
                                    if int(row.get("id") or 0) not in invalid_filter_ids
                                ]
                        except Exception as e:
                            logging.warning("[%s] segment filter validation failed: %s", root, e)
                    segment_invalid_filter_sets[root] = invalid_filter_ids
                    failure_blocked_ids = _segment_failure_blocked_ids(store, root)
                    for sid in sorted(set(standard_segment_ids) & failure_blocked_ids)[:20]:
                        _log_segment_failure_cooldown(root, sid)
                    segment_failure_blocked_sets[root] = failure_blocked_ids
                    auto_sql_rules_for_root: dict[int, SQLSegmentRule] = {}
                    if sql_ring_enabled_for_root and config.segment_sql_auto_enabled and standard_segment_ids:
                        try:
                            recent_problem_counts = store.recent_task_problem_counts(
                                root=root,
                                task_type="segment",
                                since_sec=max(6 * 3600, int(config.tasks_history_keep_days or 1) * 86400),
                            )
                            segment_rows = segment_definition_rows or db.fetch_segment_definitions(standard_segment_ids)
                            for row in segment_rows:
                                try:
                                    sid = int(row.get("id") or 0)
                                except Exception:
                                    sid = 0
                                if sid > 0:
                                    row["problem_count"] = int(recent_problem_counts.get(sid, 0) or 0)
                            try:
                                lead_columns = db.fetch_lead_columns()
                            except Exception as e:
                                lead_columns = None
                                logging.warning("[%s] segment_sql lead column introspection failed: %s", root, e)
                            detected_rules = detect_auto_sql_segment_rules(
                                segment_rows,
                                max_clauses=config.segment_sql_auto_max_clauses,
                                problem_threshold=config.segment_sql_auto_problem_threshold,
                                lead_columns=lead_columns,
                            )
                            auto_sql_rules_for_root = {
                                sid: SQLSegmentRule(
                                    segment_id=rule.segment_id,
                                    select_sql=rule.select_sql,
                                    depends_on=rule.depends_on,
                                )
                                for sid, rule in detected_rules.items()
                            }
                            auto_sig = tuple(sorted(auto_sql_rules_for_root))
                            prev_auto_sig = segment_sql_auto_signatures.get(root, ())
                            if auto_sig != prev_auto_sig:
                                if auto_sig:
                                    details = []
                                    for sid in auto_sig[:20]:
                                        meta: DetectedSQLSegmentRule | None = detected_rules.get(sid)
                                        if meta is None:
                                            continue
                                        details.append(
                                            f"{sid}({meta.reason};clauses={meta.clause_count})"
                                        )
                                    logging.info(
                                        "[%s] segment_sql auto-managed ids=%s details=%s",
                                        root,
                                        ",".join(str(x) for x in auto_sig),
                                        " | ".join(details) if details else "-",
                                    )
                                elif prev_auto_sig:
                                    logging.info("[%s] segment_sql auto-managed ids cleared", root)
                                segment_sql_auto_signatures[root] = auto_sig
                        except Exception as e:
                            logging.warning("[%s] segment_sql auto-detect failed: %s", root, e)

                    if sql_ring_enabled_for_root:
                        sql_rules_for_root = dict(auto_sql_rules_for_root)
                        sql_rules_for_root.update(dict(sql_segment_rules_cfg))
                        if dependency_parents:
                            for sid, rule in list(sql_rules_for_root.items()):
                                deps = tuple(sorted(set(rule.depends_on) | set(dependency_parents.get(int(sid), set()))))
                                if deps != rule.depends_on:
                                    sql_rules_for_root[sid] = SQLSegmentRule(
                                        segment_id=rule.segment_id,
                                        select_sql=rule.select_sql,
                                        depends_on=deps,
                                    )
                    if sql_ring_enabled_for_root and sql_rules_for_root:
                        sql_ring_plan = _plan_sql_segment_ring(standard_segment_ids, sql_rules_for_root)
                        active_sql_set = set(sql_ring_plan)
                        segment_sql_rings[root] = _reconcile_ring(
                            segment_sql_rings.get(root),
                            sql_ring_plan,
                            new_to_front=True,
                        )
                        segment_sql_rules_by_root[root] = sql_rules_for_root
                        segment_sql_active_sets[root] = active_sql_set
                        prev_done = set(segment_sql_done_sets.get(root, set()))
                        segment_sql_done_sets[root] = {sid for sid in prev_done if sid in active_sql_set}
                        standard_segment_ids = [sid for sid in standard_segment_ids if sid not in active_sql_set]
                    else:
                        segment_sql_rings[root] = deque()
                        segment_sql_rules_by_root[root] = {}
                        segment_sql_active_sets[root] = set()
                        segment_sql_done_sets[root] = set()

                    segment_weight_rows: list[dict[str, object]] = []
                    seg_w: dict[int, float] = {}
                    stale_seg_ids: set[int] = set()
                    if standard_segment_ids:
                        try:
                            segment_weight_rows = db.fetch_rows(config.sql_segment_weights, limit=5000, context=sql_ctx)
                        except Exception as e:
                            logging.warning("[%s] segment weight query failed: %s", root, e)
                            segment_weight_rows = []

                        seg_w = store.get_weights("segment", root, config.weights_recalc_interval_sec)
                        if _needs_weight_recalc(standard_segment_ids, seg_w):
                            seg_w = _segment_weights(
                                standard_segment_ids,
                                segment_weight_rows,
                                segment_whitelist_for_inst,
                                now_ts,
                            )
                            store.put_weights("segment", root, seg_w)
                        stale_seg_ids = _stale_segment_priority_ids(
                            standard_segment_ids,
                            segment_weight_rows,
                            now_ts,
                            stale_sec=_SEGMENT_STALE_PRIORITY_SEC,
                        )
                    else:
                        store.put_weights("segment", root, {})

                    seg_prio, seg_reg = _split_segment_circles(
                        standard_segment_ids,
                        seg_w,
                        segment_whitelist_for_inst,
                        config.segment_priority_weight_threshold,
                        config.segment_priority_size,
                        stale_priority_ids=stale_seg_ids,
                    )
                    if config.ring_mode == "single":
                        seg_prio, seg_reg = [], list(dict.fromkeys(standard_segment_ids))
                    if not _partition_complete(standard_segment_ids, seg_prio, seg_reg):
                        logging.warning("[%s] invalid segment partition, forcing single ring", root)
                        seg_prio, seg_reg = [], sorted(list(dict.fromkeys(standard_segment_ids)))
                    segment_prio_rings[root] = _reconcile_ring(
                        segment_prio_rings.get(root),
                        seg_prio,
                        new_to_front=True,
                    )
                    segment_reg_rings[root] = _reconcile_ring(
                        segment_reg_rings.get(root),
                        seg_reg,
                        new_to_front=True,
                    )
                    segment_prio_sets[root] = set(seg_prio)
                    segment_reg_sets[root] = set(seg_reg)
                else:
                    logging.warning("[%s] segment planning skipped: preserving previous segment rings", root)

                if campaign_trigger_ids is not None and campaign_rebuild_ids is not None:
                    campaign_trigger_ids = list(dict.fromkeys(campaign_trigger_ids))
                    campaign_rebuild_ids = list(dict.fromkeys(campaign_rebuild_ids))
                    campaign_all_ids = list(dict.fromkeys(campaign_trigger_ids + campaign_rebuild_ids))
                    camp_w = store.get_weights("campaign", root, config.weights_recalc_interval_sec)
                    campaign_weight_rows: list[dict[str, object]] = []
                    campaign_weight_query_failed = False
                    if _needs_weight_recalc(campaign_all_ids, camp_w):
                        try:
                            campaign_weights_sql = _campaign_sql_for_major(config.sql_campaign_weights, inst.mautic_major)
                            campaign_weight_rows = db.fetch_rows(campaign_weights_sql, limit=5000, context=sql_ctx)
                        except Exception as e:
                            logging.warning("[%s] campaign weight query failed: %s", root, e)
                            campaign_weight_query_failed = True
                            campaign_weight_rows = []
                        camp_w = _campaign_weights(campaign_all_ids, campaign_weight_rows, campaign_whitelist, now_ts)
                        store.put_weights("campaign", root, camp_w)
                    latest_priority_ids = _latest_campaign_ids(campaign_weight_rows, config.campaign_latest_priority_count)
                    if not latest_priority_ids:
                        # If weights are cached (no recalc) or weight query failed,
                        # keep newest published campaigns in priority lane using
                        # id-based fallback without issuing extra heavy SQL.
                        latest_priority_ids = _latest_campaign_ids_from_ids(campaign_all_ids, config.campaign_latest_priority_count)
                    if campaign_weight_query_failed:
                        logging.info("[%s] campaign latest-priority fallback to id order", root)

                    trg_prio, trg_reg = _split_campaign_circles(
                        campaign_trigger_ids,
                        camp_w,
                        campaign_whitelist,
                        config.campaign_priority_size,
                        latest_priority_ids,
                    )
                    reb_prio, reb_reg = _split_campaign_circles(
                        campaign_rebuild_ids,
                        camp_w,
                        campaign_whitelist,
                        config.campaign_priority_size,
                        latest_priority_ids,
                    )
                    if config.ring_mode == "single":
                        trg_prio, trg_reg = [], list(dict.fromkeys(campaign_trigger_ids))
                        reb_prio, reb_reg = [], list(dict.fromkeys(campaign_rebuild_ids))
                    if not _partition_complete(campaign_trigger_ids, trg_prio, trg_reg):
                        logging.warning("[%s] invalid campaign trigger partition, forcing single ring", root)
                        trg_prio, trg_reg = [], sorted(list(dict.fromkeys(campaign_trigger_ids)))
                    if not _partition_complete(campaign_rebuild_ids, reb_prio, reb_reg):
                        logging.warning("[%s] invalid campaign rebuild partition, forcing single ring", root)
                        reb_prio, reb_reg = [], sorted(list(dict.fromkeys(campaign_rebuild_ids)))
                    campaign_trigger_prio_rings[root] = _reconcile_ring(campaign_trigger_prio_rings.get(root), trg_prio)
                    campaign_trigger_reg_rings[root] = _reconcile_ring(campaign_trigger_reg_rings.get(root), trg_reg)
                    campaign_rebuild_prio_rings[root] = _reconcile_ring(campaign_rebuild_prio_rings.get(root), reb_prio)
                    campaign_rebuild_reg_rings[root] = _reconcile_ring(campaign_rebuild_reg_rings.get(root), reb_reg)
                    campaign_trigger_prio_sets[root] = set(trg_prio)
                    campaign_trigger_reg_sets[root] = set(trg_reg)
                    campaign_rebuild_prio_sets[root] = set(reb_prio)
                    campaign_rebuild_reg_sets[root] = set(reb_reg)
                else:
                    _clear_campaign_rings(
                        root=root,
                        trigger_prio_rings=campaign_trigger_prio_rings,
                        trigger_reg_rings=campaign_trigger_reg_rings,
                        rebuild_prio_rings=campaign_rebuild_prio_rings,
                        rebuild_reg_rings=campaign_rebuild_reg_rings,
                        trigger_prio_sets=campaign_trigger_prio_sets,
                        trigger_reg_sets=campaign_trigger_reg_sets,
                        rebuild_prio_sets=campaign_rebuild_prio_sets,
                        rebuild_reg_sets=campaign_rebuild_reg_sets,
                    )
                    logging.warning("[%s] campaign planning skipped: campaign rings cleared", root)

                # Use freshly planned rings/sets in the same tick.
                # Without rebinding, dispatch operates on previous-cycle objects
                # (captured before reconcile), which can skew 3+1 behavior.
                seg_prio_ring = segment_prio_rings.setdefault(root, deque())
                seg_reg_ring = segment_reg_rings.setdefault(root, deque())
                trg_prio_ring = campaign_trigger_prio_rings.setdefault(root, deque())
                trg_reg_ring = campaign_trigger_reg_rings.setdefault(root, deque())
                reb_prio_ring = campaign_rebuild_prio_rings.setdefault(root, deque())
                reb_reg_ring = campaign_rebuild_reg_rings.setdefault(root, deque())
                seg_prio_set = segment_prio_sets.setdefault(root, set(seg_prio_ring))
                seg_reg_set = segment_reg_sets.setdefault(root, set(seg_reg_ring))
                trg_prio_set = campaign_trigger_prio_sets.setdefault(root, set(trg_prio_ring))
                trg_reg_set = campaign_trigger_reg_sets.setdefault(root, set(trg_reg_ring))
                reb_prio_set = campaign_rebuild_prio_sets.setdefault(root, set(reb_prio_ring))
                reb_reg_set = campaign_rebuild_reg_sets.setdefault(root, set(reb_reg_ring))

                q_samples = queue_samples.setdefault(root, deque())
                if not cluster_cron_allowed:
                    throttled[root] = False
                elif config.disable_throttle:
                    throttled[root] = False
                else:
                    try:
                        queue_count = db.fetch_count(config.sql_mail_queue_count, context=sql_ctx)
                    except Exception as e:
                        logging.warning("[%s] mail queue query failed: %s", root, e)
                        if _is_db_dispatch_pause_error(e):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=f"mail queue db error: {e}",
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                            )
                        queue_count = 0
                    q_samples.append((now, queue_count))
                    throttled[root] = _compute_throttle_active(
                        q_samples,
                        threshold=config.queue_throttle_threshold,
                        window_min=config.queue_throttle_window_min,
                    )

            next_plan_refresh_at = now + max(1, config.poll_interval_sec)

        if config.fs_permissions_guard_enabled:
            guard_interval_sec = max(15, int(config.fs_permissions_guard_interval_sec or 300))
            for inst in installs:
                root = str(inst.root or "").strip()
                if not root:
                    continue
                last_ts = float(last_fs_permissions_guard_ts.get(root, 0.0))
                if last_ts > 0 and now - last_ts < guard_interval_sec:
                    continue
                try:
                    res = ensure_instance_permissions(
                        root=root,
                        run_as_user=config.mautic_run_as_user or "www-data",
                        guard_paths=config.fs_permissions_guard_paths,
                        fix_console_exec=bool(config.fs_permissions_guard_fix_console_exec),
                        console_relpath=config.fs_permissions_guard_console_relpath,
                    )
                    if res.repaired_paths or res.console_exec_fixed:
                        fix_count = len(res.repaired_paths) + (1 if res.console_exec_fixed else 0)
                        repaired_events = [e for e in (res.repair_events or []) if bool(getattr(e, "repaired", False))]
                        event_payload = [
                            {
                                "ts": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "path": str(getattr(e, "rel_path", "") or getattr(e, "target_path", "") or "").strip(),
                                "sample_path": str(getattr(e, "sample_path", "") or "").strip(),
                                "reason": str(getattr(e, "reason", "") or "").strip(),
                                "actor": str(getattr(e, "actor", "") or "").strip(),
                                "actor_source": str(getattr(e, "actor_source", "") or "").strip(),
                                "before_owner_group": str(getattr(e, "before_owner_group", "") or "").strip(),
                                "before_mode": str(getattr(e, "before_mode", "") or "").strip(),
                                "result": "repaired",
                                "error": "",
                            }
                            for e in repaired_events
                        ]
                        try:
                            pusher.add_fs_permissions_fix(
                                fix_count,
                                events=event_payload,
                                now_ts=now,
                            )
                        except Exception:
                            pass
                        detail_parts: list[str] = []
                        for ev in repaired_events[:8]:
                            detail_parts.append(
                                (
                                    f"path={ev.rel_path or ev.target_path} "
                                    f"reason={ev.reason or '-'} "
                                    f"actor={ev.actor or 'unknown'} "
                                    f"source={ev.actor_source or 'unknown'} "
                                    f"before={ev.before_owner_group or '-'} mode={ev.before_mode or '-'}"
                                )
                            )
                        logging.warning(
                            "[%s] fs permissions guard repaired count=%s paths=%s console_exec_fixed=%s details=%s",
                            root,
                            str(fix_count),
                            ",".join(res.repaired_paths) if res.repaired_paths else "-",
                            "yes" if res.console_exec_fixed else "no",
                            " || ".join(detail_parts) if detail_parts else "-",
                        )
                    if res.errors:
                        failed_parts: list[str] = []
                        for ev in (res.repair_events or []):
                            if bool(getattr(ev, "repaired", False)):
                                continue
                            err = str(getattr(ev, "error", "") or "").strip()
                            if not err:
                                continue
                            failed_parts.append(
                                (
                                    f"path={ev.rel_path or ev.target_path} "
                                    f"reason={ev.reason or '-'} "
                                    f"actor={ev.actor or 'unknown'} "
                                    f"source={ev.actor_source or 'unknown'} "
                                    f"error={err}"
                                )
                            )
                        logging.warning(
                            "[%s] fs permissions guard errors: %s%s",
                            root,
                            " | ".join(res.errors),
                            f" || details={' || '.join(failed_parts[:8])}" if failed_parts else "",
                        )
                except Exception as e:
                    logging.warning("[%s] fs permissions guard failed: %s", root, e)
                last_fs_permissions_guard_ts[root] = now

        db_watchdog_cfg = effective_db_watchdog_config(config)
        if bool(db_watchdog_cfg.get("enabled", False)):
            db_watchdog_interval_sec = max(30, int(db_watchdog_cfg.get("interval_sec", 300) or 300))
            for inst in installs:
                root = str(inst.root or "").strip()
                if not root:
                    continue
                last_ts = float(last_db_watchdog_ts.get(root, 0.0))
                if last_ts > 0 and now - last_ts < db_watchdog_interval_sec:
                    continue
                try:
                    running_types = [t.task_type for t in running.values() if t.root == root]
                    snap = collect_db_watchdog_snapshot(
                        cfg=config,
                        inst=inst,
                        running_task_types=running_types,
                    )
                    pusher.add_db_watchdog_observation(snap, now_ts=now)
                    if str(snap.get("status", "")).strip().lower() == "ok":
                        pl = snap.get("processlist") if isinstance(snap.get("processlist"), dict) else {}
                        rule = snap.get("rules") if isinstance(snap.get("rules"), dict) else {}
                        lock_waits = int(pl.get("metadata_lock_waits", 0) or 0)
                        long_q = int(pl.get("long_queries", 0) or 0)
                        orphans = int(pl.get("orphan_candidates", 0) or 0)
                        rule_hits = int(rule.get("hit_total", 0) or 0)
                        if lock_waits > 0 or long_q > 0 or orphans > 0 or rule_hits > 0:
                            logging.warning(
                                "[%s] db_watchdog observe: metadata_lock_waits=%s long_queries=%s orphan_candidates=%s rule_hits=%s",
                                root,
                                lock_waits,
                                long_q,
                                orphans,
                                rule_hits,
                            )
                        long_q_threshold = int(
                            db_watchdog_cfg.get(
                                "long_queries_pause_threshold",
                                _DB_WATCHDOG_LONG_QUERIES_PAUSE_THRESHOLD,
                            )
                            or 0
                        )
                        lock_threshold = int(
                            db_watchdog_cfg.get(
                                "metadata_lock_waits_pause_threshold",
                                _DB_WATCHDOG_METADATA_LOCKS_PAUSE_THRESHOLD,
                            )
                            or 0
                        )
                        if (long_q_threshold > 0 and long_q >= long_q_threshold) or (
                            lock_threshold > 0 and lock_waits >= lock_threshold
                        ):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=(
                                    "db_watchdog overload: "
                                    f"metadata_lock_waits={lock_waits} long_queries={long_q}"
                                ),
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                                pause_sec=int(db_watchdog_cfg.get("dispatch_pause_sec", _DB_DISPATCH_PAUSE_SEC) or _DB_DISPATCH_PAUSE_SEC),
                            )
                    else:
                        logging.warning("[%s] db_watchdog collect skipped/error: %s", root, snap.get("reason", "-"))
                except Exception as e:
                    logging.warning("[%s] db_watchdog collect failed: %s", root, e)
                last_db_watchdog_ts[root] = now

        if pusher.enabled():
            pending_profile_event = read_pending_profile_event(config)
            payload = pusher.build_payload(
                installs=installs,
                profile_name=config.profile_name,
                now_ts=now,
                profile_event=pending_profile_event,
            )
            payload_no_ts = dict(payload)
            payload_no_ts.pop("sent_at_utc", None)
            do_push, _changed = pusher.should_push(now, payload_no_ts)
            if pending_profile_event:
                do_push = True
            if do_push:
                ok, msg = pusher.send(payload)
                log_push_result(ok, msg)
                if ok:
                    pusher.mark_pushed(now, payload_no_ts)
                if pending_profile_event:
                    clear_pending_profile_event(
                        config,
                        event_id=str(pending_profile_event.get("event_id", "")).strip() or None,
                        delivered=bool(ok),
                        error=None if ok else str(msg),
                    )

        if (
            bool(getattr(config, "mcc_profile_guard_enabled", False))
            and config.mcc_url
            and config.mcc_token
            and now >= next_profile_guard_at
        ):
            guard_interval = max(60, int(config.mcc_push_interval_sec or config.poll_interval_sec or 300))
            try:
                drift = check_profile_drift_with_mcc(
                    config.config_file_path,
                    current_profile=(config.profile_name or ""),
                    current_config_sha=(config.config_sha256 or ""),
                )
                if str(drift.get("status", "")).strip().lower() == "drift":
                    old_profile = (config.profile_name or "").strip().lower() or None
                    desired_profile = str(drift.get("desired_profile", "")).strip().lower() or None
                    drift_reason = str(drift.get("reason", "")).strip() or "config_drift"
                    ok, note = recover_config_from_mcc(
                        config.config_file_path,
                        reason=(
                            f"{drift_reason}"
                            f" local={old_profile or '-'} desired={desired_profile or '-'}"
                            f" source={str(drift.get('config_source', '-') or '-')}"
                        ),
                    )
                    if ok:
                        logging.error(
                            "profile drift detected and repaired from MCC desired config: local=%s desired=%s (%s)",
                            old_profile or "-",
                            desired_profile or "-",
                            note,
                        )
                        config = load_config(config.config_file_path, allow_recover_from_mcc=False)
                        base_config = config
                        pusher.cfg = config
                        next_plan_refresh_at = 0.0
                        next_update_check_at = 0.0
                        next_service_profile_apply_at = 0.0
                        next_runtime_overrides_poll_at = 0.0
                        campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
                        if config.disable_whitelist:
                            campaign_whitelist = set()
                        try:
                            queue_profile_event(
                                config,
                                source="auto_recover",
                                initiated_by_user=False,
                                old_profile=old_profile,
                                new_profile=(config.profile_name or "").strip().lower() or desired_profile,
                                reason="auto_recover_profile_drift",
                                details={
                                    "desired_profile": desired_profile,
                                    "mcc_config_source": str(drift.get("config_source", "")).strip() or None,
                                },
                            )
                        except Exception as e:
                            logging.warning("profile-drift repair event enqueue failed: %s", e)
                    else:
                        logging.error(
                            "profile drift detected but MCC repair failed: local=%s desired=%s err=%s",
                            old_profile or "-",
                            desired_profile or "-",
                            note,
                        )
            except Exception as e:
                logging.warning("profile guard check failed: %s", e)
            next_profile_guard_at = now + guard_interval

        if now >= next_viber_cron_reconcile_at:
            try:
                vcron = reconcile_viber_stats_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                )
                changed = [
                    line
                    for line in vcron.lines
                    if "commented viber stats" in line or "restored managed cron" in line
                ]
                if changed:
                    logging.info("viber stats cron reconcile: %s", "; ".join(changed))
                elif not vcron.ok:
                    logging.warning("viber stats cron reconcile failed: %s", "; ".join(vcron.lines))
            except Exception as e:
                logging.warning("viber stats cron reconcile failed: %s", e)
            next_viber_cron_reconcile_at = now + 60

        if now >= next_email_fetch_cron_reconcile_at:
            try:
                email_cron = reconcile_mautic_email_fetch_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                    enabled=_any_monitored_email_fetch_replaces_mautic(config, installs),
                )
                changed = [line for line in email_cron.lines if "commented mautic email fetch" in line]
                if changed:
                    logging.info("mautic email fetch cron reconcile: %s", "; ".join(changed))
                elif not email_cron.ok:
                    logging.warning("mautic email fetch cron reconcile failed: %s", "; ".join(email_cron.lines))
            except Exception as e:
                logging.warning("mautic email fetch cron reconcile failed: %s", e)
            next_email_fetch_cron_reconcile_at = now + 60

        if now >= next_empty_leads_cleanup_cron_reconcile_at:
            try:
                ecron = reconcile_empty_leads_cleanup_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                )
                changed = [
                    line
                    for line in ecron.lines
                    if "commented empty leads cleanup" in line or "MCD_EMPTY_LEADS_MIGRATE" in line
                ]
                migrated = _migrate_empty_leads_cleanup_runtime(config, ecron.lines)
                if changed:
                    logging.info("empty leads cleanup cron reconcile: %s", "; ".join(changed))
                elif not ecron.ok:
                    logging.warning("empty leads cleanup cron reconcile failed: %s", "; ".join(ecron.lines))
                if migrated:
                    runtime_overrides_sync_requested = True
                    next_runtime_overrides_poll_at = 0.0
            except Exception as e:
                logging.warning("empty leads cleanup cron reconcile failed: %s", e)
            next_empty_leads_cleanup_cron_reconcile_at = now + 60

        if (config.profile_name or "").strip().lower() == "passive":
            for inst in installs:
                root = str(getattr(inst, "root", "") or "").strip()
                if not root:
                    continue
                _dispatch_manual_requests_for_root(
                    config=config,
                    store=store,
                    running=running,
                    popens=popens,
                    root=root,
                    seg_sql_ring=segment_sql_rings.setdefault(root, deque()),
                    seg_prio_ring=segment_prio_rings.setdefault(root, deque()),
                    seg_reg_ring=segment_reg_rings.setdefault(root, deque()),
                    trg_prio_ring=campaign_trigger_prio_rings.setdefault(root, deque()),
                    trg_reg_ring=campaign_trigger_reg_rings.setdefault(root, deque()),
                    reb_prio_ring=campaign_rebuild_prio_rings.setdefault(root, deque()),
                    reb_reg_ring=campaign_rebuild_reg_rings.setdefault(root, deque()),
                )
            if now >= next_passive_notice_at:
                logging.info("Passive profile active: automatic planning disabled; manual requests still accepted")
                next_passive_notice_at = now + 60
            if single_cycle:
                return
            time.sleep(max(0.1, float(config.dispatch_interval_sec)))
            continue

        scheduler_dispatch_pause = False
        try:
            scheduler_dispatch_pause = Path(config.scheduler_pause_flag_path).exists()
        except Exception as e:
            logging.warning("scheduler pause flag check failed: %s", e)
            scheduler_dispatch_pause = False
        if scheduler_dispatch_pause != scheduler_dispatch_pause_active:
            if scheduler_dispatch_pause:
                logging.info(
                    "scheduler pause active (%s): new task dispatch paused for all task types (running tasks continue)",
                    config.scheduler_pause_flag_path,
                )
            else:
                logging.info("scheduler pause released: task dispatch resumed")
            scheduler_dispatch_pause_active = scheduler_dispatch_pause

        backup_running = False
        if config.backup_enabled:
            try:
                backup_running = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(config)
            except Exception as e:
                logging.warning("backup lock check failed: %s", e)
                backup_running = False
        backup_dispatch_pause, backup_pause_reason = _backup_dispatch_pause_state(
            config,
            backup_running=backup_running,
        )
        if backup_dispatch_pause != backup_dispatch_pause_active:
            if backup_dispatch_pause:
                logging.info(
                    "backup guard active (%s): new task dispatch paused for all task types (running tasks continue)",
                    backup_pause_reason or "unknown",
                )
            else:
                logging.info("backup guard released: task dispatch resumed")
            backup_dispatch_pause_active = backup_dispatch_pause
        dispatch_pause = bool(scheduler_dispatch_pause or backup_dispatch_pause)
        cluster_cron_allowed = cluster_route_allows(config, "cron")
        cluster_import_allowed = cluster_route_allows(config, "import")
        cluster_cache_allowed = cluster_route_allows(config, "cache")

        for inst in installs:
            if not inst.db:
                logging.warning("[%s] skip install without db config", inst.root)
                continue

            root = inst.root
            segment_whitelist_for_inst = _segment_whitelist_effective_setting(config, inst)
            db = MauticDB(inst.db)
            now_utc = datetime.now(timezone.utc)
            sql_ctx = campaign_sql_time_context(now_utc, inst.mautic_timezone)
            inst_now = mautic_local_datetime(now_utc, inst.mautic_timezone)
            seg_sql_ring = segment_sql_rings.setdefault(root, deque())
            seg_sql_rules = segment_sql_rules_by_root.setdefault(root, {})
            seg_sql_active = segment_sql_active_sets.setdefault(root, set())
            seg_sql_done = segment_sql_done_sets.setdefault(root, set())
            seg_prio_ring = segment_prio_rings.setdefault(root, deque())
            seg_reg_ring = segment_reg_rings.setdefault(root, deque())
            trg_prio_ring = campaign_trigger_prio_rings.setdefault(root, deque())
            trg_reg_ring = campaign_trigger_reg_rings.setdefault(root, deque())
            reb_prio_ring = campaign_rebuild_prio_rings.setdefault(root, deque())
            reb_reg_ring = campaign_rebuild_reg_rings.setdefault(root, deque())
            seg_prio_set = segment_prio_sets.setdefault(root, set())
            seg_reg_set = segment_reg_sets.setdefault(root, set())
            trg_prio_set = campaign_trigger_prio_sets.setdefault(root, set())
            trg_reg_set = campaign_trigger_reg_sets.setdefault(root, set())
            reb_prio_set = campaign_rebuild_prio_sets.setdefault(root, set())
            reb_reg_set = campaign_rebuild_reg_sets.setdefault(root, set())
            segment_blocked_ids = segment_dependency_blocked_ids(
                root=root,
                candidate_ids=set(seg_prio_set) | set(seg_reg_set) | set(seg_sql_active),
                parents_by_child=segment_parents_by_root.get(root, {}),
                running=running,
                recently_finished=_recent_finished_segment_ids(root, now),
            )
            segment_blocked_ids |= set(segment_invalid_filter_sets.get(root, set()))
            segment_blocked_ids |= set(segment_failure_blocked_sets.get(root, set()))
            if _import_in_settle(root, now):
                import_pending_cache[root] = 0
            _mark_external_entities_executed(
                running=running,
                root=root,
                seg_sql_done=seg_sql_done,
                seg_sql_ring=seg_sql_ring,
                seg_prio_ring=seg_prio_ring,
                seg_reg_ring=seg_reg_ring,
                trg_prio_ring=trg_prio_ring,
                trg_reg_ring=trg_reg_ring,
                reb_prio_ring=reb_prio_ring,
                reb_reg_ring=reb_reg_ring,
            )

            if dispatch_pause:
                continue
            db_pause_until = float(db_dispatch_pause_until.get(root, 0.0))
            if db_pause_until > now:
                continue
            if db_pause_until > 0:
                logging.info(
                    "[%s] db dispatch circuit-breaker released: %s",
                    root,
                    db_dispatch_pause_reasons.get(root, "-"),
                )
                db_dispatch_pause_until.pop(root, None)
                db_dispatch_pause_reasons.pop(root, None)

            _dispatch_manual_requests_for_root(
                config=config,
                store=store,
                running=running,
                popens=popens,
                root=root,
                seg_sql_ring=seg_sql_ring,
                seg_prio_ring=seg_prio_ring,
                seg_reg_ring=seg_reg_ring,
                trg_prio_ring=trg_prio_ring,
                trg_reg_ring=trg_reg_ring,
                reb_prio_ring=reb_prio_ring,
                reb_reg_ring=reb_reg_ring,
            )

            if not (cluster_cron_allowed or cluster_import_allowed or cluster_cache_allowed):
                continue

            segment_slot_limit = _effective_segment_slot_limit(config, throttled.get(root, False))
            import_segment_slot_limit = max(1, segment_slot_limit) if cluster_import_allowed else segment_slot_limit
            _submit_import_if_segment_slot(
                config=config,
                store=store,
                running=running,
                popens=popens,
                root=root,
                cluster_import_allowed=cluster_import_allowed,
                import_pending_count=max(0, int(import_pending_cache.get(root, 0) or 0)),
                segment_slot_limit=import_segment_slot_limit,
                now_ts=now,
            )

            if (
                cluster_cron_allowed
                and config.segment_mode != "classic_loop"
                and _segment_shared_slots_available(running, root, segment_slot_limit) > 0
            ):
                _run_sql_segment_ring(
                    config=config,
                    store=store,
                    db=db,
                    root=root,
                    ring=seg_sql_ring,
                    rules=seg_sql_rules,
                    active_set=seg_sql_active,
                    done_set=seg_sql_done,
                    running=running,
                    sql_ctx=sql_ctx,
                    now_ts=now,
                    now_local=inst_now,
                )

            if cluster_cron_allowed and config.segment_mode == "classic_loop":
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_segment_full_update_template,
                    batch_limit=config.segment_batch_limit,
                )
                classic_segment_limit = _segment_task_limit_after_import(running, root, 1)
                if classic_segment_limit > 0:
                    _submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=root,
                        task_type="segment",
                        entity_id=None,
                        args=args,
                        timeout_sec=config.command_timeout_sec,
                        max_parallel_for_type=classic_segment_limit,
                        popens=popens,
                    )
            elif cluster_cron_allowed:
                if throttled.get(root, False):
                    if config.segment_throttle_whitelist_only:
                        seg_prio_limit = max(0, config.segment_throttle_whitelist_parallel)
                        seg_reg_limit = 0
                    else:
                        seg_prio_limit = max(0, config.segment_priority_parallel_throttled)
                        seg_reg_limit = max(0, config.segment_regular_parallel_throttled)
                else:
                    seg_prio_limit = max(0, config.segment_priority_parallel_idle)
                    seg_reg_limit = max(0, config.segment_regular_parallel_idle)
                seg_total_limit = seg_prio_limit + seg_reg_limit
                seg_total_limit = _segment_task_limit_after_import(running, root, seg_total_limit)
                eff_seg_prio_limit = seg_prio_limit
                eff_seg_reg_limit = seg_reg_limit
                prefer_priority_spill = False
                if (
                    not throttled.get(root, False)
                    and seg_prio_limit > 0
                    and seg_reg_limit > 0
                    and seg_prio_set
                ):
                    prio_backlog = _ring_has_launchable(
                        seg_prio_ring,
                        running=running,
                        root=root,
                        task_type="segment",
                    )
                    stuck_prio = _running_entities_stuck_for(
                        running,
                        root=root,
                        task_type="segment",
                        entity_ids=seg_prio_set,
                        now_ts=now,
                        stuck_sec=_SEGMENT_STUCK_SPILLOVER_SEC,
                    )
                    if prio_backlog and stuck_prio > 0:
                        # Keep 3+1 behavior as default, but when priority is
                        # blocked by long-running tasks we temporarily borrow
                        # regular launch slot for priority backlog.
                        eff_seg_prio_limit = min(seg_total_limit, seg_prio_limit + seg_reg_limit)
                        eff_seg_reg_limit = 0
                        prefer_priority_spill = True
                        logging.warning(
                            "[%s] segment priority spillover active: stuck_prio=%s limits=%s+%s -> %s+%s",
                            root,
                            stuck_prio,
                            seg_prio_limit,
                            seg_reg_limit,
                            eff_seg_prio_limit,
                            eff_seg_reg_limit,
                        )
                seg_resume_ring = segment_resume_rings.setdefault(root, deque())

                if throttled.get(root, False) and config.segment_throttle_whitelist_only and config.segment_throttle_kill_non_whitelist:
                    for key, task in list(running.items()):
                        if task.root != root or task.task_type != "segment":
                            continue
                        if task.entity_id is None or task.entity_id in segment_whitelist_for_inst:
                            continue
                        _kill_pid(task.pid, config.segment_kill_grace_sec)
                        store.finish(task.row_id, state="timeout", rc=None, note="throttle_kill")
                        running.pop(key, None)
                        popens.pop(key, None)
                        if task.entity_id not in seg_resume_ring:
                            seg_resume_ring.appendleft(task.entity_id)

                if not throttled.get(root, False) and seg_resume_ring:
                    _fill_from_ring(
                        ring=seg_resume_ring,
                        ring_limit=seg_total_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=None,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                        blocked_entities=segment_blocked_ids,
                    )

                if throttled.get(root, False) and config.segment_throttle_whitelist_only:
                    wl_ids = list(
                        dict.fromkeys(
                            [x for x in list(seg_prio_ring) + list(seg_reg_ring) if x in segment_whitelist_for_inst]
                        )
                    )
                    seg_wl_ring = deque(wl_ids)
                    _fill_from_ring(
                        ring=seg_wl_ring,
                        ring_limit=seg_prio_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=segment_whitelist_for_inst,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                        blocked_entities=segment_blocked_ids,
                    )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total >= seg_total_limit:
                        pass
                    else:
                        # No non-whitelist launches while throttle is active.
                        pass
                else:
                    _fill_from_ring(
                        ring=seg_prio_ring,
                        ring_limit=eff_seg_prio_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=seg_prio_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                        blocked_entities=segment_blocked_ids,
                    )
                    _fill_from_ring(
                        ring=seg_reg_ring,
                        ring_limit=eff_seg_reg_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=seg_reg_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                        blocked_entities=segment_blocked_ids,
                    )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total < seg_total_limit:
                        spill = seg_total_limit - seg_cur_total
                        if prefer_priority_spill and seg_prio_ring and spill > 0:
                            _fill_from_ring(
                                ring=seg_prio_ring,
                                ring_limit=eff_seg_prio_limit,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_prio_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                                blocked_entities=segment_blocked_ids,
                            )
                        elif seg_reg_ring:
                            _fill_from_ring(
                                ring=seg_reg_ring,
                                ring_limit=spill,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_reg_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                                blocked_entities=segment_blocked_ids,
                            )
                        elif seg_prio_ring and spill > 0:
                            # If regular ring is empty, keep total segment concurrency at target
                            # by temporarily borrowing regular slot(s) for priority ring.
                            _fill_from_ring(
                                ring=seg_prio_ring,
                                ring_limit=seg_prio_limit + spill,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_prio_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                                blocked_entities=segment_blocked_ids,
                            )

            # `mautic:campaigns:update` is treated as synonym of
            # `mautic:campaigns:rebuild` and is not scheduled separately.
            # This avoids duplicate campaign pre-processing passes.
            if cluster_cron_allowed and (config.profile_name or "").strip().lower() == "tiny":
                if _running_campaign_total(running, root) == 0:
                    next_trigger_id = None
                    if trg_prio_ring:
                        next_trigger_id = trg_prio_ring[0]
                        trg_prio_ring.rotate(-1)
                    elif trg_reg_ring:
                        next_trigger_id = trg_reg_ring[0]
                        trg_reg_ring.rotate(-1)

                    if next_trigger_id is not None:
                        launched = _submit_if_slot(
                            config=config,
                            store=store,
                            running=running,
                            root=root,
                            task_type="campaign_trigger",
                            entity_id=next_trigger_id,
                            args=render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_trigger_template,
                                id=next_trigger_id,
                                campaign_limit=config.campaign_limit,
                                batch_limit=config.campaign_batch_limit,
                            ),
                            timeout_sec=config.command_timeout_sec,
                            max_parallel_for_type=1,
                            popens=popens,
                        )
                        if launched:
                            _advance_ring_after_launch(trg_prio_ring, next_trigger_id, remove_on_launch=True)
                            _advance_ring_after_launch(trg_reg_ring, next_trigger_id, remove_on_launch=True)
                    else:
                        next_campaign_id = None
                        if reb_prio_ring:
                            next_campaign_id = reb_prio_ring[0]
                            reb_prio_ring.rotate(-1)
                        elif reb_reg_ring:
                            next_campaign_id = reb_reg_ring[0]
                            reb_reg_ring.rotate(-1)
                        if next_campaign_id is not None:
                            launched = _submit_if_slot(
                                config=config,
                                store=store,
                                running=running,
                                root=root,
                                task_type="campaign_rebuild",
                                entity_id=next_campaign_id,
                                args=render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_campaign_rebuild_template,
                                    id=next_campaign_id,
                                ),
                                timeout_sec=config.command_timeout_sec,
                                max_parallel_for_type=1,
                                popens=popens,
                            )
                            if launched:
                                _advance_ring_after_launch(reb_prio_ring, next_campaign_id, remove_on_launch=True)
                                _advance_ring_after_launch(reb_reg_ring, next_campaign_id, remove_on_launch=True)
            # Tiny mode has a single campaign worker:
            # - trigger-due campaigns first
            # - then rebuild-due campaigns
            # Import polling is dispatched above through the shared segment slot.
            monitored_email_setting = _monitored_email_parser_effective_setting(config, inst)
            last_monitored_email = last_monitored_email_parser_ts.get(root, 0.0)
            if (
                monitored_email_setting.enabled
                and cluster_cron_allowed
                and (last_monitored_email <= 0 or now - last_monitored_email >= monitored_email_setting.interval_sec)
            ):
                if not getattr(inst, "db", None):
                    logging.warning("[%s] monitored_email parser skipped: db config missing", root)
                    last_monitored_email_parser_ts[root] = now
                else:
                    try:
                        state_key = monitored_email_state_key(root, monitored_email_setting)
                        state = store.get_runtime_sync(state_key) or {}
                        result = process_monitored_email(
                            db=db,
                            local_php_path=getattr(inst, "local_php_path", None),
                            php_bin=config.php_bin,
                            settings=monitored_email_setting,
                            state=state,
                        )
                        store.put_runtime_sync(state_key, result.state)
                        if result.scanned or result.dnc_added or result.deleted or result.whitelist_dnc_removed or result.errors:
                            logging.info(
                                "[%s] monitored_email parser scanned=%s matched=%s contacts=%s dnc_added=%s dnc_existing=%s whitelist_dnc_removed=%s deleted=%s marked_seen=%s no_contact=%s types=%s errors=%s",
                                root,
                                result.scanned,
                                result.matched,
                                result.contacts_matched,
                                result.dnc_added,
                                result.dnc_existing,
                                result.whitelist_dnc_removed,
                                result.deleted,
                                result.marked_seen,
                                result.no_contact,
                                ",".join(f"{k}:{v}" for k, v in sorted(result.by_type.items())) or "-",
                                " | ".join(result.errors[:5]) if result.errors else "-",
                            )
                    except Exception as e:
                        logging.warning("[%s] monitored_email parser failed: %s", root, e)
                    last_monitored_email_parser_ts[root] = now

            if (config.profile_name or "").strip().lower() == "tiny":
                # Skip generic multi-ring campaign scheduler.
                continue

            shared_campaign_cap = max(0, config.campaign_total_parallel) if cluster_cron_allowed else 0
            rr = campaign_round_robin.get(root, 0)
            trigger_lane_configured = (
                max(0, int(config.campaign_trigger_priority_parallel))
                + max(0, int(config.campaign_trigger_regular_parallel))
            ) > 0
            prefer_rebuild = (
                shared_campaign_cap > 0
                and config.enable_campaign_rebuild
                and (rr % 2 == 1)
            )
            trg_prio_limit = max(0, config.campaign_trigger_priority_parallel) if cluster_cron_allowed else 0
            trg_reg_limit = max(0, config.campaign_trigger_regular_parallel) if cluster_cron_allowed else 0
            trg_total_limit = trg_prio_limit + trg_reg_limit
            if shared_campaign_cap > 0:
                rem = max(0, shared_campaign_cap - _running_campaign_total(running, root))
                if prefer_rebuild:
                    rem = 0
                trg_total_limit = min(trg_total_limit, rem)
                trg_prio_limit = min(trg_prio_limit, trg_total_limit)
                trg_reg_limit = min(trg_reg_limit, max(0, trg_total_limit - trg_prio_limit))
            _fill_from_ring(
                ring=trg_prio_ring,
                ring_limit=trg_prio_limit,
                total_limit=trg_total_limit,
                root=root,
                task_type="campaign_trigger",
                running=running,
                ring_entities=trg_prio_set,
                config=config,
                store=store,
                popens=popens,
                build_args=lambda cid: render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_campaign_trigger_template,
                    id=cid,
                    campaign_limit=config.campaign_limit,
                    batch_limit=config.campaign_batch_limit,
                ),
                remove_on_launch=True,
            )
            _fill_from_ring(
                ring=trg_reg_ring,
                ring_limit=trg_reg_limit,
                total_limit=trg_total_limit,
                root=root,
                task_type="campaign_trigger",
                running=running,
                ring_entities=trg_reg_set,
                config=config,
                store=store,
                popens=popens,
                build_args=lambda cid: render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_campaign_trigger_template,
                    id=cid,
                    campaign_limit=config.campaign_limit,
                    batch_limit=config.campaign_batch_limit,
                ),
                remove_on_launch=True,
            )
            trg_cur_total = _running_count(running, root, "campaign_trigger")
            if trg_cur_total < trg_total_limit:
                spill = trg_total_limit - trg_cur_total
                if trg_reg_ring:
                    _fill_from_ring(
                        ring=trg_reg_ring,
                        ring_limit=spill,
                        total_limit=trg_total_limit,
                        root=root,
                        task_type="campaign_trigger",
                        running=running,
                        ring_entities=trg_reg_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda cid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_campaign_trigger_template,
                            id=cid,
                            campaign_limit=config.campaign_limit,
                            batch_limit=config.campaign_batch_limit,
                        ),
                        remove_on_launch=True,
                    )
                elif trg_prio_ring:
                    _fill_from_ring(
                        ring=trg_prio_ring,
                        ring_limit=spill,
                        total_limit=trg_total_limit,
                        root=root,
                        task_type="campaign_trigger",
                        running=running,
                        ring_entities=trg_prio_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda cid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_campaign_trigger_template,
                            id=cid,
                            campaign_limit=config.campaign_limit,
                            batch_limit=config.campaign_batch_limit,
                        ),
                        remove_on_launch=True,
                    )
            if cluster_cron_allowed and config.enable_campaign_rebuild:
                rebuild_prio_limit = max(0, config.campaign_rebuild_priority_parallel)
                rebuild_reg_limit = max(0, config.campaign_rebuild_regular_parallel)
                rebuild_total_limit = rebuild_prio_limit + rebuild_reg_limit
                if shared_campaign_cap > 0:
                    rem = max(0, shared_campaign_cap - _running_campaign_total(running, root))
                    if (not prefer_rebuild) and (trg_prio_limit + trg_reg_limit) > 0:
                        rem = 0
                    rebuild_total_limit = min(rebuild_total_limit, rem)
                    rebuild_prio_limit = min(rebuild_prio_limit, rebuild_total_limit)
                    rebuild_reg_limit = min(rebuild_reg_limit, max(0, rebuild_total_limit - rebuild_prio_limit))
                _fill_from_ring(
                    ring=reb_prio_ring,
                    ring_limit=rebuild_prio_limit,
                    total_limit=rebuild_total_limit,
                    root=root,
                    task_type="campaign_rebuild",
                    running=running,
                    ring_entities=reb_prio_set,
                    config=config,
                    store=store,
                    popens=popens,
                    build_args=lambda cid: render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=config.cmd_campaign_rebuild_template,
                        id=cid,
                    ),
                    remove_on_launch=True,
                )
                _fill_from_ring(
                    ring=reb_reg_ring,
                    ring_limit=rebuild_reg_limit,
                    total_limit=rebuild_total_limit,
                    root=root,
                    task_type="campaign_rebuild",
                    running=running,
                    ring_entities=reb_reg_set,
                    config=config,
                    store=store,
                    popens=popens,
                    build_args=lambda cid: render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=config.cmd_campaign_rebuild_template,
                        id=cid,
                    ),
                    remove_on_launch=True,
                )
                reb_cur_total = _running_count(running, root, "campaign_rebuild")
                if reb_cur_total < rebuild_total_limit:
                    spill = rebuild_total_limit - reb_cur_total
                    if reb_reg_ring:
                        _fill_from_ring(
                            ring=reb_reg_ring,
                            ring_limit=spill,
                            total_limit=rebuild_total_limit,
                            root=root,
                            task_type="campaign_rebuild",
                            running=running,
                            ring_entities=reb_reg_set,
                            config=config,
                            store=store,
                            popens=popens,
                            build_args=lambda cid: render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_rebuild_template,
                                id=cid,
                            ),
                            remove_on_launch=True,
                        )

                    elif reb_prio_ring:
                        _fill_from_ring(
                            ring=reb_prio_ring,
                            ring_limit=spill,
                            total_limit=rebuild_total_limit,
                            root=root,
                            task_type="campaign_rebuild",
                            running=running,
                            ring_entities=reb_prio_set,
                            config=config,
                            store=store,
                            popens=popens,
                            build_args=lambda cid: render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_rebuild_template,
                                id=cid,
                            ),
                            remove_on_launch=True,
                        )

            if shared_campaign_cap > 0 and config.enable_campaign_rebuild and trigger_lane_configured:
                campaign_round_robin[root] = rr + 1

            last_cleanup = last_cleanup_ts.get(root, 0.0)
            interval = max(1, config.contacts_cleanup_interval_sec)
            if inst.mautic_timezone:
                try:
                    dt = datetime.now(ZoneInfo(inst.mautic_timezone))
                except Exception:
                    logging.warning("[%s] invalid mautic timezone in local.php: %s", root, inst.mautic_timezone)
                    dt = datetime.now()
            else:
                dt = datetime.now()
            in_quiet = dt.hour == max(0, min(23, config.contacts_cleanup_quiet_hour)) and dt.minute < max(
                1, min(180, config.contacts_cleanup_quiet_window_min)
            )
            if cluster_cron_allowed and config.enable_contacts_cleanup and in_quiet and (last_cleanup == 0.0 or now - last_cleanup >= interval):
                try:
                    before = db.count_contacts_without_comm(
                        email_field=config.contacts_cleanup_email_field,
                        phone_field=config.contacts_cleanup_phone_field,
                        mode=config.contacts_cleanup_mode,
                    )
                    deleted = db.delete_contacts_without_comm(
                        email_field=config.contacts_cleanup_email_field,
                        phone_field=config.contacts_cleanup_phone_field,
                        mode=config.contacts_cleanup_mode,
                        max_delete=config.contacts_cleanup_max_delete_per_run,
                    )
                    logging.info("[%s] contacts_cleanup ok before=%s deleted=%s", root, before, deleted)
                except Exception as e:
                    logging.warning("[%s] contacts_cleanup failed: %s", root, e)
                last_cleanup_ts[root] = now

            last_mautic_lock_cleanup = last_mautic_lock_cleanup_ts.get(root, 0.0)
            mautic_lock_cleanup_interval = max(300, int(config.mautic_lock_cleanup_interval_sec or 3600))
            mautic_lock_cleanup_in_quiet = _in_daily_quiet_window(
                dt,
                max(0, min(23, int(config.mautic_lock_cleanup_quiet_hour or 0))),
                max(1, min(1440, int(config.mautic_lock_cleanup_quiet_window_min or 1440))),
            )
            mautic_lock_cleanup_backup_running = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(
                config
            )
            mautic_lock_cleanup_backup_pause, mautic_lock_cleanup_backup_reason = _backup_dispatch_pause_state(
                config,
                backup_running=mautic_lock_cleanup_backup_running,
                now_local=dt,
            )
            if (
                config.enable_mautic_lock_cleanup
                and cluster_cron_allowed
                and mautic_lock_cleanup_in_quiet
                and not mautic_lock_cleanup_backup_pause
                and (last_mautic_lock_cleanup == 0.0 or now - last_mautic_lock_cleanup >= mautic_lock_cleanup_interval)
            ):
                cutoff_utc = (
                    now_utc - timedelta(seconds=max(1800, int(config.mautic_lock_cleanup_min_age_sec or 21600)))
                ).strftime("%Y-%m-%d %H:%M:%S")
                skip_segment_ids = {
                    int(task.entity_id)
                    for task in running.values()
                    if task.root == root and task.entity_id is not None and task.task_type in {"segment", "segment_sql"}
                }
                skip_campaign_ids = {
                    int(task.entity_id)
                    for task in running.values()
                    if task.root == root
                    and task.entity_id is not None
                    and task.task_type in {"campaign_update", "campaign_rebuild", "campaign_trigger"}
                }
                try:
                    lock_res = db.cleanup_stale_checked_out_locks(
                        cutoff_utc=cutoff_utc,
                        max_rows=config.mautic_lock_cleanup_max_rows_per_run,
                        skip_segment_ids=skip_segment_ids,
                        skip_campaign_ids=skip_campaign_ids,
                    )
                    seg_rows = lock_res.get("segments") if isinstance(lock_res, dict) else []
                    camp_rows = lock_res.get("campaigns") if isinstance(lock_res, dict) else []
                    seg_rows = seg_rows if isinstance(seg_rows, list) else []
                    camp_rows = camp_rows if isinstance(camp_rows, list) else []
                    cleared_segments = int((lock_res or {}).get("cleared_segments", 0) or 0)
                    cleared_campaigns = int((lock_res or {}).get("cleared_campaigns", 0) or 0)
                    if cleared_segments > 0 or cleared_campaigns > 0:
                        logging.warning(
                            "[%s] mautic_lock_cleanup cleared segments=%s campaigns=%s cutoff_utc=%s segment_ids=%s campaign_ids=%s",
                            root,
                            cleared_segments,
                            cleared_campaigns,
                            cutoff_utc,
                            ",".join(str(int(row.get("id") or 0)) for row in seg_rows if int(row.get("id") or 0) > 0) or "-",
                            ",".join(str(int(row.get("id") or 0)) for row in camp_rows if int(row.get("id") or 0) > 0) or "-",
                        )
                    else:
                        logging.debug("[%s] mautic_lock_cleanup idle cutoff_utc=%s", root, cutoff_utc)
                except Exception as e:
                    logging.warning("[%s] mautic_lock_cleanup failed: %s", root, e)
                last_mautic_lock_cleanup_ts[root] = now
            elif (
                config.enable_mautic_lock_cleanup
                and cluster_cron_allowed
                and mautic_lock_cleanup_in_quiet
                and mautic_lock_cleanup_backup_pause
                and (last_mautic_lock_cleanup == 0.0 or now - last_mautic_lock_cleanup >= mautic_lock_cleanup_interval)
            ):
                logging.info(
                    "[%s] mautic_lock_cleanup skipped: backup guard active (%s)",
                    root,
                    mautic_lock_cleanup_backup_reason or "backup_guard",
                )

            (
                page_hits_cleanup_enabled,
                page_hits_cleanup_interval,
                page_hits_cleanup_schedule_type,
                page_hits_cleanup_cron_expr,
                page_hits_cleanup_window_min,
                page_hits_cleanup_window_start,
                page_hits_cleanup_window_end,
                page_hits_cleanup_batch_size,
                page_hits_cleanup_batches,
                page_hits_cleanup_sleep_sec,
                page_hits_cleanup_grace_min,
                page_hits_cleanup_max_run_sec,
            ) = _page_hits_orphan_cleanup_effective_setting(config, inst)
            last_page_hits_cleanup = last_page_hits_orphan_cleanup_ts.get(root, 0.0)
            page_hits_cleanup_backup_running = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(
                config
            )
            page_hits_cleanup_backup_pause, page_hits_cleanup_backup_reason = _backup_dispatch_pause_state(
                config,
                backup_running=page_hits_cleanup_backup_running,
                now_local=dt,
            )
            page_hits_cleanup_in_window, page_hits_cleanup_window_key = _cleanup_session_key(
                schedule_type=page_hits_cleanup_schedule_type,
                now_local=datetime.now(),
                now_epoch=now,
                interval_sec=page_hits_cleanup_interval,
                cron_expr=page_hits_cleanup_cron_expr,
                window_min=page_hits_cleanup_window_min,
                window_start=page_hits_cleanup_window_start,
                window_end=page_hits_cleanup_window_end,
            )
            page_hits_cleanup_due = (
                bool(page_hits_cleanup_window_key)
                and page_hits_cleanup_in_window
                and (last_page_hits_cleanup <= 0 or now - last_page_hits_cleanup >= max(1.0, float(config.dispatch_interval_sec)))
            )
            if page_hits_cleanup_window_key:
                if page_hits_orphan_cleanup_window_keys.get(root) != page_hits_cleanup_window_key:
                    page_hits_orphan_cleanup_window_keys[root] = page_hits_cleanup_window_key
                    page_hits_orphan_cleanup_window_counts[root] = 0
                if page_hits_orphan_cleanup_done_window_keys.get(root) == page_hits_cleanup_window_key:
                    page_hits_cleanup_due = False
                if (
                    page_hits_cleanup_batches > 0
                    and page_hits_orphan_cleanup_window_counts.get(root, 0) >= page_hits_cleanup_batches
                ):
                    page_hits_cleanup_due = False
            page_hits_cleanup_due = (
                page_hits_cleanup_enabled
                and cluster_cron_allowed
                and page_hits_cleanup_due
                and not page_hits_cleanup_backup_pause
            )
            page_hits_cleanup_cutoff_utc = (
                now_utc - timedelta(minutes=page_hits_cleanup_grace_min)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if (
                page_hits_cleanup_enabled
                and cluster_cron_allowed
                and page_hits_cleanup_in_window
                and page_hits_cleanup_backup_pause
                and bool(page_hits_cleanup_window_key)
            ):
                logging.info(
                    "[%s] page_hits_orphan_cleanup skipped: backup guard active (%s)",
                    root,
                    page_hits_cleanup_backup_reason or "backup_guard",
                )

            (
                housekeeping_enabled,
                housekeeping_interval,
                housekeeping_quiet_hour,
                housekeeping_window_min,
                housekeeping_days_old,
                housekeeping_flags,
                housekeeping_optimize,
                housekeeping_dry_run,
            ) = _housekeeping_plugin_effective_setting(config, inst)
            last_housekeeping = last_housekeeping_plugin_ts.get(root, 0.0)
            housekeeping_in_quiet = _in_daily_quiet_window(
                dt,
                housekeeping_quiet_hour,
                housekeeping_window_min,
            )
            housekeeping_due = (
                housekeeping_enabled
                and cluster_cron_allowed
                and housekeeping_in_quiet
                and (last_housekeeping == 0.0 or now - last_housekeeping >= housekeeping_interval)
                and int(getattr(inst, "mautic_major", 0) or 0) in {4, 5, 6, 7}
                and _housekeeping_plugin_installed(root)
            )
            housekeeping_selected_flags = [
                _HOUSEKEEPING_ALLOWED_FLAGS[x]
                for x in housekeeping_flags
                if x in _HOUSEKEEPING_ALLOWED_FLAGS
            ]
            housekeeping_due = bool(housekeeping_due and housekeeping_selected_flags)

            (
                empty_cleanup_enabled,
                empty_cleanup_interval,
                empty_cleanup_mode,
                empty_cleanup_schedule_type,
                empty_cleanup_cron_expr,
                empty_cleanup_batch_size,
                empty_cleanup_window_min,
                empty_cleanup_window_start,
                empty_cleanup_window_end,
                empty_cleanup_max_runs_per_window,
            ) = _empty_leads_cleanup_effective_setting(
                config,
                inst,
            )
            last_empty_cleanup = last_empty_leads_cleanup_ts.get(root, 0.0)
            empty_cleanup_due = False
            empty_cleanup_in_window = False
            empty_cleanup_window_key = ""
            empty_cleanup_in_window, empty_cleanup_window_key = _cleanup_session_key(
                schedule_type=empty_cleanup_schedule_type,
                now_local=datetime.now(),
                now_epoch=now,
                interval_sec=empty_cleanup_interval,
                cron_expr=empty_cleanup_cron_expr,
                window_min=empty_cleanup_window_min,
                window_start=empty_cleanup_window_start,
                window_end=empty_cleanup_window_end,
            )

            # All cleanup schedule modes share the same drain-loop semantics:
            # a schedule occurrence opens a cleanup session, MCD runs one SQL
            # batch per dispatch tick, and the session stops only when a pass
            # deletes zero rows or the configured repeat limit is reached.
            empty_cleanup_due = (
                bool(empty_cleanup_window_key)
                and empty_cleanup_in_window
                and (last_empty_cleanup <= 0 or now - last_empty_cleanup >= max(1.0, float(config.dispatch_interval_sec)))
            )
            if empty_cleanup_window_key:
                if empty_leads_cleanup_window_keys.get(root) != empty_cleanup_window_key:
                    empty_leads_cleanup_window_keys[root] = empty_cleanup_window_key
                    empty_leads_cleanup_window_counts[root] = 0
                if empty_leads_cleanup_done_window_keys.get(root) == empty_cleanup_window_key:
                    empty_cleanup_due = False
                if (
                    empty_cleanup_max_runs_per_window > 0
                    and empty_leads_cleanup_window_counts.get(root, 0) >= empty_cleanup_max_runs_per_window
                ):
                    empty_cleanup_due = False
            service_cleanup_candidates: list[str] = []
            if page_hits_cleanup_due:
                service_cleanup_candidates.append("page_hits_orphan_cleanup")
            if (
                empty_cleanup_enabled
                and cluster_cron_allowed
                and empty_cleanup_due
                and not backup_dispatch_pause
            ):
                service_cleanup_candidates.append("empty_leads_cleanup")
            if housekeeping_due:
                service_cleanup_candidates.append("housekeeping")
            selected_service_cleanup, selected_service_cleanup_idx = _select_fair_cleanup_task(
                service_cleanup_candidates,
                maintenance_cleanup_rr_index.get(root, -1),
            )
            if selected_service_cleanup:
                maintenance_cleanup_rr_index[root] = selected_service_cleanup_idx

            if selected_service_cleanup == "housekeeping":
                template = "leuchtfeuer:housekeeping --days-old {days_old} " + " ".join(housekeeping_selected_flags)
                if housekeeping_optimize:
                    template += " --optimize-tables"
                if housekeeping_dry_run:
                    template += " --dry-run"
                try:
                    args = render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=template,
                        days_old=housekeeping_days_old,
                    )
                    launched = _submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=root,
                        task_type="housekeeping",
                        entity_id=None,
                        args=args,
                        timeout_sec=config.command_timeout_sec,
                        max_parallel_for_type=1,
                        popens=popens,
                    )
                    if launched:
                        last_housekeeping_plugin_ts[root] = now
                        logging.info(
                            "[%s] housekeeping plugin queued fair_queue=%s flags=%s",
                            root,
                            "shared" if len(service_cleanup_candidates) > 1 else "solo",
                            ",".join(housekeeping_selected_flags),
                        )
                except Exception as e:
                    logging.warning("[%s] housekeeping plugin schedule failed: %s", root, e)

            if selected_service_cleanup == "page_hits_orphan_cleanup":
                try:
                    preview = db.preview_orphan_page_hits_batch(
                        cutoff_utc=page_hits_cleanup_cutoff_utc,
                        batch_size=page_hits_cleanup_batch_size,
                    )
                    preview_count = int(preview.get("preview_count", 0) or 0)
                    if preview_count > 0:
                        logging.info(
                            "[%s] page_hits_orphan_cleanup preview count=%s min_id=%s max_id=%s min_date_hit=%s max_date_hit=%s cutoff_utc=%s fair_queue=%s",
                            root,
                            preview_count,
                            preview.get("min_id"),
                            preview.get("max_id"),
                            preview.get("min_date_hit"),
                            preview.get("max_date_hit"),
                            page_hits_cleanup_cutoff_utc,
                            "shared" if len(service_cleanup_candidates) > 1 else "solo",
                        )
                        result = db.delete_orphan_page_hits(
                            cutoff_utc=page_hits_cleanup_cutoff_utc,
                            batch_size=page_hits_cleanup_batch_size,
                            max_batches=1,
                            sleep_sec=page_hits_cleanup_sleep_sec,
                            max_run_sec=page_hits_cleanup_max_run_sec,
                        )
                        last_deleted = int(result.get("last_deleted", 0) or 0)
                        total_deleted = int(result.get("total_deleted", 0) or 0)
                        stop_reason = str(result.get("stop_reason") or "-")
                        page_hits_orphan_cleanup_active[root] = bool(total_deleted > 0 and last_deleted > 0 and stop_reason != "empty")
                        logging.info(
                            "[%s] page_hits_orphan_cleanup ok batches=%s deleted=%s last_deleted=%s elapsed=%.2fs stop=%s cutoff_utc=%s fair_queue=%s",
                            root,
                            int(result.get("batches_run", 0) or 0),
                            total_deleted,
                            last_deleted,
                            float(result.get("elapsed_sec", 0.0) or 0.0),
                            stop_reason,
                            page_hits_cleanup_cutoff_utc,
                            "shared" if len(service_cleanup_candidates) > 1 else "solo",
                        )
                    else:
                        page_hits_orphan_cleanup_active[root] = False
                        logging.debug("[%s] page_hits_orphan_cleanup idle cutoff_utc=%s", root, page_hits_cleanup_cutoff_utc)

                    message_result = db.delete_orphan_page_hit_notifications(
                        cutoff_utc=page_hits_cleanup_cutoff_utc,
                        batch_size=page_hits_cleanup_batch_size,
                    )
                    message_deleted = int(message_result.get("deleted", 0) or 0)
                    if message_deleted > 0:
                        page_hits_orphan_cleanup_active[root] = True
                        logging.info(
                            "[%s] page_hit_notification_cleanup ok table=%s scanned=%s deleted=%s orphan_hits=%s invalid_hits=%s cutoff_utc=%s",
                            root,
                            message_result.get("table", "-"),
                            int(message_result.get("scanned", 0) or 0),
                            message_deleted,
                            int(message_result.get("orphan_hits", 0) or 0),
                            int(message_result.get("invalid_hits", 0) or 0),
                            page_hits_cleanup_cutoff_utc,
                        )
                    elif preview_count <= 0 and page_hits_cleanup_window_key:
                        page_hits_orphan_cleanup_done_window_keys[root] = page_hits_cleanup_window_key
                except Exception as e:
                    page_hits_orphan_cleanup_active[root] = False
                    logging.warning("[%s] page_hits_orphan_cleanup failed: %s", root, e)
                last_page_hits_orphan_cleanup_ts[root] = now
                if page_hits_cleanup_window_key:
                    page_hits_orphan_cleanup_window_counts[root] = page_hits_orphan_cleanup_window_counts.get(root, 0) + 1

            if selected_service_cleanup == "empty_leads_cleanup":
                try:
                    result = db.delete_empty_leads(
                        mode=empty_cleanup_mode,
                        batch_size=empty_cleanup_batch_size,
                        max_batches=1,
                    )
                    deleted = int(result.get("total_deleted", 0) or 0)
                    if deleted > 0:
                        last_empty_leads_cleanup_idle_ts[root] = 0.0
                        logging.info(
                            "[%s] empty_leads_cleanup ok mode=%s batches=%s batch_size=%s deleted=%s elapsed=%.2fs stop=%s",
                            root,
                            str(result.get("mode") or empty_cleanup_mode),
                            int(result.get("batches_run", 0) or 0),
                            int(result.get("batch_size", 0) or empty_cleanup_batch_size),
                            deleted,
                            float(result.get("elapsed_sec", 0.0) or 0.0),
                            str(result.get("stop_reason") or "-"),
                        )
                    else:
                        last_empty_leads_cleanup_idle_ts[root] = now
                        if empty_cleanup_window_key:
                            empty_leads_cleanup_done_window_keys[root] = empty_cleanup_window_key
                        logging.debug("[%s] empty_leads_cleanup idle mode=%s", root, empty_cleanup_mode)
                except Exception as e:
                    logging.warning("[%s] empty_leads_cleanup failed: %s", root, e)
                last_empty_leads_cleanup_ts[root] = now
                if empty_cleanup_window_key:
                    empty_leads_cleanup_window_counts[root] = empty_leads_cleanup_window_counts.get(root, 0) + 1
            elif (
                empty_cleanup_enabled
                and cluster_cron_allowed
                and empty_cleanup_due
                and backup_dispatch_pause
                and now - last_empty_leads_cleanup_skip_ts.get(root, 0.0) >= 300
            ):
                logging.info(
                    "[%s] empty_leads_cleanup skipped: backup guard active (%s)",
                    root,
                    backup_pause_reason or "backup_guard",
                )
                last_empty_leads_cleanup_skip_ts[root] = now

            last_cache_clear = last_cache_clear_ts.get(root, 0.0)
            if (
                config.enable_cache_clear
                and cluster_cache_allowed
                and dt.hour == max(0, min(23, config.cache_clear_quiet_hour))
                and dt.minute < max(1, min(180, config.cache_clear_quiet_window_min))
                and (last_cache_clear == 0.0 or now - last_cache_clear >= max(1, config.cache_clear_interval_sec))
            ):
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_cache_clear_template,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="cache_clear",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                ):
                    last_cache_clear_ts[root] = now

            last_cache_warm = last_cache_warm_ts.get(root, 0.0)
            if (
                config.enable_cache_warm
                and cluster_cache_allowed
                and dt.hour == max(0, min(23, config.cache_warm_quiet_hour))
                and dt.minute < max(1, min(180, config.cache_warm_quiet_window_min))
                and (last_cache_warm == 0.0 or now - last_cache_warm >= max(1, config.cache_warm_interval_sec))
            ):
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_cache_warm_template,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="cache_warm",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                ):
                    last_cache_warm_ts[root] = now

            if cluster_cron_allowed and _instance_has_viber_plugin(inst):
                viber_enabled, viber_interval = _viber_stats_effective_setting(config, inst)
                viber_job_name = "viber:stats:update"
                prev = jobs_last_run.get((root, viber_job_name), 0.0)
                if viber_enabled and (prev <= 0 or now - prev >= max(60, viber_interval)):
                    args = render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=viber_job_name,
                    )
                    if _submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=root,
                        task_type="job:viber_stats_update",
                        entity_id=None,
                        args=args,
                        timeout_sec=config.command_timeout_sec,
                        max_parallel_for_type=1,
                        popens=popens,
                    ):
                        jobs_last_run[(root, viber_job_name)] = now

            for job in config.scheduled_jobs:
                if not cluster_cron_allowed:
                    break
                if not job.enabled:
                    continue
                if _is_mautic_email_fetch_template(job.command_template) and _monitored_email_fetch_replaces_mautic(config, inst):
                    continue
                prev = jobs_last_run.get((root, job.name), 0.0)
                if prev > 0 and time.time() - prev < max(1, job.interval_sec):
                    continue
                if job.quiet_hour is not None:
                    if inst.mautic_timezone:
                        try:
                            dt_job = datetime.now(ZoneInfo(inst.mautic_timezone))
                        except Exception:
                            logging.warning("[%s] invalid mautic timezone in local.php: %s", root, inst.mautic_timezone)
                            dt_job = datetime.now()
                    else:
                        dt_job = datetime.now()
                    if not (
                        dt_job.hour == max(0, min(23, int(job.quiet_hour)))
                        and dt_job.minute < max(1, min(180, int(job.quiet_window_min)))
                    ):
                        continue
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=job.command_template,
                    batch_limit=config.segment_batch_limit,
                    campaign_limit=config.campaign_limit,
                    import_limit=config.import_limit,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type=f"job:{job.name}",
                    entity_id=None,
                    args=args,
                    timeout_sec=job.timeout_sec if job.timeout_sec else config.command_timeout_sec,
                    max_parallel_for_type=max(1, config.jobs_max_workers),
                    popens=popens,
                ):
                    jobs_last_run[(root, job.name)] = time.time()

        if single_cycle:
            return
        time.sleep(max(0.1, float(config.dispatch_interval_sec)))
