from __future__ import annotations

from dataclasses import asdict
from datetime import timezone, datetime
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import socket
import subprocess
import time
from typing import Any, Mapping
import uuid
from urllib.parse import parse_qsl, urlsplit
from urllib import request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

import pymysql
from pymysql.cursors import DictCursor

from mcd_agent import __version__
from mcd_agent.apt_profile import collect_apt_state
from mcd_agent.backup import backup_profile_for_push, backup_profile_sync_payload, backup_state_for_push
from mcd_agent.cluster_assets import collect_cluster_assets_status
from mcd_agent.config import AgentConfig
from mcd_agent.form_embed import load_form_embed_state
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.mcd_control import ControlStore, SCHEMA
from mcd_agent.result_signing import ensure_result_key
from mcd_agent.result_signing import sign_result
from mcd_agent.docker_preflight_runner import DockerRuntimePreflightRunner
from mcd_agent.docker_image_inventory import capability as docker_image_inventory_capability
from mcd_agent.docker_image_inventory import collect_signed_inventory
from mcd_agent.commercial_portal import _host_fingerprint
from mcd_agent.install_readiness import collect_mautic_install_readiness
from mcd_agent.instance_size import collect_instance_sizes
from mcd_agent.install_type import detect_install_type, is_complete_plugin_bundle, plugin_dir_candidates
from mcd_agent.plugin_inventory import (
    PLUGIN_INVENTORY_PENDING_KEY,
    PLUGIN_INVENTORY_SYNC_SCHEMA,
    canonical_inventory,
    inventory_sha256,
    observation as plugin_inventory_observation,
    runtime_key as plugin_inventory_runtime_key,
    sync_payload as plugin_inventory_sync_payload,
    utc_now,
)
from mcd_agent.runtime_matrix import build_runtime_profile
from mcd_agent.inventory import InstanceInventory, MauticInstall, ensure_seeded
from mcd_agent.logical_issues import read_logical_issues_snapshot
from mcd_agent.maintenance_mode import collect_maintenance_state
from mcd_agent.mautic_version_cache import collect_mautic_version
from mcd_agent.message_queue import collect_message_queue_snapshot
from mcd_agent.runtime_overrides import local_runtime_overrides
from mcd_agent.state_backend import (
    mark_outbound_event_mysql,
    mark_state_snapshot_push_result_mysql,
    prune_outbound_events_mysql,
    queue_outbound_event_mysql,
    read_pending_outbound_event_mysql,
    state_backend_status,
    upsert_state_snapshot_mysql,
)
from mcd_agent.version_identity import agent_version_payload

_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle$")
_LOCAL_SOCKET_CANDIDATES: tuple[str, ...] = (
    "/var/run/mysqld/mysqld.sock",
    "/run/mysqld/mysqld.sock",
)
_PROFILE_EVENT_EMPTY_UNTIL: dict[str, float] = {}
_MYSQL_WARN_THROTTLE: dict[str, dict[str, Any]] = {}
_MYSQL_WARN_THROTTLE_SEC = 300
_DEFAULT_STATE_PUSH_TIMEOUT_SEC = 20
_CAMPAIGN_NATIVE_FALLBACK_RETENTION_SEC = 30 * 86400
_CAMPAIGN_NATIVE_FALLBACK_MAX_EVENTS = 5000


def _galera_routing_eligibility(galera: dict[str, Any]) -> tuple[bool, str]:
    """Return whether this Galera node is safe for new DB traffic/source use."""
    ready = _to_bool(galera.get("ready"))
    connected = _to_bool(galera.get("connected"))
    cluster_status = str(galera.get("cluster_status") or "").strip().lower()
    local_state = str(galera.get("local_state_comment") or "").strip().lower()

    if not ready:
        return False, "wsrep_ready_off"
    if not connected:
        return False, "wsrep_disconnected"
    if cluster_status != "primary":
        return False, "cluster_not_primary"
    if local_state != "synced":
        return False, "node_not_synced"
    return True, "primary_synced_ready"


def _profile_event_cache_key(cfg: AgentConfig) -> str:
    return "|".join(
        [
            str(getattr(cfg, "state_db_path", "") or "").strip(),
            str(getattr(cfg, "mcc_host_name", "") or "").strip(),
            str(getattr(cfg, "state_mysql_host", "") or "").strip(),
            str(getattr(cfg, "state_mysql_database", "") or "").strip(),
        ]
    )


def _normalize_mysql_warning(msg: str) -> str:
    raw = str(msg or "").strip()
    if raw.startswith("mysql_backoff_active:"):
        parts = raw.split(":", 2)
        if len(parts) >= 3:
            # Ignore dynamic retry countdown in warning de-duplication key.
            return f"mysql_backoff_active:{parts[2]}"
        return "mysql_backoff_active"
    return raw


def _should_log_mysql_warning(
    cfg: AgentConfig,
    *,
    bucket: str,
    msg: str,
    min_interval_sec: int = _MYSQL_WARN_THROTTLE_SEC,
) -> bool:
    key = f"{_profile_event_cache_key(cfg)}|{bucket}"
    now_ts = time.time()
    norm = _normalize_mysql_warning(msg)
    row = _MYSQL_WARN_THROTTLE.get(key, {})
    last_ts = float(row.get("ts") or 0.0)
    last_norm = str(row.get("norm") or "")
    if last_norm == norm and (now_ts - last_ts) < max(30, int(min_interval_sec or 300)):
        return False
    _MYSQL_WARN_THROTTLE[key] = {"ts": now_ts, "norm": norm}
    return True


def _is_valid_bundle_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n.lower() in {"plugin", "plugins"}:
        return False
    return bool(_BUNDLE_NAME_RE.match(n))


def _hash_payload(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def stable_change_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload copy used for change detection, without volatile sample timestamps."""
    out = dict(payload)
    out.pop("sent_at_utc", None)
    for key in ("maintenance_state", "mautic_install_readiness"):
        raw = out.get(key)
        if isinstance(raw, dict):
            cleaned = dict(raw)
            cleaned.pop("checked_at_utc", None)
            out[key] = cleaned
    out.pop("signals_collected_at_ts", None)

    # Plugin observations are collected on every state-push cycle. Their
    # observation timestamps describe freshness, but must not make an
    # otherwise unchanged payload look changed (especially when
    # mcc_push_on_change is enabled). The digest/status/error fields remain in
    # the change key, so a real inventory transition still pushes immediately.
    observations = out.get("plugin_inventory_observations")
    if isinstance(observations, list):
        out["plugin_inventory_observations"] = [
            {
                key: value
                for key, value in item.items()
                if key not in {"observed_at_utc", "last_known_observed_at_utc"}
            }
            if isinstance(item, dict)
            else item
            for item in observations
        ]
    sync = out.get("plugin_inventory_sync")
    if isinstance(sync, dict):
        stable_sync = dict(sync)
        stable_sync.pop("requested_at_utc", None)
        stable_sync.pop("last_attempt_at_utc", None)
        out["plugin_inventory_sync"] = stable_sync
    instances = out.get("instances")
    if isinstance(instances, list):
        stable_instances = []
        for item in instances:
            if not isinstance(item, dict):
                stable_instances.append(item)
                continue
            stable_item = dict(item)
            plugin_inventory = stable_item.get("plugin_inventory")
            if isinstance(plugin_inventory, dict):
                stable_plugin_inventory = dict(plugin_inventory)
                stable_plugin_inventory.pop("observed_at_utc", None)
                stable_plugin_inventory.pop("last_known_observed_at_utc", None)
                stable_item["plugin_inventory"] = stable_plugin_inventory
            stable_instances.append(stable_item)
        out["instances"] = stable_instances
    return out


def _advertise_image_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    """Advertise through both host state and its preserved readiness envelope."""
    from mcd_agent.image_compatibility import capability

    advertised = capability()
    payload["image_compatibility_capability"] = advertised
    readiness = payload.get("mautic_install_readiness")
    readiness_payload = dict(readiness) if isinstance(readiness, dict) else {}
    readiness_payload["image_compatibility_capability"] = advertised
    payload["mautic_install_readiness"] = readiness_payload
    return advertised


def _runtime_overrides_for_state_push(cfg: AgentConfig) -> dict[str, Any]:
    """Include managed form policy outcomes in the normal observed snapshot."""
    runtime = dict(local_runtime_overrides(cfg))
    state = load_form_embed_state()
    statuses = state.get("instances", {}) if isinstance(state, dict) else {}
    runtime["form_embed_instance_status"] = statuses if isinstance(statuses, dict) else {}
    return runtime


def state_identity_payload(cfg: AgentConfig) -> dict[str, str]:
    """Return the stable identity fields required by the mcd-state-v1 contract."""
    return {
        "agent_id": str(getattr(cfg, "agent_id", "") or "").strip(),
        "host_fingerprint": _host_fingerprint(),
    }


def _ingest_control_response(
    state_db_path: str,
    payload: dict[str, Any],
    control: dict[str, Any],
) -> None:
    """Persist MCC key ACKs even when no control-command envelope is present."""
    nested = control.get("control")
    if control.get("schema") != SCHEMA and isinstance(nested, dict):
        control = nested
    if control.get("schema") != SCHEMA:
        return
    store = ControlStore(state_db_path)
    commands = control.get("commands")
    if isinstance(commands, list) and control.get("contract_version") == "1.0":
        try:
            store.ingest(
                control,
                agent_id=str(payload.get("agent_id") or ""),
                host_fingerprint=str(payload.get("host_fingerprint") or ""),
            )
        except ValueError:
            # Invalid command envelopes must not suppress an otherwise valid key ACK.
            pass
    ack = control.get("result_signing_key_ack")
    result_key = payload.get("result_signing_key")
    if (
        isinstance(ack, dict)
        and ack.get("status") in {"active", "retired", "revoked"}
        and isinstance(result_key, dict)
        and ack.get("key_id") == result_key.get("key_id")
    ):
        store.record_key_ack(ack)


def _reconcile_control_receipt_conflict(state_db_path: str, response_text: str) -> int:
    """Retire only terminal receipts rejected by MCC's immutable-row 409."""
    text = str(response_text or "")
    try:
        envelope = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return 0
    if not isinstance(envelope, dict):
        return 0
    detail = envelope.get("detail") if isinstance(envelope.get("detail"), dict) else envelope
    if detail.get("code") != "immutable_terminal_conflict":
        return 0
    job_id = detail.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"docker-preflight-[A-Za-z0-9._-]+", job_id):
        return 0
    store = ControlStore(state_db_path)
    return int(store.acknowledge_terminal_conflict(
        job_id,
        reason="control receipt conflicts with immutable terminal command",
        remote_response=text,
    ))


def monitor_signals_change_payload(payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    scheduler = details.get("scheduler") if isinstance(details.get("scheduler"), dict) else {}
    return {
        "scheduler": scheduler,
        "php_console_recent": details.get("php_console_recent") if isinstance(details.get("php_console_recent"), list) else [],
    }


def _read_config_text(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def _legacy_profile_event_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "profile-event.pending.json"


def _state_conn(cfg: AgentConfig) -> sqlite3.Connection:
    Path(cfg.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.state_db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          try_count INTEGER NOT NULL DEFAULT 0,
          last_try_at REAL,
          created_at REAL NOT NULL,
          sent_at REAL,
          last_error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_events_status ON outbound_events(status, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_native_fallback_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          root TEXT NOT NULL,
          created_at REAL NOT NULL,
          payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_native_fallback_events_created "
        "ON campaign_native_fallback_events(created_at DESC, id DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_native_fallback_stats (
          root TEXT PRIMARY KEY,
          runs INTEGER NOT NULL DEFAULT 0,
          native_errors INTEGER NOT NULL DEFAULT 0,
          metric_errors INTEGER NOT NULL DEFAULT 0,
          recovered_runs INTEGER NOT NULL DEFAULT 0,
          recovered_pending INTEGER NOT NULL DEFAULT 0,
          recovered_email_stats INTEGER NOT NULL DEFAULT 0,
          first_run_at REAL NOT NULL,
          last_run_at REAL NOT NULL,
          last_status TEXT NOT NULL,
          last_failed_stage TEXT NOT NULL DEFAULT '',
          last_operation_rc INTEGER,
          last_metrics_status TEXT NOT NULL DEFAULT 'unknown',
          last_metrics_error TEXT NOT NULL DEFAULT '',
          last_pending_before INTEGER,
          last_pending_after INTEGER,
          last_email_stats_before INTEGER,
          last_email_stats_after INTEGER
        )
        """
    )
    fallback_stat_columns = {
        str(row["name"] or "")
        for row in conn.execute("PRAGMA table_info(campaign_native_fallback_stats)").fetchall()
    }
    if "recovered_runs" not in fallback_stat_columns:
        conn.execute(
            "ALTER TABLE campaign_native_fallback_stats "
            "ADD COLUMN recovered_runs INTEGER NOT NULL DEFAULT 0"
        )
    _backfill_campaign_native_fallback_recovered_runs(conn)
    _migrate_legacy_profile_event_file(conn, cfg)
    return conn


def _nullable_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _backfill_campaign_native_fallback_recovered_runs(conn: sqlite3.Connection) -> None:
    """Backfill the 0.9.284 counter once from retained durable events."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state_schema_migrations (
          name TEXT PRIMARY KEY,
          applied_at REAL NOT NULL
        )
        """
    )
    migration = "campaign_native_fallback_recovered_runs_v1"
    if conn.execute("SELECT 1 FROM state_schema_migrations WHERE name = ?", (migration,)).fetchone():
        return

    recovered_by_root: dict[str, int] = {}
    for row in conn.execute(
        "SELECT root, payload_json FROM campaign_native_fallback_events ORDER BY id"
    ).fetchall():
        root = str(row["root"] or "").strip()
        if not root:
            continue
        try:
            event = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        pending_before = _nullable_nonnegative_int(event.get("pending_before"))
        pending_after = _nullable_nonnegative_int(event.get("pending_after"))
        email_before = _nullable_nonnegative_int(event.get("email_stats_before"))
        email_after = _nullable_nonnegative_int(event.get("email_stats_after"))
        recovered_pending = (
            max(0, pending_before - pending_after)
            if pending_before is not None and pending_after is not None
            else 0
        )
        recovered_email_stats = (
            max(0, email_after - email_before)
            if email_before is not None and email_after is not None
            else 0
        )
        if recovered_pending > 0 or recovered_email_stats > 0:
            recovered_by_root[root] = recovered_by_root.get(root, 0) + 1

    for root, recovered_runs in recovered_by_root.items():
        conn.execute(
            """
            UPDATE campaign_native_fallback_stats
            SET recovered_runs = MAX(recovered_runs, ?)
            WHERE root = ?
            """,
            (recovered_runs, root),
        )
    conn.execute(
        "INSERT INTO state_schema_migrations(name, applied_at) VALUES (?, ?)",
        (migration, time.time()),
    )
    conn.commit()


def _store_campaign_native_fallback_event(cfg: AgentConfig, event: dict[str, Any], now_ts: float) -> None:
    root = str(event.get("root", "") or "").strip()
    if not root:
        return
    status = str(event.get("status", "") or "unknown").strip() or "unknown"
    failed_stage = str(event.get("failed_stage", "") or "").strip()[:80]
    metrics_status = str(event.get("metrics_status", "") or "unknown").strip() or "unknown"
    metrics_error = str(event.get("metrics_error", "") or "").strip()[:500]
    operation_rc = event.get("operation_rc")
    try:
        operation_rc = int(operation_rc) if operation_rc is not None else None
    except (TypeError, ValueError):
        operation_rc = None
    pending_before = _nullable_nonnegative_int(event.get("pending_before"))
    pending_after = _nullable_nonnegative_int(event.get("pending_after"))
    email_before = _nullable_nonnegative_int(event.get("email_stats_before"))
    email_after = _nullable_nonnegative_int(event.get("email_stats_after"))
    recovered_pending = (
        max(0, pending_before - pending_after)
        if pending_before is not None and pending_after is not None
        else 0
    )
    recovered_email_stats = (
        max(0, email_after - email_before)
        if email_before is not None and email_after is not None
        else 0
    )
    recovered_run = int(recovered_pending > 0 or recovered_email_stats > 0)
    native_error = 0 if status == "ok" and operation_rc == 0 else 1
    metric_error = 0 if metrics_status == "ok" else 1

    conn = _state_conn(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO campaign_native_fallback_events(root, created_at, payload_json) VALUES (?, ?, ?)",
            (root, now_ts, json.dumps(event, ensure_ascii=True, separators=(",", ":"))),
        )
        conn.execute(
            """
            INSERT INTO campaign_native_fallback_stats (
              root, runs, native_errors, metric_errors, recovered_runs, recovered_pending,
              recovered_email_stats, first_run_at, last_run_at, last_status,
              last_failed_stage, last_operation_rc, last_metrics_status,
              last_metrics_error, last_pending_before, last_pending_after,
              last_email_stats_before, last_email_stats_after
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(root) DO UPDATE SET
              runs = campaign_native_fallback_stats.runs + 1,
              native_errors = campaign_native_fallback_stats.native_errors + excluded.native_errors,
              metric_errors = campaign_native_fallback_stats.metric_errors + excluded.metric_errors,
              recovered_runs = campaign_native_fallback_stats.recovered_runs + excluded.recovered_runs,
              recovered_pending = campaign_native_fallback_stats.recovered_pending + excluded.recovered_pending,
              recovered_email_stats = campaign_native_fallback_stats.recovered_email_stats + excluded.recovered_email_stats,
              last_run_at = excluded.last_run_at,
              last_status = excluded.last_status,
              last_failed_stage = excluded.last_failed_stage,
              last_operation_rc = excluded.last_operation_rc,
              last_metrics_status = excluded.last_metrics_status,
              last_metrics_error = excluded.last_metrics_error,
              last_pending_before = excluded.last_pending_before,
              last_pending_after = excluded.last_pending_after,
              last_email_stats_before = excluded.last_email_stats_before,
              last_email_stats_after = excluded.last_email_stats_after
            """,
            (
                root,
                native_error,
                metric_error,
                recovered_run,
                recovered_pending,
                recovered_email_stats,
                now_ts,
                now_ts,
                status,
                failed_stage,
                operation_rc,
                metrics_status,
                metrics_error,
                pending_before,
                pending_after,
                email_before,
                email_after,
            ),
        )
        conn.execute(
            "DELETE FROM campaign_native_fallback_events WHERE created_at < ?",
            (now_ts - _CAMPAIGN_NATIVE_FALLBACK_RETENTION_SEC,),
        )
        conn.execute(
            """
            DELETE FROM campaign_native_fallback_events
            WHERE id IN (
              SELECT id FROM campaign_native_fallback_events
              ORDER BY created_at DESC, id DESC
              LIMIT -1 OFFSET ?
            )
            """,
            (_CAMPAIGN_NATIVE_FALLBACK_MAX_EVENTS,),
        )
        conn.commit()
    finally:
        conn.close()


def _read_campaign_native_fallback_events(cfg: AgentConfig, limit: int = 50) -> list[dict[str, Any]]:
    conn = _state_conn(cfg)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM campaign_native_fallback_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(500, int(limit or 50))),),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _read_campaign_native_fallback_stats(cfg: AgentConfig) -> list[dict[str, Any]]:
    conn = _state_conn(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM campaign_native_fallback_stats ORDER BY root ASC"
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("first_run_at", "last_run_at"):
            ts = float(item.get(key) or 0.0)
            item[key] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts > 0 else ""
        out.append(item)
    return out


def _campaign_native_fallback_last_run_ts(cfg: AgentConfig, root: str) -> float:
    conn = _state_conn(cfg)
    try:
        row = conn.execute(
            "SELECT last_run_at FROM campaign_native_fallback_stats WHERE root = ?",
            (str(root or "").strip(),),
        ).fetchone()
    finally:
        conn.close()
    return float(row["last_run_at"] or 0.0) if row is not None else 0.0


def _migrate_legacy_profile_event_file(conn: sqlite3.Connection, cfg: AgentConfig) -> None:
    p = _legacy_profile_event_path(cfg)
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            p.unlink(missing_ok=True)
            return
    except Exception:
        try:
            p.unlink()
        except Exception:
            pass
        return
    event_id = str(raw.get("event_id", "")).strip() or uuid.uuid4().hex
    payload = dict(raw)
    payload["event_id"] = event_id
    now_ts = time.time()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_events
            (event_id, event_type, payload_json, status, try_count, created_at)
            VALUES (?, 'profile_event', ?, 'pending', 0, ?)
            """,
            (event_id, json.dumps(payload, ensure_ascii=True, separators=(",", ":")), now_ts),
        )
        conn.commit()
    finally:
        try:
            p.unlink()
        except Exception:
            pass


def queue_profile_event(
    cfg: AgentConfig,
    *,
    source: str,
    initiated_by_user: bool,
    old_profile: str | None,
    new_profile: str | None,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_ts = time.time()
    _PROFILE_EVENT_EMPTY_UNTIL.pop(_profile_event_cache_key(cfg), None)
    payload: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "source": (source or "mcd_cli"),
        "initiated_by_user": bool(initiated_by_user),
        "old_profile": (old_profile or "").strip() or None,
        "new_profile": (new_profile or "").strip() or None,
        "reason": (reason or "profile_set"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if isinstance(details, dict) and details:
        payload["details"] = details
    event_id = str(payload.get("event_id", "")).strip()
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    mysql_ok, mysql_msg = queue_outbound_event_mysql(
        cfg,
        event_type="profile_event",
        event_id=event_id,
        payload_json=payload_json,
        created_at=now_ts,
    )
    if not mysql_ok and mysql_msg not in {"mysql_state_disabled"}:
        if _should_log_mysql_warning(cfg, bucket="queue", msg=mysql_msg):
            logging.warning("outbound_events mysql queue failed, fallback to sqlite: %s", mysql_msg)
    if not mysql_ok:
        conn = _state_conn(cfg)
        try:
            conn.execute(
                """
                INSERT INTO outbound_events
                (event_id, event_type, payload_json, status, try_count, created_at)
                VALUES (?, 'profile_event', ?, 'pending', 0, ?)
                """,
                (
                    event_id,
                    payload_json,
                    now_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return payload


def read_pending_profile_event(cfg: AgentConfig) -> dict[str, Any] | None:
    now_ts = time.time()
    cache_key = _profile_event_cache_key(cfg)
    empty_until = float(_PROFILE_EVENT_EMPTY_UNTIL.get(cache_key, 0.0) or 0.0)
    if now_ts < empty_until:
        return None

    raw_mysql, mysql_msg = read_pending_outbound_event_mysql(cfg, event_type="profile_event")
    if isinstance(raw_mysql, dict):
        _PROFILE_EVENT_EMPTY_UNTIL.pop(cache_key, None)
        return raw_mysql
    if mysql_msg not in {"mysql_state_disabled", "empty"}:
        if _should_log_mysql_warning(cfg, bucket="read", msg=mysql_msg):
            logging.warning("outbound_events mysql read failed, fallback to sqlite: %s", mysql_msg)

    conn = _state_conn(cfg)
    try:
        row = conn.execute(
            """
            SELECT event_id, payload_json
            FROM outbound_events
            WHERE event_type='profile_event' AND status IN ('pending','failed')
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            cooldown = max(5, min(60, int(getattr(cfg, "mcc_push_interval_sec", 10) or 10)))
            _PROFILE_EVENT_EMPTY_UNTIL[cache_key] = now_ts + float(cooldown)
            return None
        payload_json = str(row["payload_json"] or "")
        raw = json.loads(payload_json)
        if isinstance(raw, dict):
            _PROFILE_EVENT_EMPTY_UNTIL.pop(cache_key, None)
            return raw
        # Invalid payload in queue: mark failed and keep diagnostics.
        conn.execute(
            """
            UPDATE outbound_events
            SET status='failed', try_count=try_count+1, last_try_at=?, last_error=?
            WHERE event_id=?
            """,
            (time.time(), "invalid_payload_type", str(row["event_id"] or "")),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    cooldown = max(5, min(60, int(getattr(cfg, "mcc_push_interval_sec", 10) or 10)))
    _PROFILE_EVENT_EMPTY_UNTIL[cache_key] = now_ts + float(cooldown)
    return None


def clear_pending_profile_event(
    cfg: AgentConfig,
    *,
    event_id: str | None = None,
    delivered: bool = True,
    error: str | None = None,
) -> None:
    target_id = (event_id or "").strip()
    if not target_id:
        return
    _PROFILE_EVENT_EMPTY_UNTIL.pop(_profile_event_cache_key(cfg), None)
    mysql_ok, mysql_msg = mark_outbound_event_mysql(
        cfg,
        event_id=target_id,
        delivered=delivered,
        error=error,
    )
    if not mysql_ok and mysql_msg not in {"mysql_state_disabled"}:
        if _should_log_mysql_warning(cfg, bucket="mark", msg=mysql_msg):
            logging.warning("outbound_events mysql mark failed, fallback sqlite only: %s", mysql_msg)

    conn = _state_conn(cfg)
    now_ts = time.time()
    try:
        if delivered:
            conn.execute(
                """
                UPDATE outbound_events
                SET status='sent', sent_at=?, last_try_at=?, last_error=NULL
                WHERE event_id=?
                """,
                (now_ts, now_ts, target_id),
            )
        else:
            conn.execute(
                """
                UPDATE outbound_events
                SET status='failed', try_count=try_count+1, last_try_at=?, last_error=?
                WHERE event_id=?
                """,
                (now_ts, (error or "delivery_failed")[:2000], target_id),
            )
        conn.commit()
    finally:
        conn.close()


def prune_sent_profile_events(cfg: AgentConfig, *, keep_days: int = 14) -> int:
    keep_sec = max(1, int(keep_days)) * 86400
    cutoff = time.time() - keep_sec
    mysql_deleted, mysql_msg = prune_outbound_events_mysql(
        cfg,
        event_type="profile_event",
        cutoff_ts=cutoff,
    )
    if mysql_msg not in {"ok", "mysql_state_disabled"}:
        if _should_log_mysql_warning(cfg, bucket="prune", msg=mysql_msg):
            logging.warning("outbound_events mysql prune failed, sqlite only: %s", mysql_msg)

    conn = _state_conn(cfg)
    try:
        cur = conn.execute(
            "DELETE FROM outbound_events WHERE event_type='profile_event' AND status='sent' AND COALESCE(sent_at, created_at) < ?",
            (cutoff,),
        )
        conn.commit()
        return int(mysql_deleted or 0) + int(cur.rowcount or 0)
    finally:
        conn.close()


def _extract_version_from_php_text(text: str) -> str:
    m = re.search(r"['\"]version['\"]\s*=>\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"['\"]version['\"]\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "-"


def _to_bool(v: Any) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"1", "on", "yes", "true"}:
        return True
    if s in {"0", "off", "no", "false"}:
        return False
    return None


def _to_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(str(v).strip())
    except Exception:
        return None


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(str(v).strip())
    except Exception:
        return None


def _is_local_mysql_host(host: str | None) -> bool:
    raw = str(host or "").strip().lower()
    return raw in {"", "localhost", "127.0.0.1", "::1"}


def _pick_local_socket(password: str | None, host: str | None) -> str | None:
    if str(password or "") != "":
        return None
    if not _is_local_mysql_host(host):
        return None
    for cand in _LOCAL_SOCKET_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def _configured_or_auto_local_socket(cfg: AgentConfig, *, password: str | None, host: str | None) -> str | None:
    explicit = str(getattr(cfg, "state_mysql_unix_socket", "") or "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    return _pick_local_socket(password=password, host=host)


def _run_quick(cmd: list[str], *, timeout_sec: int = 3) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
        return int(proc.returncode), (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:
        return 124, "", str(e)


def _collect_haproxy_db_state(*, timeout_sec: int = 3) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "na",
        "service": "missing",
        "mode": "na",  # local|backup|remote|down|unknown|na
        "backend": None,
        "local_server": None,
        "local_status": None,
        "backup_up": [],
        "up_servers": [],
        "servers": [],
        "error": "",
    }

    has_cfg = os.path.exists("/etc/haproxy/haproxy.cfg")
    has_bin = shutil.which("haproxy") is not None
    if not has_cfg and not has_bin:
        return out

    rc, stdout, stderr = _run_quick(["systemctl", "is-active", "haproxy"], timeout_sec=2)
    service_state = (stdout or stderr or "unknown").strip().lower()
    out["service"] = service_state
    if rc != 0 or service_state != "active":
        out["status"] = "error"
        out["mode"] = "down"
        out["error"] = "service_not_active"
        return out

    sock_path = "/run/haproxy/admin.sock"
    if not os.path.exists(sock_path):
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = "stats_socket_missing"
        return out

    raw = ""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(max(1.0, float(timeout_sec)))
            s.connect(sock_path)
            s.sendall(b"show stat\n")
            chunks: list[bytes] = []
            while True:
                try:
                    b = s.recv(65536)
                except socket.timeout:
                    break
                if not b:
                    break
                chunks.append(b)
            raw = b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception as e:
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = f"stats_socket_read_failed: {e}"
        return out

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    header_idx = -1
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            header_idx = i
            break
    if header_idx < 0:
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = "stats_header_missing"
        return out

    csv_text = lines[header_idx][2:] + "\n" + "\n".join(lines[header_idx + 1 :])
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows: list[dict[str, str]] = []
        for row in reader:
            if not isinstance(row, dict):
                continue
            norm = {str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None}
            if norm.get("pxname"):
                rows.append(norm)
    except Exception as e:
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = f"stats_parse_failed: {e}"
        return out

    srv_rows = [
        r
        for r in rows
        if str(r.get("svname", "")).upper() not in {"", "FRONTEND", "BACKEND"}
    ]
    if not srv_rows:
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = "no_backend_servers"
        return out

    by_backend: dict[str, list[dict[str, str]]] = {}
    for r in srv_rows:
        b = str(r.get("pxname", "")).strip()
        if not b:
            continue
        by_backend.setdefault(b, []).append(r)
    if not by_backend:
        out["status"] = "degraded"
        out["mode"] = "unknown"
        out["error"] = "empty_backend_map"
        return out

    mysql_backends = [b for b in by_backend.keys() if "mysql" in b.lower()]
    backend = (mysql_backends[0] if mysql_backends else sorted(by_backend.keys())[0]).strip()
    rows_sel = by_backend.get(backend, [])
    out["backend"] = backend

    def _srv_up(st: str) -> bool:
        u = str(st or "").strip().upper()
        return u.startswith("UP") or u in {"OPEN", "L7OK", "L4OK"}

    local_row: dict[str, Any] | None = None
    backup_up: list[str] = []
    up_servers: list[str] = []
    servers_view: list[dict[str, Any]] = []
    for r in rows_sel:
        sv = str(r.get("svname", "")).strip()
        st = str(r.get("status", "")).strip()
        addr = str(r.get("addr", "")).strip()
        bck = str(r.get("bck", "")).strip()
        act = str(r.get("act", "")).strip()
        item: dict[str, Any] = {
            "name": sv,
            "status": st,
            "addr": addr,
            "bck": bck,
            "act": act,
            "check_status": str(r.get("check_status", "")).strip(),
            "lastchg": _to_int(r.get("lastchg")),
            "scur": _to_int(r.get("scur")),
        }
        servers_view.append(item)
        is_up = _srv_up(st)
        if is_up:
            up_servers.append(sv)
        if bck == "1" and is_up:
            backup_up.append(sv)
        is_local = ("local" in sv.lower()) or addr.startswith("127.0.0.1") or addr.startswith("localhost")
        if is_local and local_row is None:
            local_row = item

    out["servers"] = servers_view
    out["backup_up"] = backup_up
    out["up_servers"] = up_servers
    if local_row is not None:
        out["local_server"] = local_row.get("name")
        out["local_status"] = local_row.get("status")

    local_up = bool(local_row and _srv_up(str(local_row.get("status", ""))))
    if local_up:
        out["status"] = "ok"
        out["mode"] = "local"
    elif backup_up:
        out["status"] = "degraded"
        out["mode"] = "backup"
    elif up_servers:
        out["status"] = "degraded"
        out["mode"] = "remote"
    else:
        out["status"] = "error"
        out["mode"] = "down"
    return out


def _collect_gluster_state(*, timeout_sec: int = 5) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "na",
        "service": "missing",
        "volumes_total": 0,
        "volumes_started": 0,
        "volumes": [],
        "peers_total": 0,
        "peers_connected": 0,
        "mounts_total": 0,
        "mounts": [],
        "error": "",
    }

    gluster_bin = shutil.which("gluster")
    if not gluster_bin:
        return out

    rc, stdout, stderr = _run_quick(["systemctl", "is-active", "glusterd"], timeout_sec=2)
    service_state = (stdout or stderr or "unknown").strip().lower()
    out["service"] = service_state
    if rc != 0 or service_state != "active":
        out["status"] = "error"
        out["error"] = "glusterd_not_active"
        return out

    # Gluster mounts (client-side view)
    mounts: list[str] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) < 3:
                    continue
                fstype = parts[2].strip().lower()
                if "gluster" in fstype:
                    mounts.append(parts[1].strip())
    except Exception:
        mounts = []
    out["mounts"] = mounts
    out["mounts_total"] = len(mounts)

    # Volume info
    rc, vol_xml, vol_err = _run_quick([gluster_bin, "volume", "info", "--xml"], timeout_sec=timeout_sec)
    if rc != 0 or not vol_xml:
        out["status"] = "degraded"
        out["error"] = f"volume_info_failed: {vol_err or rc}"
        return out
    try:
        root = ET.fromstring(vol_xml)
        op_ret = int((root.findtext("opRet") or "0").strip())
    except Exception as e:
        out["status"] = "degraded"
        out["error"] = f"volume_xml_parse_failed: {e}"
        return out
    if op_ret != 0:
        out["status"] = "degraded"
        out["error"] = "volume_info_opret_nonzero"
        return out

    volumes: list[dict[str, Any]] = []
    for v in root.findall(".//volumes/volume"):
        name = str(v.findtext("name") or "").strip()
        st_str = str(v.findtext("statusStr") or v.findtext("status") or "").strip()
        bricks = v.findall(".//bricks/brick")
        volumes.append(
            {
                "name": name,
                "status": st_str,
                "bricks": len(bricks),
            }
        )
    out["volumes"] = volumes
    out["volumes_total"] = len(volumes)
    started = 0
    for v in volumes:
        s = str(v.get("status", "")).strip().lower()
        if s.startswith("start") or s in {"1", "started"}:
            started += 1
    out["volumes_started"] = started

    # Peer status (best-effort)
    peers_total = 0
    peers_connected = 0
    rc, peer_xml, _peer_err = _run_quick([gluster_bin, "peer", "status", "--xml"], timeout_sec=timeout_sec)
    if rc == 0 and peer_xml:
        try:
            prow = ET.fromstring(peer_xml)
            for p in prow.findall(".//peerStatus/peer"):
                peers_total += 1
                connected_val = str(p.findtext("connected") or "").strip().lower()
                state_val = str(p.findtext("state") or "").strip()
                state_str = str(p.findtext("stateStr") or "").strip().lower()
                is_connected = connected_val in {"1", "yes", "true", "on"} or state_val == "3" or "connected" in state_str
                if is_connected:
                    peers_connected += 1
        except Exception:
            pass
    out["peers_total"] = peers_total
    out["peers_connected"] = peers_connected

    if out["volumes_total"] == 0:
        out["status"] = "degraded"
    else:
        all_started = out["volumes_started"] == out["volumes_total"]
        peers_ok = (peers_total == 0) or (peers_connected == peers_total)
        if all_started and peers_ok:
            out["status"] = "ok"
        else:
            out["status"] = "degraded"
    return out


_MYSQL_IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")


def _quote_mysql_ident(value: object) -> str:
    raw = str(value or "").strip()
    if not _MYSQL_IDENT_RE.match(raw):
        raise ValueError(f"invalid MySQL identifier: {raw!r}")
    return "`" + raw.replace("`", "``") + "`"


def _mysql_datetime_to_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            try:
                dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _collect_replica_freshness(
    cur: Any,
    cfg: AgentConfig,
    *,
    now_utc_dt: datetime | None = None,
) -> dict[str, Any]:
    enabled = bool(getattr(cfg, "cluster_replica_freshness_enabled", False))
    max_age_default = max(300, int(getattr(cfg, "cluster_replica_freshness_max_age_sec", 172800) or 172800))
    checks = getattr(cfg, "cluster_replica_freshness_checks", []) or []
    out: dict[str, Any] = {
        "enabled": enabled,
        "status": "na",
        "max_age_sec": max_age_default,
        "checks": [],
        "errors": [],
    }
    if not enabled:
        return out
    if not isinstance(checks, list) or not checks:
        out["status"] = "degraded"
        out["errors"] = ["no_freshness_checks_configured"]
        return out

    now_dt = now_utc_dt or datetime.now(timezone.utc)
    overall_ok = True
    for raw_check in checks:
        if not isinstance(raw_check, dict):
            continue
        label = str(raw_check.get("label", "") or "").strip()
        item: dict[str, Any] = {
            "label": label,
            "status": "unknown",
            "latest_utc": None,
            "age_sec": None,
            "max_age_sec": max_age_default,
        }
        try:
            database = _quote_mysql_ident(raw_check.get("database"))
            table = _quote_mysql_ident(raw_check.get("table"))
            column = _quote_mysql_ident(raw_check.get("column"))
            order_raw = str(raw_check.get("order_column", "id") or "").strip()
            if raw_check.get("max_age_sec") is not None:
                item["max_age_sec"] = max(300, int(raw_check.get("max_age_sec") or max_age_default))
            if not label:
                item["label"] = ".".join(
                    [
                        str(raw_check.get("database") or "").strip(),
                        str(raw_check.get("table") or "").strip(),
                        str(raw_check.get("column") or "").strip(),
                    ]
                )
            if order_raw:
                order_column = _quote_mysql_ident(order_raw)
                sql = f"SELECT {column} AS latest FROM {database}.{table} ORDER BY {order_column} DESC LIMIT 1"
            else:
                sql = f"SELECT MAX({column}) AS latest FROM {database}.{table}"
            cur.execute(sql)
            row = cur.fetchone() or {}
            latest_dt = _mysql_datetime_to_utc(row.get("latest") if isinstance(row, dict) else None)
            if latest_dt is None:
                item["status"] = "stale"
                item["error"] = "latest_value_missing"
                overall_ok = False
            else:
                age = max(0, int((now_dt - latest_dt).total_seconds()))
                item["latest_utc"] = latest_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                item["age_sec"] = age
                if age > int(item["max_age_sec"] or max_age_default):
                    item["status"] = "stale"
                    overall_ok = False
                else:
                    item["status"] = "ok"
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)
            out["errors"].append(str(e))
            overall_ok = False
        out["checks"].append(item)

    if not out["checks"]:
        out["status"] = "degraded"
        out["errors"].append("no_valid_freshness_checks")
    else:
        out["status"] = "ok" if overall_ok else "degraded"
    return out


def _collect_cluster_db_state(cfg: AgentConfig, *, timeout_sec: int = 5) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    haproxy_state = _collect_haproxy_db_state(timeout_sec=max(2, int(timeout_sec)))
    gluster_state = _collect_gluster_state(timeout_sec=max(3, int(timeout_sec)))
    out: dict[str, Any] = {
        "checked_at_utc": now_utc,
        "status": "na",
        "role": "unknown",
        "source": "",
        "haproxy": haproxy_state,
        "gluster": gluster_state,
        "errors": [],
    }

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(
        *,
        source: str,
        host: str | None,
        port: int | None,
        user: str | None,
        password: str | None,
        unix_socket: str | None = None,
    ) -> None:
        u = str(user or "").strip()
        if not u:
            return
        h = str(host or "").strip() or "localhost"
        p = int(port or 3306)
        pwd = str(password or "")
        sock = str(unix_socket or "").strip() or _pick_local_socket(password=pwd, host=h)
        key = "|".join([source, h, str(p), u, sock or "", str(bool(pwd))])
        if key in seen:
            return
        seen.add(key)
        row: dict[str, Any] = {
            "source": source,
            "host": h,
            "port": p,
            "user": u,
            "password": pwd,
        }
        if sock:
            row["unix_socket"] = sock
        candidates.append(row)

    add_candidate(
        source="state_mysql",
        host=getattr(cfg, "state_mysql_host", None),
        port=getattr(cfg, "state_mysql_port", 3306),
        user=getattr(cfg, "state_mysql_user", None),
        password=getattr(cfg, "state_mysql_password", None),
        unix_socket=_configured_or_auto_local_socket(
            cfg,
            password=getattr(cfg, "state_mysql_password", None),
            host=getattr(cfg, "state_mysql_host", None),
        ),
    )
    add_candidate(
        source="backup_mysql",
        host=getattr(cfg, "backup_mysql_host", None),
        port=getattr(cfg, "backup_mysql_port", 3306) or 3306,
        user=getattr(cfg, "backup_mysql_user", None),
        password=getattr(cfg, "backup_mysql_password", None),
    )

    if not candidates:
        out["status"] = "na"
        out["errors"] = ["no_mysql_credentials"]
        return out

    def _query_map(cur, sql: str) -> dict[str, str]:
        cur.execute(sql)
        rows = cur.fetchall() or []
        m: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            k = str(row.get("Variable_name") or row.get("variable_name") or "").strip().lower()
            if not k:
                continue
            m[k] = str(row.get("Value") or row.get("value") or "").strip()
        return m

    errors: list[str] = []
    for cand in candidates:
        try:
            kwargs: dict[str, Any] = {
                "user": cand["user"],
                "password": cand.get("password", ""),
                "charset": "utf8mb4",
                "autocommit": True,
                "cursorclass": DictCursor,
                "connect_timeout": max(2, int(timeout_sec)),
                "read_timeout": max(3, int(timeout_sec)),
                "write_timeout": max(3, int(timeout_sec)),
            }
            if cand.get("unix_socket"):
                kwargs["unix_socket"] = cand["unix_socket"]
            else:
                kwargs["host"] = cand["host"]
                kwargs["port"] = int(cand.get("port", 3306) or 3306)

            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    vars_map = _query_map(
                        cur,
                        "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('wsrep_on','read_only','super_read_only')",
                    )
                    wsrep_map = _query_map(cur, "SHOW GLOBAL STATUS LIKE 'wsrep_%'")

                    replica_row: dict[str, Any] | None = None
                    replica_freshness: dict[str, Any] | None = None
                    try:
                        cur.execute("SHOW REPLICA STATUS")
                        rr = cur.fetchone()
                        if isinstance(rr, dict) and rr:
                            replica_row = rr
                    except Exception:
                        replica_row = None
                    if replica_row is None:
                        try:
                            cur.execute("SHOW SLAVE STATUS")
                            rr = cur.fetchone()
                            if isinstance(rr, dict) and rr:
                                replica_row = rr
                        except Exception:
                            replica_row = None
                    if replica_row is not None:
                        replica_freshness = _collect_replica_freshness(cur, cfg)

            role = "standalone"
            wsrep_on = _to_bool(vars_map.get("wsrep_on"))
            if bool(wsrep_map) or wsrep_on:
                role = "galera"
            elif replica_row:
                role = "replica"

            out = {
                "checked_at_utc": now_utc,
                "status": "ok",
                "role": role,
                "source": str(cand.get("source", "")),
                "haproxy": haproxy_state,
                "gluster": gluster_state,
                "read_only": _to_bool(vars_map.get("read_only")),
                "super_read_only": _to_bool(vars_map.get("super_read_only")),
                "errors": [],
            }

            if role == "galera":
                galera = {
                    "ready": _to_bool(wsrep_map.get("wsrep_ready")),
                    "connected": _to_bool(wsrep_map.get("wsrep_connected")),
                    "cluster_status": str(wsrep_map.get("wsrep_cluster_status") or "").strip() or None,
                    "local_state_comment": str(wsrep_map.get("wsrep_local_state_comment") or "").strip() or None,
                    "cluster_size": _to_int(wsrep_map.get("wsrep_cluster_size")),
                    "recv_queue_avg": _to_float(wsrep_map.get("wsrep_local_recv_queue_avg"))
                    if wsrep_map.get("wsrep_local_recv_queue_avg") not in {None, ""}
                    else _to_float(wsrep_map.get("wsrep_local_recv_queue")),
                    "send_queue_avg": _to_float(wsrep_map.get("wsrep_local_send_queue_avg"))
                    if wsrep_map.get("wsrep_local_send_queue_avg") not in {None, ""}
                    else _to_float(wsrep_map.get("wsrep_local_send_queue")),
                    "flow_control_paused": _to_float(wsrep_map.get("wsrep_flow_control_paused")),
                }
                routing_eligible, routing_reason = _galera_routing_eligibility(galera)
                galera["routing_eligible"] = routing_eligible
                galera["routing_reason"] = routing_reason
                out["galera"] = galera
                healthy = routing_eligible
                out["status"] = "ok" if healthy else "degraded"
            elif role == "replica":
                io_running = str(
                    (replica_row or {}).get("Replica_IO_Running")
                    or (replica_row or {}).get("Slave_IO_Running")
                    or ""
                ).strip()
                sql_running = str(
                    (replica_row or {}).get("Replica_SQL_Running")
                    or (replica_row or {}).get("Slave_SQL_Running")
                    or ""
                ).strip()
                lag = _to_int(
                    (replica_row or {}).get("Seconds_Behind_Source")
                    if (replica_row or {}).get("Seconds_Behind_Source") is not None
                    else (replica_row or {}).get("Seconds_Behind_Master")
                )
                replica = {
                    "io_running": io_running,
                    "sql_running": sql_running,
                    "seconds_behind": lag,
                    "source_host": str(
                        (replica_row or {}).get("Source_Host")
                        or (replica_row or {}).get("Master_Host")
                        or ""
                    ).strip(),
                    "source_port": _to_int(
                        (replica_row or {}).get("Source_Port")
                        if (replica_row or {}).get("Source_Port") is not None
                        else (replica_row or {}).get("Master_Port")
                    ),
                    "source_server_id": _to_int((replica_row or {}).get("Source_Server_Id")),
                    "sql_state": str((replica_row or {}).get("Replica_SQL_Running_State") or "").strip(),
                    "last_io_error": str((replica_row or {}).get("Last_IO_Error") or "").strip()[:500],
                    "last_sql_error": str((replica_row or {}).get("Last_SQL_Error") or "").strip()[:500],
                }
                out["replica"] = replica
                if replica_freshness is not None:
                    out["replica_freshness"] = replica_freshness
                healthy = io_running.lower() in {"yes", "on", "1", "running"} and sql_running.lower() in {
                    "yes",
                    "on",
                    "1",
                    "running",
                }
                if lag is not None and lag > 30:
                    healthy = False
                if isinstance(replica_freshness, dict) and str(replica_freshness.get("status") or "").lower() == "degraded":
                    healthy = False
                out["status"] = "ok" if healthy else "degraded"
            return out
        except Exception as e:
            errors.append(f"{cand.get('source')}: {e}")

    out["status"] = "error"
    out["errors"] = errors[-5:]
    return out


def _candidate_roots(root: str) -> list[Path]:
    root_path = Path(root)
    candidates: list[Path] = [root_path]
    base = root_path.name.lower()
    if base in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    candidates.append(root_path.parent.parent)
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps == "/" or ps in seen:
            continue
        seen.add(ps)
        out.append(p)
    return out


def _collect_installed_plugins(root: str, limit: int = 200) -> list[dict[str, str]]:
    try:
        return _scan_installed_plugins(root, limit=limit)
    except Exception:
        # Preserve the historical helper contract for callers that only need a
        # best-effort display. State publication uses the strict scanner below
        # and never turns a failed observation into an empty inventory.
        return []


def _scan_installed_plugins(
    root: str,
    limit: int = 200,
    *,
    allow_missing_dir_empty: bool = False,
) -> list[dict[str, str]]:
    plugins_dir = None
    for p in plugin_dir_candidates(root):
        if p.exists() and p.is_dir():
            plugins_dir = p
            break
    if plugins_dir is None:
        if allow_missing_dir_empty:
            return []
        raise RuntimeError("plugin_directory_unavailable")
    rows: list[dict[str, str]] = []
    for name in sorted(os.listdir(plugins_dir)):
        if name.startswith("."):
            continue
        if not _is_valid_bundle_name(name):
            continue
        pdir = plugins_dir / name
        if not pdir.is_dir():
            continue
        cfg = pdir / "Config" / "config.php"
        if not is_complete_plugin_bundle(pdir, name):
            continue
        ver = "-"
        try:
            ver = _extract_version_from_php_text(cfg.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            ver = "-"
        rows.append({"bundle": name, "version": ver or "-"})
        if len(rows) >= limit:
            break
    return rows


def _plugin_inventory_observation(
    install: MauticInstall,
    runtime_store: Any | None,
    *,
    allow_missing_dir_empty: bool = False,
) -> dict[str, Any]:
    """Observe one registry and preserve its last-known value on failure."""
    uid = str(getattr(install, "instance_uid", "") or getattr(install, "root", "") or "").strip()
    root = str(getattr(install, "root", "") or "").strip()
    previous = runtime_store.get_runtime_sync(plugin_inventory_runtime_key(uid)) if runtime_store is not None else None
    try:
        rows = _scan_installed_plugins(root, allow_missing_dir_empty=allow_missing_dir_empty)
        current = plugin_inventory_observation(
            instance_uid=uid,
            root=root,
            inventory=rows,
            status="ok",
        )
    except Exception as exc:
        current = plugin_inventory_observation(
            instance_uid=uid,
            root=root,
            inventory=[],
            status="error",
            error_type=type(exc).__name__,
            error=str(exc),
            previous=previous if isinstance(previous, dict) else None,
        )
    if runtime_store is not None:
        semantic_fields = ("status", "complete", "last_known", "inventory", "inventory_sha256", "scan_error")
        if not isinstance(previous, dict) or any(previous.get(key) != current.get(key) for key in semantic_fields):
            runtime_store.put_runtime_sync(plugin_inventory_runtime_key(uid), current)
    return current


def _plugin_inventory_sync_pending(runtime_store: Any | None) -> dict[str, Any]:
    if runtime_store is None:
        return {}
    raw = runtime_store.get_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY)
    return raw if isinstance(raw, dict) else {}


def _queue_plugin_inventory_sync(runtime_store: Any, observation: dict[str, Any], *, operation_id: str) -> dict[str, Any]:
    pending = _plugin_inventory_sync_pending(runtime_store)
    items = dict(pending.get("items") or {}) if isinstance(pending.get("items"), dict) else {}
    uid = str(observation.get("instance_uid") or "").strip()
    items[uid] = {
        "root": str(observation.get("root") or ""),
        "inventory_sha256": str(observation.get("inventory_sha256") or ""),
        "observed_at_utc": str(observation.get("observed_at_utc") or ""),
    }
    payload = {
        "schema": PLUGIN_INVENTORY_SYNC_SCHEMA,
        "status": "pending",
        "items": items,
        "requested_at_utc": str(pending.get("requested_at_utc") or utc_now()),
        "last_attempt_at_utc": str(pending.get("last_attempt_at_utc") or ""),
        "retry_count": int(pending.get("retry_count", 0) or 0),
        "last_error": str(pending.get("last_error") or "")[:500],
        "operation_id": str(operation_id or "").strip(),
    }
    runtime_store.put_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY, payload)
    return payload


def _ack_plugin_inventory_sync(runtime_store: Any | None, payload: Mapping[str, Any]) -> None:
    if runtime_store is None:
        return
    pending = _plugin_inventory_sync_pending(runtime_store)
    items = dict(pending.get("items") or {}) if isinstance(pending.get("items"), dict) else {}
    sent = payload.get("plugin_inventory_sync")
    sent_items = sent.get("items") if isinstance(sent, Mapping) else []
    for item in sent_items if isinstance(sent_items, list) else []:
        if not isinstance(item, Mapping):
            continue
        uid = str(item.get("instance_uid") or "").strip()
        current = items.get(uid)
        if isinstance(current, Mapping) and (
            str(current.get("inventory_sha256") or "") == str(item.get("inventory_sha256") or "")
            and str(current.get("observed_at_utc") or "") == str(item.get("observed_at_utc") or "")
        ):
            items.pop(uid, None)
    if items:
        pending["items"] = items
        pending["status"] = "pending"
        runtime_store.put_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY, pending)
    else:
        runtime_store.delete_runtime_sync([PLUGIN_INVENTORY_PENDING_KEY])


def _mark_plugin_inventory_sync_failure(runtime_store: Any | None, message: str) -> None:
    if runtime_store is None:
        return
    pending = _plugin_inventory_sync_pending(runtime_store)
    if not pending.get("items"):
        return
    pending["status"] = "pending"
    pending["last_attempt_at_utc"] = utc_now()
    pending["retry_count"] = int(pending.get("retry_count", 0) or 0) + 1
    pending["last_error"] = str(message or "state_push_failed")[:500]
    runtime_store.put_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY, pending)


def _plugin_inventory_sync_payload(runtime_store: Any | None) -> dict[str, Any]:
    return plugin_inventory_sync_payload(_plugin_inventory_sync_pending(runtime_store))


def _store_for_plugin_inventory(cfg: AgentConfig, runtime_store: Any | None) -> tuple[Any, bool]:
    if runtime_store is not None:
        return runtime_store, False
    from mcd_agent.daemon import TaskStore

    return TaskStore(cfg.state_db_path, cfg), True


def sync_plugin_inventory_after_operation(
    cfg: AgentConfig,
    install: MauticInstall,
    *,
    operation_id: str,
    action: str = "",
    runtime_store: Any | None = None,
) -> dict[str, Any]:
    """Persist an operation's inventory before its terminal receipt is emitted."""
    store, owned = _store_for_plugin_inventory(cfg, runtime_store)
    try:
        observed = _plugin_inventory_observation(
            install,
            store,
            allow_missing_dir_empty=str(action or "").strip().lower() in {"remove", "purge"},
        )
        _queue_plugin_inventory_sync(store, observed, operation_id=operation_id)
        ok, message = push_state_now(cfg, include_signals=False, runtime_store=store)
        pending = _plugin_inventory_sync_pending(store)
        scan_failed = str(observed.get("status") or "") != "ok"
        status = "error" if scan_failed else ("ok" if ok and not pending.get("items") else "pending")
        if not ok:
            failure = str(message or "state_push_failed")[:500]
            # send() records transport failures itself. Avoid counting the
            # same failed attempt twice, while still recording failures from
            # a disabled/short-circuited push path.
            if str(pending.get("last_error") or "") != failure:
                pending["last_attempt_at_utc"] = utc_now()
                pending["retry_count"] = int(pending.get("retry_count", 0) or 0) + 1
                pending["last_error"] = failure
                store.put_runtime_sync(PLUGIN_INVENTORY_PENDING_KEY, pending)
        return {
            "schema": PLUGIN_INVENTORY_SYNC_SCHEMA,
            "status": status,
            "inventory_sync_status": status,
            "inventory_sha256": str(observed.get("inventory_sha256") or ""),
            "observed_at_utc": str(observed.get("observed_at_utc") or ""),
            "last_attempt_at_utc": str(pending.get("last_attempt_at_utc") or utc_now()),
            "retry_count": int(pending.get("retry_count", 0) or 0),
            "last_error": str(pending.get("last_error") or "")[:500],
            "scan_error": observed.get("scan_error"),
        }
    except Exception as exc:
        return {
            "schema": PLUGIN_INVENTORY_SYNC_SCHEMA,
            "status": "error",
            "inventory_sync_status": "error",
            "last_attempt_at_utc": utc_now(),
            "last_error": f"{type(exc).__name__}: {exc}"[:500],
        }
    finally:
        if owned:
            store.close()


def _read_php_array_string(cfg_text: str, key: str) -> str:
    pat = re.compile(rf"['\"]{re.escape(key)}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)
    m = pat.search(cfg_text or "")
    if not m:
        return ""
    return str(m.group(1) or "").strip()


def _parse_sender_config_from_local_php(root: str) -> tuple[dict[str, str], str]:
    candidates: list[Path] = []
    for cand in _candidate_roots(root):
        candidates.extend(
            [
                cand / "config" / "local.php",
                cand / "app" / "config" / "local.php",
                cand / "docroot" / "config" / "local.php",
            ]
        )
    checked: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps in checked:
            continue
        checked.add(ps)
        if not p.exists() or not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        cfg = {
            "mailer_dsn": _read_php_array_string(txt, "mailer_dsn"),
            "mailer_transport": _read_php_array_string(txt, "mailer_transport"),
            "mail_transport": _read_php_array_string(txt, "mail_transport"),
            "email_transport": _read_php_array_string(txt, "email_transport"),
            "mailer_host": _read_php_array_string(txt, "mailer_host"),
            "mailer_from_email": _read_php_array_string(txt, "mailer_from_email"),
            "mailer_from_name": _read_php_array_string(txt, "mailer_from_name"),
            "mailer_return_path": _read_php_array_string(txt, "mailer_return_path"),
        }
        return cfg, ps
    return {}, ""


def _sender_mask_dsn(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        u = urlsplit(s)
        host = str(u.hostname or "").strip()
        if u.port:
            host = f"{host}:{int(u.port)}"
        qkeys = sorted({str(k).strip() for k, _v in parse_qsl(u.query, keep_blank_values=True) if str(k).strip()})
        base = f"{u.scheme}://{host}" if u.scheme else host
        if qkeys:
            base += f"?{','.join(qkeys)}"
        return base or s[:80]
    except Exception:
        return re.sub(r":[^:@/]{4,}@", ":***@", s)[:120]


def _detect_sender_profile(root: str, plugins: list[dict[str, str]]) -> dict[str, Any]:
    plugin_names = {str(x.get("bundle", "")).strip().lower() for x in plugins if isinstance(x, dict)}
    cfg, source_path = _parse_sender_config_from_local_php(root)
    transport = (
        str(cfg.get("mailer_transport", "") or "").strip().lower()
        or str(cfg.get("mail_transport", "") or "").strip().lower()
        or str(cfg.get("email_transport", "") or "").strip().lower()
    )
    dsn_raw = str(cfg.get("mailer_dsn", "") or "").strip()
    dsn_low = dsn_raw.lower()
    dsn_masked = _sender_mask_dsn(dsn_raw)
    dsn_scheme = ""
    dsn_host = ""
    dsn_port: int | None = None
    dsn_username_present = False
    dsn_password_present = False
    dsn_query: dict[str, str] = {}
    if dsn_raw:
        try:
            u = urlsplit(dsn_raw)
            dsn_scheme = str(u.scheme or "").strip().lower()
            dsn_host = str(u.hostname or "").strip().lower()
            dsn_port = int(u.port) if u.port is not None else None
            dsn_username_present = bool(u.username)
            dsn_password_present = bool(u.password)
            dsn_query = {
                str(key).strip(): str(value or "").strip()
                for key, value in parse_qsl(u.query, keep_blank_values=True)
                if str(key).strip()
            }
        except Exception:
            dsn_scheme = ""
            dsn_host = ""
            dsn_port = None
            dsn_username_present = False
            dsn_password_present = False
            dsn_query = {}
    mailer_host = str(cfg.get("mailer_host", "") or "").strip().lower()

    has_ses_plugin = "amazonsesbundle" in plugin_names
    # Mautic 4 commonly uses legacy transport names (e.g. mautic.transport.amazon_api).
    transport_is_amazon = "amazon" in transport
    transport_is_amazon_api = "amazon_api" in transport
    transport_is_ses = ("ses" in transport) or transport_is_amazon
    configured = bool(dsn_raw or transport or mailer_host)

    key = "unknown"
    label = "unknown"
    if any(x in dsn_low for x in ("ses+", "amazonaws.com")) or transport_is_ses or "amazonaws.com" in mailer_host:
        # Sender classification is based on active transport/DSN only.
        # Installed plugins are shown as hints in tooltip, but do not drive the final label.
        if dsn_scheme == "mautic+ses+api":
            key = "mautic_ses_api"
            label = "mautic+ses+api"
        elif dsn_scheme == "ses+api" or "ses+api://" in dsn_low:
            key = "ses_api"
            label = "ses+api"
        elif "ses+smtp" in dsn_low:
            key = "ses_smtp"
            label = "ses+smtp"
        elif transport_is_amazon_api or (transport_is_ses and "api" in transport):
            key = "ses_api"
            label = "ses+api"
        elif "smtp" in transport:
            key = "ses_smtp"
            label = "ses+smtp"
        else:
            key = "ses_api"
            label = "ses+api"
    elif "sendgrid+" in dsn_low or "sendgrid" in transport:
        key = "sendgrid_api"
        label = "sendgrid+api"
    elif "mailgun+" in dsn_low or "mailgun" in transport:
        key = "mailgun_api"
        label = "mailgun+api"
    elif dsn_scheme in {"smtp", "smtps"} or transport == "smtp":
        key = "smtp"
        label = "smtp"
    elif dsn_scheme in {"sendmail", "native"} or transport in {"sendmail", "mail"}:
        key = "sendmail"
        label = "sendmail"

    own_host = bool(dsn_scheme == "sendmail" and "mcd-mail-submit" in dsn_raw.lower())
    if own_host:
        key = "own_host"
        label = "own host"
    elif configured and key == "unknown":
        key = "custom"
        label = dsn_scheme or transport or "configured"

    title_lines = [
        f"Sender type: {label}",
        f"detected_key: {key}",
        f"source: {source_path or '-'}",
        f"transport: {transport or '-'}",
    ]
    if dsn_masked:
        title_lines.append(f"dsn: {dsn_masked}")
    if dsn_scheme:
        title_lines.append(f"dsn_scheme: {dsn_scheme}")
    if dsn_host:
        title_lines.append(f"dsn_host: {dsn_host}")
    if mailer_host:
        title_lines.append(f"mailer_host: {mailer_host}")
    hints: list[str] = []
    if has_ses_plugin:
        hints.append("AmazonSesBundle")
    if hints:
        title_lines.append("plugin_hints: " + ",".join(hints))

    safe_config: dict[str, Any] = {
        "configured": configured,
        "source": source_path,
        "method": label if configured and key != "unknown" else "",
        "sender_key": key if configured else "unknown",
        "dsn_scheme": dsn_scheme,
        "dsn_host": dsn_host,
        "dsn_port": dsn_port,
        "region": str(dsn_query.get("region", "") or "").strip().lower(),
        "from_email": str(cfg.get("mailer_from_email", "") or "").strip(),
        "from_name": str(cfg.get("mailer_from_name", "") or "").strip(),
        "return_path": str(cfg.get("mailer_return_path", "") or "").strip(),
        "credentials_present": {
            "username": dsn_username_present,
            "password": dsn_password_present,
        },
    }
    return {
        "sender_type": label,
        "sender_key": key,
        "sender_title": "\n".join(title_lines),
        "sender_config": safe_config,
    }


class MCCStatePusher:
    def __init__(self, cfg: AgentConfig, runtime_store: Any | None = None) -> None:
        self.cfg = cfg
        self.runtime_store = runtime_store
        self.last_push_ts = 0.0
        self.last_alert_poll_ts = 0.0
        self.last_hash = ""
        self.last_alert_level = 0
        self.latest_signals: dict[str, Any] | None = None
        self.latest_signals_ts = 0.0
        self.latest_apt_state: dict[str, Any] | None = None
        self.latest_apt_state_ts = 0.0
        self.latest_apt_probe_key = ""
        self.latest_cluster_db_state: dict[str, Any] | None = None
        self.latest_cluster_db_state_ts = 0.0
        self.latest_state_backend: dict[str, Any] | None = None
        self.latest_state_backend_ts = 0.0
        self.latest_cluster_assets_state: dict[str, Any] | None = None
        self.latest_cluster_assets_state_ts = 0.0
        self._last_snapshot_hash = ""
        self._last_snapshot_ts = 0.0
        self._last_monitor_signals_hash = ""
        self._last_monitor_signals_push_ts = 0.0
        # A command ingested from the last MCC response has been executed and
        # its terminal receipt must be delivered on the next state push.
        self._control_push_pending = False
        # Filesystem-permissions repair deltas.
        # We push deltas (not cumulative totals) so MCC 3-day aggregation
        # remains correct and stable across daemon restarts.
        self._fs_permissions_fix_pending = 0
        self._fs_permissions_events_pending: list[dict[str, Any]] = []
        # DB watchdog observe deltas.
        self._db_watchdog_pending: dict[str, int] = {
            "samples": 0,
            "errors": 0,
            "metadata_lock_waits": 0,
            "long_queries": 0,
            "orphan_candidates": 0,
            "rule_hits": 0,
        }
        self._db_watchdog_events_pending: list[dict[str, Any]] = []
        # Native campaign fallback runs are sparse diagnostic events. Keep a
        # bounded recent window so MCC receives recovery evidence without a
        # scheduler-log firehose.
        self._campaign_native_fallback_events_pending: list[dict[str, Any]] = []
        self._campaign_native_fallback_runtime: dict[str, dict[str, Any]] = {}
        self._campaign_scheduler_liveness_runtime: dict[str, dict[str, Any]] = {}

    def enabled(self) -> bool:
        return bool(self.cfg.mcc_push_enabled and self.cfg.mcc_url and self.cfg.mcc_token)

    def _plugin_inventory_sync_pending(self) -> bool:
        pending = _plugin_inventory_sync_pending(self.runtime_store)
        return bool(isinstance(pending.get("items"), dict) and pending.get("items"))

    def _plugin_operations_runtime(self, root: str) -> list[dict[str, Any]]:
        if self.runtime_store is None:
            return []
        try:
            rows = self.runtime_store.list_runtime_sync("plugin_operation_runtime:")
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for _key, raw in rows:
            if not isinstance(raw, dict) or raw.get("schema") != "mcd-plugin-operation-runtime-v1":
                continue
            if str(raw.get("root") or "") != str(root):
                continue
            result.append({str(key): value for key, value in raw.items() if not str(key).startswith("_")})
        return sorted(
            result,
            key=lambda item: (str(item.get("operation_key") or ""), str(item.get("task_id") or "")),
        )

    def set_signals(self, payload: dict[str, Any], now_ts: float) -> None:
        self.latest_signals = payload
        self.latest_signals_ts = now_ts

    def campaign_native_fallback_last_run_ts(self, root: str) -> float:
        try:
            return _campaign_native_fallback_last_run_ts(self.cfg, root)
        except Exception:
            return 0.0

    def set_campaign_native_fallback_runtime(self, root: str, payload: dict[str, Any] | None) -> None:
        key = str(root or "").strip()
        if not key:
            return
        if payload is None:
            self._campaign_native_fallback_runtime.pop(key, None)
            return
        item = dict(payload)
        item["root"] = key
        self._campaign_native_fallback_runtime[key] = item

    def set_campaign_scheduler_liveness_runtime(self, root: str, payload: dict[str, Any] | None) -> None:
        key = str(root or "").strip()
        if not key:
            return
        if payload is None:
            self._campaign_scheduler_liveness_runtime.pop(key, None)
            return
        item = dict(payload)
        item["root"] = key
        self._campaign_scheduler_liveness_runtime[key] = item

    def add_fs_permissions_fix(
        self,
        count: int = 1,
        *,
        events: list[dict[str, Any]] | None = None,
        now_ts: float | None = None,
    ) -> None:
        n = int(count or 0)
        if n > 0:
            self._fs_permissions_fix_pending += n
        if events:
            ts = float(now_ts if now_ts is not None else time.time())
            event_ts = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out: list[dict[str, Any]] = []
            for row in events:
                if not isinstance(row, dict):
                    continue
                out.append(
                    {
                        "ts": str(row.get("ts", "")).strip() or event_ts,
                        "path": str(row.get("path", "")).strip(),
                        "sample_path": str(row.get("sample_path", "")).strip(),
                        "reason": str(row.get("reason", "")).strip(),
                        "actor": str(row.get("actor", "")).strip(),
                        "actor_source": str(row.get("actor_source", "")).strip(),
                        "before_owner_group": str(row.get("before_owner_group", "")).strip(),
                        "before_mode": str(row.get("before_mode", "")).strip(),
                        "result": str(row.get("result", "")).strip() or "repaired",
                        "error": str(row.get("error", "")).strip(),
                    }
                )
            if out:
                self._fs_permissions_events_pending.extend(out)
                self._fs_permissions_events_pending = self._fs_permissions_events_pending[-200:]
        if n <= 0 and not events:
            return

    def add_db_watchdog_observation(self, payload: dict[str, Any], *, now_ts: float | None = None) -> None:
        if not isinstance(payload, dict):
            return
        self._db_watchdog_pending["samples"] = int(self._db_watchdog_pending.get("samples", 0) or 0) + 1
        status = str(payload.get("status", "")).strip().lower()
        if status != "ok":
            self._db_watchdog_pending["errors"] = int(self._db_watchdog_pending.get("errors", 0) or 0) + 1
            reason = str(payload.get("reason", "")).strip() or f"status:{status or 'unknown'}"
            self._db_watchdog_events_pending.append(
                {
                    "ts": datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()), tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "root": str(payload.get("root", "")).strip(),
                    "status": status or "error",
                    "reason": reason,
                }
            )
            self._db_watchdog_events_pending = self._db_watchdog_events_pending[-200:]
            return

        pl = payload.get("processlist")
        pl_map = pl if isinstance(pl, dict) else {}
        rule = payload.get("rules")
        rule_map = rule if isinstance(rule, dict) else {}
        lock_waits = int(pl_map.get("metadata_lock_waits", 0) or 0)
        long_q = int(pl_map.get("long_queries", 0) or 0)
        orphans = int(pl_map.get("orphan_candidates", 0) or 0)
        rule_hits = int(rule_map.get("hit_total", 0) or 0)
        self._db_watchdog_pending["metadata_lock_waits"] = int(
            self._db_watchdog_pending.get("metadata_lock_waits", 0) or 0
        ) + lock_waits
        self._db_watchdog_pending["long_queries"] = int(self._db_watchdog_pending.get("long_queries", 0) or 0) + long_q
        self._db_watchdog_pending["orphan_candidates"] = int(
            self._db_watchdog_pending.get("orphan_candidates", 0) or 0
        ) + orphans
        self._db_watchdog_pending["rule_hits"] = int(self._db_watchdog_pending.get("rule_hits", 0) or 0) + rule_hits
        top = pl_map.get("top_slowest")
        top_rows = top if isinstance(top, list) else []
        first = top_rows[0] if top_rows and isinstance(top_rows[0], dict) else {}
        self._db_watchdog_events_pending.append(
            {
                "ts": str(payload.get("checked_at_utc", "")).strip()
                or datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()), tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "root": str(payload.get("root", "")).strip(),
                "status": "ok",
                "total": int(pl_map.get("total", 0) or 0),
                "queries": int(pl_map.get("queries", 0) or 0),
                "metadata_lock_waits": lock_waits,
                "long_queries": long_q,
                "orphan_candidates": orphans,
                "rule_hits": rule_hits,
                "max_query_time_sec": int(pl_map.get("max_query_time_sec", 0) or 0),
                "top_pid": int(first.get("pid", 0) or 0) if isinstance(first, dict) else 0,
                "top_time_sec": int(first.get("time_sec", 0) or 0) if isinstance(first, dict) else 0,
                "top_state": str(first.get("state", "")).strip() if isinstance(first, dict) else "",
            }
        )
        self._db_watchdog_events_pending = self._db_watchdog_events_pending[-200:]

    def add_campaign_native_fallback(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        now_ts = time.time()
        event = {
            "event_id": str(payload.get("event_id", "")).strip() or uuid.uuid4().hex,
            "ts": str(payload.get("ts", "")).strip()
            or datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "root": str(payload.get("root", "")).strip(),
            "status": str(payload.get("status", "")).strip() or "unknown",
            "failed_stage": str(payload.get("failed_stage", "")).strip()[:80],
            "operation_rc": payload.get("operation_rc"),
            "metrics_status": str(payload.get("metrics_status", "")).strip() or "unknown",
            "metrics_error": str(payload.get("metrics_error", "")).strip()[:500],
            "pending_before": _nullable_nonnegative_int(payload.get("pending_before")),
            "pending_after": _nullable_nonnegative_int(payload.get("pending_after")),
            "email_stats_before": _nullable_nonnegative_int(payload.get("email_stats_before")),
            "email_stats_after": _nullable_nonnegative_int(payload.get("email_stats_after")),
            "started_at": str(payload.get("started_at", "") or "").strip(),
            "duration_sec": _nullable_nonnegative_int(payload.get("duration_sec")),
            "schedule_delay_sec": _nullable_nonnegative_int(payload.get("schedule_delay_sec")),
        }
        self._campaign_native_fallback_events_pending.append(event)
        self._campaign_native_fallback_events_pending = self._campaign_native_fallback_events_pending[-100:]
        try:
            _store_campaign_native_fallback_event(self.cfg, event, now_ts)
        except Exception as exc:
            logging.warning("campaign native fallback state write failed: %s", exc)

    def _signals_payload(self) -> dict[str, Any]:
        base = self.latest_signals if isinstance(self.latest_signals, dict) else {}
        out = dict(base)
        totals_raw = out.get("totals")
        totals = dict(totals_raw) if isinstance(totals_raw, dict) else {}
        pending = int(self._fs_permissions_fix_pending or 0)
        totals["fs_permissions_fix"] = int(totals.get("fs_permissions_fix", 0) or 0) + pending
        totals["db_watchdog_samples"] = int(totals.get("db_watchdog_samples", 0) or 0) + int(
            self._db_watchdog_pending.get("samples", 0) or 0
        )
        totals["db_watchdog_errors"] = int(totals.get("db_watchdog_errors", 0) or 0) + int(
            self._db_watchdog_pending.get("errors", 0) or 0
        )
        totals["db_watchdog_metadata_lock_waits"] = int(
            totals.get("db_watchdog_metadata_lock_waits", 0) or 0
        ) + int(self._db_watchdog_pending.get("metadata_lock_waits", 0) or 0)
        totals["db_watchdog_long_queries"] = int(totals.get("db_watchdog_long_queries", 0) or 0) + int(
            self._db_watchdog_pending.get("long_queries", 0) or 0
        )
        totals["db_watchdog_orphan_candidates"] = int(
            totals.get("db_watchdog_orphan_candidates", 0) or 0
        ) + int(self._db_watchdog_pending.get("orphan_candidates", 0) or 0)
        totals["db_watchdog_rule_hits"] = int(totals.get("db_watchdog_rule_hits", 0) or 0) + int(
            self._db_watchdog_pending.get("rule_hits", 0) or 0
        )
        out["totals"] = totals
        out["fs_permissions_fix_pending"] = pending
        if self._fs_permissions_events_pending:
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["fs_permissions_fix_recent"] = self._fs_permissions_events_pending[-50:]
            out["details"] = details
        if self._db_watchdog_events_pending:
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["db_watchdog_recent"] = self._db_watchdog_events_pending[-50:]
            out["details"] = details
        try:
            fallback_recent = _read_campaign_native_fallback_events(self.cfg, limit=50)
            fallback_stats = _read_campaign_native_fallback_stats(self.cfg)
        except Exception:
            fallback_recent = list(self._campaign_native_fallback_events_pending[-50:])
            fallback_stats = []
        if fallback_stats:
            totals["campaign_native_fallback_runs"] = sum(int(row.get("runs", 0) or 0) for row in fallback_stats)
            totals["campaign_native_fallback_native_errors"] = sum(
                int(row.get("native_errors", 0) or 0) for row in fallback_stats
            )
            totals["campaign_native_fallback_metric_errors"] = sum(
                int(row.get("metric_errors", 0) or 0) for row in fallback_stats
            )
            totals["campaign_native_fallback_recovered_runs"] = sum(
                int(row.get("recovered_runs", 0) or 0) for row in fallback_stats
            )
            totals["campaign_native_fallback_recovered_pending"] = sum(
                int(row.get("recovered_pending", 0) or 0) for row in fallback_stats
            )
            totals["campaign_native_fallback_recovered_email_stats"] = sum(
                int(row.get("recovered_email_stats", 0) or 0) for row in fallback_stats
            )
            out["totals"] = totals
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["campaign_native_fallback_stats"] = fallback_stats
            details["campaign_native_fallback_retention"] = {
                "days": int(_CAMPAIGN_NATIVE_FALLBACK_RETENTION_SEC // 86400),
                "max_events": int(_CAMPAIGN_NATIVE_FALLBACK_MAX_EVENTS),
            }
            out["details"] = details
        if fallback_recent:
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["campaign_native_fallback_recent"] = fallback_recent
            out["details"] = details
        if self._campaign_native_fallback_runtime:
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["campaign_native_fallback_runtime"] = [
                dict(self._campaign_native_fallback_runtime[root])
                for root in sorted(self._campaign_native_fallback_runtime)
            ]
            out["details"] = details
        if self._campaign_scheduler_liveness_runtime:
            details_raw = out.get("details")
            details = dict(details_raw) if isinstance(details_raw, dict) else {}
            details["campaign_scheduler_liveness_runtime"] = [
                dict(self._campaign_scheduler_liveness_runtime[root])
                for root in sorted(self._campaign_scheduler_liveness_runtime)
            ]
            out["details"] = details
        return out

    def _apt_probe_key(self) -> str:
        """
        Lightweight fingerprint for local APT state.
        If package DB/sources changed, refresh apt_state immediately
        even when cache interval is not yet elapsed.
        """
        paths = [
            Path("/var/lib/dpkg/status"),
            Path("/var/lib/apt/periodic/update-success-stamp"),
            Path("/var/lib/apt/lists"),
            Path("/etc/apt/sources.list"),
            Path("/etc/apt/sources.list.d"),
        ]
        parts: list[str] = []
        for p in paths:
            try:
                st = p.stat()
                parts.append(f"{p}:{int(st.st_mtime_ns)}:{int(st.st_size)}")
            except Exception:
                parts.append(f"{p}:missing")
        return "|".join(parts)

    def _apt_state(self, now_ts: float) -> dict[str, Any]:
        interval = max(30, int(getattr(self.cfg, "mcc_push_apt_state_interval_sec", 120) or 120))
        probe_key = self._apt_probe_key()
        if (
            self.latest_apt_state
            and self.latest_apt_probe_key == probe_key
            and (now_ts - self.latest_apt_state_ts) < interval
        ):
            return dict(self.latest_apt_state)
        payload = collect_apt_state(timeout_sec=30, cfg=self.cfg)
        self.latest_apt_state = payload
        self.latest_apt_state_ts = now_ts
        self.latest_apt_probe_key = probe_key
        return dict(payload)

    def _cluster_db_state(self, now_ts: float) -> dict[str, Any]:
        interval = max(30, int(getattr(self.cfg, "mcc_push_apt_state_interval_sec", 120) or 120))
        if self.latest_cluster_db_state and (now_ts - self.latest_cluster_db_state_ts) < interval:
            return dict(self.latest_cluster_db_state)
        payload = _collect_cluster_db_state(self.cfg, timeout_sec=5)
        self.latest_cluster_db_state = payload
        self.latest_cluster_db_state_ts = now_ts
        return dict(payload)

    def _state_backend_payload(self, now_ts: float) -> dict[str, Any]:
        interval = max(30, min(300, int(getattr(self.cfg, "mcc_push_interval_sec", 60) or 60)))
        if self.latest_state_backend and (now_ts - self.latest_state_backend_ts) < interval:
            return dict(self.latest_state_backend)
        payload = state_backend_status(self.cfg, probe=True)
        self.latest_state_backend = payload
        self.latest_state_backend_ts = now_ts
        return dict(payload)

    def _cluster_assets_payload(self, now_ts: float, installs: list[MauticInstall]) -> dict[str, Any]:
        interval = max(60, int(getattr(self.cfg, "cluster_assets_interval_sec", 600) or 600))
        if self.latest_cluster_assets_state and (now_ts - self.latest_cluster_assets_state_ts) < interval:
            return dict(self.latest_cluster_assets_state)
        payload = collect_cluster_assets_status(self.cfg, installs=installs)
        self.latest_cluster_assets_state = payload
        self.latest_cluster_assets_state_ts = now_ts
        return dict(payload)

    def should_push(self, now_ts: float, payload_no_ts: dict[str, Any]) -> tuple[bool, bool]:
        force_alert = False
        new_hash = _hash_payload(stable_change_payload(payload_no_ts))
        changed = new_hash != self.last_hash

        if self.latest_signals:
            lvl = 0
            overall = self.latest_signals.get("overall")
            if isinstance(overall, dict):
                try:
                    lvl = int(overall.get("level", 0) or 0)
                except Exception:
                    lvl = 0
            if lvl > 0 and lvl != self.last_alert_level:
                force_alert = True
            self.last_alert_level = lvl

        interval_due = (now_ts - self.last_push_ts) >= max(1, int(self.cfg.mcc_push_interval_sec))
        change_due = bool(self.cfg.mcc_push_on_change and changed)
        return (
            self._control_push_pending
            or self._plugin_inventory_sync_pending()
            or interval_due
            or change_due
            or force_alert
        ), changed

    def _execute_pending_control_jobs(
        self,
        control_store: ControlStore,
        *,
        agent_id: str,
        host_fingerprint: str,
        result_key: dict[str, Any] | None,
        key_path: str,
    ) -> list[dict[str, Any]]:
        if not result_key or not key_path:
            return []
        try:
            private_key, _ = ensure_result_key(key_path)
            if not control_store.active_key_ack(str(result_key.get("key_id") or "")):
                return []
            return control_store.execute_pending(
                agent_id=agent_id,
                host_fingerprint=host_fingerprint,
                active_key=(private_key, str(result_key["key_id"])),
                runner=DockerRuntimePreflightRunner(),
                signer=sign_result,
            )
        except Exception:
            return []

    def mark_pushed(self, now_ts: float, payload_no_ts: dict[str, Any]) -> None:
        self.last_push_ts = now_ts
        self.last_hash = _hash_payload(stable_change_payload(payload_no_ts))
        self._fs_permissions_fix_pending = 0
        self._fs_permissions_events_pending = []
        self._db_watchdog_pending = {
            "samples": 0,
            "errors": 0,
            "metadata_lock_waits": 0,
            "long_queries": 0,
            "orphan_candidates": 0,
            "rule_hits": 0,
        }
        self._db_watchdog_events_pending = []

    def should_push_monitor_signals(self, now_ts: float, payload: dict[str, Any]) -> bool:
        new_hash = _hash_payload(monitor_signals_change_payload(payload))
        if not new_hash or new_hash == self._last_monitor_signals_hash:
            return False
        if now_ts - self._last_monitor_signals_push_ts < 0.5:
            return False
        return True

    def send_signals(self, payload: dict[str, Any], timeout_sec: int = 3) -> tuple[bool, str]:
        if not self.enabled():
            return False, "push disabled"
        ident = resolve_agent_identity(self.cfg)
        body = {
            "hostname": str(ident.get("effective_hostname") or ""),
            "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
            "agent_hostname": str(ident.get("local_hostname") or ""),
            "configured_host_name": str(ident.get("configured_host_name") or ""),
            "agent_version": __version__,
            "signals": payload if isinstance(payload, dict) else {},
        }
        base = str(self.cfg.mcc_url).rstrip("/")
        url = base + "/api/v1/agent/signals"
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
        req = request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.mcc_token}",
                "X-MCD-Agent-Version": __version__,
            },
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                code = int(getattr(resp, "status", 200))
                text = (resp.read() or b"").decode("utf-8", errors="replace").strip()
                if 200 <= code < 300:
                    self._last_monitor_signals_hash = _hash_payload(monitor_signals_change_payload(payload))
                    self._last_monitor_signals_push_ts = time.time()
                    return True, text or "ok"
                return False, f"http {code}: {text}"
        except HTTPError as e:
            try:
                msg = (e.read() or b"").decode("utf-8", errors="replace").strip()
            except Exception:
                msg = ""
            return False, f"http {e.code}: {msg or e.reason}"
        except URLError as e:
            return False, f"urlerror: {e.reason}"
        except Exception as e:
            return False, str(e)

    def _store_state_snapshot(self, payload: dict[str, Any], now_ts: float) -> None:
        payload_hash = _hash_payload(payload)
        interval = max(30, min(300, int(getattr(self.cfg, "mcc_push_interval_sec", 60) or 60)))
        if (
            self._last_snapshot_hash == payload_hash
            and (now_ts - self._last_snapshot_ts) < interval
        ):
            return
        ok, msg = upsert_state_snapshot_mysql(
            self.cfg,
            payload=payload,
            payload_hash=payload_hash,
            created_at=now_ts,
        )
        if ok:
            self._last_snapshot_hash = payload_hash
            self._last_snapshot_ts = now_ts
        elif msg not in {"mysql_state_disabled", "snapshot_disabled"}:
            if _should_log_mysql_warning(self.cfg, bucket="snapshot", msg=msg):
                logging.warning("state snapshot mysql upsert failed: %s", msg)

    def build_payload(
        self,
        *,
        installs: list[MauticInstall],
        profile_name: str,
        now_ts: float,
        include_signals: bool = True,
        profile_event: dict[str, Any] | None = None,
        instances_snapshot_complete: bool = True,
    ) -> dict[str, Any]:
        identity = resolve_agent_identity(self.cfg)
        result_key = None
        result_private_key = None
        key_path = str(getattr(self.cfg, "result_signing_key_path", "") or "")
        if key_path:
            try:
                result_private_key, result_key = ensure_result_key(key_path)
            except Exception:
                result_key = None
        control_store = ControlStore(self.cfg.state_db_path)
        state_identity = state_identity_payload(self.cfg)
        if result_key and key_path:
            self._execute_pending_control_jobs(
                control_store,
                agent_id=state_identity["agent_id"],
                host_fingerprint=state_identity["host_fingerprint"],
                result_key=result_key,
                key_path=key_path,
            )
        docker_image_inventory = collect_signed_inventory(
            agent_id=state_identity["agent_id"],
            host_fingerprint=state_identity["host_fingerprint"],
            private_key=result_private_key,
            key_id=str(result_key.get("key_id") or "") if isinstance(result_key, dict) else "",
        )
        from mcd_agent.mautic_release_authorization import runtime_capabilities as release_capabilities
        instances = []
        plugin_inventory_observations: list[dict[str, Any]] = []
        for i in installs:
            plugin_observation = _plugin_inventory_observation(i, self.runtime_store)
            plugins = canonical_inventory(plugin_observation.get("inventory"))
            plugin_inventory_observations.append(plugin_observation)
            sender = _detect_sender_profile(i.root, plugins)
            install_type = str(i.install_type or detect_install_type(i.root)).strip().lower() or "unknown"
            runtime_profile = build_runtime_profile(
                runtime=i.runtime,
                install_type=install_type,
                capabilities=i.runtime_capabilities,
            )
            instances.append(
                {
                    "instance_uid": i.instance_uid,
                    "name": i.name,
                    "root": i.root,
                    "source": i.source,
                    "domains": list(i.domains or ([] if not i.primary_domain else [i.primary_domain])),
                    "mautic_major": i.mautic_major,
                    "mautic_version": collect_mautic_version(
                        i.root,
                        self.cfg.php_bin,
                        console_path=i.console_path,
                        run_as_user=self.cfg.mautic_run_as_user,
                        expected_major=i.mautic_major,
                    ),
                    "runtime": str(i.runtime or "host").strip().lower() or "host",
                    "install_type": install_type,
                    "runtime_capabilities": release_capabilities(i.runtime, install_type, runtime_profile.capabilities),
                    "runtime_adapter": str(i.runtime_adapter or ""),
                    "runtime_image_ref": str(i.runtime_image_ref or ""),
                    "docker_runtime_capability": getattr(i, "docker_runtime_capability", None) if str(i.runtime or "").strip().lower() == "docker" else None,
                    "runtime_profile": runtime_profile.safe_dict(),
                    "plugins": plugins,
                    "plugin_inventory": plugin_observation,
                    "sender_type": str(sender.get("sender_type", "") or "").strip() or "unknown",
                    "sender_key": str(sender.get("sender_key", "") or "").strip() or "unknown",
                    "sender_title": str(sender.get("sender_title", "") or "").strip(),
                    "sender_config": sender.get("sender_config") if isinstance(sender.get("sender_config"), dict) else {},
                    "logical_issues": read_logical_issues_snapshot(
                        self.cfg.state_db_path,
                        i.root,
                        runtime_store=self.runtime_store,
                    ),
                    "message_queue": collect_message_queue_snapshot(i),
                    "plugin_operations_runtime": self._plugin_operations_runtime(i.root),
                }
            )
        instances.sort(key=lambda x: str(x["instance_uid"]))

        payload: dict[str, Any] = {
            "schema": "mcd-state-v1",
            "control_receipts": control_store.receipts(),
            "control_capabilities": {"schema": "mcd-control-capabilities-v1", "version": "1.0", "commands": ["docker_runtime_preflight_v1"]},
            "inventory_sync_status": _plugin_inventory_sync_payload(self.runtime_store).get("status", "ok"),
            "plugin_inventory_sync": _plugin_inventory_sync_payload(self.runtime_store),
            "plugin_inventory_observations": plugin_inventory_observations,
            "docker_runtime_image_inventory_capability": docker_image_inventory_capability(),
            "docker_runtime_image_inventory": docker_image_inventory,
            "result_signing_key": result_key,
            **state_identity,
            "hostname": str(identity.get("effective_hostname") or ""),
            "mcc_host_name": str(identity.get("effective_mcc_host_name") or ""),
            "agent_hostname": str(identity.get("local_hostname") or ""),
            "configured_host_name": str(identity.get("configured_host_name") or ""),
            "template_state": {
                "is_template": bool(identity.get("is_template", False)),
                "autopromote_on_clone": bool(identity.get("autopromote_on_clone", True)),
                "clone_detected": bool(identity.get("clone_detected", False)),
                "source_host_name": str(identity.get("source_host_name") or ""),
            },
            "profile": (profile_name or "").strip().lower(),
            "hardware_service_profile": {
                "name": str(getattr(self.cfg, "hardware_profile_name", "auto") or "auto").strip().lower(),
                "scope": "php_fpm_mysql_nginx_redis_os",
            },
            "runtime_overrides": _runtime_overrides_for_state_push(self.cfg),
            "maintenance_state": collect_maintenance_state(self.cfg),
            "config_state": {
                "path": self.cfg.config_file_path,
                "schema_version": int(self.cfg.config_schema_version),
                "customized": bool(self.cfg.config_customized),
                "sha256": self.cfg.config_sha256,
                "toml": _read_config_text(self.cfg.config_file_path),
            },
            "update_state": {
                "policy": self.cfg.mcd_update_policy,
                "auto_update_enabled": bool(self.cfg.mcd_auto_update_enabled),
                "allow_test_build": bool(self.cfg.mcd_update_allow_test_build),
            },
            "backup_state": backup_state_for_push(self.cfg),
            "backup_profile": backup_profile_for_push(self.cfg),
            "backup_profile_sync": backup_profile_sync_payload(self.cfg),
            "apt_state": self._apt_state(now_ts),
            "mautic_install_readiness": collect_mautic_install_readiness(),
            "cluster_db": self._cluster_db_state(now_ts),
            "state_backend": self._state_backend_payload(now_ts),
            "cluster_assets": self._cluster_assets_payload(now_ts, installs),
            "instances": instances,
            "instances_snapshot_complete": bool(instances_snapshot_complete),
            "instance_sizes": collect_instance_sizes(installs),
            "sent_at_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _advertise_image_compatibility(payload)
        payload.update(agent_version_payload())
        if include_signals:
            payload["signals"] = self._signals_payload()
            payload["signals_collected_at_ts"] = int(self.latest_signals_ts or 0)
        if isinstance(profile_event, dict) and profile_event:
            payload["profile_event"] = profile_event
        self._store_state_snapshot(payload, now_ts)
        return payload

    def send(
        self,
        payload: dict[str, Any],
        timeout_sec: int = _DEFAULT_STATE_PUSH_TIMEOUT_SEC,
    ) -> tuple[bool, str]:
        if not self.enabled():
            return False, "push disabled"
        payload_hash = _hash_payload(payload)
        base = str(self.cfg.mcc_url).rstrip("/")
        url = base + "/api/v1/agent/state"
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.mcc_token}",
                "X-MCD-Agent-Version": __version__,
            },
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                code = int(getattr(resp, "status", 200))
                body = (resp.read() or b"").decode("utf-8", errors="replace")
                if 200 <= code < 300:
                    executed_receipts: list[dict[str, Any]] = []
                    if body.strip():
                        try:
                            control = json.loads(body)
                            if isinstance(control, dict) and (
                                control.get("schema") == SCHEMA or isinstance(control.get("control"), dict)
                            ):
                                _ingest_control_response(self.cfg.state_db_path, payload, control)
                                result_key = payload.get("result_signing_key")
                                executed_receipts = self._execute_pending_control_jobs(
                                    ControlStore(self.cfg.state_db_path),
                                    agent_id=str(payload.get("agent_id") or ""),
                                    host_fingerprint=str(payload.get("host_fingerprint") or ""),
                                    result_key=result_key if isinstance(result_key, dict) else None,
                                    key_path=str(getattr(self.cfg, "result_signing_key_path", "") or ""),
                                )
                        except (ValueError, json.JSONDecodeError):
                            pass
                    self._control_push_pending = bool(executed_receipts)
                    _ack_plugin_inventory_sync(self.runtime_store, payload)
                    mark_state_snapshot_push_result_mysql(
                        self.cfg,
                        payload_hash=payload_hash,
                        delivered=True,
                        message=body.strip() or "ok",
                    )
                    return True, body.strip() or "ok"
                mark_state_snapshot_push_result_mysql(
                    self.cfg,
                    payload_hash=payload_hash,
                    delivered=False,
                    message=f"http {code}: {body.strip()}",
                )
                _mark_plugin_inventory_sync_failure(self.runtime_store, f"http {code}: {body.strip()}")
                return False, f"http {code}: {body.strip()}"
        except HTTPError as e:
            try:
                msg = (e.read() or b"").decode("utf-8", errors="replace").strip()
            except Exception:
                msg = ""
            reconciled = _reconcile_control_receipt_conflict(self.cfg.state_db_path, msg) if e.code == 409 else 0
            if reconciled:
                self._control_push_pending = False
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=f"http {e.code}: {msg or e.reason}",
            )
            _mark_plugin_inventory_sync_failure(self.runtime_store, f"http {e.code}: {msg or e.reason}")
            return False, f"http {e.code}: {msg or e.reason}"
        except URLError as e:
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=f"urlerror: {e.reason}",
            )
            _mark_plugin_inventory_sync_failure(self.runtime_store, f"urlerror: {e.reason}")
            return False, f"urlerror: {e.reason}"
        except Exception as e:
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=str(e),
            )
            _mark_plugin_inventory_sync_failure(self.runtime_store, str(e))
            return False, str(e)


def safe_signals_level(payload: dict[str, Any] | None) -> int:
    if not payload:
        return 0
    try:
        overall = payload.get("overall")
        if isinstance(overall, dict):
            return int(overall.get("level", 0) or 0)
    except Exception:
        return 0
    return 0


def should_poll_alert(now_ts: float, last_poll_ts: float, interval_sec: int) -> bool:
    return (now_ts - last_poll_ts) >= max(1, int(interval_sec))


def log_push_result(ok: bool, msg: str) -> None:
    if ok:
        logging.info("MCC push ok: %s", msg)
    else:
        logging.warning("MCC push failed: %s", msg)


def push_state_now(
    cfg: AgentConfig,
    *,
    profile_name: str | None = None,
    include_signals: bool = False,
    runtime_store: Any | None = None,
) -> tuple[bool, str]:
    owned_runtime_store = None
    if runtime_store is None:
        # Imported lazily because daemon imports this module for its regular
        # pusher. One shared store keeps MySQL authoritative and avoids opening
        # one SQLite connection per instance during manual pushes.
        from mcd_agent.daemon import TaskStore

        owned_runtime_store = TaskStore(cfg.state_db_path, cfg)
        runtime_store = owned_runtime_store
    try:
        pusher = MCCStatePusher(cfg, runtime_store=runtime_store)
        if not pusher.enabled():
            return False, "push disabled"

        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)
        installs = inv.list_instances()
        now_ts = datetime.now(timezone.utc).timestamp()
        profile_event = read_pending_profile_event(cfg)
        payload = pusher.build_payload(
            installs=installs,
            profile_name=(profile_name if profile_name is not None else cfg.profile_name),
            now_ts=now_ts,
            include_signals=include_signals,
            profile_event=profile_event,
        )
        ok, msg = pusher.send(payload)
        if profile_event:
            clear_pending_profile_event(
                cfg,
                event_id=str(profile_event.get("event_id", "")).strip() or None,
                delivered=bool(ok),
                error=None if ok else str(msg),
            )
        return ok, msg
    finally:
        if owned_runtime_store is not None:
            owned_runtime_store.close()
