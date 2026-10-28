from __future__ import annotations

from uuid import uuid4

import json
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tarfile
from typing import Any
from dataclasses import dataclass, replace
from urllib import error as urlerror
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit
import urllib.request

from mcd_agent.config import AgentConfig
from mcd_agent.source_operation import stable_source_operation
from mcd_agent.discovery import discover_mautic
from mcd_agent.install_type import detect_install_type
from mcd_agent.maintenance_mode import (
    cron_marker_path,
    restore_cron_service_if_needed,
    scheduler_dispatch_fence,
    stop_cron_service,
    stop_running_tasks_for_maintenance,
)
from mcd_agent.models import MauticInstall
from mcd_agent.amazon_mailer_dep import (
    ensure_amazon_mailer_for_bundles,
    ensure_mailer_packages_for_sender_config,
    installed_required_bundles,
    _ensure_node20,
    _resolve_composer_bin,
)
from mcd_agent.assetmapper_verification import verify_assetmapper_upgrade
from mcd_agent.fs_permissions import ensure_instance_permissions
from mcd_agent.localphp import parse_local_php
from mcd_agent.mautic_version_cache import (
    read_mautic_version_evidence_read_only,
    read_mautic_version_read_only,
    write_mautic_version_cache,
)
from mcd_agent.mautic713_import_tag_patch import (
    ensure_patch as ensure_import_tag_patch,
    revert_patch as revert_mautic713_import_tag_patch,
)
from mcd_agent.executor import execute_mautic_command_template
from mcd_agent.plugins import run_plugins_interactive
from mcd_agent.install_readiness import _database_state, mautic7_database_compatibility
from mcd_agent.mautic_upgrade_contract import (
    composer_readiness,
    composer_readiness_from_observation,
    inspect_json_schema_repair,
    rebind_php_after_system_upgrade,
)


CORE_PLUGIN_BUNDLES = {
    "GrapesJsBuilderBundle",
    "MauticClearbitBundle",
    "MauticCloudStorageBundle",
    "MauticCrmBundle",
    "MauticEmailMarketingBundle",
    "MauticFocusBundle",
    "MauticFullContactBundle",
    "MauticGmailBundle",
    "MauticOutlookBundle",
    "MauticSocialBundle",
    "MauticTagManagerBundle",
    "MauticZapierBundle",
}

FALLBACK_BRANCH_TARGETS: dict[str, str] = {
    "4.4": "4.4.13",
    "5.1": "5.1.1",
    "5.2": "5.2.9",
    "6.0": "6.0.7",
    "7.1": "7.1.3",
}

PHP84_PACKAGE_SUFFIXES = [
    "apcu",
    "fpm",
    "cli",
    "curl",
    "mysql",
    "mailparse",
    "gd",
    "mbstring",
    "imagick",
    "bcmath",
    "zip",
    "tidy",
    "soap",
    "intl",
    "xml",
    "imap",
    "redis",
    "opcache",
]

PHP_CUSTOM_INI_NAMES = {"60-custom.ini", "90-redis-sessions.ini"}
PHP_CUSTOM_INI_HINTS = ("custom", "mcd", "sales-snap", "redis-session", "redis_sessions")
MAUTIC7_LOCALE_FIX_BUNDLE = "MauticLocaleFixBundle"


@dataclass(slots=True)
class UpgradeProbeResult:
    ok: bool
    summary: str
    detail: str = ""


@dataclass(slots=True)
class UpgradeMaintenanceGuard:
    pause_flag: Path
    owned_pause_flag: bool
    owned_cron_stop: bool
    stopped_tasks: int = 0
    stop_failed: int = 0
    dispatch_fence: Any = None


def _emit_maintenance_guard_failure(
    *,
    reason: str,
    stop_result: dict[str, Any] | None = None,
    cleanup: dict[str, Any] | None = None,
    error_type: str = "",
) -> None:
    result = stop_result if isinstance(stop_result, dict) else {}
    evidence = {
        "schema": "mcd-maintenance-guard-evidence-v1",
        "contract_version": 1,
        "status": "failed",
        "reason": str(reason),
        "error_type": str(error_type or ""),
        "tracked_running": int(result.get("tracked_running") or 0),
        "external_running": int(result.get("external_running") or 0),
        "managed_running": int(result.get("managed_running") or 0),
        "mautic_console_total": int(result.get("mautic_console_total") or 0),
        "stop_failed": int(result.get("stop_failed") or 0),
        "stopped": int(result.get("stopped") or 0),
        "stable_snapshots": int(result.get("stable_snapshots") or 0),
        "stable_snapshots_required": int(result.get("stable_snapshots_required") or 0),
        "drain_attempts": int(result.get("drain_attempts") or 0),
        "drain_timed_out": bool(result.get("drain_timed_out", False)),
        "tracked_tasks": list(result.get("tracked_tasks") or [])[:100],
        "external_tasks": list(result.get("external_tasks") or [])[:100],
        "released_stale_tasks": list(result.get("released_stale_tasks") or [])[:100],
        "cleanup": dict(cleanup or {}),
    }
    print(
        "MCD_MAINTENANCE_GUARD_EVIDENCE="
        + json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _pick_install_record(config: AgentConfig, root: str | None) -> MauticInstall:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst
        raise RuntimeError(f"Mautic install not found: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        raise RuntimeError("Multiple installs found, pass --root")
    return installs[0]


def _pick_install(config: AgentConfig, root: str | None) -> tuple[str, str]:
    inst = _pick_install_record(config, root)
    return inst.root, inst.console_path


def _branch_key(version: str) -> str:
    sv = _parse_semver(version)
    if sv == (0, 0, 0):
        return ""
    return f"{sv[0]}.{sv[1]}"


def _release_family_label(version: str) -> str:
    sv = _parse_semver(version)
    if sv == (0, 0, 0):
        return ""
    if sv[0] == 7:
        return "7"
    return f"{sv[0]}.{sv[1]}.x"


def _write_upgrade_version_cache(root: str, version: str) -> int:
    safe = str(version or "").strip()
    if not safe or safe == "0.0.0":
        return 0
    root_path = Path(root)
    candidates = [root_path]
    if root_path.name.lower() in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    candidates.append(root_path.parent.parent)
    written = 0
    seen: set[str] = set()
    for base in candidates:
        key = str(base)
        if key in seen or key == "/" or not base.exists() or not base.is_dir():
            continue
        seen.add(key)
        try:
            if write_mautic_version_cache(base, safe):
                written += 1
        except OSError:
            continue
    return written


def _scheduler_pause_flag(config: AgentConfig) -> Path:
    raw = str(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "").strip()
    path = Path(raw or "/opt/mcd/var/scheduler.pause")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _enter_upgrade_maintenance(config: AgentConfig) -> UpgradeMaintenanceGuard:
    fence = scheduler_dispatch_fence(config, exclusive=True, blocking=True)
    fence.__enter__()
    try:
        return _enter_upgrade_maintenance_locked(config, fence)
    except BaseException:
        fence.__exit__(*sys.exc_info())
        raise


def _enter_upgrade_maintenance_locked(config: AgentConfig, fence: Any) -> UpgradeMaintenanceGuard:
    pause_flag = _scheduler_pause_flag(config)
    owned_pause_flag = not pause_flag.exists()
    if owned_pause_flag:
        pause_flag.write_text("paused\n", encoding="utf-8")
        print(f"Maintenance guard: scheduler paused ({pause_flag})")
    else:
        print(f"Maintenance guard: scheduler already paused ({pause_flag})")

    owned_cron_stop = False
    marker = cron_marker_path(config)
    if marker.exists():
        print(f"Maintenance guard: cron stop marker already present ({marker}); leaving ownership unchanged")
    else:
        cron_stop = stop_cron_service(config)
        if not bool(cron_stop.get("ok", False)):
            pause_removed = False
            if owned_pause_flag and pause_flag.exists():
                try:
                    pause_flag.unlink()
                    pause_removed = not pause_flag.exists()
                except OSError:
                    pause_removed = False
            message = str(cron_stop.get("message", "") or "").strip() or "cron service stop failed"
            unit = str(cron_stop.get("unit", "") or "").strip() or "-"
            _emit_maintenance_guard_failure(
                reason="cron_stop_failed",
                cleanup={
                    "cron_restore": {"requested": False, "reason": "cron_was_not_stopped"},
                    "pause_owned": bool(owned_pause_flag),
                    "pause_removed": bool(pause_removed),
                    "dispatch_fence": "held_until_failure_evidence_emitted",
                },
            )
            raise RuntimeError(f"Maintenance guard failed: unable to stop cron unit {unit}: {message}")
        owned_cron_stop = True
        unit = str(cron_stop.get("unit", "") or "").strip() or "-"
        was_active = str(bool(cron_stop.get("was_active", False))).lower()
        print(f"Maintenance guard: cron stopped unit={unit} was_active={was_active}")

    try:
        stop_result = stop_running_tasks_for_maintenance(config, grace_sec=30, kill_orphans=True)
    except Exception as exc:
        pause_removed = False
        if owned_pause_flag and pause_flag.exists():
            try:
                pause_flag.unlink()
                pause_removed = not pause_flag.exists()
            except OSError:
                pause_removed = False
        _emit_maintenance_guard_failure(
            reason="drain_observation_failed",
            error_type=type(exc).__name__,
            cleanup={
                "cron_restore": {"requested": False, "reason": "drain_observation_failed"},
                "pause_owned": bool(owned_pause_flag),
                "pause_removed": bool(pause_removed),
                "dispatch_fence": "held_until_failure_evidence_emitted",
            },
        )
        raise RuntimeError("Maintenance guard failed: drain observation failed") from exc
    stopped_tasks = int(stop_result.get("stopped") or 0)
    stop_failed = int(stop_result.get("stop_failed") or 0)
    print(
        "Maintenance guard: running tasks stopped={stopped} failed={failed} "
        "managed_remaining={managed} console_remaining={consoles}".format(
            stopped=stopped_tasks,
            failed=stop_failed,
            managed=int(stop_result.get("managed_running") or 0),
            consoles=int(stop_result.get("mautic_console_total") or 0),
        )
    )
    # A stop attempt can race with a short-lived console process: the kill
    # helper may report a failure even though the final process snapshot is
    # already clean. Only live processes make the maintenance guard unsafe.
    managed_remaining = int(stop_result.get("managed_running") or 0)
    console_remaining = int(stop_result.get("mautic_console_total") or 0)
    if managed_remaining > 0 or console_remaining > 0:
        cron_restore: dict[str, Any] = {"ok": True, "requested": False, "started": False}
        if owned_cron_stop:
            try:
                cron_restore = restore_cron_service_if_needed(config)
            except Exception as exc:
                cron_restore = {
                    "ok": False,
                    "requested": True,
                    "started": False,
                    "error_type": type(exc).__name__,
                }
        pause_removed = False
        if owned_pause_flag and pause_flag.exists():
            try:
                pause_flag.unlink()
                pause_removed = not pause_flag.exists()
            except OSError:
                pause_removed = False
        _emit_maintenance_guard_failure(
            reason="tasks_not_quiescent",
            stop_result=stop_result,
            cleanup={
                "cron_restore": cron_restore,
                "pause_owned": bool(owned_pause_flag),
                "pause_removed": bool(pause_removed),
                "dispatch_fence": "held_until_failure_evidence_emitted",
            },
        )
        raise RuntimeError(
            "Maintenance guard failed: unable to stop all running Mautic tasks "
            f"(stop_failed={stop_failed} managed_remaining={managed_remaining} "
            f"console_remaining={console_remaining})"
        )

    return UpgradeMaintenanceGuard(
        pause_flag=pause_flag,
        owned_pause_flag=owned_pause_flag,
        owned_cron_stop=owned_cron_stop,
        stopped_tasks=stopped_tasks,
        stop_failed=stop_failed,
        dispatch_fence=fence,
    )


def _exit_upgrade_maintenance(config: AgentConfig, guard: UpgradeMaintenanceGuard) -> None:
    try:
        if guard.owned_cron_stop:
            cron_restore = restore_cron_service_if_needed(config)
            if not bool(cron_restore.get("ok", False)):
                message = str(cron_restore.get("message", "") or "").strip() or "cron service restore failed"
                unit = str(cron_restore.get("unit", "") or "").strip() or "-"
                raise RuntimeError(f"Maintenance guard failed: unable to restore cron unit {unit}: {message}")
            unit = str(cron_restore.get("unit", "") or "").strip() or "-"
            started = str(bool(cron_restore.get("started", False))).lower()
            print(f"Maintenance guard: cron restored unit={unit} started={started}")
        else:
            print("Maintenance guard: cron ownership unchanged")

        if guard.owned_pause_flag:
            if guard.pause_flag.exists():
                guard.pause_flag.unlink()
            print(f"Maintenance guard: scheduler resumed ({guard.pause_flag})")
        else:
            print("Maintenance guard: scheduler pause left unchanged")
    finally:
        if guard.dispatch_fence is not None:
            guard.dispatch_fence.__exit__(None, None, None)
            guard.dispatch_fence = None


def _default_zip_url(version: str) -> str:
    return f"https://github.com/mautic/mautic/releases/download/{version}/{version}-update.zip"


def _release_targets_from_mcc(config: AgentConfig) -> dict[str, dict[str, str]]:
    if not config.mcc_url:
        return {}
    url = config.mcc_url.rstrip("/") + "/api/v1/agent/mautic/releases"
    req = urllib.request.Request(url=url, method="GET")
    if config.mcc_token:
        req.add_header("Authorization", f"Bearer {config.mcc_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = (resp.read() or b"").decode("utf-8", errors="replace")
        raw = json.loads(body or "{}")
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, ValueError):
        return {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    branches = raw.get("branches", {})
    if not isinstance(branches, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for branch, row in branches.items():
        if not isinstance(row, dict):
            continue
        bk = str(branch).strip()
        ver = str(row.get("version", "")).strip()
        if not bk or not ver:
            continue
        zip_url = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
        out[bk] = {"version": ver, "zip_url": zip_url}
    return out


def _release_targets_fallback() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for bk, ver in FALLBACK_BRANCH_TARGETS.items():
        out[bk] = {"version": ver, "zip_url": _default_zip_url(ver)}
    return out


def _release_targets(config: AgentConfig) -> dict[str, dict[str, str]]:
    if config.mcc_url:
        return _release_targets_from_mcc(config)
    return _release_targets_fallback()


def _require_release_approval(config: AgentConfig, target: str, source_version: str = "") -> None:
    if not config.mcc_url:
        raise RuntimeError("Mautic upgrade requires a live MCC release approval")
    if not source_version or _branch_key(source_version) != _branch_key(target):
        raise RuntimeError("Cross-line apply requires bound release transition authorization")
    url = config.mcc_url.rstrip("/") + "/api/v1/agent/mautic/releases/authorize?" + urlencode({"version": target, "source_version": source_version})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.mcc_token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict) or result.get("status") != "ok" or result.get("version") != target:
            raise ValueError("invalid approval response")
    except Exception as exc:
        raise RuntimeError(f"Mautic {target} is not authorized by MCC; package replacement is blocked ({type(exc).__name__})") from exc


def _available_targets(config: AgentConfig) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _release_targets(config).values():
        ver = str(row.get("version", "")).strip()
        if not ver:
            continue
        out[ver] = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
    cache_dir = Path("/opt/mcd/cache/updates")
    if cache_dir.exists():
        for p in cache_dir.glob("*-update.zip"):
            m = re.search(r"(\d+\.\d+\.\d+)-update\.zip$", p.name)
            if not m:
                continue
            v = m.group(1)
            if config.mcc_url and v not in out:
                continue
            out[v] = str(p)
    return out


def _parse_semver(raw: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _clean_target_version(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    m = re.match(r"^(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)$", value)
    if not m:
        raise RuntimeError(f"Invalid Mautic target version: {value}")
    return m.group(1)


def _mautic_console_cmd(cmd: list[str], run_as_user: str | None = "www-data") -> list[str]:
    user = str(run_as_user or "").strip()
    if user and user != "root" and hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["sudo", "-H", "-u", user] + cmd
    return cmd


def _read_current_version(root: str, console_path: str, php_bin: str, run_as_user: str | None = "www-data") -> str:
    cmds = [
        [php_bin, console_path, "--version"],
        [php_bin, console_path, "about", "--no-interaction"],
    ]
    for cmd in cmds:
        try:
            proc = subprocess.run(
                _mautic_console_cmd(cmd, run_as_user),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    lock = Path(root) / "composer.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            for pkg in data.get("packages", []):
                if not isinstance(pkg, dict):
                    continue
                if str(pkg.get("name", "")) in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                    v = str(pkg.get("version", ""))
                    m = re.search(r"(\d+\.\d+\.\d+)", v)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return "0.0.0"


def _read_current_version_read_only(root: str) -> str:
    return str(read_mautic_version_read_only(root) or "0.0.0")


def _latest_same_branch(config: AgentConfig, version: str) -> str | None:
    targets = _available_targets(config)
    v = _parse_semver(version)
    if v == (0, 0, 0):
        return None
    candidates = [
        x
        for x in targets.keys()
        if _branch_key(x) == _branch_key(version)
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda x: _parse_semver(x))
    return latest if _parse_semver(latest) > v else None


def _is_supported_major_upgrade(current: str, target: str) -> bool:
    current_sv = _parse_semver(current)
    target_sv = _parse_semver(target)
    return current_sv[0] == 6 and target_sv[0] == 7 and target_sv > current_sv


def _upgrade_target_relation(
    current: str,
    target: str,
    *,
    allow_minor: bool = False,
    allow_major: bool = False,
) -> str:
    current_sv = _parse_semver(current)
    target_sv = _parse_semver(target)
    if current_sv == (0, 0, 0) or target_sv == (0, 0, 0):
        return "invalid"
    if target_sv <= current_sv:
        return "none"
    if target_sv[0] != current_sv[0]:
        if allow_major and _is_supported_major_upgrade(current, target):
            return "allowed"
        return "blocked_major"
    if target_sv[1] == current_sv[1]:
        return "allowed"
    if allow_minor and target_sv[1] > current_sv[1]:
        return "allowed"
    return "blocked_minor"


def _ensure_upgrade_target_allowed(
    current: str,
    target: str,
    *,
    allow_minor: bool = False,
    allow_major: bool = False,
) -> bool:
    relation = _upgrade_target_relation(current, target, allow_minor=allow_minor, allow_major=allow_major)
    if relation == "allowed":
        return True
    if relation == "none":
        return False
    if relation == "blocked_major":
        raise RuntimeError(
            f"Major upgrade is disabled in current flow: {current} -> {target}. "
            "Only patch updates are allowed by default; minor updates require --allow-minor, "
            "and the Composer Mautic 6 -> 7 flow requires --allow-major."
        )
    if relation == "blocked_minor":
        raise RuntimeError(
            f"Minor upgrade is disabled in current flow: {current} -> {target}. "
            "Pass --allow-minor for a forward minor upgrade within the same major."
        )
    raise RuntimeError(f"Invalid Mautic upgrade target: {current} -> {target}")


def _resolve_update_package(config: AgentConfig, target: str) -> Path:
    targets = _available_targets(config)
    src = targets.get(target)
    if not src:
        raise RuntimeError(f"No package source for target {target}")
    p = Path(src)
    if p.exists():
        return p
    # src is URL from static targets
    cache_dir = Path("/opt/mcd/cache/updates")
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{target}-update.zip"
    if dst.exists():
        return dst
    with urllib.request.urlopen(src, timeout=120) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    return dst


def _load_prepared_target_input(
    *,
    config: AgentConfig,
    raw_json: str | None,
    file_path: str | None,
    input_sha256: str,
    target: str,
    mode: str,
):
    if raw_json and file_path:
        raise RuntimeError("Choose exactly one prepared target transport")
    if not raw_json and not file_path:
        return None
    from mcd_agent.mautic_patch_plan import _target_version
    from mcd_agent.prepared_target import (
        PreparedTargetError,
        download_prepared_target,
        load_prepared_target_file,
        load_prepared_target_json,
        materialize_prepared_target,
        validate_prepared_target_runtime,
    )
    try:
        prepared = load_prepared_target_file(file_path, input_sha256) if file_path else load_prepared_target_json(raw_json or "")
        if prepared["target_version"] != target:
            raise PreparedTargetError("prepared_target_target_binding_mismatch")
        if mode != "auto" and prepared["install_type"] != mode:
            raise PreparedTargetError("prepared_target_install_type_binding_mismatch")
        if prepared["install_type"] == "composer":
            php_probe = subprocess.run(
                [str(config.php_bin or "php"), "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"],
                cwd="/",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            runtime_php = (php_probe.stdout or "").strip() if php_probe.returncode == 0 else ""
            validate_prepared_target_runtime(prepared, runtime_php)
        artifact = download_prepared_target(config, prepared)
        return materialize_prepared_target(prepared, artifact, read_version=_target_version)
    except PreparedTargetError as exc:
        raise RuntimeError(f"Prepared Mautic target rejected: {exc}") from exc


def _restore_prepared_target_backup(root: str, backup_path: str | Path) -> None:
    """Restore the complete pre-apply tree from the local upgrade backup."""
    target = Path(root).resolve(strict=True)
    archive = Path(backup_path).resolve(strict=True)
    if target == target.parent or target == Path("/") or not archive.is_file() or archive.is_symlink():
        raise RuntimeError("prepared_target_rollback_path_invalid")
    parent = target.parent
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members or any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            raise RuntimeError("prepared_target_rollback_archive_invalid")
        top = target.name
        if any(Path(member.name).parts[:1] != (top,) for member in members):
            raise RuntimeError("prepared_target_rollback_archive_binding_invalid")
        shutil.rmtree(target)
        tar.extractall(parent)


def _command_with_user(cmd: list[str], *, as_www_data: bool = False) -> list[str]:
    full = cmd
    if as_www_data:
        full = ["sudo", "-u", "www-data"] + cmd
    return full


def _run_capture(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> subprocess.CompletedProcess[str]:
    full = _command_with_user(cmd, as_www_data=as_www_data)
    logging.info("run: %s", " ".join(full))
    return subprocess.run(full, cwd=cwd, text=True, capture_output=True)


def _run(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> None:
    full = _command_with_user(cmd, as_www_data=as_www_data)
    proc = _run_capture(cmd, cwd=cwd, as_www_data=as_www_data)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(full)}\n{proc.stdout}\n{proc.stderr}")


def _remove_tree_with_retry(path: Path, *, strict: bool) -> None:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))

    proc = subprocess.run(["rm", "-rf", "--", str(path)], text=True, capture_output=True, check=False)
    if not path.exists():
        return
    msg = str(last_error or "").strip()
    fallback = (proc.stderr or proc.stdout or "").strip()
    detail = "; ".join(x for x in (msg, fallback) if x)
    if strict:
        raise RuntimeError(f"Unable to remove {path}: {detail or 'directory still exists'}")
    print(f"WARN unable to remove old cache tree {path}: {detail or 'directory still exists'}")


def _hard_clear_prod_cache(root: str) -> None:
    cache_root = Path(root) / "var" / "cache"
    prod_cache = cache_root / "prod"
    if prod_cache.exists():
        old_cache = cache_root / f".prod.mcd-delete-{int(time.time())}-{os.getpid()}"
        try:
            prod_cache.rename(old_cache)
            _remove_tree_with_retry(old_cache, strict=False)
        except OSError:
            _remove_tree_with_retry(prod_cache, strict=True)
    prod_cache.mkdir(parents=True, exist_ok=True)
    try:
        shutil.chown(cache_root, user="www-data", group="www-data")
        shutil.chown(prod_cache, user="www-data", group="www-data")
    except Exception:
        subprocess.run(["chown", "-R", "www-data:www-data", str(cache_root)], check=False)
    print("Composer preflight: cleared var/cache/prod")


def _clear_prod_cache_with_fallback(project_root: str, console_path: str, php_bin: str) -> None:
    proc = _run_capture([php_bin, console_path, "cache:clear"], cwd=project_root, as_www_data=True)
    if proc.returncode == 0:
        print("Composer post-update cache clear ok")
        return
    print("WARN standard cache:clear failed; falling back to hard var/cache/prod clear")
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    _hard_clear_prod_cache(project_root)
    _run([php_bin, console_path, "cache:clear"], cwd=project_root, as_www_data=True)


def _safe_mautic7_loopback_redis_dsn(dsn: str) -> str:
    raw = str(dsn or "").strip()
    if not raw.lower().startswith("redis://"):
        return raw
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host not in {"127.0.0.1", "localhost"}:
        return raw
    netloc = parsed.netloc
    idx = netloc.lower().rfind(host)
    if idx < 0:
        return raw
    # Mautic 7's Redis helper resolves 127.0.0.1/localhost into an endpoint
    # array, which Predis 3 treats as an aggregate connection and rejects. The
    # hex loopback form still connects locally but remains a scalar DSN.
    netloc = netloc[:idx] + "0x7f000001" + netloc[idx + len(host) :]
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_mautic7_loopback_redis_cache(project_root: str, target: str) -> bool:
    if _parse_semver(target)[0] != 7:
        return False
    local_php = Path(project_root) / "config" / "local.php"
    if not local_php.exists() or not local_php.is_file():
        return False
    text = local_php.read_text(encoding="utf-8", errors="ignore")
    if "cache_adapter_redis" not in text or "mautic.cache.adapter.redis" not in text:
        return False
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        dsn = match.group("dsn")
        updated = _safe_mautic7_loopback_redis_dsn(dsn)
        if updated == dsn:
            return match.group(0)
        changed = True
        return f"{match.group('prefix')}{match.group('quote')}{updated}{match.group('quote')}"

    updated = re.sub(
        r"(?P<prefix>['\"]dsn['\"]\s*=>\s*)(?P<quote>['\"])(?P<dsn>redis://[^'\"]+)(?P=quote)",
        repl,
        text,
        count=1,
    )
    if not changed or updated == text:
        return False
    backup = local_php.with_name(local_php.name + f".mcd-pre-m7-redis-dsn-{int(time.time())}.bak")
    if not backup.exists():
        shutil.copy2(local_php, backup)
    local_php.write_text(updated, encoding="utf-8")
    print("Mautic 7 Redis cache DSN normalized for Predis 3 loopback compatibility")
    return True


def _normalize_mautic7_composer_constraints(text: str, target: str) -> tuple[str, int]:
    if _parse_semver(target)[0] != 7:
        return text, 0
    try:
        data = json.loads(text)
    except Exception:
        return text, 0
    if not isinstance(data, dict):
        return text, 0
    require = data.get("require")
    if not isinstance(require, dict):
        return text, 0

    changes = 0
    current = str(require.get("composer/installers", "") or "").strip()
    if current and current != "^2.0":
        require["composer/installers"] = "^2.0"
        changes += 1
    if changes <= 0:
        return text, 0
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n", changes


def _composer_update_args(*, dry_run: bool = False) -> list[str]:
    args = ["update", "--with-all-dependencies"]
    if dry_run:
        args.append("--dry-run")
    return args


def _command_version_line(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> str:
    proc = _run_capture(cmd, cwd=cwd, as_www_data=as_www_data)
    if proc.returncode != 0:
        full = _command_with_user(cmd, as_www_data=as_www_data)
        raise RuntimeError(
            f"Version preflight failed ({proc.returncode}): {' '.join(full)}\n{proc.stdout}\n{proc.stderr}"
        )
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0].strip() if out else "ok"


def _backup_install(root: str) -> Path:
    backups = Path("/opt/mcd/backups")
    backups.mkdir(parents=True, exist_ok=True)
    out = backups / f"mautic-backup-{Path(root).name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="mcd-backup-") as td:
        tmp = Path(td) / "backup.tar.gz"
        subprocess.run(["tar", "-czf", str(tmp), "-C", str(Path(root).parent), str(Path(root).name)], check=True)
        shutil.move(str(tmp), out)
    return out


def _prepare_plugins_for_major_jump(root: str, from_ver: str, to_ver: str) -> None:
    if from_ver != "4.4.13" or to_ver != "5.1.1":
        return
    plugins_dir = Path(root) / "plugins"
    if not plugins_dir.exists():
        return
    bdir = Path("/opt/mcd/backups/plugins-pre-5.1.1")
    if bdir.exists():
        shutil.rmtree(bdir)
    shutil.copytree(plugins_dir, bdir)
    for d in plugins_dir.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in CORE_PLUGIN_BUNDLES:
            continue
        shutil.rmtree(d)


def _same_install_root(a: str, b: str) -> bool:
    try:
        return os.path.abspath(a) == os.path.abspath(b)
    except Exception:
        return str(a or "").rstrip("/") == str(b or "").rstrip("/")


def _instance_label(inst: MauticInstall) -> str:
    for value in (inst.primary_domain, inst.name, inst.instance_uid, inst.root):
        text = str(value or "").strip()
        if text:
            return text
    return "unknown instance"


def _assert_php84_host_safe(config: AgentConfig, upgraded_root: str) -> None:
    blockers: list[str] = []
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    for inst in installs:
        if _same_install_root(inst.root, upgraded_root):
            continue
        major = inst.mautic_major
        if major is None:
            try:
                detected = _read_current_version(inst.root, inst.console_path, config.php_bin, config.mautic_run_as_user)
                parsed = _parse_semver(detected)
                if parsed != (0, 0, 0):
                    major = parsed[0]
            except Exception:
                major = None
        if major is None:
            blockers.append(f"{_instance_label(inst)} (unknown Mautic version)")
        elif int(major) < 7:
            blockers.append(f"{_instance_label(inst)} (Mautic {major})")
    if blockers:
        raise RuntimeError(
            "PHP 8.4 system upgrade is blocked because this host still has instance(s) "
            "that may not support PHP 8.4: "
            + "; ".join(blockers)
        )


def _php84_package_names() -> list[str]:
    return [f"php8.4-{suffix}" for suffix in PHP84_PACKAGE_SUFFIXES]


def _install_php84_packages() -> None:
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    subprocess.run(["apt-get", "update", "--allow-releaseinfo-change"], check=True, env=env)
    subprocess.run(["apt-get", "install", "-y", *_php84_package_names()], check=True, env=env)


def _is_custom_php_ini(path: Path) -> bool:
    name = path.name
    low = name.lower()
    if name in PHP_CUSTOM_INI_NAMES:
        return True
    if not low.endswith(".ini"):
        return False
    return any(hint in low for hint in PHP_CUSTOM_INI_HINTS)


def _migrate_php_custom_ini(
    *,
    from_version: str = "8.3",
    to_version: str = "8.4",
    php_etc_root: Path = Path("/etc/php"),
) -> list[str]:
    moved: list[str] = []
    for sapi in ("cli", "fpm"):
        src_dir = php_etc_root / from_version / sapi / "conf.d"
        dst_dir = php_etc_root / to_version / sapi / "conf.d"
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(src_dir.iterdir(), key=lambda p: p.name):
            if not _is_custom_php_ini(src):
                continue
            dst = dst_dir / src.name
            if dst.exists() or dst.is_symlink():
                moved.append(f"skip existing {dst}")
                continue
            shutil.copy2(str(src), str(dst), follow_symlinks=False)
            moved.append(f"{src} -> {dst}")
    return moved


def _rewrite_nginx_php_fpm_references(
    *,
    from_version: str = "8.3",
    to_version: str = "8.4",
    nginx_roots: tuple[Path, ...] = (Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available")),
) -> list[str]:
    changed: list[str] = []
    old = f"php{from_version}-fpm"
    new = f"php{to_version}-fpm"
    for root in nginx_roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.iterdir(), key=lambda p: p.name):
            if path.is_dir():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if old not in text:
                continue
            path.write_text(text.replace(old, new), encoding="utf-8")
            changed.append(str(path))
    return changed


def _enable_php84_and_restart_nginx() -> None:
    subprocess.run(["systemctl", "enable", "--now", "php8.4-fpm"], check=True)
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        raise RuntimeError(f"nginx -t failed after PHP 8.4 switch: {(test.stderr or test.stdout or '').strip()}")
    subprocess.run(["systemctl", "restart", "nginx"], check=True)
    subprocess.run(["systemctl", "restart", "php8.4-fpm"], check=False)


def _purge_php83_packages() -> None:
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    subprocess.run(["bash", "-lc", "apt-get purge -y 'php8.3*'"], check=True, env=env)


def _apply_php84_system_upgrade(config: AgentConfig, upgraded_root: str) -> None:
    _assert_php84_host_safe(config, upgraded_root)
    print("PHP 8.4 preflight ok: all discovered host instances are Mautic 7-compatible")
    _install_php84_packages()
    moved = _migrate_php_custom_ini()
    if moved:
        print("PHP 8.4 custom ini migration:")
        for item in moved:
            print(f"  {item}")
    changed = _rewrite_nginx_php_fpm_references()
    if changed:
        print("PHP 8.4 nginx references updated: " + ", ".join(changed))
    _enable_php84_and_restart_nginx()
    php84 = str(shutil.which("php8.4") or "/usr/bin/php8.4")
    probe = subprocess.run([php84, "-v"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        raise RuntimeError("PHP 8.4 runtime is not runnable; refusing to purge PHP 8.3")
    _purge_php83_packages()
    print("PHP 8.4 system upgrade completed")


def _apply_system_upgrade(
    from_ver: str,
    to_ver: str,
    *,
    config: AgentConfig | None = None,
    upgraded_root: str | None = None,
) -> None:
    source_major = _parse_semver(from_ver)[0]
    target_major = _parse_semver(to_ver)[0]
    if (source_major, target_major) in {(6, 7), (7, 7)}:
        if config is None or not upgraded_root:
            raise RuntimeError("PHP 8.4 system upgrade requires config and upgraded root")
        _apply_php84_system_upgrade(config, upgraded_root)
        return
    if from_ver == "4.4.13" and to_ver == "5.1.1":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.2-apcu",
                "php8.2-fpm",
                "php8.2-cli",
                "php8.2-curl",
                "php8.2-mysql",
                "php8.2-mailparse",
                "php8.2-gd",
                "php8.2-mbstring",
                "php8.2-imagick",
                "php8.2-bcmath",
                "php8.2-zip",
                "php8.2-tidy",
                "php8.2-soap",
                "php8.2-intl",
                "php8.2-xml",
                "php8.2-imap",
                "php8.2-redis",
                "php8.2-opcache",
                "-y",
            ],
            check=True,
        )
        _run(["sed", "-i", "s/php8\\.0-fpm/php8.2-fpm/g", "/etc/nginx/sites-enabled/*"], cwd="/")
        _run(["service", "nginx", "restart"], cwd="/")
        subprocess.run(["apt", "purge", "php8.0*", "-y"], check=True)
    if to_ver == "6.0.7":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.3-apcu",
                "php8.3-fpm",
                "php8.3-cli",
                "php8.3-curl",
                "php8.3-mysql",
                "php8.3-mailparse",
                "php8.3-gd",
                "php8.3-mbstring",
                "php8.3-imagick",
                "php8.3-bcmath",
                "php8.3-zip",
                "php8.3-tidy",
                "php8.3-soap",
                "php8.3-intl",
                "php8.3-xml",
                "php8.3-imap",
                "php8.3-redis",
                "php8.3-opcache",
                "-y",
            ],
            check=True,
        )


def _php84_runtime_observation() -> dict[str, object]:
    php84 = str(shutil.which("php8.4") or "/usr/bin/php8.4")
    probe = subprocess.run([php84, "-r", "echo PHP_VERSION;"], capture_output=True, text=True, check=False)
    service = subprocess.run(["systemctl", "is-active", "php8.4-fpm"], capture_output=True, text=True, check=False)
    nginx = subprocess.run(["nginx", "-T"], capture_output=True, text=True, check=False)
    sockets = sorted(set(re.findall(r"fastcgi_pass\\s+unix:([^;]*php8\\.4-fpm[^;]*);", nginx.stdout or "")))
    return {
        "cli_version": (probe.stdout or "").strip() if probe.returncode == 0 else "",
        "fpm_version": "8.4" if service.returncode == 0 and (service.stdout or "").strip() == "active" else "",
        "nginx_socket": sockets,
    }


def _insert_migration_hacks_if_needed(root: str, to_ver: str) -> None:
    if to_ver != "6.0.7":
        return
    local_php = Path(root) / "config" / "local.php"
    if not local_php.exists():
        local_php = Path(root) / "app" / "config" / "local.php"
    if not local_php.exists():
        return
    cfg = parse_local_php(str(local_php))
    prefix = str(cfg.get("db_table_prefix", ""))
    table = f"{prefix}migrations"
    host = str(cfg.get("db_host", "localhost"))
    port = str(cfg.get("db_port", "3306"))
    name = str(cfg.get("db_name", ""))
    user = str(cfg.get("db_user", ""))
    pwd = str(cfg.get("db_password", ""))
    if not (name and user):
        return
    sql = (
        f"INSERT INTO `{table}` (version, executed_at, execution_time) VALUES "
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20211020114811'), NOW(), 0),"
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20230621074925'), NOW(), 0);"
    )
    cmd = [
        "mysql",
        f"-h{host}",
        f"-P{port}",
        f"-u{user}",
        f"-p{pwd}",
        name,
        "-e",
        sql,
    ]
    subprocess.run(cmd, check=True)


def _patch_lifecycle_phase(hook, root: str, phase: str) -> None:
    callback = getattr(hook, "apply_phase", None)
    if callback is not None:
        callback(root, phase)


def _prepare_patch_target_stage(config, root: str, current: str, target: str, mode: str, plan, facts_provider=None, prepared_target=None):
    from mcd_agent.mautic_patch_stage import TargetStage, application_root
    from mcd_agent.mautic_patch_plan import _target_version
    package_identity = {}
    def prepare(stage_root: Path) -> None:
        if prepared_target is not None:
            from mcd_agent.prepared_target import merge_into_live
            merge_into_live(prepared_target.root, application_root(stage_root))
            package_identity["sha256"] = prepared_target.artifact_sha256.removeprefix("sha256:")
        elif mode == "composer":
            cjson = stage_root / "composer.json"
            text = cjson.read_text(encoding="utf-8")
            updated, _count = _replace_version_tokens(text, current, target)
            updated, _count = _normalize_mautic7_composer_constraints(updated, target)
            cjson.write_text(updated, encoding="utf-8")
            composer = _resolve_composer_bin(config.php_bin)
            _run([composer, *_composer_update_args(), "--no-scripts"], cwd=str(stage_root), as_www_data=False)
            package_identity["sha256"] = hashlib.sha256((stage_root / "composer.lock").read_bytes()).hexdigest()
        elif mode == "zip":
            import zipfile
            package = _resolve_update_package(config, target)
            package_identity["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
            with zipfile.ZipFile(package) as archive:
                for item in archive.infolist():
                    parts = Path(item.filename).parts
                    if not parts or item.filename.startswith("/") or any(part in {".", ".."} for part in parts) or "\\" in item.filename or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                        raise RuntimeError("Unsafe target update archive path")
                    destination = stage_root.joinpath(*parts)
                    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != stage_root.parent):
                        raise RuntimeError("Target update archive symlink path")
                archive.extractall(stage_root)
        else:
            raise RuntimeError("Unsupported target stage install type")
    staged = TargetStage(root, plan, prepare, _target_version, application_root, facts_provider=facts_provider,
                         defer_conditions=True)
    staged.target_package_sha256 = package_identity["sha256"]
    return staged


def _patch_evidence_records(plan: dict[str, Any], outcomes: list[dict[str, Any]] | None = None,
                            failure: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build the contract-shaped record set without inventing a successful result."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in outcomes or []:
        if isinstance(row, dict) and str(row.get("id", "") or ""):
            by_id[str(row["id"])] = row
    failed_id = str((failure or {}).get("id", "") or "")
    failed_gates = (failure or {}).get("gates", [])
    records: list[dict[str, Any]] = []
    for record in plan.get("patches", []):
        identifier = str(record.get("id", "") or "")
        row = by_id.get(identifier)
        if row is not None:
            decision = str(row.get("decision", "not_applied") or "not_applied")
            gates = row.get("gates", [])
            before = row.get("before_sha256")
            after = row.get("after_sha256")
        elif identifier == failed_id:
            decision = "error"
            gates = failed_gates if isinstance(failed_gates, list) else []
            before = None
            after = None
        else:
            decision = "not_applied" if failure is not None else "not_observed"
            gates = []
            before = None
            after = None
        records.append({
            "id": identifier,
            "execution_kind": record.get("execution_kind"),
            "decision": decision,
            "gates": gates if isinstance(gates, list) else [],
            "before_sha256": before,
            "after_sha256": after,
        })
    return records


def _patch_resolution_for_upgrade_evidence(config: AgentConfig, install: MauticInstall, plan: dict[str, Any],
                                           *, current: str, target: str, mode: str,
                                           run_id: str | None) -> dict[str, Any] | None:
    """Recover the already-issued envelope; never select a second catalog plan."""
    if not getattr(config, "mcc_url", None) or not run_id:
        return None
    try:
        from mcd_agent.mautic_patch_resolution import canonical_json_sha256, resolve_plan
        result = resolve_plan(
            config, install, trigger="upgrade_lifecycle", phase="dependency_update_preflight",
            operation="apply", observed_version=current, observed_major=_parse_semver(current)[0],
            target_version=target, install_type=mode, run_id=run_id,
        )
        if (result.get("status") != "selected"
                or result.get("plan_sha256") != canonical_json_sha256(plan)
                or result.get("plan") != plan):
            return None
        return result
    except Exception as exc:
        print(f"WARN MCD v3 evidence envelope lookup unavailable: {type(exc).__name__}: {exc}")
        return None


def _report_upgrade_patch_evidence(config: AgentConfig, install: MauticInstall, resolution: dict[str, Any] | None,
                                   plan: dict[str, Any], *, started_at: str, status: str,
                                   outcomes: list[dict[str, Any]] | None = None,
                                   failure: dict[str, Any] | None = None,
                                   rollback: dict[str, Any] | None = None) -> bool:
    """Emit and durably submit one immutable-plan-bound v3 evidence record."""
    records = _patch_evidence_records(plan, outcomes, failure)
    evidence = {
        "schema": "mcd-mautic-patch-evidence-v1",
        "host_id": (resolution or {}).get("host_id", ""),
        "instance_uid": install.instance_uid,
        "catalog_revision": (resolution or {}).get("catalog_revision", ""),
        "catalog_sha256": (resolution or {}).get("catalog_sha256", ""),
        "registry_commit": plan.get("registry_commit", ""),
        "registry_sha256": plan.get("registry_sha256", ""),
        "plan_sha256": (resolution or {}).get("plan_sha256", ""),
        "trigger": plan.get("trigger", ""),
        "phase": plan.get("phase", ""),
        "run_id": plan.get("run_id", ""),
        "operation": "apply",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "selected": [str(record.get("id", "") or "") for record in plan.get("patches", [])],
        "records": records,
        "rollback": rollback or {"available": False, "attempted": False, "succeeded": False},
    }
    from mcd_agent import __version__
    evidence["agent_version"] = __version__
    print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps(evidence, ensure_ascii=True, sort_keys=True,
                                                   separators=(",", ":")), flush=True)
    if resolution is None:
        return False
    try:
        from mcd_agent.mautic_patch_resolution import _host_identity, report_evidence
        host_name, hostname = _host_identity(config)
        report_evidence(
            config, instance_uid=install.instance_uid, mcc_host_name=host_name, hostname=hostname,
            host_id=str(resolution["host_id"]), catalog_revision=str(resolution["catalog_revision"]),
            catalog_sha256=str(resolution["catalog_sha256"]), plan_sha256=str(resolution["plan_sha256"]),
            trigger="upgrade_lifecycle", phase="dependency_update_preflight",
            run_id=str(plan["run_id"]), evidence=evidence,
        )
        return True
    except Exception as exc:
        print(f"WARN MCD v3 evidence submission failed: {type(exc).__name__}: {exc}")
        return False


def _apply_zip(config: AgentConfig, root: str, console_path: str, php_bin: str, target: str, after_source_install=None) -> None:
    pkg = _resolve_update_package(config, target)
    stage = getattr(after_source_install, "target_stage", None)
    if stage is not None and hashlib.sha256(pkg.read_bytes()).hexdigest() != stage.target_package_sha256:
        raise RuntimeError("Target update archive changed after staged preflight")
    dst = Path(root) / pkg.name
    shutil.copy2(pkg, dst)
    _run([php_bin, console_path, "mautic:update:apply", "--force", f"--update-package={dst.name}"], cwd=root, as_www_data=True)
    if after_source_install:
        after_source_install(root)
    for phase in ("before_cache_warmup", "before_asset_generation", "preflight_frontend_assets", "before_doctrine_migrations"):
        _patch_lifecycle_phase(after_source_install, root, phase)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=root, as_www_data=True)


def _apply_prepared_target(
    config: AgentConfig,
    root: str,
    console_path: str,
    php_bin: str,
    target: str,
    prepared_target,
    after_source_install=None,
) -> None:
    """Apply an MCC-built skeleton through the existing upgrade lifecycle."""
    from mcd_agent.mautic_patch_stage import application_root
    from mcd_agent.prepared_target import merge_into_live

    live_application = application_root(Path(root))
    if prepared_target.input["target_version"] != target:
        raise RuntimeError("prepared_target_target_binding_mismatch")
    merge_into_live(prepared_target.root, live_application)
    print(
        "MCD_PREPARED_TARGET_EVIDENCE="
        + json.dumps(
            {
                "schema": "mcd-mautic-prepared-target-receipt-v1",
                "status": "staged",
                "prepared_target_id": prepared_target.input["prepared_target_id"],
                "target_version": target,
                "artifact_sha256": prepared_target.artifact_sha256,
                "host_composer_invoked": False,
                "preserved_paths": ["config", "app/config", "var"],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    if after_source_install:
        after_source_install(root)
    for phase in ("before_cache_warmup", "before_asset_generation", "preflight_frontend_assets", "before_doctrine_migrations"):
        _patch_lifecycle_phase(after_source_install, root, phase)
    project_root = _resolve_composer_project_root(root)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=root, as_www_data=True)
    migration_cmd = _doctrine_migrate_command(project_root, console_path, php_bin)
    _run_doctrine_migrate_with_reconcile(project_root, console_path, php_bin, migration_cmd)
    _verify_or_reconcile_doctrine_migrations(project_root, console_path, php_bin)


def _replace_version_tokens(text: str, current: str, target: str) -> tuple[str, int]:
    if not current or not target or current == target:
        return text, 0
    count = 0
    updated = text
    # Replace exact semantic version tokens only, keeps unrelated values intact.
    for src, dst in ((f"v{current}", f"v{target}"), (current, target)):
        rx = re.compile(rf"(?<![0-9A-Za-z]){re.escape(src)}(?![0-9A-Za-z])")
        updated, n = rx.subn(dst, updated)
        count += n
    return updated, count


def _resolve_composer_project_root(root: str) -> str:
    p = Path(root)
    # A recommended-project owns the lock above its application docroot.
    # Do not mistake the application's core manifest for that project.
    if p.name in {"docroot", "public"} and (p / "bin" / "console").is_file():
        parent = p.parent
        manifest_path = parent / "composer.json"
        lock_path = parent / "composer.lock"
        if manifest_path.is_file() and lock_path.is_file():
            if p.is_symlink() or manifest_path.is_symlink() or lock_path.is_symlink():
                raise RuntimeError("composer_project_root_symlink")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                requirements = manifest.get("require", {})
                packages = lock.get("packages", [])
                if not isinstance(requirements, dict) or not isinstance(packages, list):
                    raise ValueError("invalid Composer structure")
                names = {item.get("name") for item in packages if isinstance(item, dict)}
            except (ValueError, AttributeError, TypeError) as exc:
                raise RuntimeError("composer_project_root_metadata_invalid") from exc
            core_names = {"mautic/core", "mautic/core-lib"}
            if core_names.intersection(requirements) and core_names.intersection(names):
                return str(parent)
    candidates = [p, p.parent]
    for c in candidates:
        if (c / "composer.json").exists() and (c / "bin" / "console").exists():
            return str(c)
    return root


def _ensure_www_data_composer_cache() -> None:
    cache_root = Path("/var/www/.cache/composer")
    (cache_root / "vcs").mkdir(parents=True, exist_ok=True)
    for path in (Path("/var/www/.cache"), cache_root, cache_root / "vcs"):
        try:
            os.chown(path, 33, 33)
        except Exception:
            subprocess.run(["chown", "www-data:www-data", str(path)], check=False)


def _doctrine_migrate_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:migrate" in output:
        return "doctrine:migrations:migrate"
    if "doctrine:migration:migrate" in output:
        return "doctrine:migration:migrate"
    return "doctrine:migrations:migrate"


def _doctrine_version_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:version" in output:
        return "doctrine:migrations:version"
    if "doctrine:migration:version" in output:
        return "doctrine:migration:version"
    return "doctrine:migrations:version"


def _doctrine_status_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:status" in output:
        return "doctrine:migrations:status"
    if "doctrine:migration:status" in output:
        return "doctrine:migration:status"
    return "doctrine:migrations:status"


def _doctrine_up_to_date_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:up-to-date" in output:
        return "doctrine:migrations:up-to-date"
    if "doctrine:migration:up-to-date" in output:
        return "doctrine:migration:up-to-date"
    return "doctrine:migrations:up-to-date"


def _migration_status_count(output: str, label: str) -> int | None:
    m = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*(\d+)\s*\|", output)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _migration_unavailable_count(output: str) -> int:
    return _migration_status_count(output, "Executed Unavailable") or 0


def _migration_new_count(output: str) -> int:
    return _migration_status_count(output, "New") or 0


def _migration_unavailable_versions(output: str) -> list[str]:
    versions: list[str] = []
    for match in re.finditer(r"\((Mautic\\Migrations\\Version[0-9A-Za-z_]+)\)", output):
        version = match.group(1)
        if version not in versions:
            versions.append(version)
    return versions


def _migration_failed_version(output: str) -> str | None:
    patterns = (
        r"Migration\s+(Mautic\\Migrations\\Version[0-9A-Za-z_]+)\s+failed",
        r"(Mautic\\Migrations\\Version[0-9A-Za-z_]+)",
    )
    for pattern in patterns:
        m = re.search(pattern, output)
        if m:
            return m.group(1)
    return None


def _migration_already_in_schema_error(output: str) -> bool:
    needles = (
        "SQLSTATE[42S01]",
        "Base table or view already exists",
        "Table already exists",
        "already exists",
        "SQLSTATE[42S21]",
        "Duplicate column name",
        "Duplicate key name",
        "Can't create table",
    )
    lowered = output.lower()
    return any(x.lower() in lowered for x in needles)


def _run_doctrine_migrate_with_reconcile(
    project_root: str,
    console_path: str,
    php_bin: str,
    migration_cmd: str,
) -> None:
    version_cmd = _doctrine_version_command(project_root, console_path, php_bin)
    marked_versions: set[str] = set()
    for _attempt in range(50):
        proc = _run_capture(
            [php_bin, console_path, migration_cmd, "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
        if proc.returncode == 0:
            return
        output = f"{proc.stdout}\n{proc.stderr}"
        if not _migration_already_in_schema_error(output):
            raise RuntimeError(
                f"Command failed ({proc.returncode}): sudo -u www-data {php_bin} {console_path} {migration_cmd} --no-interaction"
                f"\n{proc.stdout}\n{proc.stderr}"
            )
        version = _migration_failed_version(output)
        if not version:
            raise RuntimeError(
                "Doctrine migration failed with an already-in-schema error, "
                "but MCD could not identify the migration version to mark executed\n"
                + output.strip()
            )
        if version in marked_versions:
            raise RuntimeError(
                "Doctrine migration still fails after marking the same already-in-schema version executed\n"
                + output.strip()
            )
        marked_versions.add(version)
        print(f"Doctrine migrations reconcile: marking {version} as executed after already-in-schema failure")
        _run(
            [php_bin, console_path, version_cmd, "--add", version, "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
    raise RuntimeError("Doctrine migration reconcile exceeded 50 already-in-schema retries")


def _verify_or_reconcile_doctrine_migrations(project_root: str, console_path: str, php_bin: str) -> None:
    up_to_date_cmd = _doctrine_up_to_date_command(project_root, console_path, php_bin)
    status_cmd = _doctrine_status_command(project_root, console_path, php_bin)
    version_cmd = _doctrine_version_command(project_root, console_path, php_bin)

    check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
    if check.returncode == 0:
        print("Doctrine migrations post-check: up-to-date")
        return

    status = _run_capture([php_bin, console_path, status_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
    status_output = f"{status.stdout}\n{status.stderr}"
    new_count = _migration_new_count(status_output)
    unavailable_count = _migration_unavailable_count(status_output)

    if new_count > 0:
        print(f"Doctrine migrations reconcile: marking {new_count} already-applied available migrations as executed")
        _run(
            [php_bin, console_path, version_cmd, "--add", "--all", "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
        check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
        if check.returncode == 0:
            print("Doctrine migrations post-check: up-to-date after reconcile")
            return
        status = _run_capture([php_bin, console_path, status_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
        status_output = f"{status.stdout}\n{status.stderr}"
        new_count = _migration_new_count(status_output)
        unavailable_count = _migration_unavailable_count(status_output)

    if new_count == 0 and unavailable_count > 0:
        unavailable_versions = _migration_unavailable_versions(status_output)
        if unavailable_versions:
            print(
                "Doctrine migrations reconcile: removing "
                f"{len(unavailable_versions)} unavailable migration metadata record(s)"
            )
            for version in unavailable_versions:
                _run(
                    [php_bin, console_path, version_cmd, "--delete", version, "--no-interaction"],
                    cwd=project_root,
                    as_www_data=True,
                )
            check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
            if check.returncode == 0:
                print("Doctrine migrations post-check: up-to-date after unavailable metadata cleanup")
                return

        print(
            "Doctrine migrations post-check warning: "
            f"{unavailable_count} previously executed migration(s) are no longer registered; no pending migrations remain"
        )
        return

    output = f"{check.stdout}\n{check.stderr}".strip()
    raise RuntimeError("Post-migration up-to-date check failed\n" + output)


def _mautic_core_file_candidates(root: str, relpath: Path) -> list[Path]:
    base = Path(root)
    candidates = [
        base / relpath,
        base / "docroot" / relpath,
        base / "public" / relpath,
    ]
    if base.name.lower() in {"public", "docroot", "public_html"}:
        candidates.append(base.parent / relpath)
        candidates.append(base.parent / "docroot" / relpath)
        candidates.append(base.parent / "public" / relpath)
    out: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _apply_mautic7_twig_include_hotfix(root: str, target: str) -> bool:
    """Historical compatibility name; applicability belongs to the catalog."""
    return False


def _apply_composer(root: str, console_path: str, php_bin: str, current: str, target: str, after_source_install=None) -> None:
    project_root = _resolve_composer_project_root(root)
    cjson = Path(project_root) / "composer.json"
    if not cjson.exists():
        raise RuntimeError("composer.json not found")
    composer_bin = _resolve_composer_bin(php_bin)
    _ensure_node20()
    _ensure_www_data_composer_cache()
    composer_version = _command_version_line(
        [composer_bin, "-V", "--no-interaction", "--no-ansi"],
        cwd=project_root,
        as_www_data=True,
    )
    readiness = composer_readiness_from_observation(composer_bin, composer_version)
    if readiness.get("status") not in {"reused", "success"}:
        raise RuntimeError("Composer >= 2.8.6 is required to defer application scripts safely during upgrades")
    node_version = _command_version_line(["node", "-v"], cwd=project_root)
    npm_version = _command_version_line(["npm", "-v"], cwd=project_root)
    print(f"Composer preflight ok: {composer_version} ({composer_bin})")
    print(f"Node.js preflight ok: {node_version}")
    print(f"npm preflight ok: {npm_version}")
    stage = getattr(after_source_install, "target_stage", None)
    from mcd_agent.composer_permissions import prepare_composer_paths
    staged_root = getattr(stage, "root", None)
    permissions = prepare_composer_paths(project_root,
        target_project_root=str(staged_root) if isinstance(staged_root, (str, Path)) else None)
    print("Composer permissions prepared: " + json.dumps(permissions, sort_keys=True))
    text = cjson.read_text(encoding="utf-8")
    updated, changes = _replace_version_tokens(text, current, target)
    updated, constraint_changes = _normalize_mautic7_composer_constraints(updated, target)
    changes += constraint_changes
    if changes > 0 and updated != text:
        cjson.write_text(updated, encoding="utf-8")
    if stage is not None:
        if set(stage.composer_files) != {"composer.json", "composer.lock"}:
            raise RuntimeError("Staged Composer target lacks exact dependency lock")
        for name, content in stage.composer_files.items():
            (Path(project_root) / name).write_bytes(content)
    _run([composer_bin, *_composer_update_args(dry_run=True), "--no-scripts"], cwd=project_root, as_www_data=True)
    print("Composer dependency dry-run ok")
    # Post-update scripts boot Mautic. They must never load the previous core's
    # compiled container after Composer has replaced its classes and services.
    deferred_events = ("post-autoload-dump", "post-update-cmd")
    _run(
        ["env", "COMPOSER_SKIP_SCRIPTS=" + ",".join(deferred_events), composer_bin, *(["install", "--no-interaction", "--no-scripts"] if stage is not None else _composer_update_args())],
        cwd=project_root,
        as_www_data=True,
    )
    patched = False
    _normalize_mautic7_loopback_redis_cache(project_root, target)
    _hard_clear_prod_cache(project_root)
    if after_source_install:
        after_source_install(project_root)
    for phase in ("before_cache_warmup", "before_asset_generation", "preflight_frontend_assets"):
        _patch_lifecycle_phase(after_source_install, project_root, phase)
    scripts = json.loads(cjson.read_text(encoding="utf-8")).get("scripts", {})
    for event in deferred_events:
        if event in scripts:
            _run([composer_bin, "run-script", "--no-interaction", event], cwd=project_root, as_www_data=True)
    _clear_prod_cache_with_fallback(project_root, console_path, php_bin)
    if patched:
        print("Mautic 7 Twig include hotfix cache refresh completed")
    _patch_lifecycle_phase(after_source_install, project_root, "before_doctrine_migrations")
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=project_root, as_www_data=True)
    migration_cmd = _doctrine_migrate_command(project_root, console_path, php_bin)
    _run_doctrine_migrate_with_reconcile(project_root, console_path, php_bin, migration_cmd)
    _verify_or_reconcile_doctrine_migrations(project_root, console_path, php_bin)


def run_upgrade_preflight(
    *,
    config: AgentConfig,
    root: str | None,
    mode: str,
    target_override: str | None = None,
    repair_plan_json: str | None = None,
    with_system_upgrade: bool = False,
) -> int:
    """Emit the read-only MCC-facing upgrade contract."""
    run_id = "preflight-" + uuid4().hex
    inst = _pick_install_record(config, root)
    install_root, console = inst.root, inst.console_path
    version_evidence = read_mautic_version_evidence_read_only(install_root)
    current = str(version_evidence.get("version") or "0.0.0")
    version_source = str(version_evidence.get("source") or "unavailable_read_only")
    target = _clean_target_version(target_override)
    if not target and _parse_semver(current)[0] == 6:
        target = str((_release_targets(config).get("7") or {}).get("version", ""))
    target = target or ""
    chosen_mode = detect_install_type(install_root) if mode == "auto" else mode
    composer = composer_readiness(php_bin=config.php_bin, allow_bootstrap=False)
    if chosen_mode != "composer":
        composer = {"status": "not_required", "compatible": True, "path": "", "version": "", "php": composer.get("php", {})}
    major_6_to_7 = _parse_semver(current)[0] == 6 and _parse_semver(target)[0] == 7
    if major_6_to_7 and not repair_plan_json:
        json_repair = inspect_json_schema_repair(
            root=install_root,
            current_version=current,
            target_version=target,
            local_php_path=inst.local_php_path,
        )
        discovered_prefix = str(json_repair.get("table_prefix") or "")
        if discovered_prefix or json_repair.get("status") == "supported":
            effective_repair_plan_json = json.dumps(
                build_json_repair_plan(discovered_prefix),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            json_repair = inspect_json_schema_repair(
                root=install_root,
                current_version=current,
                target_version=target,
                local_php_path=inst.local_php_path,
                repair_plan_json=effective_repair_plan_json,
            )
    else:
        json_repair = inspect_json_schema_repair(
            root=install_root,
            current_version=current,
            target_version=target,
            local_php_path=inst.local_php_path,
            repair_plan_json=repair_plan_json,
        )
    if json_repair.get("repair_plan", {}).get("status") == "accepted":
        plan = json_repair["repair_plan"].get("plan")
        if isinstance(plan, dict):
            json_repair["repair_plan"]["sha256"] = repair_plan_digest(plan)
    composer_ok = composer.get("status") in {"not_required", "reused", "success"}
    json_ok = json_repair.get("status") in {"unsupported", "supported"}
    status = "ready" if version_source == "static_metadata" and composer_ok and json_ok else "needs_attention"
    from mcd_agent.mautic_manual_upgrade import (
        MANUAL_SYSTEM_UPGRADE_CAPABILITY,
        manual_system_upgrade_capability,
    )
    manual_system_upgrade = manual_system_upgrade_capability(current, target)
    payload = {
        "schema": "mcd-mautic-upgrade-preflight-v1",
        "contract_version": 1,
        "run_id": run_id,
        "status": status,
        "root": install_root,
        "source_version": current,
        "target_version": target,
        "version_source": version_source,
        "instance": {
            "instance_uid": inst.instance_uid,
            "root": install_root,
            "console": console,
            "current_version": current,
            "target_version": target,
            "mode": chosen_mode,
        },
        "composer": {"schema": "mcd-mautic-composer-readiness-v1", **composer},
        "json_schema_repair": json_repair,
        "backup_prerequisite": json_repair.get("backup_prerequisite", {}),
        "runtime_capabilities": [MANUAL_SYSTEM_UPGRADE_CAPABILITY],
        "manual_system_upgrade": manual_system_upgrade,
    }
    if version_source != "static_metadata":
        payload["reason"] = "authoritative on-disk Mautic version evidence is unavailable"
    print(
        "MCD_UPGRADE_PREFLIGHT_EVIDENCE="
        + json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    return 0 if status == "ready" else 1


def run_upgrade_composer_prepare(
    *,
    config: AgentConfig,
    root: str | None,
    mode: str,
    target_override: str | None = None,
    with_system_upgrade: bool = False,
) -> int:
    """Perform only the pinned Composer bootstrap/readiness stage."""
    inst = _pick_install_record(config, root)
    version_evidence = read_mautic_version_evidence_read_only(inst.root)
    current = str(version_evidence.get("version") or "0.0.0")
    version_source = str(version_evidence.get("source") or "unavailable_read_only")
    target = _clean_target_version(target_override)
    if mode != "composer":
        composer = {"status": "not_required", "compatible": True, "path": "", "version": ""}
    else:
        composer = composer_readiness(php_bin=config.php_bin,
                                      allow_bootstrap=version_source == "static_metadata")
    status = "ready" if version_source == "static_metadata" and composer.get("status") in {"not_required", "reused", "success"} else "needs_attention"
    payload = {
        "schema": "mcd-mautic-upgrade-preflight-v1",
        "contract_version": 1,
        "run_id": "composer-prepare-" + uuid4().hex,
        "status": status,
        "root": inst.root,
        "source_version": current,
        "target_version": target,
        "version_source": version_source,
        "instance": {"instance_uid": inst.instance_uid, "root": inst.root, "current_version": current, "target_version": target, "mode": mode},
        "composer": {"schema": "mcd-mautic-composer-readiness-v1", **composer},
    }
    if version_source != "static_metadata":
        payload["reason"] = "authoritative on-disk Mautic version evidence is unavailable"
    print(
        "MCD_UPGRADE_PREFLIGHT_EVIDENCE="
        + json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    return 0 if status == "ready" else 1


def run_upgrade_authorize_repair(
    *,
    config: AgentConfig,
    root: str | None,
    target_override: str,
    repair_plan_json: str,
    backup_manifest_path: str,
    key_path: str = "/etc/mcd/mcc-operation-signing.key",
    output_path: str | None = None,
) -> int:
    """Issue a short-lived context only after verifying a real local backup marker."""
    inst = _pick_install_record(config, root)
    version_evidence = read_mautic_version_evidence_read_only(inst.root)
    if version_evidence.get("source") != "static_metadata":
        raise RuntimeError("repair authorization requires authoritative on-disk Mautic version evidence")
    current = str(version_evidence.get("version") or "0.0.0")
    target = _clean_target_version(target_override)
    if _parse_semver(current)[0] != 6 or _parse_semver(target)[0] != 7:
        raise RuntimeError("repair authorization is only supported for Mautic 6 to 7")
    result = issue_repair_authorization_context(
        root=inst.root,
        instance_uid=inst.instance_uid,
        source_version=current,
        target_version=target,
        repair_plan_json=repair_plan_json,
        backup_manifest_path=backup_manifest_path,
        key_path=key_path,
        output_path=output_path,
    )
    print("MCD_JSON_REPAIR_AUTHORIZATION=" + json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


def _permissions_check(config: AgentConfig, root: str, *, stage_label: str) -> None:
    user = str(config.mautic_run_as_user or "www-data").strip() or "www-data"
    res = ensure_instance_permissions(
        root=root,
        run_as_user=user,
        guard_paths=list(config.fs_permissions_guard_paths or []),
        fix_console_exec=bool(config.fs_permissions_guard_fix_console_exec),
        console_relpath=str(config.fs_permissions_guard_console_relpath or "bin/console"),
    )
    if res.errors:
        raise RuntimeError(
            "Pre-upgrade permissions check failed: " + "; ".join(str(x) for x in res.errors if str(x).strip())
        )
    repaired = len(res.repaired_paths)
    console_fixed = bool(res.console_exec_fixed)
    logging.info(
        "[%s] %s permissions check: repaired_paths=%s console_exec_fixed=%s missing_paths=%s",
        root,
        stage_label,
        repaired,
        console_fixed,
        len(res.missing_paths),
    )
    print(
        f"Permissions {stage_label}: ok"
        + (f" (repaired_paths={repaired}" + (", console_exec_fixed=1" if console_fixed else "") + ")" if (repaired or console_fixed) else "")
    )


def _pre_upgrade_permissions_check(config: AgentConfig, root: str) -> None:
    _permissions_check(config, root, stage_label="pre-upgrade")


def _active_php_fpm_services() -> list[str]:
    proc = subprocess.run(
        [
            "systemctl",
            "list-units",
            "--type",
            "service",
            "--state",
            "active",
            "--plain",
            "--no-legend",
            "php*-fpm.service",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        name = raw.strip().split(None, 1)[0].strip()
        if name.endswith(".service"):
            out.append(name)
    return out


def _curl_probe(url: str, *, resolve_local_host: str | None = None, follow_redirects: bool = True, timeout_sec: int = 20) -> UpgradeProbeResult:
    cmd = ["curl", "-ksS", "--max-time", str(max(5, int(timeout_sec)))]
    if follow_redirects:
        cmd.append("-L")
    if resolve_local_host:
        cmd.extend(["--resolve", f"{resolve_local_host}:443:127.0.0.1"])
    cmd.extend(["-o", "/dev/null", "-D", "-", url])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    body = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = body or f"curl rc={proc.returncode}"
        return UpgradeProbeResult(False, f"curl failed for {url}", detail)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    status_line = next((ln for ln in lines if ln.startswith("HTTP/")), "")
    if not status_line:
        return UpgradeProbeResult(False, f"probe returned no HTTP status for {url}", body[:600])
    m = re.search(r"^HTTP/\S+\s+(\d{3})\b", status_line)
    if not m:
        return UpgradeProbeResult(False, f"probe returned malformed HTTP status for {url}", status_line)
    code = int(m.group(1))
    if 200 <= code < 400:
        return UpgradeProbeResult(True, f"{url} -> HTTP {code}", status_line)
    return UpgradeProbeResult(False, f"{url} -> HTTP {code}", body[:600])


def _verify_nginx_and_php_services() -> None:
    proc = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
    nginx_state = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        print(f"Post-check: nginx inactive ({nginx_state or 'unknown'}), attempting start")
        start = subprocess.run(["systemctl", "start", "nginx"], capture_output=True, text=True)
        if start.returncode != 0:
            raise RuntimeError(f"Post-check failed: cannot start nginx: {(start.stderr or start.stdout or '').strip()}")
        proc = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
        nginx_state = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"Post-check failed: nginx still inactive after start: {nginx_state or 'unknown'}")
        print("Post-check: nginx auto-start OK")
    else:
        print("Post-check: nginx service OK")

    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        raise RuntimeError(f"Post-check failed: nginx -t: {(test.stderr or test.stdout or '').strip()}")
    print("Post-check: nginx -t OK")

    php_services = _active_php_fpm_services()
    if not php_services:
        raise RuntimeError("Post-check failed: no active php-fpm service found")
    print("Post-check: php-fpm active: " + ", ".join(php_services))


def _best_probe_domain(inst: MauticInstall) -> str:
    candidates: list[str] = []
    if str(inst.primary_domain or "").strip():
        candidates.append(str(inst.primary_domain or "").strip())
    for value in list(inst.domains or []):
        domain = str(value or "").strip()
        if domain and domain not in candidates:
            candidates.append(domain)
    fallback = _site_url_domain_from_install(inst)
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    return candidates[0] if candidates else ""


def _domain_from_site_url(site_url: str | None) -> str:
    value = str(site_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
    except Exception:
        return ""
    host = str(parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "_"} or "*" in host:
        return ""
    return host


def _site_url_domain_from_install(inst: MauticInstall) -> str:
    candidates: list[Path] = []
    if str(inst.local_php_path or "").strip():
        candidates.append(Path(str(inst.local_php_path)))
    root = Path(str(inst.root or ""))
    candidates.extend([root / "config" / "local.php", root / "app" / "config" / "local.php"])
    if root.name.lower() in {"public", "docroot", "public_html"}:
        candidates.extend([root.parent / "config" / "local.php", root.parent / "app" / "config" / "local.php"])
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        try:
            domain = _domain_from_site_url(parse_local_php(str(path)).get("site_url", ""))
        except Exception:
            domain = ""
        if domain:
            return domain
    return ""


def _post_upgrade_verify(config: AgentConfig, inst: MauticInstall) -> None:
    _permissions_check(config, inst.root, stage_label="post-upgrade")
    _verify_nginx_and_php_services()
    domain = _best_probe_domain(inst)
    if not domain:
        print("Post-check: no instance domain detected, skipping local/external HTTP probes")
        return

    local = _curl_probe(f"https://{domain}/", resolve_local_host=domain, follow_redirects=True, timeout_sec=20)
    if not local.ok:
        raise RuntimeError(f"Post-check failed: local origin probe: {local.summary}; {local.detail}".strip())
    print("Post-check: local origin OK: " + local.summary)

    external = _curl_probe(f"https://{domain}/", follow_redirects=True, timeout_sec=25)
    if not external.ok:
        raise RuntimeError(f"Post-check failed: external probe: {external.summary}; {external.detail}".strip())
    print("Post-check: external HTTPS OK: " + external.summary)


def _verify_assetmapper_upgrade(
    config: AgentConfig,
    inst: MauticInstall,
    *,
    project_root: str,
    target: str,
    rollback_available: bool = True,
) -> None:
    if _parse_semver(target)[0] != 7:
        return
    evidence = verify_assetmapper_upgrade(
        project_root=project_root,
        install_root=inst.root,
        console_path=inst.console_path,
        php_bin=config.php_bin,
        runtime_user=config.mautic_run_as_user or "www-data",
        domain=_best_probe_domain(inst),
        target_version=target,
        rollback_available=rollback_available,
    )
    print("MCD_ASSETMAPPER_VERIFICATION=" + json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    if evidence.get("status") != "success":
        raise RuntimeError("AssetMapper upgrade verification failed; rollback is required")


def run_upgrade_check(config: AgentConfig, root: str | None) -> int:
    inst = _pick_install_record(config, root)
    if str(getattr(inst, "runtime", "host") or "host").strip().lower() == "docker":
        print(f"root={inst.root}")
        print("runtime=docker")
        print("next=image-managed")
        return 0
    install_root, console = inst.root, inst.console_path
    version_evidence = read_mautic_version_evidence_read_only(install_root)
    version_source = str(version_evidence.get("source") or "unavailable_read_only")
    authoritative = version_source == "static_metadata"
    current = str(version_evidence.get("version") or "") if authoritative else "unknown"
    target = _latest_same_branch(config, current) if authoritative else None
    branch = _release_family_label(current) if authoritative else ""
    evidence = {
        "schema": "mcd-mautic-upgrade-version-evidence-v1",
        "root": install_root,
        "current_version": current if authoritative else None,
        "version_source": version_source,
        "authoritative": authoritative,
        "status": "ready" if authoritative else "needs_attention",
        "target_version": target or "",
    }
    if version_source != "static_metadata":
        evidence["reason"] = "authoritative on-disk Mautic version evidence is unavailable"
    print("MCD_UPGRADE_VERSION_EVIDENCE=" + json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}")
    if target is None:
        print("next=none")
    else:
        print(f"next={target}")
    return 0 if authoritative else 1


def _ensure_mautic7_locale_fix(config: AgentConfig, root: str) -> None:
    """Install/update Locale Fix and force only the two Mautic 7 safeguards."""
    run_plugins_interactive(
        config=config,
        root=root,
        selection=None,
        bundles=[MAUTIC7_LOCALE_FIX_BUNDLE],
        plugin_uids=None,
        action="auto",
        no_color=True,
        yes=True,
    )
    refreshed = _pick_install_record(config, root)
    if int(refreshed.mautic_major or 0) != 7:
        raise RuntimeError("Mautic Locale Fix activation requires Mautic 7")
    rc, out = execute_mautic_command_template(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=root,
        template=(
            "mautic:locale-fix:configure --published=1 "
            "--gmail-image-proxy-open=1 --no-interaction"
        ),
        timeout_sec=config.command_timeout_sec,
    )
    if rc != 0:
        raise RuntimeError(f"Mautic Locale Fix activation failed: {out}")
    rc, out = execute_mautic_command_template(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=root,
        template="cache:clear",
        timeout_sec=config.command_timeout_sec,
    )
    if rc != 0:
        raise RuntimeError(f"Mautic Locale Fix cache clear failed: {out}")
    print("Mautic 7 Locale Fix ready: published=1 gmail_image_proxy_open=1; other settings preserved")


@stable_source_operation
def run_upgrade_apply(
    *,
    config: AgentConfig,
    root: str | None,
    mode: str,
    yes: bool,
    do_backup: bool,
    with_system_upgrade: bool,
    target_override: str | None = None,
    allow_minor: bool = False,
    allow_major: bool = False,
    patch_plan_json: str | None = None,
    patch_run_id: str | None = None,
    repair_plan_json: str | None = None,
    repair_auth_context_file: str | None = None,
    patch_backup_context_file: str | None = None,
    repair_auth_key_file: str = "/etc/mcd/mcc-operation-signing.key",
    mcc_preflighted_single_instance: bool = False,
    allow_release_transition: bool = False,
    mcc_release_authorization_context_file: str = "",
    mcc_release_authorization_context_sha256: str = "",
    prepared_target_json: str | None = None,
    prepared_target_file: str | None = None,
    prepared_target_sha256: str = "",
) -> int:
    inst = _pick_install_record(config, root)
    if str(getattr(inst, "runtime", "host") or "host").strip().lower() == "docker":
        raise RuntimeError(
            "Docker Mautic upgrades are image-managed; activate a new platform image instead"
        )
    install_root, console = inst.root, inst.console_path
    current = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
    patch_resolution: dict[str, Any] | None = None
    validated_patch_plan: dict[str, Any] | None = None
    patch_evidence_started = datetime.now(timezone.utc).isoformat()
    patch_evidence_outcomes: list[dict[str, Any]] = []
    patch_evidence_failure: dict[str, Any] | None = None
    patch_evidence_reported = False
    target = _clean_target_version(target_override)
    if mcc_preflighted_single_instance and not allow_release_transition:
        from mcd_agent.mautic_manual_upgrade import ManualUpgradePreflightError, validate_preflighted_single_instance
        from mcd_agent.mautic_patch_plan import rejected_preflight

        def validate_manual_invocation(source_version: str) -> None:
            try:
                validate_preflighted_single_instance(
                    root=root, install_root=install_root, current=source_version,
                    target=target, mode=mode, raw_plan=patch_plan_json,
                    run_id=patch_run_id, yes=yes, allow_minor=allow_minor,
                    allow_major=allow_major, do_backup=do_backup, with_system_upgrade=with_system_upgrade,
                )
            except ManualUpgradePreflightError as exc:
                evidence = rejected_preflight(patch_run_id, exc.reason)
                print(
                    "MCD_PATCH_PLAN_EVIDENCE="
                    + json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                )
                raise

        validate_manual_invocation(current)
    if not target:
        target = _latest_same_branch(config, current)
    if not target:
        print(f"No upgrade target for current version {current}")
        return 0
    cross_line = _branch_key(current) != _branch_key(target)
    manual_system_upgrade = bool(mcc_preflighted_single_instance and not cross_line and with_system_upgrade)
    release_context = None
    requirements = {}
    external_backup_verified = False
    if cross_line:
        if not allow_release_transition or not yes or not root or root == "all":
            raise RuntimeError("Cross-line upgrade requires explicit single-instance release transition authorization")
        if _parse_semver(current) == (0, 0, 0) or _parse_semver(target) <= _parse_semver(current):
            raise RuntimeError("Invalid release transition versions")
        from mcd_agent.mautic_release_authorization import read_context, authorize
        from mcd_agent.mautic_patch_plan import parse_plan
        from mcd_agent.mautic_patch_resolution import canonical_json_sha256
        from mcd_agent.mautic_patch_stage import application_root
        authorization_plan = parse_plan(patch_plan_json or "")
        if (authorization_plan.get("run_id") != patch_run_id or authorization_plan.get("source_version") != current
                or authorization_plan.get("target_version") != target or authorization_plan.get("operation") != "apply"
                or authorization_plan.get("trigger") != "upgrade_lifecycle"
                or authorization_plan.get("phase") != "dependency_update_preflight"):
            raise RuntimeError("Release transition immutable plan binding mismatch")
        release_context = read_context(mcc_release_authorization_context_file, mcc_release_authorization_context_sha256)
        actual_mode = detect_install_type(install_root) if mode == "auto" else mode
        expected_release_binding = {
            "instance_uid": inst.instance_uid, "application_root": str(application_root(install_root)),
            "source_version": current, "target_version": target, "source_line": _branch_key(current),
            "target_line": _branch_key(target), "install_type": actual_mode, "phase": "upgrade", "operation": "apply",
            "patch_run_id": patch_run_id, "plan_sha256": canonical_json_sha256(authorization_plan),
        }
        if authorization_plan.get("install_type") != actual_mode:
            raise RuntimeError("Release transition install binding mismatch")
        def require_live_release_authorization():
            authorize(config, release_context, mcc_release_authorization_context_sha256, expected_release_binding)
        require_live_release_authorization()
        requirements = release_context["transition_requirements"]
        from mcd_agent import __version__
        minimum = requirements["minimum_agent_version"]
        if requirements["requires_backup"] and not do_backup:
            from mcd_agent.mautic_external_backup import release_backup_satisfied
            external_backup_verified = release_backup_satisfied(
                do_backup=False,
                requires_json_repair=requirements["requires_json_repair"],
                repair_plan_json=repair_plan_json,
                repair_auth_context_file=repair_auth_context_file,
                repair_auth_key_file=repair_auth_key_file,
                instance_uid=inst.instance_uid,
                root=install_root,
                source_version=current,
                target_version=target,
            )
        if ((minimum and _parse_semver(__version__) < _parse_semver(minimum))
                or actual_mode not in requirements["install_types"] or "upgrade" not in requirements["phases"]
                or (requirements["requires_backup"] and not (do_backup or external_backup_verified))
                or (with_system_upgrade and not requirements["system_upgrade_supported"])
                or (requirements["requires_json_repair"] and (not repair_plan_json or not repair_auth_context_file))):
            raise RuntimeError("Release transition prerequisites are not satisfied")
    elif not _ensure_upgrade_target_allowed(current, target):
        print(f"No upgrade target for current version {current}")
        return 0

    if not cross_line and not mcc_preflighted_single_instance:
        _require_release_approval(config, target, current)
    chosen_mode = mode
    if chosen_mode == "auto":
        chosen_mode = detect_install_type(install_root)
    prepared_target = _load_prepared_target_input(
        config=config,
        raw_json=prepared_target_json,
        file_path=prepared_target_file,
        input_sha256=prepared_target_sha256,
        target=target,
        mode=mode,
    )
    prepared_rollback_backup: Path | None = None
    if prepared_target is not None:
        # A prepared artifact is a complete source replacement. Keep a local
        # rollback point even when the operator did not request the optional
        # ordinary backup, because migration/health failures must restore the
        # exact pre-apply tree.
        chosen_mode = prepared_target.input["install_type"]
    if manual_system_upgrade:
        _assert_php84_host_safe(config, install_root)
    if requirements.get("database_compatibility") == "mautic7":
        database_ok, database_reason = mautic7_database_compatibility(_database_state())
        if not database_ok:
            raise RuntimeError("Mautic 6 to 7 upgrade is blocked: " + database_reason)
        print("Mautic 7 database preflight: " + database_reason)

    if requirements.get("requires_json_repair"):
        preflight_rc = run_upgrade_preflight(
            config=config,
            root=install_root,
            mode=chosen_mode,
            target_override=target,
            repair_plan_json=repair_plan_json,
            with_system_upgrade=with_system_upgrade,
        )
        if preflight_rc != 0:
            raise RuntimeError("Mautic upgrade preflight rejected")

    print(f"Upgrade plan: {current} -> {target} (mode={chosen_mode})")
    if not mcc_preflighted_single_instance and not patch_plan_json and not patch_run_id and getattr(config, "mcc_url", ""):
        from mcd_agent.mautic_patch_resolution import resolve_plan
        selected = resolve_plan(config, inst, trigger="upgrade_lifecycle", phase="dependency_update_preflight",
                                operation="apply", observed_version=current, observed_major=_parse_semver(current)[0],
                                target_version=target, install_type=chosen_mode)
        if selected["status"] == "blocked":
            raise RuntimeError("Mautic catalog upgrade resolution blocked: " + str(selected.get("reason")))
        if selected["status"] == "selected":
            patch_resolution = selected
            patch_plan_json = json.dumps(selected["plan"], ensure_ascii=True, separators=(",", ":"))
            patch_run_id = selected["plan"]["run_id"]
    patch_hook = None
    target_stage = None
    patch_facts_provider = None
    has_patch_plan_input = bool(patch_plan_json or patch_run_id)
    if has_patch_plan_input:
        if not patch_plan_json or not patch_run_id:
            raise RuntimeError("Atomic Mautic patch stage requires --patch-plan-json and --patch-run-id")
        from mcd_agent.mautic_patch_plan import (
            PatchPlanError,
            atomic_preflight,
            rejected_preflight,
            validate_upgrade_plan,
        )

        try:
            validated_patch_plan = validate_upgrade_plan(patch_plan_json, current, target, chosen_mode)
            if validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3":
                if validated_patch_plan["trigger"] != "upgrade_lifecycle" or validated_patch_plan["operation"] != "apply" or validated_patch_plan["run_id"] != patch_run_id:
                    raise PatchPlanError("upgrade_patch_context_mismatch")
                from mcd_agent.mautic_patch_fact_binding import needs_facts, bound_provider
                if needs_facts(validated_patch_plan):
                    patch_facts_provider = bound_provider(config, install_root)
                from mcd_agent.mautic_patch_backup import required, load_and_validate
                if required(validated_patch_plan):
                    if not patch_backup_context_file:
                        raise PatchPlanError("patch_backup_attestation_required")
                    try:
                        load_and_validate(patch_backup_context_file, key_path=repair_auth_key_file,
                                          plan=validated_patch_plan, instance_uid=inst.instance_uid, root=install_root)
                    except (OSError, RepairAuthorizationError) as exc:
                        raise PatchPlanError(str(exc)) from exc
        except PatchPlanError as exc:
            evidence = rejected_preflight(patch_run_id, str(exc))
            print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
            raise RuntimeError(f"Mautic patch preflight rejected: {exc}") from exc

        if validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3" and patch_resolution is None:
            patch_resolution = _patch_resolution_for_upgrade_evidence(
                config, inst, validated_patch_plan, current=current, target=target,
                mode=chosen_mode, run_id=patch_run_id,
            )

        def patch_hook(source_root: str) -> None:
            nonlocal patch_evidence_failure
            try:
                if target_stage is not None:
                    from mcd_agent.mautic_patch_plan import _target_version
                    from mcd_agent.mautic_patch_stage import application_root
                    staged_evidence = target_stage.verify_live(source_root, validated_patch_plan, _target_version)
                    print("MCD_PATCH_TARGET_EVIDENCE=" + json.dumps(staged_evidence, sort_keys=True))
                    source_root = str(application_root(source_root))
                if validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3":
                    # Runtime execution evaluates each selected patch's own
                    # condition; the executor has no global DB admission.
                    evidence = staged_evidence
                else:
                    evidence = atomic_preflight(source_root, patch_plan_json, patch_run_id, facts_provider=patch_facts_provider)
            except PatchPlanError as exc:
                evidence = rejected_preflight(patch_run_id, str(exc))
            print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
            if evidence.get("status") != "success":
                raise RuntimeError(f"Mautic patch preflight rejected: {evidence.get('reason', 'unknown')}")
            if validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3":
                apply_patch_phase(source_root, "post_source_install")
                # New sources are installed while maintenance still owns
                # scheduler/cron quiescence. Admit runtime-consumer patches
                # before scripts or any managed consumer can use this target.
                # Campaign-trigger patches have their own barrier at the
                # trigger-consumer handoff, intentionally distinct from the
                # broader campaign-execution phase.
                apply_patch_phase(source_root, "before_campaign_trigger")
                apply_patch_phase(source_root, "before_campaign_execution")
                apply_patch_phase(source_root, "before_background_imports")

        def apply_patch_phase(source_root: str, phase: str) -> None:
            nonlocal patch_evidence_failure
            from mcd_agent.mautic_patch_plan import execute
            from mcd_agent.mautic_patch_stage import application_root
            source_root = str(application_root(source_root))
            if patch_facts_provider is not None:
                from mcd_agent.mautic_patch_plan_v3 import _observe_facts
                # Admission belongs to runtime, after the standard updater
                # installed the target. Execution still checks fresh drift.
                target_stage.facts_receipt = _observe_facts(
                    validated_patch_plan, patch_facts_provider, "apply", phase)
            try:
                result = execute(source_root, patch_plan_json, phase, patch_run_id, facts_provider=patch_facts_provider,
                                 accepted_facts=getattr(target_stage, "facts_receipt", None))
            except Exception as exc:
                patch_evidence_failure = getattr(exc, "evidence", None) or {
                    "failure": {"id": "", "gates": []}, "completed": list(patch_evidence_outcomes),
                }
                raise
            print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps(result, sort_keys=True))
            for row in result.get("patches", []) if isinstance(result, dict) else []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                patch_evidence_outcomes[:] = [item for item in patch_evidence_outcomes if item.get("id") != row.get("id")]
                patch_evidence_outcomes.append(row)
            if result.get("status") != "success":
                raise RuntimeError("Mautic patch phase rejected: " + phase)

        if validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3":
            supported_phases = {"post_source_install", "before_campaign_trigger", "before_cache_warmup", "before_asset_generation", "preflight_frontend_assets", "before_doctrine_migrations", "before_campaign_execution", "before_background_imports"}
            if any(set(record["phases"]) - supported_phases for record in validated_patch_plan["patches"]):
                raise RuntimeError("Selected upgrade plan contains a phase unavailable in this upgrade workflow")
            try:
                target_stage = _prepare_patch_target_stage(config, _resolve_composer_project_root(install_root) if chosen_mode == "composer" else install_root,
                                                           current, target, chosen_mode, validated_patch_plan, facts_provider=patch_facts_provider,
                                                           prepared_target=prepared_target)
            except Exception as exc:
                patch_evidence_failure = getattr(exc, "evidence", None) or {
                    "failure": {"id": "", "gates": []}, "completed": list(patch_evidence_outcomes),
                }
                if not patch_evidence_reported:
                    _report_upgrade_patch_evidence(
                        config, inst, patch_resolution, validated_patch_plan,
                        started_at=patch_evidence_started, status="error",
                        outcomes=patch_evidence_failure.get("completed", patch_evidence_outcomes)
                        if isinstance(patch_evidence_failure, dict) else patch_evidence_outcomes,
                        failure=patch_evidence_failure.get("failure") if isinstance(patch_evidence_failure, dict) else None,
                    )
                    patch_evidence_reported = True
                raise
            patch_hook.target_stage = target_stage
            patch_hook.apply_phase = apply_patch_phase
    if not yes:
        ans = input("Proceed? [y/N]: ").strip().lower()
        if ans not in {"y", "yes"}:
            if target_stage is not None:
                target_stage.close()
            print("Cancelled")
            return 0

    try:
        if cross_line:
            require_live_release_authorization()
        guard = _enter_upgrade_maintenance(config)
    except Exception:
        if target_stage is not None:
            target_stage.close()
        raise
    try:
        if target_stage is not None:
            from mcd_agent.mautic_patch_plan import _target_version
            target_stage.verify_original(_resolve_composer_project_root(install_root) if chosen_mode == "composer" else install_root,
                                         validated_patch_plan, _target_version)
        if cross_line:
            require_live_release_authorization()
        if mcc_preflighted_single_instance and not cross_line:
            # Maintenance admission may take time. Recheck before permissions
            # alignment or reverting a core patch, which also mutate source.
            validate_manual_invocation(
                _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
            )
        # Mandatory preflight: align permissions before any upgrade action.
        _pre_upgrade_permissions_check(config, install_root)

        # This hotfix changes a core file. Restore the exact
        # verified original before any version change so Composer/ZIP updates
        # never inherit a local patch into a new Mautic release.
        if _parse_semver(current)[0] == 7 and current != target:
            restore = {"status": "clean", "reason": "source_upgrade_uses_catalog_plan"}
            if str(restore.get("status", "")).strip().lower() == "error":
                raise RuntimeError(
                    "Mautic import tag remediation rollback failed: "
                    + str(restore.get("reason", "unknown"))
                )
            print("Mautic import tag remediation rollback: " + str(restore.get("status", "clean")))

        if do_backup or prepared_target is not None:
            b = _backup_install(install_root)
            if prepared_target is not None:
                prepared_rollback_backup = Path(b)
            print(f"Backup created: {b}")

        if requirements.get("requires_json_repair"):
            repair_run_id = "json-repair-" + uuid4().hex
            if not repair_plan_json or not repair_auth_context_file:
                repair_evidence = {
                    "schema": "mcd-mautic-json-schema-repair-execution-v1",
                    "status": "needs_attention",
                    "run_id": repair_run_id,
                    "root": install_root,
                    "source_version": current,
                    "target_version": target,
                    "reason": "authorized backup context and repair plan are required",
                }
                print("MCD_JSON_REPAIR_EVIDENCE=" + json.dumps(repair_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                raise RuntimeError("Mautic JSON repair authorization is required")
            try:
                context, signing_key = load_authorization_context(
                    repair_auth_context_file,
                    key_path=repair_auth_key_file,
                )
                repair_evidence = execute_json_schema_repair(
                    root=install_root,
                    current_version=current,
                    target_version=target,
                    repair_plan_json=repair_plan_json,
                    authorization_context=context,
                    signing_key=signing_key,
                    instance_uid=inst.instance_uid,
                    run_id=repair_run_id,
                    local_php_path=inst.local_php_path,
                )
            except (OSError, RepairAuthorizationError) as exc:
                repair_evidence = {
                    "schema": "mcd-mautic-json-schema-repair-execution-v1",
                    "status": "needs_attention",
                    "run_id": repair_run_id,
                    "root": install_root,
                    "source_version": current,
                    "target_version": target,
                    "reason": "authorized MCC context could not be loaded",
                    "error_type": type(exc).__name__,
                }
            print("MCD_JSON_REPAIR_EVIDENCE=" + json.dumps(repair_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            if repair_evidence.get("status") != "success":
                raise RuntimeError("Mautic JSON schema repair rejected or failed")

        if prepared_target is not None:
            if cross_line:
                require_live_release_authorization()
            elif not mcc_preflighted_single_instance:
                _require_release_approval(config, target, current)
            _apply_prepared_target(config, install_root, console, config.php_bin, target, prepared_target, patch_hook)
        elif chosen_mode == "zip":
            if cross_line:
                require_live_release_authorization()
            elif not mcc_preflighted_single_instance:
                _require_release_approval(config, target, current)
            _apply_zip(config, install_root, console, config.php_bin, target, patch_hook)
        elif chosen_mode == "composer":
            if cross_line:
                require_live_release_authorization()
            elif not mcc_preflighted_single_instance:
                _require_release_approval(config, target, current)
            _apply_composer(install_root, console, config.php_bin, current, target, patch_hook)
        else:
            raise RuntimeError(f"Unsupported mode: {mode}")

        _verify_assetmapper_upgrade(
            config,
            inst,
            project_root=_resolve_composer_project_root(install_root),
            target=target,
            rollback_available=do_backup or external_backup_verified,
        )

        # Restore transport dependencies for API senders after upgrade
        # (especially relevant for zip installs where update flow may drop composer deps).
        if prepared_target is None:
            ensure_mailer_packages_for_sender_config(
                config=config,
                root=install_root,
                console_path=console,
                reason="mautic-upgrade-sender-restore",
            )

            ensure_amazon_mailer_for_bundles(
                config=config,
                root=install_root,
                console_path=console,
                bundles=installed_required_bundles(install_root),
                reason="mautic-upgrade",
            )

        if prepared_target is None and _parse_semver(current)[0] != 7 and _parse_semver(target)[0] == 7:
            _ensure_mautic7_locale_fix(config, install_root)

        final_version = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
        if _parse_semver(final_version) != _parse_semver(target):
            raise RuntimeError(f"Post-check failed: Mautic version is {final_version}, expected {target}")
        if prepared_target is not None:
            print(
                "MCD_PREPARED_TARGET_EVIDENCE="
                + json.dumps(
                    {
                        "schema": "mcd-mautic-prepared-target-receipt-v1",
                        "status": "success",
                        "prepared_target_id": prepared_target.input["prepared_target_id"],
                        "target_version": target,
                        "artifact_sha256": prepared_target.artifact_sha256,
                        "host_composer_invoked": False,
                        "rollback_backup": str(prepared_rollback_backup or ""),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
        if target_stage is not None:
            from mcd_agent.mautic_patch_stage import application_root
            from mcd_agent.mautic_patch_plan_v3 import verify_applied
            verification = verify_applied(str(application_root(install_root)), validated_patch_plan,
                                          accepted_facts=getattr(target_stage, "facts_receipt", None))
            if not patch_evidence_reported:
                _report_upgrade_patch_evidence(
                    config, inst, patch_resolution, validated_patch_plan,
                    started_at=patch_evidence_started, status="success",
                    outcomes=verification.get("patches", []) if isinstance(verification, dict) else [],
                    rollback={"available": bool(patch_evidence_outcomes), "attempted": False, "succeeded": False},
                )
                patch_evidence_reported = True
        import_patch = {"status": "already", "reason": "runtime_reconciliation_uses_catalog_plan"}
        if import_patch.get("status") == "error":
            raise RuntimeError("Import tag patch after upgrade failed: " + str(import_patch.get("reason")))
        if manual_system_upgrade:
            from mcd_agent.mautic_patch_resolution import canonical_json_sha256
            system_evidence = {
                "schema": "mcd-mautic-manual-system-upgrade-execution-v1",
                "status": "running",
                "requested": True,
                "target_php": "8.4",
                "binding": {
                    "instance_uid": inst.instance_uid,
                    "root": install_root,
                    "source_version": current,
                    "target_version": target,
                    "patch_run_id": patch_run_id or "",
                    "plan_sha256": canonical_json_sha256(validated_patch_plan),
                    "backup_path": str(b),
                },
                "host_compatibility": {"ok": True, "reason": "all discovered host instances are PHP 8.4 compatible"},
                "observed": {"cli_version": "", "fpm_version": "", "nginx_socket": []},
                "rollback": {"attempted": False, "succeeded": False},
            }
            try:
                _apply_system_upgrade(current, target, config=config, upgraded_root=install_root)
                config = replace(config, php_bin=rebind_php_after_system_upgrade(config.php_bin))
                system_evidence["observed"] = _php84_runtime_observation()
                system_evidence["status"] = "success"
            except Exception as exc:
                system_evidence.update(status="error", reason=str(exc), error_type=type(exc).__name__)
                print("MCD_MANUAL_SYSTEM_UPGRADE_EVIDENCE=" + json.dumps(system_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                raise
            print("MCD_MANUAL_SYSTEM_UPGRADE_EVIDENCE=" + json.dumps(system_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        elif with_system_upgrade:
            _apply_system_upgrade(current, target, config=config, upgraded_root=install_root)
            config = replace(config, php_bin=rebind_php_after_system_upgrade(config.php_bin))
        _post_upgrade_verify(config, inst)
        cache_count = _write_upgrade_version_cache(install_root, final_version)
        print(f"Mautic version cache refreshed: {final_version} ({cache_count} path(s))")
        print(f"Upgrade completed: {current} -> {final_version}")
    except Exception:
        rollback_state = {"available": bool(patch_evidence_outcomes), "attempted": False, "succeeded": False}
        if target_stage is not None:
            from mcd_agent.mautic_patch_plan_v3 import rollback as rollback_v3
            try:
                from mcd_agent.mautic_patch_stage import application_root
                rollback_evidence = rollback_v3(str(application_root(_resolve_composer_project_root(install_root) if chosen_mode == "composer" else install_root)),
                                                dict(validated_patch_plan, operation="rollback"), facts_provider=patch_facts_provider)
                print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps(rollback_evidence, sort_keys=True))
                rollback_state = {"available": True, "attempted": True, "succeeded": True}
            except RuntimeError as rollback_error:
                print("MCD_PATCH_PLAN_EVIDENCE=" + json.dumps({"status": "error", "rollback_succeeded": False, "reason": str(rollback_error)}, sort_keys=True))
                rollback_state = {"available": True, "attempted": True, "succeeded": False}
        if prepared_rollback_backup is not None:
            try:
                _restore_prepared_target_backup(install_root, prepared_rollback_backup)
                print("MCD_PREPARED_TARGET_EVIDENCE=" + json.dumps({
                    "schema": "mcd-mautic-prepared-target-receipt-v1",
                    "status": "rolled_back",
                    "prepared_target_id": prepared_target.input["prepared_target_id"],
                    "host_composer_invoked": False,
                    "rollback_backup": str(prepared_rollback_backup),
                }, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)
            except Exception as prepared_rollback_error:
                print("MCD_PREPARED_TARGET_EVIDENCE=" + json.dumps({
                    "schema": "mcd-mautic-prepared-target-receipt-v1",
                    "status": "rollback_failed",
                    "prepared_target_id": prepared_target.input["prepared_target_id"],
                    "host_composer_invoked": False,
                    "reason": type(prepared_rollback_error).__name__,
                }, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)
                raise
        if validated_patch_plan is not None and validated_patch_plan.get("schema") == "mcd-mautic-patch-plan-v3" and not patch_evidence_reported:
            failure = patch_evidence_failure.get("failure") if isinstance(patch_evidence_failure, dict) else None
            completed = patch_evidence_failure.get("completed") if isinstance(patch_evidence_failure, dict) else None
            _report_upgrade_patch_evidence(
                config, inst, patch_resolution, validated_patch_plan,
                started_at=patch_evidence_started, status="error",
                outcomes=completed if isinstance(completed, list) else patch_evidence_outcomes,
                failure=failure if isinstance(failure, dict) else None,
                rollback=rollback_state,
            )
            patch_evidence_reported = True
        try:
            _exit_upgrade_maintenance(config, guard)
        except Exception as cleanup_error:
            print(f"WARN maintenance cleanup failed after upgrade error: {cleanup_error}")
        raise
    finally:
        if target_stage is not None:
            target_stage.close()
        if prepared_target is not None:
            prepared_target.close()

    _exit_upgrade_maintenance(config, guard)
    return 0


def run_upgrade_interactive(config: AgentConfig, root: str | None) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
    target_next = _latest_same_branch(config, current)
    branch = _release_family_label(current)
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}")
    if not target_next:
        print("next=none")
        return 0
    print(f"latest_patch={target_next}")
    print("")
    print("Select target:")
    print("1. latest release in current family (recommended)")
    print("0. exit")
    t_choice = _ask("Target [1/0, default 1]: ").strip() or "1"
    if t_choice == "0":
        print("Cancelled")
        return 0
    target = target_next
    if not target:
        print("No valid target selected")
        return 0
    print("")
    print("Select upgrade mode:")
    print("1. auto (recommended)")
    print("2. zip")
    print("3. composer")
    print("0. exit")
    choice = _ask("Mode [1/2/3/0, default 1]: ").strip() or "1"
    if choice == "0":
        print("Cancelled")
        return 0
    mode_map = {"1": "auto", "2": "zip", "3": "composer"}
    mode = mode_map.get(choice, "auto")
    print("Backup note: MCD backup archives only the Mautic install directory.")
    backup = _ask("Create backup before upgrade? [Y/n]: ").strip().lower() not in {"n", "no"}
    sys_up = False
    print("")
    print(f"Plan: {current} -> {target} (mode={mode}, backup={backup}, with_system_upgrade=false)")
    confirm = _ask("Apply now? [y/N, 0=exit]: ").strip().lower()
    if confirm in {"0", "x", "exit", "q"}:
        print("Cancelled")
        return 0
    if confirm not in {"y", "yes"}:
        print("Cancelled")
        return 0
    return run_upgrade_apply(
        config=config,
        root=install_root,
        mode=mode,
        yes=True,
        do_backup=backup,
        with_system_upgrade=sys_up,
        target_override=target,
    )


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""
from mcd_agent.mautic_json_repair import (
    RepairAuthorizationError,
    build_json_repair_plan,
    execute_json_schema_repair,
    issue_repair_authorization_context,
    load_authorization_context,
    repair_plan_digest,
)
