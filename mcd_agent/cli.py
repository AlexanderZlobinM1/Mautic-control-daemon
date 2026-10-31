from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time

from mcd_agent import __version__
from mcd_agent.backup import (
    backup_preflight,
    backup_profile_apply_sync,
    backup_profile_masked,
    backup_profile_set,
    backup_prune,
    backup_restore,
    backup_instance_run,
    backup_run,
    backup_status,
    cluster_backup_files_assemble,
    cluster_backup_files_produce,
    cluster_backup_files_snapshot,
    cluster_backup_local_full,
    cluster_backup_local_incremental,
    cluster_backup_offsite,
    cluster_backup_offsite_dry_run,
    cluster_backup_retention_plan,
    cluster_backup_status,
)
from mcd_agent.apt_profile import (
    clear_apt_repo_profile_markers,
    collect_apt_state,
    ensure_zabbix_mysql_monitor_user,
)
from mcd_agent.admin_user import clear_hostnet_auth_mfa, hostnet_auth_mfa_status, reset_admin_password
from mcd_agent.config import load_config
from mcd_agent.commercial_portal import CommercialState, PortalClient
from mcd_agent.cluster_assets import (
    collect_cluster_assets_status,
    fix_cluster_asset_permissions,
    format_cluster_assets_text,
    guard_cluster_assets,
    reload_cluster_asset_runtime,
)
from mcd_agent.cluster_routing import (
    cluster_local_identity_values,
    cluster_route_for_command,
    cluster_route_targets,
)
from mcd_agent.contact_count_report import collect_contact_count_report
from mcd_agent.contact_field_metadata_report import (
    collect_contact_field_metadata_report,
    contact_field_metadata_error,
)
from mcd_agent.mautic_version_cache import collect_mautic_version
from mcd_agent.custom_scripts import fetch_custom_manifest, format_custom_scripts_list, run_custom_script_by_key
from mcd_agent.db import MauticDB
from mcd_agent.daemon import (
    TaskStore,
    _reconcile_running_state,
    list_external_runtime_task_summaries,
    run_loop,
)
from mcd_agent.discovery import discover_mautic
from mcd_agent.email_activity_report import collect_email_activity_report
from mcd_agent.email_counters import audit_campaign_email_counters, repair_campaign_email_counters
from mcd_agent.env import (
    build_policy_plan,
    default_policy,
    ipv6_runtime_disabled,
    ipv6_status,
    parse_policy_text,
    set_ipv6_disabled,
)
from mcd_agent.executor import (
    build_mautic_exec_args,
    command_task_type,
    execute_mautic_command,
)
from mcd_agent.fs_permissions import effective_guard_identity, ensure_instance_permissions
from mcd_agent.runtime_descriptor import descriptor_for_root
from mcd_agent.instance_delete import delete_instance_artifacts
from mcd_agent.instance_runtime import apply_instance_runtime
from mcd_agent.instance_migrate import (
    collect_source_probe,
    finalize_target_relay,
    format_source_probe_json,
    format_source_probe_text,
    import_target_db_stream,
    preflight_target_relay,
    receive_target_letsencrypt,
    receive_target_files,
    run_target_pull_migration,
    stream_source_db,
    stream_source_files,
    stream_source_letsencrypt,
)
from mcd_agent.source_operation import source_operation_lease
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.install_type import detect_install_type
from mcd_agent.hardware_profile import (
    profile_selection_path,
    read_memory_kib,
    read_profile_selection,
    reconcile_hardware_profile,
    recommended_profile,
    write_profile_selection,
)
from mcd_agent.mautic_composer_move import move_zip_to_composer
from mcd_agent.mautic_image_install import install_from_image
from mcd_agent.mautic_upgrade import (
    run_upgrade_apply,
    run_upgrade_authorize_repair,
    run_upgrade_check,
    run_upgrade_composer_prepare,
    run_upgrade_interactive,
    run_upgrade_preflight,
)
from mcd_agent.mautic6_core_patch import (
    ensure_m6_plugin_update_metadata_patch,
    patch_status as mautic6_patch_status,
    revert_m6_plugin_update_metadata_patch,
)
from mcd_agent.mautic713_import_tag_patch import (
    ensure_patch as ensure_mautic713_import_tag_patch,
    patch_status as mautic713_import_tag_patch_status,
    revert_patch as revert_mautic713_import_tag_patch,
)
from mcd_agent.mautic_locks import cleanup_stale_mautic_file_locks
from mcd_agent.local_mail import (
    configure_local_mail,
    disable_local_mail,
    local_mail_status,
    receive_local_mail,
    submit_local_mail,
)
from mcd_agent.logical_issues import (
    read_logical_issues_snapshot,
    remediate_logical_issue,
    remediate_logical_issues,
    scan_install_logical_issues,
)
from mcd_agent.mail_config import apply_mail_profile, preflight_mail_profile
from mcd_agent.maintenance_mode import collect_maintenance_state, restore_cron_service_if_needed, stop_cron_service
from mcd_agent.mode import (
    _resolve_mutable_config_path,
    profile_set,
    profile_status,
    reconcile_managed_cron,
)
from mcd_agent.nginx_baseline import default_stub_operation, ensure_nginx_baseline
from mcd_agent.plugins import run_plugins_interactive
from mcd_agent.plugin_operations import (
    effective_values,
    operation_command_tokens,
    operation_completion,
    operations_for_instance,
    validate_operation_values,
)
from mcd_agent.runtime_overrides import (
    apply_remote_overrides,
    fetch_runtime_overrides,
    local_runtime_overrides,
    push_runtime_overrides,
    touch_poll_trigger,
)
from mcd_agent.shorteners import check_yourls_update, discover_yourls, update_yourls, yourls_version
from mcd_agent.service_profiles import fetch_service_profile, service_profiles_apply_once
from mcd_agent.signals import collect_signals, format_signals_json, format_signals_text
from mcd_agent.state_push import push_state_now, queue_profile_event
from mcd_agent.state_backend import create_state_database_with_admin, state_backend_status, state_database_exists
from mcd_agent.self_update import apply_update, check_with_mcc, maybe_auto_update, update_status
from mcd_agent.tuner import format_tune_result, tune_segments
from mcd_agent.uninstall import run_uninstall
from mcd_agent.version_identity import agent_version_payload, installed_agent_version
from mcd_agent.version_check import maybe_notify_update

_MANUAL_CMD_SEP = "\x1f"


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _push_state_after_change(cfg, reason: str, *, runtime_store=None) -> None:
    try:
        ok, msg = push_state_now(cfg, include_signals=False, runtime_store=runtime_store)
        if ok:
            logging.info("MCC immediate push (%s): %s", reason, msg)
        else:
            logging.warning("MCC immediate push (%s) skipped/failed: %s", reason, msg)
    except Exception as e:
        logging.warning("MCC immediate push (%s) failed: %s", reason, e)


def _unit_exists(service: str) -> bool:
    proc = subprocess.run(["systemctl", "list-unit-files", f"{service}.service", "--no-legend"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    return f"{service}.service" in str(proc.stdout or "")


def _php_fpm_services() -> list[str]:
    proc = subprocess.run(
        ["systemctl", "list-unit-files", "php*-fpm.service", "--type=service", "--no-legend"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        name = raw.strip().split(None, 1)[0].strip()
        if name.endswith(".service"):
            out.append(name.removesuffix(".service"))
    return sorted(set(out))


def _service_enable_and_start(service: str, lines: list[str]) -> bool:
    enabled = subprocess.run(["systemctl", "is-enabled", service], capture_output=True, text=True)
    enabled_state = (enabled.stdout or enabled.stderr or "").strip()
    allowed_enabled_states = {"enabled", "enabled-runtime", "static", "alias", "indirect", "generated"}
    if enabled.returncode != 0 or enabled_state not in allowed_enabled_states:
        lines.append(f"postcheck: {service} enable required (state={enabled_state or 'disabled'})")
        en = subprocess.run(["systemctl", "enable", service], capture_output=True, text=True)
        if en.returncode != 0:
            lines.append(f"postcheck: {service} enable failed: {(en.stderr or en.stdout or '').strip()}")
            return False
        lines.append(f"postcheck: {service} enable OK")
    else:
        lines.append(f"postcheck: {service} enabled ({enabled_state})")

    active = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True)
    active_state = (active.stdout or active.stderr or "").strip()
    if active.returncode != 0 or active_state != "active":
        lines.append(f"postcheck: {service} start required (state={active_state or 'inactive'})")
        st = subprocess.run(["systemctl", "start", service], capture_output=True, text=True)
        if st.returncode != 0:
            lines.append(f"postcheck: {service} start failed: {(st.stderr or st.stdout or '').strip()}")
            return False
        active = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True)
        active_state = (active.stdout or active.stderr or "").strip()
        if active.returncode != 0 or active_state != "active":
            lines.append(f"postcheck: {service} still not active after start (state={active_state or 'inactive'})")
            return False
        lines.append(f"postcheck: {service} start OK")
    else:
        lines.append(f"postcheck: {service} active")
    return True


def _apt_service_postcheck() -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True

    if _unit_exists("nginx"):
        if not _service_enable_and_start("nginx", lines):
            return False, lines
        baseline = ensure_nginx_baseline(reload_service=True)
        baseline_actions = baseline.get("actions") if isinstance(baseline, dict) else []
        if isinstance(baseline_actions, list):
            for action in baseline_actions:
                lines.append(f"postcheck: nginx baseline {action}")
        if str(baseline.get("status", "") if isinstance(baseline, dict) else "").lower() == "error":
            lines.append(f"postcheck: nginx baseline failed: {baseline.get('error', 'unknown error')}")
            return False, lines
        test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if test.returncode != 0:
            lines.append(f"postcheck: nginx -t failed: {(test.stderr or test.stdout or '').strip()}")
            return False, lines
        lines.append("postcheck: nginx -t OK")
    else:
        lines.append("postcheck: nginx service not present, skip")

    db_service = "mariadb" if _unit_exists("mariadb") else ("mysql" if _unit_exists("mysql") else "")
    if db_service:
        ok = _service_enable_and_start(db_service, lines) and ok
    else:
        lines.append("postcheck: mysql/mariadb service not present, skip")

    if _unit_exists("cron"):
        ok = _service_enable_and_start("cron", lines) and ok
    else:
        lines.append("postcheck: cron service not present, skip")

    php_services = _php_fpm_services()
    if php_services:
        php_ok = False
        for service in php_services:
            if _service_enable_and_start(service, lines):
                php_ok = True
            else:
                ok = False
        if not php_ok:
            lines.append("postcheck: no php-fpm service recovered")
            ok = False
    else:
        lines.append("postcheck: php-fpm service not present, skip")

    return ok, lines


def _state_backend_config_and_status(cfg) -> tuple[object, dict[str, object]]:
    cfg_eff = cfg
    # Status must reflect effective runtime (local config + MCC runtime overrides),
    # otherwise CLI can show legacy while daemon already runs in mysql_hybrid.
    try:
        fetched = fetch_runtime_overrides(cfg)
        if str(fetched.get("status", "")).strip().lower() == "ok":
            ro = fetched.get("runtime_overrides")
            if isinstance(ro, dict) and ro:
                applied = apply_remote_overrides(cfg, ro)
                cfg_eff = applied.get("config", cfg)
    except Exception:
        cfg_eff = cfg
    try:
        raw = state_backend_status(cfg_eff, probe=True)
        if isinstance(raw, dict):
            return cfg_eff, raw
    except Exception as e:
        return cfg_eff, {
            "desired_backend": "unknown",
            "active_backend": "sqlite",
            "mode": "legacy",
            "reason": "status_error",
            "error": str(e),
        }
    return cfg_eff, {
        "desired_backend": "unknown",
        "active_backend": "sqlite",
        "mode": "legacy",
        "reason": "status_unavailable",
    }


def _state_backend_status_payload(cfg) -> dict[str, object]:
    _cfg_eff, status = _state_backend_config_and_status(cfg)
    return status


def _print_state_backend_status(cfg) -> dict[str, object]:
    st = _state_backend_status_payload(cfg)
    print(json.dumps(st, ensure_ascii=True, indent=2))
    return st


def _state_db_missing_only(cfg, st: dict[str, object]) -> bool:
    mode = str(st.get("mode", "") or "").strip().lower()
    active = str(st.get("active_backend", "") or "").strip().lower()
    if mode != "legacy" or active not in {"", "sqlite"}:
        return False
    exists, msg = state_database_exists(cfg)
    if msg == "ok":
        if not bool(exists):
            return True
        reason = str(st.get("reason", "") or "").strip().lower()
        error = str(st.get("error", "") or "").strip().lower()
        return reason == "mysql_schema_unavailable" and any(
            token in error
            for token in (
                "state schema not initialized",
                "state schema version missing",
                "unsupported state schema version",
            )
        )
    txt = str(msg or "").lower()
    if any(token in txt for token in ("unknown database", "access denied", "denied")):
        return True
    reason = str(st.get("reason", "") or "").strip().lower()
    return reason in {"legacy_sqlite_mode", "mysql_config_missing", "mysql_init_failed", "status_error", "status_unavailable"}


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    return json.dumps("" if value is None else str(value), ensure_ascii=True)


def _upsert_section_values(config_path: str, section_name: str, updates: dict[str, object]) -> str:
    if not updates:
        return str(_resolve_mutable_config_path(config_path))
    p = _resolve_mutable_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    section_lines = [f"{k} = {_toml_literal(v)}" for k, v in updates.items()]
    section = f"[{section_name}]\n" + "\n".join(section_lines) + "\n\n"
    m = re.search(rf"(?ms)^(\[{re.escape(section_name)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        p.write_text(section + text, encoding="utf-8")
        return str(p)
    body = m.group(2)
    key_set = set(updates.keys())
    out_lines: list[str] = []
    key_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
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
    text2 = text[: m.start(2)] + new_body + text[m.end(2) :]
    p.write_text(text2, encoding="utf-8")
    return str(p)


def _upsert_runtime_values(config_path: str, updates: dict[str, object]) -> str:
    return _upsert_section_values(config_path, "runtime", updates)


def _upsert_state_values(config_path: str, updates: dict[str, object]) -> str:
    return _upsert_section_values(config_path, "state", updates)


def _gen_state_runtime_password(length: int = 28) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(max(16, int(length))))


def _is_mysql_auth_error(msg: str) -> bool:
    txt = str(msg or "").strip().lower()
    return (
        "access denied" in txt
        or "using password" in txt
        or "authentication failed" in txt
    )


def _ipv6_disabled_now() -> bool | None:
    return ipv6_runtime_disabled(ipv6_status())


def _state_runtime_bootstrap_defaults(cfg) -> dict[str, object]:
    runtime_user = str(cfg.state_mysql_user or "").strip()
    if not runtime_user or runtime_user.lower() == "root":
        runtime_user = "mcd_state"
    runtime_host = str(cfg.state_mysql_host or "").strip() or "127.0.0.1"
    runtime_socket = str(cfg.state_mysql_unix_socket or "").strip()
    runtime_db = str(cfg.state_mysql_database or "").strip() or "mcd_state"
    runtime_port = int(cfg.state_mysql_port or 3306)
    return {
        "state_backend": "mysql_hybrid",
        "state_mysql_host": runtime_host,
        "state_mysql_port": runtime_port,
        "state_mysql_database": runtime_db,
        "state_mysql_user": runtime_user,
        "state_mysql_password": _gen_state_runtime_password(),
        "state_mysql_unix_socket": runtime_socket,
    }


def _state_section_bootstrap_updates(runtime: dict[str, object]) -> dict[str, object]:
    return {
        "backend": str(runtime.get("state_backend") or "mysql_hybrid"),
        "mysql_host": str(runtime.get("state_mysql_host") or "127.0.0.1"),
        "mysql_port": int(runtime.get("state_mysql_port") or 3306),
        "mysql_database": str(runtime.get("state_mysql_database") or "mcd_state"),
        "mysql_user": str(runtime.get("state_mysql_user") or "mcd_state"),
        "mysql_password": str(runtime.get("state_mysql_password") or ""),
        "mysql_unix_socket": str(runtime.get("state_mysql_unix_socket") or ""),
    }


def _bootstrap_state_db_with_admin(
    cfg,
    *,
    admin_user: str,
    admin_password: str | None,
    admin_host: str | None = None,
    admin_port: int | None = None,
    admin_socket: str | None = None,
) -> tuple[bool, str, object]:
    runtime = _state_runtime_bootstrap_defaults(cfg)
    ok, msg = create_state_database_with_admin(
        cfg,
        admin_user=admin_user,
        admin_password=admin_password if admin_password not in {"", None} else None,
        admin_host=admin_host,
        admin_port=admin_port,
        admin_unix_socket=admin_socket,
        runtime_user=str(runtime["state_mysql_user"]),
        runtime_password=str(runtime["state_mysql_password"]),
        runtime_database=str(runtime["state_mysql_database"]),
        runtime_host=str(runtime["state_mysql_host"]),
        runtime_port=int(runtime["state_mysql_port"]),
        runtime_unix_socket=str(runtime["state_mysql_unix_socket"] or ""),
    )
    if not ok:
        return False, msg, cfg
    runtime_updates = {
        "state_backend": runtime["state_backend"],
        "state_mysql_host": runtime["state_mysql_host"],
        "state_mysql_port": runtime["state_mysql_port"],
        "state_mysql_database": runtime["state_mysql_database"],
        "state_mysql_user": runtime["state_mysql_user"],
        "state_mysql_password": runtime["state_mysql_password"],
        "state_mysql_unix_socket": runtime["state_mysql_unix_socket"],
    }
    state_updates = _state_section_bootstrap_updates(runtime)
    persisted = _upsert_state_values(cfg.config_file_path, state_updates)
    cfg2 = load_config(cfg.config_file_path)
    sync_msg = "mcc_runtime_sync=skipped"
    try:
        pushed = push_runtime_overrides(cfg2, runtime_updates, merge=True, target="desired")
        pstatus = str(pushed.get("status", "")).strip().lower()
        if pstatus in {"ok", "disabled"}:
            touch_poll_trigger(cfg2)
            sync_msg = f"mcc_runtime_sync={pstatus}"
        else:
            preason = str(pushed.get("reason", "")).strip()
            sync_msg = f"mcc_runtime_sync=failed({preason or pstatus or 'unknown'})"
    except Exception as e:
        sync_msg = f"mcc_runtime_sync=failed({e})"
    return True, f"{msg}; runtime saved in {persisted}; {sync_msg}", cfg2


def _default_config_path() -> str:
    # Priority:
    # 1) explicit env override
    # 2) standard install path
    # 3) /etc symlink path
    # 4) local repo example (dev fallback)
    env_cfg = (os.getenv("MCD_CONFIG", "") or "").strip()
    if env_cfg:
        return env_cfg
    candidates = (
        "/opt/mcd/etc/mcd.toml",
        "/etc/mcd/mcd.toml",
    )
    for p in candidates:
        if Path(p).exists():
            return p
    fallback = Path(__file__).resolve().parents[1] / "etc" / "mcd-agent.example.toml"
    if fallback.exists():
        return str(fallback)
    return candidates[0]


def _select_root_for_ops(cfg, root: str | None, *, allow_missing_absolute: bool = False) -> str:
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    if root:
        for inst in installs:
            if inst.root == root or inst.instance_uid == root:
                return inst.root
        if allow_missing_absolute and Path(root).is_absolute():
            return root
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0].root


def _select_instance_for_ops(cfg, root: str | None):
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    target = str(root or "").strip()
    if target:
        for inst in installs:
            domains = [str(x).strip().lower() for x in (getattr(inst, "domains", []) or [])]
            if (
                inst.root == target
                or inst.instance_uid == target
                or inst.name == target
                or str(inst.primary_domain or "").strip().lower() == target.lower()
                or target.lower() in domains
            ):
                return inst
        raise RuntimeError(f"Mautic install not found for root: {target}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0]


def _plugin_operation_cli(cfg, args) -> int:
    inst = _select_instance_for_ops(cfg, args.root)
    operations = operations_for_instance(cfg, inst)
    if args.plugin_ops_cmd == "complete":
        values = operation_completion(cfg, inst, args.prefix)
        print(json.dumps(values, ensure_ascii=True) if args.json else "\n".join(values))
        return 0
    if args.plugin_ops_cmd == "list":
        payload = [
            {
                "operation_key": item.get("operation_key"),
                "bundle": item.get("bundle"),
                "operation": item.get("operation"),
                "values": effective_values(cfg, inst, item),
            }
            for item in operations
        ]
        print(json.dumps(payload, ensure_ascii=True, indent=2, default=str) if args.json else "\n".join(str(row["operation_key"]) for row in payload))
        return 0
    key = str(args.operation_key or "").strip().lower()
    item = next((row for row in operations if str(row.get("operation_key", "")).lower() == key), None)
    if item is None:
        raise RuntimeError(f"declared operation not found: {key or '<missing>'}")
    operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
    supplied: dict[str, object] = {}
    for expression in args.set:
        if "=" not in expression:
            raise RuntimeError(f"invalid --set value: {expression}")
        field, value = expression.split("=", 1)
        supplied[field.strip()] = value
    values = validate_operation_values(item, {**effective_values(cfg, inst, item), **supplied})
    if args.plugin_ops_cmd == "set":
        settings = getattr(cfg, "plugin_operation_instance_settings", {})
        settings = dict(settings) if isinstance(settings, dict) else {}
        block = dict(settings.get(inst.instance_uid, {})) if isinstance(settings.get(inst.instance_uid), dict) else {}
        block[key] = values
        settings[inst.instance_uid] = block
        _upsert_runtime_values(cfg.config_file_path, {"plugin_operation_instance_settings": settings})
        cfg2 = load_config(cfg.config_file_path)
        sync = push_runtime_overrides(cfg2, {"plugin_operation_instance_settings": settings}, merge=True, target="desired")
        touch_poll_trigger(cfg2)
        payload = {"status": "ok", "operation_key": key, "values": values, "mcc_sync": sync}
        print(json.dumps(payload, ensure_ascii=True, indent=2) if args.json else json.dumps(payload, ensure_ascii=True))
        return 0 if str(sync.get("status", "ok")).lower() in {"ok", "disabled"} else 1
    if str(operation.get("mode", "")).lower() != "action":
        raise RuntimeError("only action operations can be run")
    argv = operation_command_tokens(item, values)
    executable = shutil.which("mcd-cli") or sys.executable
    command = [executable, *argv] if executable != sys.executable else [executable, "-m", "mcd_agent.cli", *argv]
    result = subprocess.run(command, cwd=str(inst.root), text=True, capture_output=True)
    output = (result.stdout or result.stderr or "").strip()
    payload = {"status": "ok" if result.returncode == 0 else "error", "operation_key": key, "argv": argv, "output": output}
    print(json.dumps(payload, ensure_ascii=True, indent=2) if args.json else output)
    return int(result.returncode)


def _run_manual_command_with_scheduler(
    *,
    cfg,
    root: str,
    command: str,
    instance_id: int | None,
    php_bin: str,
    timeout_sec: int,
    run_as_user: str | None,
) -> tuple[int, str]:
    task_type = command_task_type(command)
    if (
        str(getattr(cfg, "profile_name", "") or "").strip().lower() == "passive"
        and task_type is not None
    ):
        return 2, (
            "passive profile rejects MCD user-task execution; "
            "cron remains external and maintenance must be explicit"
        )
    route = cluster_route_for_command(command)
    route_targets = cluster_route_targets(cfg, route)
    if route_targets:
        try:
            cmd_args = build_mautic_exec_args(
                php_bin=php_bin,
                root=root,
                command=command,
                instance_id=instance_id,
                run_as_user=run_as_user,
            )
        except ValueError as e:
            return 2, str(e)
        except FileNotFoundError as e:
            return 3, str(e)

        route_task_type = task_type
        if route_task_type is None and command == "cache:clear":
            route_task_type = "cache_clear"
        elif route_task_type is None and command == "cache:warmup":
            route_task_type = "cache_warm"
        if route_task_type is None:
            # Route is configured but this command has no scheduler task
            # representation. Fall back to local execution for compatibility.
            return execute_mautic_command(
                php_bin=php_bin,
                root=root,
                command=command,
                instance_id=instance_id,
                timeout_sec=timeout_sec,
                run_as_user=run_as_user,
            )

        store = TaskStore(cfg.state_db_path, cfg)
        local_ids = cluster_local_identity_values(cfg)
        local_ids.add(str(getattr(store, "_node_id", "") or "").strip().lower())
        remote_targets = [t for t in route_targets if str(t or "").strip().lower() not in local_ids]
        if remote_targets and not bool(getattr(store, "_mysql_mode", False)):
            return 2, (
                "cluster route requires mysql_hybrid state backend; "
                f"route={route} targets={','.join(route_targets)}"
            )
        if not remote_targets:
            return execute_mautic_command(
                php_bin=php_bin,
                root=root,
                command=command,
                instance_id=instance_id,
                timeout_sec=timeout_sec,
                run_as_user=run_as_user,
            )

        reqs: list[tuple[str, int]] = []
        command_payload = _MANUAL_CMD_SEP.join(cmd_args)
        for target in route_targets:
            target_clean = str(target or "").strip()
            if not target_clean:
                continue
            req_id = store.enqueue_manual_request(
                root=root,
                task_type=route_task_type,
                entity_id=instance_id,
                command_str=command_payload,
                timeout_sec=timeout_sec,
                target_host_name=target_clean,
            )
            reqs.append((target_clean, req_id))
        if not reqs:
            return 2, f"cluster route has no valid targets for route={route}"

        wait_sec = max(1.0, float(timeout_sec or cfg.command_timeout_sec or 0) + 5.0)
        deadline = time.time() + wait_sec
        terminal = {"done", "failed", "timeout", "lost", "skipped", "cancelled"}
        statuses: dict[tuple[str, int], str] = {}
        while time.time() < deadline:
            all_terminal = True
            for target, req_id in reqs:
                key = (target, req_id)
                st = store.get_manual_request_status_for_host(req_id, target) or "unknown"
                statuses[key] = st
                if st.strip().lower() not in terminal:
                    all_terminal = False
            if all_terminal:
                break
            time.sleep(0.15)

        parts = [
            f"{target}:request_id={req_id}:status={statuses.get((target, req_id), 'unknown')}"
            for target, req_id in reqs
        ]
        bad = [
            st
            for st in statuses.values()
            if str(st).strip().lower() in {"failed", "timeout", "lost", "skipped", "cancelled"}
        ]
        unfinished = [
            st
            for st in statuses.values()
            if str(st).strip().lower() not in terminal
        ]
        rc = 1 if bad or unfinished else 0
        return rc, f"cluster routed route={route} " + " ".join(parts)

    return execute_mautic_command(
        php_bin=php_bin,
        root=root,
        command=command,
        instance_id=instance_id,
        timeout_sec=timeout_sec,
        run_as_user=run_as_user,
    )


def _build_cache_hard_local_cli_args(cfg, root: str) -> list[str]:
    exe = shutil.which("mcd-cli")
    if not exe:
        current = str(sys.argv[0] or "").strip()
        exe = current if current and current != "-m" else "mcd-cli"
    return [
        exe,
        "cache:hard",
        "--config",
        str(getattr(cfg, "config_file_path", "") or _default_config_path()),
        "--root",
        str(root),
        "--local",
    ]


def _run_cache_hard_clear_cluster_aware(cfg, root: str) -> tuple[int, str]:
    route_targets = cluster_route_targets(cfg, "cache")
    if not route_targets:
        return _run_cache_hard_clear(cfg, root)

    store = TaskStore(cfg.state_db_path, cfg)
    local_ids = cluster_local_identity_values(cfg)
    local_ids.add(str(getattr(store, "_node_id", "") or "").strip().lower())
    remote_targets = [t for t in route_targets if str(t or "").strip().lower() not in local_ids]
    if remote_targets and not bool(getattr(store, "_mysql_mode", False)):
        return 2, (
            "cluster route requires mysql_hybrid state backend; "
            f"route=cache targets={','.join(route_targets)}"
        )

    reqs: list[tuple[str, int]] = []
    command_payload = _MANUAL_CMD_SEP.join(_build_cache_hard_local_cli_args(cfg, root))
    for target in route_targets:
        target_clean = str(target or "").strip()
        if not target_clean:
            continue
        req_id = store.enqueue_manual_request(
            root=root,
            task_type="cache_hard",
            entity_id=None,
            command_str=command_payload,
            timeout_sec=int(getattr(cfg, "command_timeout_sec", 1800) or 1800),
            target_host_name=target_clean,
        )
        reqs.append((target_clean, req_id))
    if not reqs:
        return 2, "cluster route has no valid targets for route=cache"

    wait_sec = max(1.0, min(8.0, float(getattr(cfg, "dispatch_interval_sec", 2) or 2) * 3.0))
    deadline = time.time() + wait_sec
    terminal = {"launched", "done", "failed", "timeout", "lost", "skipped", "cancelled"}
    statuses: dict[tuple[str, int], str] = {}
    while time.time() < deadline:
        all_terminal = True
        for target, req_id in reqs:
            key = (target, req_id)
            st = store.get_manual_request_status_for_host(req_id, target) or "unknown"
            statuses[key] = st
            if st.strip().lower() not in terminal:
                all_terminal = False
        if all_terminal:
            break
        time.sleep(0.15)

    parts = [
        f"{target}:request_id={req_id}:status={statuses.get((target, req_id), 'unknown')}"
        for target, req_id in reqs
    ]
    bad = [
        st
        for st in statuses.values()
        if str(st).strip().lower() in {"failed", "timeout", "lost", "cancelled"}
    ]
    return (1 if bad else 0), "cluster routed route=cache " + " ".join(parts)


def _select_installs_for_patch(cfg, root: str | None):
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    if root:
        return [x for x in installs if x.root == root or x.instance_uid == root]
    return installs


def _read_runtime_patch_policy(config_path: str) -> str:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return "required"
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\[runtime\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        return "required"
    body = m.group(1)
    pm = re.search(r'(?m)^\s*mautic6_core_patch_policy\s*=\s*"([^"]+)"', body)
    if not pm:
        return "required"
    v = pm.group(1).strip().lower()
    return v if v in {"required", "off"} else "required"


def _write_runtime_patch_policy(config_path: str, value: str) -> str:
    policy = value.strip().lower()
    if policy not in {"required", "off"}:
        raise RuntimeError("invalid policy (allowed: required, off)")
    p = _resolve_mutable_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    runtime_section = f'[runtime]\nmautic6_core_patch_policy = "{policy}"\n\n'
    m = re.search(r"(?ms)^\[runtime\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        p.write_text(runtime_section + text, encoding="utf-8")
        return str(p)
    body = m.group(1)
    if re.search(r'(?m)^\s*mautic6_core_patch_policy\s*=\s*"[^"]+"', body):
        body2 = re.sub(
            r'(?m)^\s*mautic6_core_patch_policy\s*=\s*"[^"]+"',
            f'mautic6_core_patch_policy = "{policy}"',
            body,
            count=1,
        )
    else:
        body2 = f'mautic6_core_patch_policy = "{policy}"\n' + body
    text2 = text[: m.start(1)] + body2 + text[m.end(1) :]
    p.write_text(text2, encoding="utf-8")
    return str(p)


def _pick_active_instance_interactive(cfg, current_root: str | None) -> str | None:
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    rows = inv.list_instances()
    if not rows:
        print("No instances")
        return None
    if current_root:
        for i in rows:
            if i.root == current_root:
                return current_root
    if len(rows) == 1:
        return rows[0].root
    print("")
    print("Select active instance:")
    for idx, i in enumerate(rows, start=1):
        print(f"{idx}. {i.instance_uid} {i.name} root={i.root} source={i.source} major={i.mautic_major}")
    raw = _ask("Select [number, empty=cancel]: ").strip()
    if not raw:
        print("Active instance is required when multiple installs exist.")
        return None
    try:
        n = int(raw)
    except ValueError:
        print("Invalid selection")
        return current_root
    if n < 1 or n > len(rows):
        print("Out of range")
        return current_root
    return rows[n - 1].root


def _prepare_cache_permissions(cfg, root: str) -> tuple[bool, str]:
    cache_dir = Path(root) / "var" / "cache"
    prod_dir = cache_dir / "prod"
    user = cfg.mautic_run_as_user or "www-data"
    cmds: list[list[str]] = [
        ["mkdir", "-p", str(prod_dir)],
        ["chown", "-R", f"{user}:{user}", str(cache_dir)],
        ["chmod", "-R", "u+rwX,g+rwX", str(cache_dir)],
    ]
    lines: list[str] = []
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            lines.append(f"FAIL {' '.join(cmd)} :: {err}")
            return False, "\n".join(lines)
        lines.append(f"OK   {' '.join(cmd)}")

    check = subprocess.run(["sudo", "-u", user, "test", "-w", str(prod_dir)])
    if check.returncode != 0:
        lines.append(f"WARN sudo -u {user} test -w {prod_dir} failed")
        return False, "\n".join(lines)
    lines.append(f"OK   writable by {user}: {prod_dir}")
    return True, "\n".join(lines)


def _run_permissions_fix(cfg, root: str, run_as_user: str | None = None) -> tuple[int, str]:
    runtime = descriptor_for_root(root)
    user = str(run_as_user or "").strip() or effective_guard_identity(
        runtime="docker" if runtime is not None else "host",
        runtime_user=runtime.container_user if runtime is not None else None,
        host_user=cfg.mautic_run_as_user,
    )
    try:
        res = ensure_instance_permissions(
            root=root,
            run_as_user=user,
            guard_paths=list(cfg.fs_permissions_guard_paths or []),
            fix_console_exec=bool(cfg.fs_permissions_guard_fix_console_exec),
            console_relpath=str(cfg.fs_permissions_guard_console_relpath or "bin/console"),
        )
    except Exception as e:
        return 1, f"permissions fix failed: {e}"

    lines: list[str] = []
    lines.append(f"root={root}")
    lines.append(f"user={user}")
    lines.append(
        "checked_paths={checked} repaired_paths={repaired} console_exec_fixed={console_fix} missing_paths={missing}".format(
            checked=len(res.checked_paths),
            repaired=len(res.repaired_paths),
            console_fix=1 if res.console_exec_fixed else 0,
            missing=len(res.missing_paths),
        )
    )
    if res.repaired_paths:
        lines.append("repaired: " + ", ".join(str(x) for x in res.repaired_paths))
    if res.missing_paths:
        lines.append("missing: " + ", ".join(str(x) for x in res.missing_paths))
    if res.errors:
        lines.append("errors: " + "; ".join(str(x) for x in res.errors))
    if not res.repaired_paths and not res.console_exec_fixed and not res.errors:
        lines.append("status: no changes needed")
    return (1 if res.errors else 0), "\n".join(lines)


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid: int, grace_sec: int) -> str:
    if not _is_pid_alive(pid):
        return "already-exited"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return "already-exited"
    deadline = time.time() + max(0, int(grace_sec))
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return "terminated"
        time.sleep(0.2)
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return "terminated"
    return "killed" if not _is_pid_alive(pid) else "failed"


def _tracked_running_tasks(cfg) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    store = None
    try:
        store = TaskStore(cfg.state_db_path, cfg)
        # CLI status is also a lifecycle observation boundary. Reconcile
        # durable rows before reporting them so a dead PID cannot remain a
        # false scheduler gate until the next daemon tick.
        _reconcile_running_state(store=store, running={}, popens={})
        for r in store.running_task_summaries():
            rows.append(
                {
                    "id": int(r.get("id") or 0),
                    "root": str(r.get("root") or ""),
                    "task_type": str(r.get("task_type") or ""),
                    "entity_id": r.get("entity_id"),
                    "pid": int(r.get("pid") or 0),
                    "command_str": str(r.get("command_str") or ""),
                }
            )
    except Exception:
        return []
    finally:
        if store is not None:
            store.close()
    return rows


def _managed_instance_roots(cfg) -> list[str]:
    try:
        inventory = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inventory, cfg)
        return [str(getattr(inst, "root", "") or "").strip() for inst in inventory.list_instances() if str(getattr(inst, "root", "") or "").strip()]
    except Exception:
        return []


def _external_running_tasks(cfg, tracked: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    tracked_rows = tracked if tracked is not None else _tracked_running_tasks(cfg)
    tracked_pids = {int(row.get("pid") or 0) for row in tracked_rows if int(row.get("pid") or 0) > 0}
    roots = _managed_instance_roots(cfg)
    if not roots:
        return []
    try:
        return list_external_runtime_task_summaries(roots, tracked_tasks=tracked_rows, tracked_pids=tracked_pids)
    except Exception:
        return []


def _observed_running_tasks(cfg) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    tracked = _tracked_running_tasks(cfg)
    external = _external_running_tasks(cfg, tracked)
    return tracked, external


def _list_mautic_console_processes() -> list[tuple[int, str]]:
    proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    out: list[tuple[int, str]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        args = parts[1]
        if "bin/console" in args and "mautic:" in args:
            out.append((pid, args))
    return out


def _run_cache_menu(cfg, root: str | None) -> int:
    try:
        target_root = _select_root_for_ops(cfg, root)
    except Exception as e:
        print(f"Cache menu error: {e}")
        return 1

    while True:
        print("")
        print(f"Cache Operations (root={target_root})")
        print("1. Soft Clear (cache:clear)")
        print("2. Warmup (cache:warmup)")
        print("3. Hard Clear (delete var/cache/prod)")
        print("0. Back")
        choice = _ask("Select option: ").strip().lower()
        if choice in {"0", "q", "quit", "exit"}:
            return 0

        ok, msg = _prepare_cache_permissions(cfg, target_root)
        print(msg)
        if not ok:
            print("Cache permissions check failed. Stop.")
            continue

        if choice == "1":
            rc, out = _run_manual_command_with_scheduler(
                cfg=cfg,
                php_bin=cfg.php_bin,
                root=target_root,
                command="cache:clear",
                instance_id=None,
                timeout_sec=cfg.command_timeout_sec,
                run_as_user=cfg.mautic_run_as_user,
            )
            print(out or f"cache:clear rc={rc}")
            continue
        if choice == "2":
            rc, out = _run_manual_command_with_scheduler(
                cfg=cfg,
                php_bin=cfg.php_bin,
                root=target_root,
                command="cache:warmup",
                instance_id=None,
                timeout_sec=cfg.command_timeout_sec,
                run_as_user=cfg.mautic_run_as_user,
            )
            print(out or f"cache:warmup rc={rc}")
            continue
        if choice == "3":
            rc, out = _run_cache_hard_clear_cluster_aware(cfg, target_root)
            print(out or f"cache:hard rc={rc}")
            continue
        print("Unknown option")


def _run_cache_hard_clear(cfg, root: str) -> tuple[int, str]:
    target_root = str(root or "").strip()
    if not target_root:
        return 2, "Active instance is required."
    ok, msg = _prepare_cache_permissions(cfg, target_root)
    lines: list[str] = [msg] if msg else []
    if not ok:
        lines.append("Cache permissions check failed. Stop.")
        return 1, "\n".join([x for x in lines if x])

    prod_dir = Path(target_root) / "var" / "cache" / "prod"
    proc = subprocess.run(["rm", "-rf", str(prod_dir)], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"rm -rf failed rc={proc.returncode}"
        lines.append(err)
        return int(proc.returncode or 1), "\n".join([x for x in lines if x])
    lines.append(f"OK   rm -rf {prod_dir}")

    ok2, msg2 = _prepare_cache_permissions(cfg, target_root)
    if msg2:
        lines.append(msg2)
    if not ok2:
        lines.append("Hard cache cleanup partially completed: permissions repair failed.")
        return 1, "\n".join([x for x in lines if x])
    lines.append("Hard cache cleanup done.")
    return 0, "\n".join([x for x in lines if x])


def _run_mautic6_patch_menu(cfg, root: str | None) -> int:
    while True:
        print("")
        print("Mautic 6 Core Patch")
        print(f"Policy: {_read_runtime_patch_policy(cfg.config_file_path)}")
        print("1. Status")
        print("2. Apply patch")
        print("3. Revert patch")
        print("4. Set policy required (daemon auto-patch)")
        print("5. Set policy off (do not patch)")
        print("0. Back")
        choice = _ask("Select option: ").strip().lower()
        if choice in {"0", "q", "quit", "exit"}:
            return 0
        if choice in {"1", "2", "3"}:
            installs = _select_installs_for_patch(cfg, root)
            if not installs:
                print("No matching instances")
                continue
            for inst in installs:
                if choice == "1":
                    res = mautic6_patch_status(inst, cfg)
                elif choice == "2":
                    res = ensure_m6_plugin_update_metadata_patch(inst, cfg, trigger="operator_action")
                else:
                    res = revert_m6_plugin_update_metadata_patch(inst, cfg)
                print(json.dumps(res, ensure_ascii=True, indent=2))
            if choice == "2":
                _push_state_after_change(cfg, "mautic6-core-patch-apply")
            elif choice == "3":
                _push_state_after_change(cfg, "mautic6-core-patch-revert")
            continue
        if choice in {"4", "5"}:
            policy = "required" if choice == "4" else "off"
            try:
                target = _write_runtime_patch_policy(cfg.config_file_path, policy)
                print(f"Policy written: {policy} ({target})")
                proc = subprocess.run(["systemctl", "restart", "mcd"], capture_output=True, text=True)
                if proc.returncode == 0:
                    print("service restarted: mcd")
                else:
                    err = (proc.stderr or proc.stdout or "").strip()
                    print(f"WARN service restart failed: {err}")
                _push_state_after_change(cfg, "mautic6-core-patch-policy")
            except Exception as e:
                print(f"Policy update error: {e}")
            continue
        print("Unknown option")


def _run_custom_menu(cfg) -> int:
    while True:
        print("")
        print("Custom Scripts")
        try:
            rows, source = fetch_custom_manifest(cfg, use_cache_on_error=True)
        except Exception as e:
            print(f"Custom scripts error: {e}")
            return 1
        print(f"Manifest source: {source}")
        print(format_custom_scripts_list(rows, with_idx=True))
        print("Select by number or key, r=refresh, empty=back")
        raw = _ask("Select script: ").strip()
        if not raw:
            return 0
        if raw.lower() in {"r", "refresh"}:
            continue

        selected = None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(rows):
                selected = rows[idx - 1]
        else:
            for item in rows:
                if str(item.get("key", "")).strip() == raw:
                    selected = item
                    break
        if selected is None:
            print("Invalid selection")
            continue

        key = str(selected.get("key", "")).strip()
        if not key:
            print("Invalid script key in manifest")
            continue
        args_help = str(selected.get("args_help", "")).strip()
        if args_help:
            print(f"Args hint: {args_help}")
        args_line = _ask("Arguments (optional): ").strip()
        try:
            args = shlex.split(args_line) if args_line else []
        except ValueError as e:
            print(f"Arguments parse error: {e}")
            continue
        script_interactive = _to_bool(selected.get("interactive", False))
        default_detach = not script_interactive
        if script_interactive:
            print("Script is marked interactive; foreground mode is recommended.")
        prompt = (
            "Run detached (tmux/screen if available)? [Y/n]: "
            if default_detach
            else "Run detached (tmux/screen if available)? [y/N]: "
        )
        detach_raw = _ask(prompt).strip().lower()
        if not detach_raw:
            detach = default_detach
        else:
            detach = detach_raw in {"y", "yes"}
        rc, out = run_custom_script_by_key(
            cfg,
            script_key=key,
            args=args,
            detach=detach,
            live_output=not detach,
        )
        if out:
            print(out)
        if rc != 0:
            print(f"Script failed rc={rc}")


def _run_interactive_hub(cfg, root: str | None, no_color: bool) -> int:
    active_root = root
    while active_root is None:
        active_root = _pick_active_instance_interactive(cfg, active_root)
        if active_root is None:
            ans = _ask("No active instance selected. Retry? [Y/n]: ").strip().lower()
            if ans in {"n", "no"}:
                return 1

    while True:
        if active_root is None:
            active_root = _pick_active_instance_interactive(cfg, active_root)
            if active_root is None:
                print("Active instance is required.")
                continue
        active_label = active_root or "-"
        print("")
        print(f"MCD Interactive (v{__version__})")
        print(f"Active instance: {active_label}")
        print("1. Plugins")
        print("2. Mautic Upgrade")
        print("3. Instances")
        print("4. Cache")
        print("5. Select Active Instance")
        print("6. Environment")
        print("7. Backup")
        print("8. Mautic6 Core Patch")
        print("9. Custom Scripts")
        print("0. Exit")
        choice = _ask("Select option: ").strip()
        if choice == "1":
            if not active_root:
                print("Select active instance first")
                continue
            try:
                rc = run_plugins_interactive(
                    config=cfg,
                    root=active_root,
                    selection=None,
                    bundles=None,
                    plugin_uids=None,
                    action=None,
                    no_color=bool(no_color),
                    yes=False,
                )
                if rc == 0:
                    _push_state_after_change(cfg, "plugins-interactive")
            except Exception as e:
                print(f"Plugins error: {e}")
                msg = str(e).lower()
                if "repo_base_url" in msg or "manifest" in msg:
                    print("Hint: configure plugins.repo_base_url or mcc.url in mcd config.")
            continue
        if choice == "2":
            if not active_root:
                print("Select active instance first")
                continue
            try:
                rc = run_upgrade_interactive(cfg, active_root)
                if rc == 0:
                    _push_state_after_change(cfg, "mautic-upgrade-interactive")
            except Exception as e:
                print(f"Upgrade error: {e}")
            continue
        if choice == "3":
            inv = InstanceInventory(cfg.state_db_path)
            ensure_seeded(inv, cfg)
            while True:
                print("")
                print("Instances")
                print("1. List")
                print("2. Rescan")
                print("3. Add/Update manual")
                print("4. Remove")
                print("5. Set Active")
                print("0. Back")
                c2 = _ask("Select option: ").strip()
                if c2 == "1":
                    rows = inv.list_instances()
                    for idx, i in enumerate(rows, start=1):
                        mark = "*" if i.root == active_root else " "
                        print(
                            f"{idx}. [{mark}] {i.instance_uid} {i.name} "
                            f"root={i.root} source={i.source} major={i.mautic_major}"
                        )
                    if not rows:
                        print("No instances")
                    continue
                if c2 == "2":
                    try:
                        count = inv.rescan(cfg)
                        print(f"Rescan complete: {count} instances")
                        _push_state_after_change(cfg, "instances-rescan-interactive")
                    except Exception as e:
                        print(f"Rescan error: {e}")
                    continue
                if c2 == "3":
                    name = _ask("name: ").strip()
                    root_v = _ask("root: ").strip()
                    console = _ask("console path: ").strip()
                    lphp = _ask("local.php path (optional): ").strip() or None
                    major_raw = _ask("mautic major (optional): ").strip()
                    major = int(major_raw) if major_raw else None
                    db_host = _ask("db host (optional): ").strip() or None
                    db_port_raw = _ask("db port (optional): ").strip()
                    db_port = int(db_port_raw) if db_port_raw else None
                    db_name = _ask("db name (optional): ").strip() or None
                    db_user = _ask("db user (optional): ").strip() or None
                    db_password = _ask("db password (optional): ").strip() or None
                    db_prefix = _ask("db table prefix (optional): ").strip() or None
                    if not (name and root_v and console):
                        print("name/root/console are required")
                        continue
                    try:
                        inv.add_or_update_manual(
                            name=name,
                            root=root_v,
                            console_path=console,
                            local_php_path=lphp,
                            mautic_major=major,
                            db_host=db_host,
                            db_port=db_port,
                            db_name=db_name,
                            db_user=db_user,
                            db_password=db_password,
                            db_table_prefix=db_prefix,
                        )
                        print("Manual instance saved")
                        _push_state_after_change(cfg, "instances-add-interactive")
                    except Exception as e:
                        print(f"Save manual instance error: {e}")
                    continue
                if c2 == "4":
                    name = _ask("uid, name or root: ").strip()
                    try:
                        if inv.remove(name):
                            if active_root == name:
                                active_root = None
                            print("Removed")
                            _push_state_after_change(cfg, "instances-remove-interactive")
                        else:
                            print("Not found")
                    except Exception as e:
                        print(f"Remove error: {e}")
                    continue
                if c2 == "5":
                    rows = inv.list_instances()
                    if not rows:
                        print("No instances")
                        continue
                    for idx, i in enumerate(rows, start=1):
                        print(f"{idx}. {i.instance_uid} {i.name} root={i.root} source={i.source} major={i.mautic_major}")
                    raw = _ask("Select active [number]: ").strip()
                    try:
                        n = int(raw)
                        if n < 1 or n > len(rows):
                            raise ValueError
                    except ValueError:
                        print("Invalid selection")
                        continue
                    active_root = rows[n - 1].root
                    print(f"Active instance set: {active_root}")
                    continue
                if c2 in {"0", "q", "quit", "exit"}:
                    break
                print("Unknown option")
            continue
        if choice == "4":
            if not active_root:
                print("Select active instance first")
                continue
            try:
                _run_cache_menu(cfg, active_root)
            except Exception as e:
                print(f"Cache error: {e}")
            continue
        if choice == "5":
            picked = _pick_active_instance_interactive(cfg, active_root)
            if picked:
                active_root = picked
            continue
        if choice == "6":
            while True:
                cfg_eff, st_backend = _state_backend_config_and_status(cfg)
                show_create_state_db = _state_db_missing_only(cfg_eff, st_backend)
                ipv6_disabled = _ipv6_disabled_now()
                ipv6_toggle_to_disabled = not bool(ipv6_disabled)
                ipv6_toggle_label = "Disable IPv6" if ipv6_toggle_to_disabled else "Enable IPv6"
                ipv6_state_label = "disabled" if ipv6_disabled is True else ("enabled" if ipv6_disabled is False else "unknown")
                print("")
                print("Environment")
                print(f"IPv6 state: {ipv6_state_label}")
                print(f"1. {ipv6_toggle_label}")
                print("2. State Backend Status")
                if show_create_state_db:
                    print("3. Bootstrap State DB (root password)")
                print("0. Back")
                c3 = _ask("Select option: ").strip()
                if c3 == "1":
                    for line in set_ipv6_disabled(ipv6_toggle_to_disabled):
                        print(line)
                    _push_state_after_change(
                        cfg,
                        "env-ipv6-disable-interactive" if ipv6_toggle_to_disabled else "env-ipv6-enable-interactive",
                    )
                    continue
                if c3 == "2":
                    _print_state_backend_status(cfg)
                    continue
                if c3 == "3" and show_create_state_db:
                    host_default = str(cfg_eff.state_mysql_host or "localhost")
                    port_default = int(cfg_eff.state_mysql_port or 3306)
                    admin_host = (_ask(f"DB admin host [{host_default}]: ").strip() or host_default)
                    admin_port_raw = _ask(f"DB admin port [{port_default}]: ").strip()
                    try:
                        admin_port = int(admin_port_raw) if admin_port_raw else port_default
                    except ValueError:
                        print("Invalid port")
                        continue
                    admin_user = (_ask("DB admin user [root]: ").strip() or "root")
                    sock_default = str(cfg_eff.state_mysql_unix_socket or "").strip()
                    sock_prompt = f"DB admin unix socket [{sock_default or 'auto'}]: "
                    admin_sock = _ask(sock_prompt).strip() or sock_default or None
                    while True:
                        admin_pwd = getpass.getpass("DB admin password (empty allowed): ")
                        ok, msg, cfg_after = _bootstrap_state_db_with_admin(
                            cfg_eff,
                            admin_user=admin_user,
                            admin_password=admin_pwd if admin_pwd != "" else None,
                            admin_host=admin_host,
                            admin_port=admin_port,
                            admin_socket=admin_sock,
                        )
                        if ok:
                            print(msg)
                            cfg = cfg_after
                            _push_state_after_change(cfg, "state-db-init")
                            break
                        print(f"Bootstrap failed: {msg}")
                        if _is_mysql_auth_error(msg):
                            print("DB auth failed: wrong admin password/user or socket/auth mismatch.")
                        retry = (_ask("Retry DB admin password? [Y/n]: ").strip() or "y").lower()
                        if retry not in {"y", "yes", "1", "true"}:
                            break
                    continue
                if c3 in {"0", "q", "quit", "exit"}:
                    break
                print("Unknown option")
            continue
        if choice == "7":
            if not active_root:
                print("Select active instance first")
                continue
            while True:
                print("")
                print(f"Backup (root={active_root})")
                print("1. Run now")
                print("2. Status")
                print("3. Prune")
                print("0. Back")
                c3 = _ask("Select option: ").strip()
                if c3 == "1":
                    try:
                        res = backup_run(cfg, active_root)
                        print(res.message)
                        if res.state_path:
                            print(f"state={res.state_path}")
                        _push_state_after_change(cfg, "backup-run-interactive")
                    except Exception as e:
                        print(f"Backup run error: {e}")
                    continue
                if c3 == "2":
                    try:
                        st = backup_status(cfg, active_root)
                        print(json.dumps(st, ensure_ascii=True, indent=2))
                    except Exception as e:
                        print(f"Backup status error: {e}")
                    continue
                if c3 == "3":
                    try:
                        res = backup_prune(cfg, active_root)
                        print(res.message)
                        _push_state_after_change(cfg, "backup-prune-interactive")
                    except Exception as e:
                        print(f"Backup prune error: {e}")
                    continue
                if c3 in {"0", "q", "quit", "exit"}:
                    break
                print("Unknown option")
            continue
        if choice == "8":
            if not active_root:
                print("Select active instance first")
                continue
            _run_mautic6_patch_menu(cfg, active_root)
            continue
        if choice == "9":
            _run_custom_menu(cfg)
            continue
        if choice in {"0", "q", "quit", "exit"}:
            return 0
        print("Unknown option")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCD Agent", prog="mcd-cli")
    parser.add_argument("--version", action="version", version=f"%(prog)s {installed_agent_version()}")
    parser.add_argument("--log-level", default="INFO")

    sub = parser.add_subparsers(dest="cmd", required=False)
    default_cfg = _default_config_path()

    nginx_baseline = sub.add_parser("nginx-baseline", help="Manage only the MCD-owned nginx default stub")
    nginx_baseline.add_argument("component", choices=["default-stub"])
    nginx_baseline.add_argument("op", choices=["status", "apply"], nargs="?", default="status")
    nginx_baseline.add_argument("--json", action="store_true")

    health = sub.add_parser("health", help="Print agent health")
    health.add_argument("--json", action="store_true")

    discover = sub.add_parser("discover", help="Discover local Mautic installs")
    discover.add_argument("--config", default=default_cfg)

    run = sub.add_parser("run", help="Run daemon loop")
    run.add_argument("--config", default=default_cfg)

    run_once = sub.add_parser("run-once", help="Run one polling cycle (debug)")
    run_once.add_argument("--config", default=default_cfg)

    exec_cmd = sub.add_parser("exec", help="Execute one Mautic command")
    exec_cmd.add_argument("--config", default=default_cfg)
    exec_cmd.add_argument("--root", required=True)
    exec_cmd.add_argument(
        "--command",
        required=True,
        choices=[
            "campaign:trigger",
            "segments:update",
            "campaign:rebuild",
            "campaigns:rebuild",
            "campaigns:update",
            "campaigns:trigger",
            "import",
            "cache:clear",
            "cache:warmup",
        ],
    )
    exec_cmd.add_argument("--instance-id", type=int)
    exec_cmd.add_argument("--php-bin", default="/usr/bin/php")
    exec_cmd.add_argument("--timeout", type=int, default=1800)
    exec_cmd.add_argument("--run-as-user", default="www-data")

    def _add_shorthand_exec_args(p: argparse.ArgumentParser, *, with_instance_id: bool) -> None:
        p.add_argument("--config", default=default_cfg)
        p.add_argument("--root", help="Mautic root or instance uid (optional when one local instance exists)")
        if with_instance_id:
            p.add_argument("-i", "--instance-id", type=int, dest="instance_id")
        p.add_argument("--php-bin", default="/usr/bin/php")
        p.add_argument("--timeout", type=int, default=1800)
        p.add_argument("--run-as-user", default="www-data")

    sh_seg = sub.add_parser("segments:update", help="Shorthand for exec --command segments:update")
    _add_shorthand_exec_args(sh_seg, with_instance_id=True)

    sh_ct = sub.add_parser("campaign:trigger", help="Shorthand for exec --command campaign:trigger")
    _add_shorthand_exec_args(sh_ct, with_instance_id=True)

    sh_cr = sub.add_parser("campaign:rebuild", help="Shorthand for exec --command campaign:rebuild")
    _add_shorthand_exec_args(sh_cr, with_instance_id=True)

    sh_crs = sub.add_parser("campaigns:rebuild", help="Shorthand for exec --command campaigns:rebuild")
    _add_shorthand_exec_args(sh_crs, with_instance_id=True)

    sh_cu = sub.add_parser("campaigns:update", help="Shorthand for exec --command campaigns:update")
    _add_shorthand_exec_args(sh_cu, with_instance_id=True)

    sh_ctr = sub.add_parser("campaigns:trigger", help="Shorthand for exec --command campaigns:trigger")
    _add_shorthand_exec_args(sh_ctr, with_instance_id=True)

    sh_imp = sub.add_parser("import", help="Shorthand for exec --command import")
    _add_shorthand_exec_args(sh_imp, with_instance_id=True)

    sh_cc = sub.add_parser("cache:clear", help="Shorthand for exec --command cache:clear")
    _add_shorthand_exec_args(sh_cc, with_instance_id=False)

    sh_cw = sub.add_parser("cache:warmup", help="Shorthand for exec --command cache:warmup")
    _add_shorthand_exec_args(sh_cw, with_instance_id=False)

    sh_ch = sub.add_parser("cache:hard", help="Hard clear cache directory (delete/recreate var/cache/prod)")
    _add_shorthand_exec_args(sh_ch, with_instance_id=False)
    sh_ch.add_argument("--local", action="store_true", help=argparse.SUPPRESS)

    pfix = sub.add_parser("permissions:fix", help="Repair instance filesystem permissions for Mautic runtime")
    pfix.add_argument("--config", default=default_cfg)
    pfix.add_argument("--root", help="Mautic root or instance uid (optional when one local instance exists)")
    pfix.add_argument("--run-as-user", help="Target runtime user (defaults to config mautic_run_as_user)")

    inst_delete = sub.add_parser("instance-delete", help="Delete selected Mautic instance artifacts")
    inst_delete.add_argument("--config", default=default_cfg)
    inst_delete.add_argument("--root", help="Mautic root or instance uid")
    inst_delete.add_argument("--domain", action="append", help="Expected instance domain (repeatable)")
    inst_delete.add_argument("--db-name", help="Expected database name (defaults to local.php db_name)")
    inst_delete.add_argument("--delete-files", action="store_true", help="Remove the application root directory")
    inst_delete.add_argument("--delete-vhost", action="store_true", help="Remove matching nginx vhost files/symlinks")
    inst_delete.add_argument("--delete-db", action="store_true", help="Drop the local database from local.php/--db-name")
    inst_delete.add_argument("--yes", action="store_true", help="Confirm destructive deletion")
    inst_delete.add_argument("--dry-run", action="store_true", help="Plan only; do not delete")
    inst_delete.add_argument("--json", action="store_true")

    reset_admin = sub.add_parser("admin:reset-password", help="Reset/create admin Mautic user for one instance")
    reset_admin.add_argument("--config", default=default_cfg)
    reset_admin.add_argument("--root", help="Mautic root or instance uid (optional when one local instance exists)")
    reset_admin.add_argument("--username", required=True)
    reset_admin.add_argument("--email", required=True)
    reset_admin.add_argument("--first-name", required=True, dest="first_name")
    reset_admin.add_argument("--last-name", required=True, dest="last_name")
    reset_admin.add_argument("--password-hash", required=True, dest="password_hash")
    reset_admin.add_argument("--json", action="store_true")

    mfa_status = sub.add_parser("admin:mfa-status", help="Inspect HostnetAuth MFA state for one Mautic user")
    mfa_status.add_argument("--config", default=default_cfg)
    mfa_status.add_argument("--root", help="Mautic root or instance uid (optional when one local instance exists)")
    mfa_status.add_argument("--username", required=True)
    mfa_status.add_argument("--email", required=True)
    mfa_status.add_argument("--json", action="store_true")

    mfa_clear = sub.add_parser("admin:mfa-clear", help="Clear HostnetAuth MFA state for one Mautic user")
    mfa_clear.add_argument("--config", default=default_cfg)
    mfa_clear.add_argument("--root", help="Mautic root or instance uid (optional when one local instance exists)")
    mfa_clear.add_argument("--username", required=True)
    mfa_clear.add_argument("--email", required=True)
    mfa_clear.add_argument("--json", action="store_true")

    lock_cleanup = sub.add_parser("mautic-locks:cleanup", help="Clear stale Mautic checked_out locks")
    lock_cleanup.add_argument("--config", default=default_cfg)
    lock_cleanup.add_argument("--root", help="Mautic root or instance uid (optional: all local instances)")
    lock_cleanup.add_argument("--min-age-sec", type=int, default=21600)
    lock_cleanup.add_argument("--max-rows", type=int, default=20)
    lock_cleanup.add_argument("--json", action="store_true")

    logical_issues = sub.add_parser("logical-issues", help="Inspect and remediate instance logical errors")
    logical_issues_sub = logical_issues.add_subparsers(dest="logical_issues_cmd", required=True)
    logical_scan = logical_issues_sub.add_parser("scan", help="Scan one instance and persist current logical errors")
    logical_scan.add_argument("--config", default=default_cfg)
    logical_scan.add_argument("--root", help="Mautic root or instance uid")
    logical_scan.add_argument("--json", action="store_true")
    logical_status = logical_issues_sub.add_parser("status", help="Read the persisted logical error snapshot")
    logical_status.add_argument("--config", default=default_cfg)
    logical_status.add_argument("--root", help="Mautic root or instance uid")
    logical_status.add_argument("--json", action="store_true")
    logical_remediate = logical_issues_sub.add_parser(
        "remediate", help="Apply a guarded remediation to one active logical error"
    )
    logical_remediate.add_argument("--config", default=default_cfg)
    logical_remediate.add_argument("--root", help="Mautic root or instance uid")
    logical_remediate.add_argument("--issue-id")
    logical_remediate.add_argument(
        "--segment-id",
        action="append",
        type=int,
        default=[],
        help="Disable only this affected segment ID; repeat to select more segments",
    )
    logical_remediate.add_argument(
        "--target",
        action="append",
        default=[],
        help="Batch target in ISSUE_ID=SEGMENT_ID[,SEGMENT_ID] form; repeat per issue",
    )
    logical_remediate.add_argument("--action", choices=["disable_segments"], required=True)
    logical_remediate.add_argument("--actor", default="Emmy Starwell")
    logical_remediate.add_argument("--json", action="store_true")

    email_counters = sub.add_parser(
        "email-counters",
        help="Audit/repair Mautic email sent_count drift after campaign trigger completion",
    )
    email_counters.add_argument("--config", default=default_cfg)
    email_counters.add_argument("--root", help="Mautic root, instance uid, name, or domain")
    email_counters.add_argument("--campaign-id", type=int, required=True)
    email_counters.add_argument("op", choices=["audit", "repair"], nargs="?", default="audit")
    email_counters.add_argument("--json", action="store_true")

    email_activity = sub.add_parser(
        "report:email-activity",
        help="Collect Mautic sent/read/click/unsubscribe activity report",
    )
    email_activity.add_argument("--config", default=default_cfg)
    email_activity.add_argument("--root", help="Mautic root, instance uid, name, or domain")
    email_activity.add_argument("--days", type=int, default=7)
    email_activity.add_argument("--summary", action="store_true", help="Include daily totals")
    email_activity.add_argument("--extended", action="store_true", help="Include per-email breakdown")
    email_activity.add_argument("--contact-mode", choices=["all", "fresh", "old"], default="all")
    email_activity.add_argument("--contact-age-days", type=int, default=30)
    email_activity.add_argument("--json", action="store_true")

    contact_count = sub.add_parser(
        "report:contact-count",
        help="Count Mautic contacts that have a non-empty email or mobile field",
    )
    contact_count.add_argument("--config", default=default_cfg)
    contact_count.add_argument("--root", help="Mautic root, instance uid, name, or domain")
    contact_count.add_argument("--json", action="store_true")

    contact_field_metadata = sub.add_parser(
        "report:contact-field-metadata",
        help="Collect read-only Mautic contact field definitions and storage metadata",
    )
    contact_field_metadata.add_argument("--config", default=default_cfg)
    contact_field_metadata.add_argument("--root", help="Mautic root, instance uid, name, or domain")
    contact_field_metadata.add_argument("--json", action="store_true")

    tune = sub.add_parser("tune-segments", help="Benchmark and tune segment parallelism")
    tune.add_argument("--config", default=default_cfg)
    tune.add_argument("--root")
    tune.add_argument("--max-parallel", type=int, default=4)
    tune.add_argument("--apply", action="store_true")
    tune.add_argument("--apply-priority", action="store_true")

    def _add_plugins_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=default_cfg)
        p.add_argument("--root")
        p.add_argument(
        "--action",
        choices=["auto", "install", "update", "reinstall", "remove", "purge"],
        help="Action mode for selected plugins",
        )
        p.add_argument("--select", help="Selection expression, e.g. '1-3 6 10'")
        p.add_argument("--bundle", action="append", help="Select plugin by bundle name (repeatable)")
        p.add_argument("--plugin-uid", action="append", help="Select plugin by stable manifest plugin_uid (repeatable)")
        p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
        p.add_argument("--no-color", action="store_true")
        p.add_argument("--list-available", action="store_true", help="Show plugins available in server manifest")
        p.add_argument("--list-installed", action="store_true", help="Show plugins currently installed on host")
        p.add_argument("--catalog-json", action="store_true", help="Print plugin catalog/status as JSON")
        p.add_argument("--cluster-sync-check-key", help=argparse.SUPPRESS)
        p.add_argument("--cluster-sync-check-digest", help=argparse.SUPPRESS)

    plugins = sub.add_parser("plugins", help="Interactive plugin sync/install from MCC repo")
    _add_plugins_args(plugins)
    plugin_alias = sub.add_parser("plugin", help=argparse.SUPPRESS)
    _add_plugins_args(plugin_alias)

    plugin_ops = sub.add_parser("plugin-operations", aliases=["plugin-operation"], help="Use declared plugin operations")
    plugin_ops_sub = plugin_ops.add_subparsers(dest="plugin_ops_cmd", required=True)
    for name, help_text in (("list", "List declared operations"), ("complete", "Complete declared operation keys and fields"), ("set", "Set declared operation values"), ("run", "Run a declared action operation")):
        op_parser = plugin_ops_sub.add_parser(name, help=help_text)
        op_parser.add_argument("--config", default=default_cfg)
        op_parser.add_argument("--root", help="Mautic root, instance uid, name, or domain")
        op_parser.add_argument("--operation", dest="operation_key")
        op_parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
        op_parser.add_argument("--prefix", default="")
        op_parser.add_argument("--json", action="store_true")

    local_context = sub.add_parser("mautic-local-context", help="Read-only local identity and Composer layout")
    local_context.add_argument("--config", default=default_cfg)
    local_context.add_argument("--root", required=True)
    local_context.add_argument("--json", action="store_true")

    target_verify = sub.add_parser("mautic-target-patch-verify", help="Read-only admitted staged-target signatures")
    target_verify.add_argument("--config", default=default_cfg)
    target_verify.add_argument("--root", required=True)
    target_verify.add_argument("--prepared-target-id", required=True)
    for prefix in ("verification-plan", "authorization-context", "patch-plan"):
        target_verify.add_argument("--" + prefix + "-file", required=True)
        target_verify.add_argument("--" + prefix + "-sha256", required=True)
    target_verify.add_argument("--catalog-file", required=True)
    target_verify.add_argument("--catalog-sha256", required=True)
    target_verify.add_argument("--json", action="store_true")

    verification_capability = sub.add_parser("mautic-target-patch-verification-capability", help="Read-only collector capability")
    verification_capability.add_argument("--json", action="store_true")

    image_compatibility = sub.add_parser("image-compatibility", help="Read-only fresh host compatibility probe for MCC images")
    image_compatibility.add_argument("--request-json", required=True, help="Strict request JSON file, or - for stdin")
    image_compatibility.add_argument("--json", action="store_true")
    image_compatibility_capability = sub.add_parser(
        "image-compatibility-capability",
        help="Advertise the read-only MCC image compatibility contract",
    )
    image_compatibility_capability.add_argument("--json", action="store_true")

    up = sub.add_parser("mautic-upgrade", help="Check/apply Mautic version upgrade")
    up.add_argument("--config", default=default_cfg)
    up.add_argument("--root")
    up.add_argument("op", choices=["check", "preflight", "composer-prepare", "authorize-repair", "apply", "interactive"], nargs="?", default="interactive")
    up.add_argument("--mode", choices=["auto", "zip", "composer"], default="auto")
    up.add_argument("--yes", action="store_true")
    up.add_argument("--backup", action="store_true")
    up.add_argument("--with-system-upgrade", action="store_true")
    up.add_argument("--target", help="Explicit target Mautic version")
    up.add_argument("--allow-minor", action="store_true", help="Allow a forward minor upgrade within the current major")
    up.add_argument("--allow-major", action="store_true", help="Allow the guarded Composer Mautic 6 to 7 upgrade flow")
    up.add_argument("--allow-release-transition", action="store_true", help="Request an explicitly MCC-authorized release-line transition")
    up.add_argument("--mcc-release-authorization-context-file", default="")
    up.add_argument("--mcc-release-authorization-context-sha256", default="")
    up.add_argument("--patch-plan-json", default="", help="Revision-pinned MCC Mautic patch plan for an atomic patch stage")
    up.add_argument("--patch-plan-file", default="", help="Private host file containing a large immutable patch plan")
    up.add_argument("--patch-plan-sha256", default="", help="Expected canonical immutable plan SHA-256")
    up.add_argument("--patch-run-id", default="", help="Safe idempotency key for the atomic MCC patch-plan run")
    up.add_argument("--repair-plan-json", default="", help="Strict versioned JSON-schema repair plan")
    up.add_argument("--repair-auth-context-file", default="", help="Root-owned signed MCC authorization context file for JSON repair")
    up.add_argument("--patch-backup-context-file", default="", help="Signed host-local backup attestation bound to the immutable v3 patch plan")
    up.add_argument("--prepared-target-json", default="", help="MCC prepared-target manifest input for artifact-only apply")
    up.add_argument("--prepared-target-file", default="", help="Private file containing the MCC prepared-target manifest input")
    up.add_argument("--prepared-target-sha256", default="", help="Expected SHA-256 of --prepared-target-file")
    up.add_argument("--repair-auth-key-file", default="/etc/mcd/mcc-operation-signing.key", help="Root-owned MCC operation signing key file")
    up.add_argument("--backup-manifest-path", default="", help="MCD backup marker path for authorize-repair")
    up.add_argument("--repair-auth-output-file", default="", help="Optional root-owned output path for authorize-repair context")
    up.add_argument("--json", action="store_true", help="Emit the machine-readable preflight contract")
    up.add_argument("--mcc-preflighted-single-instance", action="store_true", help="Root-only, explicitly acknowledged single-instance manual upgrade after MCC preflight; permits forward same-major targets while preserving selected patch-plan validation")

    img = sub.add_parser("mautic-image", help="Install a Mautic instance from an MCC image")
    img.add_argument("--config", default=default_cfg)
    img.add_argument("op", choices=["install"], nargs="?", default="install")
    img.add_argument("--image-ref", required=True)
    img.add_argument("--domain", required=True)
    img.add_argument("--php-version", required=True)
    img.add_argument("--certbot-dns-credential-ref", default="")
    img.add_argument("--yes", action="store_true")
    img.add_argument("--no-certbot", action="store_true")
    img.add_argument("--own-mail", action="store_true")
    img.add_argument("--mail-profile-id", default="")
    img.add_argument("--json", action="store_true")

    local_mail = sub.add_parser("local-mail", help="Manage per-instance own-host mail")
    local_mail_sub = local_mail.add_subparsers(dest="op", required=True)
    local_mail_configure = local_mail_sub.add_parser("configure")
    local_mail_configure.add_argument("--config", default=default_cfg)
    local_mail_configure.add_argument("--instance-domain", required=True)
    local_mail_configure.add_argument("--root", required=True)
    local_mail_configure.add_argument("--json", action="store_true")
    local_mail_disable = local_mail_sub.add_parser("disable")
    local_mail_disable.add_argument("--config", default=default_cfg)
    local_mail_disable.add_argument("--instance-domain", required=True)
    local_mail_disable.add_argument("--json", action="store_true")
    local_mail_status = local_mail_sub.add_parser("status")
    local_mail_status.add_argument("--config", default=default_cfg)
    local_mail_status.add_argument("--instance-domain", required=True)
    local_mail_status.add_argument("--push", action="store_true")
    local_mail_status.add_argument("--json", action="store_true")
    local_mail_submit = local_mail_sub.add_parser("submit")
    local_mail_submit.add_argument("--config", default=default_cfg)
    local_mail_submit.add_argument("--instance-domain", required=True)
    local_mail_submit.add_argument("sendmail_args", nargs=argparse.REMAINDER)
    local_mail_receive = local_mail_sub.add_parser("receive")
    local_mail_receive.add_argument("--config", default=default_cfg)
    local_mail_receive.add_argument("--instance-domain", required=True)
    local_mail_receive.add_argument("--kind", choices=["bounce", "feedback_loop"], required=True)

    mail_config = sub.add_parser("mail-config", help="Apply an MCC-stored instance mail profile")
    mail_config_sub = mail_config.add_subparsers(dest="mail_config_op", required=True)
    mail_config_apply = mail_config_sub.add_parser("apply")
    mail_config_apply.add_argument("--config", default=default_cfg)
    mail_config_apply.add_argument("--profile-id", required=True)
    mail_config_apply.add_argument("--instance-domain", required=True)
    mail_config_apply.add_argument("--root", required=True)
    mail_config_apply.add_argument("--json", action="store_true")
    mail_config_preflight = mail_config_sub.add_parser("preflight")
    mail_config_preflight.add_argument("--config", default=default_cfg)
    mail_config_preflight.add_argument("--profile-id", required=True)
    mail_config_preflight.add_argument("--instance-domain", required=True)
    mail_config_preflight.add_argument("--root", required=True)
    mail_config_preflight.add_argument("--json", action="store_true")

    cmove = sub.add_parser("composer-move", help="Move a zip Mautic instance to a Composer skeleton")
    cmove.add_argument("--config", default=default_cfg)
    cmove.add_argument("--root", required=True)
    cmove.add_argument("--domain", required=True)
    cmove.add_argument("--mautic-major", type=int, required=True)
    cmove.add_argument("--yes", action="store_true")
    cmove.add_argument("--json", action="store_true")

    hub = sub.add_parser("interactive", help="Unified interactive menu")
    hub.add_argument("--config", default=default_cfg)
    hub.add_argument("--root")
    hub.add_argument("--no-color", action="store_true")

    inv = sub.add_parser("instances", help="Manage Mautic instance inventory")
    inv.add_argument("--config", default=default_cfg)
    inv.add_argument("op", choices=["list", "rescan", "add", "remove"], nargs="?", default="list")
    inv.add_argument("--name")
    inv.add_argument("--root")
    inv.add_argument("--console-path")
    inv.add_argument("--local-php-path")
    inv.add_argument("--mautic-major", type=int)
    inv.add_argument("--db-host")
    inv.add_argument("--db-port", type=int)
    inv.add_argument("--db-name")
    inv.add_argument("--db-user")
    inv.add_argument("--db-password")
    inv.add_argument("--db-table-prefix")

    reload_cfg = sub.add_parser("reload-config", help="Rescan instances from config/discovery")
    reload_cfg.add_argument("--config", default=default_cfg)

    env = sub.add_parser("env", help="Host environment operations")
    env.add_argument("--config", default=default_cfg)
    env.add_argument("target", choices=["ipv6", "policy"])
    env.add_argument("op", choices=["status", "disable", "enable", "show", "plan"])
    env.add_argument("--policy-file")
    env.add_argument("--policy-json")
    env.add_argument("--policy-b64")
    env.add_argument("--component", choices=["all", "apt", "iptables", "database", "php", "web", "web_cf_real_ip"], default="all")
    env.add_argument("--json", action="store_true")

    svc = sub.add_parser("service-profile", help="Fetch/apply MCC-managed service profiles (hardware-aware)")
    svc.add_argument("--config", default=default_cfg)
    svc.add_argument("op", choices=["fetch", "apply", "status", "rescan"])
    svc.add_argument(
        "--component",
        choices=[
            "php_fpm",
            "php-fpm",
            "mysql",
            "apt",
            "wazuh",
            "docker",
            "mautic_db_indexes",
            "mautic-db-indexes",
            "db_indexes",
            "db-indexes",
        ],
        default="php_fpm",
    )
    svc.add_argument("--dry-run", action="store_true")
    svc.add_argument(
        "--allow-cluster-db-maintenance",
        action="store_true",
        help=(
            "Allow cluster DB-heavy profile apply for explicit maintenance windows. "
            "Without this flag, mysql and mautic_db_indexes are skipped on Galera/PXC nodes."
        ),
    )
    svc.add_argument("--json", action="store_true")

    zbx = sub.add_parser("zabbix", help="Zabbix helper operations")
    zbx.add_argument("--config", default=default_cfg)
    zbx.add_argument(
        "op",
        choices=[
            "status",
            "bootstrap-mysql-user",
        ],
        nargs="?",
        default="status",
    )
    zbx.add_argument("--force", action="store_true", help="Force rerun even if one-time marker exists")
    zbx.add_argument("--no-restart", action="store_true", help="Do not restart zabbix-agent2 after config change")
    zbx.add_argument("--json", action="store_true")

    shortner = sub.add_parser("shortner", aliases=["shortener"], help="Detect and manage local YOURLS installs")
    shortner.add_argument("--config", default=default_cfg)
    shortner.add_argument("op", choices=["detect", "version", "check-update", "update"], nargs="?", default="detect")
    shortner.add_argument("--root", help="YOURLS root path")
    shortner.add_argument("--target-version", help="Target YOURLS version for update")
    shortner.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    shortner.add_argument("--json", action="store_true")

    ro = sub.add_parser("runtime-overrides", help="Runtime overrides sync with MCC")
    ro.add_argument("--config", default=default_cfg)
    ro.add_argument("op", choices=["show", "fetch", "push", "trigger", "status"], nargs="?", default="show")
    ro.add_argument("--merge", action="store_true", help="merge on push (default: replace)")
    ro.add_argument("--target", choices=["observed", "desired"], default="observed", help="push target for runtime map")
    ro.add_argument("--json", action="store_true")

    commercial = sub.add_parser("commercial", help="Portal commercial transport (explicit activation only)")
    commercial.add_argument("--config", default=default_cfg)
    commercial.add_argument("op", choices=["status", "activate", "register", "register-host"])
    commercial.add_argument("--portal-url", default="")
    commercial.add_argument("--activation-code", default="")
    commercial.add_argument("--local-identity", default="")
    commercial.add_argument("--display-name", default="")
    commercial.add_argument("--mautic-version", default="")
    commercial.add_argument("--application-root-fingerprint", default="")
    commercial.add_argument("--activation-state", choices=["passive", "active"], default="passive")
    commercial.add_argument("--json", action="store_true")

    scheduler = sub.add_parser("scheduler", help="Pause/resume scheduler launches")
    scheduler.add_argument("--config", default=default_cfg)
    scheduler.add_argument("op", choices=["pause", "resume", "status"])
    scheduler.add_argument("--verbose", action="store_true", help="Show tracked running task details on status")
    scheduler.add_argument("--json", action="store_true")

    apt_upg = sub.add_parser("apt-upgrade", help="Run apt update + dist-upgrade preserving local config files")
    apt_upg.add_argument("--config", default=default_cfg)
    apt_upg.add_argument("--yes", action="store_true", help="Do not ask confirmation")
    apt_upg.add_argument("--json", action="store_true")

    sdb = sub.add_parser("state-db", help="State DB status/bootstrap for legacy->mysql_hybrid migration")
    sdb.add_argument("--config", default=default_cfg)
    sdb.add_argument("op", choices=["status", "init"], nargs="?", default="status")
    sdb.add_argument("--admin-host")
    sdb.add_argument("--admin-port", type=int)
    sdb.add_argument("--admin-user", default="root")
    sdb.add_argument("--admin-unix-socket")
    sdb.add_argument("--admin-password-stdin", action="store_true")
    sdb.add_argument("--json", action="store_true")

    lifecycle = sub.add_parser("tasks-lifecycle", help="Safely archive terminal task receipts and reconcile admission metadata")
    lifecycle.add_argument("--config", default=default_cfg)
    lifecycle.add_argument("op", choices=["dry-run", "apply", "purge-failed"], nargs="?", default="dry-run")
    lifecycle.add_argument("--min-age-sec", type=int)
    lifecycle.add_argument("--archive-dir")
    lifecycle.add_argument("--admission-dir", default="/run/lock/mcd-tasks")
    lifecycle.add_argument("--yes", action="store_true", help="Required for apply")
    lifecycle.add_argument("--json", action="store_true")

    maintenance = sub.add_parser("maintenance", help="Temporary maintenance mode (no profile switch)")
    maintenance.add_argument("--config", default=default_cfg)
    maintenance.add_argument("op", choices=["on", "off", "status"])
    maintenance.add_argument(
        "--no-kill-running",
        action="store_true",
        help="Only pause scheduler, do not stop currently running Mautic console tasks",
    )
    maintenance.add_argument(
        "--kill-orphans",
        action="store_true",
        help="Also stop orphan Mautic console tasks not tracked in MCD task DB",
    )
    maintenance.add_argument(
        "--grace-sec",
        type=int,
        default=10,
        help="SIGTERM grace period before SIGKILL for stopped processes",
    )
    maintenance.add_argument(
        "--stop-cron",
        action="store_true",
        help="Also stop cron service while maintenance mode is enabled (restored on maintenance off)",
    )
    maintenance.add_argument("--json", action="store_true")

    profile = sub.add_parser("profile", help="Switch MCD profile (single source of state)")
    profile.add_argument("--config", default=default_cfg)
    profile.add_argument(
        "op",
        choices=[
            "status",
            "auto",
            "passive",
            "tiny",
            "mini",
            "midi",
            "maxi",
            "hiload",
            "ultra",
            "farm-tiny",
            "farm-mini",
            "farm-midi",
            "farm-maxi",
            "farm-hiload",
            "farm-ultra",
            "custom",
        ],
    )
    profile.add_argument("--yes", action="store_true")
    profile.add_argument("--json", action="store_true")

    tcheck = sub.add_parser("time-check", help="Timezone diagnostics for OS/PHP/MySQL/Mautic")
    tcheck.add_argument("--config", default=default_cfg)

    cfgchk = sub.add_parser("config-check", help="Validate config and optionally recover from MCC desired config")
    cfgchk.add_argument("--config", default=default_cfg)
    cfgchk.add_argument("--repair-from-mcc", action="store_true")
    cfgchk.add_argument("--json", action="store_true")

    signals = sub.add_parser("signals", help="Collect lightweight critical host signals")
    signals.add_argument("--config", default=default_cfg)
    signals.add_argument("--window-min", type=int, default=15)
    signals.add_argument("--json", action="store_true")

    inst_migrate = sub.add_parser("instance-migrate", help="Instance migration preflight and relay helpers")
    inst_migrate.add_argument("--config", default=default_cfg)
    inst_migrate.add_argument(
        "op",
        choices=[
            "source-probe",
            "target-pull",
            "source-stream-files",
            "source-stream-db",
            "source-stream-letsencrypt",
            "target-preflight",
            "target-receive-files",
            "target-receive-letsencrypt",
            "target-import-db",
            "target-finalize",
        ],
        nargs="?",
        default="source-probe",
    )
    inst_migrate.add_argument("--root", help="Instance root/name/domain selector")
    inst_migrate.add_argument("--source-address")
    inst_migrate.add_argument("--source-ssh-user", default="root")
    inst_migrate.add_argument("--source-ssh-port", type=int, default=22)
    inst_migrate.add_argument("--source-ssh-key-file")
    inst_migrate.add_argument("--source-root")
    inst_migrate.add_argument("--target-root")
    inst_migrate.add_argument("--target-db-name")
    inst_migrate.add_argument("--target-db-user")
    inst_migrate.add_argument("--target-db-password")
    inst_migrate.add_argument("--domains-json", default="[]")
    inst_migrate.add_argument("--php-version")
    inst_migrate.add_argument("--runtime-adapter")
    inst_migrate.add_argument("--runtime")
    inst_migrate.add_argument("--install-type")
    inst_migrate.add_argument("--image-ref")
    inst_migrate.add_argument("--wipe-target", action="store_true")
    inst_migrate.add_argument("--wipe-target-db", action="store_true")
    inst_migrate.add_argument("--json", action="store_true")

    inst_runtime = sub.add_parser("instance-runtime", help="Normalize instance PHP runtime")
    inst_runtime.add_argument("--config", default=default_cfg)
    inst_runtime.add_argument("op", choices=["status", "apply"], nargs="?", default="status")
    inst_runtime.add_argument("--root", help="Instance root, uid, name, or primary domain")
    inst_runtime.add_argument("--dry-run", action="store_true", help="Plan only; do not write files")
    inst_runtime.add_argument("--no-reload", action="store_true", help="Do not reload php-fpm/nginx after a successful apply")
    inst_runtime.add_argument("--json", action="store_true")

    upd = sub.add_parser("self-update", help="MCD self-update via MCC approved/test/lts/cluster channels")
    upd.add_argument("--config", default=default_cfg)
    upd.add_argument("op", choices=["check", "apply", "status"], nargs="?", default="status")
    upd.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    upd.add_argument("--json", action="store_true")

    m6p = sub.add_parser("mautic6-patch", help="Manage Mautic 6 core PluginUpdateEvent metadata patch")
    m6p.add_argument("--config", default=default_cfg)
    m6p.add_argument("--root", help="Instance root or instance uid (default: all)")
    m6p.add_argument("op", choices=["status", "apply", "revert", "policy"], nargs="?", default="status")
    m6p.add_argument("--policy", choices=["required", "off"], help="Policy value for op=policy")
    m6p.add_argument("--json", action="store_true")
    m6p.add_argument("--run-id", default="", help="Original catalog execution run for revert")

    patch_context = sub.add_parser("mautic-patch-context", help="Read selected-instance context without mutation")
    sub.add_parser("mautic-release-contract", help="Print the packaged release authorization wire contract")
    patch_context.add_argument("--config", default=default_cfg)
    patch_context.add_argument("--root", required=True)
    patch_context.add_argument("--instance-uid", required=True)
    patch_context.add_argument("--json", action="store_true")

    patch_plan = sub.add_parser("mautic-patch-plan", help="Verify/apply a revision-pinned MCC Mautic patch plan")
    patch_plan.add_argument("op", choices=["contract", "status", "verify", "apply", "rollback"], nargs="?", default="contract")
    patch_plan.add_argument("--root")
    patch_plan.add_argument("--plan-json", default="")
    patch_plan.add_argument("--plan-file", default="")
    patch_plan.add_argument("--plan-sha256", default="")
    patch_plan.add_argument("--phase", default="")
    patch_plan.add_argument("--run-id", default="")
    patch_plan.add_argument("--json", action="store_true")
    patch_plan.add_argument("--config", default=default_cfg)
    patch_backup = sub.add_parser("mautic-patch-backup-authorize", help="Bind a verified backup to an immutable upgrade patch plan")
    patch_backup.add_argument("--root", required=True)
    patch_backup.add_argument("--instance-uid", required=True)
    patch_backup.add_argument("--plan-json", default="")
    patch_backup.add_argument("--plan-file", default="")
    patch_backup.add_argument("--plan-sha256", default="")
    patch_backup.add_argument("--backup-evidence-json", required=True)
    patch_backup.add_argument("--output-file", required=True)
    patch_backup.add_argument("--signing-key-file", default="/etc/mcd/mcc-operation-signing.key")

    m713p = sub.add_parser("mautic713-import-tag-patch", help="Manage the reversible Mautic 7.0-7.2 import tag remediation")
    m713p.add_argument("--config", default=default_cfg)
    m713p.add_argument("--root", help="Instance root or instance uid (default: all)")
    m713p.add_argument("op", choices=["status", "apply", "revert"], nargs="?", default="status")
    m713p.add_argument("--json", action="store_true")
    m713p.add_argument("--run-id", default="", help="Original catalog execution run for revert")

    assets = sub.add_parser("cluster-assets", help="Verify/sanitize cluster-shared Mautic plugins and app/bundles")
    assets.add_argument("--config", default=default_cfg)
    assets.add_argument("op", choices=["status", "guard", "fix-perms", "reload"], nargs="?", default="status")
    assets.add_argument("--root", help="Instance root, uid, name, or primary domain (default: all local instances)")
    assets.add_argument("--json", action="store_true")
    assets.add_argument("--no-fix-perms", action="store_true", help="For guard: do not repair owner/mode drift")
    assets.add_argument("--reload-on-change", action="store_true", help="For guard: force cache/opcache/FPM reload on content change")
    assets.add_argument("--no-cache-clear", action="store_true", help="For reload: skip Mautic cache:clear")
    assets.add_argument("--no-cache-warm", action="store_true", help="For reload: skip Mautic cache:warmup")
    assets.add_argument("--no-fpm-reload", action="store_true", help="For reload: skip PHP-FPM reload/restart")

    uninst = sub.add_parser("uninstall", help="Remove MCD and restore pre-install crontab")
    uninst.add_argument("--service-name", default="mcd")
    uninst.add_argument("--install-dir", default="/opt/mcd")
    uninst.add_argument("--etc-dir", default="/etc/mcd")
    uninst.add_argument("--no-purge", action="store_true", help="Keep /opt/mcd and /etc/mcd")
    uninst.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    bkp = sub.add_parser("backup", help="Backup via SFTP/SSHFS or pre-mounted local storage")
    bkp.add_argument("--config", default=default_cfg)
    bkp.add_argument("--root", help="Optional instance root selector (accepted for MCC compatibility; backup scope is host-level)")
    bkp.add_argument(
        "--no-prune",
        action="store_true",
        help="For a one-shot run, preserve all existing completed and incomplete backup generations",
    )
    bkp.add_argument(
        "op",
        choices=[
            "run",
            "preflight",
            "dry-run",
            "status",
            "history",
            "instance-run",
            "prune",
            "restore",
            "profile-show",
            "profile-set",
            "cluster-status",
            "cluster-local-full",
            "cluster-incremental",
            "cluster-files",
            "cluster-files-produce",
            "cluster-files-assemble",
            "cluster-offsite",
            "cluster-offsite-dry-run",
            "cluster-retention-plan",
        ],
        nargs="?",
        default="status",
    )
    bkp.add_argument("--date", help="Restore from backup date folder YYYY-MM-DD")
    bkp.add_argument("--path", help="Restore from explicit backup path")
    bkp.add_argument("--remote-root-dir", help="One-shot remote root override for instance-run")
    bkp.add_argument("--profile-json-file", help="JSON file with backup profile payload for profile-set")
    bkp.add_argument("--profile-json-stdin", action="store_true", help="Read backup profile JSON payload from stdin")
    bkp.add_argument("--contract", default="", help="Versioned profile contract (profile-set only)")
    bkp.add_argument("--replace", action="store_true", help="Replace backup profile payload instead of merge")
    bkp.add_argument("--skip-prepare-check", action="store_true", help="Store backup profile without package/storage/DB verification")
    bkp.add_argument("--json", action="store_true")

    custom = sub.add_parser("custom", help="Run custom script from MCC manifest")
    custom.add_argument("--config", default=default_cfg)
    custom.add_argument("--list", action="store_true", help="List available custom scripts")
    custom.add_argument("--json", action="store_true")
    custom.add_argument("--expected-sha256", help="Require the selected manifest entry and downloaded script to match this SHA-256")
    custom.add_argument(
        "--detach",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Detached run control. Default: auto (interactive scripts run foreground).",
    )
    custom.add_argument("script", nargs="?", help="Script key (or display name) from custom manifest")
    custom.add_argument("script_args", nargs=argparse.REMAINDER, help="Arguments for selected script (after --)")

    return parser


def _normalize_help_tokens(argv: list[str]) -> list[str]:
    # Support Windows-style help tokens for operator convenience.
    out: list[str] = []
    i = 0
    while i < len(argv):
        cur = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if cur == "--action" and nxt in {"/?", "-?"}:
            out.append("--help")
            i += 2
            continue
        out.append("--help" if cur in {"/?", "-?"} else cur)
        i += 1
    return out


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args(_normalize_help_tokens(sys.argv[1:]))

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    if not args.cmd:
        cfg = load_config(_default_config_path())
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        return _run_interactive_hub(cfg, None, False)

    if args.cmd == "nginx-baseline":
        payload = default_stub_operation(apply=args.op == "apply")
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("status") in {"ok", "drift"} else 1

    if args.cmd == "health":
        versions = agent_version_payload()
        payload = {
            "service": "mcd-agent",
            "version": str(versions.get("agent_version") or __version__),
            "status": "ok",
            **versions,
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print("mcd-agent ok")
        return 0

    if args.cmd == "config-check":
        try:
            cfg = load_config(args.config, allow_recover_from_mcc=bool(args.repair_from_mcc))
            payload = {
                "status": "ok",
                "path": cfg.config_file_path,
                "schema_version": int(cfg.config_schema_version),
                "customized": bool(cfg.config_customized),
                "sha256": cfg.config_sha256,
                "profile": (cfg.profile_name or "").strip().lower(),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(
                    f"config ok path={payload['path']} schema={payload['schema_version']} "
                    f"profile={payload['profile']} customized={payload['customized']}"
                )
            return 0
        except Exception as e:
            payload = {"status": "error", "reason": str(e)}
            if args.json:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(f"config error: {e}")
            return 1

    if args.cmd == "commercial":
        cfg = load_config(args.config, allow_recover_from_mcc=False)
        state = CommercialState(cfg.state_db_path)
        if args.op == "status":
            payload = {
                "schema": "mcd-commercial-status-v1",
                "commercial_enabled": state.commercial_enabled,
                "agent_id": state.get("agent_id"),
                "contract_version": "1.0" if state.commercial_enabled else None,
                "portal_api_base": state.get("portal_api_base") if state.commercial_enabled else None,
                "mcc_configured": bool(cfg.mcc_url and cfg.mcc_token),
            }
        elif args.op == "register-host":
            required = (args.local_identity, args.display_name)
            if not all(required):
                parser.error("commercial register-host requires --local-identity and --display-name")
            payload = PortalClient(state).register_host(local_identity=args.local_identity, display_name=args.display_name)
        elif args.op == "activate":
            if not args.portal_url or not args.activation_code:
                parser.error("commercial activate requires --portal-url and --activation-code")
            payload = PortalClient(state).activate(args.portal_url, args.activation_code, __version__)
        else:
            required = (args.local_identity, args.display_name, args.mautic_version, args.application_root_fingerprint)
            if not all(required):
                parser.error("commercial register requires installation fields")
            payload = PortalClient(state).register_installation(local_identity=args.local_identity, display_name=args.display_name, mautic_version=args.mautic_version, application_root_fingerprint=args.application_root_fingerprint, activation_state=args.activation_state)
        print(json.dumps(payload, ensure_ascii=True, indent=2 if args.json else None))
        return 0

    if args.cmd == "discover":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        installs = discover_mautic(
            cfg.discovery_roots,
            cfg.exclude_path_contains,
            cfg.supported_mautic_majors,
            cfg.custom_instances,
        )
        print(json.dumps([inst.safe_dict() for inst in installs], indent=2))
        return 0

    if args.cmd == "run":
        selection = reconcile_hardware_profile(config_path=args.config, install_dir="/opt/mcd")
        cfg = load_config(args.config)
        if selection.changed:
            logging.info("Hardware service profile changed: %s -> %s", selection.previous_profile, selection.profile)
        note = maybe_notify_update(cfg)
        if note:
            logging.info(note)
        run_loop(cfg)
        return 0

    if args.cmd == "run-once":
        selection = reconcile_hardware_profile(config_path=args.config, install_dir="/opt/mcd")
        cfg = load_config(args.config)
        if selection.changed:
            logging.info("Hardware service profile changed: %s -> %s", selection.previous_profile, selection.profile)
        note = maybe_notify_update(cfg)
        if note:
            logging.info(note)
        run_loop(cfg, single_cycle=True)
        return 0

    shorthand = {
        "segments:update",
        "campaign:trigger",
        "campaign:rebuild",
        "campaigns:rebuild",
        "campaigns:update",
        "campaigns:trigger",
        "import",
        "cache:clear",
        "cache:warmup",
    }
    if args.cmd in shorthand:
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            root = _select_root_for_ops(cfg, args.root)
        except Exception as e:
            print(str(e))
            return 2
        rc, output = _run_manual_command_with_scheduler(
            cfg=cfg,
            php_bin=args.php_bin,
            root=root,
            command=args.cmd,
            instance_id=getattr(args, "instance_id", None),
            timeout_sec=args.timeout,
            run_as_user=args.run_as_user,
        )
        if output:
            print(output)
        return rc

    if args.cmd == "cache:hard":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            root = _select_root_for_ops(cfg, args.root)
        except Exception as e:
            print(str(e))
            return 2
        if bool(getattr(args, "local", False)):
            rc, output = _run_cache_hard_clear(cfg, root)
        else:
            rc, output = _run_cache_hard_clear_cluster_aware(cfg, root)
        if output:
            print(output)
        return rc

    if args.cmd == "permissions:fix":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            root = _select_root_for_ops(cfg, args.root)
        except Exception as e:
            print(str(e))
            return 2
        rc, output = _run_permissions_fix(cfg, root, getattr(args, "run_as_user", None))
        if output:
            print(output)
        if rc == 0:
            _push_state_after_change(cfg, "permissions-fix")
        return rc

    if args.cmd == "logical-issues":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            root = _select_root_for_ops(cfg, args.root)
        except Exception as e:
            print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            return 2
        runtime_store = TaskStore(cfg.state_db_path, cfg)
        try:
            if args.logical_issues_cmd == "status":
                payload = read_logical_issues_snapshot(cfg.state_db_path, root, runtime_store=runtime_store)
                print(json.dumps(payload, ensure_ascii=True, indent=2))
                return 0 if str(payload.get("status") or "") != "error" else 1
            inventory = InstanceInventory(cfg.state_db_path)
            ensure_seeded(inventory, cfg)
            install = next((item for item in inventory.list_instances() if str(item.root) == root), None)
            if install is None:
                print(
                    json.dumps(
                        {"status": "error", "reason": "instance not found in local inventory", "root": root},
                        ensure_ascii=True,
                        indent=2,
                    )
                )
                return 2
            try:
                if args.logical_issues_cmd == "scan":
                    payload = scan_install_logical_issues(cfg, install, runtime_store=runtime_store)
                    _push_state_after_change(cfg, "logical-issues-scan", runtime_store=runtime_store)
                else:
                    if args.target:
                        if len(args.target) > 100:
                            raise ValueError("logical issue remediation is limited to 100 targets")
                        targets: list[dict[str, object]] = []
                        for raw_target in args.target:
                            issue_id, separator, raw_ids = str(raw_target or "").partition("=")
                            raw_segment_ids = [value.strip() for value in raw_ids.split(",")]
                            if (
                                not separator
                                or not issue_id.strip()
                                or not raw_segment_ids
                                or any(not value.isdigit() or int(value) <= 0 for value in raw_segment_ids)
                            ):
                                raise ValueError("logical issue target must use ISSUE_ID=SEGMENT_ID[,SEGMENT_ID]")
                            segment_ids = [int(value) for value in raw_segment_ids]
                            targets.append({"issue_id": issue_id.strip(), "segment_ids": segment_ids})
                        payload = remediate_logical_issues(
                            cfg,
                            install,
                            targets=targets,
                            action=args.action,
                            actor=args.actor,
                            runtime_store=runtime_store,
                        )
                    else:
                        if not str(args.issue_id or "").strip():
                            raise ValueError("--issue-id or at least one --target is required")
                        payload = remediate_logical_issue(
                            cfg,
                            install,
                            issue_id=args.issue_id,
                            action=args.action,
                            actor=args.actor,
                            segment_ids=list(args.segment_id or []) or None,
                            runtime_store=runtime_store,
                        )
                    _push_state_after_change(cfg, "logical-issue-remediation", runtime_store=runtime_store)
            except Exception as e:
                payload = {"status": "error", "reason": str(e), "root": root}
                print(json.dumps(payload, ensure_ascii=True, indent=2))
                return 1
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if str(payload.get("status") or "") != "error" else 1
        finally:
            runtime_store.close()

    if args.cmd == "instance-delete":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            root = _select_root_for_ops(cfg, args.root, allow_missing_absolute=True)
        except Exception as e:
            print(str(e))
            return 2
        try:
            if bool(args.json):
                with contextlib.redirect_stdout(sys.stderr):
                    payload = delete_instance_artifacts(
                        cfg,
                        root=root,
                        domains=list(args.domain or []),
                        db_name=args.db_name,
                        delete_files=bool(args.delete_files),
                        delete_vhost=bool(args.delete_vhost),
                        delete_db=bool(args.delete_db),
                        yes=bool(args.yes),
                        dry_run=bool(args.dry_run),
                    )
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                payload = delete_instance_artifacts(
                    cfg,
                    root=root,
                    domains=list(args.domain or []),
                    db_name=args.db_name,
                    delete_files=bool(args.delete_files),
                    delete_vhost=bool(args.delete_vhost),
                    delete_db=bool(args.delete_db),
                    yes=bool(args.yes),
                    dry_run=bool(args.dry_run),
                )
                print(json.dumps(payload, ensure_ascii=True, indent=2))
        except Exception as e:
            if bool(args.json):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"instance-delete error: {e}")
            return 1
        if not bool(args.dry_run):
            _push_state_after_change(cfg, "instance-delete")
        return 0 if str(payload.get("status", "")).lower() in {"ok", "warning", "planned"} else 1

    if args.cmd == "admin:reset-password":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            payload = reset_admin_password(
                cfg,
                root=args.root,
                username=args.username,
                email=args.email,
                first_name=args.first_name,
                last_name=args.last_name,
                password_hash=args.password_hash,
            )
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"admin reset password failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            user = payload.get("user") if isinstance(payload, dict) else {}
            print(
                "admin reset password ok: action={action} username={username} email={email} role_id={role_id} "
                "root={root} db={db}".format(
                    action=str(payload.get("action") or "ok"),
                    username=str((user or {}).get("username") or ""),
                    email=str((user or {}).get("email") or ""),
                    role_id=int((user or {}).get("role_id") or 0),
                    root=str(payload.get("root") or ""),
                    db=str(payload.get("db_name") or ""),
                )
            )
        _push_state_after_change(cfg, "admin-reset-password")
        return 0

    if args.cmd == "admin:mfa-status":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            payload = hostnet_auth_mfa_status(
                cfg,
                root=args.root,
                username=args.username,
                email=args.email,
            )
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"admin mfa status failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            print(
                "admin mfa status: applicable={applicable} active_mfa={active_mfa} trusted_browsers={tb} "
                "plugin_installed={installed} plugin_published={published} root={root}".format(
                    applicable=1 if bool(payload.get("applicable")) else 0,
                    active_mfa=1 if bool(payload.get("active_mfa")) else 0,
                    tb=int(payload.get("trusted_browser_count") or 0),
                    installed=1 if bool(payload.get("plugin_installed")) else 0,
                    published=1 if bool(payload.get("plugin_published")) else 0,
                    root=str(payload.get("root") or ""),
                )
            )
        return 0

    if args.cmd == "admin:mfa-clear":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        try:
            payload = clear_hostnet_auth_mfa(
                cfg,
                root=args.root,
                username=args.username,
                email=args.email,
            )
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"admin mfa clear failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            print(
                "admin mfa clear: active_mfa={active_mfa} deleted_trusted_browsers={deleted} root={root}".format(
                    active_mfa=1 if bool(payload.get("active_mfa")) else 0,
                    deleted=int(payload.get("deleted_trusted_browsers") or 0),
                    root=str(payload.get("root") or ""),
                )
            )
        _push_state_after_change(cfg, "admin-mfa-clear")
        return 0

    if args.cmd == "mautic-locks:cleanup":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)
        installs = inv.list_instances()
        target_root = str(getattr(args, "root", "") or "").strip()
        if target_root:
            installs = [i for i in installs if i.root == target_root or i.instance_uid == target_root]
            if not installs:
                msg = f"Mautic install not found for root: {target_root}"
                if bool(getattr(args, "json", False)):
                    print(json.dumps({"status": "error", "reason": msg}, ensure_ascii=True, indent=2))
                else:
                    print(msg)
                return 2
        cutoff_utc = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1800, int(getattr(args, "min_age_sec", 21600) or 21600)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        file_lock_min_age_sec = max(0, int(getattr(args, "min_age_sec", 21600) or 21600))
        results: list[dict[str, object]] = []
        changed = 0
        for inst in installs:
            row: dict[str, object] = {
                "root": inst.root,
                "name": inst.name,
                "instance_uid": inst.instance_uid,
                "cutoff_utc": cutoff_utc,
            }
            try:
                file_payload = cleanup_stale_mautic_file_locks(
                    inst.root,
                    min_age_sec=file_lock_min_age_sec,
                )
                row["file_locks"] = file_payload.get("file_locks", [])
                row["cleared_file_locks"] = int(file_payload.get("cleared_file_locks", 0) or 0)
                row["skipped_live_file_locks"] = int(file_payload.get("skipped_live_file_locks", 0) or 0)
                changed += int(row["cleared_file_locks"])
            except Exception as e:
                row["file_lock_status"] = "error"
                row["file_lock_reason"] = str(e)
                row["cleared_file_locks"] = 0
            if not inst.db:
                row["status"] = "ok"
                row["reason"] = "missing_db_config"
                row["cleared_segments"] = 0
                row["cleared_campaigns"] = 0
                results.append(row)
                continue
            try:
                payload = MauticDB(inst.db).cleanup_stale_checked_out_locks(
                    cutoff_utc=cutoff_utc,
                    max_rows=max(1, int(getattr(args, "max_rows", 20) or 20)),
                )
                row["status"] = "ok"
                row["segments"] = payload.get("segments", [])
                row["campaigns"] = payload.get("campaigns", [])
                row["cleared_segments"] = int(payload.get("cleared_segments", 0) or 0)
                row["cleared_campaigns"] = int(payload.get("cleared_campaigns", 0) or 0)
                changed += int(row["cleared_segments"]) + int(row["cleared_campaigns"])
            except Exception as e:
                row["status"] = "error"
                row["reason"] = str(e)
            results.append(row)
        if changed > 0:
            _push_state_after_change(cfg, "mautic-locks-cleanup")
        if bool(getattr(args, "json", False)):
            print(
                json.dumps(
                    {"status": "ok", "cutoff_utc": cutoff_utc, "results": results},
                    ensure_ascii=True,
                    indent=2,
                    default=str,
                )
            )
        else:
            for row in results:
                print(
                    (
                        "root={root} status={status} cleared_segments={segments} "
                        "cleared_campaigns={campaigns} cleared_file_locks={file_locks}"
                    ).format(
                        root=str(row.get("root") or ""),
                        status=str(row.get("status") or "unknown"),
                        segments=int(row.get("cleared_segments", 0) or 0),
                        campaigns=int(row.get("cleared_campaigns", 0) or 0),
                        file_locks=int(row.get("cleared_file_locks", 0) or 0),
                    )
                )
                if str(row.get("status") or "") == "error":
                    print(f"  reason={str(row.get('reason') or '')}")
                if str(row.get("file_lock_status") or "") == "error":
                    print(f"  file_lock_reason={str(row.get('file_lock_reason') or '')}")
            print(f"cutoff_utc={cutoff_utc}")
        return 0

    if args.cmd == "email-counters":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        try:
            inst = _select_instance_for_ops(cfg, getattr(args, "root", None))
            if not inst.db:
                raise RuntimeError(f"Mautic install has no DB config: {inst.root}")
            db = MauticDB(inst.db)
            if str(args.op or "audit") == "repair":
                payload = repair_campaign_email_counters(db, int(args.campaign_id))
            else:
                payload = audit_campaign_email_counters(db, int(args.campaign_id))
            payload["root"] = inst.root
            payload["instance_uid"] = inst.instance_uid
            payload["name"] = inst.name
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"email-counters failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            print(
                "email-counters {mode}: root={root} campaign_id={campaign_id} "
                "checked={checked} mismatches={mismatches} repairable={repairable} repaired={repaired} skipped={skipped}".format(
                    mode=str(payload.get("mode") or args.op),
                    root=str(payload.get("root") or ""),
                    campaign_id=int(payload.get("campaign_id", 0) or 0),
                    checked=int(payload.get("checked", 0) or 0),
                    mismatches=int(payload.get("mismatches", 0) or 0),
                    repairable=int(payload.get("repairable", 0) or 0),
                    repaired=int(payload.get("repaired", 0) or 0),
                    skipped=int(payload.get("skipped", 0) or 0),
                )
            )
            for row in payload.get("emails", []):
                if not isinstance(row, dict):
                    continue
                drift = int(row.get("drift", 0) or 0)
                if drift == 0:
                    continue
                print(
                    "  email_id={email_id} cached={cached} actual={actual} drift={drift} "
                    "pending={pending} global_pending={global_pending} status={status} reason={reason}".format(
                        email_id=int(row.get("email_id", 0) or 0),
                        cached=int(row.get("cached_sent_count", 0) or 0),
                        actual=int(row.get("actual_sent_count", 0) or 0),
                        drift=drift,
                        pending=int(row.get("campaign_pending_event_logs", 0) or 0),
                        global_pending=int(row.get("global_pending_event_logs", 0) or 0),
                        status=str(row.get("status") or ""),
                        reason=str(row.get("reason") or ""),
                    )
                )
        if str(args.op or "audit") == "repair" and int(payload.get("repaired", 0) or 0) > 0:
            _push_state_after_change(cfg, "email-counters-repair")
        return 0

    if args.cmd == "report:email-activity":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        try:
            inst = _select_instance_for_ops(cfg, getattr(args, "root", None))
            if not inst.db:
                raise RuntimeError(f"Mautic install has no DB config: {inst.root}")
            include_summary = bool(getattr(args, "summary", False))
            include_extended = bool(getattr(args, "extended", False))
            if not include_summary and not include_extended:
                include_summary = True
                include_extended = True
            payload = collect_email_activity_report(
                MauticDB(inst.db),
                days=int(getattr(args, "days", 7) or 7),
                include_summary=include_summary,
                include_extended=include_extended,
                contact_mode=str(getattr(args, "contact_mode", "all") or "all"),
                contact_age_days=int(getattr(args, "contact_age_days", 30) or 30),
            )
            payload["root"] = inst.root
            payload["instance_uid"] = inst.instance_uid
            payload["name"] = inst.name
            payload["primary_domain"] = inst.primary_domain
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"email activity report failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            totals = payload.get("summary_totals") if isinstance(payload.get("summary_totals"), dict) else {}
            print(
                "email-activity: root={root} days={days} summary_rows={summary_rows} "
                "extended_rows={extended_rows} sent={sent} read={read} clicks={clicks} unsubscribed={unsubscribed}".format(
                    root=str(payload.get("root") or ""),
                    days=int(payload.get("days", 0) or 0),
                    summary_rows=len(payload.get("summary_rows") or []),
                    extended_rows=len(payload.get("extended_rows") or []),
                    sent=int(totals.get("sent", 0) or 0),
                    read=int(totals.get("read", 0) or 0),
                    clicks=int(totals.get("clicks", 0) or 0),
                    unsubscribed=int(totals.get("unsubscribed", 0) or 0),
                )
            )
        return 0

    if args.cmd == "report:contact-count":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        try:
            inst = _select_instance_for_ops(cfg, getattr(args, "root", None))
            if not inst.db:
                raise RuntimeError(f"Mautic install has no DB config: {inst.root}")
            payload = collect_contact_count_report(MauticDB(inst.db))
            payload["root"] = inst.root
            payload["instance_uid"] = inst.instance_uid
            payload["name"] = inst.name
            payload["primary_domain"] = inst.primary_domain
        except Exception as e:
            if bool(getattr(args, "json", False)):
                print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=True, indent=2))
            else:
                print(f"contact count report failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            print(
                "contact-count: root={root} real={real} email_only={email_only} "
                "mobile_only={mobile_only} both={both} excluded={excluded}".format(
                    root=str(payload.get("root") or ""),
                    real=int(payload.get("real_contacts", 0) or 0),
                    email_only=int(payload.get("email_only", 0) or 0),
                    mobile_only=int(payload.get("mobile_only", 0) or 0),
                    both=int(payload.get("email_and_mobile", 0) or 0),
                    excluded=int(payload.get("excluded_without_email_or_mobile", 0) or 0),
                )
            )
        return 0

    if args.cmd == "report:contact-field-metadata":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        inst = None
        try:
            inst = _select_instance_for_ops(cfg, getattr(args, "root", None))
            if not inst.db:
                raise RuntimeError(f"Mautic install has no DB config: {inst.root}")
            payload = collect_contact_field_metadata_report(MauticDB(inst.db))
            payload["mautic_version"] = collect_mautic_version(
                inst.root,
                cfg.php_bin,
                console_path=inst.console_path,
                run_as_user=cfg.mautic_run_as_user,
                expected_major=inst.mautic_major,
            )
        except Exception as e:
            payload = contact_field_metadata_error(str(e))
            if inst is not None:
                payload["root"] = inst.root
                payload["instance_uid"] = inst.instance_uid
                payload["name"] = inst.name
                payload["primary_domain"] = inst.primary_domain
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(f"contact field metadata report failed: {e}")
            return 1
        payload["root"] = inst.root
        payload["instance_uid"] = inst.instance_uid
        payload["name"] = inst.name
        payload["primary_domain"] = inst.primary_domain
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            print(
                "contact-field-metadata: root={root} fields={fields} schema={schema}".format(
                    root=str(payload.get("root") or ""),
                    fields=int(payload.get("field_count", 0) or 0),
                    schema=str(payload.get("schema") or ""),
                )
            )
        return 0

    if args.cmd == "exec":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        rc, output = _run_manual_command_with_scheduler(
            cfg=cfg,
            php_bin=args.php_bin,
            root=args.root,
            command=args.command,
            instance_id=args.instance_id,
            timeout_sec=args.timeout,
            run_as_user=args.run_as_user,
        )
        if output:
            print(output)
        return rc

    if args.cmd == "tune-segments":
        payload = tune_segments(
            config_path=args.config,
            root=args.root,
            max_parallel=args.max_parallel,
            apply=bool(args.apply),
            apply_priority=bool(args.apply_priority),
        )
        print(format_tune_result(payload))
        return 0

    if args.cmd in {"plugin-operations", "plugin-operation"}:
        cfg = load_config(args.config)
        try:
            return _plugin_operation_cli(cfg, args)
        except Exception as exc:
            payload = {"status": "error", "reason": str(exc)}
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(f"plugin operation error: {exc}", file=sys.stderr)
            return 1

    if args.cmd in {"plugins", "plugin"}:
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(args.catalog_json):
            print(f"NOTICE: {note}")
        rc = run_plugins_interactive(
            config=cfg,
            root=args.root,
            selection=args.select,
            bundles=list(args.bundle or []),
            plugin_uids=list(args.plugin_uid or []),
            action=args.action,
            no_color=bool(args.no_color),
            yes=bool(args.yes),
            list_available=bool(args.list_available),
            list_installed=bool(args.list_installed),
            catalog_json=bool(args.catalog_json),
            cluster_sync_check_key=getattr(args, "cluster_sync_check_key", None),
            cluster_sync_check_digest=getattr(args, "cluster_sync_check_digest", None),
        )
        if rc == 0:
            _push_state_after_change(cfg, "plugins")
        return rc

    if args.cmd == "cluster-assets":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        if args.op == "status":
            payload = collect_cluster_assets_status(cfg, root=args.root)
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
            else:
                print(format_cluster_assets_text(payload))
            return 0 if str(payload.get("status")) in {"ok", "disabled"} else 1
        if args.op == "guard":
            payload = guard_cluster_assets(
                cfg,
                root=args.root,
                fix_permissions=not bool(args.no_fix_perms),
                reload_on_change=bool(args.reload_on_change) or bool(getattr(cfg, "cluster_assets_reload_on_change", False)),
            )
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
            else:
                assets_payload = payload.get("assets") if isinstance(payload.get("assets"), dict) else payload
                print(format_cluster_assets_text(assets_payload))
                if payload.get("changed"):
                    print("changed roots: " + ", ".join(str(x) for x in payload.get("changed", [])))
                if payload.get("reload"):
                    print("reload status: " + str((payload.get("reload") or {}).get("status", "")))
            _push_state_after_change(cfg, "cluster-assets-guard")
            return 0 if str(payload.get("status")) in {"ok", "disabled"} else 1
        if args.op == "fix-perms":
            payload = fix_cluster_asset_permissions(cfg, root=args.root)
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
            else:
                for row in payload.get("results", []) if isinstance(payload.get("results"), list) else []:
                    print(
                        "root={root} asset={asset} status={status} path={path}{reason}".format(
                            root=str(row.get("root", "")),
                            asset=str(row.get("asset", "")),
                            status=str(row.get("status", "")),
                            path=str(row.get("path", "")),
                            reason=(" reason=" + str(row.get("reason", ""))) if row.get("reason") else "",
                        )
                    )
            _push_state_after_change(cfg, "cluster-assets-fix-perms")
            return 0 if str(payload.get("status")) == "ok" else 1
        if args.op == "reload":
            payload = reload_cluster_asset_runtime(
                cfg,
                root=args.root,
                cache_clear=not bool(args.no_cache_clear),
                cache_warm=not bool(args.no_cache_warm),
                fpm_reload=not bool(args.no_fpm_reload),
            )
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
            else:
                print("cluster assets runtime reload: status=" + str(payload.get("status", "unknown")))
                for row in payload.get("instances", []) if isinstance(payload.get("instances"), list) else []:
                    print(f"root={row.get('root')} status={row.get('status')}")
            _push_state_after_change(cfg, "cluster-assets-reload")
            return 0 if str(payload.get("status")) == "ok" else 1

    if args.cmd == "mautic-local-context":
        payload = dict(schema="mcd-mautic-local-context-v2", status="error",
                       code="local_context_selection_invalid")
        if os.geteuid() != 0:
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return 2
        try:
            from mcd_agent.mautic_patch_context import load_readonly_discovery_config
            from mcd_agent.mautic_local_context import read_local_context
            payload = read_local_context(load_readonly_discovery_config(args.config), root=args.root)
        except Exception:
            payload = dict(schema="mcd-mautic-local-context-v2", status="error",
                           code="local_context_discovery_unavailable")
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return 2
        print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0

    if args.cmd == "mautic-target-patch-verify":
        from mcd_agent.mautic_target_patch_collector import verify_cli
        result = verify_cli(config_path=args.config, root=args.root, prepared_target_id=args.prepared_target_id,
            verification_plan_file=args.verification_plan_file, verification_plan_sha256=args.verification_plan_sha256,
            authorization_context_file=args.authorization_context_file, authorization_context_sha256=args.authorization_context_sha256,
            patch_plan_file=args.patch_plan_file, patch_plan_sha256=args.patch_plan_sha256,
            catalog_file=args.catalog_file, catalog_sha256=args.catalog_sha256)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0 if result["status"] == "verified" else 2

    if args.cmd == "mautic-target-patch-verification-capability":
        from mcd_agent.mautic_target_verification_capability import capability
        print(json.dumps(capability(), sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0

    if args.cmd == "image-compatibility-capability":
        from mcd_agent.image_compatibility import capability
        print(json.dumps(capability(), sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0

    if args.cmd == "image-compatibility":
        from mcd_agent.image_compatibility import (
            ERROR_SCHEMA,
            MAX_REQUEST_BYTES,
            ImageCompatibilityError,
            load_request_bytes,
            load_request_file,
            probe,
        )
        try:
            if args.request_json == "-":
                accepted = load_request_bytes(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
            else:
                accepted = load_request_file(args.request_json)
            payload = probe(accepted)
        except ImageCompatibilityError as exc:
            payload = {
                "schema": ERROR_SCHEMA,
                "status": "error",
                "compatible": False,
                "code": str(exc),
            }
            print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 2
        except Exception:
            payload = {
                "schema": ERROR_SCHEMA,
                "status": "error",
                "compatible": False,
                "code": "fresh_probe_failed",
            }
            print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 2
        if bool(args.json):
            print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
        else:
            print(
                "status={status} compatible={compatible} candidates={candidates}".format(
                    status=payload.get("status", "unknown"),
                    compatible=str(bool(payload.get("compatible"))).lower(),
                    candidates=len(payload.get("candidates", [])),
                )
            )
        return 0

    if args.cmd == "mautic-upgrade":
        if args.patch_plan_file:
            if args.patch_plan_json:
                raise RuntimeError("Choose exactly one patch plan transport")
            from mcd_agent.mautic_patch_resolution import read_plan_file
            args.patch_plan_json = read_plan_file(args.patch_plan_file, args.patch_plan_sha256)
        if args.mcc_preflighted_single_instance and args.op != "apply":
            print("--mcc-preflighted-single-instance is valid only with mautic-upgrade apply")
            return 2
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if args.op == "check":
            return run_upgrade_check(cfg, args.root)
        if args.op == "preflight":
            return run_upgrade_preflight(
                config=cfg,
                root=args.root,
                mode=args.mode,
                target_override=str(args.target or "").strip() or None,
                repair_plan_json=str(args.repair_plan_json or "").strip() or None,
                with_system_upgrade=bool(args.with_system_upgrade),
            )
        if args.op == "composer-prepare":
            return run_upgrade_composer_prepare(
                config=cfg,
                root=args.root,
                mode=args.mode,
                target_override=str(args.target or "").strip() or None,
                with_system_upgrade=bool(args.with_system_upgrade),
            )
        if args.op == "authorize-repair":
            if not args.target or not args.repair_plan_json or not args.backup_manifest_path:
                print("authorize-repair requires --target, --repair-plan-json and --backup-manifest-path")
                return 2
            return run_upgrade_authorize_repair(
                config=cfg,
                root=args.root,
                target_override=str(args.target or "").strip(),
                repair_plan_json=str(args.repair_plan_json),
                backup_manifest_path=str(args.backup_manifest_path),
                key_path=str(args.repair_auth_key_file or "/etc/mcd/mcc-operation-signing.key"),
                output_path=str(args.repair_auth_output_file or "").strip() or None,
            )
        if args.op == "interactive":
            rc = run_upgrade_interactive(cfg, args.root)
            if rc == 0:
                _push_state_after_change(cfg, "mautic-upgrade-interactive")
            return rc
        rc = run_upgrade_apply(
            config=cfg,
            root=args.root,
            mode=args.mode,
            yes=bool(args.yes),
            do_backup=bool(args.backup),
            with_system_upgrade=bool(args.with_system_upgrade),
            target_override=str(args.target or "").strip() or None,
            allow_minor=bool(args.allow_minor),
            allow_major=bool(args.allow_major),
            allow_release_transition=bool(args.allow_release_transition),
            mcc_release_authorization_context_file=str(args.mcc_release_authorization_context_file or ""),
            mcc_release_authorization_context_sha256=str(args.mcc_release_authorization_context_sha256 or ""),
            patch_plan_json=str(args.patch_plan_json or "") or None,
            patch_run_id=str(args.patch_run_id or "") or None,
            repair_plan_json=str(args.repair_plan_json or "") or None,
            repair_auth_context_file=str(args.repair_auth_context_file or "") or None,
            patch_backup_context_file=str(args.patch_backup_context_file or "") or None,
            repair_auth_key_file=str(args.repair_auth_key_file or "/etc/mcd/mcc-operation-signing.key"),
            mcc_preflighted_single_instance=bool(args.mcc_preflighted_single_instance),
            prepared_target_json=str(args.prepared_target_json or "") or None,
            prepared_target_file=str(args.prepared_target_file or "") or None,
            prepared_target_sha256=str(args.prepared_target_sha256 or ""),
        )
        if rc == 0:
            _push_state_after_change(cfg, "mautic-upgrade-apply")
        return rc

    if args.cmd == "interactive":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        return _run_interactive_hub(cfg, args.root, bool(args.no_color))

    if args.cmd == "instances":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)
        if args.op == "list":
            rows = inv.list_instances()
            if not rows:
                print("No instances")
                return 0
            for i in rows:
                domains = [str(x).strip() for x in (i.domains or []) if str(x).strip()]
                if not domains and i.primary_domain:
                    domains = [str(i.primary_domain).strip()]
                domains_cell = ",".join(domains)
                runtime = str(getattr(i, "runtime", "host") or "host")
                install_type = str(i.install_type or detect_install_type(i.root)).strip().lower() or "unknown"
                print(
                    f"{i.name}\t{i.root}\t{i.source}\tmajor={i.mautic_major}\tuid={i.instance_uid}"
                    f"\tdomains={domains_cell}\truntime={runtime}\tinstall_type={install_type}"
                )
            return 0
        if args.op == "rescan":
            count = inv.rescan(cfg)
            print(f"Rescan complete: {count} instances")
            _push_state_after_change(cfg, "instances-rescan")
            return 0
        if args.op == "add":
            if not (args.name and args.root and args.console_path):
                print("--name --root --console-path are required for add")
                return 2
            inv.add_or_update_manual(
                name=args.name,
                root=args.root,
                console_path=args.console_path,
                local_php_path=args.local_php_path,
                mautic_major=args.mautic_major,
                db_host=args.db_host,
                db_port=args.db_port,
                db_name=args.db_name,
                db_user=args.db_user,
                db_password=args.db_password,
                db_table_prefix=args.db_table_prefix,
            )
            print("Manual instance saved")
            _push_state_after_change(cfg, "instances-add")
            return 0
        if args.op == "remove":
            if not args.name:
                print("--name is required for remove")
                return 2
            print("Removed" if inv.remove(args.name) else "Not found")
            _push_state_after_change(cfg, "instances-remove")
            return 0

    if args.cmd == "mautic-image":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if bool(args.json):
            with contextlib.redirect_stdout(sys.stderr):
                result = install_from_image(
                    cfg,
                    image_ref=str(args.image_ref),
                    domain=str(args.domain),
                    php_version=str(args.php_version),
                    yes=bool(args.yes),
                    run_certbot=not bool(args.no_certbot),
                    certbot_dns_credential_ref=str(args.certbot_dns_credential_ref or "").strip() or None,
                    own_mail=bool(args.own_mail),
                    mail_profile_id=str(args.mail_profile_id or "").strip() or None,
                )
        else:
            result = install_from_image(
                cfg,
                image_ref=str(args.image_ref),
                domain=str(args.domain),
                php_version=str(args.php_version),
                yes=bool(args.yes),
                run_certbot=not bool(args.no_certbot),
                certbot_dns_credential_ref=str(args.certbot_dns_credential_ref or "").strip() or None,
                own_mail=bool(args.own_mail),
                mail_profile_id=str(args.mail_profile_id or "").strip() or None,
            )
        if bool(args.json):
            print(json.dumps(result, ensure_ascii=True, indent=2))
        _push_state_after_change(cfg, "mautic-image-install")
        return 0

    if args.cmd == "local-mail":
        cfg = load_config(args.config)
        if args.op == "submit":
            return submit_local_mail(
                domain=str(args.instance_domain),
                sendmail_args=list(args.sendmail_args or []),
                cfg=cfg,
            )
        if args.op == "receive":
            try:
                message = sys.stdin.buffer.read(25 * 1024 * 1024 + 1)
                result = receive_local_mail(
                    cfg,
                    domain=str(args.instance_domain),
                    kind=str(args.kind),
                    data=message,
                )
                logging.info(
                    "own-host inbound domain=%s type=%s status=%s contacts=%s dnc_added=%s",
                    result.get("instance_domain", "-"),
                    result.get("type", "-"),
                    result.get("status", "-"),
                    result.get("contacts", 0),
                    result.get("dnc_added", 0),
                )
                return 0
            except Exception as exc:
                print(f"own-host inbound processing failed: {exc}", file=sys.stderr)
                return 75
        try:
            if args.op == "configure":
                if not str(args.root or "").strip():
                    raise RuntimeError("--root is required for local-mail configure")
                result = configure_local_mail(cfg, domain=str(args.instance_domain), root=str(args.root))
            elif args.op == "disable":
                result = disable_local_mail(cfg, domain=str(args.instance_domain))
            else:
                result = local_mail_status(cfg, domain=str(args.instance_domain), push=bool(args.push))
        except Exception as exc:
            if bool(args.json):
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True))
            else:
                print(f"local-mail error: {exc}", file=sys.stderr)
            return 1
        if bool(args.json):
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=True))
        return 0

    if args.cmd == "mail-config":
        cfg = load_config(args.config)
        try:
            operation = preflight_mail_profile if str(args.mail_config_op) == "preflight" else apply_mail_profile
            result = operation(
                cfg,
                profile_id=str(args.profile_id),
                domain=str(args.instance_domain),
                root=str(args.root),
            )
        except Exception as exc:
            if bool(args.json):
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True))
            else:
                print(f"mail-config error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=True, indent=2 if bool(args.json) else None))
        return 0

    if args.cmd == "composer-move":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if bool(args.json):
            with contextlib.redirect_stdout(sys.stderr):
                result = move_zip_to_composer(
                    cfg,
                    root=str(args.root),
                    domain=str(args.domain),
                    mautic_major=int(args.mautic_major or 0),
                    yes=bool(args.yes),
                )
        else:
            result = move_zip_to_composer(
                cfg,
                root=str(args.root),
                domain=str(args.domain),
                mautic_major=int(args.mautic_major or 0),
                yes=bool(args.yes),
            )
        if bool(args.json):
            print(json.dumps(result, ensure_ascii=True, indent=2))
        _push_state_after_change(cfg, "composer-move")
        return 0

    if args.cmd == "reload-config":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        inv = InstanceInventory(cfg.state_db_path)
        count = inv.rescan(cfg)
        print(f"Reload complete: {count} instances")
        _push_state_after_change(cfg, "reload-config")
        return 0

    if args.cmd == "env":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if args.target == "ipv6":
            if args.op == "status":
                st = ipv6_status()
                for k, v in st.items():
                    print(f"{k}={v}")
                return 0
            if args.op in {"disable", "enable"}:
                for line in set_ipv6_disabled(args.op == "disable"):
                    print(line)
                _push_state_after_change(cfg, f"env-ipv6-{args.op}")
                return 0
            raise RuntimeError("unsupported env ipv6 operation")
        if args.target == "policy":
            if args.op == "show":
                print(json.dumps(default_policy(), ensure_ascii=True, indent=2))
                return 0
            if args.op == "plan":
                raw = ""
                if args.policy_file:
                    raw = Path(args.policy_file).read_text(encoding="utf-8")
                elif args.policy_json:
                    raw = str(args.policy_json)
                elif args.policy_b64:
                    raw = base64.b64decode(str(args.policy_b64)).decode("utf-8")
                payload = parse_policy_text(raw)
                if bool(args.json):
                    print(json.dumps(payload, ensure_ascii=True, indent=2))
                    return 0
                for line in build_policy_plan(payload, component=str(args.component or "all")):
                    print(line)
                return 0
            raise RuntimeError("unsupported env policy operation")
        raise RuntimeError("unsupported env target")

    if args.cmd == "service-profile":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        comp = str(args.component or "php_fpm")
        if args.op == "status":
            payload = {
                "enabled": bool(cfg.service_profiles_enabled),
                "auto_apply": bool(cfg.service_profiles_auto_apply),
                "poll_interval_sec": int(cfg.service_profiles_poll_interval_sec),
                "components": list(cfg.service_profiles_components or []),
            }
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        if args.op == "fetch":
            if comp not in {"php_fpm", "php-fpm", "mysql", "apt", "wazuh", "docker", "mautic_db_indexes", "mautic-db-indexes", "db_indexes", "db-indexes"}:
                print(json.dumps({"status": "error", "reason": f"unsupported component: {comp}"}, ensure_ascii=True))
                return 2
            if comp.replace("-", "_") in {"docker", "mautic_db_indexes", "db_indexes"}:
                res = service_profiles_apply_once(
                    cfg,
                    component=comp,
                    dry_run=True,
                    allow_cluster_db_maintenance=bool(args.allow_cluster_db_maintenance),
                )
                print(json.dumps(res, ensure_ascii=True, indent=2))
                return 0 if str(res.get("status", "")).strip().lower() in {"ok", "skipped"} else 1
            res = fetch_service_profile(cfg, comp)
            print(json.dumps(res, ensure_ascii=True, indent=2))
            return 0 if str(res.get("status", "")).strip().lower() == "ok" else 1
        if args.op == "rescan":
            if comp not in {"apt"}:
                print(json.dumps({"status": "error", "reason": "rescan is supported only for --component apt"}, ensure_ascii=True))
                return 2
            cleared = clear_apt_repo_profile_markers(cfg=cfg)
            res = service_profiles_apply_once(
                cfg,
                component=comp,
                dry_run=bool(args.dry_run),
                force_apt_repo_rescan=True,
            )
            out = {"status": str(res.get("status", "")).strip().lower() or "error", "cleared": cleared, "result": res}
            print(json.dumps(out, ensure_ascii=True, indent=2))
            ok = str(res.get("status", "")).strip().lower() == "ok"
            if ok and not bool(args.dry_run):
                _push_state_after_change(cfg, "service-profile-apt-rescan")
            return 0 if ok else 1
        # apply
        res = service_profiles_apply_once(
            cfg,
            component=comp,
            dry_run=bool(args.dry_run),
            allow_cluster_db_maintenance=bool(args.allow_cluster_db_maintenance),
        )
        print(json.dumps(res, ensure_ascii=True, indent=2))
        ok = str(res.get("status", "")).strip().lower() == "ok"
        if ok and not bool(args.dry_run):
            _push_state_after_change(cfg, f"service-profile-{comp}-apply")
        return 0 if ok else 1

    if args.cmd == "zabbix":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        op = str(args.op or "status").strip().lower()
        if op == "status":
            payload = collect_apt_state(timeout_sec=25, cfg=cfg, auto_bootstrap_zabbix=False)
            zbx_state = payload.get("zabbix_mysql_monitor") if isinstance(payload, dict) else {}
            out = {"status": "ok", "zabbix_mysql_monitor": zbx_state}
            print(json.dumps(out, ensure_ascii=True, indent=2))
            return 0

        fetched = fetch_service_profile(cfg, "apt")
        profile = fetched.get("profile") if isinstance(fetched, dict) else None
        if not isinstance(profile, dict):
            profile = {}
        res = ensure_zabbix_mysql_monitor_user(profile, cfg=cfg, force=bool(args.force), timeout_sec=20)
        out = {
            "status": "ok" if str(res.get("status", "")).strip().lower() in {"applied", "already_present", "noop", "skipped", "disabled"} else "error",
            "fetch_status": str(fetched.get("status", "n/a")) if isinstance(fetched, dict) else "n/a",
            "result": res,
        }
        print(json.dumps(out, ensure_ascii=True, indent=2))
        if out["status"] == "ok":
            _push_state_after_change(cfg, "zabbix-bootstrap-mysql-user")
            return 0
        return 1

    if args.cmd in {"shortner", "shortener"}:
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        op = str(args.op or "detect").strip().lower()
        try:
            if op == "detect":
                payload = {"status": "ok", "items": discover_yourls()}
            elif op == "version":
                if not args.root:
                    raise RuntimeError("--root is required for shortner version")
                payload = {
                    "status": "ok",
                    "kind": "yourls",
                    "root": str(Path(args.root).resolve()),
                    "version": yourls_version(args.root),
                }
            elif op == "check-update":
                if not args.root:
                    raise RuntimeError("--root is required for shortner check-update")
                payload = check_yourls_update(args.root)
            elif op == "update":
                if not args.root:
                    raise RuntimeError("--root is required for shortner update")
                payload = update_yourls(args.root, target_version=args.target_version, yes=bool(args.yes))
            else:
                raise RuntimeError(f"unsupported shortner op: {op}")
        except Exception as e:
            payload = {"status": "error", "reason": str(e)}
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(f"shortner {op} failed: {e}")
            return 1
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            if op == "detect":
                for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
                    print(
                        "yourls root={root} version={version} site={site} active_nginx={active}".format(
                            root=str(item.get("root") or ""),
                            version=str(item.get("version") or "-"),
                            site=str(item.get("site_url") or "-"),
                            active=1 if bool(item.get("active_nginx")) else 0,
                        )
                    )
            else:
                print(json.dumps(payload, ensure_ascii=True))
        if op == "update" and payload.get("status") == "ok" and payload.get("changed"):
            _push_state_after_change(cfg, "shortner-update")
        return 0

    if args.cmd == "runtime-overrides":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        op = str(args.op or "show").strip().lower()
        if op in {"show", "status"}:
            payload = {
                "config": cfg.config_file_path,
                "local_runtime_overrides": local_runtime_overrides(cfg),
            }
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        if op == "fetch":
            res = fetch_runtime_overrides(cfg)
            print(json.dumps(res, ensure_ascii=True, indent=2))
            return 0 if str(res.get("status", "")).strip().lower() in {"ok", "disabled"} else 1
        if op == "push":
            res = push_runtime_overrides(
                cfg,
                local_runtime_overrides(cfg),
                merge=bool(args.merge),
                target=str(args.target or "observed"),
            )
            print(json.dumps(res, ensure_ascii=True, indent=2))
            return 0 if str(res.get("status", "")).strip().lower() in {"ok", "disabled"} else 1
        # trigger
        path = touch_poll_trigger(cfg)
        payload = {"status": "ok", "trigger_path": path}
        if bool(args.json):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            print(f"runtime_overrides_trigger={path}")
        return 0

    if args.cmd == "scheduler":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        flag = Path(cfg.scheduler_pause_flag_path)
        flag.parent.mkdir(parents=True, exist_ok=True)
        if args.op == "pause":
            flag.write_text("paused\n", encoding="utf-8")
            print(f"paused: {flag}")
            return 0
        if args.op == "resume":
            if flag.exists():
                flag.unlink()
            print(f"resumed: {flag}")
            return 0
        paused = flag.exists()
        tracked, external = _observed_running_tasks(cfg)
        running = len(tracked) + len(external)
        if bool(getattr(args, "json", False)):
            payload: dict[str, object] = {
                "paused": bool(paused),
                "running_tasks": int(running),
                "tracked_running_tasks": int(len(tracked)),
                "external_running_tasks": int(len(external)),
            }
            if bool(getattr(args, "verbose", False)):
                payload["tracked_tasks"] = tracked
                payload["external_tasks"] = external
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        print(
            "paused={paused} running_tasks={running} tracked={tracked_count} external={external_count}".format(
                paused=str(paused).lower(),
                running=running,
                tracked_count=len(tracked),
                external_count=len(external),
            )
        )
        if bool(getattr(args, "verbose", False)):
            for task in tracked:
                print(
                    "task type={task_type} entity={entity} pid={pid} root={root} cmd={cmd}".format(
                        task_type=str(task.get("task_type") or "-"),
                        entity=str(task.get("entity_id") if task.get("entity_id") is not None else "-"),
                        pid=str(task.get("pid") or "-"),
                        root=str(task.get("root") or "-"),
                        cmd=str(task.get("command_str") or "-"),
                    )
                )
            for task in external:
                print(
                    "external type={task_type} entity={entity} pid={pid} root={root} cmd={cmd}".format(
                        task_type=str(task.get("task_type") or "-"),
                        entity=str(task.get("entity_id") if task.get("entity_id") is not None else "-"),
                        pid=str(task.get("pid") or "-"),
                        root=str(task.get("root") or "-"),
                        cmd=str(task.get("command_str") or "-"),
                    )
                )
        return 0

    if args.cmd == "apt-upgrade":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if not bool(args.yes):
            ans = _ask(
                "Run apt-get update && apt-get dist-upgrade -y with keep-local-config behavior? [y/N]: "
            ).strip().lower()
            if ans not in {"y", "yes"}:
                if bool(args.json):
                    print(json.dumps({"status": "cancelled"}, ensure_ascii=True, indent=2))
                else:
                    print("cancelled")
                return 1
        env = dict(os.environ)
        env["DEBIAN_FRONTEND"] = "noninteractive"
        cmd_update = ["apt-get", "update", "--allow-releaseinfo-change"]
        cmd_upgrade = [
            "apt-get",
            "dist-upgrade",
            "-y",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
        ]
        started = time.time()
        p_update = subprocess.run(cmd_update, capture_output=True, text=True, env=env)
        update_stdout = p_update.stdout or ""
        update_stderr = p_update.stderr or ""
        if update_stdout:
            print(update_stdout, end="")
        if update_stderr:
            print(update_stderr, end="", file=sys.stderr)
        p_upgrade = None
        if p_update.returncode == 0:
            p_upgrade = subprocess.run(cmd_upgrade, capture_output=True, text=True, env=env)
            up_stdout = p_upgrade.stdout or ""
            up_stderr = p_upgrade.stderr or ""
            if up_stdout:
                print(up_stdout, end="")
            if up_stderr:
                print(up_stderr, end="", file=sys.stderr)
        duration = int(max(0, time.time() - started))
        update_rc = int(p_update.returncode)
        upgrade_rc = int(p_upgrade.returncode) if p_upgrade is not None else None
        ok = update_rc == 0 and (upgrade_rc in {0, None})
        postcheck_lines: list[str] = []
        if ok:
            post_ok, postcheck_lines = _apt_service_postcheck()
            for line in postcheck_lines:
                print(line)
            ok = ok and post_ok
        payload = {
            "status": ("ok" if ok else "error"),
            "update_rc": update_rc,
            "upgrade_rc": upgrade_rc,
            "duration_sec": duration,
            "dpkg_conf_policy": "force-confdef + force-confold",
            "postcheck_lines": postcheck_lines,
        }
        if bool(args.json):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            print(
                "apt_upgrade={status} update_rc={update_rc} upgrade_rc={upgrade_rc} duration_sec={duration_sec} conf=force-confold".format(
                    **payload
                )
            )
        if ok:
            _push_state_after_change(cfg, "apt-upgrade")
        return 0 if ok else 1

    if args.cmd == "tasks-lifecycle":
        cfg = load_config(args.config)
        if args.op == "apply" and not bool(args.yes):
            parser.error("tasks-lifecycle apply requires --yes")
        store = TaskStore(cfg.state_db_path, cfg)
        try:
            if args.op == "purge-failed":
                preview = store.purge_failed_tasks(
                    admission_dir=str(args.admission_dir),
                    dry_run=True,
                )
                if bool(args.yes):
                    result = store.purge_failed_tasks(
                        admission_dir=str(args.admission_dir),
                        dry_run=False,
                        expected_candidate_ids=preview.get("candidate_ids", []),
                    )
                    result["preflight_candidate_ids"] = preview.get("candidate_ids", [])
                else:
                    result = preview
            else:
                result = store.task_lifecycle(
                    now_ts=time.time(),
                    min_age_sec=(
                        int(args.min_age_sec)
                        if args.min_age_sec is not None
                        else max(3600, int(cfg.tasks_history_keep_days) * 86400)
                    ),
                    archive_dir=str(args.archive_dir or cfg.tasks_archive_dir),
                    admission_dir=str(args.admission_dir),
                    dry_run=args.op != "apply",
                )
        finally:
            store.close()
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            print(
                "phase={phase} candidates={candidates} deleted={deleted} protected={protected} "
                "dead_letter={dead_letter} running={running} archive={archive}".format(
                    phase=result.get("phase", "-"),
                    candidates=len(result.get("candidate_ids", [])),
                    deleted=len(result.get("deleted_ids", [])),
                    protected=len(result.get("protected_ids", [])),
                    dead_letter=len(result.get("dead_letter_ids", result.get("preserved_by_state", {}).get("lost", []))),
                    running=len(result.get("running_ids", result.get("preserved_by_state", {}).get("running", []))),
                    archive=result.get("archive_path", "-"),
                )
            )
            print("candidate_ids=" + ",".join(str(x) for x in result.get("candidate_ids", [])))
        return 0

    if args.cmd == "state-db":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        cfg_eff, st = _state_backend_config_and_status(cfg)
        if args.op == "status":
            if args.json:
                print(json.dumps(st, ensure_ascii=True, indent=2))
            else:
                print(
                    "desired={desired} active={active} mode={mode} db={db} reason={reason}".format(
                        desired=str(st.get("desired_backend", "-")),
                        active=str(st.get("active_backend", "-")),
                        mode=str(st.get("mode", "-")),
                        db=str(st.get("database", "-")),
                        reason=str(st.get("reason", "-")),
                    )
                )
                if st.get("error"):
                    print(f"error={st.get('error')}")
            return 0

        # op == init
        if not _state_db_missing_only(cfg_eff, st):
            out = {
                "ok": False,
                "reason": "state_db_init_allowed_only_in_legacy_missing_or_inaccessible_state",
                "status": st,
            }
            if args.json:
                print(json.dumps(out, ensure_ascii=True, indent=2))
            else:
                print("State DB bootstrap is allowed only in legacy mode when state DB is missing/inaccessible.")
                print(json.dumps(st, ensure_ascii=True))
            return 1

        host_default = str(args.admin_host or cfg_eff.state_mysql_host or "localhost")
        port_default = int(args.admin_port or cfg_eff.state_mysql_port or 3306)
        user_default = str(args.admin_user or "root")
        sock_default = str(args.admin_unix_socket or cfg_eff.state_mysql_unix_socket or "").strip()
        if bool(args.admin_password_stdin):
            admin_pwd = sys.stdin.read().rstrip("\n")
        else:
            admin_pwd = getpass.getpass("DB admin password (empty allowed): ")
        ok, msg, cfg_after = _bootstrap_state_db_with_admin(
            cfg_eff,
            admin_user=user_default,
            admin_password=admin_pwd if admin_pwd != "" else None,
            admin_host=host_default,
            admin_port=port_default,
            admin_socket=sock_default or None,
        )
        after = _state_backend_status_payload(cfg_after if ok else cfg_eff)
        out = {"ok": bool(ok), "message": msg, "status": after}
        if args.json:
            print(json.dumps(out, ensure_ascii=True, indent=2))
        else:
            print(msg)
            print(json.dumps(after, ensure_ascii=True))
        if ok:
            _push_state_after_change(cfg_after, "state-db-init")
        return 0 if ok else 1

    if args.cmd == "maintenance":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        flag = Path(cfg.scheduler_pause_flag_path)
        flag.parent.mkdir(parents=True, exist_ok=True)
        maintenance_state = collect_maintenance_state(cfg)

        if args.op == "off":
            if flag.exists():
                flag.unlink()
            cron_restore = restore_cron_service_if_needed(cfg)
            tracked, external = _observed_running_tasks(cfg)
            managed_pids = {
                int(x.get("pid") or 0)
                for x in (tracked + external)
                if int(x.get("pid") or 0) > 0
            }
            consoles = _list_mautic_console_processes()
            orphan_count = sum(1 for pid, _ in consoles if pid not in managed_pids)
            maintenance_state = collect_maintenance_state(cfg)
            payload = {
                "status": "ok" if bool(cron_restore.get("ok", True)) else "error",
                "mode": str(maintenance_state.get("mode", "off")),
                "paused": bool(maintenance_state.get("paused", False)),
                "active": bool(maintenance_state.get("active", False)),
                "cron_stopped": bool(maintenance_state.get("cron_stopped", False)),
                "cron_service_name": str(maintenance_state.get("cron_service_name", "") or ""),
                "cron_service_active": maintenance_state.get("cron_service_active"),
                "tracked_running": len(tracked),
                "external_running": len(external),
                "managed_running": len(tracked) + len(external),
                "mautic_console_total": len(consoles),
                "orphan_console": orphan_count,
                "cron_restore": cron_restore,
            }
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(
                    "maintenance=off paused={paused} cron_stopped={cron_stopped} cron_active={cron_active} "
                    "cron_unit={cron_unit} managed_running={managed} tracked_running={tracked} external_running={external} "
                    "mautic_console_total={total} orphan_console={orphans}".format(
                        paused=str(payload["paused"]).lower(),
                        cron_stopped=str(payload["cron_stopped"]).lower(),
                        cron_active=str(payload.get("cron_service_active")),
                        cron_unit=(payload.get("cron_service_name") or "-"),
                        managed=payload["managed_running"],
                        tracked=payload["tracked_running"],
                        external=payload["external_running"],
                        total=payload["mautic_console_total"],
                        orphans=payload["orphan_console"],
                    )
                )
                if not bool(cron_restore.get("ok", True)):
                    print(f"WARN cron restore failed: {str(cron_restore.get('message', '') or '').strip()}")
            _push_state_after_change(cfg, "maintenance-off")
            return 0 if bool(cron_restore.get("ok", True)) else 1

        if args.op == "status":
            tracked, external = _observed_running_tasks(cfg)
            managed_pids = {
                int(x.get("pid") or 0)
                for x in (tracked + external)
                if int(x.get("pid") or 0) > 0
            }
            consoles = _list_mautic_console_processes()
            orphan_count = sum(1 for pid, _ in consoles if pid not in managed_pids)
            payload = {
                "status": "ok",
                "mode": str(maintenance_state.get("mode", "off")),
                "paused": bool(maintenance_state.get("paused", False)),
                "active": bool(maintenance_state.get("active", False)),
                "cron_stopped": bool(maintenance_state.get("cron_stopped", False)),
                "cron_service_name": str(maintenance_state.get("cron_service_name", "") or ""),
                "cron_service_active": maintenance_state.get("cron_service_active"),
                "tracked_running": len(tracked),
                "external_running": len(external),
                "managed_running": len(tracked) + len(external),
                "mautic_console_total": len(consoles),
                "orphan_console": orphan_count,
            }
            if bool(args.json):
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(
                    "maintenance={mode} paused={paused} cron_stopped={cron_stopped} cron_active={cron_active} "
                    "cron_unit={cron_unit} managed_running={managed} tracked_running={tracked} external_running={external} "
                    "mautic_console_total={total} orphan_console={orphans}".format(
                        mode=payload["mode"],
                        paused=str(payload["paused"]).lower(),
                        cron_stopped=str(payload["cron_stopped"]).lower(),
                        cron_active=str(payload.get("cron_service_active")),
                        cron_unit=(payload.get("cron_service_name") or "-"),
                        managed=payload["managed_running"],
                        tracked=payload["tracked_running"],
                        external=payload["external_running"],
                        total=payload["mautic_console_total"],
                        orphans=payload["orphan_console"],
                    )
                )
            return 0

        # op == "on"
        flag.write_text("paused\n", encoding="utf-8")
        stop_count = 0
        failed_count = 0
        cron_stop = {"ok": True, "requested": False, "unit": "", "cron_stopped": False, "message": ""}

        if not bool(args.no_kill_running):
            tracked, external = _observed_running_tasks(cfg)
            managed = tracked + external
            seen_pids: set[int] = set()
            for task in managed:
                pid = int(task.get("pid") or 0)
                if pid <= 0 or pid in seen_pids:
                    continue
                seen_pids.add(pid)
                res = _kill_pid(pid, int(args.grace_sec))
                if res in {"terminated", "killed", "already-exited"}:
                    stop_count += 1
                else:
                    failed_count += 1
                    print(
                        "WARN stop failed: pid={pid} task={task_type} entity={entity} result={res}".format(
                            pid=pid,
                            task_type=str(task.get("task_type") or "-"),
                            entity=str(task.get("entity_id") if task.get("entity_id") is not None else "-"),
                            res=res,
                        )
                    )

            if bool(args.kill_orphans):
                tracked_pids = {
                    int(x.get("pid") or 0)
                    for x in managed
                    if int(x.get("pid") or 0) > 0
                }
                for pid, cmd in _list_mautic_console_processes():
                    if pid in tracked_pids:
                        continue
                    res = _kill_pid(pid, int(args.grace_sec))
                    if res in {"terminated", "killed", "already-exited"}:
                        stop_count += 1
                    else:
                        failed_count += 1
                        print(f"WARN orphan stop failed: pid={pid} result={res} cmd={cmd}")

        if bool(args.stop_cron):
            cron_stop = stop_cron_service(cfg)
            if not bool(cron_stop.get("ok", False)):
                print(f"WARN cron stop failed: {str(cron_stop.get('message', '') or '').strip()}")

        tracked_after, external_after = _observed_running_tasks(cfg)
        consoles_after = _list_mautic_console_processes()
        maintenance_state = collect_maintenance_state(cfg)
        payload = {
            "status": "ok" if bool(cron_stop.get("ok", True)) else "error",
            "mode": str(maintenance_state.get("mode", "on")),
            "paused": bool(maintenance_state.get("paused", True)),
            "active": bool(maintenance_state.get("active", True)),
            "cron_stopped": bool(maintenance_state.get("cron_stopped", False)),
            "cron_service_name": str(maintenance_state.get("cron_service_name", "") or ""),
            "cron_service_active": maintenance_state.get("cron_service_active"),
            "stopped": stop_count,
            "stop_failed": failed_count,
            "tracked_running": len(tracked_after),
            "external_running": len(external_after),
            "managed_running": len(tracked_after) + len(external_after),
            "mautic_console_total": len(consoles_after),
            "cron_stop": cron_stop,
        }
        if bool(args.json):
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            print(
                "maintenance=on paused=true cron_stopped={cron_stopped} cron_active={cron_active} cron_unit={cron_unit} "
                "stopped={stopped} stop_failed={failed} managed_running={managed} tracked_running={tracked} "
                "external_running={external} mautic_console_total={total}".format(
                    cron_stopped=str(payload["cron_stopped"]).lower(),
                    cron_active=str(payload.get("cron_service_active")),
                    cron_unit=(payload.get("cron_service_name") or "-"),
                    stopped=payload["stopped"],
                    failed=payload["stop_failed"],
                    managed=payload["managed_running"],
                    tracked=payload["tracked_running"],
                    external=payload["external_running"],
                    total=payload["mautic_console_total"],
                )
            )
        _push_state_after_change(cfg, "maintenance-on")
        return 0 if bool(cron_stop.get("ok", True)) else 1

    if args.cmd == "time-check":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)

        os_now = datetime.now().astimezone()
        utc_now = datetime.now(timezone.utc)
        print(f"os_now={os_now.strftime('%Y-%m-%d %H:%M:%S %z')} tz={os_now.tzname()}")
        print(f"daemon_utc_now={utc_now.strftime('%Y-%m-%d %H:%M:%S %z')}")
        print("")

        rows = inv.list_instances()
        if not rows:
            print("No instances")
            return 0
        for inst in rows:
            print(f"[{inst.name}] root={inst.root}")
            print(f"  mautic_timezone={inst.mautic_timezone or '-'}")
            if not inst.db:
                print("  db=not configured")
                print("")
                continue
            db = MauticDB(inst.db)
            try:
                out = db.fetch_rows(
                    "SELECT @@global.time_zone AS global_tz, @@session.time_zone AS session_tz, "
                    "@@system_time_zone AS system_tz, NOW() AS mysql_now, UTC_TIMESTAMP() AS mysql_utc",
                    limit=1,
                )
                row = out[0] if out else {}
                print(
                    "  mysql_tz: global={g} session={s} system={sys}".format(
                        g=row.get("global_tz", "-"),
                        s=row.get("session_tz", "-"),
                        sys=row.get("system_tz", "-"),
                    )
                )
                print(f"  mysql_now={row.get('mysql_now', '-')}")
                print(f"  mysql_utc={row.get('mysql_utc', '-')}")
            except Exception as e:
                print(f"  mysql_error={e}")
            print("")
        return 0

    if args.cmd == "signals":
        cfg = load_config(args.config)
        payload = collect_signals(window_min=int(args.window_min), cfg=cfg)
        if args.json:
            print(format_signals_json(payload))
        else:
            print(format_signals_text(payload))
        return 0

    if args.cmd == "instance-migrate":
        cfg = load_config(args.config)
        if args.op == "source-stream-files":
            return stream_source_files(cfg, selector=args.root, output=sys.stdout.buffer)
        if args.op == "source-stream-db":
            return stream_source_db(cfg, selector=args.root, output=sys.stdout.buffer)
        if args.op == "source-stream-letsencrypt":
            with source_operation_lease(cfg):
                return stream_source_letsencrypt(domains_json=str(args.domains_json or "[]"), output=sys.stdout.buffer)
        if args.op == "target-preflight":
            missing = [
                name
                for name, value in {
                    "target-root": args.target_root,
                    "target-db-name": args.target_db_name,
                }.items()
                if not str(value or "").strip()
            ]
            if missing:
                raise RuntimeError("missing required target-preflight arguments: " + ", ".join(missing))
            with source_operation_lease(cfg):
                payload = preflight_target_relay(
                    target_root=str(args.target_root or "").strip(),
                    target_db_name=str(args.target_db_name or "").strip(),
                    wipe_target_root=bool(args.wipe_target),
                    wipe_target_db=bool(args.wipe_target_db),
                    runtime_adapter=str(args.runtime_adapter or "").strip() or None,
                    runtime=str(args.runtime or "").strip() or None,
                    install_type=str(args.install_type or "").strip() or None,
                    image_ref=str(args.image_ref or "").strip() or None,
                    domains_json=str(args.domains_json or "[]"),
                )
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) else 1
        if args.op == "target-receive-files":
            if not str(args.target_root or "").strip():
                raise RuntimeError("missing required target-receive-files argument: target-root")
            with source_operation_lease(cfg):
                payload = receive_target_files(
                    target_root=str(args.target_root or "").strip(),
                    input_stream=sys.stdin.buffer,
                    wipe_target=bool(args.wipe_target),
                )
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) else 1
        if args.op == "target-receive-letsencrypt":
            with source_operation_lease(cfg):
                payload = receive_target_letsencrypt(input_stream=sys.stdin.buffer)
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) else 1
        if args.op == "target-import-db":
            target_db_password = str(args.target_db_password or os.environ.get("MCD_MIGRATION_TARGET_DB_PASSWORD", ""))
            missing = [
                name
                for name, value in {
                    "target-db-name": args.target_db_name,
                    "target-db-user": args.target_db_user,
                    "target-db-password": target_db_password,
                }.items()
                if not str(value or "").strip()
            ]
            if missing:
                raise RuntimeError("missing required target-import-db arguments: " + ", ".join(missing))
            with source_operation_lease(cfg):
                payload = import_target_db_stream(
                    target_db_name=str(args.target_db_name or "").strip(),
                    target_db_user=str(args.target_db_user or "").strip(),
                    target_db_password=target_db_password,
                    input_stream=sys.stdin.buffer,
                )
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) else 1
        if args.op == "target-finalize":
            target_db_password = str(args.target_db_password or os.environ.get("MCD_MIGRATION_TARGET_DB_PASSWORD", ""))
            missing = [
                name
                for name, value in {
                    "target-root": args.target_root,
                    "target-db-name": args.target_db_name,
                    "target-db-user": args.target_db_user,
                    "target-db-password": target_db_password,
                }.items()
                if not str(value or "").strip()
            ]
            if missing:
                raise RuntimeError("missing required target-finalize arguments: " + ", ".join(missing))
            payload = finalize_target_relay(
                cfg,
                target_root=str(args.target_root or "").strip(),
                target_db_name=str(args.target_db_name or "").strip(),
                target_db_user=str(args.target_db_user or "").strip(),
                target_db_password=target_db_password,
                domains_json=str(args.domains_json or "[]"),
                php_version=str(args.php_version or "").strip() or None,
                runtime_adapter=str(args.runtime_adapter or "").strip() or None,
                runtime=str(args.runtime or "").strip() or None,
                image_ref=str(args.image_ref or "").strip() or None,
                install_type=str(args.install_type or "").strip() or None,
            )
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) and bool(payload.get("catchup_ok", False)) else 1
        if args.op == "target-pull":
            target_db_password = str(args.target_db_password or os.environ.get("MCD_MIGRATION_TARGET_DB_PASSWORD", ""))
            missing = [
                name
                for name, value in {
                    "source-address": args.source_address,
                    "source-ssh-key-file": args.source_ssh_key_file,
                    "source-root": args.source_root,
                    "target-root": args.target_root,
                }.items()
                if not str(value or "").strip()
            ]
            if missing:
                raise RuntimeError("missing required target-pull arguments: " + ", ".join(missing))
            payload = run_target_pull_migration(
                cfg,
                source_address=str(args.source_address or "").strip(),
                source_ssh_user=str(args.source_ssh_user or "root").strip() or "root",
                source_ssh_port=int(args.source_ssh_port or 22),
                source_ssh_key_file=str(args.source_ssh_key_file or "").strip(),
                source_root=str(args.source_root or "").strip(),
                target_root=str(args.target_root or "").strip(),
                domains_json=str(args.domains_json or "[]"),
                target_db_name=str(args.target_db_name or "").strip() or None,
                target_db_user=str(args.target_db_user or "").strip() or None,
                target_db_password=target_db_password.strip() or None,
                php_version=str(args.php_version or "").strip() or None,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0 if bool(payload.get("ok", False)) and bool(payload.get("catchup_ok", False)) else 1
        payload = collect_source_probe(cfg, selector=args.root)
        if args.json:
            print(format_source_probe_json(payload))
        else:
            print(format_source_probe_text(payload))
        return 0 if bool(payload.get("ok", False)) else 1

    if args.cmd == "instance-runtime":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note and not bool(getattr(args, "json", False)):
            print(f"NOTICE: {note}")
        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)
        installs = inv.list_instances()
        dry_run = bool(args.dry_run) or str(args.op) == "status"
        payload = apply_instance_runtime(
            installs,
            root=args.root,
            dry_run=dry_run,
            reload_services=not bool(args.no_reload),
        )
        if bool(args.json):
            print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
        else:
            print(
                "instance runtime: status={status} changed={changed} dry_run={dry_run}".format(
                    status=str(payload.get("status", "unknown")),
                    changed=1 if bool(payload.get("changed")) else 0,
                    dry_run=1 if bool(payload.get("dry_run")) else 0,
                )
            )
            for row in payload.get("instances", []) if isinstance(payload.get("instances"), list) else []:
                nginx_files = row.get("nginx_files", [])
                php_versions = row.get("php_versions", [])
                print(
                    "root={root} status={status} timezone={timezone} php={php} files={files}{reason}".format(
                        root=str(row.get("root") or ""),
                        status=str(row.get("status") or ""),
                        timezone=str(row.get("timezone") or ""),
                        php=",".join(str(x) for x in php_versions if x),
                        files=len(nginx_files) if isinstance(nginx_files, list) else 0,
                        reason=(" reason=" + str(row.get("reason") or "")) if row.get("reason") else "",
                    )
                )
            if payload.get("reason"):
                print("reason=" + str(payload.get("reason")))
        if str(payload.get("status")) == "ok" and not dry_run:
            _push_state_after_change(cfg, "instance-runtime")
        return 0 if str(payload.get("status")) == "ok" else 1

    if args.cmd == "self-update":
        cfg = load_config(args.config)
        if args.op == "status":
            st = update_status(cfg)
            if args.json:
                print(json.dumps(st, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(st, ensure_ascii=True, indent=2))
            return 0
        if args.op == "check":
            decision = check_with_mcc(cfg, auto_update_enabled=False)
            if args.json:
                print(json.dumps(decision, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(decision, ensure_ascii=True, indent=2))
            return 0
        # apply
        if not args.yes:
            ans = _ask("Apply self-update now (if available)? [y/N]: ").strip().lower()
            if ans not in {"y", "yes"}:
                print("Cancelled")
                return 1
        decision = check_with_mcc(cfg, auto_update_enabled=True)
        st = str(decision.get("status", "")).strip().lower()
        if st == "wait":
            print("MCC update slots are busy; retry in 60s")
            return 2
        if st in {"up_to_date", "disabled"}:
            msg, _retry = maybe_auto_update(cfg, force=True)
            if msg:
                print(msg)
                return 0
            print(json.dumps(decision, ensure_ascii=True))
            return 0
        if st not in {"update", "update_available"}:
            print(json.dumps(decision, ensure_ascii=True))
            return 1
        ok, msg = apply_update(cfg, decision)
        print(msg)
        if (not ok) and "deferred" in str(msg).strip().lower():
            return 2
        return 0 if ok else 1

    if args.cmd == "mautic-patch-context":
        from mcd_agent.mautic_patch_context import preflight, error
        try:
            result = preflight(args.config, args.root, args.instance_uid)
        except ValueError as exc:
            print(json.dumps(error(str(exc)), ensure_ascii=True))
            return 2
        print(json.dumps(result, ensure_ascii=True))
        return 0

    if args.cmd == "mautic-release-contract":
        from mcd_agent.mautic_release_authorization import contract
        print(json.dumps(contract(), ensure_ascii=True))
        return 0

    if args.cmd == "mautic-patch-backup-authorize":
        if args.plan_file:
            if args.plan_json:
                raise RuntimeError("Choose exactly one patch plan transport")
            from mcd_agent.mautic_patch_resolution import read_plan_file
            args.plan_json = read_plan_file(args.plan_file, args.plan_sha256)
        from mcd_agent.mautic_patch_backup import issue
        result = issue(plan=json.loads(args.plan_json), instance_uid=args.instance_uid,
                       root=args.root, backup_evidence=json.loads(args.backup_evidence_json),
                       key_path=args.signing_key_file, output_path=args.output_file)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0

    if args.cmd == "mautic-patch-plan":
        if args.plan_file:
            if args.plan_json:
                raise RuntimeError("Choose exactly one patch plan transport")
            from mcd_agent.mautic_patch_resolution import read_plan_file
            args.plan_json = read_plan_file(args.plan_file, args.plan_sha256)
        from mcd_agent.mautic_patch_plan import PatchPlanError, contract, execute, rollback
        if args.op == "contract":
            print(json.dumps(contract(), ensure_ascii=True, indent=2))
            return 0
        try:
            from mcd_agent.mautic_patch_plan import parse_plan
            from mcd_agent.mautic_patch_fact_binding import needs_facts, bound_provider
            plan = parse_plan(args.plan_json)
            options = {}
            if needs_facts(plan):
                provider = bound_provider(load_config(args.config), args.root or "")
                options["facts_provider"] = provider
                if args.op == "apply":
                    admission = execute(args.root or "", json.dumps(dict(plan, operation="verify")), args.phase, args.run_id, "verify", **options)
                    options["accepted_facts"] = admission["facts_receipt"]
            result = rollback(args.root or "", args.plan_json, args.run_id, **options) if args.op == "rollback" else execute(args.root or "", args.plan_json, args.phase, args.run_id, args.op, **options)
        except PatchPlanError as exc:
            result = {"status": "error", "reason": str(exc)}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("status") == "success" else 2

    if args.cmd == "mautic6-patch":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if args.op == "policy":
            if not args.policy:
                raise RuntimeError("--policy is required for op=policy")
            target_cfg = _write_runtime_patch_policy(args.config, args.policy)
            out = {
                "status": "ok",
                "policy": args.policy,
                "config_path": target_cfg,
            }
            proc = subprocess.run(["systemctl", "restart", "mcd"], capture_output=True, text=True)
            out["service_restart_ok"] = proc.returncode == 0
            if proc.returncode != 0:
                out["service_restart_error"] = (proc.stderr or proc.stdout or "").strip()
            if args.json:
                print(json.dumps(out, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(out, ensure_ascii=True))
            _push_state_after_change(cfg, "mautic6-core-patch-policy")
            return 0 if proc.returncode == 0 else 1

        installs = _select_installs_for_patch(cfg, args.root)
        if not installs:
            raise RuntimeError("No matching instances")
        payload = []
        rc = 0
        for inst in installs:
            if args.op == "status":
                res = mautic6_patch_status(inst, cfg)
            elif args.op == "apply":
                res = ensure_m6_plugin_update_metadata_patch(inst, cfg, trigger="operator_action")
            elif args.op == "revert":
                res = revert_m6_plugin_update_metadata_patch(inst, cfg, run_id=args.run_id or None)
            else:
                raise RuntimeError(f"unsupported op: {args.op}")
            payload.append({"root": inst.root, "instance_uid": inst.instance_uid, "result": res})
            st = str(res.get("status", "")).strip().lower()
            if st == "error":
                rc = 1

        if args.json:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            for row in payload:
                print(json.dumps(row, ensure_ascii=True))
        if args.op == "apply":
            _push_state_after_change(cfg, "mautic6-core-patch-apply")
        if args.op == "revert":
            _push_state_after_change(cfg, "mautic6-core-patch-revert")
        return rc

    if args.cmd == "mautic713-import-tag-patch":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        installs = _select_installs_for_patch(cfg, args.root)
        if not installs:
            raise RuntimeError("No matching instances")
        payload = []
        rc = 0
        for inst in installs:
            if args.op == "status":
                res = mautic713_import_tag_patch_status(inst, cfg)
            elif args.op == "apply":
                res = ensure_mautic713_import_tag_patch(inst, cfg, trigger="operator_action")
            elif args.op == "revert":
                res = revert_mautic713_import_tag_patch(inst, cfg, run_id=args.run_id or None)
            else:
                raise RuntimeError(f"unsupported op: {args.op}")
            payload.append({"root": inst.root, "instance_uid": inst.instance_uid, "result": res})
            if str(res.get("status", "")).strip().lower() == "error":
                rc = 1
        if args.json:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        else:
            for row in payload:
                print(json.dumps(row, ensure_ascii=True))
        if args.op in {"apply", "revert"}:
            _push_state_after_change(cfg, f"mautic713-import-tag-patch-{args.op}")
        return rc

    if args.cmd == "profile":
        cfg = load_config(args.config)
        old_profile_name = (cfg.profile_name or "").strip().lower() or None
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if args.op == "status":
            res = profile_status(
                pause_flag_path=cfg.scheduler_pause_flag_path,
                config_path=args.config,
            )
            selection_path = profile_selection_path("/opt/mcd")
            selection = read_profile_selection("/opt/mcd")
            cpus = max(1, int(os.cpu_count() or 1))
            memory_kib = read_memory_kib()
            status_payload = {
                "schema": "mcd-profile-status-v1",
                "status": "ok" if res.ok else "error",
                "profile": str((cfg.profile_name or "")).strip().lower(),
                "hardware_profile": str(selection.get("profile") or ""),
                "config_path": str(args.config),
                "config_mutable_path": str(_resolve_mutable_config_path(args.config)),
                "selection_path": str(selection_path),
                "selection_exists": selection_path.exists(),
                "selection": selection,
                "selection_mode": str(selection.get("mode") or "auto"),
                "hardware_recommendation": recommended_profile(cpu_count=cpus, memory_kib=memory_kib),
                "hardware_cpu_count": cpus,
                "hardware_memory_kib": memory_kib,
            }
            if args.json:
                print(json.dumps(status_payload, ensure_ascii=True, indent=2))
            else:
                for line in res.lines:
                    print(line)
                print(f"selection_path={selection_path}")
                print(f"selection_exists={str(selection_path.exists()).lower()}")
                print(f"selection_mode={status_payload['selection_mode']}")
                print(f"hardware_recommendation={status_payload['hardware_recommendation']}")
                print(f"hardware_cpu_count={cpus}")
                print(f"hardware_memory_kib={memory_kib}")
            return 0
        if args.op == "auto":
            write_profile_selection(
                "/opt/mcd",
                mode="auto",
                profile=str(getattr(cfg, "hardware_profile_name", "auto") or "auto"),
            )
            result = reconcile_hardware_profile(
                config_path=args.config,
                install_dir="/opt/mcd",
            )
            cfg_after = load_config(args.config)
            if result.changed:
                proc = subprocess.run(
                    ["systemctl", "restart", "mcd"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode != 0:
                    error = (proc.stderr or proc.stdout or "unknown error").strip()
                    print(f"profile auto: service restart failed: {error}")
                    return 1
            _push_state_after_change(cfg_after, "hardware-profile-auto")
            print(
                f"profile auto: selected={result.profile} changed={str(result.changed).lower()} "
                f"cpu={result.cpu_count} memory_kib={result.memory_kib}"
            )
            return 0
        if not args.yes:
            ans = _ask(f"Switch profile to {args.op}? [y/N]: ").strip().lower()
            if ans not in {"y", "yes"}:
                print("Cancelled")
                return 1
        res = profile_set(
            profile=args.op,
            pause_flag_path=cfg.scheduler_pause_flag_path,
            install_dir="/opt/mcd",
            config_path=args.config,
        )
        for line in res.lines:
            print(line)
        if res.ok:
            cfg_after = load_config(args.config)
            new_profile_name = (cfg_after.profile_name or "").strip().lower() or None
            try:
                queue_profile_event(
                    cfg_after,
                    source="mcd_cli",
                    initiated_by_user=True,
                    old_profile=old_profile_name,
                    new_profile=new_profile_name,
                    reason="profile_set",
                    details={"command": f"profile {args.op}"},
                )
            except Exception as e:
                logging.warning("profile-set event enqueue failed: %s", e)
            _push_state_after_change(cfg_after, "profile-set")
        return 0 if res.ok else 1

    if args.cmd == "uninstall":
        if not args.yes:
            ans = _ask("This will remove MCD and restore previous crontab. Continue? [y/N]: ").strip().lower()
            if ans not in {"y", "yes"}:
                print("Cancelled")
                return 1
        res = run_uninstall(
            service_name=args.service_name,
            install_dir=args.install_dir,
            etc_dir=args.etc_dir,
            purge=not bool(args.no_purge),
        )
        for line in res.lines:
            print(line)
        return 0 if res.ok else 1

    if args.cmd == "backup":
        cfg = load_config(args.config)
        if args.no_prune and args.op != "run":
            parser.error("--no-prune is supported only with `backup run`")
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}")
        if args.op == "cluster-status":
            st = cluster_backup_status(cfg)
            if args.json:
                print(json.dumps(st, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(st, ensure_ascii=True, indent=2))
            return 0
        if args.op == "cluster-retention-plan":
            plan = cluster_backup_retention_plan(cfg, apply=False)
            print(json.dumps(plan, ensure_ascii=True, indent=2))
            return 1 if plan.get("problems") else 0
        if args.op in {
            "cluster-local-full",
            "cluster-incremental",
            "cluster-files",
            "cluster-files-produce",
            "cluster-files-assemble",
            "cluster-offsite",
            "cluster-offsite-dry-run",
        }:
            if args.op == "cluster-local-full":
                res = cluster_backup_local_full(cfg)
                event = "backup-cluster-local-full"
            elif args.op == "cluster-incremental":
                res = cluster_backup_local_incremental(cfg)
                event = "backup-cluster-incremental"
            elif args.op == "cluster-files-produce":
                res = cluster_backup_files_produce(cfg)
                event = "backup-cluster-files-produce"
            elif args.op == "cluster-files-assemble":
                res = cluster_backup_files_assemble(cfg)
                event = "backup-cluster-files-assemble"
            elif args.op == "cluster-files":
                res = cluster_backup_files_snapshot(cfg)
                event = "backup-cluster-files"
            elif args.op == "cluster-offsite-dry-run":
                dry = cluster_backup_offsite_dry_run(cfg)
                print(json.dumps(dry, ensure_ascii=True, indent=2))
                return 0 if dry.get("ok") else 1
            else:
                res = cluster_backup_offsite(cfg)
                event = "backup-cluster-offsite"
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                            "backup_path": res.backup_path,
                            "duration_sec": res.duration_sec,
                            "bytes_written": res.bytes_written,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
                if res.backup_path:
                    print(f"path={res.backup_path}")
            _push_state_after_change(cfg, event)
            return 0 if res.ok else 1
        if args.op in {"preflight", "dry-run"}:
            res = backup_preflight(cfg, args.root)
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                            "duration_sec": res.duration_sec,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
            _push_state_after_change(cfg, "backup-preflight")
            return 0 if res.ok else 1
        if args.op == "run":
            res = backup_run(cfg, args.root, prune_retention=not args.no_prune)
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                            "backup_path": res.backup_path,
                            "duration_sec": res.duration_sec,
                            "bytes_written": res.bytes_written,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
                if res.backup_path:
                    print(f"path={res.backup_path}")
            _push_state_after_change(cfg, "backup-run")
            return 0 if res.ok else 1
        if args.op == "instance-run":
            res = backup_instance_run(cfg, args.root, remote_root_dir=args.remote_root_dir)
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                            "backup_path": res.backup_path,
                            "duration_sec": res.duration_sec,
                            "bytes_written": res.bytes_written,
                            "backup_id": res.backup_id,
                            "manifest_path": res.manifest_path,
                            "authorization_manifest_path": res.authorization_manifest_path,
                            "sha256": res.sha256,
                            "completed_at": res.completed_at,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
                if res.backup_path:
                    print(f"path={res.backup_path}")
            _push_state_after_change(cfg, "backup-instance-run")
            return 0 if res.ok else 1
        if args.op == "prune":
            res = backup_prune(cfg, args.root)
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
            _push_state_after_change(cfg, "backup-prune")
            return 0 if res.ok else 1
        if args.op == "restore":
            res = backup_restore(cfg, root=args.root, date=args.date, path=args.path)
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": res.ok,
                            "message": res.message,
                            "state_path": res.state_path,
                            "backup_path": res.backup_path,
                            "duration_sec": res.duration_sec,
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                print(res.message)
                print(f"state={res.state_path}")
                if res.backup_path:
                    print(f"path={res.backup_path}")
            _push_state_after_change(cfg, "backup-restore")
            return 0 if res.ok else 1
        if args.op == "profile-show":
            masked = backup_profile_masked(cfg)
            print(json.dumps(masked, ensure_ascii=True, indent=2))
            return 0
        if args.op == "profile-set":
            raw = ""
            if args.profile_json_stdin:
                raw = sys.stdin.read()
            elif args.profile_json_file:
                raw = Path(args.profile_json_file).read_text(encoding="utf-8")
            else:
                raise RuntimeError("profile-set requires --profile-json-file or --profile-json-stdin")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise RuntimeError("backup profile payload must be a JSON object")
            if args.contract:
                if args.contract != "mcd-backup-profile-v1":
                    raise RuntimeError(f"unsupported backup profile contract: {args.contract}")
                _, receipt = backup_profile_apply_sync(cfg, payload)
                print(json.dumps(receipt, ensure_ascii=True, indent=2))
                if receipt.get("status") == "ok":
                    cfg_after = load_config(args.config, allow_recover_from_mcc=False)
                    _push_state_after_change(cfg_after, "backup-profile-set-contract")
                return 0 if receipt.get("status") == "ok" else 1
            result = backup_profile_set(
                cfg,
                payload,
                merge=not bool(args.replace),
                prepare_check=not bool(args.skip_prepare_check),
            )
            masked = backup_profile_masked(cfg)
            if isinstance(result.get("_prepare_check"), dict):
                masked["_prepare_check"] = result["_prepare_check"]
            print(json.dumps(masked, ensure_ascii=True, indent=2))
            cfg_after = load_config(args.config, allow_recover_from_mcc=False)
            _push_state_after_change(cfg_after, "backup-profile-set")
            return 0
        st = backup_status(cfg, args.root)
        if args.op == "history":
            history = st.get("history", [])
            if args.json:
                print(json.dumps(history, ensure_ascii=True, indent=2))
            else:
                if not history:
                    print("No backup history")
                else:
                    for row in history:
                        print(json.dumps(row, ensure_ascii=True))
            return 0
        if args.json:
            print(json.dumps(st, ensure_ascii=True, indent=2))
        else:
            print(json.dumps(st, ensure_ascii=True, indent=2))
        return 0

    if args.cmd == "custom":
        cfg = load_config(args.config)
        note = maybe_notify_update(cfg)
        if note:
            print(f"NOTICE: {note}", file=sys.stderr if args.json else sys.stdout)
        if bool(args.list):
            rows, source = fetch_custom_manifest(cfg, use_cache_on_error=True)
            if bool(args.json):
                payload = {"source": source, "scripts": rows}
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Manifest source: {source}")
                print(format_custom_scripts_list(rows, with_idx=True))
            return 0
        if not args.script:
            return _run_custom_menu(cfg)
        script_args = list(args.script_args or [])
        if script_args and script_args[0] == "--":
            script_args = script_args[1:]
        rc, out = run_custom_script_by_key(
            cfg,
            script_key=str(args.script),
            args=script_args,
            detach=args.detach,
            live_output=not bool(args.detach),
            expected_sha256=args.expected_sha256,
        )
        if out:
            print(out)
        return rc

    parser.print_help()
    return 1
