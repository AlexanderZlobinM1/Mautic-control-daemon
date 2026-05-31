from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.backup import backup_lock_active
from mcd_agent.cluster_routing import cluster_route_targets
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.state_backend import mysql_state_connection, mysql_state_enabled, mysql_state_table_names
from mcd_agent.version_identity import agent_version_payload, installed_agent_version

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_CAMPAIGN_CONSOLE_COMMANDS = (
    "mautic:campaigns:trigger",
    "mautic:campaign:trigger",
    "mautic:campaigns:rebuild",
    "mautic:campaign:rebuild",
    "mautic:campaigns:update",
)
_CLUSTER_UPDATE_SYNC_KEY = "mcd_update_cluster"
_CLUSTER_UPDATE_DOWNLOAD_LOCK_TTL_SEC = 900
_CLUSTER_UPDATE_INSTALL_LOCK_TTL_SEC = 900
_CLUSTER_UPDATE_HEALTH_STALE_SEC = 300


def _semver(v: str) -> tuple[int, int, int]:
    nums = [x for x in "".join(ch if ch.isdigit() else "." for ch in (v or "")).split(".") if x.isdigit()]
    while len(nums) < 3:
        nums.append("0")
    return (int(nums[0]), int(nums[1]), int(nums[2]))


def _state_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "mcd-self-update.json"


def _lock_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "mcd-self-update.lock"


def _read_state(cfg: AgentConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        return {}
    return {}


def _write_state(cfg: AgentConfig, payload: dict[str, Any]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _clear_active_campaign_process_state(state: dict[str, Any]) -> None:
    state.pop("active_campaign_processes", None)


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 10) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


def _update_policy(cfg: AgentConfig) -> str:
    p = (cfg.mcd_update_policy or "").strip().lower()
    if p in {"off", "lts", "approved", "test", "cluster"}:
        return p
    ch = (cfg.mcd_update_channel or "").strip().lower()
    if ch in {"stable", "approved"}:
        return "approved"
    if ch in {"rc", "test"}:
        return "test"
    if ch == "lts":
        return "lts"
    if ch == "cluster":
        return "cluster"
    if ch == "off":
        return "off"
    return "approved"


def _active_campaign_processes(timeout_sec: int = 4) -> list[dict[str, Any]]:
    stdout = ""
    has_elapsed = True
    for ps_args, elapsed_supported in (
        (["ps", "-eo", "pid=,etimes=,args="], True),
        (["ps", "-eo", "pid=,args="], False),
    ):
        try:
            proc = subprocess.run(
                ps_args,
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout_sec)),
            )
        except Exception:
            continue
        if proc.returncode == 0:
            stdout = proc.stdout or ""
            has_elapsed = elapsed_supported
            break
    if not stdout:
        return []

    current_pid = os.getpid()
    rows: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or "mautic:campaign" not in line:
            continue
        if "/bin/console" not in line and "/app/console" not in line:
            continue
        if not any(cmd in line for cmd in _CAMPAIGN_CONSOLE_COMMANDS):
            continue
        parts = line.split(None, 2 if has_elapsed else 1)
        if len(parts) < (3 if has_elapsed else 2):
            continue
        try:
            pid = int(parts[0])
            elapsed_sec = int(parts[1]) if has_elapsed else 0
        except Exception:
            continue
        if pid <= 0 or pid == current_pid:
            continue
        command = parts[2] if has_elapsed else parts[1]
        argv0 = command.split(None, 1)[0] if command else ""
        exe = Path(argv0).name.lower()
        # Only a running PHP console process blocks self-update. Wrappers
        # waiting on flock/timeout/sudo do not execute Mautic work and must not
        # prevent agent replacement.
        if not (exe == "php" or exe.startswith("php")):
            continue
        rows.append({"pid": pid, "elapsed_sec": elapsed_sec, "cmd": command[:500]})
    return rows


def _active_campaign_update_defer_message(rows: list[dict[str, Any]]) -> str:
    sample = ", ".join(
        f"pid={int(row.get('pid') or 0)} age={int(row.get('elapsed_sec') or 0)}s"
        for row in rows[:3]
    )
    suffix = f" ({sample})" if sample else ""
    return f"MCD update deferred: active campaign trigger/rebuild process is running{suffix}"


def check_with_mcc(cfg: AgentConfig, *, auto_update_enabled: bool) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "auto_update_enabled": bool(auto_update_enabled),
        "update_policy": _update_policy(cfg),
        "allow_test_build": bool(cfg.mcd_update_allow_test_build),
        "config_sha256": cfg.config_sha256,
    }
    payload.update(agent_version_payload())
    url = base + "/api/v1/agent/update/check"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def release_session(cfg: AgentConfig, session_id: str, *, result_status: str, result_message: str, new_version: str) -> None:
    base = _api_base(cfg)
    if not base or not session_id:
        return
    ident = resolve_agent_identity(cfg)
    payload = {
        "session_id": session_id,
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "result_status": result_status,
        "result_message": result_message,
        "new_version": new_version,
    }
    url = base + "/api/v1/agent/update/release"
    try:
        _post_json(url, payload, cfg.mcc_token, timeout_sec=8)
    except Exception:
        return


def _download_package(url: str, dst: Path, token: str | None) -> None:
    req = request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=30) as resp:
        dst.write_bytes(resp.read())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _update_archive_path(target: str) -> Path:
    return Path("/opt/mcd") / "var" / "updates" / f"mcd-agent-{target}.tar.gz"


def _ensure_update_archive(cfg: AgentConfig, plan: dict[str, Any]) -> Path:
    target = str(plan.get("target", "")).strip()
    package_url = str(plan.get("package_url", "")).strip()
    if not target or not package_url:
        raise RuntimeError("invalid plan: target/package_url is required")
    archive_path = _update_archive_path(target)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha = str(plan.get("sha256", "")).strip().lower()
    if archive_path.exists() and expected_sha:
        actual = _sha256_file(archive_path).lower()
        if actual == expected_sha:
            return archive_path
        archive_path.unlink(missing_ok=True)
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    _download_package(package_url, tmp_path, cfg.mcc_token)
    if expected_sha:
        actual = _sha256_file(tmp_path).lower()
        if actual != expected_sha:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"sha256 mismatch expected={expected_sha} actual={actual}")
    os.replace(tmp_path, archive_path)
    return archive_path


def _copytree_ignore_artifacts(_src_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lower = name.lower()
        if name.startswith("._") or name.startswith(".__"):
            ignored.add(name)
            continue
        if name in {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}:
            ignored.add(name)
            continue
        if lower in {".ds_store"}:
            ignored.add(name)
            continue
        if lower.startswith(".venv"):
            ignored.add(name)
            continue
    return ignored


def _extract_archive_to_dir(archive_path: Path, dst_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mcd-self-update-") as td:
        td_path = Path(td)
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(td_path)
        entries = [x for x in td_path.iterdir() if not x.name.startswith(".")]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else td_path
        if not (root / "mcd_agent").exists():
            raise RuntimeError("update archive does not contain mcd_agent package")
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(
            root,
            dst_dir,
            symlinks=True,
            ignore=_copytree_ignore_artifacts,
        )


def _install_requirements_for_staged_source(install_dir: Path, staged_src_dir: Path) -> None:
    req = staged_src_dir / "requirements.txt"
    if not req.exists():
        logging.info("MCD self-update: requirements.txt not found in staged source, skip dependency sync")
        return
    venv_python = install_dir / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"venv python not found: {venv_python}")
    cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-r",
        str(req),
    ]
    proc = subprocess.run(cmd, cwd="/", capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "pip install failed").strip()
        raise RuntimeError(f"dependency install failed: {detail}")


def _pre_switch_smoke_check(install_dir: Path, staged_src_dir: Path) -> None:
    venv_python = install_dir / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"venv python not found: {venv_python}")

    compile_proc = subprocess.run(
        [str(venv_python), "-m", "compileall", "-q", str(staged_src_dir)],
        capture_output=True,
        text=True,
    )
    if compile_proc.returncode != 0:
        detail = (compile_proc.stderr or compile_proc.stdout or "compileall failed").strip()
        raise RuntimeError(f"pre-switch smoke failed (compileall): {detail}")

    smoke_script = f"""
import importlib
import pathlib
import sys

root = pathlib.Path({str(staged_src_dir)!r})
sys.path.insert(0, str(root))
for mod in (
    "mcd_agent",
    "mcd_agent.config",
    "mcd_agent.backup",
    "mcd_agent.self_update",
    "mcd_agent.daemon",
    "mcd_agent.cli",
):
    importlib.import_module(mod)

backup = importlib.import_module("mcd_agent.backup")
if not hasattr(backup, "backup_lock_active"):
    raise RuntimeError("backup_lock_active is missing in staged mcd_agent.backup")
"""
    smoke_proc = subprocess.run(
        [str(venv_python), "-c", smoke_script],
        capture_output=True,
        text=True,
    )
    if smoke_proc.returncode != 0:
        detail = (smoke_proc.stderr or smoke_proc.stdout or "import smoke failed").strip()
        raise RuntimeError(f"pre-switch smoke failed (import): {detail}")


def _restart_service_async() -> None:
    # Detached restart: current process can finish response and be replaced by systemd.
    subprocess.Popen(
        ["bash", "-lc", "(sleep 1; systemctl restart mcd) >/dev/null 2>&1 &"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _acquire_update_lock(cfg: AgentConfig, *, blocking: bool) -> Any | None:
    lock_p = _lock_path(cfg)
    lock_p.parent.mkdir(parents=True, exist_ok=True)
    lock_f = lock_p.open("w", encoding="utf-8")
    try:
        if fcntl is not None:
            op = fcntl.LOCK_EX
            if not blocking:
                op |= fcntl.LOCK_NB
            fcntl.flock(lock_f.fileno(), op)
        return lock_f
    except Exception:
        try:
            lock_f.close()
        except Exception:
            pass
        return None


def _release_update_lock(lock_f: Any | None) -> None:
    if lock_f is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_f.close()
    except Exception:
        pass


def _safe_mtime(path: Path, fallback: int) -> int:
    try:
        return int(path.stat().st_mtime)
    except Exception:
        return fallback


def _remove_path(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _prune_entries(paths: list[Path], *, keep_count: int, max_age_sec: int, now_s: int) -> int:
    keep = max(0, int(keep_count))
    age_limit = max(86_400, int(max_age_sec))
    ordered = sorted(paths, key=lambda p: _safe_mtime(p, now_s), reverse=True)
    removed = 0
    for idx, p in enumerate(ordered):
        age_sec = max(0, now_s - _safe_mtime(p, now_s))
        if idx < keep and age_sec <= age_limit:
            continue
        if _remove_path(p):
            removed += 1
    return removed


def _cleanup_update_artifacts(
    cfg: AgentConfig,
    *,
    now_s: int | None = None,
    install_dir: Path | None = None,
) -> dict[str, int]:
    now = int(time.time()) if now_s is None else int(now_s)
    install_dir = install_dir or Path("/opt/mcd")
    updates_dir = install_dir / "var" / "updates"
    backup_dir = install_dir / "var" / "backup"

    keep_archives = max(0, int(cfg.mcd_update_keep_archives))
    keep_preupdate = max(0, int(cfg.mcd_update_keep_preupdate_backups))
    max_age_days = max(1, int(cfg.mcd_update_artifacts_max_age_days))
    max_age_sec = max_age_days * 86_400

    removed = {"archives": 0, "preupdate_backups": 0, "stale_dirs": 0}

    if updates_dir.exists():
        archives = [
            p
            for p in updates_dir.iterdir()
            if p.is_file()
            and p.name.startswith("mcd-agent-")
            and (p.name.endswith(".tar.gz") or p.name.endswith(".tgz") or p.name.endswith(".tar"))
        ]
        removed["archives"] = _prune_entries(archives, keep_count=keep_archives, max_age_sec=max_age_sec, now_s=now)

        stale_stage_dirs = [
            p
            for p in updates_dir.iterdir()
            if p.is_dir() and (p.name.startswith("src.next-") or p.name.startswith("src.prev-"))
        ]
        for p in stale_stage_dirs:
            # Keep very fresh staging dirs to avoid racing with non-MCD tooling.
            age_sec = max(0, now - _safe_mtime(p, now))
            if age_sec < 3600:
                continue
            if _remove_path(p):
                removed["stale_dirs"] += 1

    if backup_dir.exists():
        preupdate_backups = [
            p for p in backup_dir.iterdir() if p.is_file() and p.name.startswith("mcd-src-preupdate-") and p.name.endswith(".tgz")
        ]
        removed["preupdate_backups"] = _prune_entries(
            preupdate_backups,
            keep_count=keep_preupdate,
            max_age_sec=max_age_sec,
            now_s=now,
        )

    if any(v > 0 for v in removed.values()):
        logging.info(
            "MCD self-update cleanup: removed archives=%s preupdate_backups=%s stale_dirs=%s",
            removed["archives"],
            removed["preupdate_backups"],
            removed["stale_dirs"],
        )
    return removed


def _maybe_run_update_cleanup(cfg: AgentConfig, state: dict[str, Any], *, now_s: int) -> bool:
    if not bool(cfg.mcd_update_cleanup_enabled):
        return False
    interval_sec = max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
    next_cleanup_ts = int(state.get("next_cleanup_ts", 0) or 0)
    if now_s < next_cleanup_ts:
        return False

    lock_f = _acquire_update_lock(cfg, blocking=False)
    if lock_f is None:
        state["last_cleanup_status"] = "deferred_lock"
        state["next_cleanup_ts"] = now_s + min(300, interval_sec)
        return True
    try:
        removed = _cleanup_update_artifacts(cfg, now_s=now_s)
        state["last_cleanup_ts"] = now_s
        state["last_cleanup_status"] = "ok"
        state["last_cleanup_removed"] = removed
        state["next_cleanup_ts"] = now_s + interval_sec
        return True
    except Exception as e:
        state["last_cleanup_ts"] = now_s
        state["last_cleanup_status"] = f"failed:{e}"
        state["next_cleanup_ts"] = now_s + interval_sec
        logging.warning("MCD self-update cleanup failed: %s", e)
        return True
    finally:
        _release_update_lock(lock_f)


def _cluster_update_enabled(cfg: AgentConfig) -> bool:
    return bool(getattr(cfg, "cluster_id", None)) and _update_policy(cfg) == "cluster" and mysql_state_enabled(cfg)


def _dedupe_hosts(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _cluster_local_host_name(cfg: AgentConfig) -> str:
    # Prefer the MCC canonical host key for rolling cluster updates. Some older
    # cluster installs did not persist [mcc].host_name locally, but the cache
    # route is ordered by cluster_node_index and uses MCC host keys. Using the
    # local hostname in that case creates duplicate participants such as
    # host-37-... and ananas-xxl for the same node, which deadlocks the rollout.
    try:
        idx = int(getattr(cfg, "cluster_node_index", 0) or 0)
    except Exception:
        idx = 0
    if idx > 0:
        cache_hosts = cluster_route_targets(cfg, "cache")
        if 0 < idx <= len(cache_hosts):
            text = str(cache_hosts[idx - 1] or "").strip()
            if text:
                return text
    ident = resolve_agent_identity(cfg)
    for key in (
        "effective_mcc_host_name",
        "configured_host_name",
        "local_hostname",
        "effective_hostname",
    ):
        text = str(ident.get(key) or "").strip()
        if text:
            return text
    return "unknown-host"


def _cluster_expected_update_hosts(cfg: AgentConfig) -> list[str]:
    hosts: list[str] = []
    # Cache route normally lists all writable web nodes and preserves the operator-defined node order.
    hosts.extend(cluster_route_targets(cfg, "cache"))
    hosts.extend(cluster_route_targets(cfg, "cron"))
    hosts.extend(cluster_route_targets(cfg, "import"))
    # Do not include the backup/replica route in the rolling update quorum.
    # The replica intentionally may run a sqlite/read-only control state mode,
    # so it cannot safely write the Galera-backed update coordinator row.
    hosts.append(_cluster_local_host_name(cfg))
    return _dedupe_hosts(hosts)


def _cluster_sync_host_name(cfg: AgentConfig) -> str:
    cluster_id = str(getattr(cfg, "cluster_id", "") or "").strip() or "default"
    return f"__cluster__:{cluster_id}"


def _cluster_plan_from_decision(cfg: AgentConfig, decision: dict[str, Any], *, now_s: int) -> dict[str, Any] | None:
    status = str(decision.get("status", "")).strip().lower()
    if status not in {"update", "update_available"}:
        return None
    target = str(decision.get("target", "")).strip()
    package_url = str(decision.get("package_url", "")).strip()
    if not target or not package_url:
        return None
    if _semver(target) <= _semver(installed_agent_version()):
        return None
    return {
        "status": "update",
        "target": target,
        "package_url": package_url,
        "sha256": str(decision.get("sha256", "") or "").strip(),
        "session_id": str(decision.get("session_id", "") or "").strip(),
        "session_owner": _cluster_local_host_name(cfg),
        "created_at": now_s,
    }


def _cluster_local_update_blockers(cfg: AgentConfig) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if cfg.backup_enabled:
        try:
            if backup_lock_active(cfg):
                blockers.append({"kind": "backup_lock", "message": "backup lock is active"})
        except Exception as e:
            blockers.append({"kind": "backup_lock_probe_failed", "message": str(e)[:300]})
    if bool(getattr(cfg, "mcd_update_defer_during_campaigns", True)):
        campaigns = _active_campaign_processes()
        if campaigns:
            blockers.append(
                {
                    "kind": "active_campaigns",
                    "message": _active_campaign_update_defer_message(campaigns),
                    "processes": campaigns[:10],
                }
            )
    return blockers


def _cluster_payload_plan(payload: dict[str, Any]) -> dict[str, Any] | None:
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return None
    target = str(plan.get("target", "") or "").strip()
    package_url = str(plan.get("package_url", "") or "").strip()
    if not target or not package_url:
        return None
    out = dict(plan)
    out["status"] = "update"
    return out


def _cluster_update_lock_expired(lock: Any, *, now_s: int, ttl_sec: int) -> bool:
    if not isinstance(lock, dict):
        return True
    ts = float(lock.get("ts") or 0.0)
    if ts <= 0:
        return True
    return (now_s - ts) > max(60, int(ttl_sec))


def _cluster_update_mutate(cfg: AgentConfig, mutator: Any) -> Any:
    names = mysql_state_table_names(cfg)
    table = names["runtime_sync"]
    host_name = _cluster_sync_host_name(cfg)
    key = _CLUSTER_UPDATE_SYNC_KEY
    now_s = int(time.time())
    conn = mysql_state_connection(cfg)
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO `{table}`(host_name, `key`, payload_json, updated_at)
                VALUES(%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE updated_at=updated_at
                """,
                (host_name, key, "{}", now_s),
            )
            cur.execute(
                f"""
                SELECT payload_json
                FROM `{table}`
                WHERE host_name=%s AND `key`=%s
                FOR UPDATE
                """,
                (host_name, key),
            )
            row = cur.fetchone() or {}
            raw_payload = row.get("payload_json") if isinstance(row, dict) else None
            try:
                payload = json.loads(str(raw_payload or "{}"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            result = mutator(payload)
            payload["updated_at"] = now_s
            cur.execute(
                f"""
                UPDATE `{table}`
                SET payload_json=%s, updated_at=%s
                WHERE host_name=%s AND `key`=%s
                """,
                (json.dumps(payload, ensure_ascii=True, separators=(",", ":")), now_s, host_name, key),
            )
        conn.commit()
        return result
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cluster_update_seed_or_refresh(
    cfg: AgentConfig,
    payload: dict[str, Any],
    *,
    candidate_plan: dict[str, Any] | None,
    expected_hosts: list[str],
    local_host: str,
    blockers: list[dict[str, Any]],
    now_s: int,
) -> dict[str, Any] | None:
    current_plan = _cluster_payload_plan(payload)
    if candidate_plan is not None:
        current_target = str((current_plan or {}).get("target", "") or "").strip()
        candidate_target = str(candidate_plan.get("target", "") or "").strip()
        phase = str(payload.get("phase", "") or "").strip().lower()
        if not current_plan or phase in {"", "done", "failed"} or _semver(candidate_target) > _semver(current_target):
            payload.clear()
            payload.update(
                {
                    "cluster_id": str(getattr(cfg, "cluster_id", "") or ""),
                    "phase": "download",
                    "plan": dict(candidate_plan),
                    "expected_hosts": expected_hosts,
                    "nodes": {},
                    "created_at": now_s,
                }
            )
            current_plan = dict(candidate_plan)
        elif candidate_target == current_target:
            # Preserve the first session owner, but refresh immutable package metadata if MCC changed it.
            plan = dict(current_plan)
            for field in ("package_url", "sha256"):
                if str(candidate_plan.get(field, "") or "").strip():
                    plan[field] = str(candidate_plan.get(field) or "").strip()
            payload["plan"] = plan
            current_plan = plan
    else:
        phase = str(payload.get("phase", "") or "").strip().lower()
        if current_plan and phase in {"done", "failed"}:
            return None

    if not current_plan:
        return None

    payload["expected_hosts"] = _dedupe_hosts(list(payload.get("expected_hosts") or []) + expected_hosts)
    nodes = payload.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        payload["nodes"] = nodes
    for host in payload["expected_hosts"]:
        node = nodes.setdefault(host, {})
        if not isinstance(node, dict):
            node = {}
            nodes[host] = node
        node.setdefault("download_status", "pending")
        node.setdefault("install_status", "pending")
    local_node = nodes.setdefault(local_host, {})
    local_node.update(
        {
            "last_seen": now_s,
            "running_version": __version__,
            "node_index": int(getattr(cfg, "cluster_node_index", 0) or 0),
            "node_role": str(getattr(cfg, "cluster_node_role", "") or ""),
            "blockers": blockers,
            "blockers_at": now_s,
        }
    )
    target = str((current_plan or {}).get("target", "") or "").strip()
    if target and _semver(installed_agent_version()) >= _semver(target):
        local_node["download_status"] = "downloaded"
        local_node["install_status"] = "success"
        local_node.setdefault("downloaded_at", now_s)
        local_node.setdefault("installed_at", now_s)
    return current_plan


def _cluster_recent_blockers(payload: dict[str, Any], *, now_s: int) -> list[str]:
    blockers: list[str] = []
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return ["cluster update state has no node health"]
    for host in payload.get("expected_hosts") or []:
        node = nodes.get(host)
        if not isinstance(node, dict):
            blockers.append(f"{host}: no update health")
            continue
        if str(node.get("install_status") or "") == "success":
            continue
        seen = int(node.get("last_seen") or 0)
        if seen <= 0 or now_s - seen > _CLUSTER_UPDATE_HEALTH_STALE_SEC:
            blockers.append(f"{host}: update health stale")
            continue
        raw_blockers = node.get("blockers")
        if isinstance(raw_blockers, list) and raw_blockers:
            label = str(raw_blockers[0].get("kind") if isinstance(raw_blockers[0], dict) else raw_blockers[0])
            blockers.append(f"{host}: {label}")
    return blockers


def _all_cluster_hosts_have_status(payload: dict[str, Any], field: str, value: str) -> bool:
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return False
    for host in payload.get("expected_hosts") or []:
        node = nodes.get(host)
        if not isinstance(node, dict) or str(node.get(field) or "") != value:
            return False
    return True


def _next_cluster_host_for_status(payload: dict[str, Any], field: str, value: str) -> str | None:
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return None
    for host in payload.get("expected_hosts") or []:
        node = nodes.get(host)
        if not isinstance(node, dict) or str(node.get(field) or "") != value:
            return str(host)
    return None


def _cluster_update_decide_locked(
    cfg: AgentConfig,
    payload: dict[str, Any],
    *,
    candidate_plan: dict[str, Any] | None,
    now_s: int,
) -> dict[str, Any]:
    local_host = _cluster_local_host_name(cfg)
    expected_hosts = _cluster_expected_update_hosts(cfg)
    blockers = _cluster_local_update_blockers(cfg)
    plan = _cluster_update_seed_or_refresh(
        cfg,
        payload,
        candidate_plan=candidate_plan,
        expected_hosts=expected_hosts,
        local_host=local_host,
        blockers=blockers,
        now_s=now_s,
    )
    if plan is None:
        return {"action": "idle", "handled": False}

    phase = str(payload.get("phase", "download") or "download").strip().lower()
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    local_node = nodes.get(local_host) if isinstance(nodes, dict) else {}
    if not isinstance(local_node, dict):
        local_node = {}
        nodes[local_host] = local_node

    if phase == "download":
        if _all_cluster_hosts_have_status(payload, "download_status", "downloaded"):
            payload["phase"] = "install"
            phase = "install"
        else:
            next_host = _next_cluster_host_for_status(payload, "download_status", "downloaded")
            lock = payload.get("download_lock")
            if _cluster_update_lock_expired(lock, now_s=now_s, ttl_sec=_CLUSTER_UPDATE_DOWNLOAD_LOCK_TTL_SEC):
                payload.pop("download_lock", None)
                lock = None
            if str(local_node.get("download_status") or "") == "downloaded":
                return {"action": "wait", "handled": True, "message": "cluster update: package downloaded locally; waiting for peers"}
            if next_host != local_host:
                return {
                    "action": "wait",
                    "handled": True,
                    "message": f"cluster update: waiting for {next_host or 'peer'} package download",
                }
            if isinstance(lock, dict) and str(lock.get("host") or "") != local_host:
                return {
                    "action": "wait",
                    "handled": True,
                    "message": f"cluster update: package download held by {lock.get('host')}",
                }
            payload["download_lock"] = {
                "host": local_host,
                "node_index": int(getattr(cfg, "cluster_node_index", 0) or 0),
                "ts": now_s,
            }
            local_node["download_status"] = "running"
            local_node["download_started_at"] = now_s
            return {"action": "download", "handled": True, "plan": dict(plan), "local_host": local_host}

    if phase == "install":
        if not _all_cluster_hosts_have_status(payload, "download_status", "downloaded"):
            payload["phase"] = "download"
            return {"action": "wait", "handled": True, "message": "cluster update: waiting for all packages to be downloaded"}
        if _all_cluster_hosts_have_status(payload, "install_status", "success"):
            payload["phase"] = "done"
            payload["completed_at"] = now_s
            return {"action": "done", "handled": True, "message": "cluster update: completed"}
        recent_blockers = _cluster_recent_blockers(payload, now_s=now_s)
        if recent_blockers:
            return {
                "action": "wait",
                "handled": True,
                "message": "cluster update deferred: " + "; ".join(recent_blockers[:5]),
            }
        next_host = _next_cluster_host_for_status(payload, "install_status", "success")
        if next_host != local_host:
            return {
                "action": "wait",
                "handled": True,
                "message": f"cluster update: waiting for {next_host or 'peer'} install",
            }
        lock = payload.get("install_lock")
        if _cluster_update_lock_expired(lock, now_s=now_s, ttl_sec=_CLUSTER_UPDATE_INSTALL_LOCK_TTL_SEC):
            payload.pop("install_lock", None)
            lock = None
        if isinstance(lock, dict) and str(lock.get("host") or "") != local_host:
            return {
                "action": "wait",
                "handled": True,
                "message": f"cluster update: install held by {lock.get('host')}",
            }
        payload["install_lock"] = {
            "host": local_host,
            "node_index": int(getattr(cfg, "cluster_node_index", 0) or 0),
            "ts": now_s,
        }
        local_node["install_status"] = "running"
        local_node["install_started_at"] = now_s
        return {"action": "install", "handled": True, "plan": dict(plan), "local_host": local_host}

    if phase == "failed":
        return {
            "action": "failed",
            "handled": True,
            "message": f"cluster update failed on {payload.get('failed_host') or 'unknown node'}: {payload.get('failed_reason') or '-'}",
        }
    return {"action": "done", "handled": True, "message": "cluster update: no active action"}


def _cluster_update_finalize_download(
    cfg: AgentConfig,
    *,
    local_host: str,
    ok: bool,
    message: str,
    now_s: int,
) -> None:
    def mutator(payload: dict[str, Any]) -> None:
        nodes = payload.setdefault("nodes", {})
        node = nodes.setdefault(local_host, {}) if isinstance(nodes, dict) else {}
        if isinstance(node, dict):
            node["download_status"] = "downloaded" if ok else "failed"
            node["download_result"] = message[:500]
            node["download_finished_at"] = now_s
        lock = payload.get("download_lock")
        if isinstance(lock, dict) and str(lock.get("host") or "") == local_host:
            payload.pop("download_lock", None)
        if ok and _all_cluster_hosts_have_status(payload, "download_status", "downloaded"):
            payload["phase"] = "install"

    _cluster_update_mutate(cfg, mutator)


def _cluster_update_finalize_install(
    cfg: AgentConfig,
    *,
    local_host: str,
    ok: bool,
    message: str,
    now_s: int,
) -> None:
    def mutator(payload: dict[str, Any]) -> None:
        nodes = payload.setdefault("nodes", {})
        node = nodes.setdefault(local_host, {}) if isinstance(nodes, dict) else {}
        if isinstance(node, dict):
            node["install_status"] = "success" if ok else "failed"
            node["install_result"] = message[:500]
            node["install_finished_at"] = now_s
            node["running_version"] = str(payload.get("plan", {}).get("target") or __version__) if ok else __version__
        lock = payload.get("install_lock")
        if isinstance(lock, dict) and str(lock.get("host") or "") == local_host:
            payload.pop("install_lock", None)
        if ok and _all_cluster_hosts_have_status(payload, "install_status", "success"):
            payload["phase"] = "done"
            payload["completed_at"] = now_s
        if not ok:
            payload["phase"] = "failed"
            payload["failed_host"] = local_host
            payload["failed_reason"] = message[:500]

    _cluster_update_mutate(cfg, mutator)


def maybe_cluster_auto_update(
    cfg: AgentConfig,
    decision: dict[str, Any],
    *,
    now_s: int,
) -> tuple[bool, str | None, int]:
    if not _cluster_update_enabled(cfg):
        return False, None, 0
    candidate_plan = _cluster_plan_from_decision(cfg, decision, now_s=now_s)
    retry_sec = max(60, int(getattr(cfg, "mcd_update_wait_retry_sec", 60) or 60))

    try:
        action = _cluster_update_mutate(
            cfg,
            lambda payload: _cluster_update_decide_locked(
                cfg,
                payload,
                candidate_plan=candidate_plan,
                now_s=now_s,
            ),
        )
    except Exception as e:
        logging.warning("MCD cluster update coordinator unavailable: %s", e)
        return False, None, 0

    if not isinstance(action, dict) or not bool(action.get("handled")):
        return False, None, 0

    action_name = str(action.get("action") or "wait")
    if action_name == "download":
        plan = action.get("plan") if isinstance(action.get("plan"), dict) else {}
        local_host = str(action.get("local_host") or _cluster_local_host_name(cfg))
        try:
            archive_path = _ensure_update_archive(cfg, plan)
            msg = f"cluster update: package downloaded on {local_host}: {archive_path}"
            _cluster_update_finalize_download(cfg, local_host=local_host, ok=True, message=msg, now_s=int(time.time()))
            return True, msg, retry_sec
        except Exception as e:
            msg = f"cluster update download failed on {local_host}: {e}"
            _cluster_update_finalize_download(cfg, local_host=local_host, ok=False, message=msg, now_s=int(time.time()))
            return True, msg, retry_sec

    if action_name == "install":
        plan = dict(action.get("plan") if isinstance(action.get("plan"), dict) else {})
        local_host = str(action.get("local_host") or _cluster_local_host_name(cfg))
        if str(plan.get("session_owner") or "") != local_host:
            plan["session_id"] = ""
        ok, msg = apply_update(cfg, plan)
        _cluster_update_finalize_install(cfg, local_host=local_host, ok=ok, message=msg, now_s=int(time.time()))
        return True, msg, retry_sec

    msg = str(action.get("message") or "cluster update: waiting")
    return True, msg, retry_sec


def apply_update(cfg: AgentConfig, plan: dict[str, Any]) -> tuple[bool, str]:
    status = str(plan.get("status", "")).strip().lower()
    if status == "update_available":
        # MCC may return update_available when the host should download/apply
        # without holding a central update session (notably cluster-channel
        # coordination). Treat that as an executable plan once target and
        # package_url are present.
        status = "update"
    if status != "update":
        return False, f"no update plan (status={status or '-'})"
    target = str(plan.get("target", "")).strip()
    package_url = str(plan.get("package_url", "")).strip()
    if not target or not package_url:
        return False, "invalid plan: target/package_url is required"
    state = _read_state(cfg)
    session_id = str(plan.get("session_id", "")).strip()
    now_s = int(time.time())
    current_installed = installed_agent_version()
    if _semver(target) <= _semver(current_installed):
        if current_installed != __version__:
            msg = (
                f"source/running version mismatch repaired: source={current_installed} "
                f"running={__version__}; service restart scheduled"
            )
            state.update(
                {
                    "last_status": "version_mismatch_restart",
                    "last_result": msg,
                    "last_target": target,
                    "last_attempt_ts": now_s,
                    "last_session_id": session_id,
                }
            )
            _clear_active_campaign_process_state(state)
            _write_state(cfg, state)
            if session_id:
                release_session(
                    cfg,
                    session_id,
                    result_status="success",
                    result_message=msg,
                    new_version=current_installed,
                )
            _restart_service_async()
            return True, msg
        if session_id:
            release_session(
                cfg,
                session_id,
                result_status="success",
                result_message=f"already up-to-date ({current_installed})",
                new_version=current_installed,
            )
        return False, f"already up-to-date ({current_installed})"

    if bool(getattr(cfg, "mcd_update_defer_during_campaigns", True)):
        active_campaigns = _active_campaign_processes()
        if active_campaigns:
            msg = _active_campaign_update_defer_message(active_campaigns)
            state.update(
                {
                    "last_status": "deferred_active_campaign",
                    "last_result": msg,
                    "last_target": target,
                    "last_attempt_ts": now_s,
                    "last_session_id": session_id,
                    "active_campaign_processes": active_campaigns[:10],
                }
            )
            _write_state(cfg, state)
            if session_id:
                release_session(
                    cfg,
                    session_id,
                    result_status="deferred",
                    result_message=msg,
                    new_version=installed_agent_version(),
                )
            return False, msg

    install_dir = Path(str(getattr(cfg, "mcd_install_dir", "/opt/mcd") or "/opt/mcd"))
    src_dir = install_dir / "src"
    backup_dir = install_dir / "var" / "backup"
    updates_dir = install_dir / "var" / "updates"
    src_next_dir = updates_dir / f"src.next-{target}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    updates_dir.mkdir(parents=True, exist_ok=True)

    lock_f = _acquire_update_lock(cfg, blocking=False)
    if lock_f is None:
        session_id = str(plan.get("session_id", "")).strip()
        if session_id:
            release_session(
                cfg,
                session_id,
                result_status="failed",
                result_message="another update is already running",
                new_version=installed_agent_version(),
            )
        return False, "another update is already running"

    archive_path = _update_archive_path(target)
    backup_path = backup_dir / f"mcd-src-preupdate-{now_s}.tgz"

    old_src_dir: Path | None = None
    swapped = False
    try:
        archive_path = _ensure_update_archive(cfg, plan)

        if src_dir.exists():
            with tarfile.open(backup_path, "w:gz") as tf:
                tf.add(src_dir, arcname="src")

        if src_next_dir.exists():
            shutil.rmtree(src_next_dir)
        _extract_archive_to_dir(archive_path, src_next_dir)
        _install_requirements_for_staged_source(install_dir, src_next_dir)
        _pre_switch_smoke_check(install_dir, src_next_dir)

        old_src_dir = updates_dir / f"src.prev-{now_s}"
        if old_src_dir.exists():
            shutil.rmtree(old_src_dir)
        if src_dir.exists():
            os.replace(src_dir, old_src_dir)
        os.replace(src_next_dir, src_dir)
        swapped = True

        release_session(
            cfg,
            session_id,
            result_status="success",
            result_message=f"updated to {target}",
            new_version=target,
        )
        state.update(
            {
                "last_status": "success",
                "last_result": f"updated to {target}",
                "last_target": target,
                "last_attempt_ts": now_s,
                "last_session_id": session_id,
            }
        )
        _clear_active_campaign_process_state(state)
        if _cluster_update_enabled(cfg):
            local_host = _cluster_local_host_name(cfg)
            state["last_cluster_update_result"] = f"update applied -> {target}; source switched, service restart scheduled"
            try:
                _cluster_update_finalize_download(
                    cfg,
                    local_host=local_host,
                    ok=True,
                    message=f"cluster update: package downloaded on {local_host}: {archive_path}",
                    now_s=now_s,
                )
                _cluster_update_finalize_install(
                    cfg,
                    local_host=local_host,
                    ok=True,
                    message=f"updated to {target}",
                    now_s=now_s,
                )
            except Exception as ce:
                logging.warning("cluster update coordinator finalize after local apply failed: %s", ce)
        if bool(cfg.mcd_update_cleanup_enabled):
            try:
                state["last_cleanup_removed"] = _cleanup_update_artifacts(cfg, now_s=now_s)
                state["last_cleanup_ts"] = now_s
                state["last_cleanup_status"] = "ok"
                state["next_cleanup_ts"] = now_s + max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
            except Exception as ce:
                state["last_cleanup_status"] = f"failed:{ce}"
        _write_state(cfg, state)
        try:
            if old_src_dir is not None and old_src_dir.exists():
                shutil.rmtree(old_src_dir)
        except Exception:
            pass
        _restart_service_async()
        return True, f"update applied -> {target}; source switched, service restart scheduled"
    except Exception as e:
        msg = str(e)
        # Best-effort rollback from pre-update source copy.
        try:
            if swapped and old_src_dir is not None and old_src_dir.exists():
                if src_dir.exists():
                    shutil.rmtree(src_dir)
                os.replace(old_src_dir, src_dir)
            elif backup_path.exists():
                if src_dir.exists():
                    shutil.rmtree(src_dir)
                with tarfile.open(backup_path, "r:gz") as tf:
                    tf.extractall(install_dir)
        except Exception:
            pass
        try:
            if src_next_dir.exists():
                shutil.rmtree(src_next_dir)
        except Exception:
            pass
        release_session(
            cfg,
            session_id,
            result_status="failed",
            result_message=msg,
            new_version=installed_agent_version(),
        )
        state.update(
            {
                "last_status": "failed",
                "last_result": msg,
                "last_target": target,
                "last_attempt_ts": now_s,
                "last_session_id": session_id,
            }
        )
        if bool(cfg.mcd_update_cleanup_enabled):
            try:
                state["last_cleanup_removed"] = _cleanup_update_artifacts(cfg, now_s=now_s)
                state["last_cleanup_ts"] = now_s
                state["last_cleanup_status"] = "ok"
                state["next_cleanup_ts"] = now_s + max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
            except Exception as ce:
                state["last_cleanup_status"] = f"failed:{ce}"
        _write_state(cfg, state)
        return False, msg
    finally:
        _release_update_lock(lock_f)


def maybe_auto_update(cfg: AgentConfig, *, force: bool = False) -> tuple[str | None, int]:
    state = _read_state(cfg)
    now_s = int(time.time())
    cleanup_state_changed = _maybe_run_update_cleanup(cfg, state, now_s=now_s)
    auto = bool(cfg.mcd_auto_update_enabled) and _update_policy(cfg) != "off"
    cluster_update_mode = auto and _cluster_update_enabled(cfg)
    next_allowed = int(state.get("next_check_ts", 0) or 0)
    if not force and now_s < next_allowed:
        # A cluster rollout is coordinated through the shared Galera state and
        # can be advanced by another node while this node's local MCC check is
        # still sleeping. Keep reconciling active shared plans so nodes that are
        # already on the target version mark themselves done immediately, and
        # peers do not wait up to the full MCC check interval.
        if cluster_update_mode:
            try:
                handled, cluster_msg, cluster_retry = maybe_cluster_auto_update(cfg, {}, now_s=now_s)
            except Exception as e:
                logging.warning("MCD cluster update coordinator unavailable during reconcile: %s", e)
                handled, cluster_msg, cluster_retry = False, None, 0
            if handled:
                state2 = _read_state(cfg)
                state2["last_cluster_update_result"] = cluster_msg or ""
                state2["next_check_ts"] = now_s + max(60, int(cluster_retry or cfg.mcd_update_wait_retry_sec or 60))
                if cleanup_state_changed:
                    state2["last_cleanup_ts"] = state.get("last_cleanup_ts")
                    state2["last_cleanup_status"] = state.get("last_cleanup_status")
                    state2["last_cleanup_removed"] = state.get("last_cleanup_removed")
                    state2["next_cleanup_ts"] = state.get("next_cleanup_ts")
                _write_state(cfg, state2)
                return cluster_msg, int(state2["next_check_ts"]) - now_s
        if cleanup_state_changed:
            _write_state(cfg, state)
        return None, max(1, next_allowed - now_s)

    if cfg.backup_enabled and not cluster_update_mode:
        try:
            if backup_lock_active(cfg):
                retry_sec = max(5, int(cfg.poll_interval_sec or 10))
                state["last_check_ts"] = now_s
                state["last_check_status"] = "deferred_backup_lock"
                state["next_check_ts"] = now_s + retry_sec
                _write_state(cfg, state)
                return "MCD update deferred: backup lock is active", retry_sec
        except Exception as e:
            logging.warning("MCD update backup lock check failed: %s", e)

    if bool(getattr(cfg, "mcd_update_defer_during_campaigns", True)):
        active_campaigns = _active_campaign_processes()
        if active_campaigns:
            state["active_campaign_processes"] = active_campaigns[:10]
            if not cluster_update_mode:
                retry_sec = max(60, int(cfg.mcd_update_wait_retry_sec or 60))
                state["last_check_ts"] = now_s
                state["last_check_status"] = "deferred_active_campaign"
                state["last_result"] = _active_campaign_update_defer_message(active_campaigns)
                state["next_check_ts"] = now_s + retry_sec
                _write_state(cfg, state)
                return str(state["last_result"]), retry_sec
        else:
            _clear_active_campaign_process_state(state)

    # Cluster mode has its own Galera-backed rollout coordinator, so MCC is
    # queried as a release catalog and must not reserve a per-host update slot.
    decision = check_with_mcc(cfg, auto_update_enabled=(auto and not cluster_update_mode))
    status = str(decision.get("status", "")).strip().lower()
    retry_after = int(decision.get("retry_after_sec", 0) or 0)
    if retry_after <= 0:
        retry_after = int(cfg.mcd_update_check_interval_sec or 3600)

    state["last_check_ts"] = now_s
    state["last_check_status"] = status or "unknown"
    state["last_decision"] = decision

    if cluster_update_mode:
        handled, cluster_msg, cluster_retry = maybe_cluster_auto_update(cfg, decision, now_s=now_s)
        if handled:
            state2 = _read_state(cfg)
            state2["last_check_ts"] = now_s
            state2["last_check_status"] = status or "unknown"
            state2["last_decision"] = decision
            state2["last_cluster_update_result"] = cluster_msg or ""
            state2["next_check_ts"] = now_s + max(60, int(cluster_retry or cfg.mcd_update_wait_retry_sec or 60))
            _write_state(cfg, state2)
            return cluster_msg, int(state2["next_check_ts"]) - now_s
        if status in {"update", "update_available"}:
            retry_sec = max(60, int(cfg.mcd_update_wait_retry_sec or retry_after or 60))
            msg = "MCD cluster update deferred: shared cluster coordinator is unavailable"
            state["last_cluster_update_result"] = msg
            state["next_check_ts"] = now_s + retry_sec
            _write_state(cfg, state)
            return msg, retry_sec

    if status in {"up_to_date", "disabled"}:
        current_installed = installed_agent_version()
        if current_installed != __version__:
            _restart_service_async()
            state["last_status"] = "version_mismatch_restart"
            state["last_result"] = (
                f"source/running version mismatch repaired: source={current_installed} "
                f"running={__version__}; service restart scheduled"
            )
            _clear_active_campaign_process_state(state)
            state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_wait_retry_sec or 60))
            _write_state(cfg, state)
            return str(state["last_result"]), int(state["next_check_ts"]) - now_s
        state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
        _clear_active_campaign_process_state(state)
        _write_state(cfg, state)
        return None, int(state["next_check_ts"]) - now_s

    if status == "wait":
        state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_wait_retry_sec or retry_after))
        _write_state(cfg, state)
        return "MCD update: MCC slots busy, waiting for next retry", int(state["next_check_ts"]) - now_s

    if status in {"update", "update_available"}:
        target = str(decision.get("target", "")).strip()
        current_installed = installed_agent_version()
        if _semver(target) <= _semver(current_installed):
            if current_installed != __version__:
                _restart_service_async()
                state["last_status"] = "version_mismatch_restart"
                state["last_result"] = (
                    f"source/running version mismatch repaired: source={current_installed} "
                    f"running={__version__}; service restart scheduled"
                )
                _clear_active_campaign_process_state(state)
                state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_wait_retry_sec or 60))
                _write_state(cfg, state)
                return str(state["last_result"]), int(state["next_check_ts"]) - now_s
            state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
            _clear_active_campaign_process_state(state)
            _write_state(cfg, state)
            return None, int(state["next_check_ts"]) - now_s
        if status == "update_available" and not auto:
            state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
            _write_state(cfg, state)
            return f"MCD update available: current={current_installed} target={target}", int(state["next_check_ts"]) - now_s
        ok, msg = apply_update(cfg, decision)
        # apply_update() persists result fields (last_status/last_result/...).
        # Re-read to avoid overwriting them with stale pre-apply state.
        state2 = _read_state(cfg)
        state2["last_check_ts"] = now_s
        state2["last_check_status"] = status or "unknown"
        state2["last_decision"] = decision
        state2["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
        _write_state(cfg, state2)
        return msg, int(state2["next_check_ts"]) - now_s

    # Transport or protocol errors: avoid noisy retries.
    err_reason = str(decision.get("reason", status or "unknown")).strip()
    logging.warning("MCD update check failed: %s", err_reason)
    state["next_check_ts"] = now_s + max(120, int(cfg.mcd_update_check_interval_sec))
    _write_state(cfg, state)
    return None, int(state["next_check_ts"]) - now_s


def update_status(cfg: AgentConfig) -> dict[str, Any]:
    out = _read_state(cfg)
    if "active_campaign_processes" in out:
        active_campaigns = _active_campaign_processes()
        if active_campaigns:
            out["active_campaign_processes"] = active_campaigns[:10]
        else:
            out.pop("active_campaign_processes", None)
    versions = agent_version_payload()
    out["current_version"] = versions.get("agent_version") or __version__
    out["running_version"] = versions.get("agent_running_version") or __version__
    out["installed_version"] = versions.get("agent_installed_version") or ""
    out["source_version"] = versions.get("agent_source_version") or ""
    out["version_mismatch"] = bool(versions.get("agent_version_mismatch"))
    current = str(out.get("current_version") or "")
    if (
        current
        and not bool(out.get("version_mismatch"))
        and str(out.get("last_status") or "") == "success"
        and str(out.get("last_target") or "") == current
    ):
        cluster_result = str(out.get("last_cluster_update_result") or "")
        if cluster_result.startswith("cluster update: waiting"):
            out["last_cluster_update_result"] = f"update applied -> {current}; source switched, service restart scheduled"
    out.setdefault("policy", _update_policy(cfg))
    out.setdefault("auto_update_enabled", bool(cfg.mcd_auto_update_enabled))
    return out
