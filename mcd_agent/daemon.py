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
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX development environments
    fcntl = None  # type: ignore[assignment]

from mcd_agent.config import (
    AgentConfig,
    DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN,
    check_profile_drift_with_mcc,
    load_config,
    persist_profile_authority,
    reapply_profile_authority,
    recover_config_from_mcc,
    remove_runtime_values,
    runtime_effective_map,
    upsert_runtime_values,
)
from mcd_agent.hardware_profile import write_profile_selection
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
from mcd_agent.email_counters import repair_campaign_email_counters
from mcd_agent.executor import render_mautic_command
from mcd_agent.native_comparator import shared_root_fence_path
from mcd_agent.fail2ban_guard import ensure_nginx_4xx_scan_safety
from mcd_agent.security_blocklist import sync_security_blocklist_once
from mcd_agent.web_ingress_firewall import ensure_managed_web_firewall_loopback
from mcd_agent.fs_permissions import effective_guard_identity, ensure_instance_permissions
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.instance_runtime import apply_instance_runtime
from mcd_agent.mautic6_core_patch import ensure_m6_plugin_update_metadata_patch, should_apply_m6_plugin_update_metadata_patch
from mcd_agent.mautic713_import_tag_patch import reconcile_import_tag_patch
from mcd_agent.grapesjs_ckeditor_patch import ensure_grapesjs_ckeditor_gpl_patch
from mcd_agent.mautic_locks import cleanup_stale_mautic_file_locks
from mcd_agent.mautic_version_cache import confirmed_mautic_major, retire_zabbix_mautic_version_userparameter
from mcd_agent.mautic_core_restore import restore_retired_mcd_core_patches
from mcd_agent.logical_issues import (
    logical_issue_blocked_segment_ids,
    logical_issue_terminal_segment_notes,
    prune_logical_issue_snapshots,
    read_logical_issues_snapshot,
    scan_install_logical_issues,
)
from mcd_agent.message_queue import (
    DEFAULT_INTERVAL_SEC as MESSAGE_QUEUE_DEFAULT_INTERVAL_SEC,
    effective_message_queue_setting,
    instance_setting_keys as message_queue_instance_setting_keys,
    normalize_interval as normalize_message_queue_interval,
    supports_message_queue,
)
from mcd_agent.mode import (
    reconcile_empty_leads_cleanup_cron,
    reconcile_managed_cron,
    reconcile_mautic_email_fetch_cron,
    reconcile_mautic_message_queue_cron,
    reconcile_plugin_operation_cron,
)
from mcd_agent.plugin_operations import (
    command_template as plugin_operation_command_template,
    effective_values as plugin_operation_effective_values,
    instance_runtime_keys as plugin_operation_instance_runtime_keys,
    legacy_cron_rules as plugin_operation_legacy_cron_rules,
    next_run_epoch as plugin_operation_next_run_epoch,
    operations_for_instance as plugin_operations_for_instance,
    schedule_due as plugin_operation_schedule_due,
    scheduled_tasks as plugin_operation_scheduled_tasks,
)
from mcd_agent.monitored_email import (
    ALLOWED_TYPES as MONITORED_EMAIL_ALLOWED_TYPES,
    MonitoredEmailParserSettings,
    monitored_email_state_key,
    process_monitored_email,
)
from mcd_agent.form_embed import sync_form_embed_settings
from mcd_agent.runtime_overrides import (
    acknowledge_desired_runtime_states,
    associate_instance_desired_states,
    apply_remote_overrides,
    canonical_profile_from_runtime_response,
    consume_poll_trigger,
    desired_profile_from_runtime_response,
    fetch_runtime_overrides,
    instance_desired_states,
    local_runtime_overrides,
    merge_instance_desired_states,
    normalize_segment_special_rings,
    overrides_fingerprint,
    push_runtime_overrides,
)
from mcd_agent.ring_utils import advance_ring_after_launch as _advance_ring_after_launch
from mcd_agent.ring_utils import mark_ring_entity_executed as _mark_ring_entity_executed
from mcd_agent.ring_utils import reconcile_ring as _reconcile_ring
from mcd_agent.ring_utils import reconcile_full_scan_pending as _reconcile_full_scan_pending
from mcd_agent.segment_admission import (
    claim_item as _segment_cycle_claim_item,
    cycle_key as _segment_cycle_key,
    mark_running as _segment_cycle_mark_running,
    mark_terminal as _segment_cycle_mark_terminal,
    manual_admission_info as _segment_cycle_manual_info,
    manual_claim as _segment_cycle_manual_claim_state,
    manual_release as _segment_cycle_manual_release_state,
    manual_terminal as _segment_cycle_manual_terminal_state,
    active_cycle_follow_up_rows as _active_cycle_follow_up_rows,
    cycle_item_tier as _segment_cycle_item_tier,
    never_built_ids as _never_built_segment_ids,
    ordinary_snapshot_order as _ordinary_segment_snapshot_order,
    normalize_ids as _normalize_segment_ids,
    pending_important_ids as _segment_cycle_pending_important_ids,
    reconcile_active_claims as _segment_cycle_reconcile_active_claims,
    release_claim as _segment_cycle_release_claim,
    source_baseline as _ordinary_segment_source_baseline,
    sync_cycle as _segment_cycle_sync_state,
)
from mcd_agent.static_segment_lanes import (
    ORPHAN_GRACE_SEC as _STATIC_LANE_ORPHAN_GRACE_SEC,
    PRIORITY as _SEGMENT_PRIORITY_LANE,
    SCHEMA as _STATIC_SEGMENT_LANES_SCHEMA,
    SUPER_PRIORITY as _SEGMENT_SUPER_PRIORITY_LANE,
    build_lane_plans as _build_static_segment_lane_plans,
    claim_item as _static_lane_claim_item,
    mark_running as _static_lane_mark_running,
    mark_terminal as _static_lane_mark_terminal,
    pending_items as _static_lane_pending_items,
    reconcile_state as _static_lane_reconcile_state,
    release_claim as _static_lane_release_claim,
    state_key as _static_lane_state_key,
)
from mcd_agent.service_profiles import service_profiles_apply_once
from mcd_agent.self_update import _ensure_mcd_service_kill_mode, maybe_auto_update
from mcd_agent.update_admission import _pid_start_time, scheduler_admission_lock, scheduler_pause_path
from mcd_agent.segment_filter_safety import format_segment_filter_issues, segment_invalid_filter_issues
from mcd_agent.segment_recurring_priority import entries_for_instance as recurring_priority_entries_for_instance
from mcd_agent.segment_recurring_priority import state_key as recurring_priority_state_key
from mcd_agent.segment_recurring_priority import state_payload as recurring_priority_state_payload
from mcd_agent.segment_sql_auto import DetectedSQLSegmentRule, detect_auto_sql_segment_rules
from mcd_agent.segment_dependencies import (
    dependency_segment_closure,
    dependency_execution_chains,
    dependency_expanded_segment_plan,
    segment_dependency_blocked_ids,
    segment_dependency_maps,
    mautic7_terminal_segment_plan,
    segment_related_ids,
    stale_dependent_segment_closure,
)
from mcd_agent.signals import collect_monitor_signals, collect_signals
from mcd_agent.web_availability import collect_web_availability
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
_CAMPAIGN_TRIGGER_AUTOMATIC_RETRY_CAP = 2
_DB_DISPATCH_PAUSE_SEC = 120
_DB_WATCHDOG_LONG_QUERIES_PAUSE_THRESHOLD = 50
_DB_WATCHDOG_METADATA_LOCKS_PAUSE_THRESHOLD = 10
_SEGMENT_DEPENDENCY_FINISH_WINDOW_SEC = 2 * 3600
_CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_GRACE_SEC = 180
_CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_INTERVAL_SEC = 60
_CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_STABLE_CHECKS = 2
_CAMPAIGN_TRIGGER_STUCK_COOLDOWN_SEC = 900
_CAMPAIGN_TRIGGER_FUTURE_WAKE_EARLY_SEC = 1.0
_CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC = 30 * 60
_CAMPAIGN_EMAIL_COUNTER_RECONCILE_MIN_INTERVAL_SEC = 6 * 3600
_LOCAL_RUNTIME_PUSH_PENDING_KEY = "local_runtime_push_pending"
_LOCAL_RUNTIME_ACCEPTED_KEY = "local_runtime_accepted"
_LOCAL_RUNTIME_METADATA_KEY = "local_runtime_metadata"
_SEGMENT_DISPATCH_CONTEXT_PREFIX = "segment_dispatch_context:"

# The ordinary dispatcher and the lifecycle monitor share one in-process
# runtime registry.  The lock is deliberately narrow: it protects the
# running/popens registry and claim-to-spawn transition, never a Mautic/DB
# planning query.  This lets the independent root workers use the exact same
# lifecycle while a slow planner is in progress.
_SCHEDULER_RUNTIME_LOCK = threading.RLock()


def _scheduler_runtime_serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize shared running/popens lifecycle transitions."""

    @wraps(method)
    def _serialized(*args: Any, **kwargs: Any) -> Any:
        with _SCHEDULER_RUNTIME_LOCK:
            return method(*args, **kwargs)

    return _serialized


def _iter_instances_with_root_dispatch_gate(
    installs: Iterable[object],
    *,
    busy_roots: set[str],
    lock: threading.RLock,
) -> Iterable[object]:
    """Yield dispatch instances while marking only the current root busy.

    The main dispatcher may spend a long time in one root's Mautic/DB work.
    Persisted ordinary refill is allowed to continue for every other root, so
    the hand-off state must be root-scoped rather than a host-wide event. The
    generator owns cleanup: advancing to the next instance, ``continue``,
    normal exhaustion, and an exception all release the previous root.
    """

    current_root = ""
    try:
        for inst in installs:
            if current_root:
                with lock:
                    busy_roots.discard(current_root)
            current_root = str(getattr(inst, "root", "") or "").strip()
            if current_root:
                with lock:
                    busy_roots.add(current_root)
            yield inst
    finally:
        if current_root:
            with lock:
                busy_roots.discard(current_root)
_MCC_RUNTIME_METADATA_KEY = "mcc_runtime_metadata"
_RUNTIME_METADATA_SCHEMA = "mcd-runtime-sync-metadata-v1"


def _is_instance_runtime_map_key(key: object) -> bool:
    text = str(key or "").strip()
    return text.endswith("_instance_settings") or text in {
        "segment_recurring_priority_v1",
        "segment_special_rings_v1",
    }


def _matching_instance_runtime_entry(entries: Mapping[str, Any], uid: object) -> str | None:
    target = str(uid or "").strip()
    if not target:
        return None
    if target in entries:
        return target
    base = target.split("@", 1)[0]
    matches = [str(key) for key in entries if str(key).split("@", 1)[0] == base]
    return matches[0] if len(matches) == 1 else None


def _merge_missing_instance_runtime_values(
    current_runtime: Mapping[str, Any],
    fallback_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore instance rows lost by an update without overriding new local edits."""
    merged = deepcopy(dict(current_runtime))
    for key, raw_fallback in fallback_runtime.items():
        if not _is_instance_runtime_map_key(key) or not isinstance(raw_fallback, dict):
            continue
        raw_current = merged.get(key)
        if not isinstance(raw_current, dict):
            merged[str(key)] = deepcopy(raw_fallback)
            continue
        next_entries = dict(raw_current)
        for uid, value in raw_fallback.items():
            if _matching_instance_runtime_entry(next_entries, uid) is None:
                next_entries[str(uid)] = deepcopy(value)
        merged[str(key)] = next_entries
    return merged


def _missing_instance_runtime_values(
    local_runtime: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return local instance rows not represented by the remote snapshot."""
    missing: dict[str, dict[str, Any]] = {}
    for key, raw_local in local_runtime.items():
        if not _is_instance_runtime_map_key(key) or not isinstance(raw_local, dict):
            continue
        raw_remote = remote_runtime.get(key)
        remote_entries = raw_remote if isinstance(raw_remote, dict) else {}
        for uid, value in raw_local.items():
            if _matching_instance_runtime_entry(remote_entries, uid) is None:
                missing.setdefault(str(key), {})[str(uid)] = deepcopy(value)
    return missing


def _remove_instance_runtime_values(
    runtime: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Remove only selected instance rows from a local-runtime comparison baseline."""
    previous = deepcopy(dict(runtime))
    for key, raw_rows in rows.items():
        if not isinstance(raw_rows, dict):
            continue
        entries = previous.get(key)
        if not isinstance(entries, dict):
            continue
        next_entries = dict(entries)
        for uid in raw_rows:
            target = str(uid or "").strip()
            base = target.split("@", 1)[0]
            for entry_uid in list(next_entries):
                if str(entry_uid) == target or str(entry_uid).split("@", 1)[0] == base:
                    next_entries.pop(entry_uid, None)
        if next_entries:
            previous[str(key)] = next_entries
        else:
            previous.pop(str(key), None)
    return previous


def _runtime_metadata_empty() -> dict[str, Any]:
    return {
        "schema": _RUNTIME_METADATA_SCHEMA,
        "clock": 0,
        "host": {},
        "instances": {},
    }


def _runtime_metadata_number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return max(0.0, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if number >= 0 else float(default)


def _runtime_metadata_revision(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        revision = int(value)
    except (TypeError, ValueError):
        return int(default)
    return revision if revision >= 0 else int(default)


def _runtime_metadata_stamp(
    raw: object = None,
    *,
    source: str = "mcd",
    updated_at: object = 0.0,
    revision: object = 0,
    event_id: object = "",
    tombstone: bool = False,
) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    selected_source = str(item.get("source", source) or source).strip().lower()
    if selected_source not in {"mcd", "mcc"}:
        selected_source = str(source or "mcd").strip().lower()
        if selected_source not in {"mcd", "mcc"}:
            selected_source = "mcd"
    selected_event_id = str(item.get("event_id", event_id) or event_id or "").strip()
    return {
        "updated_at": _runtime_metadata_number(item.get("updated_at", updated_at)),
        "revision": _runtime_metadata_revision(item.get("revision", revision)),
        "source": selected_source,
        "event_id": selected_event_id,
        "tombstone": bool(item.get("tombstone", tombstone)),
    }


def _runtime_metadata_normalize(raw: object) -> dict[str, Any]:
    """Normalize durable field metadata without trusting file mtimes."""
    out = _runtime_metadata_empty()
    if not isinstance(raw, dict):
        return out
    out["clock"] = _runtime_metadata_revision(raw.get("clock"), 0)
    host = raw.get("host")
    if isinstance(host, dict):
        for key, stamp in host.items():
            out["host"][str(key)] = _runtime_metadata_stamp(stamp)
    instances = raw.get("instances")
    if isinstance(instances, dict):
        for uid, values in instances.items():
            if not isinstance(values, dict):
                continue
            next_values: dict[str, Any] = {}
            for key, stamp in values.items():
                next_values[str(key)] = _runtime_metadata_stamp(stamp)
            if next_values:
                out["instances"][str(uid)] = next_values
    return out


def _runtime_metadata_uid_entry(metadata: Mapping[str, Any], uid: object) -> dict[str, Any] | None:
    instances = metadata.get("instances")
    if not isinstance(instances, dict):
        return None
    target = str(uid or "").strip()
    if not target:
        return None
    exact = instances.get(target)
    if isinstance(exact, dict):
        return exact
    base = target.split("@", 1)[0]
    matches = [value for key, value in instances.items() if str(key).split("@", 1)[0] == base and isinstance(value, dict)]
    return matches[0] if len(matches) == 1 else None


def _runtime_metadata_get(
    metadata: Mapping[str, Any],
    scope: str,
    key: str,
    uid: str = "",
) -> dict[str, Any] | None:
    normalized = _runtime_metadata_normalize(metadata)
    if scope == "host":
        value = normalized["host"].get(str(key))
        return value if isinstance(value, dict) else None
    values = _runtime_metadata_uid_entry(normalized, uid)
    if not isinstance(values, dict):
        return None
    value = values.get(str(key))
    return value if isinstance(value, dict) else None


def _runtime_metadata_set(
    metadata: dict[str, Any],
    scope: str,
    key: str,
    stamp: Mapping[str, Any],
    uid: str = "",
) -> None:
    if scope == "host":
        metadata.setdefault("host", {})[str(key)] = dict(stamp)
        return
    instances = metadata.setdefault("instances", {})
    uid_text = str(uid or "").strip()
    if not uid_text:
        return
    values = instances.setdefault(uid_text, {})
    values[str(key)] = dict(stamp)


def _runtime_snapshot_fields(runtime: Mapping[str, Any]) -> dict[tuple[str, str, str], Any]:
    """Flatten runtime into host-field and instance-field entries."""
    fields: dict[tuple[str, str, str], Any] = {}
    for raw_key, value in runtime.items():
        key = str(raw_key)
        if _is_instance_runtime_map_key(key) and isinstance(value, dict):
            for uid, row in value.items():
                fields[("instance", key, str(uid))] = deepcopy(row)
        elif not _is_instance_runtime_map_key(key):
            fields[("host", key, "")] = deepcopy(value)
    return fields


def _runtime_snapshot_from_fields(fields: Mapping[tuple[str, str, str], Any]) -> dict[str, Any]:
    runtime: dict[str, Any] = {}
    for (scope, key, uid), value in fields.items():
        if scope == "host":
            runtime[key] = deepcopy(value)
            continue
        entries = runtime.setdefault(key, {})
        if isinstance(entries, dict):
            entries[str(uid)] = deepcopy(value)
    return runtime


def _merge_missing_runtime_values(
    current_runtime: Mapping[str, Any],
    fallback_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Fill only absent fields/instance rows from an accepted durable snapshot."""
    merged = deepcopy(dict(current_runtime))
    for key, value in fallback_runtime.items():
        text_key = str(key)
        if _is_instance_runtime_map_key(text_key) and isinstance(value, dict):
            current = merged.get(text_key)
            if not isinstance(current, dict):
                merged[text_key] = deepcopy(value)
                continue
            next_entries = dict(current)
            for uid, row in value.items():
                if _matching_instance_runtime_entry(next_entries, uid) is None:
                    next_entries[str(uid)] = deepcopy(row)
            merged[text_key] = next_entries
        elif text_key not in merged:
            merged[text_key] = deepcopy(value)
    return merged


def _runtime_metadata_for_snapshot(
    metadata: Mapping[str, Any],
    runtime: Mapping[str, Any],
    *,
    source: str,
    updated_at: object = 0.0,
    revision: object = 0,
) -> dict[str, Any]:
    """Add deterministic bootstrap stamps for fields lacking saved metadata."""
    out = _runtime_metadata_normalize(metadata)
    fields = _runtime_snapshot_fields(runtime)
    for scope, key, uid in fields:
        if _runtime_metadata_get(out, scope, key, uid) is not None:
            continue
        stamp = _runtime_metadata_stamp(
            source=source,
            updated_at=updated_at,
            revision=revision,
            event_id=f"bootstrap:{source}:{scope}:{uid}:{key}",
        )
        _runtime_metadata_set(out, scope, key, stamp, uid)
    return out


def _runtime_metadata_max_updated_at(metadata: Mapping[str, Any]) -> float:
    normalized = _runtime_metadata_normalize(metadata)
    values: list[float] = []
    for stamp in normalized["host"].values():
        if isinstance(stamp, dict):
            values.append(_runtime_metadata_number(stamp.get("updated_at")))
    for rows in normalized["instances"].values():
        if isinstance(rows, dict):
            for stamp in rows.values():
                if isinstance(stamp, dict):
                    values.append(_runtime_metadata_number(stamp.get("updated_at")))
    return max(values, default=0.0)


def _runtime_metadata_paths(metadata: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    normalized = _runtime_metadata_normalize(metadata)
    paths: set[tuple[str, str, str]] = {
        ("host", str(key), "") for key in normalized["host"]
    }
    for uid, values in normalized["instances"].items():
        if isinstance(values, dict):
            paths.update(("instance", str(key), str(uid)) for key in values)
    return paths


def _runtime_logical_path(path: tuple[str, str, str]) -> tuple[str, str, str]:
    scope, key, uid = path
    return scope, key, uid.split("@", 1)[0] if scope == "instance" else ""


def _record_local_runtime_changes(
    metadata: Mapping[str, Any],
    previous_runtime: Mapping[str, Any],
    current_runtime: Mapping[str, Any],
    *,
    event_ts: object = None,
) -> dict[str, Any]:
    """Record one local event for every changed host/instance field."""
    out = _runtime_metadata_for_snapshot(metadata, previous_runtime, source="mcd")
    before = _runtime_snapshot_fields(previous_runtime)
    after = _runtime_snapshot_fields(current_runtime)
    changed = [path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path) or path not in after]
    if not changed:
        return out
    clock = _runtime_metadata_revision(out.get("clock"), 0) + 1
    candidate_ts = _runtime_metadata_number(event_ts, 0.0)
    if candidate_ts <= 0:
        candidate_ts = time.time()
    candidate_ts = max(candidate_ts, _runtime_metadata_max_updated_at(out) + 0.000001)
    stamp = _runtime_metadata_stamp(
        source="mcd",
        updated_at=candidate_ts,
        revision=clock,
        event_id=f"mcd:{clock}",
    )
    out["clock"] = clock
    for scope, key, uid in changed:
        current_present = (scope, key, uid) in after
        _runtime_metadata_set(
            out,
            scope,
            key,
            {**stamp, "tombstone": not current_present},
            uid,
        )
    return out


def _has_authoritative_local_runtime_edit(
    metadata: Mapping[str, Any],
    previous_runtime: Mapping[str, Any],
    current_runtime: Mapping[str, Any],
) -> bool:
    """Return true only for a durable non-bootstrap MCD mutation."""
    before = _runtime_snapshot_fields(previous_runtime)
    after = _runtime_snapshot_fields(current_runtime)
    for path in set(before) | set(after):
        if before.get(path) == after.get(path):
            continue
        scope, key, uid = path
        stamp = _runtime_metadata_get(metadata, scope, key, uid)
        if not isinstance(stamp, dict):
            continue
        if str(stamp.get("source", "")).strip().lower() == "mcd" and not str(
            stamp.get("event_id", "")
        ).startswith("bootstrap:"):
            return True
    return False


def _runtime_stamp_order(stamp: Mapping[str, Any]) -> tuple[Any, ...]:
    """Compare saved event stamps; MCC wins an exact deterministic tie."""
    updated_at = _runtime_metadata_number(stamp.get("updated_at"))
    revision = _runtime_metadata_revision(stamp.get("revision"), 0)
    source = str(stamp.get("source", "") or "").strip().lower()
    source_rank = 1 if source == "mcc" else 0
    event_id = str(stamp.get("event_id", "") or "")
    return (updated_at, revision, source_rank, event_id)


def _runtime_stamp_wins(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether left is newer than right using deterministic LWW order."""
    left_updated = _runtime_metadata_number(left.get("updated_at"))
    right_updated = _runtime_metadata_number(right.get("updated_at"))
    if left_updated > 0 and right_updated > 0:
        return _runtime_stamp_order(left) > _runtime_stamp_order(right)
    left_revision = _runtime_metadata_revision(left.get("revision"), 0)
    right_revision = _runtime_metadata_revision(right.get("revision"), 0)
    if left_revision != right_revision:
        return left_revision > right_revision
    return _runtime_stamp_order(left) > _runtime_stamp_order(right)


def _merge_legacy_priority_alias_with_metadata(
    runtime: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve legacy whitelist and priority as one LWW instance field."""
    out = deepcopy(dict(runtime))
    legacy = out.get("segment_whitelist_instance_settings")
    priority = out.get("segment_priority_instance_settings")
    if not isinstance(legacy, dict) or not isinstance(priority, dict):
        return out
    merged = dict(priority)
    for uid, legacy_value in legacy.items():
        uid_text = str(uid)
        modern_value = priority.get(uid_text)
        if modern_value is None:
            merged[uid_text] = deepcopy(legacy_value)
            continue
        modern_empty = isinstance(modern_value, dict) and modern_value.get("segments") == []
        legacy_nonempty = isinstance(legacy_value, dict) and bool(
            legacy_value.get("segments") or legacy_value.get("segment_whitelist")
        )
        modern_stamp = _runtime_metadata_get(
            metadata, "instance", "segment_priority_instance_settings", uid_text
        ) or {}
        if modern_empty and legacy_nonempty and not bool(modern_stamp.get("tombstone")):
            merged[uid_text] = deepcopy(legacy_value)
        elif not modern_empty and isinstance(modern_value, dict) and isinstance(legacy_value, dict):
            modern_ids = _to_int_list(modern_value.get("segments", []))
            legacy_ids = _to_int_list(
                legacy_value.get("segments", legacy_value.get("segment_whitelist", []))
            )
            if legacy_ids:
                merged[uid_text] = {
                    **modern_value,
                    # Preserve execution order from the legacy alias while
                    # appending only modern IDs that are not already present.
                    "segments": list(dict.fromkeys([*legacy_ids, *modern_ids])),
                }
    out["segment_priority_instance_settings"] = merged
    return out


def _resolve_runtime_snapshots(
    local_runtime: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
    local_metadata: Mapping[str, Any],
    remote_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve host and instance fields with durable last-write-wins metadata."""
    remote_runtime = _merge_legacy_priority_alias_with_metadata(remote_runtime, remote_metadata)
    local_fields = _runtime_snapshot_fields(local_runtime)
    remote_fields = _runtime_snapshot_fields(remote_runtime)
    local_meta = _runtime_metadata_for_snapshot(local_metadata, local_runtime, source="mcd")
    remote_meta = _runtime_metadata_for_snapshot(remote_metadata, remote_runtime, source="mcc")
    local_groups: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    remote_groups: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for path in local_fields:
        local_groups.setdefault(_runtime_logical_path(path), []).append(path)
    for path in remote_fields:
        remote_groups.setdefault(_runtime_logical_path(path), []).append(path)
    metadata_paths = _runtime_metadata_paths(local_meta) | _runtime_metadata_paths(remote_meta)
    all_logical = (
        set(local_groups)
        | set(remote_groups)
        | {_runtime_logical_path(path) for path in metadata_paths}
    )
    for scope, key, uid in sorted(all_logical):
        local_paths = local_groups.get((scope, key, uid), [])
        remote_paths = remote_groups.get((scope, key, uid), [])
        if not local_paths and not remote_paths:
            continue
        # Base and host-qualified UIDs describe the same instance only when
        # the alias is unambiguous. Keep multiple qualified rows separate.
        if len(local_paths) > 1 or len(remote_paths) > 1:
            continue
        local_path = local_paths[0] if local_paths else None
        remote_path = remote_paths[0] if remote_paths else None
        lookup_uid = (local_path or remote_path or (scope, key, uid))[2]
        if remote_path is None and _runtime_metadata_get(remote_meta, scope, key, lookup_uid) is None:
            _runtime_metadata_set(
                remote_meta,
                scope,
                key,
                _runtime_metadata_stamp(source="mcc", event_id=f"remote:{scope}:{lookup_uid}:{key}"),
                lookup_uid,
            )
    resolved: dict[tuple[str, str, str], Any] = {}
    resolved_meta = _runtime_metadata_empty()
    local_wins: list[tuple[str, str, str]] = []
    remote_wins: list[tuple[str, str, str]] = []
    local_push: list[tuple[str, str, str]] = []
    for logical_path in sorted(all_logical):
        scope, key, logical_uid = logical_path
        local_paths = local_groups.get(logical_path, [])
        remote_paths = remote_groups.get(logical_path, [])
        if len(local_paths) > 1 or len(remote_paths) > 1:
            continue
        local_path = local_paths[0] if local_paths else None
        remote_path = remote_paths[0] if remote_paths else None
        local_present = local_path is not None
        remote_present = remote_path is not None
        local_uid = local_path[2] if local_path else logical_uid
        remote_uid = remote_path[2] if remote_path else logical_uid
        local_stamp = _runtime_metadata_get(local_meta, scope, key, local_uid)
        remote_stamp = _runtime_metadata_get(remote_meta, scope, key, remote_uid)
        if local_present and not remote_present and not (remote_stamp and remote_stamp.get("tombstone")):
            winner = "local"
        elif remote_present and not local_present and not (local_stamp and local_stamp.get("tombstone")):
            winner = "remote"
        else:
            local_stamp = local_stamp or _runtime_metadata_stamp(source="mcd", event_id=f"local:{scope}:{uid}:{key}")
            remote_stamp = remote_stamp or _runtime_metadata_stamp(source="mcc", event_id=f"remote:{scope}:{uid}:{key}")
            winner = "remote" if _runtime_stamp_wins(remote_stamp, local_stamp) else "local"
        stamp = remote_stamp if winner == "remote" else local_stamp
        if stamp is None:
            stamp = _runtime_metadata_stamp(source=winner)
        selected_path = remote_path if winner == "remote" else local_path
        selected_uid = selected_path[2] if selected_path is not None else (remote_uid if winner == "remote" else local_uid)
        _runtime_metadata_set(resolved_meta, scope, key, stamp, selected_uid)
        if winner == "remote":
            remote_wins.append(selected_path or (scope, key, selected_uid))
            if remote_present:
                assert remote_path is not None
                resolved[remote_path] = deepcopy(remote_fields[remote_path])
        else:
            local_wins.append(selected_path or (scope, key, selected_uid))
            if local_present:
                assert local_path is not None
                resolved[local_path] = deepcopy(local_fields[local_path])
                local_event = bool(
                    local_stamp
                    and (
                        _runtime_metadata_number(local_stamp.get("updated_at")) > 0
                        or not str(local_stamp.get("event_id", "")).startswith("bootstrap:")
                    )
                )
                if local_event and (
                    not remote_present or remote_fields.get(remote_path) != local_fields[local_path]
                ):
                    local_push.append(local_path)
            elif local_stamp and local_stamp.get("tombstone") and remote_present:
                if _runtime_metadata_number(local_stamp.get("updated_at")) > 0 or not str(
                    local_stamp.get("event_id", "")
                ).startswith("bootstrap:"):
                    local_push.append(local_path or (scope, key, local_uid))
    resolved_meta["clock"] = max(
        _runtime_metadata_revision(local_meta.get("clock"), 0),
        _runtime_metadata_revision(remote_meta.get("clock"), 0),
    )
    return {
        "runtime": _runtime_snapshot_from_fields(resolved),
        "metadata": resolved_meta,
        "local_wins": local_wins,
        "remote_wins": remote_wins,
        "local_push": local_push,
        "local_tombstones": [path for path in local_wins if path not in resolved],
        "remote_tombstones": [path for path in remote_wins if path not in resolved],
    }


def _runtime_metadata_wire_merge(
    metadata: dict[str, Any],
    raw: object,
    *,
    default_source: str = "mcc",
    default_revision: object = 0,
    default_updated_at: object = 0.0,
) -> None:
    """Accept the versioned host/instance metadata envelope when MCC returns it."""
    if not isinstance(raw, dict):
        return
    host = raw.get("host")
    if isinstance(host, dict):
        for key, raw_stamp in host.items():
            _runtime_metadata_set(
                metadata,
                "host",
                str(key),
                _runtime_metadata_stamp(
                    raw_stamp,
                    source=default_source,
                    revision=default_revision,
                    updated_at=default_updated_at,
                ),
            )
    instances = raw.get("instances")
    if isinstance(instances, dict):
        for uid, values in instances.items():
            if not isinstance(values, dict):
                continue
            for key, raw_stamp in values.items():
                _runtime_metadata_set(
                    metadata,
                    "instance",
                    str(key),
                    _runtime_metadata_stamp(
                        raw_stamp,
                        source=default_source,
                        revision=default_revision,
                        updated_at=default_updated_at,
                    ),
                    str(uid),
                )
    tombstones = raw.get("tombstones")
    if isinstance(tombstones, list):
        for item in tombstones:
            if not isinstance(item, dict):
                continue
            scope = "instance" if str(item.get("scope", "host")) == "instance" else "host"
            _runtime_metadata_set(
                metadata,
                scope,
                str(item.get("key", "")),
                _runtime_metadata_stamp(
                    item,
                    source=default_source,
                    revision=default_revision,
                    updated_at=default_updated_at,
                    tombstone=True,
                ),
                str(item.get("uid", "")),
            )


def _runtime_metadata_from_response(
    response: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
    known_local_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Build remote field stamps; old responses get revision-only fallback stamps."""
    metadata = _runtime_metadata_empty()
    desired = response.get("desired_state")
    host_revision = desired.get("revision") if isinstance(desired, dict) else 0
    host_updated_at = desired.get("updated_at") if isinstance(desired, dict) else response.get("updated_at")
    raw_metadata = response.get("runtime_metadata")
    if raw_metadata is None:
        raw_metadata = response.get("field_metadata", response.get("runtime_overrides_metadata"))
    _runtime_metadata_wire_merge(
        metadata,
        raw_metadata,
        default_source="mcc",
        default_revision=host_revision,
        default_updated_at=host_updated_at,
    )
    remote_fields = _runtime_snapshot_fields(remote_runtime)
    for scope, key, uid in remote_fields:
        if _runtime_metadata_get(metadata, scope, key, uid) is None:
            row = {}
            states = response.get("instance_desired_states")
            if scope == "instance" and isinstance(states, dict):
                state = states.get(uid)
                if not isinstance(state, dict):
                    match = _matching_instance_runtime_entry(states, uid)
                    state = states.get(match) if match else None
                row = state if isinstance(state, dict) else {}
            row_revision = row.get("revision", host_revision) if isinstance(row, dict) else host_revision
            row_updated = row.get("updated_at", host_updated_at) if isinstance(row, dict) else host_updated_at
            _runtime_metadata_set(
                metadata,
                scope,
                key,
                _runtime_metadata_stamp(
                    source="mcc",
                    revision=row_revision,
                    updated_at=row_updated,
                    event_id=f"mcc:{scope}:{uid}:{key}:{row_revision}",
                ),
                uid,
            )
    # A present instance row with a revision and an explicit empty
    # runtime_overrides envelope is an intentional field-level clear. A
    # missing row or a missing key in an old snapshot is not a deletion.
    states = response.get("instance_desired_states")
    if isinstance(states, dict):
        for raw_uid, raw_state in states.items():
            if not isinstance(raw_state, dict) or "runtime_overrides" not in raw_state:
                continue
            uid = str(raw_uid)
            row_revision = raw_state.get("revision", host_revision)
            row_updated = raw_state.get("updated_at", host_updated_at)
            known_fields = _runtime_snapshot_fields(known_local_runtime)
            for scope, key, field_uid in known_fields:
                if scope != "instance" or field_uid.split("@", 1)[0] != uid.split("@", 1)[0]:
                    continue
                state_overrides = raw_state.get("runtime_overrides")
                if isinstance(state_overrides, dict) and key in state_overrides:
                    continue
                if _runtime_metadata_get(metadata, "instance", key, field_uid) is None:
                    _runtime_metadata_set(
                        metadata,
                        "instance",
                        key,
                        _runtime_metadata_stamp(
                            source="mcc",
                            revision=row_revision,
                            updated_at=row_updated,
                            event_id=f"mcc:tombstone:{uid}:{key}:{row_revision}",
                            tombstone=True,
                        ),
                        field_uid,
                    )
    return metadata


def _runtime_metadata_has_accepted(store: object) -> bool:
    try:
        raw = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
    except Exception:
        return False
    return isinstance(raw, dict) and isinstance(raw.get("runtime"), dict)


def _accepted_runtime_response_matches(store: object, response: Mapping[str, Any]) -> bool:
    """Recognize the exact accepted snapshot so a normal poll cannot echo it."""
    try:
        accepted = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
    except Exception:
        return False
    if not isinstance(accepted, dict):
        return False
    desired = response.get("desired_state")
    accepted_revision = accepted.get("host_revision")
    response_revision = desired.get("revision") if isinstance(desired, dict) else None
    if not isinstance(accepted_revision, int) or isinstance(accepted_revision, bool):
        return False
    if response_revision != accepted_revision:
        return False
    accepted_instances = accepted.get("instance_revisions")
    if not isinstance(accepted_instances, dict):
        return True
    response_instances = response.get("instance_desired_states")
    response_instances = response_instances if isinstance(response_instances, dict) else {}
    for uid, revision in accepted_instances.items():
        if not isinstance(revision, int) or isinstance(revision, bool):
            return False
        matched_uid = _matching_instance_runtime_entry(response_instances, uid)
        if matched_uid is None:
            return False
        row = response_instances.get(matched_uid)
        if not isinstance(row, dict) or row.get("revision") != revision:
            return False
    return True


def _clear_local_runtime_accepted(store: object) -> None:
    try:
        delete = getattr(store, "delete_runtime_sync", None)
        if callable(delete):
            delete([_LOCAL_RUNTIME_ACCEPTED_KEY])
    except Exception:
        logging.warning("runtime-overrides accepted marker cleanup failed")


def _runtime_metadata_has_remote_tombstone(
    metadata: Mapping[str, Any],
    key: str,
) -> bool:
    normalized = _runtime_metadata_normalize(metadata)
    host_stamp = normalized["host"].get(str(key))
    if isinstance(host_stamp, dict) and host_stamp.get("tombstone") and host_stamp.get("source") == "mcc":
        return True
    instances = normalized.get("instances")
    if isinstance(instances, dict):
        for values in instances.values():
            stamp = values.get(str(key)) if isinstance(values, dict) else None
            if isinstance(stamp, dict) and stamp.get("tombstone") and stamp.get("source") == "mcc":
                return True
    return False


def _persist_resolved_runtime_to_config(
    config: AgentConfig,
    previous_runtime: Mapping[str, Any],
    resolved_runtime: Mapping[str, Any],
    resolved_metadata: Mapping[str, Any],
) -> None:
    """Persist only resolved values and explicit MCC tombstones."""
    previous = dict(previous_runtime)
    resolved = dict(resolved_runtime)
    stable_keys = (_STABLE_RUNTIME_KEYS | set(previous) | set(resolved)) & _STABLE_RUNTIME_KEYS
    updates = {
        str(key): deepcopy(resolved[key])
        for key in stable_keys
        if key in resolved and previous.get(key) != resolved.get(key)
    }
    if updates:
        try:
            path, changed = upsert_runtime_values(config.config_file_path, updates)
            if changed:
                logging.info(
                    "runtime-overrides resolved values persisted to config (%s): keys=%s",
                    path,
                    ",".join(sorted(updates)),
                )
        except Exception as exc:
            logging.warning("runtime-overrides resolved persistence failed: %s", exc)
    removals = {
        str(key)
        for key in stable_keys
        if key in previous
        and key not in resolved
        and _runtime_metadata_has_remote_tombstone(resolved_metadata, str(key))
    }
    if removals:
        try:
            path, changed = remove_runtime_values(config.config_file_path, removals)
            if changed:
                logging.info(
                    "runtime-overrides explicit MCC tombstones removed config keys (%s): keys=%s",
                    path,
                    ",".join(sorted(removals)),
                )
        except Exception as exc:
            logging.warning("runtime-overrides explicit tombstone persistence failed: %s", exc)


def _runtime_reconciliation_previous(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    remote: Mapping[str, Any],
    resolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Make a pending local delta for fields where local LWW won."""
    previous = deepcopy(dict(baseline))
    local_fields = _runtime_snapshot_fields(current)
    remote_fields = _runtime_snapshot_fields(remote)
    for path in resolution.get("local_push", []):
        scope, key, uid = path
        entries = previous.get(key) if scope == "instance" else None
        if scope == "host":
            previous.pop(key, None)
            continue
        if isinstance(entries, dict):
            next_entries = dict(entries)
            for entry_uid in list(next_entries):
                if str(entry_uid) == uid or str(entry_uid).split("@", 1)[0] == uid.split("@", 1)[0]:
                    next_entries.pop(entry_uid, None)
            if next_entries:
                previous[key] = next_entries
            else:
                previous.pop(key, None)
    return previous


def _persist_recovered_instance_runtime(
    config: AgentConfig,
    current_runtime: Mapping[str, Any],
    recovered_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize durable instance rows before the daemon classifies local edits."""
    updates = {
        str(key): deepcopy(value)
        for key, value in recovered_runtime.items()
        if _is_instance_runtime_map_key(key) and current_runtime.get(key) != value
    }
    if not updates:
        return dict(recovered_runtime)
    try:
        path, changed = upsert_runtime_values(config.config_file_path, updates)
        if changed:
            logging.warning(
                "runtime-overrides restored missing instance rows from durable baseline (%s): keys=%s",
                path,
                ",".join(sorted(updates)),
            )
    except Exception as exc:
        logging.warning("runtime-overrides instance-row recovery failed: %s", exc)
    return dict(recovered_runtime)


def _read_local_runtime_push_pending(store: object) -> dict[str, dict[str, Any]] | None:
    """Read a durable local-runtime delta queued before a remote refresh."""
    try:
        raw = store.get_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    previous = raw.get("previous")
    current = raw.get("current")
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    return {"previous": deepcopy(previous), "current": deepcopy(current)}


def _clear_local_runtime_push_pending(store: object) -> None:
    try:
        delete = getattr(store, "delete_runtime_sync", None)
        if callable(delete):
            delete([_LOCAL_RUNTIME_PUSH_PENDING_KEY])
    except Exception:
        logging.warning("runtime-overrides pending local delta cleanup failed")


def _queue_local_runtime_delta(
    store: object,
    current_local_runtime: Mapping[str, Any],
    *,
    allow_unproven: bool = True,
) -> dict[str, dict[str, Any]] | None:
    """Persist a local edit before MCC poll; poll rebase is never an edit."""
    current = deepcopy(dict(current_local_runtime))
    baseline = store.get_runtime_sync("local_runtime")
    baseline = baseline if isinstance(baseline, dict) else {}
    metadata_raw = store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY)
    metadata = _runtime_metadata_for_snapshot(metadata_raw, baseline, source="mcd")
    pending = _read_local_runtime_push_pending(store)
    if current == baseline:
        store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, metadata)
        if pending is not None and pending["current"] != current:
            _clear_local_runtime_push_pending(store)
            pending = None
        return pending
    if not allow_unproven and pending is None and not _has_authoritative_local_runtime_edit(
        metadata, baseline, current
    ):
        return None
    metadata = _record_local_runtime_changes(metadata, baseline, current)
    store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, metadata)
    previous = pending["previous"] if pending is not None else deepcopy(baseline)
    if pending is not None and pending["current"] == current:
        return pending
    next_pending = {"previous": previous, "current": current}
    store.put_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY, next_pending)
    return next_pending


def _accepted_local_runtime_snapshot(store: object) -> dict[str, Any]:
    try:
        raw = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
    except Exception:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("runtime"), dict):
        return {}
    return deepcopy(raw["runtime"])


def _remote_runtime_response_is_stale(store: object, response: Mapping[str, Any]) -> bool:
    """Do not let a post-push stale poll clear a just-accepted instance row."""
    try:
        accepted = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
    except Exception:
        return False
    if not isinstance(accepted, dict):
        return False
    accepted_host_revision = accepted.get("host_revision")
    desired_state = response.get("desired_state")
    response_host_revision = desired_state.get("revision") if isinstance(desired_state, dict) else None
    if (
        isinstance(accepted_host_revision, int)
        and not isinstance(accepted_host_revision, bool)
    ):
        if not isinstance(response_host_revision, int) or isinstance(response_host_revision, bool):
            return True
        if response_host_revision < accepted_host_revision:
            return True
    # Instance revisions are resolved per field by _resolve_runtime_snapshots.
    # A migration may return one lagging instance row alongside a newer host
    # envelope and unrelated newer instances. Rejecting the whole response
    # here would strand valid scopes; the accepted field metadata remains the
    # monotonic guard that prevents a stale instance map from overwriting a
    # newer accepted map.
    return False


def _queue_missing_remote_instance_runtime(
    store: object,
    current_local_runtime: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """Queue local instance rows absent from a non-stale MCC snapshot."""
    accepted_runtime = _accepted_local_runtime_snapshot(store)
    comparison_runtime = _merge_missing_instance_runtime_values(remote_runtime, accepted_runtime)
    missing = _missing_instance_runtime_values(current_local_runtime, comparison_runtime)
    if not missing:
        return _read_local_runtime_push_pending(store)
    baseline = store.get_runtime_sync("local_runtime")
    baseline = baseline if isinstance(baseline, dict) else {}
    previous = _remove_instance_runtime_values(baseline, missing)
    pending = {
        "previous": previous,
        "current": deepcopy(dict(current_local_runtime)),
    }
    store.put_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY, pending)
    store.put_runtime_sync("local_runtime", previous)
    return pending


def _sync_local_runtime_baseline_after_config_recovery(config: AgentConfig, store: object) -> str:
    """Treat MCC recovery as the new local baseline, never as a local edit."""
    runtime = local_runtime_overrides(config)
    fingerprint = overrides_fingerprint(runtime)
    store.put_runtime_sync("local_runtime", runtime)
    _clear_local_runtime_push_pending(store)
    return fingerprint


def _initialize_local_runtime_baseline(config: AgentConfig, store: object) -> tuple[dict[str, Any], str]:
    """Recover local instance intent before the first post-update classification."""
    current = local_runtime_overrides(config)
    pending = _read_local_runtime_push_pending(store)
    stored = store.get_runtime_sync("local_runtime")
    if pending is not None:
        recovered = _merge_missing_instance_runtime_values(current, pending["current"])
        _persist_recovered_instance_runtime(config, current, recovered)
        stored = pending["previous"]
        store.put_runtime_sync("local_runtime", stored)
        metadata = _runtime_metadata_for_snapshot(
            store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY),
            stored,
            source="mcd",
        )
        store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, metadata)
        return dict(stored), overrides_fingerprint(stored)

    if not isinstance(stored, dict):
        stored = current
        store.put_runtime_sync("local_runtime", current)
        metadata = _runtime_metadata_for_snapshot(
            store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY),
            current,
            source="mcd",
        )
        store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, metadata)
        return dict(stored), overrides_fingerprint(stored)

    remote_runtime = store.get_runtime_sync("mcc_runtime")
    remote_runtime = remote_runtime if isinstance(remote_runtime, dict) else {}
    accepted_runtime = _accepted_local_runtime_snapshot(store)
    remote_for_recovery = _merge_missing_instance_runtime_values(remote_runtime, accepted_runtime)

    # A package/update may leave the mutable file without an instance row even
    # though the durable local baseline still has it. Restore only rows that
    # are also absent from the last known MCC/accepted snapshot; an explicit
    # local deletion against a present remote row remains a real local delta.
    recoverable = deepcopy(stored)
    for key, rows in _missing_instance_runtime_values(stored, remote_for_recovery).items():
        current_entries = current.get(key)
        if not isinstance(current_entries, dict):
            recoverable[str(key)] = deepcopy(stored.get(key, {}))
            continue
        missing_rows = {
            str(uid): deepcopy(value)
            for uid, value in rows.items()
            if _matching_instance_runtime_entry(current_entries, uid) is None
        }
        if missing_rows:
            existing = recoverable.get(key)
            existing = dict(existing) if isinstance(existing, dict) else {}
            existing.update(missing_rows)
            recoverable[str(key)] = existing
    recovered = _merge_missing_instance_runtime_values(current, recoverable)
    _persist_recovered_instance_runtime(config, current, recovered)
    current = recovered

    raw_metadata = _runtime_metadata_normalize(store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY))
    local_edit = _has_authoritative_local_runtime_edit(raw_metadata, stored, current)
    metadata = _runtime_metadata_for_snapshot(raw_metadata, stored, source="mcd")
    if current != stored and local_edit:
        metadata = _record_local_runtime_changes(metadata, stored, current)
    store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, metadata)

    missing_remote = _missing_instance_runtime_values(current, remote_for_recovery)
    if missing_remote or (current != stored and local_edit):
        previous = _remove_instance_runtime_values(stored, missing_remote)
        pending = {"previous": previous, "current": deepcopy(current)}
        store.put_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY, pending)
        store.put_runtime_sync("local_runtime", previous)
        return previous, overrides_fingerprint(previous)
    if current != stored and not local_edit:
        # A restart/profile rebase can legitimately materialize values that
        # differ from the old local snapshot. Without durable operator
        # provenance this is not a local-wins event and must not create a
        # revision push back to MCC.
        store.put_runtime_sync("local_runtime", current)
        return dict(current), overrides_fingerprint(current)
    return dict(stored), overrides_fingerprint(stored)


def _store_runtime_fetch_snapshot(
    store: object,
    response: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
    remote_metadata: Mapping[str, Any],
) -> None:
    """Persist a canonical MCC read before any local baseline classification."""
    store.put_runtime_sync("mcc_runtime", dict(remote_runtime))
    store.put_runtime_sync(_MCC_RUNTIME_METADATA_KEY, dict(remote_metadata))
    desired_state = response.get("desired_state")
    if not isinstance(desired_state, dict):
        return
    revisions: dict[str, int] = {}
    instance_states = response.get("instance_desired_states")
    if isinstance(instance_states, dict):
        for uid, item in instance_states.items():
            if isinstance(item, dict) and isinstance(item.get("revision"), int) and not isinstance(item.get("revision"), bool):
                revisions[str(uid)] = int(item["revision"])
    state_for_store = dict(desired_state)
    if isinstance(instance_states, dict):
        state_for_store["instance_desired_states"] = deepcopy(instance_states)
    state_for_store["instance_revisions"] = revisions
    store.put_runtime_sync("mcc_runtime_desired_state", state_for_store)


def _bootstrap_remote_runtime_before_local_baseline(
    config: AgentConfig,
    store: object,
    instance_uids: list[str],
) -> str:
    """Read canonical MCC state before startup can mistake a missing file row for deletion."""
    if not config.mcc_url or not config.mcc_token:
        return "disabled"
    response = fetch_runtime_overrides(config, instance_uids=instance_uids)
    status = str(response.get("status", "")).strip().lower()
    if status != "ok":
        return status or "error"
    if _remote_runtime_response_is_stale(store, response):
        logging.info("runtime-overrides startup bootstrap deferred behind accepted revision")
        return "deferred_remote_stale"

    overrides_raw = response.get("runtime_overrides")
    remote_runtime = merge_instance_desired_states(
        overrides_raw if isinstance(overrides_raw, dict) else {},
        response.get("instance_desired_states"),
    )
    if _accepted_runtime_response_matches(store, response):
        accepted_raw = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
        accepted_runtime = (
            accepted_raw.get("runtime")
            if isinstance(accepted_raw, dict) and isinstance(accepted_raw.get("runtime"), dict)
            else {}
        )
        remote_runtime = _merge_missing_runtime_values(remote_runtime, accepted_runtime)

    current_local_runtime = local_runtime_overrides(config)
    local_metadata = _runtime_metadata_for_snapshot(
        store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY),
        current_local_runtime,
        source="mcd",
    )
    remote_metadata = _runtime_metadata_from_response(
        response,
        remote_runtime,
        current_local_runtime,
    )
    resolution = _resolve_runtime_snapshots(
        current_local_runtime,
        remote_runtime,
        local_metadata,
        remote_metadata,
    )
    _store_runtime_fetch_snapshot(store, response, remote_runtime, remote_metadata)
    if resolution.get("local_push"):
        # Keep the local event for the normal accepted-push path. Do not apply
        # a remote snapshot over a newer offline MCD edit during bootstrap.
        store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, local_metadata)
        return "local_pending"

    resolved_runtime = resolution["runtime"]
    resolved_metadata = resolution["metadata"]
    _persist_resolved_runtime_to_config(
        config,
        current_local_runtime,
        resolved_runtime,
        resolved_metadata,
    )
    store.put_runtime_sync("local_runtime", resolved_runtime)
    store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, resolved_metadata)
    _clear_local_runtime_push_pending(store)
    _clear_local_runtime_accepted(store)
    return "applied"


def _rebase_local_runtime_payload(
    latest_runtime: Mapping[str, Any],
    previous_local_runtime: Mapping[str, Any],
    current_local_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply only the local delta to a fresh MCC snapshot.

    Instance-scoped maps are rebased separately through canonical UID rows so
    a replace push cannot erase an unrelated instance added in MCC.
    """
    rebased = deepcopy(dict(latest_runtime))
    changed_keys = set(previous_local_runtime) | set(current_local_runtime)
    for key in changed_keys:
        if _is_instance_runtime_map_key(key):
            continue
        if key in current_local_runtime:
            if key not in previous_local_runtime or previous_local_runtime[key] != current_local_runtime[key]:
                rebased[str(key)] = deepcopy(current_local_runtime[key])
        elif key in previous_local_runtime:
            rebased.pop(str(key), None)
    return rebased


def _normalize_instance_runtime_row(value: object) -> dict[str, Any]:
    """Normalize one desired-state row to the lane-key wire shape."""
    if not isinstance(value, dict):
        return {}
    row: dict[str, Any] = {}
    for key, item in value.items():
        if key == "revision":
            continue
        if key == "segment_special_rings_v1":
            lanes = normalize_segment_special_rings(item)
            if lanes is None:
                # Keep malformed compatibility data visible so the normal
                # validation/ack path can reject it instead of silently
                # deleting it during a local rebase.
                row[key] = deepcopy(item)
            else:
                row.update(deepcopy(lanes))
            continue
        row[str(key)] = deepcopy(item)
    return row


def _normalize_instance_runtime_rows(states: object) -> dict[str, dict[str, Any]]:
    """Return instance rows without transport-only revisions."""
    if not isinstance(states, dict):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for raw_uid, raw_state in states.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_state, dict):
            continue
        overrides = raw_state.get("runtime_overrides", raw_state)
        row = _normalize_instance_runtime_row(overrides)
        rows[uid] = row
    return rows


def _local_instance_runtime_rows(
    previous_local_runtime: Mapping[str, Any],
    current_local_runtime: Mapping[str, Any],
    installs: list[object],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Extract normalized local rows before and after the local edit."""
    previous = instance_desired_states(dict(previous_local_runtime), installs)
    current = instance_desired_states(dict(current_local_runtime), installs)
    return (
        {
            str(uid): _normalize_instance_runtime_row(row)
            for uid, row in previous.items()
            if isinstance(row, dict)
        },
        {
            str(uid): _normalize_instance_runtime_row(row)
            for uid, row in current.items()
            if isinstance(row, dict)
        },
    )


def _rebase_local_instance_runtime_states(
    latest_runtime: Mapping[str, Any],
    latest_instance_states: object,
    previous_local_runtime: Mapping[str, Any],
    current_local_runtime: Mapping[str, Any],
    installs: list[object],
    canonical_uids: object = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Apply field-level local instance deltas to a fresh MCC snapshot.

    The desired endpoint treats the replace payload as authoritative.  Keep
    every remote row and only change fields that differ from the persisted
    local baseline.  In particular, a missing local field is not a deletion
    unless that field existed in the persisted baseline.
    """
    remote_rows = _normalize_instance_runtime_rows(latest_instance_states)
    previous_rows, current_rows = _local_instance_runtime_rows(
        previous_local_runtime,
        current_local_runtime,
        installs,
    )
    known_uids = set(remote_rows)
    previous_rows = associate_instance_desired_states(previous_rows, known_uids)
    current_rows = associate_instance_desired_states(current_rows, known_uids)

    final_rows = {uid: deepcopy(row) for uid, row in remote_rows.items()}
    for uid in sorted(set(previous_rows) | set(current_rows)):
        before = previous_rows.get(uid, {})
        after = current_rows.get(uid, {})
        row = deepcopy(final_rows.get(uid, {}))
        remote_present = uid in final_rows
        for key in sorted(set(before) | set(after)):
            if key in after:
                # A key unchanged since the local baseline must not overwrite
                # a newer remote value.  A newly added/edited key is local
                # intent and is therefore applied to the fresh row.
                if key not in before or before[key] != after[key] or not remote_present:
                    row[key] = deepcopy(after[key])
            elif key in before:
                # Only an explicit removal from a baseline that contained the
                # key is a deletion.  Unseen remote keys remain untouched.
                row.pop(key, None)
        # A last local instance override removed from the persisted baseline
        # is an intentional native clear. Keep an explicit empty row in the
        # replace payload even when MCC has no row in the fresh snapshot;
        # omitting the UID would preserve MCC's old sparse map and resurrect
        # the deleted override on the next rebase. A missing local field that
        # was never present in the baseline still remains an omission.
        if remote_present or row or after or (uid in previous_rows and not after):
            final_rows[uid] = row

    # A fresh response can legitimately omit a row while a local removal is
    # being published. Prefer the stable host-qualified revision key when it
    # is already known, so the explicit empty row is checked against the
    # current per-instance revision rather than an unqualified alias.
    if isinstance(canonical_uids, (list, tuple, set, dict)):
        final_rows = associate_instance_desired_states(final_rows, canonical_uids)

    rebased = _rebase_local_runtime_payload(
        latest_runtime,
        previous_local_runtime,
        current_local_runtime,
    )
    rebased = merge_instance_desired_states(rebased, final_rows)
    # MCC replace semantics preserve an omitted sparse map. When the local
    # edit removed the last baseline row, retain an explicit empty map in the
    # host payload so that the native clear is not resurrected on rebase.
    for runtime_key in ("segment_lazy_update_instance_settings",):
        before = previous_local_runtime.get(runtime_key)
        after = current_local_runtime.get(runtime_key)
        if (
            not isinstance(before, dict)
            or not before
            or (isinstance(after, dict) and after)
        ):
            continue
        effective = rebased.get(runtime_key)
        if not isinstance(effective, dict) or not effective:
            rebased[runtime_key] = {}
    return rebased, final_rows


def _instance_state_revisions(states: object, wanted_uids: set[str]) -> dict[str, int]:
    if not isinstance(states, dict):
        return {}
    revisions: dict[str, int] = {}
    for raw_uid, raw_state in states.items():
        uid = str(raw_uid or "").strip()
        if uid not in wanted_uids or not isinstance(raw_state, dict):
            continue
        revision = raw_state.get("revision")
        if isinstance(revision, int):
            revisions[uid] = int(revision)
    return revisions


def _retry_local_runtime_push_after_conflict(
    config: AgentConfig,
    previous_local_runtime: Mapping[str, Any],
    current_local_runtime: Mapping[str, Any],
    installs: list[object],
    runtime_metadata: Mapping[str, Any] | None = None,
    instance_revisions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch current MCC state and retry the local delta on its new revision."""
    instance_uids = sorted(
        {
            str(getattr(inst, "instance_uid", "") or "").strip()
            for inst in installs
            if str(getattr(inst, "instance_uid", "") or "").strip()
        }
    )
    fresh = fetch_runtime_overrides(config, instance_uids=instance_uids)
    if str(fresh.get("status", "")).strip().lower() != "ok":
        return fresh

    latest_host = fresh.get("runtime_overrides")
    latest_host = latest_host if isinstance(latest_host, dict) else {}
    latest_instance_states = fresh.get("instance_desired_states")
    latest = merge_instance_desired_states(latest_host, latest_instance_states)
    rebased, desired_instances = _rebase_local_instance_runtime_states(
        latest,
        latest_instance_states,
        previous_local_runtime,
        current_local_runtime,
        installs,
        canonical_uids=set(instance_revisions or {}),
    )

    desired_state = fresh.get("desired_state")
    desired_revision = desired_state.get("revision") if isinstance(desired_state, dict) else None
    if not isinstance(desired_revision, int):
        desired_revision = None
    desired_uids = set(desired_instances)
    fresh_instance_revisions = _instance_state_revisions(
        latest_instance_states,
        desired_uids,
    )
    for uid, revision in (instance_revisions or {}).items():
        if uid in desired_uids and uid not in fresh_instance_revisions and isinstance(revision, int):
            fresh_instance_revisions[str(uid)] = int(revision)
    return push_runtime_overrides(
        config,
        rebased,
        merge=False,
        target="desired",
        desired_state_revision=desired_revision,
        instance_desired_states=desired_instances,
        instance_desired_state_revisions=fresh_instance_revisions,
        runtime_change_metadata=dict(runtime_metadata) if isinstance(runtime_metadata, Mapping) else None,
    )


def _push_local_runtime_change(
    config: AgentConfig,
    previous_local_runtime: Mapping[str, Any],
    current_local_runtime: Mapping[str, Any],
    installs: list[object],
    *,
    desired_revision: int | None,
    instance_revisions: Mapping[str, Any],
    runtime_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Push a local edit on a fresh snapshot, retrying once on conflict."""
    instance_uids = sorted(
        {
            str(getattr(inst, "instance_uid", "") or "").strip()
            for inst in installs
            if str(getattr(inst, "instance_uid", "") or "").strip()
        }
    )
    fresh = fetch_runtime_overrides(config, instance_uids=instance_uids)
    if str(fresh.get("status", "")).strip().lower() != "ok":
        return fresh

    latest_host = fresh.get("runtime_overrides")
    latest_host = latest_host if isinstance(latest_host, dict) else {}
    latest_instance_states = fresh.get("instance_desired_states")
    latest = merge_instance_desired_states(latest_host, latest_instance_states)
    rebased, desired_instances = _rebase_local_instance_runtime_states(
        latest,
        latest_instance_states,
        previous_local_runtime,
        current_local_runtime,
        installs,
        canonical_uids=set(instance_revisions),
    )
    fresh_desired_state = fresh.get("desired_state")
    fresh_desired_revision = (
        fresh_desired_state.get("revision")
        if isinstance(fresh_desired_state, dict)
        else None
    )
    if not isinstance(fresh_desired_revision, int):
        fresh_desired_revision = desired_revision
    fresh_instance_revisions = _instance_state_revisions(
        latest_instance_states,
        set(desired_instances),
    )
    # A fresh canonical response supersedes the stored revision.  Keep a
    # stored per-instance revision only for a local row absent from the
    # response, which can occur while a newly-created install is registering.
    for uid, revision in instance_revisions.items():
        if uid in desired_instances and uid not in fresh_instance_revisions and isinstance(revision, int):
            fresh_instance_revisions[str(uid)] = int(revision)
    pushed = push_runtime_overrides(
        config,
        rebased,
        merge=False,
        target="desired",
        desired_state_revision=fresh_desired_revision,
        instance_desired_states=desired_instances,
        instance_desired_state_revisions=fresh_instance_revisions,
        runtime_change_metadata=dict(runtime_metadata) if isinstance(runtime_metadata, Mapping) else None,
    )
    if str(pushed.get("status", "")).strip().lower() != "conflict":
        return pushed
    rebased = _retry_local_runtime_push_after_conflict(
        config,
        previous_local_runtime,
        current_local_runtime,
        installs,
        runtime_metadata=runtime_metadata,
        instance_revisions=instance_revisions,
    )
    result = dict(rebased)
    result["_local_rebase_attempted"] = True
    return result


def _store_desired_runtime_push_result(
    store: object,
    pushed: Mapping[str, Any],
    *,
    accepted_runtime: Mapping[str, Any] | None = None,
    runtime_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Persist the accepted desired revision and its per-instance revisions."""
    if str(pushed.get("status", "")).strip().lower() != "ok":
        return False
    state = pushed.get("desired_state")
    if not isinstance(state, dict):
        return False
    revision = state.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return False
    state_for_store = dict(state)
    pushed_instances = pushed.get("instance_desired_states")
    instance_revisions = (
        {
            str(uid): int(item["revision"])
            for uid, item in pushed_instances.items()
            if isinstance(item, dict)
            and isinstance(item.get("revision"), int)
            and not isinstance(item.get("revision"), bool)
        }
        if isinstance(pushed_instances, dict)
        else {}
    )
    state_for_store["instance_revisions"] = instance_revisions
    if isinstance(pushed_instances, dict):
        state_for_store["instance_desired_states"] = deepcopy(pushed_instances)
    store.put_runtime_sync("mcc_runtime_desired_state", state_for_store)
    pushed_runtime = pushed.get("runtime_overrides")
    if not isinstance(pushed_runtime, dict):
        pushed_runtime = state.get("runtime_overrides") if isinstance(state.get("runtime_overrides"), dict) else None
    if not isinstance(pushed_runtime, dict):
        pushed_runtime = deepcopy(dict(accepted_runtime)) if isinstance(accepted_runtime, Mapping) else {}
    accepted_runtime_hint = deepcopy(dict(accepted_runtime)) if isinstance(accepted_runtime, Mapping) else {}
    accepted_snapshot = merge_instance_desired_states(pushed_runtime, pushed_instances)
    accepted_snapshot = _merge_missing_runtime_values(accepted_snapshot, accepted_runtime_hint)
    store.put_runtime_sync("mcc_runtime", accepted_snapshot)
    pushed_metadata = pushed.get("runtime_metadata")
    if not isinstance(pushed_metadata, dict):
        pushed_metadata = pushed.get("field_metadata")
    if not isinstance(pushed_metadata, dict):
        pushed_metadata = runtime_metadata if isinstance(runtime_metadata, Mapping) else None
    accepted_metadata = _runtime_metadata_for_snapshot(
        pushed_metadata if isinstance(pushed_metadata, Mapping) else {},
        accepted_snapshot,
        source="mcc" if isinstance(pushed_metadata, Mapping) else "mcd",
        revision=revision,
    )
    store.put_runtime_sync(_MCC_RUNTIME_METADATA_KEY, accepted_metadata)
    store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, accepted_metadata)
    store.put_runtime_sync(
        _LOCAL_RUNTIME_ACCEPTED_KEY,
        {
            "runtime": accepted_snapshot,
            "host_revision": int(revision),
            "instance_revisions": instance_revisions,
            "metadata": accepted_metadata,
        },
    )
    return True


_CAMPAIGN_EMAIL_COUNTER_RECONCILE_RECENT_SEC = 14 * 86400
_CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_SEC = 15 * 60
# Avoid spending an entire scheduler pass probing stale audit candidates.
_CAMPAIGN_TRIGGER_STALE_SCAN_CAP = 8
_PRIORITY_DISPATCH_REGULAR_CHUNK = 8
_TASK_LOCK_BUSY_RC = 75
_CAMPAIGN_EXACT_TASK_TYPES = frozenset({"campaign_update", "campaign_rebuild", "campaign_trigger"})
_STATIC_SEGMENT_TASK_TYPES = frozenset({"segment_priority", "segment_super_priority"})
_SEGMENT_RUNTIME_TASK_TYPES = frozenset({"segment", "segment_sql", "import", *_STATIC_SEGMENT_TASK_TYPES})
_SEGMENT_RUNTIME_MARKER_SCHEMA = "mcd-segment-runtime-marker-v1"
_SEGMENT_RUNTIME_MARKER_PREFIX = "segment_runtime:"
_SEGMENT_ORPHAN_GRACE_SEC = 60
_IMPORT_FAST_FOLLOW_SEC = 60
_ENTITY_LAUNCH_GUARD: dict[str, float] = {}
_CAMPAIGN_FALLBACK_COORDINATOR_LOCK = threading.RLock()
_CAMPAIGN_FALLBACK_ACTIVE_ROOTS: set[str] = set()
_CAMPAIGN_DISPATCHING_ROOTS: dict[str, int] = {}
_CAMPAIGN_TRIGGER_STUCK_UNTIL: dict[tuple[str, int], tuple[float, str]] = {}
_CAMPAIGN_TRIGGER_FUTURE_WAKE: dict[tuple[str, int], tuple[float, str]] = {}
_CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED: dict[tuple[str, int], str] = {}
_CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT: dict[tuple[str, int], float] = {}
_CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT: dict[tuple[str, int], float] = {}
_SEGMENT_FINISHED_AT: dict[tuple[str, int], float] = {}
_SEGMENT_CYCLE_LOCK = threading.RLock()
_SEGMENT_STATIC_LANE_LOCK = threading.RLock()
_SEGMENT_ADMISSION_CONTEXT: dict[str, dict[str, object]] = {}
_SEGMENT_DISPATCH_WAKEUP: threading.Event | None = None
_ACTIVE_PRIORITY_EXECUTOR: Any | None = None
_CAMPAIGN_REBUILD_FINISHED_AT: dict[tuple[str, int], float] = {}
_SEGMENT_FILTER_WARN_TS: dict[tuple[str, str], float] = {}
_SEGMENT_FAILURE_WARN_TS: dict[tuple[str, int], float] = {}
_SEGMENT_PENDING_GUARD_LOG_TS: dict[str, float] = {}
_IMPORT_SETTLE_UNTIL: dict[str, float] = {}
_IMPORT_PENDING_OVERRIDE_WARN_TS: dict[str, float] = {}
_SQL_IMPORT_PENDING_STATUS_COUNT = (
    "SELECT COUNT(*) AS cnt FROM {prefix}imports "
    "WHERE is_published = 1 "
    "AND (status IN (1,7) "
    "OR LOWER(CAST(status AS CHAR)) IN ('queued','pending','delayed')) "
    "AND date_ended IS NULL "
    "AND (date_started IS NULL "
    "OR CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(properties, '$.line')), '0') AS UNSIGNED) < line_count)"
)
# Read-only queue observability. The configured sql_mail_queue_count remains
# the scheduler throttle policy; these queries report total and sendable-now
# rows independently to MCC.
_MAIL_QUEUE_TOTAL_COUNT = (
    "SELECT COUNT(*) AS cnt FROM {prefix}message_queue "
    "WHERE success = 0 AND date_sent IS NULL "
    "AND status IN ('pending', 'rescheduled')"
)
_MAIL_QUEUE_AVAILABLE_COUNT = (
    "SELECT COUNT(*) AS cnt FROM {prefix}message_queue "
    "WHERE success = 0 AND date_sent IS NULL AND status = 'pending' "
    "AND attempts < max_attempts "
    "AND (scheduled_date IS NULL OR scheduled_date <= '{now_utc}')"
)
_MAILRU_POSTMASTER_LATEST_ROWS = (
    "SELECT domain, stat_date, messages_sent, spam_percent, "
    "probably_spam_percent, synced_at "
    "FROM {prefix}mailru_postmaster_stats "
    "WHERE is_tracked = 1 AND messages_sent > 0 "
    "ORDER BY domain ASC, stat_date DESC, synced_at DESC"
)
_MAILRU_POSTMASTER_SPAM_STOP_PERCENT = 3.0
_SQL_IMPORT_MONITOR_ROWS = (
    "SELECT id, status, date_added, date_started, date_ended, properties "
    "FROM {prefix}imports "
    "WHERE is_published = 1 "
    "AND (status IN (1,2,3,4,5,6,7) "
    "OR LOWER(CAST(status AS CHAR)) IN ("
    "'queued','pending','in_progress','processing','running','delayed',"
    "'imported','completed','complete','done','success','failed','error','stopped','cancelled','canceled'"
    ") "
    "OR date_ended >= DATE_SUB('{now_utc}', INTERVAL 10 MINUTE)) "
    "ORDER BY "
    "CASE "
    "WHEN status IN (2) OR LOWER(CAST(status AS CHAR)) IN ('in_progress','processing','running') THEN 0 "
    "WHEN status IN (7) OR LOWER(CAST(status AS CHAR)) = 'delayed' THEN 1 "
    "WHEN status IN (1) OR LOWER(CAST(status AS CHAR)) IN ('queued','pending') THEN 2 "
    "ELSE 3 END, "
    "COALESCE(date_started, date_added, date_ended) DESC, id DESC "
    "LIMIT 80"
)


def _mailru_postmaster_metrics(db: MauticDB, *, now_ts: float) -> dict[str, object]:
    """Return the newest visible status for each tracked DKIM domain."""
    try:
        rows = db.fetch_rows(_MAILRU_POSTMASTER_LATEST_ROWS, limit=5000)
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc)[:300],
            "updated_at_ts": float(now_ts),
        }

    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower().rstrip(".")
        if not domain or not re.fullmatch(r"[a-z0-9.-]+", domain) or domain in latest:
            continue
        try:
            spam_percent = float(row.get("spam_percent") or 0.0)
        except (TypeError, ValueError):
            spam_percent = 0.0
        try:
            probably_spam_percent = float(row.get("probably_spam_percent") or 0.0)
        except (TypeError, ValueError):
            probably_spam_percent = 0.0
        blocked = spam_percent >= _MAILRU_POSTMASTER_SPAM_STOP_PERCENT
        latest[domain] = {
            "domain": domain,
            "stat_date": str(row.get("stat_date") or ""),
            "messages_sent": max(0, int(row.get("messages_sent") or 0)),
            "spam_percent": spam_percent,
            "probably_spam_percent": probably_spam_percent,
            "synced_at": str(row.get("synced_at") or ""),
            "state": "blocked" if blocked else "eligible",
            "reason": (
                f"spam_percent >= {_MAILRU_POSTMASTER_SPAM_STOP_PERCENT:.2f}%"
                if blocked
                else "spam_percent below stop threshold"
            ),
        }

    domains = sorted(latest.values(), key=lambda item: str(item.get("domain") or ""))
    blocked_count = sum(1 for item in domains if item.get("state") == "blocked")
    return {
        "available": True,
        "threshold_percent": _MAILRU_POSTMASTER_SPAM_STOP_PERCENT,
        "domains": domains,
        "blocked_count": blocked_count,
        "eligible_count": max(0, len(domains) - blocked_count),
        "updated_at_ts": float(now_ts),
    }


@dataclass(frozen=True)
class CampaignTriggerProgressSnapshot:
    due_event_logs: int
    due_root_actions: int
    due_no_action_branches: int
    pending_event_logs: int
    triggered_event_logs: int
    max_triggered_at: str
    next_scheduled_at: str = ""
    sent_email_stats: int = 0
    max_email_sent_at: str = ""

    @property
    def due_total(self) -> int:
        return (
            max(0, int(self.due_event_logs or 0))
            + max(0, int(self.due_root_actions or 0))
            + max(0, int(self.due_no_action_branches or 0))
        )

    def progress_key(self) -> tuple[int, int, int, int, int, str, str, int, str]:
        return (
            self.due_total,
            max(0, int(self.due_event_logs or 0)),
            max(0, int(self.due_no_action_branches or 0)),
            max(0, int(self.pending_event_logs or 0)),
            max(0, int(self.triggered_event_logs or 0)),
            str(self.max_triggered_at or ""),
            str(self.next_scheduled_at or ""),
            max(0, int(self.sent_email_stats or 0)),
            str(self.max_email_sent_at or ""),
        )


_SQL_SEGMENTS_ALL_PUBLISHED = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)
_SQL_SEGMENTS_LATE_DISCOVERY = (
    "SELECT ll.id, ll.date_added, ll.date_modified, ll.last_built_date "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "AND ("
    "  ll.last_built_date IS NULL "
    "  OR ll.date_added >= '{active_cycle_admitted_at}'"
    ") "
    "ORDER BY COALESCE(ll.date_added, ll.date_modified) ASC, ll.id ASC "
    "LIMIT 100"
)
_SQL_CAMPAIGNS_ALL_PUBLISHED = (
    "SELECT c.id "
    "FROM {prefix}campaigns c "
    "WHERE c.is_published = 1 "
    "AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
    "AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
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
    "backup_cluster_storage_mode",
    "backup_cluster_local_xtrabackup_enabled",
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
_PLUGIN_OPERATION_STABLE_RUNTIME_KEYS = {
    "plugin_operation_instance_settings",
}
_MESSAGE_QUEUE_STABLE_RUNTIME_KEYS = {
    "message_queue_enabled",
    "message_queue_interval_sec",
    "message_queue_instance_settings",
}
_SEGMENT_WHITELIST_STABLE_RUNTIME_KEYS = {
    "segment_whitelist_instance_settings",
    "segment_priority_instance_settings",
    "segment_super_priority_instance_settings",
    "campaign_whitelist_instance_settings",
    "segment_recurring_priority_v1",
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
_FORM_EMBED_STABLE_RUNTIME_KEYS = {
    "form_embed_instance_settings",
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
    | _PLUGIN_OPERATION_STABLE_RUNTIME_KEYS
    | _MESSAGE_QUEUE_STABLE_RUNTIME_KEYS
    | _SEGMENT_WHITELIST_STABLE_RUNTIME_KEYS
    | _EMPTY_LEADS_CLEANUP_STABLE_RUNTIME_KEYS
    | _PAGE_HITS_ORPHAN_CLEANUP_STABLE_RUNTIME_KEYS
    | _MONITORED_EMAIL_PARSER_STABLE_RUNTIME_KEYS
    | _FORM_EMBED_STABLE_RUNTIME_KEYS
    | _CLUSTER_STABLE_RUNTIME_KEYS
)
_SERVICE_CLEANUP_RUNTIME_KEYS = (
    _EMPTY_LEADS_CLEANUP_STABLE_RUNTIME_KEYS
    | _PAGE_HITS_ORPHAN_CLEANUP_STABLE_RUNTIME_KEYS
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


def _segment_runtime_marker_key(root: str, entity_id: int) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()[:24]
    return f"{_SEGMENT_RUNTIME_MARKER_PREFIX}{digest}:{int(entity_id)}"


def _segment_runtime_marker_load(
    store: "TaskStore",
    root: str,
    entity_id: int | None,
) -> dict[str, object]:
    if entity_id is None:
        return {}
    try:
        sid = int(entity_id)
    except (TypeError, ValueError):
        return {}
    if sid <= 0 or not hasattr(store, "get_runtime_sync"):
        return {}
    raw = store.get_runtime_sync(_segment_runtime_marker_key(root, sid))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _segment_runtime_marker_write(
    store: "TaskStore",
    task: "RunningTask",
    *,
    status: str,
    now_ts: float,
    note: str | None = None,
    pid: int | None = None,
    command_str: str | None = None,
) -> None:
    """Persist one segment liveness marker.

    ``last_independent_at`` is written only by callers that have independently
    confirmed a process/DB heartbeat (a launch or a live-process/DB probe).
    Marker-only observations must never refresh that timestamp; otherwise a
    dead process could keep its own orphan grace period alive forever.
    """

    if task.entity_id is None or task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES:
        return
    if not hasattr(store, "get_runtime_sync") or not hasattr(store, "put_runtime_sync"):
        return
    marker = _segment_runtime_marker_load(store, task.root, task.entity_id)
    cycle_state = store.get_runtime_sync(_segment_cycle_key(task.root))
    marker.update(
        {
            "schema": _SEGMENT_RUNTIME_MARKER_SCHEMA,
            "root": str(task.root),
            "task_type": str(task.task_type),
            "entity_id": int(task.entity_id),
            "status": str(status or "running"),
            "pid": int(pid if pid is not None else task.pid),
            "command_str": str(command_str if command_str is not None else task.command_str),
            "started_at": float(task.started_at),
            "heartbeat_at": float(now_ts),
            "updated_at": float(now_ts),
        }
    )
    if status == "running":
        if isinstance(cycle_state, Mapping):
            cycle_id = str(cycle_state.get("cycle_id") or "").strip()
            if cycle_id:
                marker["cycle_id"] = cycle_id
            try:
                marker["cycle_admitted_at"] = float(cycle_state.get("admitted_at") or 0.0)
            except (TypeError, ValueError):
                marker.pop("cycle_admitted_at", None)
        pid_start_time = _pid_start_time(int(pid if pid is not None else task.pid))
        if pid_start_time is not None:
            marker["pid_start_time"] = int(pid_start_time)
        marker["last_independent_at"] = float(now_ts)
        marker.pop("lost_at", None)
        marker.pop("terminal_at", None)
        marker.pop("terminal_note", None)
    else:
        marker["terminal_at"] = float(now_ts)
        if note:
            marker["terminal_note"] = str(note)[:500]
    store.put_runtime_sync(_segment_runtime_marker_key(task.root, int(task.entity_id)), marker)


def _segment_runtime_marker_orphan_active(
    store: "TaskStore",
    task: "RunningTask",
    *,
    now_ts: float,
    grace_sec: int = _SEGMENT_ORPHAN_GRACE_SEC,
) -> bool:
    """Keep a claim briefly after independent runtime evidence disappears.

    The grace window is anchored to the last independently confirmed
    heartbeat, never to the current poll. Persisting ``lost_at`` is only a
    bookkeeping step; it does not update ``heartbeat_at`` or the anchor.
    """

    if task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES or task.entity_id is None:
        return False
    marker = _segment_runtime_marker_load(store, task.root, task.entity_id)
    if str(marker.get("status") or "").strip().lower() != "running":
        return False
    try:
        anchor = float(
            marker.get("last_independent_at")
            or marker.get("heartbeat_at")
            or marker.get("started_at")
            or task.started_at
        )
    except (TypeError, ValueError):
        anchor = float(task.started_at or now_ts)
    try:
        lost_at = float(marker.get("lost_at") or anchor)
    except (TypeError, ValueError):
        lost_at = anchor
    if not marker.get("lost_at"):
        marker["lost_at"] = float(lost_at)
        marker["updated_at"] = float(now_ts)
        store.put_runtime_sync(_segment_runtime_marker_key(task.root, int(task.entity_id)), marker)
    return float(now_ts) - lost_at <= max(1, int(grace_sec or 1))


_SEGMENT_CHAIN_RUNTIME_SCHEMA = "mcd-segment-execution-chain-v1"
_SEGMENT_CHAIN_RUNTIME_PREFIX = "segment_chain:"
_SEGMENT_NATIVE_COVERAGE_SCHEMA = "mcd-segment-native-coverage-v1"
_SEGMENT_NATIVE_COVERAGE_PREFIX = "segment_native_coverage:"
_SEGMENT_NATIVE_CAPABILITY_SCHEMA = "mcd-segment-native-capability-v1"
_SEGMENT_NATIVE_CAPABILITY_PREFIX = "segment_native_capability:"


def _segment_chain_runtime_key(root: str, owner_id: int) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()[:24]
    return f"{_SEGMENT_CHAIN_RUNTIME_PREFIX}{digest}:{int(owner_id)}"


def _segment_chain_runtime_payload_valid(
    root: str,
    payload: Mapping[str, object],
    *,
    member_id: int | None = None,
) -> bool:
    """Reject legacy reverse-index hints that do not belong to this root.

    The storage key is root-scoped, but old payloads were persisted before
    that scope was consistently enforced.  A stale value under a valid key
    is only metadata; it must never make a foreign process look like a live
    owner of this root's closure.
    """

    if payload.get("schema") != _SEGMENT_CHAIN_RUNTIME_SCHEMA:
        return False
    canonical_root = str(root or "").strip()
    if not canonical_root or str(payload.get("root") or "").strip() != canonical_root:
        return False
    try:
        owner_id = int(payload.get("owner_id") or 0)
    except (TypeError, ValueError):
        return False
    if owner_id <= 0:
        return False
    entity_ids: list[int] = []
    raw_ids = payload.get("entity_ids")
    for raw_id in raw_ids if isinstance(raw_ids, (list, tuple, set)) else []:
        try:
            entity_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if entity_id > 0 and entity_id not in entity_ids:
            entity_ids.append(entity_id)
    if owner_id not in entity_ids:
        return False
    if member_id is not None:
        try:
            requested_id = int(member_id)
        except (TypeError, ValueError):
            return False
        if requested_id <= 0 or requested_id not in entity_ids:
            return False
    task_key = str(payload.get("task_key") or "").strip()
    if task_key:
        task_parts = task_key.rsplit("|", 2)
        if len(task_parts) != 3 or task_parts[0] != canonical_root:
            return False
        if not task_parts[1].startswith("segment"):
            return False
        try:
            task_owner_id = int(task_parts[2])
        except (TypeError, ValueError):
            return False
        if task_owner_id != owner_id:
            return False
    return True


def _segment_native_coverage_key(root: str) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()[:24]
    return f"{_SEGMENT_NATIVE_COVERAGE_PREFIX}{digest}"


def _segment_dispatch_context_parts(
    store: "TaskStore",
    root: str,
) -> tuple[Mapping[str, object], int, dict[int, set[int]], dict[int, set[int]]]:
    raw_context = store.get_runtime_sync(_segment_dispatch_context_key(root))
    context = raw_context if isinstance(raw_context, Mapping) else {}

    def _relation_map(raw: object) -> dict[int, set[int]]:
        if not isinstance(raw, Mapping):
            return {}
        result: dict[int, set[int]] = {}
        for raw_key, raw_values in raw.items():
            try:
                key = int(raw_key)
            except (TypeError, ValueError):
                continue
            if key <= 0:
                continue
            values: set[int] = set()
            for raw_value in raw_values if isinstance(raw_values, (list, tuple, set)) else []:
                try:
                    value = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    values.add(value)
            result[key] = values

        return result

    try:
        major = int(context.get("mautic_major") or 6)
    except (TypeError, ValueError):
        major = 6
    return (
        context,
        major,
        _relation_map(context.get("parents_by_child")),
        _relation_map(context.get("children_by_parent")),
    )


def _segment_native_coverage_load(
    store: "TaskStore",
    root: str,
) -> dict[str, object]:
    if not hasattr(store, "get_runtime_sync"):
        return {}
    try:
        raw = store.get_runtime_sync(_segment_native_coverage_key(root))
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _segment_native_covered_ids(
    store: "TaskStore",
    root: str,
) -> set[int]:
    payload = _segment_native_coverage_load(store, root)
    if payload.get("schema") != _SEGMENT_NATIVE_COVERAGE_SCHEMA:
        return set()
    try:
        cycle = store.get_runtime_sync(_segment_cycle_key(str(root)))
    except Exception:
        cycle = None
    cycle_id = str(cycle.get("cycle_id") or "") if isinstance(cycle, Mapping) else ""
    if not cycle_id or str(payload.get("cycle_id") or "") != cycle_id:
        return set()
    return {
        int(raw_id)
        for raw_id in payload.get("covered_ids") or []
        if str(raw_id).strip().lstrip("-").isdigit() and int(raw_id) > 0
    }


def _segment_native_coverage_record(
    store: "TaskStore",
    *,
    root: str,
    entity_id: int,
    success: bool,
    now_ts: float,
) -> None:
    """Persist successful M7 native traversal coverage for current cycle.

    Mautic 7's ``-i`` command traverses the requested root's parents.  The
    durable coverage receipt lets a later overlapping root pass only those
    already-successful parents via repeated ``--exclude=<id>`` options; a
    failed or timed-out root never contributes coverage.
    """

    if not success or not hasattr(store, "put_runtime_sync"):
        return
    try:
        context, major, parents_by_child, _children_by_parent = _segment_dispatch_context_parts(store, root)
    except Exception:
        return
    if major < 7:
        return
    try:
        cycle = store.get_runtime_sync(_segment_cycle_key(str(root)))
    except Exception:
        cycle = None
    if not isinstance(cycle, Mapping):
        return
    cycle_id = str(cycle.get("cycle_id") or "")
    if not cycle_id:
        return
    covered = dependency_segment_closure({int(entity_id)}, parents_by_child) | {int(entity_id)}
    with _SEGMENT_CYCLE_LOCK:
        payload = _segment_native_coverage_load(store, root)
        if (
            payload.get("schema") != _SEGMENT_NATIVE_COVERAGE_SCHEMA
            or str(payload.get("cycle_id") or "") != cycle_id
        ):
            payload = {
                "schema": _SEGMENT_NATIVE_COVERAGE_SCHEMA,
                "root": str(root),
                "cycle_id": cycle_id,
                "covered_ids": [],
                "receipts": {},
            }
        covered_ids = {
            int(raw_id)
            for raw_id in payload.get("covered_ids") or []
            if str(raw_id).strip().lstrip("-").isdigit() and int(raw_id) > 0
        }
        covered_ids.update(covered)
        receipts = dict(payload.get("receipts") or {}) if isinstance(payload.get("receipts"), Mapping) else {}
        receipts[str(int(entity_id))] = {
            "covered_ids": sorted(covered),
            "success": True,
            "updated_at": float(now_ts),
        }
        payload.update(
            {
                "covered_ids": sorted(covered_ids),
                "receipts": receipts,
                "updated_at": float(now_ts),
                "last_root_id": int(entity_id),
            }
        )
        store.put_runtime_sync(_segment_native_coverage_key(root), payload)


def _append_segment_native_excludes(args: list[str], exclude_ids: Iterable[int]) -> list[str]:
    normalized: list[int] = []
    for raw_id in exclude_ids:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in normalized:
            normalized.append(sid)
    if not normalized:
        return list(args)
    out = list(args)
    insert_at = next(
        (index for index, value in enumerate(out) if value == "--no-interaction"),
        len(out),
    )
    for sid in normalized:
        out[insert_at:insert_at] = [f"--exclude={sid}"]
        insert_at += 1
    return out


def _segment_native_exclude_capability(
    store: "TaskStore",
    *,
    root: str,
    config: "AgentConfig",
    mautic_major: int,
) -> bool:
    """Read-only probe for Mautic 7's repeatable native ``--exclude`` option."""

    if int(mautic_major or 0) < 7:
        return False
    key = f"{_SEGMENT_NATIVE_CAPABILITY_PREFIX}{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:24]}"
    try:
        cached = store.get_runtime_sync(key)
    except Exception:
        cached = None
    if (
        isinstance(cached, Mapping)
        and cached.get("schema") == _SEGMENT_NATIVE_CAPABILITY_SCHEMA
        and int(cached.get("mautic_major") or 0) == int(mautic_major)
        and isinstance(cached.get("supported"), bool)
    ):
        return bool(cached.get("supported"))

    command: list[str] = []
    supported = False
    status = "probe_failed"
    output = ""
    try:
        command = render_mautic_command(
            php_bin=str(getattr(config, "php_bin", "php") or "php"),
            run_as_user=str(getattr(config, "mautic_run_as_user", "") or "") or None,
            root=str(root),
            template="help mautic:segments:update --format=json",
        )
        probe = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = f"{probe.stdout or ''}\n{probe.stderr or ''}"
        compact = re.sub(r"\s+", "", output)
        supported = (
            probe.returncode == 0
            and '"name":"--exclude"' in compact
            and '"is_multiple":true' in compact
        )
        status = "supported" if supported else "unsupported"
    except Exception as exc:
        status = "probe_failed"
        output = str(exc)
    payload = {
        "schema": _SEGMENT_NATIVE_CAPABILITY_SCHEMA,
        "root": str(root),
        "mautic_major": int(mautic_major),
        "command": shlex.join(command) if command else "",
        "read_only": True,
        "supported": bool(supported),
        "status": status,
        "output_sha256": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
        "checked_at": float(time.time()),
    }
    if hasattr(store, "put_runtime_sync"):
        try:
            store.put_runtime_sync(key, payload)
        except Exception:
            pass
    return bool(supported)


def _segment_chain_runtime_load(
    store: "TaskStore",
    root: str,
    owner_id: int | None,
) -> dict[str, object]:
    if owner_id is None or not hasattr(store, "get_runtime_sync"):
        return {}
    try:
        sid = int(owner_id)
    except (TypeError, ValueError):
        return {}
    if sid <= 0:
        return {}
    raw = store.get_runtime_sync(_segment_chain_runtime_key(root, sid))
    if not isinstance(raw, Mapping):
        return {}
    payload = dict(raw)
    return payload if _segment_chain_runtime_payload_valid(root, payload, member_id=sid) else {}


def _segment_chain_entity_ids(
    store: "TaskStore",
    root: str,
    owner_id: int | None,
) -> list[int]:
    """Return the durable IDs owned by one currently running chain worker."""

    try:
        owner = int(owner_id) if owner_id is not None else 0
    except (TypeError, ValueError):
        owner = 0
    if owner <= 0:
        return []
    payload = _segment_chain_runtime_load(store, root, owner)
    if payload.get("schema") != _SEGMENT_CHAIN_RUNTIME_SCHEMA:
        return [owner]
    try:
        cycle_state = store.get_runtime_sync(_segment_cycle_key(str(root)))
    except Exception:
        cycle_state = None
    current_cycle_id = (
        str(cycle_state.get("cycle_id") or "").strip()
        if isinstance(cycle_state, Mapping)
        else ""
    )
    payload_cycle_id = str(payload.get("cycle_id") or "").strip()
    if payload_cycle_id and current_cycle_id and payload_cycle_id != current_cycle_id:
        return [owner]
    raw_ids = payload.get("entity_ids")
    ids: list[int] = []
    for raw_id in raw_ids if isinstance(raw_ids, (list, tuple, set)) else []:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in ids:
            ids.append(sid)
    if not ids or owner not in ids:
        return [owner]
    if isinstance(cycle_state, Mapping):
        cycle_ids = set(
            _normalize_segment_ids(
                [*(cycle_state.get("items") or []), *(cycle_state.get("follow_up") or [])]
            )
        )
        if cycle_ids and owner in cycle_ids:
            scoped_ids = [sid for sid in ids if sid in cycle_ids]
            if owner not in scoped_ids:
                return [owner]
            ids = scoped_ids
    return ids


def _segment_chain_runtime_write(
    store: "TaskStore",
    *,
    root: str,
    owner_id: int,
    entity_ids: Iterable[int],
    status: str,
    now_ts: float,
    task: "RunningTask" | None = None,
    command_str: str = "",
) -> None:
    """Persist the chain owner/IDs so restart adoption retains whole closure."""

    if not hasattr(store, "put_runtime_sync"):
        return
    normalized = []
    for raw_id in entity_ids:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in normalized:
            normalized.append(sid)
    if not normalized:
        normalized = [int(owner_id)]
    if task is not None:
        task_root = str(task.root or "").strip()
        task_key_root = str(task.task_key or "").rsplit("|", 2)[0]
        if task_root != str(root or "").strip() or task_key_root != str(root or "").strip():
            logging.warning(
                "[%s] refusing foreign segment chain marker owner=%s task_root=%s task_key_root=%s",
                root,
                owner_id,
                task_root,
                task_key_root,
            )
            return
    payload = _segment_chain_runtime_load(store, root, int(owner_id))
    payload.update(
        {
            "schema": _SEGMENT_CHAIN_RUNTIME_SCHEMA,
            "root": str(root),
            "owner_id": int(owner_id),
            "entity_ids": normalized,
            "status": str(status or "running"),
            "updated_at": float(now_ts),
        }
    )
    if str(status or "").strip().lower() in {"running", "admitting"}:
        cycle_state = store.get_runtime_sync(_segment_cycle_key(root))
        if isinstance(cycle_state, Mapping):
            cycle_id = str(cycle_state.get("cycle_id") or "").strip()
            if cycle_id:
                payload["cycle_id"] = cycle_id
            try:
                payload["cycle_admitted_at"] = float(cycle_state.get("admitted_at") or 0.0)
            except (TypeError, ValueError):
                payload.pop("cycle_admitted_at", None)
    if task is not None:
        payload.update(
            {
                "task_key": str(task.task_key),
                "task_type": str(task.task_type),
                "pid": int(task.pid),
                "started_at": float(task.started_at),
                "command_str": str(task.command_str),
            }
        )
    elif command_str:
        payload["command_str"] = str(command_str)
    if str(status or "").strip().lower() in {"running", "admitting"} and task is not None:
        pid_start_time = _pid_start_time(int(task.pid))
        if pid_start_time is not None:
            payload["pid_start_time"] = int(pid_start_time)
    # Keep a small reverse index under every member as well as under the
    # owner.  Restart/liveness reconciliation can then find the one worker
    # from any claimed closure member without adding a second TaskStore row.
    for chain_id in normalized:
        store.put_runtime_sync(_segment_chain_runtime_key(root, int(chain_id)), payload)


def _segment_chain_from_dispatch_context(
    store: "TaskStore",
    root: str,
    candidate_ids: Iterable[int],
    entity_id: int,
) -> list[int]:
    """Resolve one pending ring item to its chain owner and sequence.

    A full-cycle ring can contain an important parent beside a fresh
    ``remaining`` descendant.  M6 dependency connectivity alone must not
    collapse those two tiers into one worker: the descendant is not needed to
    execute the parent and would otherwise bypass the durable important gate.
    Project the connected graph onto the owner's tier, while retaining any
    pending ancestors that are required to execute that owner.  The planner
    normally promotes those ancestors durably; retaining them here is the
    fail-closed path for a partially upgraded cycle state.
    """

    candidates = []
    for raw_id in candidate_ids:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in candidates:
            candidates.append(sid)
    if int(entity_id) not in candidates or not hasattr(store, "get_runtime_sync"):
        return [int(entity_id)]
    context, major, parents_by_child, children_by_parent = _segment_dispatch_context_parts(store, root)
    eligible_ids: set[int] | None = None
    if major < 7:
        try:
            cycle_state = store.get_runtime_sync(_segment_cycle_key(str(root)))
        except Exception:
            cycle_state = None
        owner_tier = _segment_cycle_item_tier(cycle_state, int(entity_id))
        if owner_tier in {"important", "remaining"}:
            eligible_ids = {
                int(sid)
                for sid in candidates
                if _segment_cycle_item_tier(cycle_state, int(sid)) == owner_tier
            }
            # A required pending ancestor is part of the owner's executable
            # closure even if an older persisted state failed to classify it
            # with the same tier.  Descendants are deliberately not added.
            eligible_ids.update(
                int(sid)
                for sid in dependency_segment_closure({int(entity_id)}, parents_by_child)
                if int(sid) in candidates
            )
            eligible_ids.add(int(entity_id))
    chains = dependency_execution_chains(
        candidates,
        parents_by_child,
        children_by_parent,
        mautic_major=major,
        eligible_ids=eligible_ids,
    )
    for chain in chains:
        if int(entity_id) in chain:
            return chain
    return [int(entity_id)]


def _segment_chain_command_args(
    build_args: Callable[[int], list[str]],
    entity_ids: list[int],
    *,
    exclude_ids: Iterable[int] | None = None,
    command_entity_ids: Iterable[int] | None = None,
    exclude_ids_by_entity: Mapping[int, Iterable[int]] | None = None,
) -> list[str]:
    """Build one shell worker for a durable closure.

    ``entity_ids`` is the complete durable ownership set.  Native Mautic 7
    closures may contain several leaf roots but do not need an explicit
    command for every covered parent, so ``command_entity_ids`` can project
    the one worker onto the native roots while all closure IDs remain claimed
    and terminalized together.
    """

    command_ids = [int(sid) for sid in (command_entity_ids or entity_ids) if int(sid) > 0]
    commands: list[list[str]] = []
    for sid in command_ids:
        command = list(build_args(int(sid)))
        per_entity_excludes = (
            exclude_ids_by_entity.get(int(sid), ())
            if isinstance(exclude_ids_by_entity, Mapping)
            else ()
        )
        if per_entity_excludes:
            command = _append_segment_native_excludes(command, per_entity_excludes)
        elif len(command_ids) <= 1:
            command = _append_segment_native_excludes(command, exclude_ids or ())
        commands.append(command)
    if len(commands) <= 1:
        return commands[0] if commands else []
    script = "set -e\n" + "\n".join(shlex.join(command) for command in commands)
    return ["/bin/sh", "-c", script]


def _segment_native_chain_command_plan(
    entity_ids: Iterable[int],
    parents_by_child: Mapping[int, Iterable[int]],
    children_by_parent: Mapping[int, Iterable[int]],
    *,
    already_covered_ids: Iterable[int] | None = None,
) -> tuple[list[int], dict[int, list[int]]]:
    """Project one Mautic 7 closure onto native roots and exclusions.

    A connected fork such as ``57 <- 21 -> 61`` owns one scheduler worker,
    but native traversal needs two sequential leaf-root commands.  The first
    command covers the shared parent; the second receives a repeatable
    ``--exclude=21`` so the parent is not rebuilt again.  Durable claims still
    include every member of the closure.
    """

    ordered: list[int] = []
    for raw_id in entity_ids:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in ordered:
            ordered.append(sid)
    if not ordered:
        return [], {}
    component = set(ordered)
    children = children_by_parent or {}
    roots = [
        sid
        for sid in ordered
        if not (
            {
                int(child)
                for child in (children.get(sid, ()) or ())
                if str(child).strip().lstrip("-").isdigit() and int(child) > 0
            }
            & component
        )
    ]
    if not roots:
        roots = [ordered[0]]
    covered: set[int] = set()
    for raw_id in already_covered_ids or ():
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            covered.add(sid)
    command_roots = [sid for sid in roots if sid not in covered]
    excludes: dict[int, list[int]] = {}
    for sid in command_roots:
        ancestors = dependency_segment_closure({int(sid)}, dict(parents_by_child or {}))
        excludes[int(sid)] = sorted(covered & ancestors)
        covered.update(ancestors)
        covered.add(int(sid))
    return command_roots, excludes


def _segment_cycle_sync(
    store: "TaskStore",
    *,
    root: str,
    planned_ids: list[int] | tuple[int, ...] | set[int],
    never_built_ids: list[int] | tuple[int, ...] | set[int],
    running_ids: list[int] | tuple[int, ...] | set[int],
    excluded_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    now_ts: float,
    source_snapshot: Mapping[object, object] | None = None,
    important_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    remaining_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    lazy_mode: bool | None = None,
) -> tuple[dict[str, object], list[int], bool]:
    """Persist an ordinary cycle without resetting an active cursor."""

    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        state, added, started_new = _segment_cycle_sync_state(
            store.get_runtime_sync(key),
            planned_ids=planned_ids,
            never_built=never_built_ids,
            running_ids=running_ids,
            excluded_ids=excluded_ids,
            now_ts=now_ts,
            source_snapshot=source_snapshot,
            important_ids=important_ids,
            remaining_ids=remaining_ids,
            lazy_mode=lazy_mode,
        )
        store.put_runtime_sync(key, state)
    return state, added, started_new


def _segment_cycle_marker_process_live(marker: Mapping[str, object]) -> bool:
    """Return process evidence for one persisted ordinary-cycle marker."""

    try:
        pid = int(marker.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not _is_pid_alive(pid):
        return False
    try:
        expected_start = int(marker.get("pid_start_time") or 0)
    except (TypeError, ValueError):
        expected_start = 0
    if expected_start <= 0 or _pid_start_time(pid) != expected_start:
        return False
    command = str(marker.get("command_str") or "").strip()
    return not command or _pid_matches_task_command(pid, command)


def _segment_cycle_orphan_grace_sec(config: object) -> int:
    """Use the existing profile-managed orphan observation window."""

    raw = getattr(config, "segment_sql_orphan_after_sec", None)
    if raw is None:
        return max(1, int(_SEGMENT_ORPHAN_GRACE_SEC or 1))
    try:
        return max(30, int(raw or _SEGMENT_ORPHAN_GRACE_SEC))
    except (TypeError, ValueError):
        return max(1, int(_SEGMENT_ORPHAN_GRACE_SEC or 1))


def _segment_cycle_active_evidence(
    store: "TaskStore",
    *,
    root: str,
    state: Mapping[str, object],
    running: Mapping[str, "RunningTask"],
) -> dict[int, dict[str, object]]:
    """Build read-only evidence for claims in one ordinary cycle.

    Runtime markers are useful for generation identity, but marker-only state
    is never treated as live.  A current scheduler task or a live TaskStore
    row must independently prove adoption; the same rule applies to a chain
    marker.  This keeps ordinary Mautic/PHP work non-blocking without allowing
    an old terminal marker to close a newer cycle.
    """

    active_ids = set(
        _normalize_segment_ids(
            [*(state.get("claimed") or []), *(state.get("running") or [])]
        )
    )
    evidence: dict[int, dict[str, object]] = {}
    cycle_id = str(state.get("cycle_id") or "").strip()

    def _merge(sid: int, candidate: Mapping[str, object]) -> None:
        previous = evidence.get(int(sid))
        if previous is None:
            evidence[int(sid)] = dict(candidate)
            return
        previous_live = any(
            bool(previous.get(key))
            for key in ("active", "executor_active", "live", "process", "runtime_confirmed", "taskstore_active")
        )
        candidate_live = any(
            bool(candidate.get(key))
            for key in ("active", "executor_active", "live", "process", "runtime_confirmed", "taskstore_active")
        )
        if candidate_live and not previous_live:
            evidence[int(sid)] = dict(candidate)
        elif candidate_live == previous_live:
            merged = dict(previous)
            merged.update(candidate)
            evidence[int(sid)] = merged

    for sid in sorted(active_ids):
        marker = _segment_runtime_marker_load(store, root, sid)
        if marker:
            marker_evidence = dict(marker)
            marker_evidence["live"] = (
                str(marker.get("status") or "").strip().lower() == "running"
                and _segment_cycle_marker_process_live(marker)
            )
            _merge(sid, marker_evidence)
        chain_marker = _segment_chain_runtime_load(store, root, sid)
        if chain_marker:
            chain_evidence = dict(chain_marker)
            chain_evidence["live"] = (
                str(chain_marker.get("status") or "").strip().lower() in {"running", "admitting"}
                and _segment_cycle_marker_process_live(chain_marker)
            )
            _merge(sid, chain_evidence)

    for task in running.values():
        if (
            task.root != root
            or task.entity_id is None
            or int(task.entity_id) not in active_ids
            or task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES
        ):
            continue
        marker = _segment_runtime_marker_load(store, root, int(task.entity_id))
        task_evidence: dict[str, object] = {
            "executor_active": True,
            "taskstore_active": True,
            "cycle_id": marker.get("cycle_id") if marker else cycle_id,
            "started_at": marker.get("started_at", task.started_at) if marker else task.started_at,
            "independent_at": time.time(),
        }
        _merge(int(task.entity_id), task_evidence)

    try:
        rows = store.running_rows()
    except Exception:
        rows = []
    for row in rows:
        if str(row.get("root") or "") != root:
            continue
        task_type = str(row.get("task_type") or "")
        if task_type not in _SEGMENT_RUNTIME_TASK_TYPES:
            continue
        try:
            sid = int(row.get("entity_id") or 0)
        except (TypeError, ValueError):
            continue
        if sid <= 0 or sid not in active_ids:
            continue
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        command = str(row.get("command_str") or "")
        if pid <= 0 or not _is_pid_alive(pid) or (command and not _pid_matches_task_command(pid, command)):
            continue
        _merge(
            sid,
            {
                "taskstore_active": True,
                "process": True,
                "cycle_id": _segment_runtime_marker_load(store, root, sid).get("cycle_id", cycle_id),
                "started_at": row.get("started_at"),
                "independent_at": time.time(),
            },
        )
    return evidence


def _segment_cycle_reconcile_orphans(
    store: "TaskStore",
    *,
    root: str,
    running: Mapping[str, "RunningTask"],
    now_ts: float,
    orphan_grace_sec: int = _SEGMENT_ORPHAN_GRACE_SEC,
) -> tuple[dict[str, object], list[int]]:
    """Release only proven stale ordinary claims before pending projection."""

    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        raw_state = store.get_runtime_sync(key)
        if not isinstance(raw_state, Mapping):
            return {}, []
        evidence = _segment_cycle_active_evidence(
            store,
            root=root,
            state=raw_state,
            running=running,
        )
        state, released = _segment_cycle_reconcile_active_claims(
            raw_state,
            active_evidence=evidence,
            now_ts=float(now_ts),
            orphan_grace_sec=int(orphan_grace_sec or _SEGMENT_ORPHAN_GRACE_SEC),
        )
        if state != dict(raw_state):
            store.put_runtime_sync(key, state)
    if released:
        _refresh_scheduler_important_root(store, root)
    return state, released


def _segment_cycle_pending_ids(state: Mapping[str, object] | None) -> set[int]:
    """Return unclaimed, unfinished items from an active persisted cycle.

    This is the durable source of truth for ordinary dispatch.  In-memory
    planner state is intentionally not part of this projection: a restart or
    a failed plan refresh must not make an already-admitted cycle invisible.
    """

    if not isinstance(state, Mapping):
        return set()
    if str(state.get("status") or "active").strip().lower() != "active":
        return set()
    items = set(_normalize_segment_ids(state.get("items"))) | set(
        _normalize_segment_ids(state.get("follow_up"))
    )
    finished = set(_normalize_segment_ids(state.get("done"))) | set(
        _normalize_segment_ids(state.get("errors"))
    )
    active = set(_normalize_segment_ids(state.get("claimed"))) | set(
        _normalize_segment_ids(state.get("running"))
    )
    return items - finished - active


def _segment_cycle_pending_order(
    state: Mapping[str, object] | None,
    *,
    excluded_ids: list[int] | tuple[int, ...] | set[int] | None = None,
) -> list[int]:
    """Project durable ordinary work into the ordered pending dispatch list.

    The frozen ``cycle.items`` cursor is never reordered.  IDs discovered
    after its admission live in a separate durable follow-up FIFO and receive
    the next free ordinary worker before the frozen cursor advances.  An ID
    discovered by a plan refresh is not eligible for claim until ``sync_cycle``
    has persisted it, and an item already claimed/running is left to its
    existing worker.  This projection is also used after restart so volatile
    planner caches cannot strand durable work.
    """

    if not isinstance(state, Mapping):
        return []
    pending = _segment_cycle_pending_ids(state)
    excluded = set(_normalize_segment_ids(excluded_ids))
    important = set(_normalize_segment_ids(state.get("important_items")))
    remaining = set(_normalize_segment_ids(state.get("remaining_items")))
    # A legacy state has no explicit tier fields; normalize_state in the
    # admission module projects it as important, so this remains safe across
    # upgrade/restart.
    ordered_all = [
        *_normalize_segment_ids(state.get("follow_up")),
        *_normalize_segment_ids(state.get("items")),
    ]
    ordered = [sid for sid in ordered_all if sid in important]
    ordered.extend(sid for sid in ordered_all if sid in remaining and sid not in important)
    ordered.extend(sid for sid in ordered_all if sid not in important and sid not in remaining)
    return [sid for sid in ordered if sid in pending and sid not in excluded]


def _active_persisted_segment_cycle_roots(
    store: "TaskStore",
    roots: list[str] | tuple[str, ...] | set[str],
) -> set[str]:
    """Return roots whose durable ordinary cycle is not terminal yet.

    A plan refresh performs many potentially slow read-only DB and filesystem
    operations.  It must not run that work for an already admitted root while
    its cycle is waiting for its next worker.  Other roots remain eligible for
    independent planning, and the dispatcher can use this projection without
    touching application databases for the guarded root.
    """

    active_roots: set[str] = set()
    for raw_root in roots:
        root = str(raw_root or "").strip()
        if not root:
            continue
        state = store.get_runtime_sync(_segment_cycle_key(root))
        if not isinstance(state, Mapping):
            continue
        if str(state.get("status") or "").strip().lower() != "active":
            continue
        pending_ids = _segment_cycle_pending_ids(state)
        claimed_or_running = set(_normalize_segment_ids(state.get("claimed"))) | set(
            _normalize_segment_ids(state.get("running"))
        )
        if pending_ids or claimed_or_running:
            active_roots.add(root)
    return active_roots


def _segment_cycle_admission_inputs(
    state: Mapping[str, object] | None,
    planned_ids: list[int] | tuple[int, ...] | set[int],
    never_built_ids: list[int] | tuple[int, ...] | set[int],
    *,
    pressure: bool,
) -> tuple[list[int], list[int]]:
    """Hold only new ordinary admissions while pressure is active."""

    planned = _normalize_segment_ids(planned_ids)
    never_built = _normalize_segment_ids(never_built_ids)
    if not pressure or not isinstance(state, Mapping):
        return planned, never_built
    if str(state.get("status") or "active").strip().lower() != "active":
        return [], []
    existing = set(_normalize_segment_ids(state.get("items")))
    return (
        [sid for sid in planned if sid in existing],
        [sid for sid in never_built if sid in existing],
    )


def _segment_cycle_claim(
    store: "TaskStore",
    *,
    root: str,
    entity_id: int,
    eligible_ids: list[int] | tuple[int, ...] | set[int] | None,
    blocked_ids: list[int] | tuple[int, ...] | set[int] | None,
    running_ids: list[int] | tuple[int, ...] | set[int] | None,
    now_ts: float,
) -> bool:
    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        state, claimed = _segment_cycle_claim_item(
            store.get_runtime_sync(key),
            entity_id=int(entity_id),
            eligible_ids=eligible_ids,
            blocked_ids=blocked_ids,
            running_ids=running_ids,
            now_ts=now_ts,
        )
        if claimed:
            store.put_runtime_sync(key, state)
            _refresh_scheduler_important_root(store, root)
        return claimed


def _segment_cycle_release(store: "TaskStore", *, root: str, entity_id: int, now_ts: float) -> None:
    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        state = _segment_cycle_release_claim(
            store.get_runtime_sync(key),
            entity_id=int(entity_id),
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)
    _refresh_scheduler_important_root(store, root)


def _segment_cycle_running(store: "TaskStore", *, root: str, entity_id: int, now_ts: float) -> None:
    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        state = _segment_cycle_mark_running(
            store.get_runtime_sync(key),
            entity_id=int(entity_id),
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)
    _refresh_scheduler_important_root(store, root)


def _segment_cycle_terminal(
    store: "TaskStore",
    *,
    root: str,
    entity_id: int,
    success: bool,
    note: str | None,
    now_ts: float,
) -> None:
    key = _segment_cycle_key(root)
    with _SEGMENT_CYCLE_LOCK:
        state = _segment_cycle_mark_terminal(
            store.get_runtime_sync(key),
            entity_id=int(entity_id),
            success=bool(success),
            note=note,
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)
    _refresh_scheduler_important_root(store, root)


def _terminalize_segment_data_errors(
    store: "TaskStore",
    *,
    root: str,
    state: Mapping[str, object] | None,
    terminal_notes: Mapping[int, str] | None,
    excluded_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    now_ts: float,
) -> tuple[dict[str, object], list[int]]:
    """Close admitted unsafe topology instead of leaving it blocked forever.

    This is deliberately narrower than the normal blocked-id admission path:
    only already-admitted pending items with an explicit topology data-error
    note are terminalized.  Ordinary Mautic/PHP work is still launched and
    receives its retry/error result from the actual command.
    """

    if not isinstance(state, Mapping) or not terminal_notes:
        return dict(state) if isinstance(state, Mapping) else {}, []
    pending = _segment_cycle_pending_order(state, excluded_ids=excluded_ids)
    targets = [int(sid) for sid in pending if int(sid) in terminal_notes]
    for sid in targets:
        _segment_cycle_terminal(
            store,
            root=root,
            entity_id=int(sid),
            success=False,
            note=str(terminal_notes.get(int(sid)) or "data_error:logical_issue"),
            now_ts=float(now_ts),
        )
    if not targets:
        return dict(state), []
    updated = store.get_runtime_sync(_segment_cycle_key(root))
    return (dict(updated) if isinstance(updated, Mapping) else {}), targets


def _manual_static_lane_for_ids(
    static_state: Mapping[str, object] | None,
    entity_ids: list[int],
    *,
    super_priority_ids: set[int],
    priority_ids: set[int],
) -> tuple[str | None, str | None]:
    """Resolve one static owner for a manual target/parent closure."""

    lanes_raw = static_state.get("lanes") if isinstance(static_state, Mapping) else {}
    lanes = lanes_raw if isinstance(lanes_raw, Mapping) else {}
    memberships: dict[int, set[str]] = {}
    for lane_name in (_SEGMENT_SUPER_PRIORITY_LANE, _SEGMENT_PRIORITY_LANE):
        lane_payload = lanes.get(lane_name)
        lane_items = lane_payload.get("items") if isinstance(lane_payload, Mapping) else []
        for raw_sid in lane_items or []:
            try:
                sid = int(raw_sid)
            except (TypeError, ValueError):
                continue
            if sid > 0:
                memberships.setdefault(sid, set()).add(lane_name)

    assigned: set[str] = set()
    for sid in entity_ids:
        current = memberships.get(int(sid), set())
        if len(current) > 1:
            return None, "manual_dependency_overlap_static_lanes"
        if current:
            assigned.update(current)
    if assigned:
        if len(assigned) > 1:
            return None, "manual_dependency_overlap_static_lanes"
        # Persisted lane membership is authoritative for an active queue. A
        # super-priority owner may absorb a lower-priority configured parent,
        # but a lower lane may not absorb a super-priority dependency.
        if _SEGMENT_PRIORITY_LANE in assigned and any(
            sid in super_priority_ids and sid not in memberships for sid in entity_ids
        ):
            return None, "manual_dependency_overlap_static_lanes"
        return next(iter(assigned)), None
    if any(sid in super_priority_ids for sid in entity_ids):
        return _SEGMENT_SUPER_PRIORITY_LANE, None
    if any(sid in priority_ids for sid in entity_ids):
        return _SEGMENT_PRIORITY_LANE, None
    return None, None


def _segment_cycle_manual_prepare(
    store: "TaskStore",
    *,
    root: str,
    request_id: int,
    entity_ids: list[int],
    observed_snapshot: Mapping[object, object] | None,
    super_priority_ids: set[int],
    priority_ids: set[int],
    globally_running_ids: set[int],
    now_ts: float,
) -> tuple[bool, str]:
    """Atomically claim a manual segment target in ordinary or static state."""

    ids = [int(sid) for sid in entity_ids if int(sid) > 0]
    if not ids:
        return False, "manual_target_missing"
    if set(ids) & set(globally_running_ids):
        return False, "manual_dependency_overlap_running"
    ordinary_key = _segment_cycle_key(root)
    static_key = _static_lane_state_key(root)
    with _SEGMENT_CYCLE_LOCK:
        with _SEGMENT_STATIC_LANE_LOCK:
            ordinary_raw = store.get_runtime_sync(ordinary_key) or {}
            static_raw = store.get_runtime_sync(static_key) or {}
            static_lane, static_reason = _manual_static_lane_for_ids(
                static_raw if isinstance(static_raw, Mapping) else {},
                ids,
                super_priority_ids=super_priority_ids,
                priority_ids=priority_ids,
            )
            ordinary_items = {
                int(value)
                for value in list(ordinary_raw.get("items") or [])
                if str(value).strip().isdigit() and int(value) > 0
            } if isinstance(ordinary_raw, Mapping) else set()
            static_items: set[int] = set()
            static_lanes_raw = static_raw.get("lanes") if isinstance(static_raw, Mapping) else {}
            if isinstance(static_lanes_raw, Mapping):
                for lane_payload in static_lanes_raw.values():
                    if not isinstance(lane_payload, Mapping):
                        continue
                    static_items.update(
                        int(value)
                        for value in list(lane_payload.get("items") or [])
                        if str(value).strip().isdigit() and int(value) > 0
                    )
            if static_reason:
                return False, static_reason
            if static_lane is not None and set(ids) & ordinary_items:
                return False, "manual_dependency_overlap_ordinary"
            if static_lane is None and set(ids) & static_items:
                return False, "manual_dependency_overlap_static"

            if static_lane is None:
                next_ordinary, claimed, reason = _segment_cycle_manual_claim_state(
                    ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                    request_id=int(request_id),
                    entity_ids=ids,
                    lane="ordinary",
                    observed_snapshot=observed_snapshot,
                    now_ts=now_ts,
                )
                if not claimed:
                    return False, reason
                store.put_runtime_sync(ordinary_key, next_ordinary)
                return True, reason

            next_static = dict(static_raw) if isinstance(static_raw, Mapping) else {}
            next_static.setdefault("schema", _STATIC_SEGMENT_LANES_SCHEMA)
            lanes = next_static.get("lanes") if isinstance(next_static.get("lanes"), Mapping) else {}
            lane_payload = dict(lanes.get(static_lane) or {}) if isinstance(lanes, Mapping) else {}
            lane_payload["items"] = list(dict.fromkeys([*(lane_payload.get("items") or []), *ids]))
            next_lanes = dict(lanes) if isinstance(lanes, Mapping) else {}
            next_lanes[static_lane] = lane_payload
            next_static["lanes"] = next_lanes
            claimed_static: list[int] = []
            for sid in ids:
                next_static, claimed = _static_lane_claim_item(
                    next_static,
                    lane=static_lane,
                    entity_id=int(sid),
                    blocked_ids=[],
                    globally_running_ids=globally_running_ids,
                    now_ts=now_ts,
                )
                if not claimed:
                    for claimed_sid in claimed_static:
                        next_static = _static_lane_release_claim(
                            next_static,
                            lane=static_lane,
                            entity_id=int(claimed_sid),
                            now_ts=now_ts,
                        )
                    return False, "manual_static_claim_conflict"
                claimed_static.append(int(sid))
            next_ordinary, claimed, reason = _segment_cycle_manual_claim_state(
                ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                request_id=int(request_id),
                entity_ids=ids,
                lane=static_lane,
                observed_snapshot=observed_snapshot,
                now_ts=now_ts,
            )
            if not claimed:
                for claimed_sid in claimed_static:
                    next_static = _static_lane_release_claim(
                        next_static,
                        lane=static_lane,
                        entity_id=int(claimed_sid),
                        now_ts=now_ts,
                    )
                return False, reason
            store.put_runtime_sync(static_key, next_static)
            store.put_runtime_sync(ordinary_key, next_ordinary)
            return True, reason


def _segment_cycle_manual_release(
    store: "TaskStore",
    *,
    root: str,
    request_id: int,
    now_ts: float,
) -> None:
    ordinary_key = _segment_cycle_key(root)
    static_key = _static_lane_state_key(root)
    with _SEGMENT_CYCLE_LOCK:
        with _SEGMENT_STATIC_LANE_LOCK:
            ordinary_raw = store.get_runtime_sync(ordinary_key) or {}
            info = _segment_cycle_manual_info(
                ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                request_id=int(request_id),
            )
            if isinstance(info, Mapping) and str(info.get("lane") or "") in {
                _SEGMENT_SUPER_PRIORITY_LANE,
                _SEGMENT_PRIORITY_LANE,
            }:
                static_state = store.get_runtime_sync(static_key) or {}
                for sid in list(info.get("entity_ids") or []):
                    static_state = _static_lane_release_claim(
                        static_state if isinstance(static_state, Mapping) else {},
                        lane=str(info.get("lane")),
                        entity_id=int(sid),
                        now_ts=now_ts,
                    )
                store.put_runtime_sync(static_key, static_state)
            next_ordinary = _segment_cycle_manual_release_state(
                ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                request_id=int(request_id),
                now_ts=now_ts,
            )
            store.put_runtime_sync(ordinary_key, next_ordinary)


def _segment_cycle_manual_terminal(
    store: "TaskStore",
    *,
    root: str,
    request_id: int,
    success: bool,
    note: str | None,
    now_ts: float,
) -> None:
    ordinary_key = _segment_cycle_key(root)
    static_key = _static_lane_state_key(root)
    with _SEGMENT_CYCLE_LOCK:
        with _SEGMENT_STATIC_LANE_LOCK:
            ordinary_raw = store.get_runtime_sync(ordinary_key) or {}
            info = _segment_cycle_manual_info(
                ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                request_id=int(request_id),
            )
            next_ordinary = _segment_cycle_manual_terminal_state(
                ordinary_raw if isinstance(ordinary_raw, Mapping) else {},
                request_id=int(request_id),
                success=bool(success),
                note=note,
                now_ts=now_ts,
            )
            if isinstance(info, Mapping) and str(info.get("lane") or "") in {
                _SEGMENT_SUPER_PRIORITY_LANE,
                _SEGMENT_PRIORITY_LANE,
            }:
                static_state = store.get_runtime_sync(static_key) or {}
                lane = str(info.get("lane"))
                for sid in list(info.get("entity_ids") or []):
                    static_state = _static_lane_mark_terminal(
                        static_state if isinstance(static_state, Mapping) else {},
                        lane=lane,
                        entity_id=int(sid),
                        success=bool(success),
                        note=note,
                        now_ts=now_ts,
                    )
                store.put_runtime_sync(static_key, static_state)
            store.put_runtime_sync(ordinary_key, next_ordinary)


def _segment_terminal_and_wake(
    store: "TaskStore",
    wakeup: threading.Event,
    task: "RunningTask",
    success: bool,
    note: str | None,
) -> None:
    """Commit terminal state and wake the dispatcher for an immediate claim."""

    if task.entity_id is None:
        return
    if task.manual_request_id is not None:
        _segment_cycle_manual_terminal(
            store,
            root=task.root,
            request_id=int(task.manual_request_id),
            success=bool(success),
            note=note,
            now_ts=time.time(),
        )
    else:
        chain_ids = _segment_chain_entity_ids(store, task.root, int(task.entity_id))
        terminal_at = time.time()
        for chain_id in chain_ids:
            marker_task = replace(
                task,
                entity_id=int(chain_id),
                task_key=_task_key(task.root, task.task_type, int(chain_id)),
            )
            _segment_runtime_marker_write(
                store,
                marker_task,
                status="terminal",
                now_ts=terminal_at,
                note=note,
            )
            _segment_cycle_terminal(
                store,
                root=task.root,
                entity_id=int(chain_id),
                success=bool(success),
                note=note,
                now_ts=terminal_at,
            )
            _mark_segment_finished(task.root, int(chain_id), now_ts=terminal_at)
        if success and task.task_type == "segment":
            native_roots = [int(task.entity_id)]
            try:
                _dispatch_context, dispatch_major, parents_by_child, children_by_parent = (
                    _segment_dispatch_context_parts(store, task.root)
                )
                if int(dispatch_major or 0) >= 7:
                    native_roots, _native_excludes = _segment_native_chain_command_plan(
                        chain_ids,
                        parents_by_child,
                        children_by_parent,
                    )
            except Exception:
                native_roots = [int(task.entity_id)]
            for native_root in native_roots:
                _segment_native_coverage_record(
                    store,
                    root=task.root,
                    entity_id=int(native_root),
                    success=True,
                    now_ts=terminal_at,
                )
        if len(chain_ids) > 1:
            _segment_chain_runtime_write(
                store,
                root=task.root,
                owner_id=int(task.entity_id),
                entity_ids=chain_ids,
                status="terminal",
                now_ts=terminal_at,
                task=task,
            )
    wakeup.set()


def _static_lanes_reconcile(
    store: "TaskStore",
    *,
    root: str,
    plans: dict[str, list[int]],
    now_ts: float,
    active_evidence: Mapping[tuple[str, int], Mapping[str, object] | bool] | None = None,
    orphan_grace_sec: int = _STATIC_LANE_ORPHAN_GRACE_SEC,
) -> tuple[dict[str, object], dict[str, list[int]], bool]:
    key = _static_lane_state_key(root)
    with _SEGMENT_STATIC_LANE_LOCK:
        state, added, started_new = _static_lane_reconcile_state(
            store.get_runtime_sync(key),
            plans=plans,
            now_ts=now_ts,
            active_evidence=active_evidence,
            orphan_grace_sec=orphan_grace_sec,
        )
        store.put_runtime_sync(key, state)
    return state, added, started_new


def _static_lane_runtime_evidence(
    *,
    store: "TaskStore",
    root: str,
    static_state: Mapping[str, object] | None,
    running: Mapping[str, "RunningTask"],
    priority_executor: "_PriorityTaskExecutor | None",
    liveness_probe: Callable[["RunningTask"], Mapping[str, object] | bool] | None = None,
    store_running_rows: list[Mapping[str, object]] | None = None,
    now_ts: float | None = None,
) -> dict[tuple[str, int], dict[str, object]]:
    """Resolve independent evidence for persisted static-lane claims.

    The persisted lane marker is deliberately used only as a grace anchor.
    A live executor, nonterminal TaskStore row, or read-only segment process/DB
    probe must be present before a claim is adopted as live.
    """

    now = float(now_ts if now_ts is not None else time.time())
    lanes_raw = static_state.get("lanes") if isinstance(static_state, Mapping) else {}
    lanes = lanes_raw if isinstance(lanes_raw, Mapping) else {}
    task_types = {
        _SEGMENT_SUPER_PRIORITY_LANE: "segment_super_priority",
        _SEGMENT_PRIORITY_LANE: "segment_priority",
    }
    rows = store_running_rows
    if rows is None:
        try:
            rows = store.running_rows()
        except Exception:
            rows = []

    running_candidates: dict[tuple[int, str], RunningTask] = {}
    for task in running.values():
        if str(task.root) != str(root) or task.entity_id is None:
            continue
        running_candidates.setdefault((int(task.entity_id), str(task.task_type)), task)

    store_candidates: set[tuple[int, str]] = set()
    for row in rows or []:
        if str(row.get("root") or "") != str(root) or row.get("entity_id") is None:
            continue
        try:
            sid = int(row.get("entity_id"))
        except (TypeError, ValueError):
            continue
        store_candidates.add((sid, str(row.get("task_type") or "")))

    result: dict[tuple[str, int], dict[str, object]] = {}
    for lane_name, task_type in task_types.items():
        payload = lanes.get(lane_name)
        payload = payload if isinstance(payload, Mapping) else {}
        active_ids = set(_normalize_segment_ids([*(payload.get("claimed") or []), *(payload.get("running") or [])]))
        for sid in active_ids:
            evidence: dict[str, object] = {}
            if priority_executor is not None:
                try:
                    if priority_executor.is_active(str(root), task_type, int(sid)):
                        evidence["executor_active"] = True
                except Exception:
                    pass
            if (int(sid), task_type) in store_candidates:
                evidence["taskstore_active"] = True
            if any((int(sid), candidate_type) in store_candidates for candidate_type in ("segment", "segment_sql")):
                evidence["taskstore_active"] = True

            candidate = running_candidates.get((int(sid), task_type))
            if candidate is None:
                candidate = running_candidates.get((int(sid), "segment"))
            marker = _segment_runtime_marker_load(store, root, int(sid))
            chain_payload = _segment_chain_runtime_load(store, root, int(sid))
            if (
                chain_payload.get("schema") == _SEGMENT_CHAIN_RUNTIME_SCHEMA
                and str(chain_payload.get("status") or "").strip().lower()
                in {"admitting", "running"}
                and _segment_cycle_marker_process_live(chain_payload)
            ):
                # One chain has one executor owner. Reverse-index evidence
                # prevents non-owner members from being released by static
                # orphan reconciliation while that owner is still alive.
                # A persisted marker without current PID/start identity is
                # only stale metadata and must not block this root.
                evidence["taskstore_active"] = True
            if marker:
                for field in ("last_independent_at", "started_at"):
                    try:
                        if marker.get(field) is not None:
                            evidence[field] = float(marker[field])
                    except (TypeError, ValueError):
                        pass
            if candidate is None and (marker or liveness_probe is not None):
                try:
                    marker_started_at = float(marker.get("started_at") or now)
                except (TypeError, ValueError):
                    marker_started_at = now
                try:
                    marker_pid = int(marker.get("pid") or 0)
                except (TypeError, ValueError):
                    marker_pid = 0
                candidate = RunningTask(
                    row_id=0,
                    root=str(root),
                    task_key=_task_key(str(root), task_type, int(sid)),
                    task_type=task_type,
                    entity_id=int(sid),
                    command_str=str(marker.get("command_str") or ""),
                    timeout_sec=0,
                    attempts=1,
                    started_at=marker_started_at,
                    pid=marker_pid,
                )
            if candidate is not None and liveness_probe is not None:
                try:
                    probe = liveness_probe(candidate)
                except Exception:
                    probe = {}
                if isinstance(probe, Mapping):
                    if any(bool(probe.get(key)) for key in ("active", "db", "db_process", "process", "runtime_confirmed")):
                        evidence["db"] = True
                elif isinstance(probe, bool) and probe:
                    evidence["process"] = True
            result[(lane_name, int(sid))] = evidence
    return result


def _static_lane_claim(
    store: "TaskStore",
    *,
    root: str,
    lane: str,
    entity_id: int,
    blocked_ids: set[int],
    globally_running_ids: set[int],
    now_ts: float,
) -> bool:
    key = _static_lane_state_key(root)
    with _SEGMENT_STATIC_LANE_LOCK:
        state, claimed = _static_lane_claim_item(
            store.get_runtime_sync(key),
            lane=lane,
            entity_id=int(entity_id),
            blocked_ids=blocked_ids,
            globally_running_ids=globally_running_ids,
            now_ts=now_ts,
        )
        if claimed:
            store.put_runtime_sync(key, state)
        return claimed


def _static_lane_running(store: "TaskStore", *, root: str, lane: str, entity_id: int, now_ts: float) -> None:
    key = _static_lane_state_key(root)
    with _SEGMENT_STATIC_LANE_LOCK:
        state = _static_lane_mark_running(
            store.get_runtime_sync(key),
            lane=lane,
            entity_id=int(entity_id),
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)


def _static_lane_release(store: "TaskStore", *, root: str, lane: str, entity_id: int, now_ts: float) -> None:
    key = _static_lane_state_key(root)
    with _SEGMENT_STATIC_LANE_LOCK:
        state = _static_lane_release_claim(
            store.get_runtime_sync(key),
            lane=lane,
            entity_id=int(entity_id),
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)


def _static_lane_terminal(
    store: "TaskStore",
    *,
    root: str,
    lane: str,
    entity_id: int,
    success: bool,
    note: str | None,
    now_ts: float,
) -> None:
    key = _static_lane_state_key(root)
    with _SEGMENT_STATIC_LANE_LOCK:
        state = _static_lane_mark_terminal(
            store.get_runtime_sync(key),
            lane=lane,
            entity_id=int(entity_id),
            success=bool(success),
            note=note,
            now_ts=now_ts,
        )
        store.put_runtime_sync(key, state)


def _mark_campaign_rebuild_finished(root: str, campaign_id: int | None, now_ts: float | None = None) -> None:
    if campaign_id is None:
        return
    try:
        cid = int(campaign_id)
    except Exception:
        return
    if cid <= 0:
        return
    _CAMPAIGN_REBUILD_FINISHED_AT[(str(root), cid)] = float(now_ts or time.time())
    _CAMPAIGN_TRIGGER_FUTURE_WAKE.pop((str(root), cid), None)


def _mark_campaign_trigger_finished(root: str, campaign_id: int | None) -> None:
    if campaign_id is None:
        return
    try:
        cid = int(campaign_id)
    except Exception:
        return
    if cid <= 0:
        return
    _CAMPAIGN_REBUILD_FINISHED_AT.pop((str(root), cid), None)


def _campaign_trigger_after_rebuild_pending(
    root: str,
    rings: tuple[deque[int], ...],
) -> bool:
    root_key = str(root)
    return any(
        (root_key, int(campaign_id)) in _CAMPAIGN_REBUILD_FINISHED_AT
        for ring in rings
        for campaign_id in ring
    )


def _campaign_trigger_waits_for_rebuild(
    *,
    root: str,
    campaign_id: int,
    planned_after_ts: float,
    running: dict[str, "RunningTask"],
) -> bool:
    try:
        cid = int(campaign_id)
    except Exception:
        return False
    if cid <= 0:
        return False
    if _is_running(running, root, "campaign_rebuild", cid):
        return True
    rebuilt_at = float(_CAMPAIGN_REBUILD_FINISHED_AT.get((str(root), cid), 0.0) or 0.0)
    return rebuilt_at < float(planned_after_ts or 0.0)


def _campaign_rebuild_waits_for_trigger(
    *,
    root: str,
    campaign_id: int,
    running: dict[str, "RunningTask"],
) -> bool:
    try:
        cid = int(campaign_id)
    except Exception:
        return False
    if cid <= 0:
        return False
    return _is_running(running, root, "campaign_trigger", cid)


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


def _instance_setting_keys(inst: object) -> list[str]:
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


def _plugin_operation_has_registered_bundle(db: MauticDB | None, item: object) -> bool:
    """Require Mautic registry state in addition to the plugin directory.

    The catalog helper verifies the filesystem path. A stale or incomplete
    directory must not make an optional scheduled operation executable.
    """
    if not isinstance(item, dict) or db is None:
        return False
    bundle = str(item.get("bundle", "") or "").strip()
    return bool(bundle and db.has_installed_plugin_matching(bundle))


def _instance_scoped_whitelist_entry(settings: object, inst: object) -> tuple[bool, object]:
    if not isinstance(settings, dict):
        return False, None

    instance_keys = _instance_setting_keys(inst)
    qualified_keys: list[str] = []
    instance_key_set = set(instance_keys)
    for raw_key in settings:
        key = str(raw_key or "").strip()
        base_key, separator, _host_name = key.partition("@")
        if separator and base_key in instance_key_set and key not in instance_key_set:
            qualified_keys.append(key)

    for key in instance_keys + qualified_keys + ["default"]:
        if key in settings:
            return True, settings.get(key)
    return False, None


def _whitelist_realtime_block(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    block = raw.get("realtime", {})
    if not isinstance(block, dict) or not _to_boolish(block.get("enabled"), True):
        return {}
    return block


def _whitelist_realtime_int(
    block: dict[str, object],
    key: str,
    *,
    maximum: int | None = None,
) -> int:
    try:
        value = max(0, int(block.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0
    return min(value, maximum) if maximum is not None else value


@dataclass(frozen=True)
class SegmentWhitelistRealtimeSetting:
    ids: frozenset[int] = frozenset()
    interval_sec: int = 0
    parallel: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.ids and self.interval_sec > 0 and self.parallel > 0)


@dataclass(frozen=True)
class CampaignWhitelistRealtimeSetting:
    ids: frozenset[int] = frozenset()
    rebuild_interval_sec: int = 0
    trigger_interval_sec: int = 0
    rebuild_parallel: int = 0
    trigger_parallel: int = 0

    @property
    def rebuild_enabled(self) -> bool:
        return bool(self.ids and self.rebuild_interval_sec > 0 and self.rebuild_parallel > 0)

    @property
    def trigger_enabled(self) -> bool:
        return bool(self.ids and self.trigger_interval_sec > 0 and self.trigger_parallel > 0)


def _plugin_operation_guard_matches(db: MauticDB, guard: object, now_local: datetime) -> bool:
    if not isinstance(guard, dict) or str(guard.get("type", "") or "") != "mautic_db_count":
        return False
    query = str(guard.get("query", "") or "").strip()
    if not re.match(r"(?is)^select\s+count\s*\(", query):
        return False
    context: dict[str, str] = {}
    raw_context = guard.get("context") if isinstance(guard.get("context"), dict) else {}
    for key, source in raw_context.items():
        if str(source or "") == "instance.now_local":
            context[str(key)] = now_local.strftime("%Y-%m-%d %H:%M:%S")
    positive = db.fetch_count(query, context=context) > 0
    return positive if bool(guard.get("positive", True)) else not positive


def _plugin_operation_bootstrap_digest(root: str, operation_key: str, task_id: str) -> str:
    payload = "\0".join((str(root), str(operation_key), str(task_id)))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def _plugin_operation_bootstrap_state_key(digest: str) -> str:
    return f"plugin_operation_bootstrap:{digest}"


def _plugin_operation_bootstrap_completed(store: "TaskStore", digest: str) -> bool:
    state = store.get_runtime_sync(_plugin_operation_bootstrap_state_key(digest)) or {}
    return bool(state.get("completed"))


def _mark_plugin_operation_bootstrap_completed(
    store: "TaskStore",
    digest: str,
    *,
    reason: str,
) -> None:
    state = store.get_runtime_sync(_plugin_operation_bootstrap_state_key(digest)) or {}
    store.put_runtime_sync(
        _plugin_operation_bootstrap_state_key(digest),
        {
            **state,
            "completed": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "reason": str(reason or "completed"),
        },
    )


def _sync_plugin_operation_bootstrap_enabled_state(
    store: "TaskStore",
    digest: str,
    *,
    enabled: bool,
) -> None:
    state = store.get_runtime_sync(_plugin_operation_bootstrap_state_key(digest)) or {}
    previous = state.get("enabled")
    if previous is not None and bool(previous) == bool(enabled):
        return
    store.put_runtime_sync(
        _plugin_operation_bootstrap_state_key(digest),
        {
            **state,
            "enabled": bool(enabled),
            "completed": False,
            "enabled_changed_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _plugin_operation_bootstrap_is_completed(
    store: "TaskStore",
    digest: str,
    *,
    db: MauticDB,
    complete_when: object,
    now_local: datetime,
) -> bool:
    completed = _plugin_operation_bootstrap_completed(store, digest)
    if not isinstance(complete_when, dict):
        return completed
    if _plugin_operation_guard_matches(db, complete_when, now_local):
        if not completed:
            _mark_plugin_operation_bootstrap_completed(
                store,
                digest,
                reason="adopted_existing_plugin_data",
            )
        return True
    # A previous completion marker must not suppress bootstrap after the
    # plugin data was removed or the bundle was reinstalled into the same root.
    return False


def _plugin_operation_bootstrap_digest_from_task(task: "RunningTask") -> str:
    prefix = "job:plugin_operation_bootstrap:"
    task_type = str(task.task_type or "")
    digest = task_type[len(prefix) :] if task_type.startswith(prefix) else ""
    digest = digest.split(":", 1)[0]
    return digest if re.fullmatch(r"[a-f0-9]{24}", digest) else ""


_PLUGIN_OPERATION_RUNTIME_SCHEMA = "mcd-plugin-operation-runtime-v1"
_PLUGIN_OPERATION_RUNTIME_PREFIX = "plugin_operation_runtime:"
_PLUGIN_APPLY_RESULT_PREFIX = "MCD_PLUGIN_APPLY_RESULT="
_PLUGIN_APPLY_RESULT_REQUIRED_FIELDS = {
    "overall_status",
    "post_step_status",
    "cache_inventory_confirmed",
    "rc",
    "inventory",
}


def _plugin_operation_resource_key(operation_key: str, task: dict[str, Any]) -> str:
    explicit = str(task.get("resource_key", "") or "").strip()
    task_id = str(task.get("id", "operation") or "operation").strip()
    return explicit or f"plugin:{operation_key}:{task_id}"


def _plugin_operation_resource_digest(root: str, resource_key: str) -> str:
    return hashlib.sha256(f"{root}|{resource_key}".encode("utf-8")).hexdigest()[:32]


def _plugin_operation_runtime_key(digest: str) -> str:
    return f"{_PLUGIN_OPERATION_RUNTIME_PREFIX}{digest}"


def _plugin_operation_runtime_digest_from_task(task: "RunningTask") -> str:
    task_type = str(task.task_type or "")
    if not task_type.startswith("job:plugin_operation"):
        return ""
    digest = task_type.rsplit(":", 1)[-1]
    return digest if re.fullmatch(r"[a-f0-9]{32}", digest) else ""


def _plugin_operation_runtime_update(
    store: "TaskStore",
    digest: str,
    updates: dict[str, object],
) -> dict[str, object]:
    key = _plugin_operation_runtime_key(digest)
    current = store.get_runtime_sync(key) or {}
    payload: dict[str, object] = {**current, "schema": _PLUGIN_OPERATION_RUNTIME_SCHEMA, **updates}
    store.put_runtime_sync(key, payload)
    return payload


def _plugin_operation_output_paths(config: AgentConfig, digest: str) -> tuple[str, str]:
    directory = Path(config.state_db_path).resolve().parent / "plugin-operation-runtime"
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    return str(directory / f"{digest}.stdout"), str(directory / f"{digest}.stderr")


def _plugin_operation_read_output(path: str, limit: int = 2000) -> str:
    if not path:
        return ""
    try:
        value = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = value[-max(1, int(limit)):].strip()
    terminal_result = ""
    for raw in reversed(value.splitlines()):
        line = raw.strip()
        if not line.startswith(_PLUGIN_APPLY_RESULT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(_PLUGIN_APPLY_RESULT_PREFIX) :])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schema") != "mcd-plugin-apply-result-v1":
            continue
        if payload.get("operation") != "plugin_apply":
            continue
        if not _PLUGIN_APPLY_RESULT_REQUIRED_FIELDS.issubset(payload):
            continue
        terminal_result = line
        break
    if not terminal_result or terminal_result in tail.splitlines():
        return tail
    return f"{tail}\n{terminal_result}".strip()


def _plugin_operation_runtime_finish(
    store: "TaskStore",
    task: "RunningTask",
    *,
    state: str,
    rc: int | None,
    skipped_reason: str = "",
    stale: bool = False,
    now_ts: float | None = None,
) -> None:
    digest = _plugin_operation_runtime_digest_from_task(task)
    if not digest:
        return
    now = time.time() if now_ts is None else float(now_ts)
    current = store.get_runtime_sync(_plugin_operation_runtime_key(digest)) or {}
    stdout_path = str(task.stdout_path or current.get("_stdout_path") or "")
    stderr_path = str(task.stderr_path or current.get("_stderr_path") or "")
    _plugin_operation_runtime_update(
        store,
        digest,
        {
            "state": state,
            "last_finished_at": now,
            "rc": rc,
            "duration_sec": max(0.0, now - float(task.started_at or now)),
            "stdout": _plugin_operation_read_output(stdout_path),
            "stderr": _plugin_operation_read_output(stderr_path),
            "skipped_reason": skipped_reason,
            "heartbeat_at": now,
            "owner_pid": int(task.pid or 0),
            "stale": bool(stale),
        },
    )


def _plugin_operation_runtime_heartbeat(store: "TaskStore", task: "RunningTask", now_ts: float) -> None:
    digest = _plugin_operation_runtime_digest_from_task(task)
    if not digest:
        return
    current = store.get_runtime_sync(_plugin_operation_runtime_key(digest)) or {}
    if int(current.get("owner_pid") or 0) != int(task.pid or 0):
        return
    _plugin_operation_runtime_update(
        store,
        digest,
        {"state": "running", "heartbeat_at": float(now_ts), "stale": False},
    )


def _static_segment_setting_value(settings: object, inst: object) -> tuple[bool, object]:
    return _instance_scoped_whitelist_entry(settings, inst)


def _static_segment_ids_from_value(raw: object, *, super_priority: bool) -> set[int]:
    if isinstance(raw, dict):
        if "enabled" in raw and not _to_boolish(raw.get("enabled"), True):
            return set()
        if _to_boolish(raw.get("disable_whitelist"), False) or _to_boolish(raw.get("disabled"), False):
            return set()
        keys = (
            ("segment_super_priority", "super_priority", "segments", "ids")
            if super_priority
            else ("segment_priority", "segment_whitelist", "priority", "segments", "ids", "whitelist")
        )
        for key in keys:
            if key in raw:
                return set(_to_int_list(raw.get(key)))
        return set()
    if isinstance(raw, bool):
        return set()
    return set(_to_int_list(raw))


def _segment_priority_effective_setting(config: AgentConfig, inst: object) -> set[int]:
    """Resolve the canonical priority list; whitelist is only its old alias."""

    if bool(getattr(config, "disable_whitelist", False)):
        return set()
    global_ids = set(getattr(config, "segment_priority", []) or [])
    global_ids.update(getattr(config, "segment_whitelist", []) or [])
    global_ids |= _load_segment_whitelist_file(getattr(config, "segment_whitelist_file", None), inst)
    found_priority, raw_priority = _static_segment_setting_value(
        getattr(config, "segment_priority_instance_settings", {}),
        inst,
    )
    found_legacy, raw_legacy = _static_segment_setting_value(
        getattr(config, "segment_whitelist_instance_settings", {}),
        inst,
    )
    if not found_priority and not found_legacy:
        return global_ids
    # An instance-scoped entry replaces the global baseline, while priority
    # and legacy-whitelist entries for that same instance are one merged
    # entity and are deduplicated below.
    ids: set[int] = set()
    for raw in (raw_priority if found_priority else None, raw_legacy if found_legacy else None):
        if isinstance(raw, bool):
            if not raw:
                continue
        else:
            ids |= _static_segment_ids_from_value(raw, super_priority=False)
        if isinstance(raw, dict):
            file_raw = str(raw.get("segment_priority_file", raw.get("segment_whitelist_file", "")) or "").strip()
            if file_raw:
                ids |= _load_segment_whitelist_file(file_raw, inst)
    return ids


def _segment_super_priority_effective_setting(config: AgentConfig, inst: object) -> set[int]:
    """Resolve the sole additional static super-priority MCC list."""

    if bool(getattr(config, "disable_whitelist", False)):
        return set()
    global_ids = set(getattr(config, "segment_super_priority", []) or [])
    found, raw = _static_segment_setting_value(
        getattr(config, "segment_super_priority_instance_settings", {}),
        inst,
    )
    if not found:
        return global_ids
    if isinstance(raw, bool):
        return global_ids if raw else set()
    return _static_segment_ids_from_value(raw, super_priority=True)


def _segment_lazy_update_effective_setting(config: AgentConfig, inst: object) -> bool:
    """Resolve lazy planning per instance with the host value as fallback.

    The host-level ``segment_lazy_update_enabled`` remains the compatible
    default.  A canonical instance row may override it by immutable UID,
    root/name/domain alias, or the existing host-qualified ``<uid>@<host>``
    alias.  Invalid rows deliberately fall back to the host default instead
    of silently turning a full planner into a lazy one.
    """

    host_default = bool(getattr(config, "segment_lazy_update_enabled", False))
    found, raw = _static_segment_setting_value(
        getattr(config, "segment_lazy_update_instance_settings", {}),
        inst,
    )
    if not found:
        return host_default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, Mapping):
        if _to_boolish(raw.get("tombstone"), False):
            return host_default
        for key in ("segment_lazy_update_enabled", "lazy", "enabled", "value"):
            if key in raw:
                value = raw.get(key)
                if isinstance(value, (bool, int, float, str)):
                    return _to_boolish(value, host_default)
    return host_default


def _segment_full_scan_admission_enabled(
    *,
    lazy_segment_plan: bool,
    force_segment_full_scan: bool,
    cluster_cron_allowed: bool,
) -> bool:
    """Allow a forced full snapshot to admit fresh work only in full mode."""
    return bool(force_segment_full_scan and not lazy_segment_plan and cluster_cron_allowed)


def _segment_static_lane_parallel_limit(
    config: AgentConfig,
    inst: object,
    *,
    super_priority: bool,
) -> int:
    """Resolve an optional per-instance static-lane slot override.

    MCC's special-ring envelope is translated into the supported per-instance
    lane maps.  ``slots_override`` is lane metadata carried by those maps,
    not a new runtime key; honoring it here keeps the canonical lane contract
    effective without introducing host- or instance-specific scheduler code.
    """

    # Every enabled super instance owns one dedicated worker by default. An
    # explicit lane override may increase only that instance's super ring.
    default = (
        1
        if super_priority
        else max(1, int(getattr(config, "segment_priority_parallel_idle", 1) or 1))
    )
    settings_key = (
        "segment_super_priority_instance_settings"
        if super_priority
        else "segment_priority_instance_settings"
    )
    found, raw = _static_segment_setting_value(getattr(config, settings_key, {}), inst)
    if not found or not isinstance(raw, dict) or "slots_override" not in raw:
        return default
    try:
        override = int(raw.get("slots_override"))
    except (TypeError, ValueError):
        return default
    return max(1, override)


def _segment_whitelist_effective_setting(config: AgentConfig, inst: object) -> set[int]:
    """Backward-compatible effective whitelist, including legacy realtime IDs."""

    ids = _segment_priority_effective_setting(config, inst)
    found, raw = _instance_scoped_whitelist_entry(
        getattr(config, "segment_whitelist_instance_settings", {}),
        inst,
    )
    if found and isinstance(raw, dict):
        ids.update(_to_int_list(_whitelist_realtime_block(raw).get("ids", [])))
    return ids


def _segment_whitelist_realtime_setting(
    config: AgentConfig,
    inst: object,
) -> SegmentWhitelistRealtimeSetting:
    if bool(getattr(config, "disable_whitelist", False)):
        return SegmentWhitelistRealtimeSetting()
    found, raw = _instance_scoped_whitelist_entry(
        getattr(config, "segment_whitelist_instance_settings", {}),
        inst,
    )
    if not found or not isinstance(raw, dict):
        return SegmentWhitelistRealtimeSetting()
    if ("enabled" in raw and not _to_boolish(raw.get("enabled"), True)) or _to_boolish(
        raw.get("disable_whitelist"),
        False,
    ):
        return SegmentWhitelistRealtimeSetting()
    block = _whitelist_realtime_block(raw)
    ids = frozenset(x for x in _to_int_list(block.get("ids", [])) if x > 0)
    return SegmentWhitelistRealtimeSetting(
        ids=ids,
        interval_sec=_whitelist_realtime_int(block, "interval_sec"),
        parallel=_whitelist_realtime_int(block, "parallel", maximum=16),
    )


def _priority_realtime_segment_ids(setting: SegmentWhitelistRealtimeSetting) -> frozenset[int]:
    """Return only segments that explicitly request periodic execution."""
    return setting.ids if setting.enabled else frozenset()


def _campaign_whitelist_effective_setting(config: AgentConfig, inst: object) -> set[int]:
    if bool(getattr(config, "disable_whitelist", False)):
        return set()

    global_ids = set(getattr(config, "campaign_whitelist", []) or []) | _load_id_file(
        getattr(config, "campaign_whitelist_file", None)
    )
    settings = getattr(config, "campaign_whitelist_instance_settings", {})
    found, raw = _instance_scoped_whitelist_entry(settings, inst)
    if not found:
        return global_ids
    if isinstance(raw, dict):
        if "enabled" in raw and not _to_boolish(raw.get("enabled"), True):
            return set()
        if _to_boolish(raw.get("disable_whitelist"), False):
            return set()
        ids_raw = raw.get(
            "campaign_whitelist",
            raw.get("campaigns", raw.get("ids", raw.get("whitelist", []))),
        )
        ids = set(_to_int_list(ids_raw))
        ids.update(_to_int_list(_whitelist_realtime_block(raw).get("ids", [])))
        return ids
    if isinstance(raw, bool):
        return global_ids if raw else set()
    return set(_to_int_list(raw))


def _campaign_whitelist_realtime_setting(
    config: AgentConfig,
    inst: object,
) -> CampaignWhitelistRealtimeSetting:
    if bool(getattr(config, "disable_whitelist", False)):
        return CampaignWhitelistRealtimeSetting()
    found, raw = _instance_scoped_whitelist_entry(
        getattr(config, "campaign_whitelist_instance_settings", {}),
        inst,
    )
    if not found or not isinstance(raw, dict):
        return CampaignWhitelistRealtimeSetting()
    if ("enabled" in raw and not _to_boolish(raw.get("enabled"), True)) or _to_boolish(
        raw.get("disable_whitelist"),
        False,
    ):
        return CampaignWhitelistRealtimeSetting()
    block = _whitelist_realtime_block(raw)
    ids = frozenset(x for x in _to_int_list(block.get("ids", [])) if x > 0)
    return CampaignWhitelistRealtimeSetting(
        ids=ids,
        rebuild_interval_sec=_whitelist_realtime_int(block, "rebuild_interval_sec"),
        trigger_interval_sec=_whitelist_realtime_int(block, "trigger_interval_sec"),
        rebuild_parallel=_whitelist_realtime_int(block, "rebuild_parallel", maximum=16),
        trigger_parallel=_whitelist_realtime_int(block, "trigger_parallel", maximum=16),
    )


def _priority_realtime_campaign_rebuild_ids(
    setting: CampaignWhitelistRealtimeSetting,
) -> frozenset[int]:
    """Return only campaigns that explicitly request periodic rebuilds.

    A campaign whitelist is queue priority, not a periodic execution request.
    The normal due-work planner places those IDs in its priority ring.
    """
    return setting.ids if setting.rebuild_enabled else frozenset()


def _segment_whitelist_file_key_for_instance(inst: object) -> str:
    for attr in ("instance_uid", "primary_domain", "name", "root"):
        value = str(getattr(inst, attr, "") or "").strip()
        if value:
            return value
    keys = _instance_setting_keys(inst)
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
    keys = set(_instance_setting_keys(inst))
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


def _published_campaign_whitelist_ids(
    db: MauticDB,
    whitelist: set[int],
    sql_ctx: dict[str, str],
) -> list[int]:
    ids = sorted({int(x) for x in whitelist if int(x) > 0})
    if not ids:
        return []
    id_sql = ",".join(str(x) for x in ids)
    query = (
        "SELECT c.id "
        "FROM {prefix}campaigns c "
        f"WHERE c.id IN ({id_sql}) "
        "AND c.is_published = 1 "
        "AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
        "AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
        "ORDER BY c.id ASC"
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
        for key in _instance_setting_keys(inst) + ["default"]:
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


def _is_mautic_message_send_template(template: str) -> bool:
    return bool(re.search(r"\bmautic:messages:send\b", str(template or "")))


def _legacy_message_queue_job_setting(config: AgentConfig) -> tuple[bool, int] | None:
    for job in config.scheduled_jobs:
        if _is_mautic_message_send_template(job.command_template):
            return bool(job.enabled), normalize_message_queue_interval(job.interval_sec)
    return None


def _adopt_message_queue_settings(
    config: AgentConfig,
    installs: list[object],
    cron_migrations: dict[str, int],
) -> tuple[dict[str, object], list[tuple[str, bool, int, str]]]:
    current = getattr(config, "message_queue_instance_settings", {})
    next_settings: dict[str, object] = dict(current) if isinstance(current, dict) else {}
    legacy_job_setting = _legacy_message_queue_job_setting(config)
    normalized_cron_migrations = {
        str(Path(str(root or "/")).resolve()): normalize_message_queue_interval(interval)
        for root, interval in cron_migrations.items()
        if str(root or "").strip()
    }
    added: list[tuple[str, bool, int, str]] = []

    for inst in installs:
        if not supports_message_queue(inst):
            continue
        keys = message_queue_instance_setting_keys(inst)
        if not keys or any(key in next_settings for key in [*keys, "default"]):
            continue
        normalized_root = str(Path(str(getattr(inst, "root", "") or "/")).resolve())
        if normalized_root in normalized_cron_migrations:
            enabled = True
            interval_sec = normalized_cron_migrations[normalized_root]
            source = "cron"
        elif legacy_job_setting is not None:
            enabled, interval_sec = legacy_job_setting
            source = "legacy_jobs"
        else:
            enabled = bool(getattr(config, "message_queue_enabled", False))
            interval_sec = normalize_message_queue_interval(
                getattr(config, "message_queue_interval_sec", MESSAGE_QUEUE_DEFAULT_INTERVAL_SEC)
            )
            source = "default"
        next_settings[keys[0]] = {
            "enabled": bool(enabled),
            "interval_sec": int(interval_sec),
        }
        added.append((keys[0], bool(enabled), int(interval_sec), source))
    return next_settings, added


def _with_message_queue_instance_settings(
    config: AgentConfig,
    settings: dict[str, object],
) -> AgentConfig:
    """Return updated runtime config without mutating the frozen dataclass."""
    return replace(config, message_queue_instance_settings=settings)


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
    for key in _instance_setting_keys(inst) + ["default"]:
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
    grace_min = max(
        5,
        int(
            getattr(
                config,
                "page_hits_orphan_cleanup_grace_min",
                DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN,
            )
            or DEFAULT_PAGE_HITS_ORPHAN_CLEANUP_GRACE_MIN
        ),
    )
    max_run_sec = max(30, int(getattr(config, "page_hits_orphan_cleanup_max_run_sec", 180) or 180))
    settings = getattr(config, "page_hits_orphan_cleanup_instance_settings", {})
    if not isinstance(settings, dict):
        return enabled, interval, schedule_type, cron_expr, window_min, window_start, window_end, batch_size, batches, sleep_sec, grace_min, max_run_sec
    for key in _instance_setting_keys(inst) + ["default"]:
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
    *,
    instance_states: object = None,
) -> None:
    stable_keys = set(applied_keys) & _STABLE_RUNTIME_KEYS
    eff_runtime = runtime_effective_map(config)
    if isinstance(instance_states, dict):
        # ``runtime_effective_map`` is a profile-shaped view and can omit
        # instance-scoped maps (for example while a named profile is being
        # rebased).  Desired instance rows are canonical and must be merged
        # into the persistence view before writing the mutable config.
        canonical_instance_runtime = merge_instance_desired_states({}, instance_states)
        eff_runtime = merge_instance_desired_states(eff_runtime, instance_states)
        stable_keys.update(set(canonical_instance_runtime) & _STABLE_RUNTIME_KEYS)
    if not stable_keys:
        return
    stable_keys_sorted = sorted(stable_keys)
    updates: dict[str, object] = {}
    for key in stable_keys_sorted:
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
                ",".join(stable_keys_sorted),
            )
    except Exception as e:
        logging.warning("runtime-overrides persist to config failed: %s", e)
    if (
        "segment_whitelist_instance_settings" in stable_keys
        or "segment_whitelist" in stable_keys
        or "segment_whitelist_file" in stable_keys
    ):
        _sync_segment_whitelist_file(config, installs)
    if "form_embed_instance_settings" in stable_keys:
        _sync_and_publish_form_embed_status(config, installs, reason="runtime_override")


def _remove_absent_stable_runtime_from_config(config: AgentConfig, runtime: dict[str, object]) -> None:
    absent = _STABLE_RUNTIME_KEYS - set(runtime)
    if not absent:
        return
    try:
        path, changed = remove_runtime_values(config.config_file_path, absent)
        if changed:
            logging.info("runtime-overrides removed absent stable keys from config (%s)", path)
    except Exception as e:
        logging.warning("runtime-overrides stable-key cleanup failed: %s", e)


def _sync_and_publish_form_embed_status(
    config: AgentConfig,
    installs: list[object] | None,
    *,
    reason: str,
) -> bool:
    """Reconcile saved form policies and retain their observed status in MCC."""
    settings = getattr(config, "form_embed_instance_settings", {})
    if not isinstance(settings, dict) or not settings:
        return False
    try:
        result = sync_form_embed_settings(config, list(installs or []))
        observed = result.get("instances", {}) if isinstance(result, dict) else {}
        pushed = push_runtime_overrides(
            config,
            {"form_embed_instance_status": observed if isinstance(observed, dict) else {}},
            merge=True,
        )
        if str(pushed.get("status", "")).lower() != "ok":
            logging.warning("form embed status push failed (%s): %s", reason, pushed.get("reason", "unknown"))
            return False
        logging.info("form embed status reconciled and published (%s)", reason)
        return True
    except Exception as e:
        logging.warning("form embed settings sync failed (%s): %s", reason, e)
        return False


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


def _toposort_sql_segment_ids(
    ids: list[int],
    rules: dict[int, SQLSegmentRule],
    priority_order: dict[int, int] | None = None,
) -> list[int]:
    uniq = list(dict.fromkeys([x for x in ids if x in rules]))
    if not uniq:
        return []
    order = dict(priority_order or {})

    def _order_key(sid: int) -> tuple[int, int]:
        return (int(order.get(int(sid), len(order) + int(sid))), int(sid))

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
    queue = sorted([sid for sid in uniq if indeg[sid] == 0], key=_order_key)
    ordered: list[int] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing.get(current, set()), key=_order_key):
            indeg[nxt] = max(0, indeg[nxt] - 1)
            if indeg[nxt] == 0 and nxt not in queue and nxt not in ordered:
                queue.append(nxt)
                queue.sort(key=_order_key)
    if len(ordered) == len(uniq):
        return ordered
    # Keep deterministic order on dependency cycles/malformed graphs.
    tail = [sid for sid in sorted(uniq, key=_order_key) if sid not in set(ordered)]
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
    due_order = {sid: idx for idx, sid in enumerate(due_ids)}
    ordered = _toposort_sql_segment_ids(list(planned), rules, priority_order=due_order)
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


_SEGMENT_SQL_WORKERS_LOCK = threading.Lock()
_SEGMENT_SQL_WORKERS: dict[tuple[str, int], dict[str, object]] = {}


def _segment_sql_worker_prune() -> None:
    with _SEGMENT_SQL_WORKERS_LOCK:
        for key, meta in list(_SEGMENT_SQL_WORKERS.items()):
            thread = meta.get("thread")
            if isinstance(thread, threading.Thread) and thread.is_alive():
                continue
            _SEGMENT_SQL_WORKERS.pop(key, None)


def _segment_sql_worker_ids(root: str) -> set[int]:
    _segment_sql_worker_prune()
    root_s = str(root)
    with _SEGMENT_SQL_WORKERS_LOCK:
        return {int(sid) for (row_root, sid), _meta in _SEGMENT_SQL_WORKERS.items() if row_root == root_s}


def _segment_sql_worker_running(root: str, segment_id: int | None = None) -> bool:
    ids = _segment_sql_worker_ids(root)
    if segment_id is None:
        return bool(ids)
    try:
        sid = int(segment_id)
    except Exception:
        return False
    return sid in ids


def _segment_sql_worker_count(root: str) -> int:
    return len(_segment_sql_worker_ids(root))


def _segment_sql_worker_total_count() -> int:
    _segment_sql_worker_prune()
    with _SEGMENT_SQL_WORKERS_LOCK:
        return len(_SEGMENT_SQL_WORKERS)


_SCHEDULER_SEGMENT_TASK_TYPES = frozenset({"segment", "segment_sql", "import", *_STATIC_SEGMENT_TASK_TYPES})
_SCHEDULER_CAMPAIGN_TASK_TYPES = frozenset({"campaign_update", "campaign_trigger", "campaign_rebuild"})
_SCHEDULER_SUPER_PRIORITY_TASK_TYPES = frozenset({"segment_super_priority"})
_SCHEDULER_PRIORITY_SEGMENT_TASK_TYPES = frozenset(
    {"segment", "segment_priority", "segment_super_priority", "segment_recurring_priority"}
)
_SCHEDULER_PRIORITY_CAMPAIGN_TASK_TYPES = frozenset(
    {"campaign_update", "campaign_rebuild", "campaign_trigger", "campaign_native_fallback"}
)
_SCHEDULER_PRIORITY_WORK_TASK_TYPES = (
    _SCHEDULER_PRIORITY_SEGMENT_TASK_TYPES | _SCHEDULER_PRIORITY_CAMPAIGN_TASK_TYPES
)
_SCHEDULER_WORK_TASK_TYPES = _SCHEDULER_SEGMENT_TASK_TYPES | _SCHEDULER_CAMPAIGN_TASK_TYPES
_SCHEDULER_SHARED_WORK_TASK_TYPES = frozenset(
    (_SCHEDULER_WORK_TASK_TYPES | _SCHEDULER_PRIORITY_WORK_TASK_TYPES)
    - _SCHEDULER_SUPER_PRIORITY_TASK_TYPES
)
_SCHEDULER_BASELINE_SEGMENT_TASK_TYPES = frozenset(
    _SCHEDULER_SEGMENT_TASK_TYPES - _SCHEDULER_SUPER_PRIORITY_TASK_TYPES
)
_SCHEDULER_BASELINE_CAMPAIGN_TASK_TYPES = _SCHEDULER_CAMPAIGN_TASK_TYPES
_SCHEDULER_BACKGROUND_TASK_TYPES = frozenset({"segment", "segment_sql"})
_SCHEDULER_RESERVED_SAFETY_TASK_TYPES = frozenset({"job:plugin_operation_safety"})
_SCHEDULER_PRIORITY_PENDING_ROOTS: frozenset[str] = frozenset()
_SCHEDULER_IMPORTANT_PENDING_ROOTS: frozenset[str] = frozenset()
_SCHEDULER_FAIRNESS_PROMOTED_ROOTS: frozenset[str] = frozenset()
_SCHEDULER_FAIRNESS_RESERVED_ROOT = ""
_SCHEDULER_FAIRNESS_CLAIMED_ROOTS: set[str] = set()
_SCHEDULER_FAIRNESS_LOCK = threading.Lock()
_SCHEDULER_BASELINE_ADMISSION_INTERVAL_SEC = 5.0
_SCHEDULER_BASELINE_LAST_LAUNCH_BY_LANE: dict[str, float] = {}
_SCHEDULER_DISTRIBUTION_LOCK = threading.RLock()
_SCHEDULER_DISTRIBUTION_READY_ROOTS: tuple[str, ...] = ()
_SCHEDULER_DISTRIBUTION_SERVED_ROOTS: set[str] = set()
_SCHEDULER_DISTRIBUTION_CURSOR = 0


def _scheduler_round_robin_order(
    ready_roots: Iterable[object],
    worker_slots: int,
    *,
    cursor: int = 0,
) -> list[str]:
    """Return the sliding root order used by shared regular admission.

    The first pass contains each root at most once. When fewer roots than
    workers are ready, subsequent passes may repeat roots, which is the
    intended independent-worker behavior under the per-root/profile guards.
    """

    roots = list(dict.fromkeys(str(root).strip() for root in ready_roots if str(root).strip()))
    if not roots:
        return []
    start = int(cursor or 0) % len(roots)
    rotated = roots[start:] + roots[:start]
    return [rotated[index % len(rotated)] for index in range(max(1, int(worker_slots or 1)))]


def _set_scheduler_distribution_roots(
    ready_roots: Iterable[object],
    *,
    worker_slots: int,
    cursor: int | None = None,
) -> None:
    global _SCHEDULER_DISTRIBUTION_READY_ROOTS, _SCHEDULER_DISTRIBUTION_CURSOR
    raw_roots = tuple(dict.fromkeys(str(root).strip() for root in ready_roots if str(root).strip()))
    with _SCHEDULER_DISTRIBUTION_LOCK:
        if cursor is not None:
            _SCHEDULER_DISTRIBUTION_CURSOR = max(0, int(cursor))
        roots = tuple(
            _scheduler_round_robin_order(
                raw_roots,
                len(raw_roots),
                cursor=_SCHEDULER_DISTRIBUTION_CURSOR,
            )
        )
        if roots != _SCHEDULER_DISTRIBUTION_READY_ROOTS:
            _SCHEDULER_DISTRIBUTION_READY_ROOTS = roots
            _SCHEDULER_DISTRIBUTION_SERVED_ROOTS.clear()
        if roots:
            _SCHEDULER_DISTRIBUTION_CURSOR = (
                _SCHEDULER_DISTRIBUTION_CURSOR + max(1, int(worker_slots or 1))
            ) % len(roots)


def _scheduler_shared_admission_allowed(root: str, task_type: str) -> bool:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
        return True
    with _SCHEDULER_DISTRIBUTION_LOCK:
        ready = set(_SCHEDULER_DISTRIBUTION_READY_ROOTS)
        if not ready or str(root) not in ready:
            return True
        unserved = ready - _SCHEDULER_DISTRIBUTION_SERVED_ROOTS
        if unserved and str(root) in _SCHEDULER_DISTRIBUTION_SERVED_ROOTS:
            return False
        if not unserved:
            _SCHEDULER_DISTRIBUTION_SERVED_ROOTS.clear()
        return True


def _scheduler_shared_admission_ready(root: str, task_type: str) -> bool:
    """Read the RR gate without mutating its served-root cursor."""

    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
        return True
    with _SCHEDULER_DISTRIBUTION_LOCK:
        ready = set(_SCHEDULER_DISTRIBUTION_READY_ROOTS)
        if not ready or str(root) not in ready:
            return True
        unserved = ready - _SCHEDULER_DISTRIBUTION_SERVED_ROOTS
        return not (unserved and str(root) in _SCHEDULER_DISTRIBUTION_SERVED_ROOTS)


def _mark_scheduler_shared_admission(root: str, task_type: str) -> None:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
        return
    with _SCHEDULER_DISTRIBUTION_LOCK:
        if str(root) in _SCHEDULER_DISTRIBUTION_READY_ROOTS:
            _SCHEDULER_DISTRIBUTION_SERVED_ROOTS.add(str(root))


def _set_scheduler_important_pending_roots(roots: Iterable[object]) -> None:
    global _SCHEDULER_IMPORTANT_PENDING_ROOTS
    with _SCHEDULER_DISTRIBUTION_LOCK:
        _SCHEDULER_IMPORTANT_PENDING_ROOTS = frozenset(
            str(root).strip() for root in roots if str(root).strip()
        )


def _scheduler_ready_important_ids(store: object, root: str) -> set[int]:
    try:
        payload = store.get_runtime_sync(_segment_cycle_key(str(root)))
        pending = set(_segment_cycle_pending_important_ids(payload))
        context = store.get_runtime_sync(_segment_dispatch_context_key(str(root)))
    except Exception:
        return set()
    if isinstance(context, Mapping):
        # The dispatch context is the full authoritative execution snapshot;
        # the pending guard is intentionally display/diagnostic telemetry and
        # may truncate or lag blocked IDs.
        pending.difference_update(_normalize_segment_ids(context.get("blocked_ids")))
        if context.get("scheduler_pause") or context.get("backup_pause") or context.get("dispatch_pause"):
            return set()
        try:
            if float(context.get("db_pause_until") or 0.0) > time.time():
                return set()
        except (TypeError, ValueError):
            return set()
    return pending


def _refresh_scheduler_important_root(
    store: object,
    root: str,
    *,
    blocked_ids: Iterable[object] | None = None,
) -> bool:
    """Refresh one root's ready-important marker after claim/terminal changes."""

    pending_ids = _scheduler_ready_important_ids(store, root)
    if blocked_ids is not None:
        pending_ids.difference_update(_normalize_segment_ids(blocked_ids))
    pending = bool(pending_ids)
    global _SCHEDULER_IMPORTANT_PENDING_ROOTS
    current = set(_SCHEDULER_IMPORTANT_PENDING_ROOTS)
    if pending:
        current.add(str(root))
    else:
        current.discard(str(root))
    _set_scheduler_important_pending_roots(current)
    return pending


def _scheduler_important_root_dispatchable(
    config: AgentConfig,
    running: dict[str, "RunningTask"],
    root: str,
    *,
    priority_executor: object | None = None,
) -> bool:
    """Return whether an important root can take a real segment admission now.

    A pending important item on a full instance is not a global remaining-tier
    barrier: another root may use a free host worker.  The RR check is read
    without advancing fairness so this helper is safe to call while deciding
    whether a different root's remaining item may proceed.
    """

    if not _scheduler_shared_admission_ready(root, "segment"):
        return False
    host_slots = _scheduler_host_slots_available(
        config,
        running,
        "segment",
        root=root,
        priority_executor=priority_executor,
    )
    if host_slots is not None and host_slots <= 0:
        return False
    instance_slots = _scheduler_instance_slots_available(
        config,
        running,
        root=root,
        task_type="segment",
        priority_executor=priority_executor,
    )
    return instance_slots is None or instance_slots > 0


def _scheduler_segment_entity_allowed(
    store: object,
    root: str,
    entity_id: object,
    *,
    config: AgentConfig | None = None,
    running: dict[str, "RunningTask"] | None = None,
    priority_executor: object | None = None,
) -> bool:
    """Gate fresh remaining work behind currently dispatchable important work."""

    try:
        payload = store.get_runtime_sync(_segment_cycle_key(str(root)))
        tier = _segment_cycle_item_tier(payload, entity_id)
    except Exception:
        return True
    _refresh_scheduler_important_root(store, root)
    if tier != "remaining":
        return True
    important_roots = set(_SCHEDULER_IMPORTANT_PENDING_ROOTS)
    if config is not None and running is not None:
        important_roots = {
            pending_root
            for pending_root in important_roots
            if _scheduler_important_root_dispatchable(
                config,
                running,
                pending_root,
                priority_executor=priority_executor,
            )
        }
    return not important_roots


def _scheduler_task_lane(task_type: str | None) -> str | None:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_SEGMENT_TASK_TYPES or normalized in _SCHEDULER_PRIORITY_SEGMENT_TASK_TYPES:
        return "segment"
    if normalized in _SCHEDULER_CAMPAIGN_TASK_TYPES or normalized in _SCHEDULER_PRIORITY_CAMPAIGN_TASK_TYPES:
        return "campaign"
    return None


def _scheduler_baseline_lane(task_type: str | None) -> str | None:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_BASELINE_SEGMENT_TASK_TYPES:
        return "segment"
    if normalized in _SCHEDULER_BASELINE_CAMPAIGN_TASK_TYPES:
        return "campaign"
    return None


def _scheduler_host_lane_limit(config: AgentConfig, task_type: str | None) -> int:
    host_limit = max(0, int(getattr(config, "scheduler_host_max_parallel", 0) or 0))
    lane = _scheduler_task_lane(task_type)
    if host_limit <= 0 or lane is None:
        return host_limit
    if lane == "segment":
        if str(getattr(config, "segment_mode", "")).strip().lower() == "classic_loop":
            configured = 1
        else:
            configured = max(0, int(getattr(config, "segment_priority_parallel_idle", 0) or 0)) + max(
                0,
                int(getattr(config, "segment_regular_parallel_idle", 0) or 0),
            )
    else:
        configured = max(0, int(getattr(config, "campaign_total_parallel", 0) or 0))
        if configured <= 0:
            configured = max(
                max(0, int(getattr(config, "campaign_priority_parallel", 0) or 0))
                + max(0, int(getattr(config, "campaign_regular_parallel", 0) or 0)),
                max(0, int(getattr(config, "campaign_trigger_priority_parallel", 0) or 0))
                + max(0, int(getattr(config, "campaign_trigger_regular_parallel", 0) or 0)),
                max(0, int(getattr(config, "campaign_rebuild_priority_parallel", 0) or 0))
                + max(0, int(getattr(config, "campaign_rebuild_regular_parallel", 0) or 0)),
            )
    return min(host_limit, configured if configured > 0 else host_limit)


def _scheduler_capacity_key(root: str, task_type: str, entity_id: object) -> str:
    """Return one logical worker identity for host/instance accounting.

    Static wrappers, ordinary/adopted rows and native campaign variants can
    describe the same underlying Mautic work with different task type labels.
    Capacity must count that logical worker once, while keeping imports and
    full-segment workers distinct.
    """

    normalized = str(task_type or "").strip().lower()
    if normalized in {
        "segment",
        "segment_sql",
        "segment_priority",
        "segment_super_priority",
        "segment_recurring_priority",
    }:
        kind = "segment"
    elif normalized in {"campaign_update", "campaign_rebuild", "campaign_trigger"}:
        kind = "campaign"
    elif normalized == "campaign_native_fallback":
        kind = "campaign_native_fallback"
    elif normalized == "import":
        kind = "import"
    else:
        kind = normalized
    entity = "-" if entity_id is None else str(entity_id)
    return f"{root}|{kind}|{entity}"


def _scheduler_priority_active_items(
    priority_executor: object | None = None,
) -> list[tuple[str, str, str]]:
    executor = priority_executor if priority_executor is not None else _ACTIVE_PRIORITY_EXECUTOR
    if executor is None:
        return []
    active_items = getattr(executor, "active_items", None)
    if not callable(active_items):
        return []
    try:
        return [
            (str(key), str(root), str(task_type))
            for key, root, task_type, _lane in active_items(_SCHEDULER_SHARED_WORK_TASK_TYPES)
        ]
    except Exception:
        return []


def _scheduler_actual_work_items(
    running: dict[str, "RunningTask"],
    *,
    priority_executor: object | None = None,
) -> dict[str, tuple[str, str]]:
    """Build a deduplicated logical-worker view for capacity decisions."""

    items: dict[str, tuple[str, str]] = {}
    known_types = _SCHEDULER_SHARED_WORK_TASK_TYPES
    dedicated_super_keys: set[str] = set()
    for key, task in list(running.items()):
        task_type = str(getattr(task, "task_type", "") or "").strip().lower()
        if task_type in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
            root = str(getattr(task, "root", "") or "").strip()
            if root:
                dedicated_super_keys.add(
                    _scheduler_capacity_key(root, task_type, getattr(task, "entity_id", key))
                )
    executor = priority_executor if priority_executor is not None else _ACTIVE_PRIORITY_EXECUTOR
    active_items = getattr(executor, "active_items", None) if executor is not None else None
    if callable(active_items):
        try:
            for key, root, task_type, _lane in active_items(_SCHEDULER_SUPER_PRIORITY_TASK_TYPES):
                dedicated_super_keys.add(
                    _scheduler_capacity_key(
                        str(root),
                        str(task_type),
                        str(key).rsplit("|", 1)[-1],
                    )
                )
        except Exception:
            pass
    for key, task in list(running.items()):
        task_type = str(getattr(task, "task_type", "") or "").strip().lower()
        if task_type not in known_types:
            continue
        root = str(getattr(task, "root", "") or "").strip()
        if not root:
            continue
        entity_id = getattr(task, "entity_id", key)
        capacity_key = _scheduler_capacity_key(root, task_type, entity_id)
        if capacity_key in dedicated_super_keys:
            # A generic adopted wrapper can observe the same PID/entity as a
            # dedicated super lane. The authoritative lane owner wins, and
            # the super worker remains outside shared regular accounting.
            continue
        items.setdefault(capacity_key, (root, task_type))

    with _SEGMENT_SQL_WORKERS_LOCK:
        sql_workers = list(_SEGMENT_SQL_WORKERS)
    for root, segment_id in sql_workers:
        root_value = str(root or "").strip()
        if not root_value:
            continue
        capacity_key = _scheduler_capacity_key(root_value, "segment_sql", segment_id)
        items.setdefault(capacity_key, (root_value, "segment_sql"))

    for key, root, task_type in _scheduler_priority_active_items(priority_executor):
        entity_token = str(key).rsplit("|", 1)[-1]
        entity_id: object = None if entity_token == "-" else entity_token
        capacity_key = _scheduler_capacity_key(root, task_type, entity_id)
        if capacity_key in dedicated_super_keys:
            continue
        items.setdefault(capacity_key, (root, task_type))
    return items


def _scheduler_super_priority_active_count(
    running: dict[str, "RunningTask"],
    *,
    root: str | None = None,
    priority_executor: object | None = None,
) -> int:
    """Count dedicated super workers without touching shared regular capacity."""

    count = sum(
        1
        for task in running.values()
        if str(getattr(task, "task_type", "") or "").strip().lower()
        in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES
        and (root is None or str(getattr(task, "root", "") or "") == str(root))
    )
    executor = priority_executor if priority_executor is not None else _ACTIVE_PRIORITY_EXECUTOR
    active_items = getattr(executor, "active_items", None) if executor is not None else None
    if callable(active_items):
        try:
            count += sum(
                1
                for _key, active_root, task_type, _lane in active_items(
                    _SCHEDULER_SUPER_PRIORITY_TASK_TYPES
                )
                if root is None or str(active_root) == str(root)
            )
        except Exception:
            pass
    return count


def _scheduler_actual_work_count(
    running: dict[str, "RunningTask"],
    *,
    root: str | None = None,
    lane: str | None = None,
    priority_executor: object | None = None,
) -> int:
    return sum(
        1
        for item_root, task_type in _scheduler_actual_work_items(
            running,
            priority_executor=priority_executor,
        ).values()
        if (root is None or item_root == root)
        and (lane is None or _scheduler_task_lane(task_type) == lane)
    )


def _scheduler_host_running_count(
    running: dict[str, "RunningTask"],
    task_type: str | None = None,
    *,
    priority_executor: object | None = None,
) -> int:
    return _scheduler_actual_work_count(
        running,
        lane=_scheduler_task_lane(task_type),
        priority_executor=priority_executor,
    )


def _scheduler_root_lane_running_count(
    running: dict[str, "RunningTask"],
    root: str,
    lane: str,
    *,
    priority_executor: object | None = None,
) -> int:
    return sum(
        1
        for item_root, task_type in _scheduler_actual_work_items(
            running,
            priority_executor=priority_executor,
        ).values()
        if item_root == root and _scheduler_baseline_lane(task_type) == lane
    )


def _scheduler_baseline_running_count(
    running: dict[str, "RunningTask"],
    lane: str | None = None,
) -> int:
    occupied = {
        (task.root, task_lane)
        for task in running.values()
        if (task_lane := _scheduler_baseline_lane(task.task_type)) is not None
        and (lane is None or task_lane == lane)
    }
    if lane in {None, "segment"}:
        with _SEGMENT_SQL_WORKERS_LOCK:
            occupied.update((root, "segment") for root, _sid in _SEGMENT_SQL_WORKERS)
    return len(occupied)


def _scheduler_host_slots_available(
    config: AgentConfig,
    running: dict[str, "RunningTask"],
    task_type: str | None = None,
    *,
    root: str = "",
    priority_executor: object | None = None,
) -> int | None:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_RESERVED_SAFETY_TASK_TYPES or normalized.startswith("job:plugin_operation"):
        return None
    if normalized in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
        # Super-priority owns a dedicated per-instance ring. Its lane limit is
        # enforced by the executor/static dispatcher, not by shared regular
        # host capacity.
        return None
    lane = _scheduler_task_lane(normalized)
    baseline_lane = _scheduler_baseline_lane(normalized)
    host_limit = max(0, int(getattr(config, "scheduler_host_max_parallel", 0) or 0))
    if (
        host_limit <= 0
        and baseline_lane is not None
        and root
        and _scheduler_root_lane_running_count(
            running,
            root,
            baseline_lane,
            priority_executor=priority_executor,
        ) <= 0
    ):
        # Without an explicit host safety cap every instance still owns one
        # lightweight baseline lane.  An explicit host cap is different: all
        # real work, including baseline work, must fit inside that hard cap.
        return 1
    if lane == "segment" and host_limit <= 0:
        # The selected profile's per-instance segment lanes are authoritative
        # unless an operator explicitly configures a host-wide safety cap.
        # Hardware-derived recommendations belong in metadata, not dispatch.
        return None
    if (
        bool(getattr(config, "scheduler_elastic_slots_enabled", True))
        and host_limit > 0
        and normalized in _SCHEDULER_SHARED_WORK_TASK_TYPES
    ):
        effective_limit = host_limit
        if normalized in _SCHEDULER_BACKGROUND_TASK_TYPES and host_limit > 1:
            reserve = max(0, int(getattr(config, "scheduler_emergency_reserved_slots", 1) or 0))
            effective_limit = max(1, host_limit - min(host_limit - 1, reserve))
        running_total = _scheduler_actual_work_count(
            running,
            priority_executor=priority_executor,
        )
        # Baselines are only outside the shared pool when the operator has not
        # configured a host-wide safety cap.  With an explicit cap, counting
        # only "additional" work would let one baseline per root bypass the
        # cap and fan out past the host limit.
        shared_running_total = running_total
        with _SCHEDULER_FAIRNESS_LOCK:
            reserved_root = _SCHEDULER_FAIRNESS_RESERVED_ROOT
            reservation_claimed = reserved_root in _SCHEDULER_FAIRNESS_CLAIMED_ROOTS
        if reserved_root and not reservation_claimed and root != reserved_root:
            effective_limit = max(0, effective_limit - 1)
        return max(0, effective_limit - shared_running_total)
    limit = _scheduler_host_lane_limit(config, task_type)
    if limit <= 0:
        return None
    if host_limit > 0:
        shared_lane_running = _scheduler_host_running_count(
            running,
            task_type,
            priority_executor=priority_executor,
        )
    else:
        shared_lane_running = max(
            0,
            _scheduler_host_running_count(
                running,
                task_type,
                priority_executor=priority_executor,
            ) - _scheduler_baseline_running_count(running, lane),
        )
    return max(0, limit - shared_lane_running)


def _set_scheduler_priority_pending_roots(roots: set[str]) -> None:
    global _SCHEDULER_PRIORITY_PENDING_ROOTS
    _SCHEDULER_PRIORITY_PENDING_ROOTS = frozenset(str(root) for root in roots if str(root))


def _set_scheduler_fairness_promoted_roots(roots: object) -> None:
    global _SCHEDULER_FAIRNESS_PROMOTED_ROOTS, _SCHEDULER_FAIRNESS_RESERVED_ROOT
    normalized_order = tuple(dict.fromkeys(str(root) for root in roots if str(root)))
    normalized = frozenset(normalized_order)
    with _SCHEDULER_FAIRNESS_LOCK:
        _SCHEDULER_FAIRNESS_PROMOTED_ROOTS = normalized
        _SCHEDULER_FAIRNESS_RESERVED_ROOT = normalized_order[0] if normalized_order else ""
        _SCHEDULER_FAIRNESS_CLAIMED_ROOTS.intersection_update({_SCHEDULER_FAIRNESS_RESERVED_ROOT})


def _mark_scheduler_fairness_claim(root: str, task_type: str) -> None:
    normalized_root = str(root or "").strip()
    normalized_task_type = str(task_type or "").strip().lower()
    if not normalized_root or normalized_task_type not in _SCHEDULER_WORK_TASK_TYPES:
        return
    with _SCHEDULER_FAIRNESS_LOCK:
        if normalized_root == _SCHEDULER_FAIRNESS_RESERVED_ROOT:
            _SCHEDULER_FAIRNESS_CLAIMED_ROOTS.add(normalized_root)


def _take_scheduler_fairness_claimed_roots() -> set[str]:
    with _SCHEDULER_FAIRNESS_LOCK:
        claimed = set(_SCHEDULER_FAIRNESS_CLAIMED_ROOTS)
        _SCHEDULER_FAIRNESS_CLAIMED_ROOTS.clear()
    return claimed


def _scheduler_baseline_launch_due(lane: str, now_ts: float | None = None) -> bool:
    now = float(time.time() if now_ts is None else now_ts)
    with _SCHEDULER_FAIRNESS_LOCK:
        previous = float(_SCHEDULER_BASELINE_LAST_LAUNCH_BY_LANE.get(lane, 0.0) or 0.0)
    return previous <= 0 or now - previous >= _SCHEDULER_BASELINE_ADMISSION_INTERVAL_SEC


def _mark_scheduler_baseline_launch(lane: str, started_at: float) -> None:
    with _SCHEDULER_FAIRNESS_LOCK:
        _SCHEDULER_BASELINE_LAST_LAUNCH_BY_LANE[str(lane)] = float(started_at)


def _scheduler_instance_slots_available(
    config: AgentConfig,
    running: dict[str, "RunningTask"],
    *,
    root: str,
    task_type: str,
    priority_executor: object | None = None,
) -> int | None:
    normalized = str(task_type or "").strip().lower()
    if normalized in _SCHEDULER_SUPER_PRIORITY_TASK_TYPES:
        return None
    if normalized not in _SCHEDULER_SHARED_WORK_TASK_TYPES:
        return None
    lane = _scheduler_baseline_lane(normalized)
    limit = max(0, int(getattr(config, "scheduler_instance_max_parallel", 0) or 0))
    if (
        limit <= 0
        and lane is not None
        and _scheduler_root_lane_running_count(
            running,
            root,
            lane,
            priority_executor=priority_executor,
        ) <= 0
    ):
        # The implicit per-root baseline remains available only when the
        # operator did not set an explicit instance cap.  Explicit caps must
        # count baseline tasks as real occupancy, just like host caps do.
        return 1
    if limit <= 0:
        return None
    if normalized in _SCHEDULER_BACKGROUND_TASK_TYPES and root in _SCHEDULER_PRIORITY_PENDING_ROOTS:
        limit = max(0, limit - 1)
    running_total = _scheduler_actual_work_count(
        running,
        root=root,
        priority_executor=priority_executor,
    )
    return max(0, limit - running_total)


def _segment_sql_running_state_ids(
    *,
    store: "TaskStore",
    root: str,
    config: AgentConfig,
    now_ts: float,
) -> set[int]:
    """Return SQL segment locks that are still fresh across daemon restarts."""
    orphan_after_sec = max(30, int(getattr(config, "segment_sql_orphan_after_sec", 900) or 900))
    out: set[int] = set()
    try:
        rows = store.list_runtime_sync("segment_sql_state:")
    except Exception:
        rows = []
    for _key, payload in rows:
        if not isinstance(payload, dict):
            continue
        if str(payload.get("root") or "") != str(root):
            continue
        if str(payload.get("status") or "").strip().lower() != "running":
            continue
        try:
            sid = int(payload.get("segment_id") or 0)
        except Exception:
            sid = 0
        if sid <= 0:
            continue
        heartbeat_at = float(payload.get("heartbeat_at") or payload.get("started_at") or 0.0)
        lock_orphan_after = max(30, int(payload.get("orphan_after_sec") or orphan_after_sec))
        if heartbeat_at > 0 and (float(now_ts) - heartbeat_at) <= float(lock_orphan_after):
            out.add(sid)
    return out


def _segment_sql_running_state_count(
    *,
    store: "TaskStore" | None,
    root: str,
    config: AgentConfig | None,
    now_ts: float | None = None,
) -> int:
    if store is None or config is None:
        return 0
    return len(
        _segment_sql_running_state_ids(
            store=store,
            root=root,
            config=config,
            now_ts=float(now_ts if now_ts is not None else time.time()),
        )
    )


def _segment_sql_active_db_rebuild_query_count(db: MauticDB | None) -> int:
    if db is None:
        return 0
    try:
        rows = db.fetch_processlist(limit=500)
    except Exception:
        return 0
    count = 0
    for row in rows:
        info = str(row.get("Info") or row.get("info") or "").strip()
        command = str(row.get("Command") or row.get("command") or "").strip().lower()
        if command and command not in {"query", "execute"}:
            continue
        info_l = info.lower()
        if "mcd_tmp_segment_leads" not in info_l:
            continue
        if "insert ignore into" not in info_l:
            continue
        count += 1
    return count


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


def _refresh_mautic_segment_count_cache(
    *,
    root: str,
    segment_id: int,
    count: int,
    php_bin: str,
    run_as_user: str | None,
    timeout_sec: int = 30,
) -> bool:
    """Refresh Mautic's segment count cache after direct SQL rebuilds."""
    root_path = Path(root)
    if not (root_path / "vendor" / "autoload.php").exists() or not (root_path / "app" / "AppKernel.php").exists():
        return False

    php = str(php_bin or "php").strip() or "php"
    sid = int(segment_id)
    contact_count = max(0, int(count))
    script = f"""
<?php
require_once __DIR__ . '/vendor/autoload.php';
require_once __DIR__ . '/app/AppKernel.php';
$kernel = new AppKernel('prod', false);
$kernel->boot();
$container = $kernel->getContainer();
$helper = $container->get('mautic.helper.segment.count.cache');
$helper->setSegmentContactCount({sid}, {contact_count});
$kernel->shutdown();
"""
    cmd = [php]
    user = str(run_as_user or "").strip()
    if user:
        cmd = ["sudo", "-u", user, php]
    try:
        proc = subprocess.run(
            cmd,
            input=script,
            cwd=str(root_path),
            text=True,
            capture_output=True,
            timeout=max(5, int(timeout_sec)),
        )
    except Exception as e:
        logging.warning("[%s] segment_sql count-cache refresh failed id=%s: %s", root, sid, e)
        return False
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"rc={proc.returncode}"
        logging.warning("[%s] segment_sql count-cache refresh failed id=%s: %s", root, sid, detail)
        return False
    return True


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
    max_per_tick: int | None = None,
    ring_label: str = "segment_sql",
    async_worker: bool = False,
    sql_db_running_count: int | None = None,
    on_launch=None,
    on_terminal=None,
    dynamic_blocked=None,
) -> int:
    if not config.segment_sql_ring_enabled:
        return 0
    configured_limit = (
        getattr(config, "segment_sql_ring_max_per_tick", 1)
        if max_per_tick is None
        else max_per_tick
    )
    limit = max(0, int(configured_limit or 0))
    if limit <= 0 or not ring or not rules:
        return 0
    host_slots = _scheduler_host_slots_available(config, running, "segment_sql")
    if host_slots is not None and host_slots <= 0:
        return 0
    instance_slots = _scheduler_instance_slots_available(
        config,
        running,
        root=root,
        task_type="segment_sql",
    )
    if instance_slots is not None and instance_slots <= 0:
        return 0
    if async_worker and max(
        _segment_sql_worker_count(root),
        _segment_sql_running_state_count(store=store, root=root, config=config, now_ts=now_ts),
        max(0, int(sql_db_running_count or 0)),
    ) >= limit:
        return 0
    launched = 0
    scans = len(ring)
    while scans > 0 and launched < limit:
        host_slots = _scheduler_host_slots_available(config, running, "segment_sql")
        if host_slots is not None and host_slots <= 0:
            break
        instance_slots = _scheduler_instance_slots_available(
            config,
            running,
            root=root,
            task_type="segment_sql",
        )
        if instance_slots is not None and instance_slots <= 0:
            break
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
        if dynamic_blocked is not None and dynamic_blocked(sid):
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

        def _execute_rebuild() -> bool:
            try:
                res = db.rebuild_segment_membership(
                    segment_id=sid,
                    select_query_template=rule.select_sql,
                    context=sql_ctx,
                    statement_timeout_sec=int(getattr(config, "segment_sql_statement_timeout_sec", 1800) or 1800),
                )
                _refresh_mautic_segment_count_cache(
                    root=root,
                    segment_id=sid,
                    count=int(res.get("inserted_count", res.get("selected_count", 0)) or 0),
                    php_bin=str(getattr(config, "php_bin", "php") or "php"),
                    run_as_user=getattr(config, "mautic_run_as_user", None),
                )
                _mark_segment_finished(root, sid, now_ts=time.time())
                done_set.add(sid)
                _segment_sql_finish(
                    store=store,
                    root=root,
                    segment_id=sid,
                    owner=owner,
                    now_ts=time.time(),
                    result="ok",
                    note="rebuilt",
                    extra={
                        "ring": str(ring_label or "segment_sql"),
                        "selected_count": int(res.get("selected_count", 0) or 0),
                        "deleted_count": int(res.get("deleted_count", 0) or 0),
                        "inserted_count": int(res.get("inserted_count", 0) or 0),
                        "duration_sec": float(res.get("duration_sec", 0.0) or 0.0),
                    },
                )
                logging.info(
                    "[%s] %s rebuilt id=%s selected=%s inserted=%s deleted=%s duration=%.2fs",
                    root,
                    str(ring_label or "segment_sql"),
                    sid,
                    int(res.get("selected_count", 0) or 0),
                    int(res.get("inserted_count", 0) or 0),
                    int(res.get("deleted_count", 0) or 0),
                    float(res.get("duration_sec", 0.0) or 0.0),
                )
                return True
            except Exception as e:
                _segment_sql_finish(
                    store=store,
                    root=root,
                    segment_id=sid,
                    owner=owner,
                    now_ts=time.time(),
                    result="error",
                    note=str(e),
                )
                logging.warning("[%s] %s rebuild failed id=%s: %s", root, str(ring_label or "segment_sql"), sid, e)
                return False
            finally:
                hb_stop.set()
                hb_thread.join(timeout=2.0)

        _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment_sql", sid)] = float(now_ts)
        # Keep shared cooldown in sync with regular `segment` task type.
        _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment", sid)] = float(now_ts)

        if async_worker:
            worker_key = (str(root), int(sid))

            def _worker() -> None:
                success = False
                try:
                    success = bool(_execute_rebuild())
                finally:
                    with _SEGMENT_SQL_WORKERS_LOCK:
                        _SEGMENT_SQL_WORKERS.pop(worker_key, None)
                    if on_terminal is not None:
                        try:
                            on_terminal(sid, success, None if success else "segment_sql_error")
                        except Exception:
                            pass

            thread = threading.Thread(
                target=_worker,
                name=f"{str(ring_label or 'segment_sql')}-worker-{sid}",
                daemon=True,
            )
            with _SEGMENT_SQL_WORKERS_LOCK:
                existing = _SEGMENT_SQL_WORKERS.get(worker_key)
                existing_thread = existing.get("thread") if isinstance(existing, dict) else None
                if isinstance(existing_thread, threading.Thread) and existing_thread.is_alive():
                    hb_stop.set()
                    hb_thread.join(timeout=2.0)
                    ring.rotate(-1)
                    continue
                _SEGMENT_SQL_WORKERS[worker_key] = {
                    "thread": thread,
                    "started_at": float(now_ts),
                    "ring": str(ring_label or "segment_sql"),
                }
            thread.start()
            ring.rotate(-1)
            launched += 1
            if on_launch is not None:
                try:
                    on_launch(sid)
                except Exception:
                    pass
            logging.info("[%s] %s worker started id=%s", root, str(ring_label or "segment_sql"), sid)
            continue

        success = bool(_execute_rebuild())
        if on_terminal is not None:
            try:
                on_terminal(sid, success, None if success else "segment_sql_error")
            except Exception:
                pass
        if success:
            ring.rotate(-1)
            launched += 1
            if on_launch is not None:
                try:
                    on_launch(sid)
                except Exception:
                    pass
        else:
            ring.rotate(-1)
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


def _cluster_offsite_not_before_reached(config: AgentConfig, dt_local: datetime) -> bool:
    return _local_time_reached(
        dt_local,
        int(getattr(config, "backup_cluster_offsite_not_before_hour", 2) or 0),
        int(getattr(config, "backup_cluster_offsite_not_before_minute", 0) or 0),
    )


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
    stdout_path: str = ""
    stderr_path: str = ""


class _SchedulerRuntimeRegistry(dict[str, RunningTask]):
    """Snapshot iteration for the shared registry's root-local workers.

    The main dispatcher may be inside a slow root-local SQL call while the
    persisted refill worker admits a different root. Returning snapshots for
    iteration prevents a cross-root launch from invalidating a concurrent
    lifecycle/count scan; the actual claim-to-spawn transition remains
    serialized by ``_SCHEDULER_RUNTIME_LOCK``.
    """

    def values(self):  # type: ignore[override]
        return list(super().values())

    def items(self):  # type: ignore[override]
        return list(super().items())


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
        # Whitelist files are optional; an absent file means no file-scoped IDs.
        logging.debug("optional ID file not found: %s", path)
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
    # Compatibility helper for older callers. The ordinary scheduler no longer
    # uses this computed split; its persisted cycle uses source snapshots.
    uniq = list(dict.fromkeys(ids))
    stale_set = set(stale_priority_ids or set())
    priority_set: set[int] = set(whitelist)
    priority_set.update(stale_set)
    ranked = sorted(uniq, key=lambda x: (-weights.get(x, 0.0), x))
    for sid in ranked[: max(0, priority_size)]:
        priority_set.add(sid)
    for sid in uniq:
        if weights.get(sid, 0.0) >= threshold:
            priority_set.add(sid)

    def _prio_key(sid: int) -> tuple[int, int, float, int]:
        return (
            0 if sid in whitelist else 1,
            0 if sid in stale_set else 1,
            -weights.get(sid, 0.0),
            sid,
        )

    priority = sorted([x for x in uniq if x in priority_set], key=_prio_key)
    regular = sorted(
        [x for x in uniq if x not in priority_set],
        key=lambda x: (-weights.get(x, 0.0), x),
    )
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


def _reconcile_campaign_rings(
    old_priority: deque[int] | None,
    old_regular: deque[int] | None,
    priority_ids: list[int],
    regular_ids: list[int],
) -> tuple[deque[int], deque[int]]:
    return (
        _reconcile_ring(old_priority, priority_ids, new_to_front=True),
        _reconcile_ring(old_regular, regular_ids),
    )


def _force_due_campaigns_to_priority(
    priority_ids: list[int],
    regular_ids: list[int],
    due_ids: list[int],
) -> tuple[list[int], list[int]]:
    planned = set(priority_ids) | set(regular_ids)
    due = [cid for cid in dict.fromkeys(due_ids) if cid in planned]
    if not due:
        return priority_ids, regular_ids
    due_set = set(due)
    return (
        due + [cid for cid in priority_ids if cid not in due_set],
        [cid for cid in regular_ids if cid not in due_set],
    )


def _move_ring_entities_to_front(ring: deque[int], entity_ids: list[int]) -> None:
    if not ring or not entity_ids:
        return
    ordered = list(ring)
    present = set(ordered)
    front = [eid for eid in dict.fromkeys(entity_ids) if eid in present]
    if not front:
        return
    front_set = set(front)
    ring.clear()
    ring.extend(front + [eid for eid in ordered if eid not in front_set])


def _merge_campaign_trigger_audit_ids(due_ids: list[int], audit_ids: list[int]) -> list[int]:
    """Keep audit-discovered published campaigns in the trigger plan.

    The narrow due SQL can miss campaigns that still need a regular trigger
    pass for newly-added contacts. The periodic all-published audit is the
    safety net for those campaigns, so its ids must survive more than one
    scheduler tick.
    """
    return list(dict.fromkeys(list(due_ids or []) + list(audit_ids or [])))


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


def _campaign_trigger_event_log_due_exists_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    return (
        "SELECT COUNT(*) AS cnt FROM ("
        "SELECT 1 "
        "FROM {prefix}campaigns c "
        f"WHERE c.id = {cid} "
        "  AND c.is_published = 1 "
        "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
        "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
        "  AND EXISTS ("
        "    SELECT 1 "
        "    FROM {prefix}campaign_lead_event_log el "
        "    WHERE el.campaign_id = c.id "
        "      AND el.is_scheduled = 1 "
        "      AND ("
        "        (el.trigger_date IS NOT NULL AND el.trigger_date <= '{now_event_log}') "
        "        OR el.trigger_date IS NULL"
        "      ) "
        "    LIMIT 1"
        "  ) "
        "LIMIT 1"
        ") q"
    )


def _campaign_trigger_root_action_due_exists_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    return (
        "SELECT COUNT(*) AS cnt FROM ("
        "SELECT 1 "
        "FROM {prefix}campaigns c "
        f"WHERE c.id = {cid} "
        "  AND c.is_published = 1 "
        "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
        "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
        "  AND EXISTS ("
        "    SELECT 1 "
        "    FROM {prefix}campaign_events ce "
        "    INNER JOIN {prefix}campaign_leads cld "
        "      ON cld.campaign_id = c.id "
        "     AND cld.manually_removed = 0 "
        "     AND cld.date_last_exited IS NULL "
        "    WHERE ce.campaign_id = c.id "
        "      AND ce.event_type IN ('action', 'condition') "
        "      AND ce.parent_id IS NULL "
        "      AND ("
        "        ce.trigger_mode IN ('immediate', 'interval') "
        "        OR ce.trigger_mode IS NULL "
        "        OR ("
        "          ce.trigger_mode = 'date' "
        "          AND (ce.trigger_date IS NULL OR ce.trigger_date <= '{now_utc}')"
        "        )"
        "      ) "
        "      AND NOT EXISTS ("
        "        SELECT 1 "
        "        FROM {prefix}campaign_lead_event_log el0 "
        "        WHERE el0.campaign_id = cld.campaign_id "
        "          AND el0.lead_id = cld.lead_id "
        "          AND el0.rotation <=> cld.rotation "
        "        LIMIT 1"
        "      ) "
        "    LIMIT 1"
        "  ) "
        "LIMIT 1"
        ") q"
    )


def _campaign_trigger_no_action_due_exists_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    interval_due_expr = (
        "CASE LOWER(COALESCE(ce.trigger_interval_unit, 'd')) "
        "WHEN 'i' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) MINUTE) "
        "WHEN 'h' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) HOUR) "
        "WHEN 'd' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) DAY) "
        "WHEN 'm' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) MONTH) "
        "WHEN 'y' THEN DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) YEAR) "
        "ELSE DATE_ADD(parent_log.date_triggered, INTERVAL COALESCE(ce.trigger_interval, 0) DAY) "
        "END"
    )
    return (
        "SELECT COUNT(*) AS cnt FROM ("
        "SELECT 1 "
        "FROM {prefix}campaigns c "
        f"WHERE c.id = {cid} "
        "  AND c.is_published = 1 "
        "  AND (c.publish_up IS NULL OR c.publish_up <= '{now_utc}') "
        "  AND (c.publish_down IS NULL OR c.publish_down >= '{now_utc}') "
        "  AND EXISTS ("
        "    SELECT 1 "
        "    FROM {prefix}campaign_events d "
        "    INNER JOIN {prefix}campaign_events ce "
        "      ON ce.parent_id = d.id "
        "     AND ce.decision_path = 'no' "
        "     AND ce.event_type IN ('action', 'condition') "
        "    INNER JOIN {prefix}campaign_lead_event_log parent_log "
        "      ON parent_log.campaign_id = c.id "
        "     AND parent_log.event_id = d.parent_id "
        "     AND parent_log.date_triggered IS NOT NULL "
        "    INNER JOIN {prefix}campaign_leads cld "
        "      ON cld.campaign_id = parent_log.campaign_id "
        "     AND cld.lead_id = parent_log.lead_id "
        "     AND cld.rotation <=> parent_log.rotation "
        "     AND cld.manually_removed = 0 "
        "     AND cld.date_last_exited IS NULL "
        "    WHERE d.campaign_id = c.id "
        "      AND d.event_type = 'decision' "
        "      AND d.parent_id IS NOT NULL "
        "      AND ("
        "        ce.trigger_mode = 'immediate' "
        "        OR ce.trigger_mode IS NULL "
        "        OR ("
        "          ce.trigger_mode = 'date' "
        "          AND (ce.trigger_date IS NULL OR ce.trigger_date <= '{now_utc}')"
        "        ) "
        "        OR ("
        "          ce.trigger_mode = 'interval' "
        "          AND parent_log.date_triggered IS NOT NULL "
        f"          AND ({interval_due_expr}) <= '{{now_utc}}'"
        "        )"
        "      ) "
        "      AND NOT EXISTS ("
        "        SELECT 1 "
        "        FROM {prefix}campaign_lead_event_log dlog "
        "        WHERE dlog.campaign_id = parent_log.campaign_id "
        "          AND dlog.event_id = d.id "
        "          AND dlog.lead_id = parent_log.lead_id "
        "          AND dlog.rotation <=> parent_log.rotation "
        "        LIMIT 1"
        "      ) "
        "    LIMIT 1"
        "  ) "
        "LIMIT 1"
        ") q"
    )


def _campaign_trigger_event_log_progress_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    return (
        "SELECT "
        "  SUM(CASE WHEN el.is_scheduled = 1 THEN 1 ELSE 0 END) AS pending_event_logs, "
        "  SUM(CASE WHEN el.date_triggered IS NOT NULL THEN 1 ELSE 0 END) AS triggered_event_logs, "
        "  COALESCE(DATE_FORMAT(MAX(el.date_triggered), '%Y-%m-%d %H:%i:%s'), '') AS max_triggered_at, "
        "  COALESCE(DATE_FORMAT(MIN(CASE "
        "    WHEN el.is_scheduled = 1 THEN el.trigger_date ELSE NULL "
        "  END), '%Y-%m-%d %H:%i:%s'), '') AS next_scheduled_at "
        "FROM {prefix}campaign_lead_event_log el "
        f"WHERE el.campaign_id = {cid}"
    )


def _campaign_trigger_email_progress_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    return (
        "SELECT "
        "  COUNT(es.id) AS sent_email_stats, "
        "  COALESCE(DATE_FORMAT(MAX(es.date_sent), '%Y-%m-%d %H:%i:%s'), '') AS max_email_sent_at "
        "FROM {prefix}email_stats es "
        "INNER JOIN {prefix}campaign_events ce ON ce.id = es.source_id "
        "WHERE es.source = 'campaign.event' "
        f"  AND ce.campaign_id = {cid}"
    )


def _campaign_trigger_latest_failed_reason_sql(campaign_id: int) -> str:
    cid = int(campaign_id)
    return (
        "SELECT "
        "  COALESCE(DATE_FORMAT(fl.date_added, '%Y-%m-%d %H:%i:%s'), '') AS date_added, "
        "  COALESCE(LEFT(fl.reason, 240), '') AS reason "
        "FROM {prefix}campaign_lead_event_failed_log fl "
        "INNER JOIN {prefix}campaign_lead_event_log el ON el.id = fl.log_id "
        f"WHERE el.campaign_id = {cid} "
        "ORDER BY fl.date_added DESC "
        "LIMIT 1"
    )


def _campaign_trigger_latest_failed_reason(db: MauticDB, campaign_id: int, sql_ctx: dict[str, str]) -> str:
    try:
        rows = db.fetch_rows(_campaign_trigger_latest_failed_reason_sql(int(campaign_id)), limit=1, context=sql_ctx)
    except Exception:
        return ""
    if not rows:
        return ""
    row = rows[0]
    reason = str(row.get("reason") or "").strip()
    date_added = str(row.get("date_added") or "").strip()
    if not reason:
        return ""
    return f"{date_added} {reason}".strip()


def _row_int(row: dict[str, object], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except Exception:
        return 0


def _parse_mysql_datetime_ts(raw: str) -> float | None:
    value = str(raw or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return None


def _campaign_trigger_recent_enough(raw_max_triggered_at: str, now_ts: float) -> bool:
    ts = _parse_mysql_datetime_ts(raw_max_triggered_at)
    if ts is None:
        return False
    age = float(now_ts) - float(ts)
    return age <= float(_CAMPAIGN_EMAIL_COUNTER_RECONCILE_RECENT_SEC)


def _campaign_trigger_progress_snapshot(
    db: MauticDB,
    campaign_id: int,
    sql_ctx: dict[str, str],
) -> CampaignTriggerProgressSnapshot:
    cid = int(campaign_id)
    due_event_logs = db.fetch_count(_campaign_trigger_event_log_due_exists_sql(cid), context=sql_ctx)
    due_root_actions = db.fetch_count(_campaign_trigger_root_action_due_exists_sql(cid), context=sql_ctx)
    due_no_action_branches = db.fetch_count(_campaign_trigger_no_action_due_exists_sql(cid), context=sql_ctx)
    rows = db.fetch_rows(_campaign_trigger_event_log_progress_sql(cid), limit=1, context=sql_ctx)
    row = rows[0] if rows else {}
    email_rows = db.fetch_rows(_campaign_trigger_email_progress_sql(cid), limit=1, context=sql_ctx)
    email_row = email_rows[0] if email_rows else {}
    return CampaignTriggerProgressSnapshot(
        due_event_logs=max(0, int(due_event_logs or 0)),
        due_root_actions=max(0, int(due_root_actions or 0)),
        due_no_action_branches=max(0, int(due_no_action_branches or 0)),
        pending_event_logs=_row_int(row, "pending_event_logs"),
        triggered_event_logs=_row_int(row, "triggered_event_logs"),
        max_triggered_at=str(row.get("max_triggered_at") or ""),
        next_scheduled_at=str(row.get("next_scheduled_at") or ""),
        sent_email_stats=_row_int(email_row, "sent_email_stats"),
        max_email_sent_at=str(email_row.get("max_email_sent_at") or ""),
    )


def _campaign_email_counter_reconcile(
    *,
    db: MauticDB,
    root: str,
    campaign_id: int | None,
    reason: str,
    force: bool = False,
) -> dict[str, object] | None:
    if campaign_id is None:
        return None
    key = (str(root), int(campaign_id))
    now_ts = time.time()
    # Native Mautic updates the denormalized emails.sent_count at the end of a
    # trigger transaction, after email_stats rows may already be visible. A
    # concurrent repair can therefore be added to Mautic's pending increment.
    # Fail closed for every campaign process on the same root, including
    # untracked native/fallback children, and retry after the process exits.
    if _root_has_live_campaign_process(str(root), {}):
        last_log_ts = float(_CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT.get(key, 0.0) or 0.0)
        if last_log_ts <= 0 or now_ts - last_log_ts >= float(_CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_SEC):
            _CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT[key] = now_ts
            logging.info(
                "[%s] email counter reconcile deferred campaign_id=%s reason=%s guard=campaign_process_active",
                root,
                int(campaign_id),
                reason,
            )
        return {
            "status": "deferred",
            "campaign_id": int(campaign_id),
            "reason": "campaign_process_active",
        }
    if not force:
        last_ts = float(_CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT.get(key, 0.0) or 0.0)
        if last_ts > 0 and now_ts - last_ts < float(_CAMPAIGN_EMAIL_COUNTER_RECONCILE_MIN_INTERVAL_SEC):
            return None
    _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT[key] = now_ts
    try:
        payload = repair_campaign_email_counters(db, int(campaign_id))
    except Exception as e:
        logging.warning("[%s] email counter reconcile failed campaign_id=%s reason=%s: %s", root, campaign_id, reason, e)
        return None
    mismatches = int(payload.get("mismatches", 0) or 0)
    repaired = int(payload.get("repaired", 0) or 0)
    skipped = int(payload.get("skipped", 0) or 0)
    if mismatches or repaired or skipped:
        logging.info(
            "[%s] email counter reconcile campaign_id=%s reason=%s checked=%s mismatches=%s repaired=%s skipped=%s",
            root,
            int(campaign_id),
            reason,
            int(payload.get("checked", 0) or 0),
            mismatches,
            repaired,
            skipped,
        )
    return payload


def _campaign_trigger_should_skip_launch(
    *,
    db: MauticDB,
    config: AgentConfig,
    root: str,
    campaign_id: int,
    sql_ctx: dict[str, str],
) -> bool:
    if not bool(getattr(config, "campaign_trigger_due_guard_enabled", True)):
        return False
    cooldown_key = (str(root), int(campaign_id))
    cooldown = _CAMPAIGN_TRIGGER_STUCK_UNTIL.get(cooldown_key)
    now_ts = time.time()
    future_wake = _CAMPAIGN_TRIGGER_FUTURE_WAKE.get(cooldown_key)
    if future_wake is not None and now_ts < float(future_wake[0]):
        return True
    if cooldown is not None:
        until_ts, reason = cooldown
        if now_ts < float(until_ts):
            logging.warning(
                "[%s] campaign_trigger skipped stuck cooldown id=%s until=%s reason=%s",
                root,
                int(campaign_id),
                datetime.fromtimestamp(float(until_ts), timezone.utc).isoformat(),
                reason or "-",
            )
            return True
        _CAMPAIGN_TRIGGER_STUCK_UNTIL.pop(cooldown_key, None)
    try:
        snapshot = _campaign_trigger_progress_snapshot(db, int(campaign_id), sql_ctx)
    except Exception as e:
        logging.warning("[%s] campaign_trigger due guard failed id=%s: %s", root, campaign_id, e)
        return False
    if snapshot.due_total > 0:
        _CAMPAIGN_TRIGGER_FUTURE_WAKE.pop(cooldown_key, None)
        _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED.pop(cooldown_key, None)
        return False
    next_scheduled_at = str(snapshot.next_scheduled_at or "").strip()
    next_scheduled_ts = _parse_mysql_datetime_ts(next_scheduled_at)
    now_event_log_ts = _parse_mysql_datetime_ts(str(sql_ctx.get("now_event_log") or ""))
    if snapshot.pending_event_logs > 0 and next_scheduled_ts is not None and now_event_log_ts is not None:
        delay_sec = float(next_scheduled_ts) - float(now_event_log_ts)
        if delay_sec > 0:
            wake_at = now_ts + max(0.25, delay_sec - _CAMPAIGN_TRIGGER_FUTURE_WAKE_EARLY_SEC)
            _CAMPAIGN_TRIGGER_FUTURE_WAKE[cooldown_key] = (wake_at, next_scheduled_at)
            if _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED.get(cooldown_key) != next_scheduled_at:
                _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED[cooldown_key] = next_scheduled_at
                logging.info(
                    "[%s] campaign_trigger scheduled wake id=%s at=%s pending_logs=%s",
                    root,
                    int(campaign_id),
                    next_scheduled_at,
                    snapshot.pending_event_logs,
                )
            return True
    _CAMPAIGN_TRIGGER_FUTURE_WAKE.pop(cooldown_key, None)
    _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED.pop(cooldown_key, None)
    logging.info(
        "[%s] campaign_trigger skipped stale id=%s pending_logs=%s triggered_logs=%s max_triggered=%s",
        root,
        int(campaign_id),
        snapshot.pending_event_logs,
        snapshot.triggered_event_logs,
        snapshot.max_triggered_at or "-",
    )
    if snapshot.triggered_event_logs > 0 and _campaign_trigger_recent_enough(snapshot.max_triggered_at, now_ts):
        _campaign_email_counter_reconcile(
            db=db,
            root=root,
            campaign_id=int(campaign_id),
            reason="stale-complete-skip",
        )
    return True


def _campaign_trigger_skip_removes_from_ring(
    root: str,
    campaign_id: int,
    whitelist: set[int],
) -> bool:
    cid = int(campaign_id)
    if cid in whitelist:
        return False
    return (str(root), cid) not in _CAMPAIGN_TRIGGER_FUTURE_WAKE


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


_SCHEDULER_MONITOR_PLAN_PREFIX = "scheduler_monitor_plan:"


def _scheduler_monitor_plan_key(root: str) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()
    return f"{_SCHEDULER_MONITOR_PLAN_PREFIX}{digest}"


def _segment_pending_guard_key(root: str) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()
    return f"segment_pending_guard:{digest}"


def _segment_dispatch_context_key(root: str) -> str:
    digest = hashlib.sha1(str(root or "").encode("utf-8")).hexdigest()
    return f"{_SEGMENT_DISPATCH_CONTEXT_PREFIX}{digest}"


def _unique_positive_ids(ids: list[int] | deque[int] | tuple[int, ...]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in ids:
        try:
            value = int(raw)
        except Exception:
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _monitor_cycle_mark_launched(
    cycle_done: dict[tuple[str, str], set[int]],
    *,
    root: str,
    task_type: str,
    entity_id: int | None,
) -> None:
    if entity_id is None:
        return
    try:
        eid = int(entity_id)
    except Exception:
        return
    if eid <= 0:
        return
    cycle_done.setdefault((str(root), str(task_type)), set()).add(eid)


def _monitor_running_entity_ids(
    running: dict[str, "RunningTask"],
    *,
    root: str,
    task_types: set[str],
) -> set[int]:
    out: set[int] = set()
    for task in running.values():
        if task.root != root or task.task_type not in task_types or task.entity_id is None:
            continue
        try:
            eid = int(task.entity_id)
        except Exception:
            continue
        if eid > 0:
            out.add(eid)
    return out


def _monitor_cycle_snapshot(
    *,
    root: str,
    task_type: str,
    planned_ids: list[int] | deque[int] | tuple[int, ...],
    queued_ids: list[int] | deque[int] | tuple[int, ...] | None = None,
    item_variants: dict[str, list[int] | set[int] | tuple[int, ...]] | None = None,
    cycle_done: dict[tuple[str, str], set[int]],
    running: dict[str, "RunningTask"],
    running_task_types: set[str],
) -> dict[str, object]:
    task_type = str(task_type)
    running_ids = _monitor_running_entity_ids(running, root=root, task_types=running_task_types)
    done_set = cycle_done.setdefault((str(root), task_type), set())
    base_planned = _unique_positive_ids(list(planned_ids))
    queue_source = base_planned if queued_ids is None else _unique_positive_ids(list(queued_ids))
    if not base_planned and not queue_source and not running_ids:
        done_set.clear()
        return {
            "task_type": task_type,
            "queued": [],
            "done": [],
            "running": [],
            "total": 0,
        }

    # ``queued_ids`` may contain durable ordinary-cycle work that is no longer
    # present in a volatile ring after restart or an empty plan refresh.  It is
    # still part of the monitor plan and must remain visible to Operations.
    planned = _unique_positive_ids(
        base_planned + list(queue_source) + list(done_set) + sorted(running_ids)
    )
    planned_set = set(planned)
    if not planned_set:
        done_set.clear()
    else:
        done_set.intersection_update(planned_set)

    if planned_set and planned_set.issubset(done_set) and not (running_ids & planned_set) and queue_source:
        done_set.clear()

    done_ordered = [sid for sid in planned if sid in done_set]
    queued = [sid for sid in queue_source if sid in planned_set and sid not in done_set and sid not in running_ids]
    payload: dict[str, object] = {
        "task_type": task_type,
        "queued": queued[:200],
        "done": done_ordered[:200],
        "running": sorted(running_ids & planned_set)[:200],
        "total": len(planned),
    }
    if item_variants:
        variant_payload: dict[str, list[int]] = {}
        for variant, ids in item_variants.items():
            key = str(variant or "").strip().lower()
            if not key:
                continue
            values = [eid for eid in _unique_positive_ids(list(ids)) if eid in planned_set]
            if values:
                variant_payload[key] = values[:200]
        if variant_payload:
            payload["item_variants"] = variant_payload
    return payload


def _import_monitor_cycle_snapshot(import_monitor: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(import_monitor, dict):
        return None
    queued = _unique_positive_ids(list(import_monitor.get("queued") or []))
    running = _unique_positive_ids(list(import_monitor.get("running") or []))
    done = _unique_positive_ids(list(import_monitor.get("done") or []))
    planned = _unique_positive_ids(queued + running + done)
    payload: dict[str, object] = {
        "task_type": "import",
        "queued": queued[:200],
        "done": done[:200],
        "running": running[:200],
        "total": len(planned),
    }
    raw_variants = import_monitor.get("item_variants") if isinstance(import_monitor.get("item_variants"), dict) else {}
    variants: dict[str, list[int]] = {}
    planned_set = set(planned)
    for variant, ids in raw_variants.items():
        key = str(variant or "").strip().lower()
        if not key:
            continue
        values = [eid for eid in _unique_positive_ids(list(ids or [])) if eid in planned_set]
        if values:
            variants[key] = values[:200]
    if variants:
        payload["item_variants"] = variants
    raw_statuses = import_monitor.get("item_statuses") if isinstance(import_monitor.get("item_statuses"), dict) else {}
    statuses: dict[str, str] = {}
    for raw_id, raw_status in raw_statuses.items():
        try:
            eid = int(raw_id)
        except Exception:
            continue
        status = str(raw_status or "").strip().lower()
        if eid > 0 and eid in planned_set and status:
            statuses[str(eid)] = status
    if statuses:
        payload["item_statuses"] = statuses
    return payload


def _monitor_visible_queued_ids(
    *,
    ring: list[int] | deque[int] | tuple[int, ...],
    root: str,
    task_type: str,
    running: dict[str, "RunningTask"],
    config: AgentConfig,
    now_ts: float,
    blocked_entities: set[int] | None = None,
    dynamic_blocked=None,
    running_task_types: set[str] | None = None,
    bypass_repeat_guard_entities: set[int] | None = None,
) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    running_types = running_task_types or {task_type}
    for raw in list(ring):
        try:
            eid = int(raw)
        except Exception:
            continue
        if eid <= 0 or eid in seen:
            continue
        seen.add(eid)
        if blocked_entities and eid in blocked_entities:
            continue
        if dynamic_blocked is not None:
            try:
                if dynamic_blocked(eid):
                    continue
            except Exception:
                continue
        if any(_is_running(running, root, running_task_type, eid) for running_task_type in running_types):
            continue
        if (
            not bypass_repeat_guard_entities
            or eid not in bypass_repeat_guard_entities
        ) and not _launch_allowed(config, root, task_type, eid, now_ts=now_ts):
            continue
        out.append(eid)
    return out


def _publish_scheduler_monitor_cycles(
    *,
    store: "TaskStore",
    root: str,
    cycle_done: dict[tuple[str, str], set[int]],
    running: dict[str, "RunningTask"],
    now_ts: float,
    seg_sql_ring: deque[int],
    seg_resume_ring: deque[int],
    seg_prio_ring: deque[int],
    seg_reg_ring: deque[int],
    campaign_trigger_prio_ring: deque[int],
    campaign_trigger_reg_ring: deque[int],
    campaign_rebuild_prio_ring: deque[int],
    campaign_rebuild_reg_ring: deque[int],
    segment_queued_ids: list[int] | None = None,
    campaign_trigger_queued_ids: list[int] | None = None,
    campaign_rebuild_queued_ids: list[int] | None = None,
    import_monitor: dict[str, object] | None = None,
) -> None:
    cycles = [
        _monitor_cycle_snapshot(
            root=root,
            task_type="segment",
            planned_ids=list(seg_sql_ring) + list(seg_resume_ring) + list(seg_prio_ring) + list(seg_reg_ring),
            queued_ids=segment_queued_ids,
            item_variants={"sql": list(seg_sql_ring)},
            cycle_done=cycle_done,
            running=running,
            running_task_types={"segment", "segment_sql"},
        ),
        _monitor_cycle_snapshot(
            root=root,
            task_type="campaign_rebuild",
            planned_ids=list(campaign_rebuild_prio_ring) + list(campaign_rebuild_reg_ring),
            queued_ids=campaign_rebuild_queued_ids,
            cycle_done=cycle_done,
            running=running,
            running_task_types={"campaign_rebuild", "campaign_update"},
        ),
        _monitor_cycle_snapshot(
            root=root,
            task_type="campaign_trigger",
            planned_ids=list(campaign_trigger_prio_ring) + list(campaign_trigger_reg_ring),
            queued_ids=campaign_trigger_queued_ids,
            cycle_done=cycle_done,
            running=running,
            running_task_types={"campaign_trigger"},
        ),
    ]
    import_cycle = _import_monitor_cycle_snapshot(import_monitor)
    if import_cycle is not None:
        cycles.append(import_cycle)
    payload = {
        "version": 1,
        "root": str(root),
        "updated_at": float(now_ts),
        "cycles": cycles,
    }
    store.put_runtime_sync(_scheduler_monitor_plan_key(root), payload)


def _merge_persisted_segment_monitor_cycle(
    *,
    store: "TaskStore",
    root: str,
    pending_ids: list[int] | tuple[int, ...] | deque[int],
    done_ids: list[int] | tuple[int, ...] | deque[int],
    running: dict[str, "RunningTask"],
    now_ts: float,
) -> None:
    """Refresh only the durable ordinary segment cycle in monitor state.

    The independent refill worker must not invent empty campaign rings or
    replace an import snapshot owned by the canonical planner.  Merge the one
    segment cycle into the last monitor envelope and preserve every unrelated
    cycle verbatim.
    """

    existing = store.get_runtime_sync(_scheduler_monitor_plan_key(root))
    if not isinstance(existing, Mapping):
        existing = {}
    payload = dict(existing)
    cycles_raw = existing.get("cycles")
    cycles = [dict(item) for item in cycles_raw if isinstance(item, Mapping)] if isinstance(cycles_raw, list) else []
    pending = _unique_positive_ids(list(pending_ids))
    done = _unique_positive_ids(list(done_ids))
    running_ids = sorted(
        _monitor_running_entity_ids(
            running,
            root=root,
            task_types={"segment", "segment_sql"},
        )
    )
    planned = _unique_positive_ids(pending + done + running_ids)
    segment_cycle = {
        "task_type": "segment",
        "queued": [sid for sid in pending if sid not in running_ids and sid not in done][:200],
        "done": done[:200],
        "running": running_ids[:200],
        "total": len(planned),
        "source": "persisted_refill",
    }
    replaced = False
    for index, cycle in enumerate(cycles):
        if str(cycle.get("task_type") or "").strip().lower() == "segment":
            cycles[index] = segment_cycle
            replaced = True
            break
    if not replaced:
        cycles.insert(0, segment_cycle)
    payload.update(
        {
            "version": int(existing.get("version") or 1),
            "root": str(root),
            "updated_at": float(now_ts),
            "cycles": cycles,
        }
    )
    store.put_runtime_sync(_scheduler_monitor_plan_key(root), payload)


def _publish_segment_monitor_cycle(
    *,
    store: "TaskStore",
    root: str,
    cycle_done: dict[tuple[str, str], set[int]],
    running: dict[str, "RunningTask"],
    now_ts: float,
    seg_sql_ring: deque[int],
    seg_resume_ring: deque[int],
    seg_prio_ring: deque[int],
    seg_reg_ring: deque[int],
) -> None:
    empty_ring: deque[int] = deque()
    _publish_scheduler_monitor_cycles(
        store=store,
        root=root,
        cycle_done=cycle_done,
        running=running,
        now_ts=now_ts,
        seg_sql_ring=seg_sql_ring,
        seg_resume_ring=seg_resume_ring,
        seg_prio_ring=seg_prio_ring,
        seg_reg_ring=seg_reg_ring,
        campaign_trigger_prio_ring=empty_ring,
        campaign_trigger_reg_ring=empty_ring,
        campaign_rebuild_prio_ring=empty_ring,
        campaign_rebuild_reg_ring=empty_ring,
    )


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
    dynamic_blocked=None,
    should_skip=None,
    remove_on_skip: bool = True,
    remove_on_launch: bool = False,
    on_launch=None,
    bypass_repeat_guard_entities: set[int] | None = None,
    queued_at_by_entity: dict[int, float] | None = None,
    admission_claim=None,
    admission_release=None,
    admission_running=None,
    bypass_baseline_admission: bool = False,
    max_launches: int = 1,
) -> int:
    if task_type == "segment" and admission_claim is None:
        context = _SEGMENT_ADMISSION_CONTEXT.get(str(root), {})
        admission_claim = context.get("claim")
        admission_release = context.get("release")
        admission_running = context.get("running")
    if not ring or ring_limit <= 0 or total_limit <= 0:
        return 0
    launched_count = 0
    scans = len(ring)
    if should_skip is not None:
        # Campaign trigger audit entries may be complete but remain visible in
        # the ring until their next audit refresh. Limit their DB preflight
        # work so a large shared host cannot delay fresh due campaigns on
        # later instance roots.
        scans = min(scans, _CAMPAIGN_TRIGGER_STALE_SCAN_CAP)
    while (
        ring
        and
        launched_count < max(1, int(max_launches or 1))
        and _running_count_for_entities(running, root, task_type, ring_entities) < ring_limit
        and scans > 0
    ):
        eid = ring[0]
        scans -= 1
        chain_ids = [int(eid)]
        if task_type == "segment":
            chain_ids = _segment_chain_from_dispatch_context(
                store,
                root,
                list(ring),
                int(eid),
            )
            # The topological owner is the only ring entry allowed to start
            # a multi-ID worker. A child remains visible in the durable FIFO,
            # but must not create a second process for the same closure.
            if chain_ids and int(chain_ids[0]) != int(eid):
                ring.rotate(-1)
                continue
        native_exclude_ids: list[int] = []
        native_command_entity_ids: list[int] | None = None
        native_exclude_ids_by_entity: dict[int, list[int]] = {}
        covered_ids: set[int] = set()
        if task_type == "segment":
            try:
                dispatch_context, dispatch_major, parents_by_child, children_by_parent = (
                    _segment_dispatch_context_parts(store, root)
                )
            except Exception:
                dispatch_context, dispatch_major, parents_by_child, children_by_parent = {}, 0, {}, {}
            if int(dispatch_major or 0) >= 7:
                covered_ids = _segment_native_covered_ids(store, root)
                try:
                    cycle_payload = store.get_runtime_sync(_segment_cycle_key(str(root)))
                except Exception:
                    cycle_payload = None
                cycle_items = (
                    set(_normalize_segment_ids(cycle_payload.get("items")))
                    | set(_normalize_segment_ids(cycle_payload.get("follow_up")))
                    if isinstance(cycle_payload, Mapping)
                    else set()
                )
                if int(eid) in covered_ids and int(eid) in cycle_items:
                    _segment_cycle_terminal(
                        store,
                        root=root,
                        entity_id=int(eid),
                        success=True,
                        note="native_covered_by_successful_root",
                        now_ts=time.time(),
                    )
                    _mark_segment_finished(root, int(eid), now_ts=time.time())
                    remove_covered = (
                        bool(remove_on_launch(eid))
                        if callable(remove_on_launch)
                        else bool(remove_on_launch)
                    )
                    _advance_ring_after_launch(ring, eid, remove_on_launch=remove_covered)
                    if remove_covered:
                        _remove_ring_entities(ring, {int(eid)})
                    logging.info(
                        "[%s] mautic7 segment root=%s skipped native_covered_by_prior_root",
                        root,
                        eid,
                    )
                    continue
                native_command_entity_ids, native_exclude_ids_by_entity = _segment_native_chain_command_plan(
                    chain_ids,
                    parents_by_child,
                    children_by_parent,
                    already_covered_ids=covered_ids,
                )
                native_exclude_ids = sorted(
                    {
                        int(value)
                        for values in native_exclude_ids_by_entity.values()
                        for value in values
                    }
                )
                if native_exclude_ids:
                    native_exclude_supported = dispatch_context.get("native_segment_exclude_supported")
                    if native_exclude_supported is None:
                        native_exclude_supported = _segment_native_exclude_capability(
                            store,
                            root=root,
                            config=config,
                            mautic_major=int(dispatch_major),
                        )
                        if hasattr(store, "put_runtime_sync"):
                            try:
                                refreshed_context = dict(dispatch_context)
                                refreshed_context["native_segment_exclude_supported"] = bool(
                                    native_exclude_supported
                                )
                                refreshed_context["native_segment_capability_checked_at"] = float(time.time())
                                store.put_runtime_sync(
                                    _segment_dispatch_context_key(root),
                                    refreshed_context,
                                )
                            except Exception:
                                pass
                else:
                    native_exclude_supported = True
                if native_exclude_ids and native_exclude_supported is False:
                    # Never silently repeat a successfully rebuilt parent when
                    # the runtime capability was explicitly disproven.  A
                    # fresh read-only help probe/descriptor refresh must set
                    # this field before the root can be retried.
                    ring.rotate(-1)
                    logging.warning(
                        "[%s] mautic7 root=%s waits for native --exclude capability ids=%s",
                        root,
                        eid,
                        ",".join(str(value) for value in native_exclude_ids),
                    )
                    continue
        if task_type == "segment" and not _scheduler_segment_entity_allowed(
            store,
            root,
            eid,
            config=config,
            running=running,
        ):
            ring.rotate(-1)
            continue
        if blocked_entities and any(int(sid) in blocked_entities for sid in chain_ids):
            ring.rotate(-1)
            continue
        if dynamic_blocked is not None and dynamic_blocked(eid):
            ring.rotate(-1)
            continue
        if should_skip is not None:
            try:
                skip_entity = bool(should_skip(eid))
            except Exception as e:
                logging.warning("[%s] %s entity=%s skip check failed: %s", root, task_type, eid, e)
                skip_entity = False
            if skip_entity:
                remove_skipped = bool(remove_on_skip(eid)) if callable(remove_on_skip) else bool(remove_on_skip)
                _advance_ring_after_launch(ring, eid, remove_on_launch=remove_skipped)
                continue
        if any(_is_running(running, root, task_type, int(sid)) for sid in chain_ids):
            ring.rotate(-1)
            continue
        bypass_repeat_guard = bool(
            bypass_repeat_guard_entities
            and any(int(sid) in bypass_repeat_guard_entities for sid in chain_ids)
        )
        if not bypass_repeat_guard and not _launch_allowed(config, root, task_type, eid):
            ring.rotate(-1)
            continue
        claimed_ids: list[int] = []
        claimed = False
        if admission_claim is not None:
            for chain_sid in chain_ids:
                try:
                    chain_claimed = bool(admission_claim(int(chain_sid)))
                except Exception as e:
                    logging.warning(
                        "[%s] %s entity=%s admission claim failed: %s",
                        root,
                        task_type,
                        chain_sid,
                        e,
                    )
                    chain_claimed = False
                if not chain_claimed:
                    break
                claimed_ids.append(int(chain_sid))
            claimed = len(claimed_ids) == len(chain_ids)
            if not claimed:
                if admission_release is not None:
                    for chain_sid in claimed_ids:
                        try:
                            admission_release(int(chain_sid))
                        except Exception:
                            pass
                ring.rotate(-1)
                continue
        args = (
            _segment_chain_command_args(
                build_args,
                chain_ids,
                exclude_ids=native_exclude_ids,
                command_entity_ids=native_command_entity_ids,
                exclude_ids_by_entity=native_exclude_ids_by_entity,
            )
            if task_type == "segment"
            else build_args(eid)
        )
        if task_type == "segment" and len(chain_ids) > 1:
            _segment_chain_runtime_write(
                store,
                root=root,
                owner_id=int(eid),
                entity_ids=chain_ids,
                status="admitting",
                now_ts=time.time(),
                command_str=shlex.join(args),
            )
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
            bypass_repeat_guard=bypass_repeat_guard,
            bypass_baseline_admission=bypass_baseline_admission,
        )
        if launched:
            if admission_running is not None and claimed:
                for chain_sid in chain_ids:
                    try:
                        admission_running(int(chain_sid))
                    except Exception:
                        pass
            if task_type == "segment" and len(chain_ids) > 1:
                chain_task = running.get(_task_key(root, task_type, int(eid)))
                _segment_chain_runtime_write(
                    store,
                    root=root,
                    owner_id=int(eid),
                    entity_ids=chain_ids,
                    status="running",
                    now_ts=time.time(),
                    task=chain_task,
                )
                if chain_task is not None:
                    for chain_sid in chain_ids:
                        marker_task = replace(
                            chain_task,
                            entity_id=int(chain_sid),
                            task_key=_task_key(root, task_type, int(chain_sid)),
                        )
                        _segment_runtime_marker_write(
                            store,
                            marker_task,
                            status="running",
                            now_ts=time.time(),
                        )
            if task_type == "segment":
                queued_at = (queued_at_by_entity or {}).pop(eid, None)
                if queued_at is not None:
                    logging.info(
                        "[%s] segment scheduler launch id=%s queue_wait_sec=%.3f source=import_followup",
                        root,
                        eid,
                        max(0.0, time.time() - float(queued_at)),
                    )
            if on_launch is not None:
                for chain_sid in chain_ids:
                    try:
                        on_launch(int(chain_sid))
                    except Exception:
                        pass
            remove_launched = bool(remove_on_launch(eid)) if callable(remove_on_launch) else bool(remove_on_launch)
            _advance_ring_after_launch(ring, eid, remove_on_launch=remove_launched)
            if remove_launched and len(chain_ids) > 1:
                _remove_ring_entities(ring, set(chain_ids))
            launched_count += 1
        else:
            # Count a failed attempt for this round as well. A root that is
            # temporarily blocked by repeat/DB/lock guards must not leave the
            # shared host cursor waiting forever while other roots are ready.
            _mark_scheduler_shared_admission(root, task_type)
            if admission_release is not None and claimed:
                for chain_sid in chain_ids:
                    try:
                        admission_release(int(chain_sid))
                    except Exception:
                        pass
            if task_type == "segment" and len(chain_ids) > 1:
                _segment_chain_runtime_write(
                    store,
                    root=root,
                    owner_id=int(eid),
                    entity_ids=chain_ids,
                    status="released",
                    now_ts=time.time(),
                )
            break
    return launched_count


def _campaign_spill_prefers_priority(priority_ring: deque[int], priority_limit: int) -> bool:
    """Keep forced campaign work from starving behind an unlimited regular ring."""
    return bool(priority_ring) and max(0, int(priority_limit or 0)) <= 0


def _remove_ring_entities(ring: deque[int], entity_ids: set[int]) -> int:
    if not ring or not entity_ids:
        return 0
    blocked = {int(entity_id) for entity_id in entity_ids if int(entity_id) > 0}
    if not blocked:
        return 0
    before = len(ring)
    keep = [entity_id for entity_id in ring if int(entity_id) not in blocked]
    if len(keep) == before:
        return 0
    ring.clear()
    ring.extend(keep)
    return before - len(keep)


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


def _task_store_serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize every TaskStore operation that can touch a shared DB handle.

    ``TaskStore`` intentionally uses one SQLite connection for the daemon's
    scheduler and state-push threads. ``check_same_thread=False`` permits that
    ownership model, but it does not make a connection re-entrant: an
    overlapping ``execute``/``commit`` can raise ``sqlite3.InterfaceError``
    and leave the scheduler with in-flight work marked lost. Keep the lock at
    the store boundary so SQLite and the optional MySQL handle follow the same
    transaction ordering, without coupling the fix to a host or task type.
    """

    @wraps(method)
    def _serialized(self: "TaskStore", *args: Any, **kwargs: Any) -> Any:
        with self._db_lock:
            return method(self, *args, **kwargs)

    return _serialized


class TaskStore:
    def __init__(self, path: str, cfg: AgentConfig | None = None) -> None:
        self.path = path
        self.cfg = cfg
        self._db_lock = threading.RLock()
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

    @_task_store_serialized
    def close(self) -> None:
        try:
            if self._mysql_conn is not None:
                self._mysql_conn.close()
        except Exception:
            pass
        self._mysql_conn = None
        try:
            self.conn.close()
        except Exception:
            pass

    def _is_local_host_alias(self, host_name: str | None) -> bool:
        host_lc = str(host_name or "").strip().lower()
        return bool(host_lc and host_lc in self._node_aliases_lc)

    def _mysql_alias_where(self) -> tuple[str, tuple[str, ...]]:
        aliases = tuple(sorted(self._node_aliases or {self._node_id}))
        placeholders = ",".join(["%s"] * len(aliases))
        return placeholders, aliases

    @_task_store_serialized
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

    @_task_store_serialized
    def _ensure_sqlite_column(self, table: str, column: str, ddl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        cols = {str(r["name"]) for r in cur.fetchall()}
        if column not in cols:
            self.conn.execute(ddl)

    @_task_store_serialized
    def _mysql_mark_error(self) -> None:
        self._mysql_retry_after_ts = time.time() + 15.0
        try:
            if self._mysql_conn is not None:
                self._mysql_conn.close()
        except Exception:
            pass
        self._mysql_conn = None

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
    def _sqlite_fetchall_dicts(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    @_task_store_serialized
    def _sqlite_prune_for_failover(self) -> None:
        """In mysql mode keep only minimum local fallback footprint."""
        if not self._mysql_mode:
            return
        try:
            self.conn.execute("DELETE FROM tasks WHERE state!='running'")
            self.conn.execute("DELETE FROM manual_requests WHERE status NOT IN ('pending','launched')")
            self.conn.execute("DELETE FROM weight_cache")
            # Keep only bounded current-state keys needed for failover. Logical
            # issues use one overwritten key per instance, never an append-only
            # journal in the legacy shadow.
            self.conn.execute(
                """
                DELETE FROM runtime_sync
                WHERE key NOT IN ('taskstore_mysql_migrated_v1', 'local_runtime', 'local_runtime_push_pending', 'local_runtime_accepted', 'local_runtime_metadata', 'mcc_runtime', 'mcc_runtime_metadata')
                  AND key NOT LIKE 'logical_issues:v1:%'
                """
            )
            self.conn.commit()
        except Exception:
            pass

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
    def recent_task_duration_stats(
        self,
        *,
        root: str,
        task_type: str,
        since_sec: int,
        min_duration_sec: int = 0,
    ) -> dict[int, dict[str, object]]:
        cutoff = time.time() - float(max(60, int(since_sec)))
        min_duration = max(0.0, float(min_duration_sec or 0))
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT entity_id, started_at, finished_at
                        FROM `{tasks_table}`
                        WHERE host_name=%s
                          AND root=%s
                          AND task_type=%s
                          AND entity_id IS NOT NULL
                          AND state='done'
                          AND COALESCE(rc, 0)=0
                          AND started_at IS NOT NULL
                          AND finished_at IS NOT NULL
                          AND finished_at >= %s
                        ORDER BY finished_at ASC
                        """,
                        (self._node_id, str(root), str(task_type), cutoff),
                    )
                    return self._aggregate_task_duration_rows(rows, min_duration=min_duration)
                except Exception:
                    pass

        rows = self._sqlite_fetchall_dicts(
            """
            SELECT entity_id, started_at, finished_at
            FROM tasks
            WHERE root=?
              AND task_type=?
              AND entity_id IS NOT NULL
              AND state='done'
              AND COALESCE(rc, 0)=0
              AND started_at IS NOT NULL
              AND finished_at IS NOT NULL
              AND finished_at >= ?
            ORDER BY finished_at ASC
            """,
            (str(root), str(task_type), cutoff),
        )
        return self._aggregate_task_duration_rows(rows, min_duration=min_duration)

    @staticmethod
    def _aggregate_task_duration_rows(
        rows: list[dict[str, object]],
        *,
        min_duration: float,
    ) -> dict[int, dict[str, object]]:
        out: dict[int, dict[str, object]] = {}
        for row in rows:
            try:
                eid = int(row.get("entity_id") or 0)
                started = float(row.get("started_at") or 0.0)
                finished = float(row.get("finished_at") or 0.0)
            except Exception:
                continue
            if eid <= 0 or started <= 0 or finished <= 0:
                continue
            duration = max(0.0, finished - started)
            if duration < min_duration:
                continue
            stats = out.setdefault(
                eid,
                {
                    "success_count": 0,
                    "max_duration_sec": 0.0,
                    "last_duration_sec": 0.0,
                    "last_finished_at": 0.0,
                },
            )
            stats["success_count"] = int(stats.get("success_count") or 0) + 1
            stats["max_duration_sec"] = max(float(stats.get("max_duration_sec") or 0.0), duration)
            if finished >= float(stats.get("last_finished_at") or 0.0):
                stats["last_duration_sec"] = duration
                stats["last_finished_at"] = finished
        return out

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
    def task_lifecycle(
        self,
        *,
        now_ts: float,
        min_age_sec: int,
        archive_dir: str,
        admission_dir: str,
        dry_run: bool,
        states: tuple[str, ...] = ("done", "failed", "timeout", "lost", "skipped", "cancelled"),
    ) -> dict[str, object]:
        """Preview/apply safe archival of old terminal task receipts.

        Running rows and rows whose execution lock is currently held are
        preserved.  The lock directory contains persistent inode names, so
        existence alone is not treated as activity; POSIX flock probing keeps
        stale lock files from blocking cleanup forever and fails closed when a
        lock cannot be inspected.
        """

        wanted_states = tuple(
            dict.fromkeys(str(state or "").strip().lower() for state in states if str(state or "").strip())
        )
        if not wanted_states:
            wanted_states = ("done", "failed", "timeout", "lost", "skipped", "cancelled")
        now = float(now_ts)
        cutoff = now - max(0.0, float(min_age_sec))
        running_rows = self.running_rows()
        running_ids = sorted(int(row.get("id") or 0) for row in running_rows if int(row.get("id") or 0) > 0)

        def _fetch_rows() -> list[dict[str, object]]:
            placeholders = ",".join(["%s"] * len(wanted_states))
            if self._mysql_mode:
                table = self._mysql_tables.get("tasks", "")
                if table and self._mysql_available():
                    try:
                        return self._mysql_query(
                            f"""
                            SELECT id, host_name, root, task_key, task_type, entity_id,
                                   command_str, pid, timeout_sec, attempts, state, note,
                                   started_at, finished_at, rc, manual_request_id
                            FROM `{table}`
                            WHERE host_name=%s
                              AND state IN ({placeholders})
                              AND COALESCE(finished_at, started_at) < %s
                            ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                            """,
                            (self._node_id, *wanted_states, cutoff),
                        )
                    except Exception:
                        pass
            sqlite_placeholders = ",".join(["?"] * len(wanted_states))
            return self._sqlite_fetchall_dicts(
                f"""
                SELECT id, root, task_key, task_type, entity_id, command_str,
                       pid, timeout_sec, attempts, state, note, started_at,
                       finished_at, rc, manual_request_id
                FROM tasks
                WHERE state IN ({sqlite_placeholders})
                  AND COALESCE(finished_at, started_at) < ?
                ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                """,
                (*wanted_states, cutoff),
            )

        def _lock_held(task_key: str) -> bool:
            digest = hashlib.sha256(str(task_key).encode("utf-8")).hexdigest()
            path = Path(admission_dir) / f"{digest}.lock"
            if not path.exists():
                return False
            if fcntl is None:
                return True
            try:
                with path.open("a+") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
            except OSError:
                return True

        rows = _fetch_rows()
        protected_rows: list[dict[str, object]] = []
        candidate_rows: list[dict[str, object]] = []
        for row in rows:
            if _lock_held(str(row.get("task_key") or "")):
                protected_rows.append(row)
            else:
                candidate_rows.append(row)

        candidate_ids = [int(row.get("id") or 0) for row in candidate_rows if int(row.get("id") or 0) > 0]
        protected_ids = [int(row.get("id") or 0) for row in protected_rows if int(row.get("id") or 0) > 0]
        dead_letter_states = {"failed", "timeout", "lost"}
        dead_letter_ids = [
            int(row.get("id") or 0)
            for row in candidate_rows
            if int(row.get("id") or 0) > 0 and str(row.get("state") or "").strip().lower() in dead_letter_states
        ]
        preserved_by_state: dict[str, list[int]] = {"running": list(running_ids)}
        for row in protected_rows:
            state = str(row.get("state") or "unknown").strip().lower() or "unknown"
            preserved_by_state.setdefault(state, []).append(int(row.get("id") or 0))

        result: dict[str, object] = {
            "phase": "dry_run" if dry_run else "apply",
            "cutoff_ts": cutoff,
            "candidate_ids": candidate_ids,
            "deleted_ids": [],
            "protected_ids": protected_ids,
            "dead_letter_ids": dead_letter_ids,
            "running_ids": running_ids,
            "preserved_by_state": preserved_by_state,
            "archive_path": "",
            "admission_dir": str(admission_dir),
        }
        if dry_run or not candidate_rows:
            return result

        archive_root = Path(archive_dir)
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"tasks-lifecycle-{time.time_ns()}.jsonl.gz"
        try:
            with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
                for row in candidate_rows:
                    handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

        deleted_ids: list[int] = []
        if self._mysql_mode:
            table = self._mysql_tables.get("tasks", "")
            if table and self._mysql_available():
                try:
                    placeholders = ",".join(["%s"] * len(candidate_ids))
                    self._mysql_exec(
                        f"DELETE FROM `{table}` WHERE host_name=%s AND id IN ({placeholders})",
                        (self._node_id, *candidate_ids),
                    )
                    deleted_ids = list(candidate_ids)
                except Exception:
                    deleted_ids = []
        if not deleted_ids:
            placeholders = ",".join(["?"] * len(candidate_ids))
            self.conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", tuple(candidate_ids))
            self.conn.commit()
            deleted_ids = list(candidate_ids)
        result["phase"] = "applied"
        result["deleted_ids"] = deleted_ids
        result["archive_path"] = str(archive_path)
        return result

    @_task_store_serialized
    def purge_failed_tasks(
        self,
        *,
        admission_dir: str,
        dry_run: bool,
        expected_candidate_ids: list[object] | None = None,
    ) -> dict[str, object]:
        """Compatibility operation for explicitly purging failure receipts."""

        archive_dir = str(getattr(self.cfg, "tasks_archive_dir", "/opt/mcd/var/task-history"))
        preview = self.task_lifecycle(
            now_ts=time.time(),
            min_age_sec=0,
            archive_dir=archive_dir,
            admission_dir=admission_dir,
            dry_run=True,
            states=("failed", "timeout", "lost"),
        )
        if dry_run:
            return preview
        actual = sorted(int(value) for value in preview.get("candidate_ids", []) if int(value) > 0)
        expected = sorted(
            int(value) for value in (expected_candidate_ids or []) if str(value).strip().isdigit() and int(value) > 0
        )
        if expected_candidate_ids is not None and expected != actual:
            preview["phase"] = "preflight_changed"
            preview["reason"] = "candidate_ids_changed"
            return preview
        return self.task_lifecycle(
            now_ts=time.time(),
            min_age_sec=0,
            archive_dir=archive_dir,
            admission_dir=admission_dir,
            dry_run=False,
            states=("failed", "timeout", "lost"),
        )

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
    def delete_runtime_sync(self, keys: list[str]) -> int:
        clean = list(dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip()))
        if not clean:
            return 0
        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    placeholders = ",".join(["%s"] * len(clean))
                    self._mysql_exec(
                        f"DELETE FROM `{table}` WHERE host_name=%s AND `key` IN ({placeholders})",
                        (self._node_id, *clean),
                    )
                except Exception:
                    pass
        placeholders = ",".join(["?"] * len(clean))
        cur = self.conn.execute(
            f"DELETE FROM runtime_sync WHERE key IN ({placeholders})",
            tuple(clean),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    @_task_store_serialized
    def list_runtime_sync(self, prefix: str = "") -> list[tuple[str, dict[str, object]]]:
        key_prefix = str(prefix or "")
        out: dict[str, dict[str, object]] = {}

        def _add_rows(rows: list[dict[str, object]] | list[sqlite3.Row]) -> None:
            for row in rows:
                try:
                    key = str(row["key"] or "")
                    raw = str(row["payload_json"] or "").strip()
                except Exception:
                    continue
                if not key or not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    out[key] = payload

        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    if key_prefix:
                        rows = self._mysql_query(
                            f"SELECT `key`, payload_json FROM `{table}` WHERE host_name=%s AND `key` LIKE %s",
                            (self._node_id, f"{key_prefix}%"),
                        )
                    else:
                        rows = self._mysql_query(
                            f"SELECT `key`, payload_json FROM `{table}` WHERE host_name=%s",
                            (self._node_id,),
                        )
                    _add_rows(rows)
                except Exception:
                    pass

        try:
            if key_prefix:
                rows = self.conn.execute(
                    "SELECT key, payload_json FROM runtime_sync WHERE key LIKE ?",
                    (f"{key_prefix}%",),
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT key, payload_json FROM runtime_sync").fetchall()
            _add_rows(rows)
        except Exception:
            pass

        return sorted(out.items())

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
    def get_manual_request_status(self, req_id: int) -> str | None:
        return self.get_manual_request_status_for_host(req_id, None)

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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

    @_task_store_serialized
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


@_scheduler_runtime_serialized
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
    # Scheduler children are started in their own session. Signal the whole
    # process group so killing a flock/sudo wrapper cannot leave the PHP
    # console process running forever as an orphan.
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = 0
    kill_group = pgid > 1 and pgid != os.getpgrp()

    def _alive() -> bool:
        if not kill_group:
            return _is_pid_alive(pid)
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal(sig: int) -> None:
        if kill_group:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)

    try:
        _signal(signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + max(0, grace_sec)
    while time.time() < deadline:
        if not _alive():
            return
        time.sleep(0.5)
    if _alive():
        try:
            _signal(signal.SIGKILL)
        except OSError:
            return


def _spawn_command(
    *,
    root: str,
    args: list[str],
    stdout_path: str = "",
    stderr_path: str = "",
) -> subprocess.Popen[bytes]:
    stdout_handle = open(stdout_path, "wb") if stdout_path else None
    stderr_handle = open(stderr_path, "wb") if stderr_path else None
    try:
        return subprocess.Popen(
            args,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle or subprocess.DEVNULL,
            stderr=stderr_handle or subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()


def _task_key(root: str, task_type: str, entity_id: int | None) -> str:
    eid = "-" if entity_id is None else str(entity_id)
    return f"{root}|{task_type}|{eid}"


def _campaign_dispatch_begin(root: str, task_type: str) -> bool:
    if task_type not in _CAMPAIGN_EXACT_TASK_TYPES:
        return True
    with _CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
        if root in _CAMPAIGN_FALLBACK_ACTIVE_ROOTS:
            return False
        _CAMPAIGN_DISPATCHING_ROOTS[root] = int(_CAMPAIGN_DISPATCHING_ROOTS.get(root, 0) or 0) + 1
        return True


def _campaign_dispatch_end(root: str, task_type: str) -> None:
    if task_type not in _CAMPAIGN_EXACT_TASK_TYPES:
        return
    with _CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
        remaining = int(_CAMPAIGN_DISPATCHING_ROOTS.get(root, 0) or 0) - 1
        if remaining > 0:
            _CAMPAIGN_DISPATCHING_ROOTS[root] = remaining
        else:
            _CAMPAIGN_DISPATCHING_ROOTS.pop(root, None)


def _campaign_priority_capacity(config: AgentConfig) -> int | None:
    """Return the shared per-instance cap for priority campaign work.

    The ordinary ring scheduler already applies ``campaign_total_parallel``.
    Priority/liveness work is admitted by a separate executor, so it must
    derive the same cap locally or trigger and rebuild lanes can bypass it.
    ``None`` preserves compatibility for configurations that predate the
    campaign lane settings.
    """
    explicit = max(0, int(getattr(config, "campaign_total_parallel", 0) or 0))
    if explicit > 0:
        return explicit
    configured = max(
        max(0, int(getattr(config, "campaign_priority_parallel", 0) or 0))
        + max(0, int(getattr(config, "campaign_regular_parallel", 0) or 0)),
        max(0, int(getattr(config, "campaign_trigger_priority_parallel", 0) or 0))
        + max(0, int(getattr(config, "campaign_trigger_regular_parallel", 0) or 0)),
        max(0, int(getattr(config, "campaign_rebuild_priority_parallel", 0) or 0))
        + max(0, int(getattr(config, "campaign_rebuild_regular_parallel", 0) or 0)),
        max(0, int(getattr(config, "campaign_update_priority_parallel", 0) or 0))
        + max(0, int(getattr(config, "campaign_update_regular_parallel", 0) or 0)),
    )
    return configured or None


def _campaign_fallback_try_begin(
    root: str,
    *,
    campaign_worker_active: bool,
    priority_campaign_active: bool,
) -> bool:
    with _CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
        if (
            root in _CAMPAIGN_FALLBACK_ACTIVE_ROOTS
            or int(_CAMPAIGN_DISPATCHING_ROOTS.get(root, 0) or 0) > 0
            or campaign_worker_active
            or priority_campaign_active
        ):
            return False
        _CAMPAIGN_FALLBACK_ACTIVE_ROOTS.add(root)
        return True


def _campaign_fallback_end(root: str) -> None:
    with _CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
        _CAMPAIGN_FALLBACK_ACTIVE_ROOTS.discard(root)


def _task_execution_lock_key(root: str, task_type: str, entity_id: int | None) -> str:
    if task_type in _CAMPAIGN_EXACT_TASK_TYPES:
        return _task_key(root, "campaign", entity_id)
    if task_type in _STATIC_SEGMENT_TASK_TYPES:
        return _task_key(root, "segment", entity_id)
    if task_type == "segment_recurring_priority":
        return _task_key(root, "segment", entity_id)
    return _task_key(root, task_type, entity_id)


def _segment_static_closure_lock_key(
    root: str,
    entity_id: int,
    parents_by_child: Mapping[int, set[int]] | None,
    children_by_parent: Mapping[int, set[int]] | None,
) -> str:
    """Return one execution lock for a connected native segment closure."""

    closure = _segment_static_closure_component(entity_id, parents_by_child, children_by_parent)
    normalized = sorted(int(value) for value in closure if int(value) > 0)
    digest = hashlib.sha256(",".join(str(value) for value in normalized).encode("utf-8")).hexdigest()[:24]
    return f"{root}|segment-closure|{digest}"


def _segment_static_closure_component(
    entity_id: int,
    parents_by_child: Mapping[int, set[int]] | None,
    children_by_parent: Mapping[int, set[int]] | None,
) -> set[int]:
    """Return the undirected connected component for one segment entity."""

    parents = parents_by_child or {}
    children = children_by_parent or {}
    component = {int(entity_id)}
    frontier = [int(entity_id)]
    while frontier:
        current = frontier.pop()
        neighbours = set(parents.get(current, set())) | set(children.get(current, set()))
        for neighbour in neighbours:
            try:
                value = int(neighbour)
            except Exception:
                continue
            if value > 0 and value not in component:
                component.add(value)
                frontier.append(value)
    return component


def _segment_static_closure_conflict(
    entity_id: int,
    active_ids: set[int],
    parents_by_child: Mapping[int, set[int]] | None,
    children_by_parent: Mapping[int, set[int]] | None,
) -> bool:
    """Check closure overlap, not only direct parent/entity equality."""

    try:
        candidate = segment_related_ids(int(entity_id), parents_by_child or {}, children_by_parent or {})
    except Exception:
        candidate = {int(entity_id)}
    for active_id in active_ids:
        try:
            active = segment_related_ids(int(active_id), parents_by_child or {}, children_by_parent or {})
        except Exception:
            active = {int(active_id)}
        if candidate & active:
            return True
    return False


def _task_locked_args(
    task_key: str,
    args: list[str],
    *,
    root: str = "",
    task_type: str = "",
    execution_lock_key: str | None = None,
) -> list[str]:
    flock_bin = Path("/usr/bin/flock")
    if not flock_bin.is_file():
        return list(args)
    lock_dir = Path("/run/lock/mcd-tasks")
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.warning("task execution lock unavailable: %s", exc)
        return list(args)
    effective_task_key = str(execution_lock_key or task_key)
    digest = hashlib.sha256(effective_task_key.encode("utf-8")).hexdigest()
    locked_args = [
        str(flock_bin),
        "--nonblock",
        "--conflict-exit-code",
        str(_TASK_LOCK_BUSY_RC),
        str(lock_dir / f"{digest}.lock"),
        *args,
    ]
    if not str(root or "").strip():
        return locked_args
    if task_type not in _CAMPAIGN_EXACT_TASK_TYPES and task_type != "campaign_native_fallback":
        root_lock = shared_root_fence_path(root)
        return [
            str(flock_bin), "--shared", "--nonblock", "--conflict-exit-code", str(_TASK_LOCK_BUSY_RC),
            str(root_lock), *locked_args,
        ]
    root_digest = hashlib.sha256(f"{root}|campaign-root".encode("utf-8")).hexdigest()
    campaign_locked_args = [
        str(flock_bin),
        "--shared" if task_type in _CAMPAIGN_EXACT_TASK_TYPES else "--exclusive",
        "--nonblock",
        "--conflict-exit-code",
        str(_TASK_LOCK_BUSY_RC),
        str(lock_dir / f"{root_digest}.lock"),
        *locked_args,
    ]
    return campaign_locked_args


class _PriorityTaskExecutor:
    def __init__(self) -> None:
        self._active: dict[str, tuple[str, str, str]] = {}
        self._last_started: dict[str, float] = {}
        self._last_finished: dict[str, float] = {}
        self._guard = threading.RLock()

    def is_active(self, root: str, task_type: str, entity_id: int) -> bool:
        key = _task_key(root, task_type, entity_id)
        with self._guard:
            return key in self._active

    def has_active_campaign_work(self, root: str) -> bool:
        with self._guard:
            return any(
                active_root == root and active_type.startswith("campaign_")
                for active_root, active_type, _capacity_lane in self._active.values()
            )

    def has_active_task_type(self, task_type: str) -> bool:
        with self._guard:
            return any(
                active_type == task_type
                for _active_root, active_type, _capacity_lane in self._active.values()
            )

    def active_count(self, root: str | None = None, task_types: set[str] | None = None) -> int:
        with self._guard:
            return sum(
                1
                for active_root, active_type, _capacity_lane in self._active.values()
                if (root is None or active_root == root)
                and (task_types is None or active_type in task_types)
            )

    def active_items(
        self,
        task_types: set[str] | None = None,
    ) -> list[tuple[str, str, str, str]]:
        with self._guard:
            return [
                (key, active_root, active_type, capacity_lane)
                for key, (active_root, active_type, capacity_lane) in self._active.items()
                if task_types is None or active_type in task_types
            ]

    def last_finished(self, root: str, task_type: str, entity_id: int) -> float:
        key = _task_key(root, task_type, entity_id)
        with self._guard:
            return float(self._last_finished.get(key, 0.0) or 0.0)

    def last_started(self, root: str, task_type: str, entity_id: int) -> float:
        key = _task_key(root, task_type, entity_id)
        with self._guard:
            return float(self._last_started.get(key, 0.0) or 0.0)

    def launch(
        self,
        config: AgentConfig,
        *,
        root: str,
        task_type: str,
        entity_id: int,
        args: list[str],
        interval_sec: int,
        max_parallel: int,
        on_success: Callable[[], None] | None = None,
        on_complete: Callable[[int | None], None] | None = None,
        on_start: Callable[[int], None] | None = None,
        timeout_sec: int | None = None,
        log_lifecycle: bool = True,
        capacity_lane: str | None = None,
        log_label: str = "priority",
        execution_lock_key: str | None = None,
        running: dict[str, RunningTask] | None = None,
    ) -> bool:
        # Fast-fail while the persisted update barrier is already active. The
        # final check in _run() remains mandatory because the barrier may be
        # published after this admission attempt starts.
        with scheduler_admission_lock(config):
            if scheduler_pause_path(config).exists():
                return False
        key = _task_key(root, task_type, entity_id)
        now = time.time()
        interval = max(1, int(interval_sec or 1))
        lane = str(capacity_lane or task_type).strip() or task_type
        lifecycle_label = str(log_label or "priority").strip() or "priority"
        if not _campaign_dispatch_begin(root, task_type):
            return False
        admitted = False
        try:
            # Ordinary submit and priority launch share this lock.  The
            # reservation is inserted before the worker thread is spawned, so
            # neither lane can pass a host/instance cap concurrently with the
            # other.  Configured lane budgets remain independent; these gates
            # apply only to the explicit host/instance ceilings.
            with _SCHEDULER_RUNTIME_LOCK, self._guard:
                previous = float(self._last_started.get(key, 0.0) or 0.0)
                active_for_lane = sum(
                    1
                    for active_root, _active_type, active_lane in self._active.values()
                    if active_root == root and active_lane == lane
                )
                campaign_capacity = (
                    _campaign_priority_capacity(config)
                    if task_type in _CAMPAIGN_EXACT_TASK_TYPES
                    else None
                )
                active_campaign_total = sum(
                    1
                    for active_root, active_type, _active_lane in self._active.values()
                    if active_root == root and active_type in _CAMPAIGN_EXACT_TASK_TYPES
                )
                host_slots = None
                instance_slots = None
                if running is not None:
                    host_slots = _scheduler_host_slots_available(
                        config,
                        running,
                        task_type,
                        root=root,
                        priority_executor=self,
                    )
                    instance_slots = _scheduler_instance_slots_available(
                        config,
                        running,
                        root=root,
                        task_type=task_type,
                        priority_executor=self,
                    )
                admitted = (
                    key not in self._active
                    and _launch_allowed(
                        config,
                        root,
                        task_type,
                        entity_id,
                        now_ts=now,
                        min_repeat_sec=interval,
                    )
                    and not (previous > 0 and now - previous < interval)
                    and active_for_lane < max(1, int(max_parallel or 1))
                    and _scheduler_shared_admission_allowed(root, task_type)
                    and (
                        campaign_capacity is None
                        or active_campaign_total < campaign_capacity
                    )
                    and (host_slots is None or host_slots > 0)
                    and (instance_slots is None or instance_slots > 0)
                )
                if admitted:
                    self._active[key] = (root, task_type, lane)
                    self._last_started[key] = now
                    _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, task_type, entity_id)] = now
                    _mark_scheduler_shared_admission(root, task_type)
        finally:
            _campaign_dispatch_end(root, task_type)
        if not admitted:
            return False

        def _run() -> None:
            lock_key = _task_execution_lock_key(root, task_type, entity_id)
            locked_args = _task_locked_args(
                lock_key,
                args,
                root=root,
                task_type=task_type,
                execution_lock_key=execution_lock_key,
            )
            if log_lifecycle:
                logging.info("[%s] %s spawned %s entity=%s", root, lifecycle_label, task_type, entity_id)
            completed = False
            rc: int | None = None
            proc: subprocess.Popen[bytes] | None = None
            try:
                timeout = max(
                    0,
                    int(
                        timeout_sec
                        if timeout_sec is not None
                        else (getattr(config, "command_timeout_sec", 0) or 0)
                    ),
                )
                with scheduler_admission_lock(config):
                    if scheduler_pause_path(config).exists():
                        return
                    proc = _spawn_command(root=root, args=locked_args)
                if on_start is not None:
                    on_start(int(proc.pid))
                rc = int(proc.wait(timeout=timeout if timeout > 0 else None) or 0)
                if rc == _TASK_LOCK_BUSY_RC:
                    if log_lifecycle:
                        logging.info("[%s] %s lock busy %s entity=%s", root, lifecycle_label, task_type, entity_id)
                elif rc == 0:
                    completed = True
                    if log_lifecycle:
                        logging.info("[%s] %s finished %s entity=%s rc=0", root, lifecycle_label, task_type, entity_id)
                else:
                    if log_lifecycle:
                        logging.warning(
                            "[%s] %s failed %s entity=%s rc=%s",
                            root,
                            lifecycle_label,
                            task_type,
                            entity_id,
                            rc,
                        )
            except subprocess.TimeoutExpired:
                if proc is not None:
                    _kill_pid(proc.pid, int(getattr(config, "segment_kill_grace_sec", 10) or 10))
                if log_lifecycle:
                    logging.warning("[%s] %s timeout %s entity=%s", root, lifecycle_label, task_type, entity_id)
            except Exception as exc:
                if log_lifecycle:
                    logging.warning(
                        "[%s] %s error %s entity=%s: %s",
                        root,
                        lifecycle_label,
                        task_type,
                        entity_id,
                        exc,
                    )
            finally:
                if completed and on_success is not None:
                    try:
                        on_success()
                    except Exception as exc:
                        logging.warning(
                            "[%s] priority completion hook failed %s entity=%s: %s",
                            root,
                            task_type,
                            entity_id,
                            exc,
                        )
                with self._guard:
                    self._active.pop(key, None)
                    if completed:
                        self._last_finished[key] = time.time()
                    elif rc == _TASK_LOCK_BUSY_RC:
                        self._last_started.pop(key, None)
                if on_complete is not None:
                    try:
                        on_complete(rc)
                    except Exception as exc:
                        logging.warning(
                            "[%s] priority completion report failed %s entity=%s: %s",
                            root,
                            task_type,
                            entity_id,
                            exc,
                        )

        threading.Thread(
            target=_run,
            name=f"mcd-{lifecycle_label}-{task_type}-{entity_id}",
            daemon=True,
        ).start()
        return True


def _campaign_native_fallback_metrics(db: MauticDB) -> dict[str, object]:
    """Collect best-effort, schema-aware evidence around one native fallback run."""
    out: dict[str, object] = {"pending_due": None, "email_stats": None, "error": ""}
    errors: list[str] = []

    try:
        event_columns = {
            str(row.get("Field") or "").strip().lower()
            for row in db.fetch_rows("SHOW COLUMNS FROM `{prefix}campaign_lead_event_log`")
            if isinstance(row, dict)
        }
        if {"date_triggered", "is_scheduled", "trigger_date"}.issubset(event_columns):
            pending_query = (
                "SELECT COUNT(*) FROM {prefix}campaign_lead_event_log "
                "WHERE date_triggered IS NULL AND is_scheduled = 1 "
                "AND (trigger_date IS NULL OR trigger_date <= UTC_TIMESTAMP())"
            )
        elif {"triggered", "date_scheduled"}.issubset(event_columns):
            pending_query = (
                "SELECT COUNT(*) FROM {prefix}campaign_lead_event_log "
                "WHERE triggered = 0 AND date_scheduled IS NOT NULL "
                "AND date_scheduled <= UTC_TIMESTAMP()"
            )
        else:
            raise RuntimeError("unsupported campaign_lead_event_log schema")
        out["pending_due"] = max(0, int(db.fetch_count(pending_query) or 0))
    except Exception as exc:
        errors.append(f"pending_due:{type(exc).__name__}:{exc}")

    try:
        email_columns = {
            str(row.get("Field") or "").strip().lower()
            for row in db.fetch_rows("SHOW COLUMNS FROM `{prefix}email_stats`")
            if isinstance(row, dict)
        }
        if "source" not in email_columns:
            raise RuntimeError("email_stats.source is unavailable")
        out["email_stats"] = max(
            0,
            int(db.fetch_count("SELECT COUNT(*) FROM {prefix}email_stats WHERE source = 'campaign.event'") or 0),
        )
    except Exception as exc:
        errors.append(f"email_stats:{type(exc).__name__}:{exc}")

    out["error"] = "; ".join(errors)[:500]
    return out


def _campaign_native_fallback_last_run_after_completion(
    *,
    previous_run_ts: float,
    completed_at: float,
    rc: int | None,
) -> float:
    if rc == _TASK_LOCK_BUSY_RC:
        return float(previous_run_ts or 0.0)
    return max(float(previous_run_ts or 0.0), float(completed_at or 0.0))


def _campaign_native_fallback_args(config: AgentConfig, root: str) -> list[str]:
    update = render_mautic_command(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=root,
        template="mautic:campaigns:update",
    )
    trigger = render_mautic_command(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=root,
        template="mautic:campaigns:trigger",
    )
    script = (
        f"{shlex.join(update)}; update_rc=$?; "
        f"{shlex.join(trigger)}; trigger_rc=$?; "
        'if [ "$update_rc" -ne 0 ]; then exit 100; fi; '
        'if [ "$trigger_rc" -ne 0 ]; then exit 101; fi; exit 0'
    )
    return ["/bin/sh", "-c", script]


def _priority_campaign_due_check_needed(
    *,
    now_ts: float,
    last_checked_ts: float,
    rebuild_started_ts: float,
    rebuild_finished_ts: float,
    checked_rebuild_ts: float,
    interval_sec: int,
) -> bool:
    if rebuild_started_ts <= 0:
        return False
    if rebuild_finished_ts > checked_rebuild_ts:
        return True
    return now_ts - last_checked_ts >= max(1, int(interval_sec or 1))


def _dispatch_due_campaign_triggers(
    *,
    config: AgentConfig,
    root: str,
    due_ids: list[int],
    running: dict[str, RunningTask],
    priority_executor: _PriorityTaskExecutor,
    on_success: Callable[[int], None],
    realtime: CampaignWhitelistRealtimeSetting | None = None,
) -> list[int]:
    launched: list[int] = []
    trigger_interval = max(
        1,
        int(config.campaign_trigger_min_repeat_sec or 0),
        int(config.campaign_trigger_audit_interval_sec or 0),
    )
    for campaign_id in dict.fromkeys(int(value) for value in due_ids if int(value) > 0):
        if (
            _is_running(running, root, "campaign_rebuild", campaign_id)
            or _is_running(running, root, "campaign_trigger", campaign_id)
            or priority_executor.is_active(root, "campaign_rebuild", campaign_id)
            or priority_executor.is_active(root, "campaign_trigger", campaign_id)
        ):
            continue
        realtime_trigger = bool(
            realtime is not None
            and realtime.trigger_enabled
            and campaign_id in realtime.ids
        )
        effective_interval = realtime.trigger_interval_sec if realtime_trigger else trigger_interval
        effective_parallel = (
            realtime.trigger_parallel
            if realtime_trigger
            else max(1, int(config.campaign_trigger_priority_parallel or 1))
        )
        started = priority_executor.launch(
            config,
            root=root,
            task_type="campaign_trigger",
            entity_id=campaign_id,
            running=running,
            args=render_mautic_command(
                php_bin=config.php_bin,
                run_as_user=config.mautic_run_as_user,
                root=root,
                template=config.cmd_campaign_trigger_template,
                id=campaign_id,
                campaign_limit=config.campaign_limit,
                batch_limit=config.campaign_batch_limit,
            ),
            interval_sec=effective_interval,
            max_parallel=effective_parallel,
            on_success=lambda campaign_id=campaign_id: on_success(campaign_id),
            capacity_lane="campaign_trigger_realtime" if realtime_trigger else None,
            log_label="realtime" if realtime_trigger else "priority",
        )
        if started:
            launched.append(campaign_id)
            logging.info("[%s] due campaign trigger dispatched entity=%s", root, campaign_id)
    return launched


def _dispatch_due_campaign_rebuilds(
    *,
    config: AgentConfig,
    root: str,
    due_ids: list[int],
    running: dict[str, RunningTask],
    priority_executor: _PriorityTaskExecutor,
    on_success: Callable[[int], None],
) -> list[int]:
    """Admit due rebuilds outside the segment/plugin scheduler lanes."""
    launched: list[int] = []
    rebuild_interval = max(1, int(config.campaign_rebuild_min_repeat_sec or 0))
    rebuild_parallel = max(1, int(config.campaign_rebuild_priority_parallel or 1))
    for campaign_id in dict.fromkeys(int(value) for value in due_ids if int(value) > 0):
        if (
            _is_running(running, root, "campaign_rebuild", campaign_id)
            or _is_running(running, root, "campaign_trigger", campaign_id)
            or priority_executor.is_active(root, "campaign_rebuild", campaign_id)
            or priority_executor.is_active(root, "campaign_trigger", campaign_id)
        ):
            continue
        started = priority_executor.launch(
            config,
            root=root,
            task_type="campaign_rebuild",
            entity_id=campaign_id,
            running=running,
            args=render_mautic_command(
                php_bin=config.php_bin,
                run_as_user=config.mautic_run_as_user,
                root=root,
                template=config.cmd_campaign_rebuild_template,
                id=campaign_id,
            ),
            interval_sec=rebuild_interval,
            max_parallel=rebuild_parallel,
            on_success=lambda campaign_id=campaign_id: on_success(campaign_id),
            capacity_lane="campaign_rebuild_liveness",
            log_label="liveness",
        )
        if started:
            launched.append(campaign_id)
            logging.info("[%s] due campaign rebuild dispatched by liveness lane entity=%s", root, campaign_id)
    return launched


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


def _launch_allowed(
    config: AgentConfig,
    root: str,
    task_type: str,
    entity_id: int | None,
    now_ts: float | None = None,
    min_repeat_sec: int | None = None,
) -> bool:
    min_repeat = (
        _task_repeat_interval_sec(config, task_type)
        if min_repeat_sec is None
        else max(0, int(min_repeat_sec or 0))
    )
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


def _root_has_live_import_process(running: dict[str, RunningTask], root: str) -> bool:
    if _running_count(running, root, "import") > 0:
        return True
    console = str(Path(root) / "bin" / "console")
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5)
    except Exception:
        return True
    if proc.returncode != 0:
        return True
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if "mautic:import" not in lower:
            continue
        if console in line or f" {root} " in line:
            return True
    return False


def _root_has_live_campaign_process(root: str, running: dict[str, RunningTask]) -> bool:
    if _running_campaign_total(running, root) > 0:
        return True
    console = str(Path(root) / "bin" / "console")
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5)
    except Exception:
        return True
    if proc.returncode != 0:
        return True
    commands = (
        "mautic:campaigns:update",
        "mautic:campaigns:rebuild",
        "mautic:campaigns:trigger",
    )
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if not any(command in lower for command in commands):
            continue
        if console in line or f" {root} " in line:
            return True
    return False


def _recover_orphaned_imports_if_safe(
    db: MauticDB,
    root: str,
    running: dict[str, RunningTask],
    *,
    grace_sec: int = 60,
) -> int:
    if _root_has_live_import_process(running, root):
        return 0
    try:
        recovered = db.recover_orphaned_imports(str(Path(root) / "var" / "import"), grace_sec=grace_sec)
    except Exception as e:
        logging.warning("[%s] import orphan recovery failed: %s", root, e)
        return 0
    if recovered > 0:
        logging.warning("[%s] recovered orphaned in-progress imports: %s", root, recovered)
    return max(0, int(recovered or 0))


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


def _import_pending_poll_due(
    *,
    config: AgentConfig,
    root: str,
    now_ts: float,
    last_poll_ts: dict[str, float],
    last_activity_ts: dict[str, float],
    running: dict[str, "RunningTask"],
    pending_cache: dict[str, int],
) -> bool:
    interval = max(1, int(getattr(config, "import_poll_interval_sec", 15) or 15))
    if now_ts - float(last_poll_ts.get(root, 0.0) or 0.0) >= float(interval):
        return True
    if _running_count(running, root, "import") > 0:
        return False
    if max(0, int(pending_cache.get(root, 0) or 0)) > 0:
        return True
    last_activity = float(last_activity_ts.get(root, 0.0) or 0.0)
    return last_activity > 0 and now_ts - last_activity <= _IMPORT_FAST_FOLLOW_SEC


def _has_import_date_value(raw: object) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    return not text.startswith("0000-00-00")


def _classify_import_monitor_row(row: dict[str, object]) -> tuple[str, str, str]:
    # date_ended is authoritative terminal evidence across Mautic 4-7. It
    # wins over stale numeric status values such as status=1.
    if _has_import_date_value(row.get("date_ended")):
        status_raw = str(row.get("status", "") or "").strip().lower()
        if status_raw in {"4", "failed", "error"}:
            return "done", "", "error"
        if status_raw in {"5", "stopped", "cancelled", "canceled", "paused"}:
            return "done", "stopped", "stopped"
        return "done", "", "success"
    status_raw = str(row.get("status", "") or "").strip().lower()
    status_raw = status_raw.replace(" ", "_").replace("-", "_")
    if status_raw in {"2", "6", "in_progress", "processing", "running", "manual"}:
        return "running", "", "processing"
    if status_raw in {"7", "delayed"}:
        return "queued", "delayed", "delayed"
    if status_raw in {"1", "queued", "pending"}:
        return "queued", "", "queued"
    if status_raw in {"4", "failed", "error"}:
        return "done", "", "error"
    if status_raw in {"5", "stopped", "cancelled", "canceled"}:
        return "queued", "stopped", "stopped"
    if status_raw in {"3", "imported", "completed", "complete", "done", "success"}:
        return "done", "", "success"
    return "queued", "", "queued"


def _import_target_segment_ids(properties: object) -> list[int]:
    """Read only the explicit Mautic import target-list setting.

    Imports can add contacts directly to a segment through ``defaults.list``.
    That write races a concurrent native segment update for the same segment.
    The payload is JSON on supported Mautic versions; invalid or unfamiliar
    payloads are deliberately ignored instead of being deserialized.
    """
    payload = properties
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        return []
    raw_lists = defaults.get("list")
    values = list(raw_lists) if isinstance(raw_lists, (list, tuple, set)) else [raw_lists]
    return _unique_positive_ids(values)


def _import_monitor_target_segment_ids(
    import_monitor: dict[str, object] | None,
    *,
    lanes: set[str] | None = None,
    successful_only: bool = False,
) -> dict[int, list[int]]:
    """Return explicit import target segments keyed by import id."""
    if not isinstance(import_monitor, dict):
        return {}
    wanted_lanes = lanes or {"queued", "running", "done"}
    statuses = import_monitor.get("item_statuses")
    statuses = statuses if isinstance(statuses, dict) else {}
    raw_targets = import_monitor.get("target_segments_by_import")
    raw_targets = raw_targets if isinstance(raw_targets, dict) else {}
    out: dict[int, list[int]] = {}
    for lane in wanted_lanes:
        raw_ids = import_monitor.get(lane)
        if not isinstance(raw_ids, (list, tuple, set, deque)):
            continue
        for import_id in _unique_positive_ids(list(raw_ids)):
            if successful_only and str(statuses.get(str(import_id), "")).lower() != "success":
                continue
            targets = _unique_positive_ids(list(raw_targets.get(str(import_id), []) or []))
            if targets:
                out[import_id] = targets
    return out


def _fetch_import_monitor_snapshot(
    db: MauticDB,
    root: str,
    sql_ctx: dict[str, str],
) -> dict[str, object]:
    rows = db.fetch_rows(_SQL_IMPORT_MONITOR_ROWS, limit=80, context=sql_ctx)
    queued: list[int] = []
    running: list[int] = []
    done: list[int] = []
    variants: dict[str, list[int]] = {}
    statuses: dict[str, str] = {}
    targets_by_import: dict[str, list[int]] = {}
    for row in rows:
        try:
            import_id = int(row.get("id", 0) or 0)
        except Exception:
            continue
        if import_id <= 0:
            continue
        lane, variant, status = _classify_import_monitor_row(row)
        if lane == "running":
            running.append(import_id)
        elif lane == "done":
            done.append(import_id)
        else:
            queued.append(import_id)
        if variant:
            variants.setdefault(variant, []).append(import_id)
        if status:
            statuses[str(import_id)] = status
        targets = _import_target_segment_ids(row.get("properties"))
        if targets:
            targets_by_import[str(import_id)] = targets
    return {
        "queued": _unique_positive_ids(queued),
        "running": _unique_positive_ids(running),
        "done": _unique_positive_ids(done),
        "item_variants": {k: _unique_positive_ids(v) for k, v in variants.items()},
        "item_statuses": statuses,
        "target_segments_by_import": targets_by_import,
    }


def _running_count(running: dict[str, RunningTask], root: str, task_type_prefix: str) -> int:
    return sum(1 for t in running.values() if t.root == root and t.task_type.startswith(task_type_prefix))


def _import_process_liveness_signal(pid: int) -> dict[str, object]:
    """Read independent CPU/IO counters for one live import process.

    A PID and the scheduler's own runtime marker are ownership evidence, not
    progress evidence.  These kernel counters are independent of MCD state
    and therefore may extend an import lease when they advance.
    """

    if int(pid or 0) <= 0:
        return {}
    signal: dict[str, object] = {}
    try:
        stat_raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        stat_tail = stat_raw.rsplit(")", 1)[-1].split()
        if len(stat_tail) > 12:
            signal["cpu_progress"] = (int(stat_tail[11]), int(stat_tail[12]))
    except (OSError, TypeError, ValueError, IndexError):
        pass
    try:
        io_values: dict[str, int] = {}
        for line in Path(f"/proc/{int(pid)}/io").read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, raw_value = line.partition(":")
            if not sep or name.strip() not in {"read_bytes", "write_bytes", "rchar", "wchar"}:
                continue
            try:
                io_values[name.strip()] = int(raw_value.strip())
            except ValueError:
                continue
        if io_values:
            signal["io_progress"] = tuple(sorted(io_values.items()))
    except OSError:
        pass
    return signal


def _import_path_liveness_signal(root: str, command_str: str) -> dict[str, object]:
    """Read upload/log file counters without treating MCD markers as progress."""

    candidates: list[Path] = [Path(root) / "var" / "import", Path(root) / "var" / "logs"]
    try:
        command_args = shlex.split(str(command_str or ""))
    except ValueError:
        command_args = []
    for raw_arg in command_args:
        value = str(raw_arg or "").strip()
        if not value or value.startswith("-"):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        if candidate.exists():
            candidates.append(candidate)

    fingerprints: list[tuple[str, int, int, int]] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            stat = candidate.stat()
        except OSError:
            continue
        fingerprints.append((resolved, int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_mode)))
        if not candidate.is_dir():
            continue
        try:
            entries = list(os.scandir(candidate))
        except OSError:
            continue
        for entry in entries[:512]:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            fingerprints.append(
                (
                    str(Path(candidate) / entry.name),
                    int(entry_stat.st_mtime_ns),
                    int(entry_stat.st_size),
                    int(entry_stat.st_mode),
                )
            )
    if not fingerprints:
        return {}
    fingerprints.sort()
    return {"file_progress": tuple(fingerprints)}


def _host_running_import_count(running: dict[str, RunningTask]) -> int:
    """Return the number of active import workers across the host for metrics."""

    return sum(1 for task in running.values() if str(task.task_type or "").strip().lower() == "import")


def _running_campaign_total(running: dict[str, RunningTask], root: str) -> int:
    return sum(
        1
        for t in running.values()
        if t.root == root and t.task_type in {"campaign_update", "campaign_trigger", "campaign_rebuild"}
    )


def _rotated_dispatch_installs(installs: list[object], cursor: int) -> list[object]:
    if not installs:
        return []
    start = max(0, int(cursor or 0)) % len(installs)
    return list(installs[start:]) + list(installs[:start])


def _priority_interleaved_dispatch_installs(
    installs: list[object],
    config: AgentConfig,
    *,
    regular_chunk: int = _PRIORITY_DISPATCH_REGULAR_CHUNK,
) -> list[object]:
    if not installs:
        return []
    priority = [
        inst
        for inst in installs
        if _segment_whitelist_effective_setting(config, inst)
        or _campaign_whitelist_effective_setting(config, inst)
    ]
    if not priority:
        return list(installs)
    priority_roots = {str(getattr(inst, "root", "") or "") for inst in priority}
    regular = [inst for inst in installs if str(getattr(inst, "root", "") or "") not in priority_roots]
    _ = regular_chunk
    return priority + regular


def _fairness_watchdog_dispatch_installs(
    installs: list[object],
    *,
    pending_roots: set[str],
    pending_since_by_root: dict[str, float],
    now_ts: float,
    watchdog_sec: int,
) -> tuple[list[object], list[str]]:
    unique: list[object] = []
    seen: set[str] = set()
    for inst in installs:
        root = str(getattr(inst, "root", "") or "").strip()
        if not root or root in seen:
            continue
        seen.add(root)
        unique.append(inst)

    pending = {str(root) for root in pending_roots if str(root) in seen}
    for root in list(pending_since_by_root):
        if root not in pending:
            pending_since_by_root.pop(root, None)
    for root in pending:
        pending_since_by_root.setdefault(root, float(now_ts))

    threshold = max(30, int(watchdog_sec or 300))
    promoted_roots = {
        root
        for root, started_at in pending_since_by_root.items()
        if root in pending and float(now_ts) - float(started_at) >= float(threshold)
    }
    promoted = [inst for inst in unique if str(getattr(inst, "root", "") or "").strip() in promoted_roots]
    regular = [inst for inst in unique if str(getattr(inst, "root", "") or "").strip() not in promoted_roots]
    return promoted + regular, [str(getattr(inst, "root", "") or "").strip() for inst in promoted]


def _fairness_promotion_log_due(
    promoted_roots: list[str],
    previous_promoted_roots: frozenset[str],
    *,
    now_ts: float,
    next_log_at: float,
) -> bool:
    if not promoted_roots:
        return False
    return not previous_promoted_roots or float(now_ts) >= float(next_log_at or 0.0)


def _pending_import_first_dispatch_installs(
    installs: list[object],
    pending_by_root: dict[str, int],
    pending_order: list[str] | tuple[str, ...] | None = None,
) -> list[object]:
    if not installs or not pending_by_root:
        return list(installs)
    pending: list[object] = []
    remaining: list[object] = []
    for inst in installs:
        root = str(getattr(inst, "root", "") or "")
        try:
            has_pending_import = int(pending_by_root.get(root, 0) or 0) > 0
        except (TypeError, ValueError):
            has_pending_import = False
        (pending if has_pending_import else remaining).append(inst)
    if pending_order:
        order = {str(root): index for index, root in enumerate(pending_order)}
        pending.sort(key=lambda inst: order.get(str(getattr(inst, "root", "") or ""), len(order)))
    return pending + remaining


def _campaign_pressure_active(
    config: AgentConfig,
    running: dict[str, RunningTask],
    root: str,
    *,
    trigger_prio_ring: deque[int],
    trigger_reg_ring: deque[int],
    rebuild_prio_ring: deque[int],
    rebuild_reg_ring: deque[int],
    trigger_dynamic_blocked=None,
    now_ts: float | None = None,
) -> bool:
    if not bool(getattr(config, "segment_throttle_during_campaigns", True)):
        return False
    running_campaigns = [
        task
        for task in running.values()
        if task.root == root and task.task_type in _SCHEDULER_CAMPAIGN_TASK_TYPES
    ]
    if not running_campaigns:
        return False
    min_running_count = max(0, int(getattr(config, "campaign_pressure_min_running_count", 2) or 0))
    if min_running_count > 0 and len(running_campaigns) >= min_running_count:
        return True
    min_running_sec = max(0, int(getattr(config, "campaign_pressure_min_running_sec", 120) or 0))
    if min_running_sec <= 0:
        return True
    now = time.time() if now_ts is None else float(now_ts)
    return any(now - float(task.started_at or 0) >= min_running_sec for task in running_campaigns)


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


def _effective_segment_slot_limit(
    config: AgentConfig,
    throttled_active: bool,
) -> int:
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


def _effective_segment_regular_slot_limit(
    config: AgentConfig,
    throttled_active: bool,
) -> int:
    """Return the ordinary worker budget without static priority capacity."""
    if config.segment_mode == "classic_loop":
        return 1
    if throttled_active:
        if config.segment_throttle_whitelist_only:
            return 0
        return max(0, int(config.segment_regular_parallel_throttled or 0))
    return max(0, int(config.segment_regular_parallel_idle or 0))


def _segment_shared_slots_used(
    running: dict[str, RunningTask],
    root: str,
    *,
    store: TaskStore | None = None,
    config: AgentConfig | None = None,
    now_ts: float | None = None,
    sql_db_running_count: int | None = None,
) -> int:
    sql_running_count = max(
        _segment_sql_worker_count(root),
        _segment_sql_running_state_count(store=store, root=root, config=config, now_ts=now_ts),
        max(0, int(sql_db_running_count or 0)),
    )
    # Static priority/super-priority executors are a separate admission lane.
    # They must not consume the ordinary cycle/import budget: otherwise one
    # active super-priority item can make a configured regular slot appear
    # unavailable and strand a durable ordinary cursor.
    return (
        _running_count(running, root, "segment")
        + sql_running_count
        + _running_count(running, root, "import")
    )


def _segment_shared_slots_available(
    running: dict[str, RunningTask],
    root: str,
    segment_slot_limit: int,
    *,
    store: TaskStore | None = None,
    config: AgentConfig | None = None,
    now_ts: float | None = None,
    sql_db_running_count: int | None = None,
) -> int:
    return max(
        0,
        max(0, int(segment_slot_limit or 0))
        - _segment_shared_slots_used(
            running,
            root,
            store=store,
            config=config,
            now_ts=now_ts,
            sql_db_running_count=sql_db_running_count,
        ),
    )


def _ordinary_segment_work_pending(store: TaskStore, root: str) -> bool:
    """Return whether an admitted ordinary cycle is waiting for a worker."""

    return bool(_segment_cycle_pending_ids(store.get_runtime_sync(_segment_cycle_key(root))))


def _segment_task_limit_after_import(
    running: dict[str, RunningTask],
    root: str,
    segment_slot_limit: int,
) -> int:
    return max(0, max(0, int(segment_slot_limit or 0)) - _running_count(running, root, "import"))


def _segment_pending_dispatch_limits(
    config: AgentConfig,
    throttled_active: bool,
    running: dict[str, RunningTask],
    root: str,
    *,
    segment_slot_limit: int,
    priority_pending: bool,
    store: TaskStore | None = None,
    now_ts: float | None = None,
    sql_db_running_count: int | None = None,
) -> tuple[int, int]:
    """Return the absolute pending cap and the currently free launch budget.

    ``total_limit`` passed to ``_fill_from_ring`` is an absolute per-root
    segment cap (after subtracting a singleton import worker).  The second
    value is only the number of new launches available in this scheduler tick.
    These values must not be collapsed: doing so underfills a persisted cycle
    whenever it already has one or more ordinary workers running.

    When no priority work is ready, ordinary pending work may borrow unused
    priority capacity.  The profile total remains subject to the explicit
    scheduler host/instance caps enforced by ``_submit_if_slot``.
    """

    regular_budget = max(
        1,
        _effective_segment_regular_slot_limit(config, throttled_active),
    )
    if not throttled_active and not priority_pending:
        regular_budget = max(regular_budget, max(0, int(segment_slot_limit or 0)))
    total_limit = _segment_task_limit_after_import(running, root, regular_budget)
    launch_budget = min(
        max(0, total_limit),
        _segment_shared_slots_available(
            running,
            root,
            regular_budget,
            store=store,
            config=config,
            now_ts=now_ts,
            sql_db_running_count=sql_db_running_count,
        ),
    )
    return total_limit, launch_budget


def _import_followup_slot_limit(
    running: dict[str, RunningTask],
    root: str,
    segment_slot_limit: int,
    *,
    ordinary_segment_pending: bool = False,
) -> int:
    """Leave one slot for unrelated segment work when followups are queued."""

    limit = _segment_task_limit_after_import(running, root, segment_slot_limit)
    if ordinary_segment_pending:
        return max(0, limit - 1)
    return limit


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
    sql_db_running_count: int | None = None,
) -> bool:
    if (
        not cluster_import_allowed
        or not config.enable_import_polling
        or int(import_pending_count or 0) <= 0
        or _import_in_settle(root, now_ts)
        or _running_count(running, root, "import") > 0
        or _root_has_live_import_process(running, root)
    ):
        return False
    if _segment_shared_slots_available(
        running,
        root,
        segment_slot_limit,
        store=store,
        config=config,
        now_ts=now_ts,
        sql_db_running_count=sql_db_running_count,
    ) <= 0:
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


def _dispatch_recurring_priority_segments(
    *,
    config: AgentConfig,
    inst: object,
    store: TaskStore,
    running: dict[str, RunningTask],
    priority_executor: _PriorityTaskExecutor,
    entries: list[object],
    enabled: bool,
) -> int:
    root = str(getattr(inst, "root", "") or "")
    local_instance_uid = str(getattr(inst, "instance_uid", "") or "")
    expected_keys = {recurring_priority_state_key(root, int(getattr(entry, "segment_id"))) for entry in entries}
    stale_keys = [key for key, _payload in store.list_runtime_sync(f"segment_recurring_priority:{root}:") if key not in expected_keys]
    if stale_keys:
        store.delete_runtime_sync(stale_keys)
    if not enabled:
        return 0

    # Recurring super-priority is an independent admission loop, but its
    # process still consumes the same farm segment capacity.  Preserve one
    # free shared slot for an already-admitted ordinary cycle; otherwise a
    # continuously due super ring can starve the durable ordinary tail.
    segment_capacity_configured = hasattr(config, "segment_priority_parallel_idle") and hasattr(
        config, "segment_regular_parallel_idle"
    )
    segment_slot_limit = _effective_segment_slot_limit(config, False) if segment_capacity_configured else 0
    if segment_capacity_configured and _ordinary_segment_work_pending(store, root):
        available = _segment_shared_slots_available(
            running,
            root,
            segment_slot_limit,
            store=store,
            config=config,
            now_ts=time.time(),
        )
        if available <= 1:
            return 0

    launched = 0
    for entry in entries:
        segment_id = int(getattr(entry, "segment_id"))
        interval_sec = int(getattr(entry, "max_interval_sec"))
        instance_uid = str(getattr(entry, "instance_uid", "") or local_instance_uid)
        key = recurring_priority_state_key(root, segment_id)
        current = store.get_runtime_sync(key) or {}
        now = time.time()
        if _is_running(running, root, "segment", segment_id) or _segment_sql_worker_running(root, segment_id):
            if str(current.get("last_status") or "") != "waiting_overlap":
                store.put_runtime_sync(
                    key,
                    recurring_priority_state_payload(
                        root=root,
                        instance_uid=instance_uid,
                        segment_id=segment_id,
                        max_interval_sec=interval_sec,
                        active=False,
                        pid=None,
                        last_started_at=current.get("last_started_at"),
                        last_finished_at=current.get("last_finished_at"),
                        last_status="waiting_overlap",
                        last_rc=current.get("last_rc"),
                        last_error="",
                        next_run_at=now,
                        updated_at=now,
                    ),
                )
            continue
        if priority_executor.is_active(root, "segment_recurring_priority", segment_id):
            continue

        def _on_start(
            pid: int,
            *,
            state_key: str = key,
            started_at: float = now,
            root_value: str = root,
            uid_value: str = instance_uid,
            sid: int = segment_id,
            interval: int = interval_sec,
            previous: dict[str, object] = dict(current),
        ) -> None:
            store.put_runtime_sync(
                state_key,
                recurring_priority_state_payload(
                    root=root_value,
                    instance_uid=uid_value,
                    segment_id=sid,
                    max_interval_sec=interval,
                    active=True,
                    pid=pid,
                    last_started_at=started_at,
                    last_finished_at=previous.get("last_finished_at"),
                    last_status="running",
                    last_rc=previous.get("last_rc"),
                    last_error="",
                    next_run_at=started_at + interval,
                    updated_at=time.time(),
                ),
            )

        def _on_complete(
            rc: int | None,
            *,
            state_key: str = key,
            started_at: float = now,
            root_value: str = root,
            uid_value: str = instance_uid,
            sid: int = segment_id,
            interval: int = interval_sec,
        ) -> None:
            finished_at = time.time()
            lock_busy = rc == _TASK_LOCK_BUSY_RC
            success = rc == 0
            status = "ok" if success else ("waiting_overlap" if lock_busy else "failed")
            next_run_at = finished_at if lock_busy else max(started_at + interval, finished_at)
            store.put_runtime_sync(
                state_key,
                recurring_priority_state_payload(
                    root=root_value,
                    instance_uid=uid_value,
                    segment_id=sid,
                    max_interval_sec=interval,
                    active=False,
                    pid=None,
                    last_started_at=started_at,
                    last_finished_at=finished_at,
                    last_status=status,
                    last_rc=rc,
                    last_error="" if success or lock_busy else (f"exit_{rc}" if rc is not None else "spawn_or_timeout"),
                    next_run_at=next_run_at,
                    updated_at=finished_at,
                ),
            )

        dispatch_window = max(1, int(getattr(config, "dispatch_interval_sec", 1) or 1))
        admission_interval = max(1, interval_sec - dispatch_window)
        started = priority_executor.launch(
            config,
            root=root,
            task_type="segment_recurring_priority",
            entity_id=segment_id,
            running=running,
            args=render_mautic_command(
                php_bin=config.php_bin,
                run_as_user=config.mautic_run_as_user,
                root=root,
                template=config.cmd_segment_update_template,
                id=segment_id,
                batch_limit=config.segment_batch_limit,
            ),
            interval_sec=admission_interval,
            max_parallel=1,
            on_start=_on_start,
            on_complete=_on_complete,
            timeout_sec=config.command_timeout_sec,
            capacity_lane="segment_recurring_priority",
            log_label="recurring-priority",
        )
        if started:
            launched += 1
        elif not current:
            store.put_runtime_sync(
                key,
                recurring_priority_state_payload(
                    root=root,
                    instance_uid=instance_uid,
                    segment_id=segment_id,
                    max_interval_sec=interval_sec,
                    active=False,
                    pid=None,
                    last_started_at=None,
                    last_finished_at=None,
                    last_status="scheduled",
                    last_rc=None,
                    last_error="",
                    next_run_at=now,
                    updated_at=now,
                ),
            )
    return launched


class _RecurringSegmentPriorityLoop:
    """Run recurring segment admission independently from tenant DB planning."""

    def __init__(self, priority_executor: _PriorityTaskExecutor, *, start: bool = True) -> None:
        self._priority_executor = priority_executor
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._snapshot: tuple[AgentConfig, list[tuple[object, list[object]]], TaskStore, dict[str, RunningTask], bool] | None = None
        if start:
            threading.Thread(
                target=self._run,
                name="mcd-segment-recurring-priority",
                daemon=True,
            ).start()

    def update(
        self,
        *,
        config: AgentConfig,
        installs: list[object],
        store: TaskStore,
        running: dict[str, RunningTask],
        enabled: bool,
    ) -> None:
        planned = [
            (
                inst,
                list(
                    recurring_priority_entries_for_instance(
                        getattr(config, "segment_recurring_priority_v1", {}),
                        inst,
                    )
                ),
            )
            for inst in installs
        ]
        with self._guard:
            self._snapshot = (config, planned, store, running, bool(enabled))
        self._wake.set()

    def run_once(self) -> int:
        with self._guard:
            snapshot = self._snapshot
        if snapshot is None:
            return 0
        config, planned, store, running, enabled = snapshot
        launched = 0
        for inst, entries in planned:
            try:
                launched += _dispatch_recurring_priority_segments(
                    config=config,
                    inst=inst,
                    store=store,
                    running=running,
                    priority_executor=self._priority_executor,
                    entries=entries,
                    enabled=enabled,
                )
            except Exception as exc:
                logging.warning(
                    "[%s] recurring priority admission failed: %s",
                    str(getattr(inst, "root", "") or ""),
                    exc,
                )
        return launched

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            self.run_once()


def _is_running(running: dict[str, RunningTask], root: str, task_type: str, entity_id: int | None) -> bool:
    if task_type in {"segment", "segment_sql"} and entity_id is not None:
        if _segment_sql_worker_running(root, int(entity_id)):
            return True
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
    bypass_repeat_guard: bool = False,
    bypass_baseline_admission: bool = False,
    stdout_path: str = "",
    stderr_path: str = "",
) -> bool:
    if not _campaign_dispatch_begin(root, task_type):
        return False
    try:
        return _submit_if_slot_uncoordinated(
            config=config,
            store=store,
            running=running,
            root=root,
            task_type=task_type,
            entity_id=entity_id,
            args=args,
            timeout_sec=timeout_sec,
            max_parallel_for_type=max_parallel_for_type,
            popens=popens,
            ignore_limit=ignore_limit,
            manual_request_id=manual_request_id,
            bypass_repeat_guard=bypass_repeat_guard,
            bypass_baseline_admission=bypass_baseline_admission,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    finally:
        _campaign_dispatch_end(root, task_type)


def _wait_for_segment_process_exit(
    proc: subprocess.Popen[bytes],
    wakeup: threading.Event,
) -> None:
    try:
        proc.wait()
    except Exception:
        pass
    finally:
        wakeup.set()


@_scheduler_runtime_serialized
def _submit_if_slot_uncoordinated(
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
    bypass_repeat_guard: bool = False,
    bypass_baseline_admission: bool = False,
    stdout_path: str = "",
    stderr_path: str = "",
) -> bool:
    key = _task_key(root, task_type, entity_id)
    manual_override = bool(manual_request_id is not None and ignore_limit)
    if key in running:
        return False
    if store.has_running_task_key(key):
        return False
    if manual_request_id is not None and any(
        task.root == root and task.manual_request_id is not None
        for task in running.values()
    ):
        return False
    # Imports are singleton per instance/root. Keep this guard at the common
    # submit boundary so manual and future dispatch paths cannot overlap an
    # import on the same instance while other instances use free host slots.
    if str(task_type or "").strip().lower() == "import" and _root_has_live_import_process(running, root):
        return False
    if manual_request_id is None and not bypass_repeat_guard and not _launch_allowed(config, root, task_type, entity_id):
        return False
    if manual_request_id is None and not bypass_repeat_guard:
        min_repeat = _task_repeat_interval_sec(config, task_type)
        if min_repeat > 0:
            prev = store.last_task_started_at(key)
            if prev > 0 and time.time() - float(prev) < float(min_repeat):
                return False
    # A manual/operator request is the one explicit exception to the normal
    # shared-capacity and round-robin budget.  It may occupy one additional
    # slot, but every ownership, duplicate, dependency, import-singleton and
    # update-admission lock above/below still applies.  The manual worker is
    # deliberately kept in ``running`` so normal admission sees the true
    # overcommit and waits until capacity is genuinely free again.
    if not manual_override and not _scheduler_shared_admission_allowed(root, task_type):
        return False
    baseline_lane = _scheduler_baseline_lane(task_type)
    baseline_admission = bool(
        str(task_type or "").strip().lower() != "import"
        and
        baseline_lane is not None
        and _scheduler_root_lane_running_count(running, root, baseline_lane) <= 0
    )
    if (
        baseline_admission
        and not bypass_baseline_admission
        and not manual_override
        and not _scheduler_baseline_launch_due(str(baseline_lane))
    ):
        return False
    host_slots = _scheduler_host_slots_available(config, running, task_type, root=root)
    if not manual_override and host_slots is not None and host_slots <= 0:
        return False
    if not manual_override:
        instance_slots = _scheduler_instance_slots_available(
            config,
            running,
            root=root,
            task_type=task_type,
        )
        if instance_slots is not None and instance_slots <= 0:
            return False
    if not ignore_limit and _running_count(running, root, task_type) >= max_parallel_for_type:
        return False

    args = _task_locked_args(
        _task_execution_lock_key(root, task_type, entity_id),
        args,
        root=root,
        task_type=task_type,
    )
    # The updater creates the persisted barrier under this same lock. Holding
    # it through the final pause check and spawn closes the check-to-spawn race
    # for every lane, including super/priority/manual/import admissions.
    with scheduler_admission_lock(config):
        if scheduler_pause_path(config).exists():
            return False
        proc = _spawn_command(root=root, args=args, stdout_path=stdout_path, stderr_path=stderr_path)
    if task_type == "segment" and _SEGMENT_DISPATCH_WAKEUP is not None:
        threading.Thread(
            target=_wait_for_segment_process_exit,
            args=(proc, _SEGMENT_DISPATCH_WAKEUP),
            name=f"mcd-segment-wakeup-{entity_id or 'full'}",
            daemon=True,
        ).start()
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
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    task.row_id = store.add_running(task)
    _segment_runtime_marker_write(store, task, status="running", now_ts=task.started_at)
    running[key] = task
    if not manual_override:
        _mark_scheduler_shared_admission(root, task_type)
    if baseline_admission and not bypass_baseline_admission and not manual_override:
        _mark_scheduler_baseline_launch(str(baseline_lane), task.started_at)
    if not manual_override:
        _mark_scheduler_fairness_claim(root, task_type)
    if popens is not None:
        popens[key] = proc
    _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, task_type, entity_id)] = task.started_at
    logging.info("[%s] spawned %s pid=%s entity=%s", root, task_type, task.pid, entity_id)
    return True


def _effective_task_retry_max(config: AgentConfig, task: RunningTask) -> int:
    """Return the effective maximum number of starts for one task.

    Segment retries are deliberately a separate host-profile policy.  Keep
    this calculation shared by the completion gate and the respawn path so a
    segment configured for retries is not filtered out by the generic
    ``task_retry_max`` setting before ``_respawn_task`` can apply its policy.
    """
    retry_max = int(config.task_retry_max)
    if task.manual_request_id is None and task.task_type == "campaign_trigger" and retry_max == 1:
        retry_max = _CAMPAIGN_TRIGGER_AUTOMATIC_RETRY_CAP
    if task.manual_request_id is None and task.task_type == "segment" and task.entity_id is not None:
        # ``segment_retry_count`` is a historical key name, but its current
        # contract is an inclusive total-start cap. Do not add an implicit
        # initial attempt: a profile value of 3 means starts 1..3 exactly.
        segment_start_limit = max(1, int(getattr(config, "segment_retry_count", 2) or 0))
        retry_max = segment_start_limit
    return retry_max


@_scheduler_runtime_serialized
def _respawn_task(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    task: RunningTask,
) -> bool:
    if str(task.task_type or "").strip().lower() == "import" and _root_has_live_import_process(running, task.root):
        logging.warning(
            "[%s] import retry deferred: instance import worker already active",
            task.root,
        )
        return False
    # task_retry_max semantics:
    # - <= 0 : unlimited retries
    # - 1    : no retry (initial run only), unless a targeted campaign trigger override applies
    # - > 1  : bounded retries up to configured attempt cap
    retry_max = _effective_task_retry_max(config, task)
    # Repeated-failure cooldown is an admission guard for a new segment
    # cycle, applied by ``_segment_failure_blocked_ids``.  Do not consult the
    # aggregate history here: this function runs after the current attempt was
    # finished, so counting history would also count the retries in this same
    # cycle and truncate the configured retry budget.
    if retry_max > 0 and task.attempts >= retry_max:
        return False
    if not _campaign_dispatch_begin(task.root, task.task_type):
        return False
    try:
        try:
            args = task.command_str.split(_CMD_SEP)
            with scheduler_admission_lock(config):
                if scheduler_pause_path(config).exists():
                    return False
                proc = _spawn_command(
                    root=task.root,
                    args=args,
                    stdout_path=task.stdout_path,
                    stderr_path=task.stderr_path,
                )
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
            stdout_path=task.stdout_path,
            stderr_path=task.stderr_path,
        )
        next_task.row_id = store.add_running(next_task)
        _segment_runtime_marker_write(store, next_task, status="running", now_ts=next_task.started_at)
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
    finally:
        _campaign_dispatch_end(task.root, task.task_type)


def _campaign_trigger_progress_watchdog(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    task: RunningTask,
    key: str,
    db_configs_by_root: dict[str, object],
    mautic_timezones_by_root: dict[str, str | None],
    state_by_key: dict[str, dict[str, object]],
    now_ts: float,
) -> bool:
    if task.manual_request_id is not None:
        return False
    if task.task_type != "campaign_trigger" or task.entity_id is None:
        state_by_key.pop(key, None)
        return False
    if not bool(getattr(config, "campaign_trigger_progress_watchdog_enabled", True)):
        state_by_key.pop(key, None)
        return False
    grace_sec = max(
        0,
        int(
            getattr(
                config,
                "campaign_trigger_progress_watchdog_grace_sec",
                _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_GRACE_SEC,
            )
            or _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_GRACE_SEC
        ),
    )
    if now_ts - float(task.started_at or 0.0) < float(grace_sec):
        return False
    db_cfg = db_configs_by_root.get(str(task.root))
    if db_cfg is None:
        return False
    wd = state_by_key.setdefault(key, {})
    interval_sec = max(
        10,
        int(
            getattr(
                config,
                "campaign_trigger_progress_watchdog_interval_sec",
                _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_INTERVAL_SEC,
            )
            or _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_INTERVAL_SEC
        ),
    )
    last_check = float(wd.get("last_check", 0.0) or 0.0)
    if last_check > 0 and now_ts - last_check < float(interval_sec):
        return False
    wd["last_check"] = float(now_ts)

    try:
        now_utc = datetime.now(timezone.utc)
        sql_ctx = campaign_sql_time_context(now_utc, mautic_timezones_by_root.get(str(task.root)))
        snapshot = _campaign_trigger_progress_snapshot(MauticDB(db_cfg), int(task.entity_id), sql_ctx)
    except Exception as e:
        wd["last_error"] = str(e)
        logging.warning("[%s] campaign_trigger progress watchdog failed id=%s: %s", task.root, task.entity_id, e)
        return False

    progress_key = snapshot.progress_key()
    previous_key = wd.get("progress_key")
    if previous_key != progress_key:
        wd["progress_key"] = progress_key
        wd["stable_checks"] = 0
        wd["last_change"] = float(now_ts)
        wd["due_total"] = snapshot.due_total
        return False

    stable_checks = int(wd.get("stable_checks", 0) or 0) + 1
    wd["stable_checks"] = stable_checks
    wd["due_total"] = snapshot.due_total
    required_checks = max(
        1,
        int(
            getattr(
                config,
                "campaign_trigger_progress_watchdog_stable_checks",
                _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_STABLE_CHECKS,
            )
            or _CAMPAIGN_TRIGGER_PROGRESS_WATCHDOG_STABLE_CHECKS
        ),
    )
    if stable_checks < required_checks:
        return False

    reason = ""
    try:
        reason = _campaign_trigger_latest_failed_reason(MauticDB(db_cfg), int(task.entity_id), sql_ctx)
    except Exception:
        reason = ""
    note = "killed_stuck_due_no_progress" if snapshot.due_total > 0 else "killed_no_due_no_progress"
    cooldown_reason = reason or note
    _CAMPAIGN_TRIGGER_STUCK_UNTIL[(str(task.root), int(task.entity_id))] = (
        now_ts + float(_CAMPAIGN_TRIGGER_STUCK_COOLDOWN_SEC),
        cooldown_reason,
    )
    _kill_pid(task.pid, int(getattr(config, "segment_kill_grace_sec", 10) or 10))
    store.finish(task.row_id, state="done", rc=0, note=note)
    _mark_campaign_trigger_finished(task.root, task.entity_id)
    if snapshot.due_total == 0:
        _campaign_email_counter_reconcile(
            db=MauticDB(db_cfg),
            root=task.root,
            campaign_id=task.entity_id,
            reason="watchdog-no-due-finish",
            force=True,
        )
    running.pop(key, None)
    popens.pop(key, None)
    state_by_key.pop(key, None)
    logging.warning(
        "[%s] campaign_trigger id=%s killed by progress watchdog: due=%s sent_email_stats=%s "
        "max_email_sent_at=%s no DB progress for %s checks reason=%s",
        task.root,
        task.entity_id,
        snapshot.due_total,
        snapshot.sent_email_stats,
        snapshot.max_email_sent_at or "-",
        stable_checks,
        reason or "-",
    )
    return True


def _monitor_running(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    db_configs_by_root: dict[str, object] | None = None,
    mautic_timezones_by_root: dict[str, str | None] | None = None,
    campaign_progress_watchdog: dict[str, dict[str, object]] | None = None,
    import_progress_watchdog: dict[str, dict[str, object]] | None = None,
    segment_terminal_callback: Callable[[RunningTask, bool, str | None], None] | None = None,
    segment_liveness_probe: Callable[[RunningTask], Mapping[str, object] | bool] | None = None,
    import_liveness_probe: Callable[[RunningTask], Mapping[str, object] | bool] | None = None,
    roots: set[str] | None = None,
) -> None:
    now = time.time()
    db_configs = db_configs_by_root or {}
    tz_by_root = mautic_timezones_by_root or {}
    progress_watchdog = campaign_progress_watchdog if campaign_progress_watchdog is not None else {}
    import_watchdog = import_progress_watchdog if import_progress_watchdog is not None else {}

    def _segment_liveness_evidence(task: RunningTask) -> dict[str, object]:
        if task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES:
            return {}
        evidence: Mapping[str, object] | bool = {}
        if segment_liveness_probe is not None:
            try:
                evidence = segment_liveness_probe(task)
            except Exception as exc:
                logging.debug(
                    "[%s] segment liveness probe failed entity=%s: %s",
                    task.root,
                    task.entity_id,
                    exc,
                )
                evidence = {}
        if isinstance(evidence, Mapping):
            result = {str(key): value for key, value in evidence.items()}
        else:
            result = {"active": bool(evidence)}

        # Marker-only state is a bounded restart bridge, never a heartbeat.
        independent = any(
            bool(result.get(key))
            for key in ("active", "process", "db", "db_process", "runtime_confirmed", "completed")
        )
        if independent:
            _segment_runtime_marker_write(store, task, status="running", now_ts=now)
            result["active"] = True
            return result
        if _segment_runtime_marker_orphan_active(store, task, now_ts=now):
            result["marker"] = True
            result["active"] = True
        return result

    def _import_liveness_evidence(task: RunningTask) -> dict[str, object]:
        if task.task_type != "import":
            return {}
        evidence: Mapping[str, object] | bool = {}
        if import_liveness_probe is not None:
            try:
                evidence = import_liveness_probe(task)
            except Exception as exc:
                logging.debug(
                    "[%s] import liveness probe failed pid=%s: %s",
                    task.root,
                    task.pid,
                    exc,
                )
                evidence = {}
        if isinstance(evidence, Mapping):
            return {str(key): value for key, value in evidence.items()}
        return {"active": bool(evidence)}

    def _import_watchdog_keepalive(task: RunningTask, key: str, evidence: Mapping[str, object]) -> bool:
        """Apply the import stall watchdog; return True while the lease is kept."""

        state = import_watchdog.setdefault(key, {})
        independent_fields = {
            field: evidence.get(field)
            for field in (
                "db_progress",
                "db_query_progress",
                "db_active",
                "active_db",
                "file_progress",
                "file_offset",
                "log_progress",
                "cpu_progress",
                "io_progress",
            )
            if field in evidence
        }
        signal_fingerprint = repr(sorted(independent_fields.items(), key=lambda item: item[0]))
        signal_changed = bool(independent_fields) and signal_fingerprint != state.get("signal_fingerprint")
        if independent_fields:
            state["signal_fingerprint"] = signal_fingerprint

        active_db = bool(evidence.get("db_active") or evidence.get("active_db"))
        if signal_changed or active_db:
            state["last_progress_at"] = float(now)
            state.pop("stall_observed_at", None)
        elif "last_progress_at" not in state:
            # On restart/adoption, a current independent signal establishes a
            # fresh lease.  With no signal, the task's persisted start time is
            # the conservative baseline.
            state["last_progress_at"] = float(task.started_at or now)

        stall_timeout = max(
            1.0,
            float(getattr(config, "import_stall_timeout_sec", 1800) or 1800),
        )
        last_progress_at = float(state.get("last_progress_at") or task.started_at or now)
        if now - last_progress_at <= stall_timeout:
            return True

        observed_at = state.get("stall_observed_at")
        if observed_at is None:
            state["stall_observed_at"] = float(now)
            logging.warning(
                "[%s] import pid=%s has no independent progress for %.1fs; starting stall recheck grace",
                task.root,
                task.pid,
                max(0.0, now - last_progress_at),
            )
            return True
        grace_sec = max(1.0, min(30.0, stall_timeout / 10.0))
        if now - float(observed_at) <= grace_sec:
            return True

        _kill_pid(task.pid, int(getattr(config, "segment_kill_grace_sec", 10) or 10))
        note = "killed_by_import_stall_watchdog"
        _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note=note)
        store.finish(task.row_id, state="timeout", rc=None, note=note)
        _plugin_operation_runtime_finish(
            store,
            task,
            state="timeout",
            rc=None,
            skipped_reason=note,
            stale=True,
            now_ts=now,
        )
        import_watchdog.pop(key, None)
        running.pop(key, None)
        popens.pop(key, None)
        _mark_import_settle(config, task.root, now, elapsed_sec=now - float(task.started_at))
        if task.manual_request_id is not None:
            store.finish_manual_request(task.manual_request_id, "timeout", note)
        logging.warning(
            "[%s] import entity=%s killed by stall watchdog after %.1fs without independent progress",
            task.root,
            task.entity_id,
            max(0.0, now - last_progress_at),
        )
        return False

    def _finish_reconciled_segment_success(task: RunningTask, key: str) -> None:
        _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note=None)
        store.finish(task.row_id, state="done", rc=0, note=None)
        progress_watchdog.pop(key, None)
        if task.task_type == "segment":
            _mark_segment_finished(task.root, task.entity_id, now_ts=now)
        running.pop(key, None)
        popens.pop(key, None)
        if task.manual_request_id is not None:
            store.finish_manual_request(task.manual_request_id, "done", None)
        if (
            segment_terminal_callback is not None
            and task.task_type == "segment"
            and task.entity_id is not None
        ):
            segment_terminal_callback(task, True, None)

    for key, task in list(running.items()):
        if roots is not None and str(task.root) not in roots:
            continue
        _plugin_operation_runtime_heartbeat(store, task, now)
        if bool(getattr(task, "external", False)):
            alive = _is_pid_alive(task.pid)
            if alive and _pid_matches_task_command(task.pid, task.command_str):
                continue
            running.pop(key, None)
            popens.pop(key, None)
            if task.task_type == "segment":
                _mark_segment_finished(task.root, task.entity_id, now_ts=now)
                logging.info(
                    "[%s] segment scheduler exit id=%s outcome=external_released runtime_sec=%.3f",
                    task.root,
                    task.entity_id,
                    max(0.0, now - float(task.started_at)),
                )
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
                lock_busy = rc == _TASK_LOCK_BUSY_RC
                state = "done" if rc == 0 or lock_busy else "failed"
                note = "task_lock_busy" if lock_busy else (None if rc == 0 else "non_zero_exit")
                if rc != 0 and task.task_type in _SEGMENT_RUNTIME_TASK_TYPES:
                    note = "mautic_failed"
                _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note=note)
                store.finish(task.row_id, state=state, rc=rc, note=note)
                _plugin_operation_runtime_finish(
                    store,
                    task,
                    state="skipped" if lock_busy else state,
                    rc=rc,
                    skipped_reason="resource_lock_busy" if lock_busy else "",
                    now_ts=now,
                )
                progress_watchdog.pop(key, None)
                import_watchdog.pop(key, None)
                if rc == 0 and task.task_type == "segment":
                    _mark_segment_finished(task.root, task.entity_id, now_ts=now)
                if task.task_type == "segment":
                    logging.info(
                        "[%s] segment scheduler exit id=%s outcome=%s rc=%s runtime_sec=%.3f",
                        task.root,
                        task.entity_id,
                        "success" if rc == 0 else ("lock_busy" if lock_busy else "failed"),
                        rc,
                        max(0.0, now - float(task.started_at)),
                    )
                if rc == 0 and task.task_type == "campaign_rebuild":
                    _mark_campaign_rebuild_finished(task.root, task.entity_id, now_ts=now)
                if rc == 0 and task.task_type == "campaign_trigger":
                    _mark_campaign_trigger_finished(task.root, task.entity_id)
                    db_cfg = db_configs.get(str(task.root))
                    if db_cfg is not None:
                        _campaign_email_counter_reconcile(
                            db=MauticDB(db_cfg),
                            root=task.root,
                            campaign_id=task.entity_id,
                            reason="trigger-exit",
                            force=True,
                        )
                if rc == 0 and task.task_type == "import":
                    _mark_import_settle(config, task.root, now, elapsed_sec=now - float(task.started_at))
                bootstrap_digest = _plugin_operation_bootstrap_digest_from_task(task)
                if rc == 0 and bootstrap_digest:
                    _mark_plugin_operation_bootstrap_completed(
                        store,
                        bootstrap_digest,
                        reason="successful_bootstrap_command",
                    )
                running.pop(key, None)
                popens.pop(key, None)
                respawned = False
                # A failed import is terminal for this queued item.  Retrying
                # it from the monitor can duplicate a partially applied CSV
                # and races the next FIFO item; the next poll/reconciliation
                # owns any deliberate retry decision.  Explicit retry calls
                # still pass through _respawn_task's per-instance singleton.
                if rc != 0 and not lock_busy and task.task_type != "import":
                    logging.warning("[%s] %s entity=%s failed rc=%s", task.root, task.task_type, task.entity_id, rc)
                    retry_max = _effective_task_retry_max(config, task)
                    retry_enabled = (retry_max <= 0) or (retry_max > 1)
                    if retry_enabled:
                        if config.task_retry_delay_sec > 0:
                            time.sleep(config.task_retry_delay_sec)
                        respawned = _respawn_task(config=config, store=store, running=running, popens=popens, task=task)
                if task.manual_request_id is not None and not respawned:
                    store.finish_manual_request(task.manual_request_id, state, note)
                if (
                    segment_terminal_callback is not None
                    and task.task_type == "segment"
                    and task.entity_id is not None
                    and not respawned
                ):
                    segment_terminal_callback(task, bool(rc == 0), note)
                continue

        alive = _is_pid_alive(task.pid)
        if alive and proc is None and not _pid_matches_task_command(task.pid, task.command_str):
            evidence = _segment_liveness_evidence(task)
            if bool(evidence.get("completed")):
                _finish_reconciled_segment_success(task, key)
                continue
            if bool(evidence.get("active")):
                logging.info(
                    "[%s] %s entity=%s command mismatch deferred while runtime evidence remains active",
                    task.root,
                    task.task_type,
                    task.entity_id,
                )
                continue
            loss_note = "scheduler_error" if task.task_type in _SEGMENT_RUNTIME_TASK_TYPES else "pid_cmd_mismatch"
            _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note=loss_note)
            store.finish(task.row_id, state="lost", rc=None, note=loss_note)
            _plugin_operation_runtime_finish(
                store,
                task,
                state="lost",
                rc=None,
                skipped_reason="owner_pid_command_mismatch",
                stale=True,
                now_ts=now,
            )
            progress_watchdog.pop(key, None)
            import_watchdog.pop(key, None)
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", loss_note)
            if (
                segment_terminal_callback is not None
                and task.task_type == "segment"
                and task.entity_id is not None
            ):
                segment_terminal_callback(task, False, loss_note)
            logging.warning(
                "[%s] %s entity=%s pid=%s command-mismatch detected, marking lost",
                task.root,
                task.task_type,
                task.entity_id,
                task.pid,
            )
            continue

        if alive and _campaign_trigger_progress_watchdog(
            config=config,
            store=store,
            running=running,
            popens=popens,
            task=task,
            key=key,
            db_configs_by_root=db_configs,
            mautic_timezones_by_root=tz_by_root,
            state_by_key=progress_watchdog,
            now_ts=now,
        ):
            continue

        if alive and task.task_type == "import":
            # Imports deliberately bypass the generic command/worker timeout.
            # They may run for hours; only a bounded absence of independent
            # progress evidence is actionable.
            _import_watchdog_keepalive(task, key, _import_liveness_evidence(task))
            continue

        elapsed = now - task.started_at
        timeout_threshold: int | None = None
        if task.timeout_sec > 0 and config.worker_watchdog_sec > 0:
            timeout_threshold = min(task.timeout_sec, config.worker_watchdog_sec)
        elif task.timeout_sec > 0:
            timeout_threshold = task.timeout_sec
        elif config.worker_watchdog_sec > 0:
            timeout_threshold = config.worker_watchdog_sec

        if _plugin_operation_runtime_digest_from_task(task):
            timeout_threshold = None

        if alive and (timeout_threshold is None or elapsed <= timeout_threshold):
            _segment_runtime_marker_write(store, task, status="running", now_ts=now)
            continue

        if alive and timeout_threshold is not None and elapsed > timeout_threshold:
            if task.task_type == "segment":
                logging.warning(
                    "[%s] segment scheduler exit id=%s outcome=timeout runtime_sec=%.3f threshold_sec=%s",
                    task.root,
                    task.entity_id,
                    max(0.0, elapsed),
                    timeout_threshold,
                )
            _kill_pid(task.pid, config.segment_kill_grace_sec)
            _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note="killed_by_timeout")
            store.finish(task.row_id, state="timeout", rc=None, note="killed_by_timeout")
            progress_watchdog.pop(key, None)
            import_watchdog.pop(key, None)
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
            if (
                segment_terminal_callback is not None
                and task.task_type == "segment"
                and task.entity_id is not None
                and not respawned
            ):
                segment_terminal_callback(task, False, "killed_by_timeout")
            continue

        if not alive:
            evidence = _segment_liveness_evidence(task)
            if bool(evidence.get("completed")):
                _finish_reconciled_segment_success(task, key)
                continue
            if bool(evidence.get("active")):
                logging.info(
                    "[%s] %s entity=%s pid lost; keeping claim during bounded runtime reconciliation",
                    task.root,
                    task.task_type,
                    task.entity_id,
                )
                continue
            loss_note = "scheduler_error" if task.task_type in _SEGMENT_RUNTIME_TASK_TYPES else "pid_not_alive"
            _segment_runtime_marker_write(store, task, status="terminal", now_ts=now, note=loss_note)
            if task.task_type == "segment":
                logging.warning(
                    "[%s] segment scheduler exit id=%s outcome=lost runtime_sec=%.3f",
                    task.root,
                    task.entity_id,
                    max(0.0, elapsed),
                )
            store.finish(task.row_id, state="lost", rc=None, note=loss_note)
            _plugin_operation_runtime_finish(
                store,
                task,
                state="lost",
                rc=None,
                skipped_reason="owner_pid_not_alive",
                stale=True,
                now_ts=now,
            )
            progress_watchdog.pop(key, None)
            if task.task_type == "import":
                _mark_import_settle(config, task.root, now, elapsed_sec=now - float(task.started_at))
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", loss_note)
            if (
                segment_terminal_callback is not None
                and task.task_type == "segment"
                and task.entity_id is not None
            ):
                segment_terminal_callback(task, False, loss_note)
            logging.warning(
                "[%s] %s entity=%s pid_lost=%s note=%s",
                task.root,
                task.task_type,
                task.entity_id,
                task.pid,
                loss_note,
            )


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
    segment_liveness_probe: Callable[[RunningTask], Mapping[str, object] | bool] | None = None,
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

    def _liveness_active(task: RunningTask) -> bool:
        if task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES:
            return False
        evidence: Mapping[str, object] | bool = {}
        if segment_liveness_probe is not None:
            try:
                evidence = segment_liveness_probe(task)
            except Exception:
                evidence = {}
        if isinstance(evidence, Mapping):
            if any(
                bool(evidence.get(key))
                for key in ("active", "process", "db", "db_process", "runtime_confirmed", "completed")
            ):
                _segment_runtime_marker_write(store, task, status="running", now_ts=time.time())
                return True
        elif bool(evidence):
            _segment_runtime_marker_write(store, task, status="running", now_ts=time.time())
            return True
        return _segment_runtime_marker_orphan_active(store, task, now_ts=time.time())

    for row in rows:
        task = _running_task_from_row(row)
        if not _is_pid_alive(task.pid):
            if _liveness_active(task):
                valid_by_key[task.task_key] = task
                stats.setdefault("deferred_liveness", 0)
                stats["deferred_liveness"] += 1
                continue
            loss_note = "scheduler_error" if task.task_type in _SEGMENT_RUNTIME_TASK_TYPES else "pid_not_alive"
            _segment_runtime_marker_write(store, task, status="terminal", now_ts=time.time(), note=loss_note)
            store.finish(task.row_id, state="lost", rc=None, note=loss_note)
            stats["lost_pid"] += 1
            continue
        if not _pid_matches_task_command(task.pid, task.command_str):
            if _liveness_active(task):
                valid_by_key[task.task_key] = task
                stats.setdefault("deferred_liveness", 0)
                stats["deferred_liveness"] += 1
                continue
            loss_note = "scheduler_error" if task.task_type in _SEGMENT_RUNTIME_TASK_TYPES else "pid_cmd_mismatch"
            _segment_runtime_marker_write(store, task, status="terminal", now_ts=time.time(), note=loss_note)
            store.finish(task.row_id, state="lost", rc=None, note=loss_note)
            stats["lost_cmd"] += 1
            continue

        # Refresh the persisted marker for adopted runtime work.  This records
        # the PID start ticks through the same PID-reuse-safe primitive used by
        # update admission, so a legacy marker cannot make a recycled PID look
        # like independent liveness evidence.
        if task.task_type in _SEGMENT_RUNTIME_TASK_TYPES:
            _segment_runtime_marker_write(store, task, status="running", now_ts=time.time())

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
    seg_sql_long_done: set[int] | None = None,
    seg_sql_long_ring: deque[int] | None = None,
    monitor_cycle_done: dict[tuple[str, str], set[int]] | None = None,
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
            if seg_sql_long_done is not None:
                seg_sql_long_done.add(entity_id)
            if seg_sql_long_ring is not None:
                _mark_ring_entity_executed(seg_sql_long_ring, entity_id)
            _mark_ring_entity_executed(seg_prio_ring, entity_id)
            _mark_ring_entity_executed(seg_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="segment",
                    entity_id=entity_id,
                )
        elif task.task_type == "campaign_trigger":
            _mark_ring_entity_executed(trg_prio_ring, entity_id)
            _mark_ring_entity_executed(trg_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_trigger",
                    entity_id=entity_id,
                )
        elif task.task_type == "campaign_rebuild":
            _mark_ring_entity_executed(reb_prio_ring, entity_id)
            _mark_ring_entity_executed(reb_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_rebuild",
                    entity_id=entity_id,
                )


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
    monitor_cycle_done: dict[tuple[str, str], set[int]] | None = None,
    db: MauticDB | None = None,
    mautic_major: int = 0,
    segment_super_priority_ids: set[int] | None = None,
    segment_priority_ids: set[int] | None = None,
) -> None:
    pending = store.pending_manual_requests(root, limit=64)
    if not pending:
        return
    if (str(getattr(config, "profile_name", "") or "").strip().lower() == "passive"):
        rejected = 0
        for row in pending:
            try:
                req_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            store.finish_manual_request(req_id, "skipped", "passive_profile_user_dispatch_disabled")
            rejected += 1
        logging.warning(
            "[%s] passive profile rejected manual user-task dispatch requests=%s; cron remains external",
            root,
            rejected,
        )
        return
    manual_segment_snapshot: Mapping[object, object] | None = None
    manual_segment_parents: dict[int, set[int]] = {}
    if db is not None:
        try:
            manual_rows = db.fetch_published_segment_filters()
            manual_segment_snapshot = _ordinary_segment_source_baseline(manual_rows)
            _manual_children, manual_segment_parents = segment_dependency_maps(manual_rows)
        except Exception as exc:
            logging.warning("[%s] manual segment admission read failed: %s", root, exc)
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

            if int(mautic_major or 0) >= 7:
                manual_entity_ids = [int(entity_id)]
            else:
                manual_entity_ids = dependency_expanded_segment_plan(
                    [int(entity_id)],
                    manual_segment_parents,
                )
            if manual_segment_snapshot is None:
                current_cycle = store.get_runtime_sync(_segment_cycle_key(root)) or {}
                current_observed = (
                    current_cycle.get("observed_snapshot")
                    if isinstance(current_cycle, Mapping)
                    else None
                )
            else:
                current_observed = manual_segment_snapshot
            globally_running_ids = {
                int(task.entity_id)
                for task in running.values()
                if task.root == root
                and task.entity_id is not None
                and task.task_type in {"segment", "segment_sql"}
            }
            globally_running_ids.update(_segment_sql_worker_ids(root))
            prepared, prepare_reason = _segment_cycle_manual_prepare(
                store,
                root=root,
                request_id=req_id,
                entity_ids=manual_entity_ids,
                observed_snapshot=current_observed if isinstance(current_observed, Mapping) else None,
                super_priority_ids=set(segment_super_priority_ids or set()),
                priority_ids=set(segment_priority_ids or set()),
                globally_running_ids=globally_running_ids,
                now_ts=time.time(),
            )
            if not prepared:
                store.finish_manual_request(req_id, "skipped", prepare_reason)
                continue

        try:
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
        except Exception:
            if task_type == "segment" and entity_id is not None:
                _segment_cycle_manual_release(
                    store,
                    root=root,
                    request_id=req_id,
                    now_ts=time.time(),
                )
            raise
        if not launched:
            if task_type == "segment" and entity_id is not None:
                _segment_cycle_manual_release(
                    store,
                    root=root,
                    request_id=req_id,
                    now_ts=time.time(),
                )
            store.finish_manual_request(req_id, "skipped", "already_running")
            continue

        task_key = _task_key(root, task_type, entity_id)
        store.mark_manual_request_launched(req_id, task_key)

        if task_type == "segment":
            _mark_ring_entity_executed(seg_sql_ring, entity_id)
            _mark_ring_entity_executed(seg_prio_ring, entity_id)
            _mark_ring_entity_executed(seg_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="segment",
                    entity_id=entity_id,
                )
        elif task_type == "campaign_trigger":
            _mark_ring_entity_executed(trg_prio_ring, entity_id)
            _mark_ring_entity_executed(trg_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_trigger",
                    entity_id=entity_id,
                )
        elif task_type == "campaign_rebuild":
            _mark_ring_entity_executed(reb_prio_ring, entity_id)
            _mark_ring_entity_executed(reb_reg_ring, entity_id)
            if monitor_cycle_done is not None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_rebuild",
                    entity_id=entity_id,
                )

        logging.info(
            "[%s] manual request launched id=%s task=%s entity=%s",
            root,
            req_id,
            task_type,
            entity_id,
        )


def _refresh_instance_db_maps(
    installs: list[object],
    db_configs_by_root: dict[str, object],
    mautic_timezones_by_root: dict[str, str | None],
) -> None:
    active_roots: set[str] = set()
    for inst in installs:
        root = str(getattr(inst, "root", "") or "").strip()
        if not root:
            continue
        active_roots.add(root)
        db_cfg = getattr(inst, "db", None)
        if db_cfg is not None:
            db_configs_by_root[root] = db_cfg
            tz = getattr(inst, "mautic_timezone", None)
            mautic_timezones_by_root[root] = str(tz).strip() if tz else None
        else:
            db_configs_by_root.pop(root, None)
            mautic_timezones_by_root.pop(root, None)
    for root in list(db_configs_by_root):
        if root not in active_roots:
            db_configs_by_root.pop(root, None)
            mautic_timezones_by_root.pop(root, None)


def _inventory_snapshot_for_push(
    inventory: InstanceInventory,
    current: list[MauticInstall],
) -> tuple[list[MauticInstall], bool]:
    try:
        return inventory.list_instances(), True
    except Exception as exc:
        logging.warning("inventory refresh before MCC state push failed: %s", exc)
        return current, False


def _apply_instance_runtime_guard(installs: list[MauticInstall], *, reason: str) -> None:
    try:
        payload = apply_instance_runtime(installs, reload_services=True)
    except Exception as e:
        logging.warning("instance runtime apply failed (%s): %s", reason, e)
        return
    status = str(payload.get("status", "")).strip().lower()
    if status != "ok":
        logging.warning("instance runtime apply failed (%s): %s", reason, payload.get("reason", "unknown"))
        return
    if bool(payload.get("changed", False)):
        logging.info(
            "instance runtime applied (%s): actions=%s",
            reason,
            ",".join(str(x) for x in payload.get("actions", []) if x) or "-",
        )


def run_loop(config: AgentConfig, single_cycle: bool = False) -> None:
    global _ACTIVE_PRIORITY_EXECUTOR, _SEGMENT_DISPATCH_WAKEUP
    logging.info("MCD loop started")
    base_config = config
    profile_authority = str(getattr(config, "profile_name", "") or "").strip().lower()

    try:
        if _ensure_mcd_service_kill_mode():
            logging.info("MCD systemd unit migrated to KillMode=process; running task workers survive supervisor restart")
    except Exception as exc:
        logging.warning("MCD systemd unit migration failed: %s", exc)

    try:
        fail2ban_guard = ensure_nginx_4xx_scan_safety()
        if bool(fail2ban_guard.get("changed", False)):
            logging.warning(
                "Unsafe path-agnostic nginx 4xx jail disabled: released=%s preserved=%s",
                fail2ban_guard.get("released_bans", 0),
                fail2ban_guard.get("preserved_bans", 0),
            )
        elif str(fail2ban_guard.get("status", "")) == "error":
            logging.warning("Fail2ban nginx 4xx safety guard failed: %s", fail2ban_guard.get("reason", "unknown"))
    except Exception as exc:
        logging.warning("Fail2ban nginx 4xx safety guard failed: %s", exc)

    try:
        web_firewall_guard = ensure_managed_web_firewall_loopback()
        if bool(web_firewall_guard.get("changed", False)):
            logging.info("Managed web firewall loopback guard applied")
        elif str(web_firewall_guard.get("status", "")) == "error":
            logging.warning("Managed web firewall loopback guard failed: %s", web_firewall_guard.get("reason", "unknown"))
    except Exception as exc:
        logging.warning("Managed web firewall loopback guard failed: %s", exc)

    campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
    if config.disable_whitelist:
        campaign_whitelist = set()

    store = TaskStore(config.state_db_path, config)
    store.put_runtime_sync(
        "profile_authority",
        {
            "profile": profile_authority,
            "source": "local_config",
            "updated_at": time.time(),
        },
    )
    inventory = InstanceInventory(config.state_db_path)
    seeded = ensure_seeded(inventory, config)
    logging.info("Instance inventory loaded: %s", seeded)
    try:
        rows, source = fetch_custom_manifest(config, use_cache_on_error=True)
        logging.info("Custom scripts manifest loaded: source=%s count=%s", source, len(rows))
    except Exception as e:
        logging.warning("Custom scripts manifest prefetch failed: %s", e)

    running = _SchedulerRuntimeRegistry(_load_orphans_from_store(store))
    popens: dict[str, subprocess.Popen[bytes]] = {}
    db_configs_by_root: dict[str, object] = {}
    mautic_timezones_by_root: dict[str, str | None] = {}
    campaign_progress_watchdog: dict[str, dict[str, object]] = {}
    import_progress_watchdog: dict[str, dict[str, object]] = {}

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
    bootstrap_status = _bootstrap_remote_runtime_before_local_baseline(
        config,
        store,
        [str(getattr(inst, "instance_uid", "") or "") for inst in installs],
    )
    if bootstrap_status == "applied":
        try:
            config = load_config(config.config_file_path, allow_recover_from_mcc=False)
            base_config = config
            profile_authority = str(getattr(config, "profile_name", "") or "").strip().lower()
        except Exception as exc:
            logging.warning("runtime-overrides startup bootstrap reload failed: %s", exc)
    _, startup_local_runtime_fp = _initialize_local_runtime_baseline(config, store)
    _sync_segment_whitelist_file(config, installs)
    _refresh_instance_db_maps(installs, db_configs_by_root, mautic_timezones_by_root)
    _apply_instance_runtime_guard(installs, reason="startup")

    def _segment_liveness_probe(task: RunningTask) -> Mapping[str, object]:
        """Collect read-only Mautic/DB evidence before expiring a lost claim."""

        if task.entity_id is None or task.task_type not in _SEGMENT_RUNTIME_TASK_TYPES:
            return {}
        chain_ids = _segment_chain_entity_ids(store, task.root, int(task.entity_id))
        sid = int(task.entity_id)
        db_cfg = db_configs_by_root.get(str(task.root))
        db_active = False
        db_completed = False
        if db_cfg is not None:
            try:
                for row in MauticDB(db_cfg).fetch_processlist(limit=200):
                    info = str(row.get("Info") or row.get("info") or "").strip().lower()
                    command = str(row.get("Command") or row.get("command") or "").strip().lower()
                    if command and command not in {"query", "execute"}:
                        continue
                    if "lead_lists" not in info and "leadlist" not in info and "segment" not in info:
                        continue
                    if any(re.search(rf"(?<!\d){int(chain_id)}(?!\d)", info) for chain_id in chain_ids):
                        db_active = True
                        break
            except Exception:
                db_active = False
            try:
                rows = MauticDB(db_cfg).fetch_segment_definitions(chain_ids)
                for row in rows:
                    built_raw = row.get("last_built_date") if isinstance(row, Mapping) else None
                    built_at = _parse_mysql_datetime_ts(str(built_raw or ""))
                    if built_at is not None and built_at >= float(task.started_at) - 0.5:
                        db_completed = True
                        break
            except Exception:
                db_completed = False
        return {"db": db_active, "completed": db_completed}

    def _import_liveness_probe(task: RunningTask) -> Mapping[str, object]:
        """Collect independent Mautic import progress signals.

        The persisted scheduler marker is intentionally absent here.  Import
        leases may be extended only by Mautic/DB status changes, upload/log
        file movement, or kernel CPU/IO counters from the owned process.
        """

        evidence: dict[str, object] = {}
        monitor = import_monitor_cache.get(str(task.root))
        if isinstance(monitor, dict):
            monitor_view = {
                "queued": list(monitor.get("queued") or []),
                "running": list(monitor.get("running") or []),
                "done": list(monitor.get("done") or []),
                "item_statuses": dict(monitor.get("item_statuses") or {}),
            }
            if any(monitor_view.values()):
                evidence["db_progress"] = json.dumps(monitor_view, sort_keys=True, default=str)
        db_cfg = db_configs_by_root.get(str(task.root))
        if db_cfg is not None:
            try:
                query_fingerprint: list[str] = []
                for row in MauticDB(db_cfg).fetch_processlist(limit=200):
                    info = str(row.get("Info") or row.get("info") or "").strip()
                    command = str(row.get("Command") or row.get("command") or "").strip().lower()
                    if command and command not in {"query", "execute"}:
                        continue
                    if not re.search(r"(?:^|[^a-z])(imports?|lead_import|mautic_import)(?:[^a-z]|$)", info, re.IGNORECASE):
                        continue
                    query_fingerprint.append(info[:500])
                if query_fingerprint:
                    evidence["db_active"] = True
                    evidence["db_query_progress"] = tuple(sorted(query_fingerprint))
            except Exception:
                pass
        evidence.update(_import_process_liveness_signal(task.pid))
        evidence.update(_import_path_liveness_signal(task.root, task.command_str))
        return evidence

    _sync_and_publish_form_embed_status(config, installs, reason="startup")
    retired_zabbix_probe = retire_zabbix_mautic_version_userparameter()
    if bool(retired_zabbix_probe.get("changed", False)):
        logging.info("retired per-instance Zabbix mautic.version probe")
    elif str(retired_zabbix_probe.get("status", "ok")) == "error":
        logging.warning("could not retire per-instance Zabbix mautic.version probe: %s", retired_zabbix_probe.get("reason", "unknown"))
    segment_sql_rings: dict[str, deque[int]] = {}
    segment_sql_rules_by_root: dict[str, dict[int, SQLSegmentRule]] = {}
    segment_sql_active_sets: dict[str, set[int]] = {}
    segment_sql_done_sets: dict[str, set[int]] = {}
    segment_sql_long_rings: dict[str, deque[int]] = {}
    segment_sql_long_rules_by_root: dict[str, dict[int, SQLSegmentRule]] = {}
    segment_sql_long_active_sets: dict[str, set[int]] = {}
    segment_sql_long_done_sets: dict[str, set[int]] = {}
    segment_sql_auto_signatures: dict[str, tuple[int, ...]] = {}
    segment_dependencies_by_root: dict[str, dict[int, set[int]]] = {}
    segment_parents_by_root: dict[str, dict[int, set[int]]] = {}
    segment_failure_blocked_sets: dict[str, set[int]] = {}
    segment_logical_issue_blocked_sets: dict[str, set[int]] = {}
    segment_logical_data_error_notes: dict[str, dict[int, str]] = {}
    last_logical_issue_scan_ts: dict[str, float] = {}
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
    monitor_cycle_done: dict[tuple[str, str], set[int]] = {}
    queue_samples: dict[str, deque[tuple[float, int]]] = {}
    mail_queue_metrics: dict[str, dict[str, object]] = {}
    throttled: dict[str, bool] = {}
    last_import_poll_ts: dict[str, float] = {}
    last_import_activity_ts: dict[str, float] = {}
    import_pending_cache: dict[str, int] = {}
    import_pending_order: list[str] = []
    import_monitor_cache: dict[str, dict[str, object]] = {}
    import_completed_segment_followups: dict[str, set[int]] = {}
    import_completed_segment_followup_queued_at: dict[str, dict[int, float]] = {}
    import_followup_wait_log_ts: dict[str, float] = {}
    import_completed_segment_imports: dict[str, dict[int, float]] = {}
    last_import_monitor_warn_ts: dict[str, float] = {}
    segment_last_full_scan_ts: dict[str, float] = {}
    segment_full_scan_pending: dict[str, set[int]] = {}
    segment_force_full_scan_until: dict[str, float] = {}
    segment_active_discovery_last_ts: dict[str, float] = {}
    campaign_pressure_at_plan: dict[str, bool] = {}
    last_cleanup_ts: dict[str, float] = {}
    last_mautic_lock_cleanup_ts: dict[str, float] = {}
    last_page_hits_orphan_cleanup_ts: dict[str, float] = {}
    page_hits_orphan_cleanup_active: dict[str, bool] = {}
    page_hits_orphan_cleanup_window_counts: dict[str, int] = {}
    page_hits_orphan_cleanup_window_keys: dict[str, str] = {}
    page_hits_orphan_cleanup_done_window_keys: dict[str, str] = {}
    last_empty_leads_cleanup_ts: dict[str, float] = {}
    last_empty_leads_cleanup_idle_ts: dict[str, float] = {}
    last_empty_leads_cleanup_skip_ts: dict[str, float] = {}
    last_campaign_trigger_audit_ts: dict[str, float] = {}
    campaign_trigger_audit_ids_by_root: dict[str, list[int]] = {}
    campaign_trigger_plan_started_at_by_root: dict[str, dict[int, float]] = {}
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
    next_security_blocklist_sync_at = 0.0
    next_backup_profile_sync_at = 0.0
    next_backup_storage_probe_at = 0.0
    next_runtime_overrides_poll_at = 0.0
    next_managed_cron_reconcile_at = 0.0
    next_plugin_operation_cron_reconcile_at = 0.0
    next_message_queue_cron_reconcile_at = 0.0
    message_queue_settings_push_pending: dict[str, object] | None = None
    next_email_fetch_cron_reconcile_at = 0.0
    next_empty_leads_cleanup_cron_reconcile_at = 0.0
    next_cluster_assets_guard_at = 0.0
    next_inventory_auto_rescan_at = time.time() + min(300, max(60, int(getattr(config, "poll_interval_sec", 60) or 60)))
    last_inventory_signature = ""
    runtime_overrides_sync_requested = False
    local_runtime_push_pending = _read_local_runtime_push_pending(store) is not None
    # Always perform an initial runtime-overrides sync after daemon start/restart.
    # This is required even when periodic polling is disabled to avoid running
    # with stale local-only runtime after self-update/service restart.
    startup_runtime_sync_pending = bool(config.mcc_url and config.mcc_token)
    next_profile_guard_at = 0.0
    last_runtime_overrides_fp = ""
    last_runtime_overrides_error = ""
    last_monitor_signals_push_error = ""
    last_local_runtime_fp = startup_local_runtime_fp
    dispatch_instance_cursor = 0
    scheduler_pending_since_by_root: dict[str, float] = {}
    scheduler_last_promoted_roots: frozenset[str] = frozenset()
    scheduler_next_fairness_log_at = 0.0
    pusher = MCCStatePusher(config, runtime_store=store)

    priority_executor = _PriorityTaskExecutor()
    _ACTIVE_PRIORITY_EXECUTOR = priority_executor
    segment_dispatch_wakeup = threading.Event()
    ordinary_refill_wakeup = threading.Event()
    planner_busy = threading.Event()
    # The persisted refill worker runs continuously.  Only the root currently
    # being handled by the main dispatcher is gated; a slow Mautic/DB section
    # for root A must not stop admitted work for root B.
    main_dispatch_busy_roots: set[str] = set()
    lifecycle_monitor_busy_roots: set[str] = set()
    lifecycle_monitor_futures: dict[str, Future[object]] = {}
    lifecycle_monitor_executors: dict[str, ThreadPoolExecutor] = {}
    lifecycle_monitor_wakeup = threading.Event()
    _SEGMENT_DISPATCH_WAKEUP = segment_dispatch_wakeup

    def _segment_terminal_callback(task: RunningTask, success: bool, note: str | None) -> None:
        # A terminal worker frees a slot immediately. Wake the scheduler so
        # the next claim does not wait for the configured dispatch interval.
        _segment_terminal_and_wake(store, segment_dispatch_wakeup, task, success, note)
        ordinary_refill_wakeup.set()

    def _monitor_one_root(root: str) -> None:
        """Run the canonical lifecycle monitor for one root-local slice."""

        try:
            _monitor_running(
                config=config,
                store=store,
                running=running,
                popens=popens,
                db_configs_by_root=db_configs_by_root,
                mautic_timezones_by_root=mautic_timezones_by_root,
                campaign_progress_watchdog=campaign_progress_watchdog,
                import_progress_watchdog=import_progress_watchdog,
                segment_terminal_callback=_segment_terminal_callback,
                segment_liveness_probe=_segment_liveness_probe,
                import_liveness_probe=_import_liveness_probe,
                roots={root},
            )
        finally:
            with _SCHEDULER_RUNTIME_LOCK:
                lifecycle_monitor_busy_roots.discard(root)

    def _monitor_authoritative_runtime() -> None:
        """Schedule the one canonical lifecycle monitor in root-local slices.

        A process terminal event, TaskStore completion, retry, and ordinary
        slot release must not wait behind a slow liveness/import probe for a
        different root. Every slice calls the same ``_monitor_running``
        implementation; only its execution lane is root-local.
        """

        if single_cycle:
            _monitor_running(
                config=config,
                store=store,
                running=running,
                popens=popens,
                db_configs_by_root=db_configs_by_root,
                mautic_timezones_by_root=mautic_timezones_by_root,
                campaign_progress_watchdog=campaign_progress_watchdog,
                import_progress_watchdog=import_progress_watchdog,
                segment_terminal_callback=_segment_terminal_callback,
                segment_liveness_probe=_segment_liveness_probe,
                import_liveness_probe=_import_liveness_probe,
            )
            return

        roots = {
            str(getattr(inst, "root", "") or "").strip()
            for inst in installs
            if str(getattr(inst, "root", "") or "").strip()
        }
        roots.update(str(task.root).strip() for task in running.values() if str(task.root).strip())
        for root, future in list(lifecycle_monitor_futures.items()):
            if not future.done():
                continue
            lifecycle_monitor_futures.pop(root, None)
            try:
                future.result()
            except Exception as exc:
                logging.warning("[%s] canonical lifecycle monitor failed: %s", root, exc)

        for root in sorted(roots):
            if root in lifecycle_monitor_futures:
                continue
            with _SCHEDULER_RUNTIME_LOCK:
                if root in main_dispatch_busy_roots or root in lifecycle_monitor_busy_roots:
                    continue
                lifecycle_monitor_busy_roots.add(root)
            executor = lifecycle_monitor_executors.setdefault(
                root,
                ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="mcd-root-lifecycle-" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8],
                ),
            )
            try:
                lifecycle_monitor_futures[root] = executor.submit(_monitor_one_root, root)
            except Exception:
                with _SCHEDULER_RUNTIME_LOCK:
                    lifecycle_monitor_busy_roots.discard(root)
                raise

    def _lifecycle_monitor_scheduler() -> None:
        """Continuously tick the canonical root-local lifecycle monitors."""

        while True:
            try:
                _monitor_authoritative_runtime()
            except Exception as exc:
                logging.warning("canonical lifecycle monitor scheduler failed: %s", exc)
            lifecycle_monitor_wakeup.wait(
                timeout=max(0.1, min(1.0, float(getattr(config, "dispatch_interval_sec", 1) or 1)))
            )
            lifecycle_monitor_wakeup.clear()

    def _active_cycle_observation_worker() -> None:
        """Observe late admissions/imports in independent root-local jobs."""

        futures: dict[str, Future[dict[str, object]]] = {}
        executors: dict[str, ThreadPoolExecutor] = {}
        last_submitted: dict[str, float] = {}
        plan_futures: dict[str, Future[dict[str, object]]] = {}
        plan_executors: dict[str, ThreadPoolExecutor] = {}
        plan_last_submitted: dict[str, float] = {}
        last_error_log = 0.0

        def _observe_root(inst: object, admitted_at: float | None) -> dict[str, object]:
            root = str(getattr(inst, "root", "") or "").strip()
            db = MauticDB(getattr(inst, "db"))
            sql_ctx = campaign_sql_time_context(
                datetime.now(timezone.utc),
                getattr(inst, "mautic_timezone", None),
                getattr(inst, "mautic_major", None),
            )
            result: dict[str, object] = {
                "root": root,
                "late_rows": [],
                "admitted_at": float(admitted_at or 0.0),
            }
            if admitted_at is not None and admitted_at > 0:
                admitted_text = datetime.fromtimestamp(
                    float(admitted_at),
                    tz=timezone.utc,
                ).strftime("%Y-%m-%d %H:%M:%S")
                result["late_rows"] = db.fetch_rows(
                    _SQL_SEGMENTS_LATE_DISCOVERY,
                    limit=100,
                    context={
                        **sql_ctx,
                        "active_cycle_admitted_at": admitted_text,
                    },
                )
            if cluster_route_allows(config, "import") and bool(getattr(config, "enable_import_polling", True)):
                result["import_pending"] = _fetch_import_pending_count(db, config, root, sql_ctx)
                result["import_monitor"] = _fetch_import_monitor_snapshot(db, root, sql_ctx)
            return result

        def _plan_terminal_root(inst: object) -> dict[str, object]:
            """Build one root's next ordinary cycle without a host-wide plan.

            This is intentionally a root-local admission snapshot, not a
            second executor/lifecycle.  It covers the terminal -> next-cycle
            transition that cannot be delegated to the persisted refill worker
            (which only consumes already-admitted work).  A slow DB for root A
            therefore occupies only A's planner future; root B can admit and
            refill its own next cycle.
            """

            root = str(getattr(inst, "root", "") or "").strip()
            now_plan = time.time()
            state = store.get_runtime_sync(_segment_cycle_key(root)) or {}
            if not isinstance(state, Mapping):
                state = {}
            current_status = str(state.get("status") or "").strip().lower()
            if current_status == "active" and (
                _segment_cycle_pending_ids(state)
                or _normalize_segment_ids(state.get("claimed"))
                or _normalize_segment_ids(state.get("running"))
            ):
                return {"root": root, "status": "active"}
            if not root or not getattr(inst, "db", None):
                return {"root": root, "status": "no_db"}
            if not cluster_route_allows(config, "cron"):
                return {"root": root, "status": "cron_disallowed"}
            try:
                if scheduler_pause_path(config).exists():
                    return {"root": root, "status": "scheduler_paused"}
            except Exception:
                pass
            try:
                backup_paused, backup_reason = _backup_dispatch_pause_state(
                    config,
                    backup_running=bool(config.backup_enabled and backup_lock_active(config)),
                )
            except Exception:
                backup_paused, backup_reason = False, ""
            if backup_paused:
                return {"root": root, "status": "backup_paused", "reason": backup_reason}
            db_pause_until = float(db_dispatch_pause_until.get(root, 0.0) or 0.0)
            if db_pause_until > now_plan:
                return {"root": root, "status": "db_paused", "db_pause_until": db_pause_until}

            db = MauticDB(getattr(inst, "db"))
            sql_ctx = campaign_sql_time_context(
                datetime.now(timezone.utc),
                getattr(inst, "mautic_timezone", None),
                getattr(inst, "mautic_major", None),
            )
            lazy_segment_plan = _segment_lazy_update_effective_setting(config, inst)
            try:
                if lazy_segment_plan:
                    observed_ids = db.fetch_ids(config.sql_segments_due, limit=5000, context=sql_ctx)
                else:
                    observed_ids = db.fetch_ids(_SQL_SEGMENTS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                dep_rows = db.fetch_published_segment_filters()
            except Exception as exc:
                if _is_db_dispatch_pause_error(exc):
                    _mark_db_dispatch_pause(
                        root=root,
                        reason=f"root-local terminal plan db error: {exc}",
                        now_ts=now_plan,
                        pause_until=db_dispatch_pause_until,
                        pause_reasons=db_dispatch_pause_reasons,
                        pause_sec=int(
                            effective_db_watchdog_config(config).get(
                                "dispatch_pause_sec",
                                _DB_DISPATCH_PAUSE_SEC,
                            )
                            or _DB_DISPATCH_PAUSE_SEC
                        ),
                    )
                return {"root": root, "status": "db_error", "error": str(exc)}

            observed = _unique_positive_ids(list(observed_ids or []))
            static_configured = _segment_priority_effective_setting(config, inst) | _segment_super_priority_effective_setting(
                config,
                inst,
            )
            dependency_children, dependency_parents = segment_dependency_maps(dep_rows)
            segment_dependencies_by_root[root] = dependency_children
            segment_parents_by_root[root] = dependency_parents
            static_excluded = set(static_configured)
            static_plans = _build_static_segment_lane_plans(
                super_priority_ids=_segment_super_priority_effective_setting(config, inst),
                priority_ids=_segment_priority_effective_setting(config, inst),
                parents_by_child=dependency_parents,
                children_by_parent=dependency_children,
                mautic_major=int(getattr(inst, "mautic_major", 0) or 0),
            )
            for lane_ids in static_plans.values():
                static_excluded.update(int(sid) for sid in lane_ids if int(sid) > 0)

            previous_baseline = state.get("baseline") if state.get("baseline_ready") else {}
            previous_baseline = previous_baseline if isinstance(previous_baseline, Mapping) else {}
            retry_ids = [*(_normalize_segment_ids(state.get("errors"))), *(_normalize_segment_ids(state.get("manual_retry_ids")))]
            never_built = _never_built_segment_ids(dep_rows)
            plan_groups: dict[str, list[int]] = {}
            standard_ids = _ordinary_segment_snapshot_order(
                dep_rows,
                observed,
                static_priority_ids=static_excluded,
                baseline_snapshot=previous_baseline,
                retry_ids=retry_ids,
                stale_after_sec=int(
                    getattr(config, "segment_stale_after_sec", _SEGMENT_STALE_PRIORITY_SEC)
                    or _SEGMENT_STALE_PRIORITY_SEC
                ),
                now_ts=float(now_plan),
                include_fresh_remaining=not lazy_segment_plan,
                groups=plan_groups,
            )
            if observed and not standard_ids and not lazy_segment_plan:
                # In full-scan mode the published snapshot is deliberately
                # authoritative even if the dependency read lags. In lazy
                # mode an empty plan is meaningful: do not turn a fresh
                # current snapshot back into ordinary work.
                standard_ids = list(observed)
                plan_groups["important"] = list(standard_ids)
                plan_groups["remaining"] = []
            important_ids = set(_normalize_segment_ids(plan_groups.get("important")))
            important_ids.update(_normalize_segment_ids(never_built))
            if int(getattr(inst, "mautic_major", 0) or 0) >= 7:
                standard_ids, _suppressed = mautic7_terminal_segment_plan(standard_ids, dependency_children)
            else:
                standard_ids = dependency_expanded_segment_plan(standard_ids, dependency_parents)
                dependent_ids = stale_dependent_segment_closure(
                    _recent_finished_segment_ids(root, now_plan),
                    dep_rows,
                    dependency_children,
                )
                standard_ids = list(dict.fromkeys([*standard_ids, *dependent_ids]))
            # Dependencies required by an important seed are important too;
            # otherwise a parent could be delayed behind the fresh tail.
            important_closure = set(important_ids)
            frontier = list(important_ids)
            while frontier:
                child = frontier.pop()
                for parent in dependency_parents.get(int(child), set()):
                    if int(parent) not in important_closure:
                        important_closure.add(int(parent))
                        frontier.append(int(parent))
            standard_ids = [sid for sid in standard_ids if sid not in static_excluded]
            important_ids.intersection_update(standard_ids)
            important_ids.update(int(sid) for sid in standard_ids if int(sid) in important_closure)
            remaining_ids = set(standard_ids) - important_ids
            if not standard_ids and not never_built:
                return {"root": root, "status": "no_new_cycle"}

            pressure = _campaign_pressure_active(
                config,
                running,
                root,
                trigger_prio_ring=campaign_trigger_prio_rings.setdefault(root, deque()),
                trigger_reg_ring=campaign_trigger_reg_rings.setdefault(root, deque()),
                rebuild_prio_ring=campaign_rebuild_prio_rings.setdefault(root, deque()),
                rebuild_reg_ring=campaign_rebuild_reg_rings.setdefault(root, deque()),
                trigger_dynamic_blocked=lambda _cid: False,
                now_ts=now_plan,
            )
            cycle_planned, cycle_never_built = _segment_cycle_admission_inputs(
                state,
                standard_ids,
                never_built,
                pressure=pressure,
            )
            with _SCHEDULER_RUNTIME_LOCK:
                running_ids = [
                    int(task.entity_id)
                    for task in running.values()
                    if task.root == root and task.task_type == "segment" and task.entity_id is not None
                ]
            cycle_state, added_ids, started_new = _segment_cycle_sync(
                store,
                root=root,
                planned_ids=cycle_planned,
                never_built_ids=cycle_never_built,
                running_ids=running_ids,
                excluded_ids=static_excluded,
                now_ts=now_plan,
                source_snapshot=_ordinary_segment_source_baseline(dep_rows),
                important_ids=[sid for sid in cycle_planned if sid in important_ids]
                + [sid for sid in cycle_never_built if sid in important_ids],
                remaining_ids=[sid for sid in cycle_planned if sid in remaining_ids],
                lazy_mode=lazy_segment_plan,
            )
            pending = _segment_cycle_pending_order(cycle_state, excluded_ids=static_excluded)
            filter_hint_ids: set[int] = set()
            if pending:
                try:
                    invalid_issues = segment_invalid_filter_issues(db.fetch_segment_definitions(pending))
                    filter_hint_ids = set(invalid_issues)
                    if filter_hint_ids:
                        _log_invalid_segment_filters(
                            root,
                            format_segment_filter_issues(invalid_issues),
                        )
                except Exception as exc:
                    logging.warning("[%s] root-local filter hint inspection failed: %s", root, exc)
            failure_blocked = _segment_failure_blocked_ids(store, root)
            segment_failure_blocked_sets[root] = failure_blocked
            logical_blocked = set(segment_logical_issue_blocked_sets.get(root, set()))
            blocked_ids = failure_blocked | logical_blocked
            static_state = store.get_runtime_sync(_static_lane_state_key(root)) or {}
            static_active: set[int] = set()
            lanes = static_state.get("lanes") if isinstance(static_state, Mapping) else {}
            if isinstance(lanes, Mapping):
                for lane in lanes.values():
                    if isinstance(lane, Mapping):
                        static_active.update(_normalize_segment_ids(lane.get("claimed")))
                        static_active.update(_normalize_segment_ids(lane.get("running")))
            sql_ids = set(segment_sql_active_sets.get(root, set())) | set(segment_sql_long_active_sets.get(root, set()))
            import_targets: set[int] = set()
            for targets in _import_monitor_target_segment_ids(
                import_monitor_cache.get(root),
                lanes={"queued", "running"},
            ).values():
                import_targets.update(targets)
            priority_pending = bool(
                segment_prio_rings.get(root)
                or campaign_trigger_prio_rings.get(root)
                or campaign_rebuild_prio_rings.get(root)
            )
            slot_limit = _effective_segment_slot_limit(config, bool(pressure))
            context = {
                "schema": "mcd-segment-dispatch-context-v1",
                "root": root,
                "mautic_major": int(getattr(inst, "mautic_major", 0) or 0),
                "updated_at": float(time.time()),
                "cycle_id": cycle_state.get("cycle_id"),
                "cycle_status": str(cycle_state.get("status") or ""),
                "blocked_ids": sorted(blocked_ids),
                "sql_managed_ids": sorted(sql_ids),
                "static_configured_ids": sorted(static_configured),
                "static_active_ids": sorted(static_active),
                "active_import_target_ids": sorted(import_targets),
                "parents_by_child": {str(k): sorted(v) for k, v in dependency_parents.items()},
                "children_by_parent": {str(k): sorted(v) for k, v in dependency_children.items()},
                "scheduler_pause": False,
                "backup_pause": False,
                "backup_pause_reason": "",
                "dispatch_pause": False,
                "db_pause_until": float(db_dispatch_pause_until.get(root, 0.0) or 0.0),
                "db_pause_reason": str(db_dispatch_pause_reasons.get(root, "") or ""),
                "cluster_cron_allowed": True,
                "cluster_import_allowed": cluster_route_allows(config, "import"),
                "throttled_active": bool(pressure),
                "priority_pending": priority_pending,
                "segment_slot_limit": slot_limit,
                "segment_lazy_update_enabled": bool(lazy_segment_plan),
            }
            store.put_runtime_sync(_segment_dispatch_context_key(root), context)
            pending_after = _segment_cycle_pending_order(cycle_state, excluded_ids=static_excluded)
            store.put_runtime_sync(
                _segment_pending_guard_key(root),
                {
                    "schema": "mcd-segment-pending-guard-v1",
                    "root": root,
                    "updated_at": float(time.time()),
                    "cycle_status": str(cycle_state.get("status") or ""),
                    "cycle_id": cycle_state.get("cycle_id"),
                    "eligible_pending_count": len(_segment_cycle_pending_ids(cycle_state)),
                    "projected_pending_count": len(pending_after),
                    "blocked_count": len(set(pending_after) & blocked_ids),
                    "blocked_ids": sorted(set(pending_after) & blocked_ids)[:100],
                    "sql_managed_count": len(sql_ids),
                    "sql_managed_ids": sorted(sql_ids)[:100],
                    "static_configured_count": len(static_configured),
                    "static_configured_ids": sorted(static_configured)[:100],
                    "static_active_count": len(static_active),
                    "db_pause_until": float(db_dispatch_pause_until.get(root, 0.0) or 0.0),
                    "db_pause_reason": str(db_dispatch_pause_reasons.get(root, "") or ""),
                    "cluster_cron_allowed": True,
                    "cluster_import_allowed": cluster_route_allows(config, "import"),
                    "absolute_total_limit": None,
                    "free_launch_budget": None,
                    "launched": 0,
                    "reason": "root_local_terminal_plan",
                },
            )
            return {
                "root": root,
                "status": "admitted",
                "cycle_id": cycle_state.get("cycle_id"),
                "started_new": started_new,
                "added": added_ids,
            }

        while True:
            now_observer = time.time()
            for root, future in list(futures.items()):
                if not future.done():
                    continue
                futures.pop(root, None)
                try:
                    result = future.result()
                except Exception as exc:
                    if _is_db_dispatch_pause_error(exc):
                        _mark_db_dispatch_pause(
                            root=root,
                            reason=f"root-local observer db error: {exc}",
                            now_ts=now_observer,
                            pause_until=db_dispatch_pause_until,
                            pause_reasons=db_dispatch_pause_reasons,
                            pause_sec=int(
                                effective_db_watchdog_config(config).get(
                                    "dispatch_pause_sec",
                                    _DB_DISPATCH_PAUSE_SEC,
                                )
                                or _DB_DISPATCH_PAUSE_SEC
                            ),
                        )
                    if now_observer - last_error_log >= 30.0:
                        last_error_log = now_observer
                        logging.warning("[%s] root-local observer failed: %s", root, exc)
                    continue
                late_rows = result.get("late_rows") if isinstance(result, Mapping) else []
                if isinstance(result, Mapping):
                    late_rows = _active_cycle_follow_up_rows(
                        late_rows if isinstance(late_rows, list) else [],
                        admitted_at=result.get("admitted_at"),
                    )
                if isinstance(late_rows, list) and late_rows:
                    state = store.get_runtime_sync(_segment_cycle_key(root)) or {}
                    if isinstance(state, Mapping) and str(state.get("status") or "").lower() == "active":
                        inst = next(
                            (
                                candidate
                                for candidate in list(installs)
                                if str(getattr(candidate, "root", "") or "").strip() == root
                            ),
                            None,
                        )
                        if inst is not None:
                            static_ids = _segment_priority_effective_setting(config, inst) | _segment_super_priority_effective_setting(
                                config,
                                inst,
                            )
                            late_ids = [
                                int(row.get("id") or 0)
                                for row in late_rows
                                if isinstance(row, Mapping) and int(row.get("id") or 0) > 0
                            ]
                            never_built = [
                                int(row.get("id") or 0)
                                for row in late_rows
                                if isinstance(row, Mapping)
                                and int(row.get("id") or 0) > 0
                                and not row.get("last_built_date")
                            ]
                            running_ids = [
                                int(row.get("entity_id"))
                                for row in store.running_rows()
                                if str(row.get("root") or "") == root
                                and str(row.get("task_type") or "") == "segment"
                                and row.get("entity_id") is not None
                            ]
                            next_state, added_ids, _ = _segment_cycle_sync(
                                store,
                                root=root,
                                planned_ids=late_ids,
                                never_built_ids=never_built,
                                running_ids=running_ids,
                                excluded_ids=static_ids,
                                now_ts=now_observer,
                                source_snapshot=_ordinary_segment_source_baseline(late_rows),
                            )
                            segment_active_discovery_last_ts[root] = now_observer
                            if added_ids:
                                logging.info(
                                    "[%s] bounded active-cycle discovery queued follow_up=%s cycle_id=%s",
                                    root,
                                    ",".join(str(sid) for sid in added_ids[:50]),
                                    next_state.get("cycle_id", "-"),
                                )
                if isinstance(result, Mapping) and "import_pending" in result:
                    import_pending_cache[root] = max(0, int(result.get("import_pending") or 0))
                    monitor = result.get("import_monitor")
                    if isinstance(monitor, dict):
                        import_monitor_cache[root] = monitor
                        completed_target_segments = _import_monitor_target_segment_ids(
                            monitor,
                            lanes={"done"},
                            successful_only=True,
                        )
                        completed_imports = import_completed_segment_imports.setdefault(root, {})
                        for import_id, targets in completed_target_segments.items():
                            if import_id in completed_imports:
                                continue
                            import_completed_segment_followups.setdefault(root, set()).update(targets)
                            queued_at = import_completed_segment_followup_queued_at.setdefault(root, {})
                            for segment_id in targets:
                                queued_at.setdefault(segment_id, now_observer)
                            completed_imports[import_id] = now_observer
                        for import_id, observed_at in list(completed_imports.items()):
                            if now_observer - float(observed_at) > 3600.0:
                                completed_imports.pop(import_id, None)
                ordinary_refill_wakeup.set()

            for root, future in list(plan_futures.items()):
                if not future.done():
                    continue
                plan_futures.pop(root, None)
                try:
                    result = future.result()
                except Exception as exc:
                    if _is_db_dispatch_pause_error(exc):
                        _mark_db_dispatch_pause(
                            root=root,
                            reason=f"root-local terminal planner db error: {exc}",
                            now_ts=now_observer,
                            pause_until=db_dispatch_pause_until,
                            pause_reasons=db_dispatch_pause_reasons,
                            pause_sec=int(
                                effective_db_watchdog_config(config).get(
                                    "dispatch_pause_sec",
                                    _DB_DISPATCH_PAUSE_SEC,
                                )
                                or _DB_DISPATCH_PAUSE_SEC
                            ),
                        )
                    if now_observer - last_error_log >= 30.0:
                        last_error_log = now_observer
                        logging.warning("[%s] root-local terminal planner failed: %s", root, exc)
                    continue
                if isinstance(result, Mapping) and str(result.get("status") or "") == "admitted":
                    logging.info(
                        "[%s] root-local terminal planner admitted cycle=%s started_new=%s added=%s",
                        root,
                        result.get("cycle_id", "-"),
                        bool(result.get("started_new", False)),
                        ",".join(str(value) for value in list(result.get("added") or [])[:50]) or "-",
                    )
                ordinary_refill_wakeup.set()

            for inst in list(installs):
                root = str(getattr(inst, "root", "") or "").strip()
                if not root or not getattr(inst, "db", None) or root in futures:
                    continue
                state = store.get_runtime_sync(_segment_cycle_key(root)) or {}
                active = isinstance(state, Mapping) and str(state.get("status") or "").lower() == "active"
                admitted_at = float(state.get("admitted_at") or 0.0) if active else None
                if not active and root not in plan_futures:
                    plan_interval = max(
                        5.0,
                        min(30.0, float(getattr(config, "dispatch_interval_sec", 1) or 1) * 10.0),
                    )
                    if now_observer - float(plan_last_submitted.get(root, 0.0) or 0.0) >= plan_interval:
                        plan_last_submitted[root] = now_observer
                        planner_executor = plan_executors.setdefault(
                            root,
                            ThreadPoolExecutor(
                                max_workers=1,
                                thread_name_prefix=(
                                    "mcd-root-plan-"
                                    + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
                                ),
                            ),
                        )
                        plan_futures[root] = planner_executor.submit(_plan_terminal_root, inst)
                observe_interval = max(
                    5.0,
                    min(30.0, float(getattr(config, "dispatch_interval_sec", 1) or 1) * 5.0),
                )
                if now_observer - float(last_submitted.get(root, 0.0) or 0.0) < observe_interval:
                    continue
                if not active and not cluster_route_allows(config, "import"):
                    continue
                last_submitted[root] = now_observer
                root_executor = executors.setdefault(
                    root,
                    ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix=(
                            "mcd-root-observe-"
                            + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
                        ),
                    ),
                )
                futures[root] = root_executor.submit(_observe_root, inst, admitted_at)
            time.sleep(0.2)

    def _ordinary_persisted_refill_worker() -> None:
        """Keep admitted ordinary cycles moving while DB planning is slow.

        This is a second scheduling *tick*, not a second task lifecycle.  It
        uses the shared ``running``/``popens`` registry, the complete durable
        dispatch context, the canonical ``_fill_from_ring`` admission path,
        and the same submit/claim/terminal callbacks as the canonical
        ``_monitor_running`` path.  Lifecycle monitoring runs in separate
        root-local slices using that same canonical implementation; a slow
        liveness/import probe must never be placed in this shared refill path.
        The refill worker runs continuously for already-admitted
        persisted work, yielding only for the root currently owned by the
        main dispatcher.  It therefore keeps ordinary queues moving during
        slow planning without creating a second lifecycle or competing with
        the main dispatcher.
        """

        last_error_log = 0.0

        def _relation_map(raw: object) -> dict[int, set[int]]:
            if not isinstance(raw, Mapping):
                return {}
            result: dict[int, set[int]] = {}
            for raw_key, raw_values in raw.items():
                try:
                    key = int(raw_key)
                except Exception:
                    continue
                if key <= 0:
                    continue
                values: set[int] = set()
                for raw_value in raw_values if isinstance(raw_values, (list, tuple, set)) else []:
                    try:
                        value = int(raw_value)
                    except Exception:
                        continue
                    if value > 0:
                        values.add(value)
                result[key] = values
            return result

        def _dynamic_blocked(
            *,
            root: str,
            entity_id: int,
            parents_by_child: dict[int, set[int]],
            children_by_parent: dict[int, set[int]],
            cycle_ids: set[int],
            static_active_ids: set[int],
            active_import_target_ids: set[int],
        ) -> bool:
            if int(entity_id) in active_import_target_ids:
                return True
            if not parents_by_child and not children_by_parent:
                return False
            try:
                candidate_related = segment_related_ids(
                    int(entity_id),
                    parents_by_child,
                    children_by_parent,
                )
            except Exception:
                candidate_related = {int(entity_id)}
            if not candidate_related:
                return False
            for task in running.values():
                if task.root != root or task.entity_id is None:
                    continue
                if task.task_type not in {"segment", "segment_sql"}:
                    continue
                try:
                    running_related = segment_related_ids(
                        int(task.entity_id),
                        parents_by_child,
                        children_by_parent,
                    )
                except Exception:
                    running_related = {int(task.entity_id)}
                if candidate_related & running_related:
                    return True
            priority_types = {
                "segment",
                "segment_priority",
                "segment_super_priority",
                "segment_recurring_priority",
            }
            for priority_sid in cycle_ids | static_active_ids:
                if not any(priority_executor.is_active(root, task_type, int(priority_sid)) for task_type in priority_types):
                    continue
                try:
                    priority_related = segment_related_ids(
                        int(priority_sid),
                        parents_by_child,
                        children_by_parent,
                    )
                except Exception:
                    priority_related = {int(priority_sid)}
                if candidate_related & priority_related:
                    return True
            for sql_sid in _segment_sql_worker_ids(root):
                try:
                    sql_related = segment_related_ids(
                        int(sql_sid),
                        parents_by_child,
                        children_by_parent,
                    )
                except Exception:
                    sql_related = {int(sql_sid)}
                if candidate_related & sql_related:
                    return True
            return False

        while True:
            try:
                with _SCHEDULER_RUNTIME_LOCK:
                    if not cluster_route_allows(config, "cron"):
                        continue
                    cluster_import_allowed = cluster_route_allows(config, "import")
                    try:
                        scheduler_paused = bool(scheduler_pause_path(config).exists())
                    except Exception:
                        scheduler_paused = False
                    try:
                        backup_paused, _backup_reason = _backup_dispatch_pause_state(
                            config,
                            backup_running=bool(config.backup_enabled and backup_lock_active(config)),
                        )
                    except Exception:
                        backup_paused = False
                    if scheduler_paused or backup_paused:
                        continue
                    for inst in list(installs):
                        root = str(getattr(inst, "root", "") or "").strip()
                        if not root or not getattr(inst, "db", None):
                            continue
                        if root in main_dispatch_busy_roots or root in lifecycle_monitor_busy_roots:
                            continue
                        state = store.get_runtime_sync(_segment_cycle_key(root)) or {}
                        if not isinstance(state, Mapping):
                            continue
                        active_cycle = str(state.get("status") or "").strip().lower() == "active"
                        import_pending = max(0, int(import_pending_cache.get(root, 0) or 0))
                        if not active_cycle and import_pending <= 0:
                            continue
                        context = store.get_runtime_sync(_segment_dispatch_context_key(root)) or {}
                        if (
                            not isinstance(context, Mapping)
                            or context.get("schema") != "mcd-segment-dispatch-context-v1"
                        ):
                            # No authoritative snapshot yet: let the normal
                            # planner establish one instead of guessing from
                            # diagnostic telemetry.
                            continue
                        try:
                            context_updated = float(context.get("updated_at") or 0.0)
                        except Exception:
                            context_updated = 0.0
                        if context_updated <= 0.0:
                            continue
                        db_pause_until = max(
                            float(context.get("db_pause_until") or 0.0),
                            float(db_dispatch_pause_until.get(root, 0.0) or 0.0),
                        )
                        now_root = time.time()
                        if db_pause_until > now_root:
                            continue
                        if not bool(context.get("cluster_cron_allowed", True)):
                            continue

                        blocked_ids = set(_normalize_segment_ids(context.get("blocked_ids")))
                        sql_ids = set(_normalize_segment_ids(context.get("sql_managed_ids")))
                        static_ids = set(_normalize_segment_ids(context.get("static_configured_ids")))
                        static_ids |= _segment_priority_effective_setting(config, inst)
                        static_ids |= _segment_super_priority_effective_setting(config, inst)
                        excluded_ids = sql_ids | static_ids
                        pending_order = (
                            _segment_cycle_pending_order(state, excluded_ids=excluded_ids)
                            if active_cycle
                            else []
                        )
                        throttled_raw = context.get("throttled_active")
                        priority_raw = context.get("priority_pending")
                        slot_raw = context.get("segment_slot_limit")
                        if throttled_raw is None or priority_raw is None or slot_raw is None:
                            continue
                        throttled_active = bool(throttled_raw)
                        priority_pending = bool(priority_raw)
                        segment_slot_limit = max(0, int(slot_raw or 0))

                        if cluster_import_allowed and import_pending > 0:
                            _submit_import_if_segment_slot(
                                config=config,
                                store=store,
                                running=running,
                                popens=popens,
                                root=root,
                                cluster_import_allowed=True,
                                import_pending_count=import_pending,
                                segment_slot_limit=max(1, segment_slot_limit),
                                now_ts=now_root,
                                sql_db_running_count=_segment_sql_worker_count(root),
                            )
                        if not pending_order:
                            continue

                        parents_by_child = _relation_map(context.get("parents_by_child"))
                        children_by_parent = _relation_map(context.get("children_by_parent"))
                        static_active_ids = set(_normalize_segment_ids(context.get("static_active_ids")))
                        static_state = store.get_runtime_sync(_static_lane_state_key(root)) or {}
                        lanes = static_state.get("lanes") if isinstance(static_state, Mapping) else {}
                        if isinstance(lanes, Mapping):
                            for lane_payload in lanes.values():
                                if not isinstance(lane_payload, Mapping):
                                    continue
                                static_active_ids.update(_normalize_segment_ids(lane_payload.get("claimed")))
                                static_active_ids.update(_normalize_segment_ids(lane_payload.get("running")))
                        active_import_target_ids = set(
                            _normalize_segment_ids(context.get("active_import_target_ids"))
                        )
                        for targets in _import_monitor_target_segment_ids(
                            import_monitor_cache.get(root),
                            lanes={"queued", "running"},
                        ).values():
                            active_import_target_ids.update(targets)
                        cycle_entities = set(
                            _normalize_segment_ids(state.get("items"))
                            + _normalize_segment_ids(state.get("follow_up"))
                        ) - excluded_ids
                        cycle_ids = set(cycle_entities) | excluded_ids
                        running_ids = {
                            int(task.entity_id)
                            for task in running.values()
                            if task.root == root
                            and task.task_type in {"segment", "segment_sql"}
                            and task.entity_id is not None
                        }
                        running_ids.update(static_active_ids)
                        running_ids.update(_segment_sql_worker_ids(root))
                        for candidate_id in cycle_ids | static_active_ids:
                            if any(
                                priority_executor.is_active(root, task_type, int(candidate_id))
                                for task_type in {
                                    "segment",
                                    "segment_priority",
                                    "segment_super_priority",
                                    "segment_recurring_priority",
                                }
                            ):
                                running_ids.add(int(candidate_id))

                        def _claim(sid: int, *, root: str = root, eligible: set[int] = cycle_entities) -> bool:
                            return _segment_cycle_claim(
                                store,
                                root=root,
                                entity_id=int(sid),
                                eligible_ids=eligible,
                                blocked_ids=blocked_ids,
                                running_ids=running_ids,
                                now_ts=time.time(),
                            )

                        def _mark_running(sid: int, *, root: str = root) -> None:
                            _segment_cycle_running(store, root=root, entity_id=int(sid), now_ts=time.time())

                        def _release(sid: int, *, root: str = root) -> None:
                            _segment_cycle_release(store, root=root, entity_id=int(sid), now_ts=time.time())

                        def _dynamic(eid: int, *, root: str = root) -> bool:
                            return _dynamic_blocked(
                                root=root,
                                entity_id=int(eid),
                                parents_by_child=parents_by_child,
                                children_by_parent=children_by_parent,
                                cycle_ids=cycle_ids,
                                static_active_ids=static_active_ids,
                                active_import_target_ids=active_import_target_ids,
                            )

                        def _on_launch(sid: int, *, root: str = root) -> None:
                            segment_full_scan_pending.setdefault(root, set()).discard(int(sid))
                            _monitor_cycle_mark_launched(
                                monitor_cycle_done,
                                root=root,
                                task_type="segment",
                                entity_id=int(sid),
                            )

                        total_limit, launch_budget = _segment_pending_dispatch_limits(
                            config,
                            throttled_active,
                            running,
                            root,
                            segment_slot_limit=segment_slot_limit,
                            priority_pending=priority_pending,
                            store=store,
                            now_ts=now_root,
                            sql_db_running_count=_segment_sql_worker_count(root),
                        )
                        if launch_budget <= 0:
                            continue
                        _fill_from_ring(
                            ring=deque(pending_order),
                            ring_limit=total_limit,
                            total_limit=total_limit,
                            root=root,
                            task_type="segment",
                            running=running,
                            ring_entities=set(pending_order),
                            config=config,
                            store=store,
                            popens=popens,
                            build_args=lambda sid, root=root: render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_segment_update_template,
                                id=sid,
                                batch_limit=config.segment_batch_limit,
                            ),
                            blocked_entities=blocked_ids,
                            dynamic_blocked=_dynamic,
                            admission_claim=_claim,
                            admission_release=_release,
                            admission_running=_mark_running,
                            on_launch=_on_launch,
                            bypass_repeat_guard_entities=set(pending_order),
                            bypass_baseline_admission=True,
                            remove_on_launch=True,
                            max_launches=launch_budget,
                        )
                        refreshed = store.get_runtime_sync(_segment_cycle_key(root)) or state
                        refreshed_state = refreshed if isinstance(refreshed, Mapping) else state
                        refreshed_pending = _segment_cycle_pending_order(
                            refreshed_state,
                            excluded_ids=excluded_ids,
                        )
                        _merge_persisted_segment_monitor_cycle(
                            store=store,
                            root=root,
                            pending_ids=refreshed_pending,
                            done_ids=_normalize_segment_ids(refreshed_state.get("done")),
                            running=running,
                            now_ts=time.time(),
                        )
            except Exception as exc:
                now_error = time.time()
                if now_error - last_error_log >= 30.0:
                    last_error_log = now_error
                    logging.warning("ordinary persisted refill worker failed: %s", exc)
            ordinary_refill_wakeup.wait(timeout=max(0.2, min(1.0, float(config.dispatch_interval_sec or 1))))
            ordinary_refill_wakeup.clear()

    recurring_segment_priority_loop = _RecurringSegmentPriorityLoop(priority_executor)
    priority_trigger_last_checked: dict[str, float] = {}
    priority_trigger_checked_rebuild: dict[str, float] = {}
    priority_campaign_due_ids: dict[str, list[int]] = {}
    priority_campaign_due_scanned_at: dict[str, float] = {}
    priority_campaign_due_error_at: dict[str, float] = {}
    campaign_rebuild_liveness_scanned_at: dict[str, float] = {}
    campaign_rebuild_liveness_error_at: dict[str, float] = {}
    last_campaign_native_fallback_ts: dict[str, float] = {}

    def _priority_trigger_completed(root: str, campaign_id: int, db_cfg: object) -> None:
        _mark_campaign_trigger_finished(root, campaign_id)
        _campaign_email_counter_reconcile(
            db=MauticDB(db_cfg),
            root=root,
            campaign_id=campaign_id,
            reason="priority-trigger-exit",
            force=True,
        )

    def _priority_scheduler() -> None:
        while True:
            cfg = config
            profile = str(getattr(cfg, "profile_name", "") or "").strip().lower()
            if profile == "passive" or not cluster_route_allows(cfg, "cron"):
                time.sleep(1.0)
                continue
            try:
                if Path(cfg.scheduler_pause_flag_path).exists() or (cfg.backup_enabled and backup_lock_active(cfg)):
                    time.sleep(1.0)
                    continue
            except Exception:
                time.sleep(1.0)
                continue

            for inst in list(installs):
                if not getattr(inst, "db", None):
                    continue
                root = str(getattr(inst, "root", "") or "").strip()
                if not root:
                    continue
                segment_realtime = _segment_whitelist_realtime_setting(cfg, inst)
                campaign_realtime = _campaign_whitelist_realtime_setting(cfg, inst)
                due_scan_now = time.time()
                due_scan_interval = max(
                    5,
                    min(30, int(getattr(cfg, "campaign_trigger_audit_interval_sec", 60) or 60)),
                )
                if (
                    due_scan_now - float(priority_campaign_due_scanned_at.get(root, 0.0) or 0.0)
                    >= due_scan_interval
                ):
                    try:
                        due_sql = _campaign_sql_for_major(
                            cfg.sql_campaign_triggers_due,
                            getattr(inst, "mautic_major", None),
                        )
                        due_ids = MauticDB(inst.db).fetch_ids(
                            due_sql,
                            limit=5000,
                            context=campaign_sql_time_context(
                                datetime.now(timezone.utc),
                                getattr(inst, "mautic_timezone", None),
                                getattr(inst, "mautic_major", None),
                            ),
                        )
                        priority_campaign_due_ids[root] = list(dict.fromkeys(due_ids or []))
                    except Exception as exc:
                        priority_campaign_due_ids[root] = []
                        last_error_at = float(priority_campaign_due_error_at.get(root, 0.0) or 0.0)
                        if due_scan_now - last_error_at >= 300.0:
                            priority_campaign_due_error_at[root] = due_scan_now
                            logging.warning("[%s] due campaign priority scan failed: %s", root, exc)
                    priority_campaign_due_scanned_at[root] = due_scan_now
                pending_due_ids = list(priority_campaign_due_ids.get(root, []))
                if pending_due_ids:
                    launched_due_ids = set(
                        _dispatch_due_campaign_triggers(
                            config=cfg,
                            root=root,
                            due_ids=pending_due_ids,
                            running=running,
                            priority_executor=priority_executor,
                            on_success=lambda campaign_id, root=root, db_cfg=inst.db: _priority_trigger_completed(
                                root,
                                campaign_id,
                                db_cfg,
                            ),
                            realtime=campaign_realtime,
                        )
                    )
                    if launched_due_ids:
                        priority_campaign_due_ids[root] = [
                            campaign_id
                            for campaign_id in pending_due_ids
                            if int(campaign_id) not in launched_due_ids
                        ]
                if root not in last_campaign_native_fallback_ts:
                    last_campaign_native_fallback_ts[root] = pusher.campaign_native_fallback_last_run_ts(root)
                fallback_interval = max(300, int(getattr(cfg, "campaign_native_fallback_interval_sec", 3600) or 3600))
                fallback_now = time.time()
                fallback_last = float(last_campaign_native_fallback_ts.get(root, 0.0) or 0.0)
                fallback_scheduled_at = fallback_last + fallback_interval if fallback_last > 0 else fallback_now
                fallback_enabled = bool(getattr(cfg, "campaign_native_fallback_enabled", False))
                if not fallback_enabled:
                    pusher.set_campaign_native_fallback_runtime(root, None)
                fallback_due = (
                    fallback_enabled
                    and fallback_now - fallback_last >= fallback_interval
                )
                fallback_block_reason = ""
                if fallback_due and _running_campaign_total(running, root) > 0:
                    fallback_block_reason = "campaign_worker_active"
                elif fallback_due and priority_executor.has_active_campaign_work(root):
                    fallback_block_reason = "priority_campaign_active"
                elif fallback_due and priority_executor.has_active_task_type("campaign_native_fallback"):
                    fallback_block_reason = "host_fallback_active"
                elif fallback_due and _root_has_live_campaign_process(root, running):
                    fallback_block_reason = "campaign_process_active"
                if fallback_due and not fallback_block_reason:
                    reserved = _campaign_fallback_try_begin(
                        root,
                        campaign_worker_active=_running_campaign_total(running, root) > 0,
                        priority_campaign_active=priority_executor.has_active_campaign_work(root),
                    )
                    if not reserved:
                        fallback_block_reason = "campaign_dispatch_race"
                    elif _root_has_live_campaign_process(root, running):
                        _campaign_fallback_end(root)
                        fallback_block_reason = "campaign_process_active"
                if fallback_due and fallback_block_reason:
                    pusher.set_campaign_native_fallback_runtime(
                        root,
                        {
                            "status": "deferred",
                            "reason": fallback_block_reason,
                            "scheduled_at": datetime.fromtimestamp(
                                fallback_scheduled_at,
                                tz=timezone.utc,
                            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "delay_sec": max(0, int((fallback_now - fallback_scheduled_at) // 60) * 60),
                        },
                    )
                if fallback_due and not fallback_block_reason:
                    metrics_before = _campaign_native_fallback_metrics(MauticDB(inst.db))
                    fallback_started_at = time.time()
                    fallback_schedule_delay_sec = max(0, int(fallback_started_at - fallback_scheduled_at))

                    def _fallback_started(
                        pid: int,
                        *,
                        root: str = root,
                        started_at: float = fallback_started_at,
                        scheduled_at: float = fallback_scheduled_at,
                        delay_sec: int = fallback_schedule_delay_sec,
                    ) -> None:
                        pusher.set_campaign_native_fallback_runtime(
                            root,
                            {
                                "status": "running",
                                "pid": int(pid),
                                "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                ),
                                "scheduled_at": datetime.fromtimestamp(scheduled_at, tz=timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                ),
                                "schedule_delay_sec": delay_sec,
                            },
                        )

                    def _fallback_completed(
                        rc: int | None,
                        *,
                        root: str = root,
                        db_cfg: object = inst.db,
                        metrics_before: dict[str, object] = metrics_before,
                        started_at: float = fallback_started_at,
                        schedule_delay_sec: int = fallback_schedule_delay_sec,
                        previous_run_ts: float = fallback_last,
                        scheduled_at: float = fallback_scheduled_at,
                    ) -> None:
                        try:
                            completed_at = time.time()
                            last_campaign_native_fallback_ts[root] = (
                                _campaign_native_fallback_last_run_after_completion(
                                    previous_run_ts=previous_run_ts,
                                    completed_at=completed_at,
                                    rc=rc,
                                )
                            )
                            if rc == _TASK_LOCK_BUSY_RC:
                                pusher.set_campaign_native_fallback_runtime(
                                    root,
                                    {
                                        "status": "deferred",
                                        "reason": "campaign_root_lock_busy",
                                        "scheduled_at": datetime.fromtimestamp(
                                            scheduled_at,
                                            tz=timezone.utc,
                                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "delay_sec": max(0, int(time.time() - scheduled_at)),
                                    },
                                )
                                return
                            metrics_after = _campaign_native_fallback_metrics(MauticDB(db_cfg))
                            before_error = str(metrics_before.get("error") or "").strip()
                            after_error = str(metrics_after.get("error") or "").strip()
                            metric_error = "; ".join(part for part in (before_error, after_error) if part)[:500]
                            status = "ok" if rc == 0 else "error"
                            failed_stage = ""
                            if rc == 100:
                                failed_stage = "update"
                            elif rc == 101:
                                failed_stage = "trigger"
                            elif rc not in (0, None):
                                failed_stage = "executor"
                            elif rc is None:
                                failed_stage = "timeout_or_exception"
                            event = {
                                "root": root,
                                "status": status,
                                "failed_stage": failed_stage,
                                "operation_rc": rc,
                                "metrics_status": "error" if metric_error else "ok",
                                "metrics_error": metric_error,
                                "pending_before": metrics_before.get("pending_due"),
                                "pending_after": metrics_after.get("pending_due"),
                                "email_stats_before": metrics_before.get("email_stats"),
                                "email_stats_after": metrics_after.get("email_stats"),
                                "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                ),
                                "duration_sec": max(0, int(time.time() - started_at)),
                                "schedule_delay_sec": schedule_delay_sec,
                                "timeout_sec": _CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC,
                            }
                            pusher.add_campaign_native_fallback(event)
                            pusher.set_campaign_native_fallback_runtime(root, None)
                            level = logging.info if rc == 0 else logging.warning
                            level(
                                "[%s] campaign native fallback status=%s stage=%s rc=%s metrics=%s "
                                "pending=%s->%s email_stats=%s->%s",
                                root,
                                status,
                                failed_stage or "complete",
                                rc,
                                "error" if metric_error else "ok",
                                metrics_before.get("pending_due"),
                                metrics_after.get("pending_due"),
                                metrics_before.get("email_stats"),
                                metrics_after.get("email_stats"),
                            )
                        finally:
                            _campaign_fallback_end(root)

                    fallback_started = priority_executor.launch(
                        cfg,
                        root=root,
                        task_type="campaign_native_fallback",
                        entity_id=0,
                        running=running,
                        args=_campaign_native_fallback_args(cfg, root),
                        interval_sec=fallback_interval,
                        max_parallel=1,
                        on_complete=_fallback_completed,
                        on_start=_fallback_started,
                        timeout_sec=_CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC,
                        log_lifecycle=False,
                    )
                    if fallback_started:
                        last_campaign_native_fallback_ts[root] = time.time()
                        # Keep priority work out of this pass. The fallback
                        # was admitted only while this root was idle.
                        continue
                    _campaign_fallback_end(root)
                    pusher.set_campaign_native_fallback_runtime(
                        root,
                        {
                            "status": "deferred",
                            "reason": "launch_guard",
                            "scheduled_at": datetime.fromtimestamp(
                                fallback_scheduled_at,
                                tz=timezone.utc,
                            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "delay_sec": max(0, int((time.time() - fallback_scheduled_at) // 60) * 60),
                        },
                    )
                segment_priority_ids = _segment_priority_effective_setting(cfg, inst)
                segment_super_priority_ids = _segment_super_priority_effective_setting(cfg, inst)
                static_plans = {_SEGMENT_SUPER_PRIORITY_LANE: [], _SEGMENT_PRIORITY_LANE: []}
                static_children: dict[int, set[int]] = {}
                static_parents: dict[int, set[int]] = {}
                if segment_priority_ids or segment_super_priority_ids:
                    try:
                        static_rows = MauticDB(inst.db).fetch_published_segment_filters()
                        static_children, static_parents = segment_dependency_maps(static_rows)
                        static_plans = _build_static_segment_lane_plans(
                            super_priority_ids=segment_super_priority_ids,
                            priority_ids=segment_priority_ids,
                            parents_by_child=static_parents,
                            children_by_parent=static_children,
                            mautic_major=int(getattr(inst, "mautic_major", 0) or 0),
                        )
                    except Exception as exc:
                        logging.warning("[%s] static segment lane planning failed: %s", root, exc)
                static_state_before = store.get_runtime_sync(_static_lane_state_key(root)) or {}
                static_active_evidence = _static_lane_runtime_evidence(
                    store=store,
                    root=root,
                    static_state=static_state_before if isinstance(static_state_before, Mapping) else {},
                    running=running,
                    priority_executor=priority_executor,
                    liveness_probe=_segment_liveness_probe,
                    now_ts=time.time(),
                )
                static_state, static_added, static_started_new = _static_lanes_reconcile(
                    store,
                    root=root,
                    plans=static_plans,
                    now_ts=time.time(),
                    active_evidence=static_active_evidence,
                    orphan_grace_sec=_STATIC_LANE_ORPHAN_GRACE_SEC,
                )
                if any(static_added.values()):
                    logging.info(
                        "[%s] static segment lanes cycle started_new=%s super_added=%s priority_added=%s",
                        root,
                        static_started_new,
                        ",".join(str(x) for x in static_added[_SEGMENT_SUPER_PRIORITY_LANE]) or "-",
                        ",".join(str(x) for x in static_added[_SEGMENT_PRIORITY_LANE]) or "-",
                    )

                def _static_lane_running_ids() -> set[int]:
                    ids = {
                        int(task.entity_id)
                        for task in running.values()
                        if task.root == root
                        and task.entity_id is not None
                        and task.task_type in {"segment", "segment_sql"}
                    }
                    for lane_name in (_SEGMENT_SUPER_PRIORITY_LANE, _SEGMENT_PRIORITY_LANE):
                        lane_payload = (static_state.get("lanes") or {}).get(lane_name, {})
                        ids.update(int(x) for x in lane_payload.get("running", []) if int(x) > 0)
                    ids.update(_segment_sql_worker_ids(root))
                    return ids

                static_pending = {
                    lane_name: _static_lane_pending_items(static_state, lane_name)
                    for lane_name in (_SEGMENT_SUPER_PRIORITY_LANE, _SEGMENT_PRIORITY_LANE)
                }
                static_host_limit = _scheduler_host_lane_limit(cfg, "segment")
                for lane_name, task_type, interval_sec, max_parallel in (
                    (
                        _SEGMENT_SUPER_PRIORITY_LANE,
                        "segment_super_priority",
                        max(1, int(getattr(cfg, "segment_super_priority_interval_sec", 1) or 1)),
                        _segment_static_lane_parallel_limit(cfg, inst, super_priority=True),
                    ),
                    (
                        _SEGMENT_PRIORITY_LANE,
                        "segment_priority",
                        max(1, int(cfg.segment_full_scan_interval_sec or 60)),
                        _segment_static_lane_parallel_limit(cfg, inst, super_priority=False),
                    ),
                ):
                    for segment_id in static_pending[lane_name]:
                        static_chain_ids = [int(segment_id)]
                        for candidate_chain in dependency_execution_chains(
                            list(static_pending[lane_name]),
                            static_parents,
                            static_children,
                            mautic_major=int(getattr(inst, "mautic_major", 0) or 0),
                        ):
                            if int(segment_id) in candidate_chain:
                                static_chain_ids = [int(value) for value in candidate_chain]
                                break
                        if static_chain_ids[0] != int(segment_id):
                            # The topological owner admits the complete
                            # closure; child entries remain durable lane
                            # members but never spawn a second worker.
                            continue
                        static_native_command_ids: list[int] | None = None
                        static_native_excludes: dict[int, list[int]] = {}
                        static_major = int(getattr(inst, "mautic_major", 0) or 0)
                        if static_major >= 7:
                            (
                                static_native_command_ids,
                                static_native_excludes,
                            ) = _segment_native_chain_command_plan(
                                static_chain_ids,
                                static_parents,
                                static_children,
                            )
                            if any(static_native_excludes.values()) and not _segment_native_exclude_capability(
                                store,
                                root=root,
                                config=cfg,
                                mautic_major=static_major,
                            ):
                                logging.warning(
                                    "[%s] static Mautic 7 segment closure waits for native --exclude capability",
                                    root,
                                )
                                continue
                        if (
                            task_type != "segment_super_priority"
                            and
                            static_host_limit > 0
                            and _scheduler_host_running_count(
                                running,
                                "segment",
                                priority_executor=priority_executor,
                            ) >= static_host_limit
                        ):
                            break
                        if task_type != "segment_super_priority":
                            static_host_slots = _scheduler_host_slots_available(
                                cfg,
                                running,
                                task_type,
                                root=root,
                                priority_executor=priority_executor,
                            )
                            if static_host_slots is not None and static_host_slots <= 0:
                                break
                            static_instance_slots = _scheduler_instance_slots_available(
                                cfg,
                                running,
                                root=root,
                                task_type=task_type,
                                priority_executor=priority_executor,
                            )
                            if static_instance_slots is not None and static_instance_slots <= 0:
                                break
                        active_ids = _static_lane_running_ids()
                        if _segment_static_closure_conflict(
                            int(segment_id),
                            active_ids,
                            static_parents,
                            static_children,
                        ):
                            continue
                        if priority_executor.active_count(root, {task_type}) >= max_parallel:
                            break
                        claimed = _static_lane_claim(
                            store,
                            root=root,
                            lane=lane_name,
                            entity_id=int(segment_id),
                            blocked_ids=set(static_chain_ids) & active_ids,
                            globally_running_ids=active_ids,
                            now_ts=time.time(),
                        )
                        claimed_static_ids = [int(segment_id)] if claimed else []
                        if claimed:
                            for chain_id in static_chain_ids[1:]:
                                chain_claimed = _static_lane_claim(
                                    store,
                                    root=root,
                                    lane=lane_name,
                                    entity_id=int(chain_id),
                                    blocked_ids=set(static_chain_ids) & active_ids,
                                    globally_running_ids=active_ids | set(claimed_static_ids),
                                    now_ts=time.time(),
                                )
                                if not chain_claimed:
                                    break
                                claimed_static_ids.append(int(chain_id))
                        if len(claimed_static_ids) != len(static_chain_ids):
                            for chain_id in claimed_static_ids:
                                _static_lane_release(
                                    store,
                                    root=root,
                                    lane=lane_name,
                                    entity_id=int(chain_id),
                                    now_ts=time.time(),
                                )
                            continue

                        static_args = _segment_chain_command_args(
                            lambda sid: render_mautic_command(
                                php_bin=cfg.php_bin,
                                run_as_user=cfg.mautic_run_as_user,
                                root=root,
                                template=cfg.cmd_segment_update_template,
                                id=int(sid),
                                batch_limit=cfg.segment_batch_limit,
                            ),
                            static_chain_ids,
                            command_entity_ids=static_native_command_ids,
                            exclude_ids_by_entity=static_native_excludes,
                        )
                        _segment_chain_runtime_write(
                            store,
                            root=root,
                            owner_id=int(segment_id),
                            entity_ids=static_chain_ids,
                            status="admitting",
                            now_ts=time.time(),
                            command_str=shlex.join(static_args),
                        )
                        static_started_at = time.time()
                        static_timeout_sec = max(
                            0,
                            int(getattr(cfg, "priority_segment_timeout_sec", 3600) or 0),
                        )
                        static_command_str = shlex.join(static_args)

                        def _static_segment_marker_task(
                            pid: int,
                            *,
                            root: str = root,
                            task_type: str = task_type,
                            segment_id: int = int(segment_id),
                            started_at: float = static_started_at,
                            command_str: str = static_command_str,
                            timeout_sec: int = static_timeout_sec,
                        ) -> RunningTask:
                            return RunningTask(
                                row_id=0,
                                root=root,
                                task_key=_task_key(root, task_type, segment_id),
                                task_type=task_type,
                                entity_id=segment_id,
                                command_str=command_str,
                                timeout_sec=timeout_sec,
                                attempts=1,
                                started_at=started_at,
                                pid=int(pid),
                            )

                        def _static_segment_started(
                            pid: int,
                            *,
                            root: str = root,
                            command_str: str = static_command_str,
                            started_at: float = static_started_at,
                            chain_ids: tuple[int, ...] = tuple(static_chain_ids),
                            owner_id: int = int(segment_id),
                        ) -> None:
                            marker_at = max(float(started_at), time.time())
                            for chain_id in chain_ids:
                                task = _static_segment_marker_task(pid, segment_id=int(chain_id))
                                _segment_runtime_marker_write(
                                    store,
                                    task,
                                    status="running",
                                    now_ts=marker_at,
                                    pid=int(pid),
                                    command_str=command_str,
                                )
                            _segment_chain_runtime_write(
                                store,
                                root=root,
                                owner_id=owner_id,
                                entity_ids=chain_ids,
                                status="running",
                                now_ts=marker_at,
                                task=_static_segment_marker_task(pid, command_str=command_str),
                            )

                        def _static_segment_complete(
                            rc: int | None,
                            *,
                            root: str = root,
                            command_str: str = static_command_str,
                            lane_name: str = lane_name,
                            segment_id: int = int(segment_id),
                            chain_ids: tuple[int, ...] = tuple(static_chain_ids),
                        ) -> None:
                            note = None if rc == 0 else (
                                "task_lock_busy" if rc == _TASK_LOCK_BUSY_RC else "static_segment_error"
                            )
                            terminal_at = time.time()
                            for chain_id in chain_ids:
                                _segment_runtime_marker_write(
                                    store,
                                    _static_segment_marker_task(0, segment_id=int(chain_id)),
                                    status="terminal",
                                    now_ts=terminal_at,
                                    note=note,
                                    pid=0,
                                    command_str=command_str,
                                )
                                _static_lane_terminal(
                                    store,
                                    root=root,
                                    lane=lane_name,
                                    entity_id=int(chain_id),
                                    success=bool(rc == 0),
                                    note=note,
                                    now_ts=terminal_at,
                                )
                                _mark_segment_finished(root, int(chain_id), now_ts=terminal_at)
                            _segment_chain_runtime_write(
                                store,
                                root=root,
                                owner_id=segment_id,
                                entity_ids=chain_ids,
                                status="terminal",
                                now_ts=terminal_at,
                                task=_static_segment_marker_task(0, command_str=command_str),
                            )
                            segment_dispatch_wakeup.set()

                        started = priority_executor.launch(
                            cfg,
                            root=root,
                            task_type=task_type,
                            entity_id=int(segment_id),
                            running=running,
                            args=static_args,
                            interval_sec=interval_sec,
                            max_parallel=max_parallel,
                            timeout_sec=static_timeout_sec,
                            on_success=lambda root=root, chain_ids=tuple(static_chain_ids): [
                                _mark_segment_finished(root, int(chain_id)) for chain_id in chain_ids
                            ],
                            on_complete=_static_segment_complete,
                            on_start=_static_segment_started,
                            execution_lock_key=_segment_static_closure_lock_key(
                                root,
                                int(segment_id),
                                static_parents,
                                static_children,
                            ),
                        )
                        if started:
                            running_at = time.time()
                            for chain_id in static_chain_ids:
                                _static_lane_running(
                                    store,
                                    root=root,
                                    lane=lane_name,
                                    entity_id=int(chain_id),
                                    now_ts=running_at,
                                )
                        else:
                            for chain_id in static_chain_ids:
                                _static_lane_release(
                                    store,
                                    root=root,
                                    lane=lane_name,
                                    entity_id=int(chain_id),
                                    now_ts=time.time(),
                                )

                for segment_id in sorted(_priority_realtime_segment_ids(segment_realtime)):
                    priority_executor.launch(
                        cfg,
                        root=root,
                        task_type="segment",
                        entity_id=int(segment_id),
                        running=running,
                        args=render_mautic_command(
                            php_bin=cfg.php_bin,
                            run_as_user=cfg.mautic_run_as_user,
                            root=root,
                            template=cfg.cmd_segment_update_template,
                            id=int(segment_id),
                            batch_limit=cfg.segment_batch_limit,
                        ),
                        interval_sec=segment_realtime.interval_sec,
                        max_parallel=segment_realtime.parallel,
                        timeout_sec=max(
                            0,
                            int(getattr(cfg, "priority_segment_timeout_sec", 3600) or 0),
                        ),
                        on_success=lambda root=root, segment_id=int(segment_id): _mark_segment_finished(
                            root,
                            segment_id,
                        ),
                        capacity_lane="segment_realtime",
                        log_label="realtime",
                    )

                for campaign_id in sorted(_priority_realtime_campaign_rebuild_ids(campaign_realtime)):
                    campaign_id = int(campaign_id)
                    priority_executor.launch(
                        cfg,
                        root=root,
                        task_type="campaign_rebuild",
                        entity_id=campaign_id,
                        running=running,
                        args=render_mautic_command(
                            php_bin=cfg.php_bin,
                            run_as_user=cfg.mautic_run_as_user,
                            root=root,
                            template=cfg.cmd_campaign_rebuild_template,
                            id=campaign_id,
                        ),
                        interval_sec=campaign_realtime.rebuild_interval_sec,
                        max_parallel=campaign_realtime.rebuild_parallel,
                        on_success=lambda root=root, campaign_id=campaign_id: _mark_campaign_rebuild_finished(
                            root,
                            campaign_id,
                        ),
                        capacity_lane="campaign_rebuild_realtime",
                        log_label="realtime",
                    )
                    if priority_executor.is_active(root, "campaign_rebuild", campaign_id):
                        continue
                    trigger_key = _task_key(root, "campaign_trigger", campaign_id)
                    now_priority = time.time()
                    rebuild_started = priority_executor.last_started(root, "campaign_rebuild", campaign_id)
                    rebuild_finished = priority_executor.last_finished(root, "campaign_rebuild", campaign_id)
                    last_checked = float(priority_trigger_last_checked.get(trigger_key, 0.0) or 0.0)
                    checked_rebuild = float(priority_trigger_checked_rebuild.get(trigger_key, 0.0) or 0.0)
                    trigger_is_realtime = (
                        campaign_realtime.trigger_enabled
                        and campaign_id in campaign_realtime.ids
                    )
                    trigger_interval = (
                        campaign_realtime.trigger_interval_sec
                        if trigger_is_realtime
                        else max(
                            1,
                            int(cfg.campaign_trigger_min_repeat_sec or 0),
                            int(cfg.campaign_trigger_audit_interval_sec or 0),
                        )
                    )
                    if not _priority_campaign_due_check_needed(
                        now_ts=now_priority,
                        last_checked_ts=last_checked,
                        rebuild_started_ts=rebuild_started,
                        rebuild_finished_ts=rebuild_finished,
                        checked_rebuild_ts=checked_rebuild,
                        interval_sec=trigger_interval,
                    ):
                        continue
                    try:
                        now_utc_priority = datetime.now(timezone.utc)
                        due = not _campaign_trigger_should_skip_launch(
                            db=MauticDB(inst.db),
                            config=cfg,
                            root=root,
                            campaign_id=campaign_id,
                            sql_ctx=campaign_sql_time_context(
                                now_utc_priority,
                                inst.mautic_timezone,
                                inst.mautic_major,
                            ),
                        )
                    except Exception as exc:
                        logging.warning("[%s] priority campaign due check failed entity=%s: %s", root, campaign_id, exc)
                        due = False
                    priority_trigger_last_checked[trigger_key] = now_priority
                    priority_trigger_checked_rebuild[trigger_key] = rebuild_finished
                    if due:
                        priority_executor.launch(
                            cfg,
                            root=root,
                            task_type="campaign_trigger",
                            entity_id=campaign_id,
                            running=running,
                            args=render_mautic_command(
                                php_bin=cfg.php_bin,
                                run_as_user=cfg.mautic_run_as_user,
                                root=root,
                                template=cfg.cmd_campaign_trigger_template,
                                id=campaign_id,
                                campaign_limit=cfg.campaign_limit,
                                batch_limit=cfg.campaign_batch_limit,
                            ),
                            interval_sec=trigger_interval,
                            max_parallel=(
                                campaign_realtime.trigger_parallel
                                if trigger_is_realtime
                                else max(1, int(cfg.campaign_trigger_priority_parallel or 1))
                            ),
                            on_success=lambda root=root, campaign_id=campaign_id, db_cfg=inst.db: _priority_trigger_completed(
                                root,
                                campaign_id,
                                db_cfg,
                            ),
                            capacity_lane="campaign_trigger_realtime" if trigger_is_realtime else None,
                            log_label="realtime" if trigger_is_realtime else "priority",
                        )
            time.sleep(1.0)

    def _campaign_rebuild_liveness_scheduler() -> None:
        while True:
            cfg = config
            profile = str(getattr(cfg, "profile_name", "") or "").strip().lower()
            if (
                profile == "passive"
                or not bool(getattr(cfg, "enable_campaign_rebuild", False))
                or not cluster_route_allows(cfg, "cron")
            ):
                time.sleep(1.0)
                continue
            try:
                if Path(cfg.scheduler_pause_flag_path).exists() or (cfg.backup_enabled and backup_lock_active(cfg)):
                    time.sleep(1.0)
                    continue
            except Exception:
                time.sleep(1.0)
                continue

            scan_now = time.time()
            scan_interval = max(
                30,
                min(300, int(getattr(cfg, "campaign_rebuild_poll_interval_sec", 300) or 300)),
            )
            for inst in list(installs):
                if not getattr(inst, "db", None):
                    continue
                root = str(getattr(inst, "root", "") or "").strip()
                if not root:
                    continue
                last_scan = float(campaign_rebuild_liveness_scanned_at.get(root, 0.0) or 0.0)
                if scan_now - last_scan < scan_interval:
                    continue
                campaign_rebuild_liveness_scanned_at[root] = scan_now
                scanned_at = datetime.fromtimestamp(scan_now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                pusher.set_campaign_scheduler_liveness_runtime(
                    root,
                    {
                        "schema": 1,
                        "status": "scanning",
                        "lane": "campaign_rebuild_liveness",
                        "scanned_at": scanned_at,
                        "scan_interval_sec": scan_interval,
                    },
                )
                try:
                    due_sql = _campaign_sql_for_major(
                        cfg.sql_campaign_rebuilds_due,
                        getattr(inst, "mautic_major", None),
                    )
                    due_ids = MauticDB(inst.db).fetch_ids(
                        due_sql,
                        limit=5000,
                        context=campaign_sql_time_context(
                            datetime.now(timezone.utc),
                            getattr(inst, "mautic_timezone", None),
                            getattr(inst, "mautic_major", None),
                        ),
                    )
                except Exception as exc:
                    error_text = str(exc)[:500]
                    pusher.set_campaign_scheduler_liveness_runtime(
                        root,
                        {
                            "schema": 1,
                            "status": "error",
                            "lane": "campaign_rebuild_liveness",
                            "scanned_at": scanned_at,
                            "scan_interval_sec": scan_interval,
                            "error": error_text,
                        },
                    )
                    last_error_at = float(campaign_rebuild_liveness_error_at.get(root, 0.0) or 0.0)
                    if scan_now - last_error_at >= 300.0:
                        campaign_rebuild_liveness_error_at[root] = scan_now
                        logging.warning("[%s] campaign rebuild liveness scan failed: %s", root, exc)
                    continue

                planned_ids = list(dict.fromkeys(int(value) for value in (due_ids or []) if int(value) > 0))
                launched_ids = _dispatch_due_campaign_rebuilds(
                    config=cfg,
                    root=root,
                    due_ids=planned_ids,
                    running=running,
                    priority_executor=priority_executor,
                    on_success=lambda campaign_id, root=root: _mark_campaign_rebuild_finished(root, campaign_id),
                )
                pusher.set_campaign_scheduler_liveness_runtime(
                    root,
                    {
                        "schema": 1,
                        "status": "dispatched" if launched_ids else ("pending" if planned_ids else "idle"),
                        "lane": "campaign_rebuild_liveness",
                        "scanned_at": scanned_at,
                        "scan_interval_sec": scan_interval,
                        "pending_count": len(planned_ids),
                        "pending_ids": planned_ids[:50],
                        "launched_count": len(launched_ids),
                        "launched_ids": launched_ids[:50],
                    },
                )
            time.sleep(1.0)

    if not single_cycle:
        threading.Thread(
            target=_active_cycle_observation_worker,
            name="mcd-root-observer",
            daemon=True,
        ).start()
        threading.Thread(
            target=_ordinary_persisted_refill_worker,
            name="mcd-ordinary-persisted-refill",
            daemon=True,
        ).start()
        threading.Thread(
            target=_lifecycle_monitor_scheduler,
            name="mcd-canonical-lifecycle-scheduler",
            daemon=True,
        ).start()
        threading.Thread(target=_priority_scheduler, name="mcd-priority-scheduler", daemon=True).start()
        threading.Thread(
            target=_campaign_rebuild_liveness_scheduler,
            name="mcd-campaign-rebuild-liveness",
            daemon=True,
        ).start()

    while True:
        if single_cycle:
            _monitor_authoritative_runtime()
        now = time.time()
        if now >= next_scheduler_reconcile_at:
            rec = _reconcile_running_state(
                store=store,
                running=running,
                popens=popens,
                segment_liveness_probe=_segment_liveness_probe,
            )
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
                _apply_instance_runtime_guard(installs, reason="inventory-auto-rescan")
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
        pending_local_runtime = None
        if config.mcc_url and config.mcc_token:
            pending_local_runtime = _queue_local_runtime_delta(
                store,
                local_runtime,
                allow_unproven=not startup_runtime_sync_pending,
            )
            if pending_local_runtime is not None:
                local_runtime_push_pending = True
        if config.mcc_url and config.mcc_token and (
            pending_local_runtime is not None or local_fp != last_local_runtime_fp
        ):
            if pending_local_runtime is None:
                previous_local_runtime = store.get_runtime_sync("local_runtime")
                previous_local_runtime = previous_local_runtime if isinstance(previous_local_runtime, dict) else {}
                if local_runtime == previous_local_runtime:
                    last_local_runtime_fp = local_fp
                else:
                    pending_local_runtime = _queue_local_runtime_delta(store, local_runtime, allow_unproven=True)
                    local_runtime_push_pending = pending_local_runtime is not None
            if pending_local_runtime is not None:
                previous_local_runtime = pending_local_runtime["previous"]
                local_runtime_to_push = pending_local_runtime["current"]
                known_state = store.get_runtime_sync("mcc_runtime_desired_state") or {}
                known_revision_raw = known_state.get("revision")
                known_revision = known_revision_raw if isinstance(known_revision_raw, int) else None
                known_instance_revisions_raw = known_state.get("instance_revisions")
                known_instance_revisions = (
                    known_instance_revisions_raw if isinstance(known_instance_revisions_raw, dict) else {}
                )
                push_metadata_raw = store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY)
                push_metadata = _runtime_metadata_for_snapshot(
                    push_metadata_raw,
                    local_runtime_to_push,
                    source="mcd",
                )
                # Local edits are accepted only when based on MCC's current
                # revision. If the revision moved, rebase the local delta on
                # a fresh canonical snapshot instead of polling it over the
                # local edit. The pending payload is durable so a remote poll
                # or daemon restart cannot hide this edit.
                pushed = _push_local_runtime_change(
                    config,
                    previous_local_runtime,
                    local_runtime_to_push,
                    installs,
                    desired_revision=known_revision,
                    instance_revisions=known_instance_revisions,
                    runtime_metadata=push_metadata,
                )
                p_status = str(pushed.get("status", "")).strip().lower()
                accepted = p_status == "ok" and _store_desired_runtime_push_result(
                    store,
                    pushed,
                    accepted_runtime=local_runtime_to_push,
                    runtime_metadata=push_metadata,
                )
                if accepted:
                    after_push_runtime = local_runtime_overrides(config)
                    store.put_runtime_sync("local_runtime", local_runtime_to_push)
                    if after_push_runtime != local_runtime_to_push:
                        # A second edit arrived while the first write was in
                        # flight. Keep the accepted state as the new baseline
                        # and queue only the newer delta.
                        pending_local_runtime = {
                            "previous": deepcopy(local_runtime_to_push),
                            "current": deepcopy(after_push_runtime),
                        }
                        store.put_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY, pending_local_runtime)
                        local_runtime_push_pending = True
                        last_local_runtime_fp = overrides_fingerprint(local_runtime_to_push)
                    else:
                        _clear_local_runtime_push_pending(store)
                        local_runtime_push_pending = False
                        last_local_runtime_fp = overrides_fingerprint(local_runtime_to_push)
                    logging.info(
                        "runtime-overrides local change pushed to MCC (keys=%s)",
                        ",".join(sorted(local_runtime_to_push.keys())) if local_runtime_to_push else "-",
                    )
                    _sync_and_publish_form_embed_status(config, installs, reason="local_runtime_push")
                elif p_status == "conflict":
                    local_runtime_push_pending = True
                    logging.warning(
                        "runtime-overrides local change rebase failed: %s",
                        pushed.get("reason", "unknown"),
                    )
                elif p_status != "disabled":
                    local_runtime_push_pending = True
                    logging.warning("runtime-overrides local push failed: %s", pushed.get("reason", "unknown"))
        elif not (config.mcc_url and config.mcc_token):
            store.put_runtime_sync("local_runtime", local_runtime)
            store.put_runtime_sync(
                _LOCAL_RUNTIME_METADATA_KEY,
                _runtime_metadata_for_snapshot(
                    store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY),
                    local_runtime,
                    source="mcd",
                ),
            )
            _clear_local_runtime_push_pending(store)
            last_local_runtime_fp = local_fp
            local_runtime_push_pending = False

        if consume_poll_trigger(config):
            runtime_overrides_sync_requested = True
            next_runtime_overrides_poll_at = 0.0
            logging.info("runtime-overrides poll trigger consumed: immediate MCC sync requested")

        runtime_poll_enabled = bool(getattr(config, "mcc_runtime_overrides_poll_enabled", False))
        should_runtime_poll = runtime_poll_enabled and now >= next_runtime_overrides_poll_at
        if config.mcc_url and config.mcc_token and not local_runtime_push_pending:
            # Re-sample immediately before the remote fetch. A file edit can
            # arrive after the normal local-change block above; queue it
            # durably before the poll may refresh the baseline.
            pre_poll_runtime = local_runtime_overrides(config)
            if _queue_local_runtime_delta(store, pre_poll_runtime, allow_unproven=False) is not None:
                local_runtime_push_pending = True
        if config.mcc_url and config.mcc_token and (
            (startup_runtime_sync_pending or runtime_overrides_sync_requested or should_runtime_poll)
            and not local_runtime_push_pending
        ):
            ro = fetch_runtime_overrides(
                config,
                instance_uids=[str(getattr(inst, "instance_uid", "") or "") for inst in installs],
            )
            status = str(ro.get("status", "")).strip().lower()
            poll_interval = max(15, min(300, int(config.mcc_push_interval_sec or config.poll_interval_sec or 60)))
            if status == "ok" and not local_runtime_push_pending:
                # Re-sample after the network round trip as well. If the
                # operator edited runtime while MCC was responding, defer
                # applying/persisting that response until the local delta is
                # accepted.
                during_poll_runtime = local_runtime_overrides(config)
                if _queue_local_runtime_delta(store, during_poll_runtime, allow_unproven=False) is not None:
                    local_runtime_push_pending = True
                    status = "deferred_local_delta"
            if status == "ok" and not local_runtime_push_pending:
                # An accepted desired write must not be undone by a stale
                # response from a request that was already in flight. After
                # that guard, resolve every host field and instance field by
                # saved event metadata; a missing key is not a deletion.
                if _remote_runtime_response_is_stale(store, ro):
                    status = "deferred_remote_stale"
                    logging.info("runtime-overrides remote response deferred behind accepted revision")
                else:
                    overrides_raw = ro.get("runtime_overrides")
                    remote_runtime = (
                        merge_instance_desired_states(
                            overrides_raw if isinstance(overrides_raw, dict) else {},
                            ro.get("instance_desired_states"),
                        )
                    )
                    instance_states = ro.get("instance_desired_states")
                    if _accepted_runtime_response_matches(store, ro):
                        accepted_raw = store.get_runtime_sync(_LOCAL_RUNTIME_ACCEPTED_KEY)
                        accepted_runtime = (
                            accepted_raw.get("runtime")
                            if isinstance(accepted_raw, dict) and isinstance(accepted_raw.get("runtime"), dict)
                            else {}
                        )
                        remote_runtime = _merge_missing_runtime_values(remote_runtime, accepted_runtime)
                    current_local_runtime = local_runtime_overrides(config)
                    local_metadata = _runtime_metadata_for_snapshot(
                        store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY),
                        current_local_runtime,
                        source="mcd",
                    )
                    remote_metadata = _runtime_metadata_from_response(
                        ro,
                        remote_runtime,
                        current_local_runtime,
                    )
                    resolution = _resolve_runtime_snapshots(
                        current_local_runtime,
                        remote_runtime,
                        local_metadata,
                        remote_metadata,
                    )
                    if resolution.get("local_push"):
                        baseline = store.get_runtime_sync("local_runtime")
                        baseline = baseline if isinstance(baseline, dict) else {}
                        previous = _runtime_reconciliation_previous(
                            baseline,
                            current_local_runtime,
                            remote_runtime,
                            resolution,
                        )
                        pending = {
                            "previous": previous,
                            "current": deepcopy(current_local_runtime),
                        }
                        store.put_runtime_sync(_LOCAL_RUNTIME_PUSH_PENDING_KEY, pending)
                        store.put_runtime_sync("local_runtime", previous)
                        store.put_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY, local_metadata)
                        local_runtime_push_pending = True
                        status = "deferred_local_delta"
                    else:
                        if _runtime_metadata_has_accepted(store):
                            _clear_local_runtime_accepted(store)
                        ro["_mcd_remote_runtime"] = remote_runtime
                        ro["_mcd_remote_metadata"] = remote_metadata
                        ro["_mcd_resolved_runtime"] = resolution["runtime"]
                        ro["_mcd_resolved_metadata"] = resolution["metadata"]
                        store.put_runtime_sync("mcc_runtime", remote_runtime)
                        store.put_runtime_sync(_MCC_RUNTIME_METADATA_KEY, remote_metadata)
            if status == "ok":
                overrides_raw = ro.get("runtime_overrides")
                instance_states = ro.get("instance_desired_states")
                overrides = ro.get("_mcd_resolved_runtime")
                if not isinstance(overrides, dict):
                    overrides = overrides_raw if isinstance(overrides_raw, dict) else {}
                remote_runtime = ro.get("_mcd_remote_runtime")
                if not isinstance(remote_runtime, dict):
                    remote_runtime = merge_instance_desired_states(
                        overrides_raw if isinstance(overrides_raw, dict) else {},
                        instance_states,
                    )
                resolved_metadata = ro.get("_mcd_resolved_metadata")
                if not isinstance(resolved_metadata, dict):
                    resolved_metadata = store.get_runtime_sync(_MCC_RUNTIME_METADATA_KEY) or {}
                store.put_runtime_sync("mcc_runtime", remote_runtime)
                desired_state = ro.get("desired_state")
                if isinstance(desired_state, dict):
                    revisions: dict[str, int] = {}
                    if isinstance(instance_states, dict):
                        for uid, item in instance_states.items():
                            if isinstance(item, dict) and isinstance(item.get("revision"), int):
                                revisions[str(uid)] = int(item["revision"])
                    state_for_store = dict(desired_state)
                    state_for_store["instance_revisions"] = revisions
                    store.put_runtime_sync("mcc_runtime_desired_state", state_for_store)
                response_profile = canonical_profile_from_runtime_response(ro)
                desired_response_profile = desired_profile_from_runtime_response(ro)
                # An observed passive snapshot can be stale while a selected
                # farm profile is still authoritative. A desired profile,
                # including an intentional passive selection, is authoritative;
                # otherwise only an active observed profile may repair a stale
                # passive fallback.
                if response_profile and (
                    desired_response_profile is not None or response_profile != "passive"
                ):
                    profile_authority = response_profile
                fp = overrides_fingerprint(overrides)
                config_profile = str(getattr(config, "profile_name", "") or "").strip().lower()
                profile_sync_needed = bool(profile_authority and config_profile != profile_authority)
                effective_runtime = local_runtime_overrides(config)
                runtime_apply_needed = effective_runtime != overrides
                if fp != last_runtime_overrides_fp or runtime_apply_needed or profile_sync_needed:
                    # Persist only the LWW-resolved values. An absent key is
                    # removed only when the winning MCC metadata is an
                    # explicit tombstone; an old/partial response cannot
                    # erase a local stable value during startup.
                    if fp != last_runtime_overrides_fp or runtime_apply_needed:
                        _persist_resolved_runtime_to_config(
                            config,
                            local_runtime_overrides(config),
                            overrides,
                            resolved_metadata,
                        )
                    base_config = load_config(config.config_file_path, allow_recover_from_mcc=False)
                    if profile_authority:
                        if str(base_config.profile_name or "").strip().lower() != profile_authority:
                            try:
                                persist_profile_authority(config.config_file_path, profile_authority)
                                base_config = load_config(config.config_file_path, allow_recover_from_mcc=False)
                            except Exception as exc:
                                logging.warning("runtime-overrides profile authority persist failed: %s", exc)
                        base_config = reapply_profile_authority(base_config, profile_authority)
                    applied = apply_remote_overrides(base_config, overrides)
                    next_cfg = applied["config"]
                    if isinstance(next_cfg, AgentConfig) and profile_authority:
                        next_cfg = reapply_profile_authority(next_cfg, profile_authority)
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
                        last_monitored_email_parser_ts.clear()
                        maintenance_cleanup_rr_index.clear()
                        logging.info("runtime-overrides cleanup schedules refreshed for immediate scheduler apply")
                    # Take one last durable snapshot immediately before any
                    # local baseline refresh. This also covers a profile-only
                    # remote apply where the aggregate runtime fingerprint is
                    # unchanged. A file edit after the earlier race check
                    # must never be absorbed into the new baseline.
                    late_local_runtime = local_runtime_overrides(config)
                    if _queue_local_runtime_delta(store, late_local_runtime, allow_unproven=False) is not None:
                        local_runtime_push_pending = True
                    if fp != last_runtime_overrides_fp and not local_runtime_push_pending:
                        _persist_resolved_runtime_to_config(
                            config,
                            local_runtime_overrides(config),
                            overrides,
                            resolved_metadata,
                        )
                    # Persisted MCC values are not a new local operator edit.
                    # Refresh the local fingerprint so the next tick does not
                    # echo the same revision back as another desired write.
                    # When a local delta was queued during this poll, retain
                    # the old baseline; the next loop must push that delta.
                    if local_runtime_push_pending:
                        startup_runtime_sync_pending = True
                        runtime_overrides_sync_requested = True
                        next_runtime_overrides_poll_at = 0.0
                        logging.info("runtime-overrides remote apply deferred behind durable local change")
                    else:
                        current_local_runtime = local_runtime_overrides(config)
                        store.put_runtime_sync("local_runtime", current_local_runtime)
                        store.put_runtime_sync(
                            _LOCAL_RUNTIME_METADATA_KEY,
                            _runtime_metadata_for_snapshot(
                                resolved_metadata,
                                current_local_runtime,
                                source="mcc",
                            ),
                        )
                        last_local_runtime_fp = overrides_fingerprint(current_local_runtime)
                    last_runtime_overrides_fp = fp
                    store.put_runtime_sync(
                        "active_runtime",
                        {
                            "profile": (config.profile_name or "").strip().lower(),
                            "profile_authority": profile_authority,
                            "applied_keys": applied_keys,
                            "blocked_keys": blocked_keys,
                            "unsupported_keys": unsupported_keys,
                        },
                    )
                if not local_runtime_push_pending:
                    # ACK the complete MCC-requested host map, including
                    # operator-owned structured keys. Chunking bounds the
                    # transport; projecting to desired_state.runtime_overrides
                    # here would silently drop keys that MCC still expects.
                    desired_state_runtime = (
                        desired_state.get("runtime_overrides")
                        if isinstance(desired_state, dict)
                        else None
                    )
                    # ACK evidence must describe the canonical desired
                    # envelope; effective values are read back from config.
                    desired_host_runtime = (
                        desired_state_runtime
                        if isinstance(desired_state_runtime, dict)
                        else overrides_raw if isinstance(overrides_raw, dict) else {}
                    )
                    for failure in acknowledge_desired_runtime_states(
                        config,
                        store,
                        desired_state=desired_state,
                        host_runtime=desired_host_runtime,
                        instance_states=instance_states,
                        runtime_fingerprint=fp,
                    ):
                        logging.warning("runtime desired-state acknowledgement failed: %s", failure)
                    startup_runtime_sync_pending = False
                    last_runtime_overrides_error = ""
                    next_runtime_overrides_poll_at = now + poll_interval
            elif status == "disabled":
                startup_runtime_sync_pending = False
                last_runtime_overrides_error = ""
                next_runtime_overrides_poll_at = now + poll_interval
            elif status == "deferred_local_delta":
                # An unrelated local runtime delta (for example a message
                # queue migration retry) must not strand an already accepted
                # canonical segment-lane snapshot. Apply only the accepted
                # lane scopes here; the pending local delta remains queued and
                # continues through its own rebase/ACK path.
                accepted_runtime = _accepted_local_runtime_snapshot(store)
                lane_keys = {
                    "segment_priority_instance_settings",
                    "segment_super_priority_instance_settings",
                }
                accepted_lanes = {
                    key: deepcopy(value)
                    for key, value in accepted_runtime.items()
                    if key in lane_keys
                }
                if accepted_lanes:
                    effective_runtime = local_runtime_overrides(config)
                    lane_runtime = dict(effective_runtime)
                    lane_runtime.update(accepted_lanes)
                    if lane_runtime != effective_runtime:
                        applied = apply_remote_overrides(config, lane_runtime)
                        next_cfg = applied.get("config")
                        if isinstance(next_cfg, AgentConfig):
                            config = next_cfg
                            pusher.cfg = config
                            _persist_resolved_runtime_to_config(
                                config,
                                effective_runtime,
                                lane_runtime,
                                store.get_runtime_sync(_LOCAL_RUNTIME_METADATA_KEY) or {},
                            )
                            logging.info("runtime-overrides accepted segment lanes applied during local-delta retry")
                accepted_desired = store.get_runtime_sync("mcc_runtime_desired_state") or {}
                ack_desired = (
                    accepted_desired
                    if isinstance(accepted_desired, dict)
                    and isinstance(accepted_desired.get("revision"), int)
                    else desired_state if isinstance(desired_state, dict) else {}
                )
                if isinstance(ack_desired, dict) and isinstance(ack_desired.get("revision"), int):
                    accepted_instance_states = ack_desired.get("instance_desired_states")
                    if not isinstance(accepted_instance_states, dict) or any(
                        not isinstance(item, dict) or not isinstance(item.get("runtime_overrides"), dict)
                        for item in accepted_instance_states.values()
                    ):
                        accepted_instance_states = (
                            instance_states if isinstance(instance_states, dict)
                            else instance_desired_states(accepted_runtime, installs)
                        )
                    ack_failures = acknowledge_desired_runtime_states(
                        config,
                        store,
                        desired_state=ack_desired,
                        host_runtime=accepted_runtime,
                        instance_states=accepted_instance_states,
                        runtime_fingerprint=overrides_fingerprint(accepted_runtime),
                    )
                    for failure in ack_failures:
                        logging.warning("runtime desired-state ACK retry failed: %s", failure)
                startup_runtime_sync_pending = True
                runtime_overrides_sync_requested = True
                next_runtime_overrides_poll_at = 0.0
            elif status == "deferred_remote_stale":
                startup_runtime_sync_pending = True
                runtime_overrides_sync_requested = True
                next_runtime_overrides_poll_at = 0.0
            else:
                reason = str(ro.get("reason", "unknown")).strip() or "unknown"
                if reason != last_runtime_overrides_error:
                    logging.warning("runtime-overrides fetch failed: %s", reason)
                last_runtime_overrides_error = reason
                next_runtime_overrides_poll_at = now + max(30, poll_interval)
            if status not in {"deferred_local_delta", "deferred_remote_stale"}:
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
                    components = ["php_fpm", "mysql", "apt", "wazuh", "mautic_db_indexes"]
                for comp in components:
                    if comp not in {"php_fpm", "php-fpm", "mysql", "apt", "wazuh", "mautic_db_indexes", "mautic-db-indexes", "db_indexes", "db-indexes"}:
                        continue
                    # Automatic service-profile runs are advisory only. Writing
                    # host configs or changing packages requires an explicit
                    # operator command through mcd-cli service-profile
                    # apply/rescan.
                    res = service_profiles_apply_once(config, component=comp, dry_run=True)
                    status = str(res.get("status", "")).strip().lower()
                    if status == "ok":
                        applied = res.get("apply")
                        if isinstance(applied, dict):
                            logging.info("service-profile %s advisory check: %s", comp, applied.get("status", "ok"))
                    elif status == "skipped":
                        logging.info("service-profile %s skipped: %s", comp, res.get("reason", "-"))
                    else:
                        logging.warning("service-profile %s advisory check failed: %s", comp, res.get("reason", "unknown"))
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))
            except Exception as e:
                logging.warning("service-profile auto-apply failed: %s", e)
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))

        if now >= next_security_blocklist_sync_at:
            poll_sec = 300
            try:
                security_sync = sync_security_blocklist_once(config)
                applied = security_sync.get("apply")
                if isinstance(applied, dict):
                    poll_sec = max(30, min(3600, int(applied.get("poll_sec") or 60)))
                    if bool(applied.get("changed", False)):
                        logging.warning(
                            "security blocklist reconciled: status=%s desired=%s added=%s removed=%s",
                            applied.get("status", "unknown"),
                            applied.get("desired", 0),
                            len(applied.get("added") or []),
                            len(applied.get("removed") or []),
                        )
                elif str(security_sync.get("status") or "").lower() not in {"ok", "noop", "disabled"}:
                    logging.warning(
                        "security blocklist sync failed: %s",
                        security_sync.get("reason", "unknown"),
                    )
            except Exception as exc:
                logging.warning("security blocklist sync failed: %s", exc)
            next_security_blocklist_sync_at = now + poll_sec

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
                        bool(getattr(config, "backup_cluster_local_xtrabackup_enabled", True))
                        and run_day != last_cluster_full_day
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
                        and _cluster_offsite_not_before_reached(config, dt_local)
                        and (
                            not bool(getattr(config, "backup_cluster_local_xtrabackup_enabled", True))
                            or _cluster_local_full_ready_for_offsite(config, dt_local)
                        )
                        and not _cluster_offsite_done_for_date(config, dt_local)
                    ):
                        cluster_job = "offsite"
                    elif (
                        bool(getattr(config, "backup_cluster_local_xtrabackup_enabled", True))
                        and _cluster_local_full_done_for_date(config, dt_local)
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
                web_observation = collect_web_availability(config, installs)
                if web_observation is not None:
                    signals_payload["web_availability"] = web_observation
                if mail_queue_metrics:
                    details_raw = signals_payload.get("details")
                    details = dict(details_raw) if isinstance(details_raw, dict) else {}
                    details["mail_queue"] = {
                        "instances": {
                            str(root): dict(metrics)
                            for root, metrics in mail_queue_metrics.items()
                            if isinstance(metrics, dict)
                        },
                        "updated_at_ts": float(now),
                    }
                    signals_payload["details"] = details
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

        # Never run heavyweight planning for a root that already has an
        # admitted ordinary cycle.  The dispatch pass below is deliberately
        # allowed to refill that root from TaskStore.runtime_sync while other
        # roots continue their independent discovery/admission work.  This is
        # a per-root terminal gate, not a host-wide "wait for every cycle"
        # barrier.
        active_persisted_cycle_roots = set()
        plan_installs = list(installs)
        if now >= next_plan_refresh_at:
            active_persisted_cycle_roots = _active_persisted_segment_cycle_roots(
                store,
                [str(getattr(inst, "root", "") or "") for inst in installs],
            )
            installs = inventory.list_instances()
            reconcile_import_tag_patch(config, installs)
            _refresh_instance_db_maps(installs, db_configs_by_root, mautic_timezones_by_root)
            _apply_instance_runtime_guard(installs, reason="plan-refresh")
            active_persisted_cycle_roots = _active_persisted_segment_cycle_roots(
                store,
                [str(getattr(inst, "root", "") or "") for inst in installs],
            )
            plan_installs = [
                inst
                for inst in installs
                if str(getattr(inst, "root", "") or "").strip()
                not in active_persisted_cycle_roots
            ]
            if active_persisted_cycle_roots:
                logging.info(
                    "segment plan refresh uses per-root terminal gate active_roots=%s planned_roots=%s",
                    ",".join(sorted(active_persisted_cycle_roots)[:8]),
                    len(plan_installs),
                )
            for inst in plan_installs:
                try:
                    patch_res = ensure_grapesjs_ckeditor_gpl_patch(inst, config)
                    patch_status = str(patch_res.get("status", "")).strip().lower()
                    if patch_status == "patched":
                        logging.info("[%s] Mautic 7 GrapesJS CKEditor GPL patch applied", inst.root)
                    elif patch_status == "error":
                        logging.warning(
                            "[%s] Mautic 7 GrapesJS CKEditor GPL patch error: %s",
                            inst.root,
                            patch_res.get("files", []),
                        )
                except Exception as e:
                    logging.warning("[%s] Mautic 7 GrapesJS CKEditor GPL patch check failed: %s", inst.root, e)
            # Logical validation is inventory health, not scheduler work. Run
            # it for passive hosts too so MCC can report and remediate broken
            # configurations even when native cron owns execution.
            for inst in plan_installs:
                root = str(getattr(inst, "root", "") or "").strip()
                if not root or getattr(inst, "db", None) is None:
                    continue
                try:
                    logical_scan_due = now - float(last_logical_issue_scan_ts.get(root, 0.0)) >= 300.0
                    if logical_scan_due:
                        logical_snapshot = scan_install_logical_issues(config, inst, runtime_store=store)
                        last_logical_issue_scan_ts[root] = now
                    else:
                        logical_snapshot = read_logical_issues_snapshot(
                            config.state_db_path,
                            root,
                            runtime_store=store,
                        )
                    logical_blocked_ids = logical_issue_blocked_segment_ids(logical_snapshot)
                    segment_logical_issue_blocked_sets[root] = logical_blocked_ids
                    segment_logical_data_error_notes[root] = logical_issue_terminal_segment_notes(logical_snapshot)
                    if logical_blocked_ids and logical_scan_due:
                        logging.warning(
                            "[%s] logical issue guard blocked segment ids=%s",
                            root,
                            ",".join(str(x) for x in sorted(logical_blocked_ids)[:100]),
                        )
                except Exception as e:
                    logging.warning("[%s] logical issue scan failed: %s", root, e)
            try:
                removed_logical_snapshots = prune_logical_issue_snapshots(
                    config.state_db_path,
                    [str(getattr(inst, "root", "") or "") for inst in installs],
                    runtime_store=store,
                )
                if removed_logical_snapshots:
                    logging.info("logical issue state pruned stale instances=%s", removed_logical_snapshots)
            except Exception as e:
                logging.warning("logical issue state prune failed: %s", e)
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
                        monitor_cycle_done=monitor_cycle_done,
                    )
                if now >= next_passive_notice_at:
                    logging.info(
                        "Passive profile active: automatic/user-task planning disabled; "
                        "manual user requests rejected; cron remains external"
                    )
                    next_passive_notice_at = now + 60
                # Do not let a low poll_interval spin the passive branch every
                # loop: MCC state push, config guard and self-update live after
                # planning and must get regular cycles too.
                next_plan_refresh_at = now + max(5, config.poll_interval_sec)
                if single_cycle:
                    return
                time.sleep(max(0.1, float(config.dispatch_interval_sec)))
                continue
            # From here through the plan refresh below, a root-local DB/full
            # scan may take minutes.  Let the independent persisted refill
            # worker own the canonical monitor/dispatch tick for admitted
            # roots during that interval.
            planner_busy.set()
            now_utc = datetime.now(timezone.utc)
            now_ts = int(now_utc.timestamp())
            sql_segment_rules_cfg = _parse_sql_segment_rules(getattr(config, "segment_sql_ring_rules", {}))
            if config.segment_sql_ring_enabled and config.segment_mode == "classic_loop" and sql_segment_rules_cfg:
                logging.warning("segment_sql_ring ignored because segment_mode=classic_loop")
            cluster_cron_allowed = cluster_route_allows(config, "cron")
            cluster_import_allowed = cluster_route_allows(config, "import")
            cluster_cache_allowed = cluster_route_allows(config, "cache")
            for inst in plan_installs:
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
                        confirmed_major = confirmed_mautic_major(
                            inst.root,
                            config.php_bin,
                            console_path=inst.console_path,
                            local_php_path=inst.local_php_path,
                            expected_major=6,
                            run_as_user=config.mautic_run_as_user,
                        )
                        if confirmed_major != 6:
                            patch_res = {
                                "status": "skip",
                                "reason": "runtime_major_not_independently_confirmed",
                                "root": inst.root,
                            }
                        else:
                            patch_res = ensure_m6_plugin_update_metadata_patch(inst, config)
                        p_status = str(patch_res.get("status", "")).strip().lower()
                        if p_status == "patched":
                            logging.info("[%s] mautic6 core patch applied: %s", inst.root, patch_res.get("path", "-"))
                        elif p_status == "error":
                            logging.warning("[%s] mautic6 core patch error: %s", inst.root, patch_res.get("reason", "unknown"))
                        elif p_status == "skip":
                            logging.info("[%s] mautic6 core patch skipped: %s", inst.root, patch_res.get("reason", "unknown"))
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
                segment_priority_for_inst = _segment_priority_effective_setting(config, inst)
                segment_super_priority_for_inst = _segment_super_priority_effective_setting(config, inst)
                segment_static_ids_for_inst = (
                    segment_priority_for_inst | segment_super_priority_for_inst
                )
                segment_whitelist_for_inst = segment_priority_for_inst
                campaign_whitelist_for_inst = _campaign_whitelist_effective_setting(config, inst)
                db = MauticDB(inst.db)
                sql_ctx = campaign_sql_time_context(now_utc, inst.mautic_timezone, inst.mautic_major)
                sql_ring_enabled_for_root = bool(
                    config.segment_sql_ring_enabled and config.segment_mode != "classic_loop"
                )
                sql_rules_for_root: dict[int, SQLSegmentRule] = {}

                segment_ids: list[int] | None = None
                campaign_trigger_ids: list[int] | None = None
                campaign_rebuild_ids: list[int] | None = None
                campaign_pressure_at_plan[root] = _campaign_pressure_active(
                    config,
                    running,
                    root,
                    trigger_prio_ring=campaign_trigger_prio_rings.setdefault(root, deque()),
                    trigger_reg_ring=campaign_trigger_reg_rings.setdefault(root, deque()),
                    rebuild_prio_ring=campaign_rebuild_prio_rings.setdefault(root, deque()),
                    rebuild_reg_ring=campaign_rebuild_reg_rings.setdefault(root, deque()),
                    now_ts=now,
                )
                if _running_count(running, root, "import") > 0:
                    last_import_activity_ts[root] = now
                import_settling = _import_in_settle(root, now)
                if import_settling:
                    import_pending_cache[root] = 0
                elif cluster_import_allowed and _import_pending_poll_due(
                    config=config,
                    root=root,
                    now_ts=now,
                    last_poll_ts=last_import_poll_ts,
                    last_activity_ts=last_import_activity_ts,
                    running=running,
                    pending_cache=import_pending_cache,
                ):
                    try:
                        _recover_orphaned_imports_if_safe(db, root, running, grace_sec=max(60, config.import_poll_interval_sec * 4))
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
                if cluster_import_allowed:
                    try:
                        import_monitor_cache[root] = _fetch_import_monitor_snapshot(db, root, sql_ctx)
                    except Exception as e:
                        last_warn = float(last_import_monitor_warn_ts.get(root, 0.0) or 0.0)
                        if now - last_warn >= 300.0:
                            last_import_monitor_warn_ts[root] = now
                            logging.warning("[%s] import monitor query failed: %s", root, e)
                        import_monitor_cache[root] = {}
                else:
                    import_monitor_cache[root] = {}
                import_monitor = import_monitor_cache.get(root, {})
                completed_target_segments = _import_monitor_target_segment_ids(
                    import_monitor,
                    lanes={"done"},
                    successful_only=True,
                )
                completed_imports = import_completed_segment_imports.setdefault(root, {})
                for import_id, targets in completed_target_segments.items():
                    if import_id in completed_imports:
                        continue
                    import_completed_segment_followups.setdefault(root, set()).update(targets)
                    queued_at_by_id = import_completed_segment_followup_queued_at.setdefault(root, {})
                    for segment_id in targets:
                        queued_at_by_id.setdefault(segment_id, now)
                    completed_imports[import_id] = now
                    logging.info(
                        "[%s] import completion queued segment follow-up import=%s segments=%s queue_size=%s",
                        root,
                        import_id,
                        ",".join(str(segment_id) for segment_id in targets),
                        len(import_completed_segment_followups.get(root, set())),
                    )
                for import_id, observed_at in list(completed_imports.items()):
                    if now - float(observed_at) > 3600.0:
                        completed_imports.pop(import_id, None)
                import_pending_now = max(0, int(import_pending_cache.get(root, 0)))
                import_force_until = float(segment_force_full_scan_until.get(root, 0.0))
                import_hold_sec = max(120, int(config.import_poll_interval_sec) * 8)
                if import_pending_now > 0 or import_completed_segment_followups.get(root):
                    import_force_until = max(import_force_until, now + float(import_hold_sec))
                    segment_force_full_scan_until[root] = import_force_until
                force_segment_full_scan = now < import_force_until
                periodic_full_scan_enabled = bool(getattr(config, "segment_periodic_full_scan_enabled", False))
                full_scan_interval_sec = max(0, int(getattr(config, "segment_full_scan_interval_sec", 300) or 0))
                lazy_segment_plan = _segment_lazy_update_effective_setting(config, inst)
                if (
                    periodic_full_scan_enabled
                    and not force_segment_full_scan
                    and full_scan_interval_sec > 0
                    and now - float(segment_last_full_scan_ts.get(root, 0.0)) >= float(full_scan_interval_sec)
                ):
                    force_segment_full_scan = True
                if cluster_cron_allowed:
                    try:
                        if force_segment_full_scan or not lazy_segment_plan:
                            segment_ids = db.fetch_ids(_SQL_SEGMENTS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                            segment_last_full_scan_ts[root] = now
                            logging.info(
                                "[%s] segment full-scan planned (reason=%s pending_imports=%s interval=%ss)",
                                root,
                                "import_activity" if now < import_force_until else (
                                    "full_cycle" if not lazy_segment_plan
                                    else "periodic_full_scan"
                                ),
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
                campaign_trigger_due_ids: list[int] = []
                if cluster_cron_allowed:
                    try:
                        campaign_triggers_due_sql = _campaign_sql_for_major(
                            config.sql_campaign_triggers_due,
                            inst.mautic_major,
                        )
                        campaign_trigger_ids = db.fetch_ids(campaign_triggers_due_sql, limit=5000, context=sql_ctx)
                        campaign_trigger_due_ids = list(dict.fromkeys(campaign_trigger_ids or []))
                        audit_interval = max(0, int(getattr(config, "campaign_trigger_audit_interval_sec", 0) or 0))
                        if audit_interval > 0 and now - float(last_campaign_trigger_audit_ts.get(root, 0.0)) >= float(audit_interval):
                            audit_ids = db.fetch_ids(_SQL_CAMPAIGNS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                            campaign_trigger_audit_ids_by_root[root] = list(dict.fromkeys(audit_ids or []))
                            if audit_ids:
                                before = set(campaign_trigger_ids)
                                campaign_trigger_ids = _merge_campaign_trigger_audit_ids(campaign_trigger_ids, audit_ids)
                                added = sorted(set(campaign_trigger_ids) - before)
                                logging.info(
                                    "[%s] campaign trigger audit planned ids=%s added=%s interval=%ss",
                                    root,
                                    len(audit_ids),
                                    ",".join(str(x) for x in added[:50]) if added else "-",
                                    audit_interval,
                                )
                            last_campaign_trigger_audit_ts[root] = now
                        elif campaign_trigger_audit_ids_by_root.get(root):
                            campaign_trigger_ids = _merge_campaign_trigger_audit_ids(
                                campaign_trigger_ids,
                                campaign_trigger_audit_ids_by_root[root],
                            )
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
                    campaign_trigger_due_ids = []
                    campaign_rebuild_ids = []

                if campaign_trigger_ids is not None:
                    trigger_plan_started_at = campaign_trigger_plan_started_at_by_root.setdefault(root, {})
                    planned_trigger_ids = {int(x) for x in campaign_trigger_ids if int(x) > 0}
                    for stale_id in list(trigger_plan_started_at):
                        if stale_id not in planned_trigger_ids:
                            trigger_plan_started_at.pop(stale_id, None)
                            _CAMPAIGN_REBUILD_FINISHED_AT.pop((str(root), stale_id), None)
                            _CAMPAIGN_TRIGGER_FUTURE_WAKE.pop((str(root), stale_id), None)
                            _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED.pop((str(root), stale_id), None)
                    for cid in planned_trigger_ids:
                        trigger_plan_started_at.setdefault(cid, now)
                    if config.enable_campaign_rebuild and campaign_rebuild_ids is not None:
                        rebuild_before_trigger_ids = [
                            int(cid)
                            for cid in campaign_trigger_ids
                            if _campaign_trigger_waits_for_rebuild(
                                root=root,
                                campaign_id=int(cid),
                                planned_after_ts=trigger_plan_started_at.get(int(cid), now),
                                running=running,
                            )
                        ]
                        if rebuild_before_trigger_ids:
                            campaign_rebuild_ids = _merge_campaign_trigger_audit_ids(
                                campaign_rebuild_ids,
                                rebuild_before_trigger_ids,
                            )

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
                    observed_segment_ids = list(dict.fromkeys(segment_ids))
                    standard_segment_ids = list(observed_segment_ids)
                    published_segment_ids: set[int] | None = None
                    dependency_children: dict[int, set[int]] = {}
                    dependency_parents: dict[int, set[int]] = {}
                    never_built_segment_ids: list[int] = []
                    dep_rows: list[dict[str, object]] = []
                    source_snapshot: Mapping[object, object] | None = None
                    static_excluded_ids = set(segment_static_ids_for_inst)
                    previous_cycle_payload = store.get_runtime_sync(_segment_cycle_key(root)) or {}
                    if not isinstance(previous_cycle_payload, Mapping):
                        previous_cycle_payload = {}
                    previous_cycle_items = set(_normalize_segment_ids(previous_cycle_payload.get("items")))
                    # Bootstrap is fail-open: without a completed baseline,
                    # every observed built candidate is eligible for the
                    # nearest ordinary cycle. The baseline is persisted only
                    # after terminal completion of that cycle.
                    previous_cycle_baseline: Mapping[object, object] = {}
                    previous_cycle_retry_ids: list[object] = []
                    if isinstance(previous_cycle_payload, dict):
                        if previous_cycle_payload.get("baseline_ready"):
                            candidate_baseline = previous_cycle_payload.get("baseline")
                            previous_cycle_baseline = (
                                candidate_baseline
                                if isinstance(candidate_baseline, Mapping)
                                else {}
                            )
                        previous_cycle_retry_ids = list(previous_cycle_payload.get("errors") or [])
                        previous_cycle_retry_ids.extend(list(previous_cycle_payload.get("manual_retry_ids") or []))
                    full_scan_admission = _segment_full_scan_admission_enabled(
                        lazy_segment_plan=lazy_segment_plan,
                        force_segment_full_scan=force_segment_full_scan,
                        cluster_cron_allowed=cluster_cron_allowed,
                    )
                    try:
                        dep_rows = db.fetch_published_segment_filters()
                        published_segment_ids = {int(row["id"]) for row in dep_rows}
                        source_snapshot = _ordinary_segment_source_baseline(dep_rows)
                        never_built_segment_ids = _never_built_segment_ids(dep_rows)
                        dependency_children, dependency_parents = segment_dependency_maps(dep_rows)
                        segment_dependencies_by_root[root] = dependency_children
                        segment_parents_by_root[root] = dependency_parents
                        static_plans = _build_static_segment_lane_plans(
                            super_priority_ids=segment_super_priority_for_inst,
                            priority_ids=segment_priority_for_inst,
                            parents_by_child=dependency_parents,
                            children_by_parent=dependency_children,
                            mautic_major=int(getattr(inst, "mautic_major", 0) or 0),
                        )
                        for lane_ids in static_plans.values():
                            static_excluded_ids.update(int(sid) for sid in lane_ids if int(sid) > 0)
                        plan_groups: dict[str, list[int]] = {}
                        standard_segment_ids = _ordinary_segment_snapshot_order(
                            dep_rows,
                            observed_segment_ids,
                            static_priority_ids=static_excluded_ids,
                            baseline_snapshot=previous_cycle_baseline,
                            retry_ids=previous_cycle_retry_ids,
                            stale_after_sec=int(
                                getattr(config, "segment_stale_after_sec", _SEGMENT_STALE_PRIORITY_SEC)
                                or _SEGMENT_STALE_PRIORITY_SEC
                            ),
                            now_ts=float(now),
                            include_fresh_remaining=not lazy_segment_plan,
                            groups=plan_groups,
                        )
                        if observed_segment_ids and not standard_segment_ids and not lazy_segment_plan:
                            # The due query is already an eligibility decision.
                            # A secondary definition/snapshot read can lag it
                            # (notably for newly created segments) and must not
                            # erase a full-scan plan before it reaches the
                            # durable cycle and scheduler lanes. In lazy mode
                            # an empty plan is intentional and must stay empty.
                            standard_segment_ids = list(observed_segment_ids)
                            plan_groups["important"] = list(standard_segment_ids)
                            plan_groups["remaining"] = []
                            logging.info(
                                "[%s] segment due-plan materialized after empty snapshot filter ids=%s",
                                root,
                                ",".join(str(x) for x in standard_segment_ids[:50]),
                            )
                        if never_built_segment_ids:
                            logging.info(
                                "[%s] never-built segment admission ids=%s",
                                root,
                                ",".join(str(x) for x in never_built_segment_ids[:50]),
                            )
                        if int(getattr(inst, "mautic_major", 0) or 0) >= 7:
                            planned_before = list(standard_segment_ids)
                            standard_segment_ids, suppressed_ids = mautic7_terminal_segment_plan(
                                standard_segment_ids,
                                dependency_children,
                            )
                            if suppressed_ids or standard_segment_ids != planned_before:
                                logging.info(
                                    "[%s] mautic7 segment terminal plan ids=%s covered_internal_ids=%s",
                                    root,
                                    ",".join(str(x) for x in standard_segment_ids[:50]) or "-",
                                    ",".join(str(x) for x in sorted(suppressed_ids)[:50]),
                                )
                        else:
                            planned_before = list(standard_segment_ids)
                            standard_segment_ids = dependency_expanded_segment_plan(
                                standard_segment_ids,
                                dependency_parents,
                            )
                            added_dependencies = sorted(set(standard_segment_ids) - set(planned_before))
                            if added_dependencies:
                                logging.info(
                                    "[%s] legacy segment dependency plan added ids=%s",
                                    root,
                                    ",".join(str(x) for x in added_dependencies[:50]),
                                )
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
                        important_ids = set(_normalize_segment_ids(plan_groups.get("important")))
                        important_ids.update(_normalize_segment_ids(never_built_segment_ids))
                        important_closure = set(important_ids)
                        frontier = list(important_ids)
                        while frontier:
                            child = frontier.pop()
                            for parent in dependency_parents.get(int(child), set()):
                                if int(parent) not in important_closure:
                                    important_closure.add(int(parent))
                                    frontier.append(int(parent))
                    except Exception as e:
                        dependency_children = segment_dependencies_by_root.get(root, {})
                        dependency_parents = segment_parents_by_root.get(root, {})
                        important_ids = set(_normalize_segment_ids(standard_segment_ids))
                        important_closure = set(important_ids)
                        logging.warning("[%s] segment dependency planning failed: %s", root, e)
                    # Static priority/super-priority own their full closure;
                    # ordinary scheduling must not duplicate those entities.
                    standard_segment_ids = [
                        sid for sid in standard_segment_ids if sid not in static_excluded_ids
                    ]
                    important_ids.intersection_update(standard_segment_ids)
                    important_ids.update(
                        int(sid) for sid in standard_segment_ids if int(sid) in important_closure
                    )
                    remaining_ids = set(standard_segment_ids) - important_ids
                    # Full-scan admissions must enter the durable ordinary
                    # cycle before claim/dispatch is built.  Keeping this
                    # reconciliation after cycle sync leaves the ID visible
                    # in the regular ring but absent from cycle.items, so the
                    # claim guard rejects it forever after restart.
                    standard_segment_ids = _reconcile_full_scan_pending(
                        segment_full_scan_pending.setdefault(root, set()),
                        standard_segment_ids,
                        full_scan=full_scan_admission,
                        published_ids=published_segment_ids,
                    )
                    cycle_planned_ids, cycle_never_built_ids = _segment_cycle_admission_inputs(
                        previous_cycle_payload,
                        standard_segment_ids,
                        never_built_segment_ids,
                        pressure=campaign_pressure_at_plan.get(root, False),
                    )
                    if campaign_pressure_at_plan.get(root, False) and (
                        len(cycle_planned_ids) != len(standard_segment_ids)
                        or len(cycle_never_built_ids) != len(never_built_segment_ids)
                    ):
                        logging.info(
                            "[%s] campaign pressure held new ordinary admissions existing_cycle_items=%s planned=%s",
                            root,
                            len(previous_cycle_items),
                            len(cycle_planned_ids),
                        )
                    cycle_state, cycle_added_ids, cycle_started_new = _segment_cycle_sync(
                        store,
                        root=root,
                        planned_ids=cycle_planned_ids,
                        never_built_ids=cycle_never_built_ids,
                        running_ids=[
                            int(task.entity_id)
                            for task in running.values()
                            if task.root == root and task.task_type == "segment" and task.entity_id is not None
                        ]
                        + [
                            int(segment_id)
                            for segment_id in standard_segment_ids
                            if priority_executor.is_active(root, "segment", int(segment_id))
                        ],
                        excluded_ids=static_excluded_ids,
                        now_ts=float(now),
                        source_snapshot=source_snapshot,
                        important_ids=[sid for sid in cycle_planned_ids if sid in important_ids]
                        + [sid for sid in cycle_never_built_ids if sid in important_ids],
                        remaining_ids=[sid for sid in cycle_planned_ids if sid in remaining_ids],
                        lazy_mode=lazy_segment_plan,
                    )
                    # Every unfinished item in the durable current cycle is
                    # already admitted work. The regular ring is rebuilt from
                    # this persisted projection only; a candidate held by
                    # pressure or a failed plan must not become an orphan ring
                    # entry that can never pass the durable claim guard.
                    if cycle_added_ids:
                        logging.info(
                            "[%s] segment cycle admission cycle_id=%s started_new=%s added=%s",
                            root,
                            cycle_state.get("cycle_id", "-"),
                            cycle_started_new,
                            ",".join(str(x) for x in cycle_added_ids[:50]),
                        )
                    standard_segment_ids = _segment_cycle_pending_order(
                        cycle_state,
                        excluded_ids=static_excluded_ids,
                    )
                    segment_definition_rows: list[dict[str, object]] = []
                    filter_hint_ids: set[int] = set()
                    if standard_segment_ids:
                        try:
                            segment_definition_rows = db.fetch_segment_definitions(standard_segment_ids)
                            invalid_issues = segment_invalid_filter_issues(segment_definition_rows)
                            filter_hint_ids = set(invalid_issues)
                            if filter_hint_ids:
                                _log_invalid_segment_filters(
                                    root,
                                    format_segment_filter_issues(invalid_issues),
                                )
                        except Exception as e:
                            logging.warning("[%s] segment filter hint inspection failed: %s", root, e)
                    failure_blocked_ids = _segment_failure_blocked_ids(store, root)
                    for sid in sorted(set(standard_segment_ids) & failure_blocked_ids)[:20]:
                        _log_segment_failure_cooldown(root, sid)
                    segment_failure_blocked_sets[root] = failure_blocked_ids
                    auto_sql_rules_for_root: dict[int, SQLSegmentRule] = {}
                    auto_sql_long_rules_for_root: dict[int, SQLSegmentRule] = {}
                    auto_sql_long_scores_for_root: dict[int, tuple[int, float]] = {}
                    if sql_ring_enabled_for_root and config.segment_sql_auto_enabled:
                        try:
                            recent_problem_counts = store.recent_task_problem_counts(
                                root=root,
                                task_type="segment",
                                since_sec=max(6 * 3600, int(config.tasks_history_keep_days or 1) * 86400),
                            )
                            recent_long_duration_stats: dict[int, dict[str, object]] = {}
                            if bool(getattr(config, "segment_sql_auto_long_native_enabled", True)):
                                recent_long_duration_stats = store.recent_task_duration_stats(
                                    root=root,
                                    task_type="segment",
                                    since_sec=max(
                                        int(getattr(config, "segment_sql_auto_long_native_history_sec", 7 * 86400) or 0),
                                        int(config.tasks_history_keep_days or 1) * 86400,
                                    ),
                                    min_duration_sec=int(
                                        getattr(config, "segment_sql_auto_long_native_min_duration_sec", 600) or 600
                                    ),
                                )
                            auto_candidate_ids = list(
                                dict.fromkeys(
                                    [
                                        *[int(x) for x in standard_segment_ids if int(x) > 0],
                                        *[int(x) for x in recent_problem_counts if int(x) > 0],
                                        *[int(x) for x in recent_long_duration_stats if int(x) > 0],
                                    ]
                                )
                            )
                            segment_rows = db.fetch_segment_definitions(auto_candidate_ids) if auto_candidate_ids else []
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
                                long_native_stats=recent_long_duration_stats,
                                long_native_min_duration_sec=int(
                                    getattr(config, "segment_sql_auto_long_native_min_duration_sec", 600) or 600
                                ),
                                long_native_min_successes=int(
                                    getattr(config, "segment_sql_auto_long_native_min_successes", 1) or 1
                                ),
                            )
                            auto_sql_long_rules_for_root = {
                                sid: SQLSegmentRule(
                                    segment_id=rule.segment_id,
                                    select_sql=rule.select_sql,
                                    depends_on=rule.depends_on,
                                )
                                for sid, rule in detected_rules.items()
                                if float(rule.long_native_duration_sec or 0.0) > 0.0
                            }
                            auto_sql_long_scores_for_root = {
                                sid: (int(rule.problem_count or 0), float(rule.long_native_duration_sec or 0.0))
                                for sid, rule in detected_rules.items()
                                if sid in auto_sql_long_rules_for_root
                            }
                            auto_sql_rules_for_root = {
                                sid: SQLSegmentRule(
                                    segment_id=rule.segment_id,
                                    select_sql=rule.select_sql,
                                    depends_on=rule.depends_on,
                                )
                                for sid, rule in detected_rules.items()
                                if sid not in auto_sql_long_rules_for_root
                            }
                            auto_sig = tuple(sorted(detected_rules))
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
                        sql_long_rules_for_root = dict(auto_sql_long_rules_for_root)
                        sql_rules_for_root = dict(auto_sql_rules_for_root)
                        sql_rules_for_root.update(dict(sql_segment_rules_cfg))
                        long_sql_ids = set(sql_long_rules_for_root)
                        sql_rules_for_root = {
                            sid: rule for sid, rule in sql_rules_for_root.items() if sid not in long_sql_ids
                        }
                        all_sql_rules_for_root = dict(sql_long_rules_for_root)
                        all_sql_rules_for_root.update(dict(sql_rules_for_root))
                        if dependency_parents:
                            for sid, rule in list(all_sql_rules_for_root.items()):
                                deps = tuple(sorted(set(rule.depends_on) | set(dependency_parents.get(int(sid), set()))))
                                if deps != rule.depends_on:
                                    all_sql_rules_for_root[sid] = SQLSegmentRule(
                                        segment_id=rule.segment_id,
                                        select_sql=rule.select_sql,
                                        depends_on=deps,
                                    )
                            sql_long_rules_for_root = {
                                sid: rule for sid, rule in all_sql_rules_for_root.items() if sid in long_sql_ids
                            }
                            sql_rules_for_root = {
                                sid: rule for sid, rule in all_sql_rules_for_root.items() if sid not in long_sql_ids
                            }
                    if sql_ring_enabled_for_root and (sql_long_rules_for_root or sql_rules_for_root):
                        sql_long_priority_ids = sorted(
                            sql_long_rules_for_root,
                            key=lambda sid: (
                                -int(auto_sql_long_scores_for_root.get(sid, (0, 0.0))[0]),
                                -float(auto_sql_long_scores_for_root.get(sid, (0, 0.0))[1]),
                                int(sid),
                            ),
                        )
                        sql_long_ring_plan = _plan_sql_segment_ring(
                            list(dict.fromkeys([*sql_long_priority_ids, *standard_segment_ids])),
                            sql_long_rules_for_root,
                        )
                        active_long_sql_set = set(sql_long_ring_plan)
                        sql_ring_plan = _plan_sql_segment_ring(
                            [sid for sid in standard_segment_ids if sid not in active_long_sql_set],
                            sql_rules_for_root,
                        )
                        active_sql_set = active_long_sql_set | set(sql_ring_plan)
                        # Long SQL auto-promotion is priority-based: segments
                        # with recent scheduler failures must move ahead of an
                        # older slow-only backlog. Rebuild this deque in plan
                        # order instead of preserving historical ring order.
                        segment_sql_long_rings[root] = deque(sql_long_ring_plan)
                        segment_sql_rings[root] = _reconcile_ring(
                            segment_sql_rings.get(root),
                            sql_ring_plan,
                            new_to_front=True,
                        )
                        segment_sql_long_rules_by_root[root] = sql_long_rules_for_root
                        segment_sql_rules_by_root[root] = sql_rules_for_root
                        segment_sql_long_active_sets[root] = active_long_sql_set
                        segment_sql_active_sets[root] = active_sql_set
                        prev_long_done = set(segment_sql_long_done_sets.get(root, set()))
                        segment_sql_long_done_sets[root] = {sid for sid in prev_long_done if sid in active_long_sql_set}
                        prev_done = set(segment_sql_done_sets.get(root, set()))
                        segment_sql_done_sets[root] = {sid for sid in prev_done if sid in active_sql_set}
                        standard_segment_ids = [sid for sid in standard_segment_ids if sid not in active_sql_set]
                    else:
                        segment_sql_long_rings[root] = deque()
                        segment_sql_long_rules_by_root[root] = {}
                        segment_sql_long_active_sets[root] = set()
                        segment_sql_long_done_sets[root] = set()
                        segment_sql_rings[root] = deque()
                        segment_sql_rules_by_root[root] = {}
                        segment_sql_active_sets[root] = set()
                        segment_sql_done_sets[root] = set()

                    # Ordinary never has a computed priority lane. Static
                    # priority/super-priority are dispatched by the persisted
                    # MCC lanes above and were removed from this snapshot.
                    store.put_weights("segment", root, {})
                    seg_prio, seg_reg = [], list(dict.fromkeys(standard_segment_ids))
                    if not _partition_complete(standard_segment_ids, seg_prio, seg_reg):
                        logging.warning("[%s] invalid segment partition, forcing single ring", root)
                        seg_prio, seg_reg = [], list(dict.fromkeys(standard_segment_ids))
                    segment_prio_rings[root] = _reconcile_ring(
                        segment_prio_rings.get(root),
                        seg_prio,
                    )
                    segment_reg_rings[root] = _reconcile_ring(
                        segment_reg_rings.get(root),
                        seg_reg,
                    )
                    segment_prio_sets[root] = set(seg_prio)
                    segment_reg_sets[root] = set(seg_reg)
                    if force_segment_full_scan or segment_full_scan_pending.get(root):
                        logging.info(
                            "[%s] segment plan fetched=%s pending_full_scan=%s priority=%s regular=%s "
                            "invalid=%s logical_blocked=%s",
                            root,
                            segment_ids[:100],
                            sorted(segment_full_scan_pending.get(root, set()))[:100],
                            list(segment_prio_rings[root])[:100],
                            list(segment_reg_rings[root])[:100],
                            sorted(filter_hint_ids)[:100],
                            sorted(segment_logical_issue_blocked_sets.get(root, set()))[:100],
                        )
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
                        camp_w = _campaign_weights(
                            campaign_all_ids,
                            campaign_weight_rows,
                            campaign_whitelist_for_inst,
                            now_ts,
                        )
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
                        campaign_whitelist_for_inst,
                        config.campaign_priority_size,
                        latest_priority_ids,
                    )
                    trg_prio, trg_reg = _force_due_campaigns_to_priority(
                        trg_prio,
                        trg_reg,
                        campaign_trigger_due_ids,
                    )
                    reb_prio, reb_reg = _split_campaign_circles(
                        campaign_rebuild_ids,
                        camp_w,
                        campaign_whitelist_for_inst,
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
                    (
                        campaign_trigger_prio_rings[root],
                        campaign_trigger_reg_rings[root],
                    ) = _reconcile_campaign_rings(
                        campaign_trigger_prio_rings.get(root),
                        campaign_trigger_reg_rings.get(root),
                        trg_prio,
                        trg_reg,
                    )
                    _move_ring_entities_to_front(
                        campaign_trigger_prio_rings[root],
                        campaign_trigger_due_ids,
                    )
                    (
                        campaign_rebuild_prio_rings[root],
                        campaign_rebuild_reg_rings[root],
                    ) = _reconcile_campaign_rings(
                        campaign_rebuild_prio_rings.get(root),
                        campaign_rebuild_reg_rings.get(root),
                        reb_prio,
                        reb_reg,
                    )
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

                previous_metrics = mail_queue_metrics.get(root)
                current_metrics = dict(previous_metrics) if isinstance(previous_metrics, dict) else {}
                try:
                    queue_total = db.fetch_count(_MAIL_QUEUE_TOTAL_COUNT, context=sql_ctx)
                    queue_available = db.fetch_count(_MAIL_QUEUE_AVAILABLE_COUNT, context=sql_ctx)
                    current_metrics.update(
                        {
                            "total": max(0, int(queue_total)),
                            "available": max(0, int(queue_available)),
                            "collected_at_ts": float(now),
                        }
                    )
                    current_metrics.pop("error", None)
                except Exception as e:
                    logging.warning("[%s] mail queue visibility query failed: %s", root, e)
                    current_metrics["error"] = str(e)[:300]
                    current_metrics["collected_at_ts"] = float(now)
                current_metrics["postmaster"] = _mailru_postmaster_metrics(db, now_ts=now)
                mail_queue_metrics[root] = current_metrics

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
            planner_busy.clear()

        if config.fs_permissions_guard_enabled:
            guard_interval_sec = max(15, int(config.fs_permissions_guard_interval_sec or 300))
            for inst in plan_installs:
                root = str(inst.root or "").strip()
                if not root:
                    continue
                last_ts = float(last_fs_permissions_guard_ts.get(root, 0.0))
                if last_ts > 0 and now - last_ts < guard_interval_sec:
                    continue
                try:
                    permission_identity = effective_guard_identity(
                        runtime=inst.runtime,
                        runtime_user=inst.runtime_user,
                        host_user=config.mautic_run_as_user,
                    )
                    res = ensure_instance_permissions(
                        root=root,
                        run_as_user=permission_identity,
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
            previous_roots = {str(item.root) for item in installs}
            installs, instances_snapshot_complete = _inventory_snapshot_for_push(inventory, installs)
            current_roots = {str(item.root) for item in installs}
            if instances_snapshot_complete and current_roots != previous_roots:
                logging.info(
                    "inventory refreshed before MCC state push: before=%s after=%s",
                    len(previous_roots),
                    len(current_roots),
                )
                _refresh_instance_db_maps(installs, db_configs_by_root, mautic_timezones_by_root)
            pending_profile_event = read_pending_profile_event(config)
            payload = pusher.build_payload(
                installs=installs,
                profile_name=config.profile_name,
                now_ts=now,
                profile_event=pending_profile_event,
                instances_snapshot_complete=instances_snapshot_complete,
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
                        profile_authority = str(config.profile_name or "").strip().lower()
                        if desired_profile and desired_profile != old_profile:
                            write_profile_selection(
                                "/opt/mcd",
                                mode="manual",
                                profile=(config.profile_name or desired_profile),
                            )
                        base_config = config
                        last_local_runtime_fp = _sync_local_runtime_baseline_after_config_recovery(config, store)
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

        if now >= next_managed_cron_reconcile_at:
            try:
                mcron = reconcile_managed_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                    installs=installs,
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                )
                changed = [line for line in mcron.lines if "commented managed cron" in line]
                if changed:
                    logging.info("managed cron reconcile: %s", "; ".join(changed))
                elif not mcron.ok:
                    logging.warning("managed cron reconcile failed: %s", "; ".join(mcron.lines))
            except Exception as e:
                logging.warning("managed cron reconcile failed: %s", e)
            next_managed_cron_reconcile_at = now + 60

        if now >= next_plugin_operation_cron_reconcile_at:
            try:
                plugin_cron = reconcile_plugin_operation_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                    rules=plugin_operation_legacy_cron_rules(config, installs),
                )
                changed = [
                    line
                    for line in plugin_cron.lines
                    if "catalog plugin cron reconciled" in line
                ]
                if changed:
                    logging.info("plugin operation cron reconcile: %s", "; ".join(changed))
                current_settings = getattr(config, "plugin_operation_instance_settings", {})
                next_settings = dict(current_settings) if isinstance(current_settings, dict) else {}
                changed_settings = False
                operation_instance_keys: dict[tuple[str, str], list[str]] = {}

                def _has_canonical_operation(keys: list[str], operation_key: str) -> bool:
                    for key in [*keys, "default"]:
                        block = next_settings.get(key)
                        if isinstance(block, dict) and isinstance(block.get(operation_key), dict):
                            return True
                    return False

                for inst in installs:
                    instance_keys = plugin_operation_instance_runtime_keys(inst)
                    if not instance_keys:
                        continue
                    instance_key = instance_keys[0]
                    for plugin_item in plugin_operations_for_instance(config, inst):
                        operation_key = str(plugin_item.get("operation_key", "") or "")
                        if not operation_key:
                            continue
                        operation_instance_keys[(instance_key, operation_key)] = instance_keys
                        legacy_values = plugin_item.get("legacy_values")
                        if not isinstance(legacy_values, dict) or not legacy_values:
                            continue
                        if _has_canonical_operation(instance_keys, operation_key):
                            continue
                        operation = (
                            plugin_item.get("operation")
                            if isinstance(plugin_item.get("operation"), dict)
                            else {}
                        )
                        values = {
                            str(field.get("id")): field.get("default")
                            for field in list(operation.get("fields") or [])
                            if isinstance(field, dict) and str(field.get("id", "") or "")
                        }
                        for field_id in list(values):
                            if field_id in legacy_values:
                                values[field_id] = legacy_values[field_id]
                        block = (
                            dict(next_settings.get(instance_key) or {})
                            if isinstance(next_settings.get(instance_key), dict)
                            else {}
                        )
                        block[operation_key] = values
                        next_settings[instance_key] = block
                        changed_settings = True

                for line in plugin_cron.lines:
                    prefix = "MCD_PLUGIN_OPERATION_MIGRATE_JSON="
                    if not str(line).startswith(prefix):
                        continue
                    try:
                        migrated = json.loads(str(line)[len(prefix):])
                    except Exception:
                        continue
                    instance_key = str(migrated.get("instance_key", "") or "")
                    operation_key = str(migrated.get("operation_key", "") or "")
                    interval_sec = int(migrated.get("interval_sec", 0) or 0)
                    enabled_field = str(migrated.get("enabled_field", "enabled") or "enabled")
                    interval_field = str(migrated.get("interval_field", "interval_sec") or "interval_sec")
                    if instance_key and operation_key and bool(migrated.get("cron_found", False)):
                        instance_keys = operation_instance_keys.get((instance_key, operation_key), [instance_key])
                        if _has_canonical_operation(instance_keys, operation_key):
                            continue
                        migrated_values: dict[str, object] = {enabled_field: True}
                        if interval_sec > 0:
                            migrated_values[interval_field] = max(60, interval_sec)
                        block = (
                            dict(next_settings.get(instance_key) or {})
                            if isinstance(next_settings.get(instance_key), dict)
                            else {}
                        )
                        block[operation_key] = migrated_values
                        next_settings[instance_key] = block
                        changed_settings = True
                if changed_settings:
                    pushed = push_runtime_overrides(
                        config,
                        {"plugin_operation_instance_settings": next_settings},
                        merge=True,
                        target="desired",
                    )
                    if str(pushed.get("status", "")).lower() == "ok":
                        config.plugin_operation_instance_settings = next_settings
                    else:
                        logging.warning("plugin operation settings migration push failed: %s", pushed.get("reason", "unknown"))
                if not plugin_cron.ok:
                    logging.warning("plugin operation cron reconcile failed: %s", "; ".join(plugin_cron.lines))
            except Exception as e:
                logging.warning("plugin operation cron reconcile failed: %s", e)
            next_plugin_operation_cron_reconcile_at = now + 60

        if now >= next_message_queue_cron_reconcile_at:
            try:
                supported_installs = [inst for inst in installs if supports_message_queue(inst)]
                queue_cron = reconcile_mautic_message_queue_cron(
                    profile_name=(config.profile_name or ""),
                    install_dir="/opt/mcd",
                    managed_roots=[inst.root for inst in supported_installs],
                )
                changed = [
                    line
                    for line in queue_cron.lines
                    if "mautic message queue cron lines" in line
                ]
                if changed:
                    logging.info("mautic message queue cron reconcile: %s", "; ".join(changed))

                cron_migrations: dict[str, int] = {}
                migration_prefix = "MCD_MESSAGE_QUEUE_MIGRATE_JSON="
                for line in queue_cron.lines:
                    if not str(line).startswith(migration_prefix):
                        continue
                    try:
                        migrated = json.loads(str(line)[len(migration_prefix):])
                    except Exception:
                        continue
                    root_key = str(migrated.get("root", "") or "").strip()
                    if not root_key or not bool(migrated.get("enabled", False)):
                        continue
                    cron_migrations[root_key] = normalize_message_queue_interval(
                        migrated.get("interval_sec", MESSAGE_QUEUE_DEFAULT_INTERVAL_SEC)
                    )

                next_settings, added = _adopt_message_queue_settings(
                    config,
                    supported_installs,
                    cron_migrations,
                )

                settings_to_push = message_queue_settings_push_pending
                if added:
                    # Keep cron/legacy behavior continuous even when MCC is
                    # temporarily unreachable; marker state makes this
                    # adoption repeatable after a daemon restart.
                    config = _with_message_queue_instance_settings(config, next_settings)
                    settings_to_push = next_settings
                if settings_to_push is not None:
                    pushed = push_runtime_overrides(
                        config,
                        {"message_queue_instance_settings": settings_to_push},
                        merge=True,
                        target="desired",
                    )
                    if str(pushed.get("status", "")).lower() == "ok":
                        config = _with_message_queue_instance_settings(config, settings_to_push)
                        message_queue_settings_push_pending = None
                        if added:
                            logging.info(
                                "mautic message queue settings adopted: %s",
                                ", ".join(
                                    f"{key}=enabled:{str(enabled).lower()}/interval:{interval_sec}/source:{source}"
                                    for key, enabled, interval_sec, source in added
                                ),
                            )
                        else:
                            logging.info("mautic message queue pending settings synchronized to MCC")
                    else:
                        config = _with_message_queue_instance_settings(config, settings_to_push)
                        message_queue_settings_push_pending = settings_to_push
                        logging.warning(
                            "mautic message queue settings migration push failed; local behavior retained: %s",
                            pushed.get("reason", "unknown"),
                        )
                if not queue_cron.ok:
                    logging.warning(
                        "mautic message queue cron reconcile failed: %s",
                        "; ".join(queue_cron.lines),
                    )
            except Exception as e:
                logging.warning("mautic message queue cron reconcile failed: %s", e)
            next_message_queue_cron_reconcile_at = now + 60

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
                    monitor_cycle_done=monitor_cycle_done,
                )
            if now >= next_passive_notice_at:
                logging.info(
                    "Passive profile active: automatic/user-task planning disabled; "
                    "manual user requests rejected; cron remains external"
                )
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

        priority_pending_roots = {
            root
            for root in {str(getattr(inst, "root", "") or "").strip() for inst in installs}
            if root
            and (
                max(0, int(import_pending_cache.get(root, 0) or 0)) > 0
                or bool(campaign_trigger_prio_rings.get(root))
                or bool(campaign_trigger_reg_rings.get(root))
                or bool(campaign_rebuild_prio_rings.get(root))
                or bool(campaign_rebuild_reg_rings.get(root))
            )
        }
        pending_roots = set(priority_pending_roots)
        pending_roots.update(
            root
            for root in {str(getattr(inst, "root", "") or "").strip() for inst in installs}
            if root
            and (
                bool(segment_sql_long_rings.get(root))
                or bool(segment_sql_rings.get(root))
                or bool(segment_resume_rings.get(root))
                or bool(segment_prio_rings.get(root))
                or bool(segment_reg_rings.get(root))
            )
        )
        _set_scheduler_priority_pending_roots(priority_pending_roots)
        pending_import_roots = {
            str(getattr(inst, "root", "") or "").strip()
            for inst in installs
            if str(getattr(inst, "root", "") or "").strip()
            and max(0, int(import_pending_cache.get(str(getattr(inst, "root", "") or "").strip(), 0) or 0)) > 0
        }
        import_pending_order[:] = [root for root in import_pending_order if root in pending_import_roots]
        for inst in installs:
            root = str(getattr(inst, "root", "") or "").strip()
            if root and root in pending_import_roots and root not in import_pending_order:
                import_pending_order.append(root)
        dispatch_installs = _pending_import_first_dispatch_installs(
            _priority_interleaved_dispatch_installs(
                _rotated_dispatch_installs(installs, dispatch_instance_cursor),
                config,
            ),
            import_pending_cache,
            import_pending_order,
        )
        for progressed_root in _take_scheduler_fairness_claimed_roots():
            scheduler_pending_since_by_root.pop(progressed_root, None)
        dispatch_installs, promoted_roots = _fairness_watchdog_dispatch_installs(
            dispatch_installs,
            pending_roots=pending_roots,
            pending_since_by_root=scheduler_pending_since_by_root,
            now_ts=now,
            watchdog_sec=int(getattr(config, "scheduler_fairness_watchdog_sec", 300) or 300),
        )
        shared_ready_roots = [
            str(getattr(inst, "root", "") or "").strip()
            for inst in dispatch_installs
            if str(getattr(inst, "root", "") or "").strip() in pending_roots
        ]
        shared_worker_slots = max(
            1,
            int(getattr(config, "scheduler_host_max_parallel", 0) or 0),
            int(getattr(config, "jobs_max_workers", 0) or 0),
        )
        persisted_fairness = store.get_runtime_sync("scheduler_fairness_state") or {}
        persisted_distribution_cursor = None
        if isinstance(persisted_fairness, Mapping):
            try:
                persisted_distribution_cursor = int(
                    persisted_fairness.get("shared_distribution_cursor")
                )
            except (TypeError, ValueError):
                persisted_distribution_cursor = None
        _set_scheduler_distribution_roots(
            shared_ready_roots,
            worker_slots=shared_worker_slots,
            cursor=(
                persisted_distribution_cursor
                if not _SCHEDULER_DISTRIBUTION_READY_ROOTS
                else None
            ),
        )
        _set_scheduler_important_pending_roots(set())
        for ready_root in dict.fromkeys(shared_ready_roots):
            _refresh_scheduler_important_root(store, ready_root)
        _set_scheduler_fairness_promoted_roots(promoted_roots)
        promoted_signature = frozenset(promoted_roots)
        oldest_wait_sec = max(
            [max(0, int(now - started_at)) for started_at in scheduler_pending_since_by_root.values()],
            default=0,
        )
        if _fairness_promotion_log_due(
            promoted_roots,
            scheduler_last_promoted_roots,
            now_ts=now,
            next_log_at=scheduler_next_fairness_log_at,
        ):
            logging.warning(
                "scheduler fairness watchdog promoted roots count=%s oldest_wait_sec=%s sample=%s",
                len(promoted_roots),
                oldest_wait_sec,
                ",".join(promoted_roots[:3]),
            )
            scheduler_next_fairness_log_at = now + 3600
        elif not promoted_signature:
            scheduler_next_fairness_log_at = 0.0
        scheduler_last_promoted_roots = promoted_signature
        store.put_runtime_sync(
            "scheduler_fairness_state",
            {
                "version": 2,
                "updated_at": float(now),
                "host_limit": max(0, int(getattr(config, "scheduler_host_max_parallel", 0) or 0)),
                "emergency_reserved_slots": max(
                    0,
                    int(getattr(config, "scheduler_emergency_reserved_slots", 1) or 0),
                ),
                "instance_limit": max(0, int(getattr(config, "scheduler_instance_max_parallel", 0) or 0)),
                "pending_roots": sorted(pending_roots),
                "priority_pending_roots": sorted(priority_pending_roots),
                "important_pending_roots": sorted(_SCHEDULER_IMPORTANT_PENDING_ROOTS),
                "shared_distribution_roots": list(_SCHEDULER_DISTRIBUTION_READY_ROOTS),
                "shared_distribution_cursor": int(_SCHEDULER_DISTRIBUTION_CURSOR),
                "promoted_roots": promoted_roots,
                "reserved_roots": promoted_roots[:1],
                "oldest_wait_sec": oldest_wait_sec,
            },
        )
        if dispatch_installs:
            dispatch_instance_cursor = (dispatch_instance_cursor + 1) % len(dispatch_installs)
        recurring_segment_priority_loop.update(
            config=config,
            installs=list(dispatch_installs),
            store=store,
            running=running,
            enabled=cluster_cron_allowed,
        )
        for inst in _iter_instances_with_root_dispatch_gate(
            dispatch_installs,
            busy_roots=main_dispatch_busy_roots,
            lock=_SCHEDULER_RUNTIME_LOCK,
        ):
            if not inst.db:
                logging.warning("[%s] skip install without db config", inst.root)
                continue

            root = inst.root
            with _SCHEDULER_RUNTIME_LOCK:
                if root in lifecycle_monitor_busy_roots:
                    continue
            segment_priority_for_inst = _segment_priority_effective_setting(config, inst)
            segment_super_priority_for_inst = _segment_super_priority_effective_setting(config, inst)
            segment_static_ids_for_inst = segment_priority_for_inst | segment_super_priority_for_inst
            segment_whitelist_for_inst = segment_static_ids_for_inst
            recurring_priority_entries = recurring_priority_entries_for_instance(
                getattr(config, "segment_recurring_priority_v1", {}),
                inst,
            )
            campaign_whitelist_for_inst = _campaign_whitelist_effective_setting(config, inst)
            db = MauticDB(inst.db)
            now_utc = datetime.now(timezone.utc)
            sql_ctx = campaign_sql_time_context(now_utc, inst.mautic_timezone, inst.mautic_major)
            inst_now = mautic_local_datetime(now_utc, inst.mautic_timezone)
            seg_sql_long_ring = segment_sql_long_rings.setdefault(root, deque())
            seg_sql_long_rules = segment_sql_long_rules_by_root.setdefault(root, {})
            seg_sql_long_active = segment_sql_long_active_sets.setdefault(root, set())
            seg_sql_long_done = segment_sql_long_done_sets.setdefault(root, set())
            seg_sql_ring = segment_sql_rings.setdefault(root, deque())
            seg_sql_rules = segment_sql_rules_by_root.setdefault(root, {})
            seg_sql_active = segment_sql_active_sets.setdefault(root, set())
            seg_sql_done = segment_sql_done_sets.setdefault(root, set())
            sql_managed_segment_ids = set(seg_sql_active) | set(seg_sql_long_active)
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
            trigger_plan_started_at = campaign_trigger_plan_started_at_by_root.setdefault(root, {})
            segment_blocked_ids = segment_dependency_blocked_ids(
                root=root,
                candidate_ids=set(seg_prio_set) | set(seg_reg_set) | set(seg_sql_active) | set(seg_sql_long_active),
                parents_by_child=segment_parents_by_root.get(root, {}),
                running=running,
                recently_finished=_recent_finished_segment_ids(root, now),
            )
            segment_blocked_ids |= set(segment_failure_blocked_sets.get(root, set()))
            segment_blocked_ids |= set(segment_logical_issue_blocked_sets.get(root, set()))
            segment_blocked_ids |= {entry.segment_id for entry in recurring_priority_entries}
            active_import_target_ids: set[int] = set()
            for targets in _import_monitor_target_segment_ids(
                import_monitor_cache.get(root),
                lanes={"queued", "running"},
            ).values():
                active_import_target_ids.update(targets)
            segment_blocked_ids |= active_import_target_ids
            dependency_parents_for_root = segment_parents_by_root.get(root, {})
            dependency_children_for_root = segment_dependencies_by_root.get(root, {})
            segment_cycle_payload = store.get_runtime_sync(_segment_cycle_key(root)) or {}
            if not isinstance(segment_cycle_payload, Mapping):
                segment_cycle_payload = {}
            segment_cycle_payload, reconciled_orphan_ids = _segment_cycle_reconcile_orphans(
                store,
                root=root,
                running=running,
                now_ts=time.time(),
                orphan_grace_sec=_segment_cycle_orphan_grace_sec(config),
            )
            if reconciled_orphan_ids:
                logging.warning(
                    "[%s] released stale ordinary segment claims from prior/missing workers ids=%s",
                    root,
                    ",".join(str(sid) for sid in reconciled_orphan_ids),
                )
            segment_cycle_payload, terminalized_data_errors = _terminalize_segment_data_errors(
                store,
                root=root,
                state=segment_cycle_payload,
                terminal_notes=segment_logical_data_error_notes.get(root, {}),
                excluded_ids=sql_managed_segment_ids | set(segment_static_ids_for_inst),
                now_ts=time.time(),
            )
            if terminalized_data_errors:
                segment_blocked_ids.difference_update(terminalized_data_errors)
                logging.error(
                    "[%s] terminalized unsafe segment topology as data_error ids=%s",
                    root,
                    ",".join(str(sid) for sid in terminalized_data_errors),
                )
            _refresh_scheduler_important_root(
                store,
                root,
                blocked_ids=segment_blocked_ids,
            )
            persisted_pending_cycle_order = _segment_cycle_pending_order(
                segment_cycle_payload,
                excluded_ids=sql_managed_segment_ids | set(segment_static_ids_for_inst),
            )
            persisted_pending_cycle_ids = set(persisted_pending_cycle_order)
            eligible_pending_cycle_ids = _segment_cycle_pending_ids(segment_cycle_payload)
            static_lane_payload = store.get_runtime_sync(_static_lane_state_key(root)) or {}
            static_lane_active_ids: set[int] = set()
            static_lanes_payload = static_lane_payload.get("lanes") if isinstance(static_lane_payload, dict) else {}
            if isinstance(static_lanes_payload, dict):
                for lane_payload in static_lanes_payload.values():
                    if not isinstance(lane_payload, dict):
                        continue
                    static_lane_active_ids.update(
                        int(value)
                        for value in [*(lane_payload.get("claimed") or []), *(lane_payload.get("running") or [])]
                        if str(value).strip().isdigit() and int(value) > 0
                    )
            segment_cycle_ids = set(
                _normalize_segment_ids(
                    [
                        *(segment_cycle_payload.get("items") or []),
                        *(segment_cycle_payload.get("follow_up") or []),
                    ]
                )
            )
            if not segment_cycle_ids:
                segment_cycle_ids = set(seg_prio_ring) | set(seg_reg_ring)

            def _segment_chain_running_conflict(eid: int) -> bool:
                if int(eid) in active_import_target_ids:
                    return True
                if not dependency_parents_for_root and not dependency_children_for_root:
                    return False
                try:
                    candidate_related = segment_related_ids(
                        int(eid),
                        dependency_parents_for_root,
                        dependency_children_for_root,
                    )
                except Exception:
                    return False
                if not candidate_related:
                    return False
                for task in running.values():
                    if task.root != root or task.entity_id is None:
                        continue
                    if task.task_type not in {"segment", "segment_sql"}:
                        continue
                    try:
                        running_related = segment_related_ids(
                            int(task.entity_id),
                            dependency_parents_for_root,
                            dependency_children_for_root,
                        )
                    except Exception:
                        running_related = {int(task.entity_id)}
                    if candidate_related & running_related:
                        return True
                for priority_sid in segment_cycle_ids:
                    if not priority_executor.is_active(root, "segment", int(priority_sid)):
                        continue
                    try:
                        priority_related = segment_related_ids(
                            int(priority_sid),
                            dependency_parents_for_root,
                            dependency_children_for_root,
                        )
                    except Exception:
                        priority_related = {int(priority_sid)}
                    if candidate_related & priority_related:
                        return True
                for static_sid in static_lane_active_ids:
                    try:
                        static_related = segment_related_ids(
                            int(static_sid),
                            dependency_parents_for_root,
                            dependency_children_for_root,
                        )
                    except Exception:
                        static_related = {int(static_sid)}
                    if candidate_related & static_related:
                        return True
                for running_sid in _segment_sql_worker_ids(root):
                    try:
                        running_related = segment_related_ids(
                            int(running_sid),
                            dependency_parents_for_root,
                            dependency_children_for_root,
                        )
                    except Exception:
                        running_related = {int(running_sid)}
                    if candidate_related & running_related:
                        return True
                return False

            def _segment_running_ids() -> set[int]:
                ids = {
                    int(task.entity_id)
                    for task in running.values()
                    if task.root == root and task.task_type == "segment" and task.entity_id is not None
                }
                ids.update(
                    sid
                    for sid in segment_cycle_ids
                    if priority_executor.is_active(root, "segment", int(sid))
                )
                ids.update(static_lane_active_ids)
                return ids

            def _claim_segment_cycle_item(sid: int) -> bool:
                return _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=int(sid),
                    eligible_ids=segment_cycle_ids,
                    blocked_ids=segment_blocked_ids,
                    running_ids=_segment_running_ids(),
                    now_ts=time.time(),
                )

            def _mark_segment_cycle_running(sid: int) -> None:
                _segment_cycle_running(store, root=root, entity_id=int(sid), now_ts=time.time())

            def _release_segment_cycle_item(sid: int) -> None:
                _segment_cycle_release(store, root=root, entity_id=int(sid), now_ts=time.time())

            _SEGMENT_ADMISSION_CONTEXT[str(root)] = {
                "claim": _claim_segment_cycle_item,
                "release": _release_segment_cycle_item,
                "running": _mark_segment_cycle_running,
            }

            def _trigger_waits_for_rebuild(cid: int) -> bool:
                if not config.enable_campaign_rebuild:
                    return False
                return _campaign_trigger_waits_for_rebuild(
                    root=root,
                    campaign_id=int(cid),
                    planned_after_ts=trigger_plan_started_at.get(int(cid), now),
                    running=running,
                )

            def _trigger_has_no_due_work(cid: int) -> bool:
                return _campaign_trigger_should_skip_launch(
                    db=db,
                    config=config,
                    root=root,
                    campaign_id=int(cid),
                    sql_ctx=sql_ctx,
                )

            def _rebuild_waits_for_trigger(cid: int) -> bool:
                return _campaign_rebuild_waits_for_trigger(
                    root=root,
                    campaign_id=int(cid),
                    running=running,
                )

            def _mark_segment_cycle(sid: int) -> None:
                segment_full_scan_pending.setdefault(root, set()).discard(sid)
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="segment",
                    entity_id=sid,
                )

            def _mark_segment_cycle_terminal(sid: int, success: bool, note: str | None = None) -> None:
                _segment_cycle_terminal(
                    store,
                    root=root,
                    entity_id=int(sid),
                    success=bool(success),
                    note=note,
                    now_ts=time.time(),
                )

            def _mark_campaign_trigger_cycle(cid: int) -> None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_trigger",
                    entity_id=cid,
                )

            def _mark_campaign_rebuild_cycle(cid: int) -> None:
                _monitor_cycle_mark_launched(
                    monitor_cycle_done,
                    root=root,
                    task_type="campaign_rebuild",
                    entity_id=cid,
                )

            def _publish_monitor_cycle() -> None:
                segment_resume_ring = segment_resume_rings.setdefault(root, deque())
                _remove_ring_entities(segment_resume_ring, sql_managed_segment_ids)
                segment_queued_ids: list[int] = []
                campaign_trigger_queued_ids: list[int] = []
                campaign_rebuild_queued_ids: list[int] = []
                if cluster_cron_allowed and config.segment_mode != "classic_loop":
                    durable_pending_order = _segment_cycle_pending_order(
                        segment_cycle_payload,
                        excluded_ids=sql_managed_segment_ids | set(segment_static_ids_for_inst),
                    )
                    segment_queued_ids = _unique_positive_ids(
                        _monitor_visible_queued_ids(
                            ring=durable_pending_order,
                            root=root,
                            task_type="segment",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                            bypass_repeat_guard_entities=set(durable_pending_order),
                        )
                        + _monitor_visible_queued_ids(
                            ring=seg_sql_long_ring,
                            root=root,
                            task_type="segment_sql",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                        )
                        + _monitor_visible_queued_ids(
                            ring=seg_sql_ring,
                            root=root,
                            task_type="segment_sql",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                        )
                        + _monitor_visible_queued_ids(
                            ring=segment_resume_ring,
                            root=root,
                            task_type="segment",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                        )
                        + _monitor_visible_queued_ids(
                            ring=seg_prio_ring,
                            root=root,
                            task_type="segment",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                        )
                        + _monitor_visible_queued_ids(
                            ring=seg_reg_ring,
                            root=root,
                            task_type="segment",
                            running=running,
                            config=config,
                            now_ts=now,
                            blocked_entities=segment_blocked_ids,
                            dynamic_blocked=_segment_chain_running_conflict,
                            running_task_types={"segment", "segment_sql"},
                        )
                    )
                if cluster_cron_allowed:
                    campaign_trigger_queued_ids = _unique_positive_ids(
                        _monitor_visible_queued_ids(
                            ring=trg_prio_ring,
                            root=root,
                            task_type="campaign_trigger",
                            running=running,
                            config=config,
                            now_ts=now,
                            dynamic_blocked=_trigger_waits_for_rebuild,
                        )
                        + _monitor_visible_queued_ids(
                            ring=trg_reg_ring,
                            root=root,
                            task_type="campaign_trigger",
                            running=running,
                            config=config,
                            now_ts=now,
                            dynamic_blocked=_trigger_waits_for_rebuild,
                        )
                    )
                    if config.enable_campaign_rebuild:
                        campaign_rebuild_queued_ids = _unique_positive_ids(
                            _monitor_visible_queued_ids(
                                ring=reb_prio_ring,
                                root=root,
                                task_type="campaign_rebuild",
                                running=running,
                                config=config,
                                now_ts=now,
                                running_task_types={"campaign_rebuild", "campaign_update"},
                                dynamic_blocked=_rebuild_waits_for_trigger,
                            )
                            + _monitor_visible_queued_ids(
                                ring=reb_reg_ring,
                                root=root,
                                task_type="campaign_rebuild",
                                running=running,
                                config=config,
                                now_ts=now,
                                running_task_types={"campaign_rebuild", "campaign_update"},
                                dynamic_blocked=_rebuild_waits_for_trigger,
                            )
                        )
                if config.segment_mode == "classic_loop":
                    empty_ring: deque[int] = deque()
                    _publish_scheduler_monitor_cycles(
                        store=store,
                        root=root,
                        cycle_done=monitor_cycle_done,
                        running=running,
                        now_ts=now,
                        seg_sql_ring=empty_ring,
                        seg_resume_ring=empty_ring,
                        seg_prio_ring=empty_ring,
                        seg_reg_ring=empty_ring,
                        campaign_trigger_prio_ring=trg_prio_ring,
                        campaign_trigger_reg_ring=trg_reg_ring,
                        campaign_rebuild_prio_ring=reb_prio_ring,
                        campaign_rebuild_reg_ring=reb_reg_ring,
                        segment_queued_ids=[],
                        campaign_trigger_queued_ids=campaign_trigger_queued_ids,
                        campaign_rebuild_queued_ids=campaign_rebuild_queued_ids,
                        import_monitor=import_monitor_cache.get(root),
                    )
                    return
                _publish_scheduler_monitor_cycles(
                    store=store,
                    root=root,
                    cycle_done=monitor_cycle_done,
                    running=running,
                    now_ts=now,
                    seg_sql_ring=deque(list(seg_sql_long_ring) + list(seg_sql_ring)),
                    seg_resume_ring=segment_resume_ring,
                    seg_prio_ring=seg_prio_ring,
                    seg_reg_ring=seg_reg_ring,
                    campaign_trigger_prio_ring=trg_prio_ring,
                    campaign_trigger_reg_ring=trg_reg_ring,
                    campaign_rebuild_prio_ring=reb_prio_ring,
                    campaign_rebuild_reg_ring=reb_reg_ring,
                    segment_queued_ids=segment_queued_ids,
                    campaign_trigger_queued_ids=campaign_trigger_queued_ids,
                    campaign_rebuild_queued_ids=campaign_rebuild_queued_ids,
                    import_monitor=import_monitor_cache.get(root),
                )

            if _import_in_settle(root, now):
                import_pending_cache[root] = 0
            _mark_external_entities_executed(
                running=running,
                root=root,
                seg_sql_done=seg_sql_done,
                seg_sql_ring=seg_sql_ring,
                seg_sql_long_done=seg_sql_long_done,
                seg_sql_long_ring=seg_sql_long_ring,
                seg_prio_ring=seg_prio_ring,
                seg_reg_ring=seg_reg_ring,
                trg_prio_ring=trg_prio_ring,
                trg_reg_ring=trg_reg_ring,
                reb_prio_ring=reb_prio_ring,
                reb_reg_ring=reb_reg_ring,
                monitor_cycle_done=monitor_cycle_done,
            )

            db_pause_until = float(db_dispatch_pause_until.get(root, 0.0))

            def _persist_segment_dispatch_context(
                *,
                throttled_active: bool | None = None,
                priority_pending: bool | None = None,
                segment_slot_limit: int | None = None,
            ) -> None:
                """Persist the complete execution policy for the root.

                ``mcd-segment-pending-guard-v1`` is telemetry and is allowed
                to truncate display lists.  The independent refill worker
                must never treat that receipt as policy.  This separate
                context is the durable, full-fidelity admission snapshot it
                may consume while the planner is busy elsewhere.
                """

                parents = {
                    str(int(child)): sorted(int(parent) for parent in parent_ids if int(parent) > 0)
                    for child, parent_ids in segment_parents_by_root.get(root, {}).items()
                }
                children = {
                    str(int(parent)): sorted(int(child) for child in child_ids if int(child) > 0)
                    for parent, child_ids in segment_dependencies_by_root.get(root, {}).items()
                }
                context = {
                    "schema": "mcd-segment-dispatch-context-v1",
                    "root": root,
                    "mautic_major": int(getattr(inst, "mautic_major", 0) or 0),
                    "updated_at": float(time.time()),
                    "cycle_id": segment_cycle_payload.get("cycle_id"),
                    "cycle_status": str(segment_cycle_payload.get("status") or ""),
                    "blocked_ids": sorted(int(value) for value in segment_blocked_ids if int(value) > 0),
                    "sql_managed_ids": sorted(int(value) for value in sql_managed_segment_ids if int(value) > 0),
                    "static_configured_ids": sorted(int(value) for value in segment_static_ids_for_inst if int(value) > 0),
                    "static_active_ids": sorted(int(value) for value in static_lane_active_ids if int(value) > 0),
                    "active_import_target_ids": sorted(int(value) for value in active_import_target_ids if int(value) > 0),
                    "parents_by_child": parents,
                    "children_by_parent": children,
                    "scheduler_pause": bool(scheduler_dispatch_pause),
                    "backup_pause": bool(backup_dispatch_pause),
                    "backup_pause_reason": str(backup_pause_reason or ""),
                    "dispatch_pause": bool(dispatch_pause),
                    "db_pause_until": float(db_pause_until),
                    "db_pause_reason": str(db_dispatch_pause_reasons.get(root, "") or ""),
                    "cluster_cron_allowed": bool(cluster_cron_allowed),
                    "cluster_import_allowed": bool(cluster_import_allowed),
                    "throttled_active": throttled_active,
                    "priority_pending": priority_pending,
                    "segment_slot_limit": segment_slot_limit,
                    "segment_lazy_update_enabled": _segment_lazy_update_effective_setting(config, inst),
                }
                store.put_runtime_sync(_segment_dispatch_context_key(root), context)

            def _record_segment_pending_guard(
                reason: str,
                *,
                total_limit: int | None = None,
                launch_budget: int | None = None,
                launched: int = 0,
                throttled_active: bool | None = None,
                priority_pending: bool | None = None,
                segment_slot_limit: int | None = None,
            ) -> None:
                cycle_status = str(segment_cycle_payload.get("status") or "").strip().lower()
                if cycle_status != "active" and not eligible_pending_cycle_ids:
                    return
                blocked_pending_ids = sorted(
                    int(value)
                    for value in persisted_pending_cycle_ids & segment_blocked_ids
                )
                payload = {
                    "schema": "mcd-segment-pending-guard-v1",
                    "root": root,
                    "updated_at": float(now),
                    "cycle_status": cycle_status,
                    "cycle_id": segment_cycle_payload.get("cycle_id"),
                    "eligible_pending_count": len(eligible_pending_cycle_ids),
                    "projected_pending_count": len(persisted_pending_cycle_ids),
                    "blocked_count": len(blocked_pending_ids),
                    "blocked_ids": blocked_pending_ids[:100],
                    "sql_managed_count": len(sql_managed_segment_ids),
                    "sql_managed_ids": sorted(sql_managed_segment_ids)[:100],
                    "static_configured_count": len(segment_static_ids_for_inst),
                    "static_configured_ids": sorted(segment_static_ids_for_inst)[:100],
                    "static_active_count": len(static_lane_active_ids),
                    "running_segment": _running_count(running, root, "segment"),
                    "running_import": _running_count(running, root, "import"),
                    "running_sql": _segment_sql_worker_count(root),
                    "scheduler_pause": bool(scheduler_dispatch_pause),
                    "backup_pause": bool(backup_dispatch_pause),
                    "backup_pause_reason": str(backup_pause_reason or ""),
                    "dispatch_pause": bool(dispatch_pause),
                    "db_pause_until": float(db_pause_until),
                    "db_pause_reason": str(db_dispatch_pause_reasons.get(root, "") or ""),
                    "cluster_cron_allowed": bool(cluster_cron_allowed),
                    "cluster_import_allowed": bool(cluster_import_allowed),
                    "cluster_cache_allowed": bool(cluster_cache_allowed),
                    "absolute_total_limit": total_limit,
                    "free_launch_budget": launch_budget,
                    "throttled_active": throttled_active,
                    "priority_pending": priority_pending,
                    "segment_slot_limit": segment_slot_limit,
                    "launched": int(launched or 0),
                    "reason": str(reason),
                }
                store.put_runtime_sync(_segment_pending_guard_key(root), payload)
                previous_log = float(_SEGMENT_PENDING_GUARD_LOG_TS.get(root, 0.0) or 0.0)
                if now - previous_log >= 30.0:
                    _SEGMENT_PENDING_GUARD_LOG_TS[root] = now
                    logging.info(
                        "[%s] segment pending guard reason=%s eligible=%s projected=%s blocked=%s sql=%s static=%s db_pause=%s total_limit=%s launch_budget=%s launched=%s",
                        root,
                        reason,
                        len(eligible_pending_cycle_ids),
                        len(persisted_pending_cycle_ids),
                        len(blocked_pending_ids),
                        len(sql_managed_segment_ids),
                        len(segment_static_ids_for_inst),
                        bool(db_pause_until > now),
                        total_limit,
                        launch_budget,
                        int(launched or 0),
                    )

            # Publish the full execution policy before any early dispatch
            # pause.  The persisted refill worker reads this context, never
            # the display-oriented pending-guard receipt.
            _persist_segment_dispatch_context(
                throttled_active=bool(throttled.get(root, False)),
                segment_slot_limit=_effective_segment_slot_limit(
                    config,
                    bool(throttled.get(root, False)),
                ),
            )
            if dispatch_pause:
                _record_segment_pending_guard("dispatch_pause")
                _publish_monitor_cycle()
                continue
            if db_pause_until > now:
                _record_segment_pending_guard("db_pause")
                _publish_monitor_cycle()
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
                monitor_cycle_done=monitor_cycle_done,
                db=db,
                mautic_major=int(getattr(inst, "mautic_major", 0) or 0),
                segment_super_priority_ids=segment_super_priority_for_inst,
                segment_priority_ids=segment_priority_for_inst,
            )

            if not (cluster_cron_allowed or cluster_import_allowed or cluster_cache_allowed):
                _record_segment_pending_guard("all_cluster_lanes_disallowed")
                _publish_monitor_cycle()
                continue

            campaign_pressure = _campaign_pressure_active(
                config,
                running,
                root,
                trigger_prio_ring=trg_prio_ring,
                trigger_reg_ring=trg_reg_ring,
                rebuild_prio_ring=reb_prio_ring,
                rebuild_reg_ring=reb_reg_ring,
                trigger_dynamic_blocked=_trigger_waits_for_rebuild,
                now_ts=now,
            )
            segment_throttled_active = bool(throttled.get(root, False) or campaign_pressure)
            segment_slot_limit = _effective_segment_slot_limit(
                config,
                segment_throttled_active,
            )
            _persist_segment_dispatch_context(
                throttled_active=segment_throttled_active,
                segment_slot_limit=segment_slot_limit,
            )
            import_segment_slot_limit = max(1, segment_slot_limit) if cluster_import_allowed else segment_slot_limit
            segment_sql_db_running_count = _segment_sql_active_db_rebuild_query_count(db)
            segment_launched_this_tick = 0
            if _submit_import_if_segment_slot(
                config=config,
                store=store,
                running=running,
                popens=popens,
                root=root,
                cluster_import_allowed=cluster_import_allowed,
                import_pending_count=max(0, int(import_pending_cache.get(root, 0) or 0)),
                segment_slot_limit=import_segment_slot_limit,
                now_ts=now,
                sql_db_running_count=segment_sql_db_running_count,
            ):
                last_import_activity_ts[root] = now
                segment_launched_this_tick += 1

            import_followup_ids = set(import_completed_segment_followups.get(root, set()))
            ordinary_segment_pending = bool(
                persisted_pending_cycle_ids
                or seg_sql_long_ring
                or seg_sql_ring
                or segment_resume_rings.get(root)
                or seg_prio_ring
                or seg_reg_ring
            )
            import_followup_limit = _import_followup_slot_limit(
                running,
                root,
                segment_slot_limit,
                ordinary_segment_pending=ordinary_segment_pending,
            )
            if (
                cluster_cron_allowed
                and not segment_throttled_active
                and import_followup_ids
                and import_followup_limit > 0
            ):
                import_followup_ring = deque(sorted(import_followup_ids))

                def _mark_import_followup_launch(sid: int) -> None:
                    import_completed_segment_followups.setdefault(root, set()).discard(sid)
                    _mark_segment_cycle(sid)

                _fill_from_ring(
                    ring=import_followup_ring,
                    ring_limit=import_followup_limit,
                    total_limit=import_followup_limit,
                    root=root,
                    task_type="segment",
                    running=running,
                    ring_entities=import_followup_ids,
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
                    dynamic_blocked=_segment_chain_running_conflict,
                    on_launch=_mark_import_followup_launch,
                    bypass_repeat_guard_entities=import_followup_ids,
                    queued_at_by_entity=import_completed_segment_followup_queued_at.setdefault(root, {}),
                    remove_on_launch=True,
                    max_launches=import_followup_limit,
                )
            elif import_followup_ids:
                if not cluster_cron_allowed:
                    reason = "cron_disallowed"
                elif segment_throttled_active:
                    reason = "throttled"
                elif import_followup_limit <= 0:
                    reason = "shared_segment_slots_busy"
                else:
                    reason = "dispatch_guard"
                wait_sec = max(
                    0.0,
                    now - min(import_completed_segment_followup_queued_at.get(root, {}).values(), default=now),
                )
                last_wait_log = float(import_followup_wait_log_ts.get(root, 0.0) or 0.0)
                if now - last_wait_log >= 60.0:
                    import_followup_wait_log_ts[root] = now
                    logging.info(
                        "[%s] segment scheduler follow-up waiting segments=%s queue_wait_sec=%.3f reason=%s slots=%s",
                        root,
                        len(import_followup_ids),
                        wait_sec,
                        reason,
                        import_followup_limit,
                    )

            # A pending item from an already-admitted ordinary cycle is not a
            # new admission.  Dispatch it even while campaign/queue pressure
            # has reduced the normal ring limits, and do not let the global
            # five-second baseline cadence make a persisted cursor wait for a
            # different instance's launch.  Claim/dependency, host/instance,
            # exact-task and repeat guards remain in force.
            if (
                cluster_cron_allowed
                and persisted_pending_cycle_ids
            ):
                pending_cycle_ring = deque(persisted_pending_cycle_order)
                # Persisted ordinary work uses the regular budget while
                # priority work is ready, but may borrow unused priority
                # capacity when that ring is empty.  Keep the absolute cap
                # separate from the free launch budget: otherwise a cycle
                # with one running worker and a 3-worker regular budget is
                # incorrectly capped at 2 total workers.
                pending_priority = _ring_has_launchable(
                    seg_prio_ring,
                    running=running,
                    root=root,
                    task_type="segment",
                )
                _persist_segment_dispatch_context(
                    throttled_active=segment_throttled_active,
                    priority_pending=pending_priority,
                    segment_slot_limit=segment_slot_limit,
                )
                pending_total_limit, pending_launch_budget = _segment_pending_dispatch_limits(
                    config,
                    segment_throttled_active,
                    running,
                    root,
                    segment_slot_limit=segment_slot_limit,
                    priority_pending=pending_priority,
                    store=store,
                    now_ts=now,
                    sql_db_running_count=segment_sql_db_running_count,
                )
                pending_launched_this_tick = 0
                if pending_cycle_ring and pending_launch_budget > 0:
                    pending_launched_this_tick = _fill_from_ring(
                        ring=pending_cycle_ring,
                        ring_limit=pending_total_limit,
                        total_limit=pending_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=persisted_pending_cycle_ids,
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
                        dynamic_blocked=_segment_chain_running_conflict,
                        on_launch=_mark_segment_cycle,
                        remove_on_launch=True,
                        bypass_repeat_guard_entities=persisted_pending_cycle_ids,
                        bypass_baseline_admission=True,
                        max_launches=pending_launch_budget,
                    )
                    segment_launched_this_tick += pending_launched_this_tick
                if not pending_cycle_ring:
                    pending_guard_reason = "no_projected_pending"
                elif pending_launch_budget <= 0:
                    pending_guard_reason = (
                        "all_pending_blocked"
                        if len(persisted_pending_cycle_ids & segment_blocked_ids)
                        >= len(persisted_pending_cycle_ids)
                        else "no_free_launch_budget"
                    )
                elif pending_launched_this_tick <= 0:
                    pending_guard_reason = "dispatch_guard"
                else:
                    pending_guard_reason = "launched"
                _record_segment_pending_guard(
                    pending_guard_reason,
                    total_limit=pending_total_limit,
                    launch_budget=pending_launch_budget,
                    launched=pending_launched_this_tick,
                    throttled_active=segment_throttled_active,
                    priority_pending=pending_priority,
                    segment_slot_limit=segment_slot_limit,
                )
            elif eligible_pending_cycle_ids or str(segment_cycle_payload.get("status") or "").lower() == "active":
                if not cluster_cron_allowed:
                    pending_guard_reason = "cron_disallowed"
                elif set(
                    _normalize_segment_ids(
                        [*(segment_cycle_payload.get("claimed") or []), *(segment_cycle_payload.get("running") or [])]
                    )
                ):
                    pending_guard_reason = "cycle_claim_or_terminal_reconcile"
                else:
                    pending_guard_reason = "no_projected_pending"
                _record_segment_pending_guard(pending_guard_reason)

            if (
                segment_launched_this_tick <= 0
                and cluster_cron_allowed
                and config.segment_mode != "classic_loop"
                and _segment_shared_slots_available(
                    running,
                    root,
                    segment_slot_limit,
                    store=store,
                    config=config,
                    now_ts=now,
                    sql_db_running_count=segment_sql_db_running_count,
                )
                > 0
                and not (segment_throttled_active and config.segment_throttle_whitelist_only)
            ):
                segment_launched_this_tick += _run_sql_segment_ring(
                    config=config,
                    store=store,
                    db=db,
                    root=root,
                    ring=seg_sql_long_ring,
                    rules=seg_sql_long_rules,
                    active_set=seg_sql_long_active,
                    done_set=seg_sql_long_done,
                    running=running,
                    sql_ctx=sql_ctx,
                    now_ts=now,
                    now_local=inst_now,
                    max_per_tick=int(getattr(config, "segment_sql_long_ring_max_per_tick", 1) or 0),
                    ring_label="segment_sql_long",
                    async_worker=True,
                    sql_db_running_count=segment_sql_db_running_count,
                    on_launch=_mark_segment_cycle,
                    on_terminal=_mark_segment_cycle_terminal,
                    dynamic_blocked=_segment_chain_running_conflict,
                )
            if (
                segment_launched_this_tick <= 0
                and cluster_cron_allowed
                and config.segment_mode != "classic_loop"
                and _segment_shared_slots_available(
                    running,
                    root,
                    segment_slot_limit,
                    store=store,
                    config=config,
                    now_ts=now,
                    sql_db_running_count=segment_sql_db_running_count,
                )
                > 0
                and not (segment_throttled_active and config.segment_throttle_whitelist_only)
            ):
                segment_launched_this_tick += _run_sql_segment_ring(
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
                    sql_db_running_count=segment_sql_db_running_count,
                    on_launch=_mark_segment_cycle,
                    on_terminal=_mark_segment_cycle_terminal,
                    dynamic_blocked=_segment_chain_running_conflict,
                )

            if cluster_cron_allowed and config.segment_mode == "classic_loop":
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_segment_full_update_template,
                    batch_limit=config.segment_batch_limit,
                )
                classic_segment_limit = _segment_task_limit_after_import(
                    running,
                    root,
                    min(1, segment_slot_limit),
                )
                if (
                    classic_segment_limit > 0
                    and segment_launched_this_tick <= 0
                    and not active_import_target_ids
                ):
                    if _submit_if_slot(
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
                    ):
                        segment_launched_this_tick += 1
            elif cluster_cron_allowed:
                if segment_throttled_active:
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
                seg_total_limit = min(seg_total_limit, max(0, int(segment_slot_limit)))
                eff_seg_prio_limit = seg_prio_limit
                eff_seg_reg_limit = seg_reg_limit
                prefer_priority_spill = False
                if (
                    not segment_throttled_active
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
                _remove_ring_entities(seg_resume_ring, sql_managed_segment_ids)

                if segment_throttled_active and config.segment_throttle_whitelist_only and config.segment_throttle_kill_non_whitelist:
                    non_whitelist_tasks = [
                        (key, task)
                        for key, task in running.items()
                        if task.root == root
                        and task.task_type == "segment"
                        and task.entity_id is not None
                        and task.entity_id not in segment_whitelist_for_inst
                    ]
                    whitelist_running = any(
                        task.root == root
                        and task.task_type == "segment"
                        and task.entity_id in segment_whitelist_for_inst
                        for task in running.values()
                    ) or _segment_sql_worker_count(root) > 0
                    keep_baseline_key = None
                    if non_whitelist_tasks and not whitelist_running:
                        keep_baseline_key = min(non_whitelist_tasks, key=lambda item: item[1].started_at)[0]
                    for key, task in non_whitelist_tasks:
                        if key == keep_baseline_key:
                            continue
                        _kill_pid(task.pid, config.segment_kill_grace_sec)
                        store.finish(task.row_id, state="timeout", rc=None, note="throttle_kill")
                        running.pop(key, None)
                        popens.pop(key, None)
                        if task.entity_id not in seg_resume_ring:
                            seg_resume_ring.appendleft(task.entity_id)

                if not segment_throttled_active and seg_resume_ring:
                    segment_launched_this_tick += _fill_from_ring(
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
                        dynamic_blocked=_segment_chain_running_conflict,
                        on_launch=_mark_segment_cycle,
                        max_launches=seg_total_limit,
                    )

                if segment_throttled_active and config.segment_throttle_whitelist_only:
                    wl_ids = list(
                        dict.fromkeys(
                            [x for x in list(seg_prio_ring) + list(seg_reg_ring) if x in segment_whitelist_for_inst]
                        )
                    )
                    seg_wl_ring = deque(wl_ids)
                    segment_launched_this_tick += _fill_from_ring(
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
                        dynamic_blocked=_segment_chain_running_conflict,
                        on_launch=_mark_segment_cycle,
                        max_launches=seg_prio_limit,
                    )
                    if _scheduler_root_lane_running_count(running, root, "segment") <= 0:
                        for baseline_ring, baseline_entities in (
                            (seg_prio_ring, seg_prio_set),
                            (seg_reg_ring, seg_reg_set),
                        ):
                            if segment_launched_this_tick > 0:
                                break
                            segment_launched_this_tick += _fill_from_ring(
                                ring=baseline_ring,
                                ring_limit=1,
                                total_limit=1,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=baseline_entities,
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
                                dynamic_blocked=_segment_chain_running_conflict,
                                on_launch=_mark_segment_cycle,
                                max_launches=1,
                            )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total >= seg_total_limit:
                        pass
                    else:
                        # No non-whitelist launches while throttle is active.
                        pass
                else:
                    segment_launched_this_tick += _fill_from_ring(
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
                        dynamic_blocked=_segment_chain_running_conflict,
                        on_launch=_mark_segment_cycle,
                        max_launches=eff_seg_prio_limit,
                    )
                    segment_launched_this_tick += _fill_from_ring(
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
                        dynamic_blocked=_segment_chain_running_conflict,
                        on_launch=_mark_segment_cycle,
                        max_launches=eff_seg_reg_limit,
                    )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total < seg_total_limit:
                        spill = seg_total_limit - seg_cur_total
                        if prefer_priority_spill and seg_prio_ring and spill > 0:
                            segment_launched_this_tick += _fill_from_ring(
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
                                dynamic_blocked=_segment_chain_running_conflict,
                                on_launch=_mark_segment_cycle,
                                max_launches=spill,
                            )
                        elif seg_reg_ring:
                            segment_launched_this_tick += _fill_from_ring(
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
                                dynamic_blocked=_segment_chain_running_conflict,
                                on_launch=_mark_segment_cycle,
                                max_launches=spill,
                            )
                        elif seg_prio_ring and spill > 0:
                            # If regular ring is empty, keep total segment concurrency at target
                            # by temporarily borrowing regular slot(s) for priority ring.
                            segment_launched_this_tick += _fill_from_ring(
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
                                dynamic_blocked=_segment_chain_running_conflict,
                                on_launch=_mark_segment_cycle,
                                max_launches=spill,
                            )

            _publish_monitor_cycle()

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
                    if next_trigger_id is not None and _trigger_waits_for_rebuild(int(next_trigger_id)):
                        next_trigger_id = None
                    if next_trigger_id is not None and _trigger_has_no_due_work(int(next_trigger_id)):
                        remove_skipped = _campaign_trigger_skip_removes_from_ring(
                            root,
                            int(next_trigger_id),
                            campaign_whitelist_for_inst,
                        )
                        _advance_ring_after_launch(trg_prio_ring, next_trigger_id, remove_on_launch=remove_skipped)
                        _advance_ring_after_launch(trg_reg_ring, next_trigger_id, remove_on_launch=remove_skipped)
                        next_trigger_id = None

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
                            _mark_campaign_trigger_cycle(next_trigger_id)
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
                        if next_campaign_id is not None and _rebuild_waits_for_trigger(int(next_campaign_id)):
                            next_campaign_id = None
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
                                _mark_campaign_rebuild_cycle(next_campaign_id)
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
                _publish_monitor_cycle()
                # Skip generic multi-ring campaign scheduler.
                continue

            shared_campaign_cap = max(0, config.campaign_total_parallel) if cluster_cron_allowed else 0
            rr = campaign_round_robin.get(root, 0)
            trigger_lane_configured = (
                max(0, int(config.campaign_trigger_priority_parallel))
                + max(0, int(config.campaign_trigger_regular_parallel))
            ) > 0
            trigger_after_rebuild_pending = _campaign_trigger_after_rebuild_pending(
                root,
                (trg_prio_ring, trg_reg_ring),
            )
            prefer_rebuild = bool(
                config.enable_campaign_rebuild
                and trigger_lane_configured
                and (rr % 2 == 1)
                and not trigger_after_rebuild_pending
            )

            def _campaign_shared_available() -> int | None:
                if shared_campaign_cap <= 0:
                    return None
                return max(0, shared_campaign_cap - _running_campaign_total(running, root))

            def _campaign_lane_limits(prio_limit: int, reg_limit: int) -> tuple[int, int, int]:
                prio = max(0, int(prio_limit or 0)) if cluster_cron_allowed else 0
                reg = max(0, int(reg_limit or 0)) if cluster_cron_allowed else 0
                total = prio + reg
                shared_avail = _campaign_shared_available()
                if shared_avail is not None:
                    total = min(total, shared_avail)
                    prio = min(prio, total)
                    reg = min(reg, max(0, total - prio))
                return prio, reg, total

            def _try_campaign_trigger_once() -> int:
                trg_prio_limit, trg_reg_limit, trg_total_limit = _campaign_lane_limits(
                    config.campaign_trigger_priority_parallel,
                    config.campaign_trigger_regular_parallel,
                )
                launched = _fill_from_ring(
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
                    dynamic_blocked=_trigger_waits_for_rebuild,
                    should_skip=_trigger_has_no_due_work,
                    remove_on_skip=lambda cid: _campaign_trigger_skip_removes_from_ring(
                        root,
                        int(cid),
                        campaign_whitelist_for_inst,
                    ),
                    remove_on_launch=lambda cid: cid not in campaign_whitelist_for_inst,
                    on_launch=_mark_campaign_trigger_cycle,
                )
                if launched <= 0:
                    launched += _fill_from_ring(
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
                        dynamic_blocked=_trigger_waits_for_rebuild,
                        should_skip=_trigger_has_no_due_work,
                        remove_on_skip=lambda cid: _campaign_trigger_skip_removes_from_ring(
                            root,
                            int(cid),
                            campaign_whitelist_for_inst,
                        ),
                        remove_on_launch=True,
                        on_launch=_mark_campaign_trigger_cycle,
                    )
                trg_cur_total = _running_count(running, root, "campaign_trigger")
                if launched <= 0 and trg_cur_total < trg_total_limit:
                    spill = trg_total_limit - trg_cur_total
                    if trg_reg_ring and not _campaign_spill_prefers_priority(
                        trg_prio_ring,
                        trg_prio_limit,
                    ):
                        launched += _fill_from_ring(
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
                            dynamic_blocked=_trigger_waits_for_rebuild,
                            should_skip=_trigger_has_no_due_work,
                            remove_on_skip=lambda cid: _campaign_trigger_skip_removes_from_ring(
                                root,
                                int(cid),
                                campaign_whitelist_for_inst,
                            ),
                            remove_on_launch=True,
                            on_launch=_mark_campaign_trigger_cycle,
                        )
                    elif trg_prio_ring:
                        launched += _fill_from_ring(
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
                            dynamic_blocked=_trigger_waits_for_rebuild,
                            should_skip=_trigger_has_no_due_work,
                            remove_on_skip=lambda cid: _campaign_trigger_skip_removes_from_ring(
                                root,
                                int(cid),
                                campaign_whitelist_for_inst,
                            ),
                            remove_on_launch=lambda cid: cid not in campaign_whitelist_for_inst,
                            on_launch=_mark_campaign_trigger_cycle,
                        )
                return launched

            def _try_campaign_rebuild_once() -> int:
                if not (cluster_cron_allowed and config.enable_campaign_rebuild):
                    return 0
                rebuild_prio_limit, rebuild_reg_limit, rebuild_total_limit = _campaign_lane_limits(
                    config.campaign_rebuild_priority_parallel,
                    config.campaign_rebuild_regular_parallel,
                )
                launched = _fill_from_ring(
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
                    dynamic_blocked=_rebuild_waits_for_trigger,
                    remove_on_launch=lambda cid: cid not in campaign_whitelist_for_inst,
                    on_launch=_mark_campaign_rebuild_cycle,
                )
                if launched <= 0:
                    launched += _fill_from_ring(
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
                        dynamic_blocked=_rebuild_waits_for_trigger,
                        remove_on_launch=True,
                        on_launch=_mark_campaign_rebuild_cycle,
                    )
                reb_cur_total = _running_count(running, root, "campaign_rebuild")
                if launched <= 0 and reb_cur_total < rebuild_total_limit:
                    spill = rebuild_total_limit - reb_cur_total
                    if reb_reg_ring and not _campaign_spill_prefers_priority(
                        reb_prio_ring,
                        rebuild_prio_limit,
                    ):
                        launched += _fill_from_ring(
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
                            dynamic_blocked=_rebuild_waits_for_trigger,
                            remove_on_launch=True,
                            on_launch=_mark_campaign_rebuild_cycle,
                        )
                    elif reb_prio_ring:
                        launched += _fill_from_ring(
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
                            dynamic_blocked=_rebuild_waits_for_trigger,
                            remove_on_launch=lambda cid: cid not in campaign_whitelist_for_inst,
                            on_launch=_mark_campaign_rebuild_cycle,
                        )
                return launched

            campaign_launched_this_tick = 0
            if prefer_rebuild:
                campaign_launched_this_tick += _try_campaign_rebuild_once()
                if campaign_launched_this_tick <= 0:
                    campaign_launched_this_tick += _try_campaign_trigger_once()
            else:
                campaign_launched_this_tick += _try_campaign_trigger_once()
                if campaign_launched_this_tick <= 0:
                    campaign_launched_this_tick += _try_campaign_rebuild_once()

            if campaign_launched_this_tick > 0 and config.enable_campaign_rebuild and trigger_lane_configured:
                campaign_round_robin[root] = rr + 1

            _publish_monitor_cycle()

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
                skip_segment_ids.update(_segment_sql_worker_ids(root))
                skip_campaign_ids = {
                    int(task.entity_id)
                    for task in running.values()
                    if task.root == root
                    and task.entity_id is not None
                    and task.task_type in {"campaign_update", "campaign_rebuild", "campaign_trigger"}
                }
                try:
                    file_lock_res = cleanup_stale_mautic_file_locks(
                        root,
                        min_age_sec=max(0, int(config.mautic_lock_cleanup_min_age_sec or 21600)),
                    )
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
                    file_rows = file_lock_res.get("file_locks") if isinstance(file_lock_res, dict) else []
                    file_rows = file_rows if isinstance(file_rows, list) else []
                    cleared_file_locks = int((file_lock_res or {}).get("cleared_file_locks", 0) or 0)
                    if cleared_segments > 0 or cleared_campaigns > 0 or cleared_file_locks > 0:
                        logging.warning(
                            (
                                "[%s] mautic_lock_cleanup cleared segments=%s campaigns=%s file_locks=%s "
                                "cutoff_utc=%s segment_ids=%s campaign_ids=%s file_lock_paths=%s"
                            ),
                            root,
                            cleared_segments,
                            cleared_campaigns,
                            cleared_file_locks,
                            cutoff_utc,
                            ",".join(str(int(row.get("id") or 0)) for row in seg_rows if int(row.get("id") or 0) > 0) or "-",
                            ",".join(str(int(row.get("id") or 0)) for row in camp_rows if int(row.get("id") or 0) > 0) or "-",
                            ",".join(
                                str(row.get("path") or "")
                                for row in file_rows
                                if str(row.get("status") or "") == "cleared" and str(row.get("path") or "")
                            )
                            or "-",
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
            selected_service_cleanup, selected_service_cleanup_idx = _select_fair_cleanup_task(
                service_cleanup_candidates,
                maintenance_cleanup_rr_index.get(root, -1),
            )
            if selected_service_cleanup:
                maintenance_cleanup_rr_index[root] = selected_service_cleanup_idx

            if selected_service_cleanup == "page_hits_orphan_cleanup":
                try:
                    page_hits_index_ready = db.orphan_page_hits_cleanup_index_ready()
                except Exception as e:
                    page_hits_index_ready = False
                    logging.warning("[%s] page_hits_orphan_cleanup index check failed: %s", root, e)
                if not page_hits_index_ready:
                    page_hits_orphan_cleanup_active[root] = False
                    logging.warning(
                        "[%s] page_hits_orphan_cleanup skipped: required index idx_mcd_ph_lead_date is missing",
                        root,
                    )
                    if page_hits_cleanup_window_key:
                        page_hits_orphan_cleanup_done_window_keys[root] = page_hits_cleanup_window_key
                        page_hits_orphan_cleanup_window_counts[root] = (
                            page_hits_orphan_cleanup_window_counts.get(root, 0) + 1
                        )
                    last_page_hits_orphan_cleanup_ts[root] = now
                    selected_service_cleanup = ""

            if selected_service_cleanup == "page_hits_orphan_cleanup":
                page_hits_cleanup_lock = db.try_acquire_orphan_page_hits_cleanup_lock()
                if page_hits_cleanup_lock is None:
                    logging.info("[%s] page_hits_orphan_cleanup skipped: cleanup lock is already held", root)
                    selected_service_cleanup = ""

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
                finally:
                    db.release_orphan_page_hits_cleanup_lock(page_hits_cleanup_lock)
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

            if cluster_cron_allowed and supports_message_queue(inst):
                queue_enabled, queue_interval = effective_message_queue_setting(config, inst)
                queue_job_name = "mautic:messages:send"
                previous = jobs_last_run.get((root, queue_job_name), 0.0)
                if queue_enabled and (previous <= 0 or now - previous >= queue_interval):
                    args = render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=queue_job_name,
                    )
                    if _submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=root,
                        task_type="job:mautic_messages_send",
                        entity_id=None,
                        args=args,
                        timeout_sec=config.command_timeout_sec,
                        max_parallel_for_type=1,
                        popens=popens,
                    ):
                        jobs_last_run[(root, queue_job_name)] = now

            if cluster_cron_allowed:
                for plugin_item in plugin_operations_for_instance(config, inst):
                    operation = plugin_item.get("operation") if isinstance(plugin_item.get("operation"), dict) else {}
                    if str(operation.get("mode", "") or "") != "scheduled":
                        continue
                    if not _plugin_operation_has_registered_bundle(db, plugin_item):
                        continue
                    operation_key = str(plugin_item.get("operation_key", "") or "")
                    values = plugin_operation_effective_values(config, inst, plugin_item)
                    for plugin_task in plugin_operation_scheduled_tasks(plugin_item):
                        task_id = str(plugin_task.get("id", "") or "")
                        run_key = f"{operation_key}:{task_id}"
                        resource_key = _plugin_operation_resource_key(operation_key, plugin_task)
                        resource_digest = _plugin_operation_resource_digest(root, resource_key)
                        previous = jobs_last_run.get((root, run_key), 0.0)
                        bootstrap = (
                            plugin_task.get("bootstrap")
                            if isinstance(plugin_task.get("bootstrap"), dict)
                            else None
                        )
                        if bootstrap is not None and bool(bootstrap.get("reset_on_enable", False)):
                            schedule = (
                                plugin_task.get("schedule")
                                if isinstance(plugin_task.get("schedule"), dict)
                                else {}
                            )
                            enabled_field = str(schedule.get("enabled_field", "enabled") or "enabled")
                            _sync_plugin_operation_bootstrap_enabled_state(
                                store,
                                _plugin_operation_bootstrap_digest(root, operation_key, task_id),
                                enabled=bool(values.get(enabled_field, True)),
                            )
                        try:
                            if not plugin_operation_schedule_due(
                                plugin_item,
                                values,
                                now_epoch=now,
                                now_local=dt,
                                last_epoch=previous,
                                task=plugin_task,
                            ):
                                continue
                            guard = plugin_task.get("guard")
                            if guard is not None and not _plugin_operation_guard_matches(db, guard, inst_now):
                                _plugin_operation_runtime_update(
                                    store,
                                    resource_digest,
                                    {
                                        "root": root,
                                        "operation_key": operation_key,
                                        "task_id": task_id,
                                        "resource_key": resource_key,
                                        "state": "skipped",
                                        "skipped_reason": "guard_not_met",
                                        "next_run_at": plugin_operation_next_run_epoch(
                                            plugin_item,
                                            values,
                                            now_epoch=now,
                                            now_local=dt,
                                            last_epoch=now,
                                            task=plugin_task,
                                        ),
                                        "heartbeat_at": now,
                                        "stale": False,
                                    },
                                )
                                jobs_last_run[(root, run_key)] = now
                                continue
                            selected_command = (
                                plugin_task.get("command")
                                if isinstance(plugin_task.get("command"), dict)
                                else {}
                            )
                            task_type = (
                                "job:plugin_operation_safety"
                                if str(plugin_task.get("task_class", "standard") or "standard") == "safety"
                                else "job:plugin_operation"
                            )
                            if bootstrap is not None:
                                digest = _plugin_operation_bootstrap_digest(root, operation_key, task_id)
                                complete_when = bootstrap.get("complete_when")
                                try:
                                    completed = _plugin_operation_bootstrap_is_completed(
                                        store,
                                        digest,
                                        db=db,
                                        complete_when=complete_when,
                                        now_local=inst_now,
                                    )
                                except Exception as exc:
                                    logging.warning(
                                        "[%s] plugin operation %s/%s bootstrap probe failed: %s",
                                        root,
                                        operation_key,
                                        task_id,
                                        exc,
                                    )
                                    completed = False
                                if not completed:
                                    selected_command = (
                                        bootstrap.get("command")
                                        if isinstance(bootstrap.get("command"), dict)
                                        else selected_command
                                    )
                                    task_type = f"job:plugin_operation_bootstrap:{digest}"
                            task_type = f"{task_type}:{resource_digest}"
                            template = plugin_operation_command_template(
                                plugin_item,
                                values,
                                task=plugin_task,
                                command=selected_command,
                            )
                            args = render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=template,
                            )
                            next_run_at = plugin_operation_next_run_epoch(
                                plugin_item,
                                values,
                                now_epoch=now,
                                now_local=dt,
                                last_epoch=now,
                                task=plugin_task,
                            )
                            runtime_base: dict[str, object] = {
                                "root": root,
                                "operation_key": operation_key,
                                "task_id": task_id,
                                "resource_key": resource_key,
                                "next_run_at": next_run_at,
                            }
                            task_key = _task_key(root, task_type, None)
                            if task_key in running or store.has_running_task_key(task_key):
                                _plugin_operation_runtime_update(
                                    store,
                                    resource_digest,
                                    {
                                        **runtime_base,
                                        "state": "skipped",
                                        "skipped_reason": f"resource_busy:{resource_key}",
                                        "heartbeat_at": now,
                                        "stale": False,
                                    },
                                )
                                jobs_last_run[(root, run_key)] = now
                                continue
                            stdout_path, stderr_path = _plugin_operation_output_paths(config, resource_digest)
                            if _submit_if_slot(
                                config=config,
                                store=store,
                                running=running,
                                root=root,
                                task_type=task_type,
                                entity_id=None,
                                args=args,
                                timeout_sec=config.command_timeout_sec,
                                max_parallel_for_type=1,
                                popens=popens,
                                bypass_repeat_guard=True,
                                stdout_path=stdout_path,
                                stderr_path=stderr_path,
                            ):
                                jobs_last_run[(root, run_key)] = now
                                launched = running.get(task_key)
                                _plugin_operation_runtime_update(
                                    store,
                                    resource_digest,
                                    {
                                        **runtime_base,
                                        "state": "running",
                                        "last_started_at": now,
                                        "last_finished_at": None,
                                        "rc": None,
                                        "duration_sec": None,
                                        "stdout": "",
                                        "stderr": "",
                                        "skipped_reason": "",
                                        "heartbeat_at": now,
                                        "owner_pid": int(launched.pid if launched is not None else 0),
                                        "stale": False,
                                        "_stdout_path": stdout_path,
                                        "_stderr_path": stderr_path,
                                    },
                                )
                            else:
                                _plugin_operation_runtime_update(
                                    store,
                                    resource_digest,
                                    {
                                        **runtime_base,
                                        "state": "skipped",
                                        "skipped_reason": f"resource_busy:{resource_key}",
                                        "heartbeat_at": now,
                                        "stale": False,
                                    },
                                )
                                jobs_last_run[(root, run_key)] = now
                        except Exception as exc:
                            logging.warning(
                                "[%s] plugin operation %s/%s schedule failed: %s",
                                root,
                                operation_key,
                                task_id,
                                exc,
                            )
            for job in config.scheduled_jobs:
                if not cluster_cron_allowed:
                    break
                if not job.enabled:
                    continue
                if supports_message_queue(inst) and _is_mautic_message_send_template(job.command_template):
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

        if pusher.enabled():
            try:
                monitor_signals = collect_monitor_signals(
                    config,
                    mail_queue_metrics=mail_queue_metrics,
                )
                if pusher.should_push_monitor_signals(time.time(), monitor_signals):
                    ok, msg = pusher.send_signals(monitor_signals)
                    if ok:
                        last_monitor_signals_push_error = ""
                    else:
                        reason = str(msg or "unknown")
                        if reason != last_monitor_signals_push_error:
                            logging.warning("monitor signals push failed: %s", reason)
                            last_monitor_signals_push_error = reason
            except Exception as e:
                reason = str(e)
                if reason != last_monitor_signals_push_error:
                    logging.warning("monitor signals push failed: %s", reason)
                    last_monitor_signals_push_error = reason

        if single_cycle:
            _ACTIVE_PRIORITY_EXECUTOR = None
            _SEGMENT_DISPATCH_WAKEUP = None
            return
        segment_dispatch_wakeup.wait(timeout=max(0.1, float(config.dispatch_interval_sec)))
        segment_dispatch_wakeup.clear()
