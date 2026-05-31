from __future__ import annotations

import fcntl
import gzip
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

from mcd_agent.config import AgentConfig, resolve_mutable_config_path, upsert_section_values
from mcd_agent.cluster_routing import cluster_local_identity_values
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.models import DBConfig, MauticInstall
from mcd_agent.secret_store import SecretStore


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROFILE_ROW_ID = 1
_MYDUMPER_FLAG_CACHE: dict[tuple[str, str], bool] = {}
_XTRABACKUP_FLAG_CACHE: dict[tuple[str, str], bool] = {}

_CLUSTER_FILE_FORBIDDEN_PREFIXES = (
    "/dev",
    "/proc",
    "/run",
    "/sys",
    "/tmp",
    "/var/cache",
    "/var/lib/glusterd",
    "/var/lib/mysql",
    "/var/log",
    "/var/run",
    "/var/tmp",
    "/root",
)

_CLUSTER_FILE_FORBIDDEN_EXACT = {
    "/",
    "/boot",
    "/home",
    "/mnt",
    "/opt",
    "/usr",
    "/var",
    "/var/lib",
}

_CLUSTER_FILE_RSYNC_EXCLUDES = (
    "/etc/.pwd.lock",
    "/etc/gshadow",
    "/etc/gshadow-",
    "/etc/mysql/debian.cnf",
    "/etc/security/opasswd",
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/ssh/ssh_host_*_key",
    "/var/lib/glusterd/***",
)


@dataclass(frozen=True)
class BackupResult:
    ok: bool
    message: str
    state_path: str
    backup_path: str | None = None
    duration_sec: int | None = None
    bytes_written: int | None = None


@dataclass
class _PreparedMysqlRuntime:
    socket_path: Path
    datadir: Path
    run_dir: Path
    process: subprocess.Popen[Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_local_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _json_read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _short_error(value: Any, *, limit: int = 4000) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _compact_backup_history(history: Any, *, keep: int = 20) -> list[dict[str, Any]]:
    if not isinstance(history, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in history[: max(0, int(keep))]:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if "error" in row:
            row["error"] = _short_error(row.get("error"))
        compact.append(row)
    return compact


def _run(
    cmd: list[str],
    *,
    timeout_sec: int = 0,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if timeout_sec > 0:
        kwargs["timeout"] = timeout_sec
    if env is not None:
        kwargs["env"] = env
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        details = err or out or f"exit={proc.returncode}"
        raise RuntimeError(f"command failed: {' '.join(cmd)} :: {details}")
    return proc


def _mounted(path: Path) -> bool:
    p = str(path.resolve())
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == p:
            return True
    return False


def _unmount(path: Path, timeout_sec: int) -> None:
    if not _mounted(path):
        return
    try:
        _run(["fusermount", "-u", str(path)], timeout_sec=timeout_sec, check=True)
    except Exception:
        _run(["umount", "-l", str(path)], timeout_sec=timeout_sec, check=False)


def _format_remote_dir(root_dir: str, instance_name: str) -> str:
    base = root_dir.strip().strip("/")
    name = instance_name.strip().strip("/")
    if not base:
        return name
    return f"{base}/{name}"


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    _json_write(path / ".mcd-backup.json", payload)


def _path_rel_to(base: Path, path: Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(base.resolve(strict=False)))
    except Exception:
        return str(path)


def _asset_bytes(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            total = 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += int((Path(root) / name).stat().st_size)
                    except Exception:
                        pass
            return total
    except Exception:
        pass
    return 0


def _backup_manifest_from_marker(
    *,
    mount_path: Path,
    backup_dir: Path,
    marker: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    dumped = marker.get("dumped_instances")
    first = dumped[0] if isinstance(dumped, list) and dumped and isinstance(dumped[0], dict) else {}
    db_asset: dict[str, Any] = {}
    files_asset: dict[str, Any] = {}
    db_archive_raw = str(marker.get("db_archive_path") or "").strip()
    db_archive = backup_dir / Path(db_archive_raw).name if db_archive_raw else Path()
    if db_archive_raw and db_archive.exists() and db_archive.is_file():
        db_asset = {
            "path": _path_rel_to(backup_dir, db_archive),
            "bytes": _asset_bytes(db_archive),
            "type": "sql.gz",
        }
    db_root = backup_dir / "databases"
    if not db_asset and db_root.exists():
        db_asset = {
            "path": _path_rel_to(backup_dir, db_root),
            "bytes": _asset_bytes(db_root),
            "type": "directory",
        }
    archive_name = "files.tar.gz"
    archive = backup_dir / archive_name
    if not archive.exists():
        archive = backup_dir / str(marker.get("files_archive_path") or "").split("/")[-1]
    if archive.exists() and archive.is_file():
        files_asset = {
            "path": _path_rel_to(backup_dir, archive),
            "bytes": _asset_bytes(archive),
            "type": "tar.gz",
        }
    manifest = {
        "schema": "mcc.backup.manifest.v1",
        "kind": kind,
        "label": str(marker.get("cluster_name") or marker.get("host_name") or first.get("instance_name") or backup_dir.name),
        "created_at_utc": str(marker.get("ts_utc") or _utc_now_iso()),
        "source_host": str(marker.get("host_name") or ""),
        "source_domain": str(first.get("instance_name") or ""),
        "source_webroot": str(first.get("root") or ""),
        "source_database": str(marker.get("database") or first.get("database") or ""),
        "mautic_major": int(marker.get("mautic_major") or 0),
        "mautic_version": str(marker.get("mautic_version") or ""),
        "method": str(marker.get("method") or ""),
        "backup_path": _path_rel_to(mount_path, backup_dir),
        "bytes_written": int(marker.get("bytes_written") or 0),
        "files_asset": files_asset,
        "db_asset": db_asset,
        "restorable_as_image": bool(files_asset.get("path") and db_asset.get("path")),
    }
    if marker.get("cluster_backup"):
        manifest["cluster_backup"] = True
        manifest["cluster_name"] = str(marker.get("cluster_name") or "")
    return manifest


def _write_storage_backup_manifest_and_index(
    *,
    mount_path: Path,
    backup_dir: Path,
    marker: dict[str, Any],
    kind: str,
) -> None:
    try:
        manifest = _backup_manifest_from_marker(
            mount_path=mount_path,
            backup_dir=backup_dir,
            marker=marker,
            kind=kind,
        )
        manifest_path = backup_dir / "mcc-backup-manifest.json"
        _json_write(manifest_path, manifest)
        manifest_rel = _path_rel_to(mount_path, manifest_path)
        index_entry = dict(manifest)
        index_entry["manifest_path"] = manifest_rel
        index_entry["backup_dir"] = _path_rel_to(mount_path, backup_dir)
        digest = re.sub(r"[^A-Za-z0-9_.-]+", "-", index_entry["backup_dir"]).strip(".-")
        if not digest:
            digest = f"backup-{int(time.time())}"
        index_dir = mount_path / "mcc-backups-index.d"
        index_dir.mkdir(parents=True, exist_ok=True)
        _json_write(index_dir / f"{digest[:160]}.json", index_entry)
    except Exception:
        # Indexing must never turn a completed backup into a failed backup.
        return


def _verify_dump_dir(path: Path) -> tuple[bool, str, int]:
    if not path.exists() or not path.is_dir():
        return False, "dump directory missing", 0
    entries = list(path.iterdir())
    if not entries:
        return False, "dump directory is empty", 0
    metadata = [x for x in entries if x.name.startswith("metadata")]
    if not metadata:
        return False, "metadata file not found", 0
    data_files = [x for x in entries if x.is_file() and (x.name.endswith(".sql") or ".sql." in x.name)]
    if not data_files:
        return False, "no SQL chunk files found", 0
    total = 0
    for f in entries:
        if f.is_file():
            total += int(f.stat().st_size)
    if total <= 0:
        return False, "total dump size is zero", 0
    return True, "ok", total


def _storage_usage(path: Path) -> dict[str, Any] | None:
    try:
        du = shutil.disk_usage(path)
    except Exception:
        return None
    total = int(getattr(du, "total", 0) or 0)
    used = int(getattr(du, "used", 0) or 0)
    free = int(getattr(du, "free", 0) or 0)
    used_pct = (float(used) / float(total) * 100.0) if total > 0 else 0.0
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_pct": round(used_pct, 2),
        "checked_at": _utc_now_iso(),
    }


def _archive_files(cfg: AgentConfig, out_dir: Path) -> None:
    if not cfg.backup_archive_enabled:
        return
    src = [p for p in cfg.backup_archive_paths if Path(p).exists()]
    if not src:
        return
    target = out_dir / cfg.backup_archive_name
    cmd = ["tar", "-czf", str(target)] + src
    _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)


def _archive_instance_files(cfg: AgentConfig, inst: MauticInstall, out_dir: Path) -> Path:
    root = Path(inst.root)
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"instance root not found: {inst.root}")
    target = out_dir / "files.tar.gz"
    cmd = [
        "tar",
        "--exclude=var/cache",
        "--exclude=var/logs",
        "--exclude=app/cache",
        "--exclude=app/logs",
        "-czf",
        str(target),
        "-C",
        str(root),
        ".",
    ]
    _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    return target


def _prune_by_copies(parent: Path, keep: int) -> list[str]:
    removed: list[str] = []
    if keep <= 0 or not parent.exists():
        return removed
    candidates = [x for x in parent.iterdir() if x.is_dir() and _DATE_RE.match(x.name)]
    candidates.sort(key=lambda x: x.name, reverse=True)
    for old in candidates[keep:]:
        subprocess.run(["rm", "-rf", str(old)], check=False)
        removed.append(old.name)
    return removed


def _cleanup_incomplete_dirs(
    parent: Path,
    *,
    min_age_sec: int = 0,
    keep: set[Path] | None = None,
) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    failed: list[str] = []
    if not parent.exists():
        return removed, failed
    now = time.time()
    keep_resolved = {p.resolve(strict=False) for p in (keep or set())}
    for child in parent.iterdir():
        try:
            child_resolved = child.resolve(strict=False)
        except Exception:
            child_resolved = child
        if child_resolved in keep_resolved:
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        if not child.name.startswith(".incomplete-"):
            continue
        try:
            age_sec = max(0.0, now - child.stat().st_mtime)
        except Exception:
            age_sec = float(min_age_sec)
        if age_sec < max(0, int(min_age_sec)):
            continue
        try:
            shutil.rmtree(child)
            removed.append(child.name)
        except Exception:
            failed.append(child.name)
    return removed, failed


def _dir_date(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d")
    except Exception:
        return None


def _xtrabackup_db_dir(backup_dir: Path) -> Path:
    return backup_dir / "databases" / "physical-xtrabackup"


def _xtrabackup_checkpoint_text(db_dir: Path) -> str:
    checkpoints = db_dir / "xtrabackup_checkpoints"
    if not checkpoints.exists():
        return ""
    try:
        return checkpoints.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _xtrabackup_kind_from_checkpoints(db_dir: Path) -> str:
    text = _xtrabackup_checkpoint_text(db_dir).lower()
    if "backup_type" not in text:
        return ""
    if "full-backuped" in text or "full-prepared" in text:
        return "full"
    if "incremental" in text:
        return "incremental"
    return ""


def _xtrabackup_chain_id_for_full(path: Path) -> str:
    return f"full:{path.name}"


def _xtrabackup_marker_kind(marker: dict[str, Any]) -> str:
    for key in ("backup_kind", "xtrabackup_kind"):
        value = str(marker.get(key) or "").strip().lower()
        if value in {"full", "incremental"}:
            return value
    return ""


def _xtrabackup_marker_chain_id(marker: dict[str, Any]) -> str:
    for key in ("chain_id", "xtrabackup_chain_id"):
        value = str(marker.get(key) or "").strip()
        if value:
            return value
    return ""


def _xtrabackup_marker_full_path(marker: dict[str, Any]) -> str:
    for key in ("full_backup_path", "xtrabackup_full_backup_path", "xtrabackup_chain_full_path"):
        value = str(marker.get(key) or "").strip()
        if value:
            return value
    return ""


def _xtrabackup_backup_entries(parent: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not parent.exists():
        return entries
    for child in parent.iterdir():
        if not child.is_dir() or not _DATE_RE.match(child.name):
            continue
        db_dir = _xtrabackup_db_dir(child)
        marker = _read_backup_marker(child)
        status = str(marker.get("status") or "").strip().lower()
        if status and status != "ok":
            continue
        method = str(marker.get("method") or "").strip().lower()
        if method and method != "xtrabackup":
            continue
        if not db_dir.exists():
            continue
        kind = _xtrabackup_marker_kind(marker)
        if kind not in {"full", "incremental"}:
            kind = _xtrabackup_kind_from_checkpoints(db_dir)
        if kind not in {"full", "incremental"}:
            continue
        chain_id = _xtrabackup_marker_chain_id(marker)
        if not chain_id:
            if kind == "full":
                chain_id = _xtrabackup_chain_id_for_full(child)
            else:
                full_path_raw = _xtrabackup_marker_full_path(marker)
                chain_id = f"full:{Path(full_path_raw).name}" if full_path_raw else ""
        if not chain_id:
            continue
        dt = _dir_date(child)
        entries.append(
            {
                "path": child,
                "db_dir": db_dir,
                "date": dt,
                "kind": kind,
                "chain_id": chain_id,
                "marker": marker,
            }
        )
    entries.sort(key=lambda x: (x.get("date") or datetime.min, str(x.get("path") or "")))
    return entries


def _select_xtrabackup_plan(cfg: AgentConfig, parent: Path) -> dict[str, Any]:
    entries = _xtrabackup_backup_entries(parent)
    fulls = [x for x in entries if x.get("kind") == "full"]
    full_defaults = {
        "kind": "full",
        "base_dir": None,
        "base_path": "",
        "chain_id": "",
        "full_path": "",
        "chain_index": 0,
    }
    if not bool(getattr(cfg, "backup_xtrabackup_incremental_enabled", True)):
        return dict(full_defaults)
    if not fulls:
        return dict(full_defaults)
    latest_full = fulls[-1]
    latest_full_dt = latest_full.get("date")
    full_interval_days = max(1, int(getattr(cfg, "backup_xtrabackup_full_interval_days", 7) or 7))
    if isinstance(latest_full_dt, datetime):
        if datetime.now() - latest_full_dt >= timedelta(days=full_interval_days):
            return dict(full_defaults)
    chain_id = str(latest_full.get("chain_id") or "")
    chain_entries = [x for x in entries if str(x.get("chain_id") or "") == chain_id]
    latest = chain_entries[-1] if chain_entries else latest_full
    base_dir = latest.get("db_dir")
    if not isinstance(base_dir, Path) or not base_dir.exists():
        return dict(full_defaults)
    return {
        "kind": "incremental",
        "base_dir": base_dir,
        "base_path": str(latest.get("path") or ""),
        "chain_id": chain_id,
        "full_path": str(latest_full.get("path") or ""),
        "chain_index": len(chain_entries),
    }


def _prune_xtrabackup_retention(parent: Path, cfg: AgentConfig) -> list[str]:
    removed: list[str] = []
    entries = _xtrabackup_backup_entries(parent)
    if not entries:
        return removed
    full_keep = max(1, int(getattr(cfg, "backup_xtrabackup_retention_full_copies", 3) or 3))
    incr_keep_days = max(1, int(getattr(cfg, "backup_xtrabackup_retention_incremental_days", 7) or 7))
    fulls = [x for x in entries if x.get("kind") == "full"]
    keep_chains = {str(x.get("chain_id") or "") for x in fulls[-full_keep:]}
    cutoff = datetime.now() - timedelta(days=incr_keep_days)
    remove_paths: list[Path] = []
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, Path):
            continue
        chain_id = str(entry.get("chain_id") or "")
        kind = str(entry.get("kind") or "")
        dt = entry.get("date")
        if chain_id and chain_id not in keep_chains:
            remove_paths.append(path)
            continue
        if kind == "incremental" and isinstance(dt, datetime) and dt < cutoff:
            remove_paths.append(path)
    seen: set[str] = set()
    for path in sorted(remove_paths, key=lambda p: str(p)):
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        subprocess.run(["rm", "-rf", str(path)], check=False)
        removed.append(path.name)
    return removed


def _path_total_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += int(item.stat().st_size)
        except Exception:
            pass
    return total


def _backup_entry_bytes(entry: dict[str, Any]) -> int:
    marker = entry.get("marker")
    if isinstance(marker, dict):
        try:
            value = int(marker.get("bytes_written") or 0)
            if value > 0:
                return value
        except Exception:
            pass
    path = entry.get("path")
    return _path_total_bytes(path) if isinstance(path, Path) else 0


def _estimate_xtrabackup_required_bytes(parent: Path, plan: dict[str, Any]) -> int:
    entries = _xtrabackup_backup_entries(parent)
    kind = str(plan.get("kind") or "full").strip().lower()
    full_entries = [x for x in entries if x.get("kind") == "full"]
    full_sizes = [_backup_entry_bytes(x) for x in full_entries]
    latest_full_size = next((x for x in reversed(full_sizes) if x > 0), 0)
    if kind == "incremental":
        chain_id = str(plan.get("chain_id") or "").strip()
        chain_entries = [x for x in entries if str(x.get("chain_id") or "") == chain_id]
        incremental_sizes = [_backup_entry_bytes(x) for x in chain_entries if x.get("kind") == "incremental"]
        previous_incremental = max(incremental_sizes) if incremental_sizes else 0
        baseline = max(previous_incremental, min(latest_full_size // 5, 100 * 1024 * 1024 * 1024))
        return max(5 * 1024 * 1024 * 1024, int(float(baseline) * 1.25)) if latest_full_size > 0 else 0
    return int(float(latest_full_size) * 1.10) if latest_full_size > 0 else 0


def _delete_xtrabackup_chain(parent: Path, chain_id: str) -> list[str]:
    removed: list[str] = []
    if not chain_id:
        return removed
    entries = _xtrabackup_backup_entries(parent)
    paths = [
        x.get("path")
        for x in entries
        if str(x.get("chain_id") or "") == chain_id and isinstance(x.get("path"), Path)
    ]
    for path in sorted(paths, key=lambda p: str(p)):
        if not isinstance(path, Path) or not path.exists():
            continue
        subprocess.run(["rm", "-rf", str(path)], check=False)
        removed.append(path.name)
    return removed


def _oldest_xtrabackup_chain_ids(parent: Path, *, exclude_chain_id: str = "") -> list[str]:
    entries = _xtrabackup_backup_entries(parent)
    out: list[str] = []
    seen: set[str] = set()
    for entry in [x for x in entries if x.get("kind") == "full"]:
        chain_id = str(entry.get("chain_id") or "").strip()
        if not chain_id or chain_id == exclude_chain_id or chain_id in seen:
            continue
        seen.add(chain_id)
        out.append(chain_id)
    return out


def _ensure_xtrabackup_space(parent: Path, mount_path: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    required = _estimate_xtrabackup_required_bytes(parent, plan)
    if required <= 0:
        return plan, []
    usage = _storage_usage(mount_path)
    free = int(usage.get("free_bytes") or 0) if isinstance(usage, dict) else 0
    if free >= required:
        return plan, []
    removed: list[str] = []
    protected_chain = str(plan.get("chain_id") or "").strip() if str(plan.get("kind") or "") == "incremental" else ""
    for chain_id in _oldest_xtrabackup_chain_ids(parent, exclude_chain_id=protected_chain):
        removed += _delete_xtrabackup_chain(parent, chain_id)
        usage = _storage_usage(mount_path)
        free = int(usage.get("free_bytes") or 0) if isinstance(usage, dict) else 0
        if free >= required:
            return plan, removed
    if str(plan.get("kind") or "") == "incremental":
        # If only the active chain is left and storage is still insufficient,
        # fall back to a new full backup and allow removing the oldest full chain.
        full_plan = {
            "kind": "full",
            "base_dir": None,
            "base_path": "",
            "chain_id": "",
            "full_path": "",
            "chain_index": 0,
        }
        required = _estimate_xtrabackup_required_bytes(parent, full_plan)
        usage = _storage_usage(mount_path)
        free = int(usage.get("free_bytes") or 0) if isinstance(usage, dict) else 0
        if required > 0 and free < required:
            for chain_id in _oldest_xtrabackup_chain_ids(parent):
                removed += _delete_xtrabackup_chain(parent, chain_id)
                usage = _storage_usage(mount_path)
                free = int(usage.get("free_bytes") or 0) if isinstance(usage, dict) else 0
                if free >= required:
                    return full_plan, removed
        return full_plan, removed
    return plan, removed


def _list_instances(cfg: AgentConfig, *, force_rescan: bool = False) -> list[MauticInstall]:
    inv = InstanceInventory(cfg.state_db_path)
    if force_rescan:
        inv.rescan(cfg)
    else:
        ensure_seeded(inv, cfg)
    return inv.list_instances()


def _host_name(cfg: AgentConfig) -> str:
    raw = (cfg.backup_host_name or "").strip()
    if raw:
        return raw
    return socket.gethostname().strip() or "host"


def _host_slug(cfg: AgentConfig) -> str:
    name = _host_name(cfg)
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "host"


def _lock_path(cfg: AgentConfig) -> Path:
    return Path(cfg.backup_lock_dir) / f"backup-{_host_slug(cfg)}.lock"


def backup_lock_active(config: AgentConfig) -> bool:
    """Return True when host backup/restore lock is currently held."""
    cfg = _effective_cfg(config)
    if bool(getattr(cfg, "backup_cluster_enabled", False)):
        for name in ("local-full", "local-incremental", "files", "offsite"):
            if _lock_active(_cluster_lock_path(cfg, name)):
                return True
        if _cluster_offsite_processes(cfg):
            return True
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("w", encoding="utf-8")
    locked = False
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        return False
    except BlockingIOError:
        return True
    finally:
        if locked:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lock_fh.close()
        except Exception:
            pass


def _state_path(cfg: AgentConfig) -> Path:
    return Path(cfg.backup_state_dir) / f"host-{_host_slug(cfg)}.json"


def _profile_store_conn(cfg: AgentConfig) -> sqlite3.Connection:
    Path(cfg.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.state_db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_profile (
          id INTEGER PRIMARY KEY CHECK(id=1),
          payload_enc TEXT NOT NULL,
          updated_at REAL NOT NULL
        )
        """
    )
    con.commit()
    return con


def _profile_crypto(cfg: AgentConfig) -> SecretStore:
    return SecretStore(key_path=cfg.backup_secrets_key_path, env_master_key="MCD_BACKUP_MASTER_KEY")


def backup_profile_get(cfg: AgentConfig) -> dict[str, Any]:
    con = _profile_store_conn(cfg)
    try:
        row = con.execute("SELECT payload_enc FROM backup_profile WHERE id=?", (_PROFILE_ROW_ID,)).fetchone()
        if row is None:
            return {}
        token = str(row["payload_enc"] or "").strip()
        if not token:
            return {}
        plain = _profile_crypto(cfg).decrypt(token)
        payload = json.loads(plain)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    finally:
        con.close()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[index]
        else:
            out[k] = v
    return out


def _profile_storage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("storage")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("host", "user", "remote_path", "key_file", "password"):
        if key in src:
            out[key] = str(src.get(key) or "").strip() if key != "password" else str(src.get(key) or "")
    if "port" in src:
        try:
            out["port"] = int(src.get("port")) if src.get("port") is not None else 22
        except Exception:
            pass
    return out


def _profile_mysql_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("mysql")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("host", "user", "password", "database"):
        if key in src:
            out[key] = str(src.get(key) or "").strip() if key != "password" else str(src.get(key) or "")
    if "port" in src:
        try:
            out["port"] = int(src.get("port")) if src.get("port") is not None else 3306
        except Exception:
            pass
    return out


def _profile_archive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("archive")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    if "enabled" in src:
        out["enabled"] = bool(src.get("enabled"))
    if "name" in src:
        out["name"] = str(src.get("name") or "").strip()
    if "paths" in src:
        raw = src.get("paths")
        if isinstance(raw, list):
            out["paths"] = [str(x).strip() for x in raw if str(x).strip()]
    return out


def _profile_backup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "remote_root_dir" in payload:
        remote_root = str(payload.get("remote_root_dir") or "").strip().strip("/")
        if remote_root:
            out["remote_root_dir"] = remote_root
    return out


def _sync_profile_payload_to_config(cfg: AgentConfig, payload: dict[str, Any]) -> bool:
    changed = False
    backup = _profile_backup_payload(payload)
    if backup:
        _, c = upsert_section_values(cfg.config_file_path, "backup", backup)
        changed = changed or c
    storage = _profile_storage_payload(payload)
    if storage:
        _, c = upsert_section_values(cfg.config_file_path, "backup.storage", storage)
        changed = changed or c
    mysql = _profile_mysql_payload(payload)
    if mysql:
        _, c = upsert_section_values(cfg.config_file_path, "backup.mysql", mysql)
        changed = changed or c
    archive = _profile_archive_payload(payload)
    if archive:
        _, c = upsert_section_values(cfg.config_file_path, "backup.archive", archive)
        changed = changed or c
    return changed


def _config_profile_payload(cfg: AgentConfig) -> dict[str, Any]:
    """
    Read explicit backup profile fragments from mutable config file.
    Only sections present in text config are returned.
    """
    p = resolve_mutable_config_path(cfg.config_file_path)
    if not p.exists():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    backup = raw.get("backup")
    if not isinstance(backup, dict):
        return {}
    out: dict[str, Any] = {}
    storage = _profile_storage_payload({"storage": backup.get("storage", {})})
    if storage and str(storage.get("host") or "").strip() and str(storage.get("user") or "").strip():
        out["storage"] = storage
    mysql = _profile_mysql_payload({"mysql": backup.get("mysql", {})})
    if mysql and any(str(mysql.get(k) or "").strip() for k in ("host", "user", "password", "database")):
        out["mysql"] = mysql
    archive = _profile_archive_payload({"archive": backup.get("archive", {})})
    if archive:
        out["archive"] = archive
    backup_settings = _profile_backup_payload(backup)
    if backup_settings:
        out.update(backup_settings)
    return out


def backup_profile_sync_from_config(cfg: AgentConfig) -> dict[str, Any]:
    """
    Sync explicit [backup.*] values from mutable text config into profile DB.
    This keeps hidden runtime profile in DB aligned with operator edits.
    """
    from_cfg = _config_profile_payload(cfg)
    if not from_cfg:
        return {"status": "skipped", "reason": "no_backup_profile_sections"}
    current = backup_profile_get(cfg)
    merged = _deep_merge(current, from_cfg)
    if merged == current:
        return {"status": "ok", "changed": False, "keys": []}
    backup_profile_set(cfg, from_cfg, merge=True, sync_config=False)
    return {"status": "ok", "changed": True, "keys": sorted(from_cfg.keys())}


def backup_profile_set(
    cfg: AgentConfig,
    payload: dict[str, Any],
    *,
    merge: bool = True,
    sync_config: bool = True,
) -> dict[str, Any]:
    current = backup_profile_get(cfg) if merge else {}
    merged = _deep_merge(current, payload)
    token = _profile_crypto(cfg).encrypt(json.dumps(merged, ensure_ascii=True, separators=(",", ":")))
    con = _profile_store_conn(cfg)
    try:
        con.execute(
            """
            INSERT INTO backup_profile(id, payload_enc, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload_enc=excluded.payload_enc, updated_at=excluded.updated_at
            """,
            (_PROFILE_ROW_ID, token, time.time()),
        )
        con.commit()
    finally:
        con.close()
    if sync_config:
        try:
            _sync_profile_payload_to_config(cfg, merged)
        except Exception:
            # Non-fatal: profile DB is authoritative for backup runtime.
            pass
    return merged


def _mask_value(v: str | None) -> str:
    if not v:
        return ""
    if len(v) <= 3:
        return "***"
    return v[:1] + "***" + v[-1:]


def backup_profile_masked(cfg: AgentConfig) -> dict[str, Any]:
    p = backup_profile_get(cfg)
    out = json.loads(json.dumps(p, ensure_ascii=True)) if p else {}
    storage = out.get("storage")
    if isinstance(storage, dict) and storage.get("password") is not None:
        storage["password"] = _mask_value(str(storage.get("password") or ""))
    mysql = out.get("mysql")
    if isinstance(mysql, dict) and mysql.get("password") is not None:
        mysql["password"] = _mask_value(str(mysql.get("password") or ""))
    return out


def _effective_cfg(cfg: AgentConfig) -> AgentConfig:
    p = backup_profile_get(cfg)
    if not p:
        return cfg
    storage = p.get("storage") if isinstance(p.get("storage"), dict) else {}
    mysql = p.get("mysql") if isinstance(p.get("mysql"), dict) else {}
    archive = p.get("archive") if isinstance(p.get("archive"), dict) else {}
    backup = _profile_backup_payload(p)
    out = cfg
    if backup:
        out = replace(
            out,
            backup_remote_root_dir=str(backup.get("remote_root_dir")).strip()
            if backup.get("remote_root_dir")
            else out.backup_remote_root_dir,
        )
    if storage:
        out = replace(
            out,
            backup_ssh_host=str(storage.get("host")).strip() if storage.get("host") else out.backup_ssh_host,
            backup_ssh_port=int(storage.get("port")) if storage.get("port") is not None else out.backup_ssh_port,
            backup_ssh_user=str(storage.get("user")).strip() if storage.get("user") else out.backup_ssh_user,
            backup_ssh_remote_path=str(storage.get("remote_path")).strip() if storage.get("remote_path") else out.backup_ssh_remote_path,
            backup_ssh_key_file=str(storage.get("key_file")).strip() if storage.get("key_file") else out.backup_ssh_key_file,
            backup_ssh_password=str(storage.get("password")) if storage.get("password") else out.backup_ssh_password,
        )
    if mysql:
        out = replace(
            out,
            backup_mysql_host=str(mysql.get("host")).strip() if mysql.get("host") else out.backup_mysql_host,
            backup_mysql_port=int(mysql.get("port")) if mysql.get("port") is not None else out.backup_mysql_port,
            backup_mysql_user=str(mysql.get("user")).strip() if mysql.get("user") else out.backup_mysql_user,
            backup_mysql_password=str(mysql.get("password")) if mysql.get("password") else out.backup_mysql_password,
            backup_mysql_database=str(mysql.get("database")).strip() if mysql.get("database") else out.backup_mysql_database,
        )
    if archive:
        out = replace(
            out,
            backup_archive_enabled=bool(archive.get("enabled")) if archive.get("enabled") is not None else out.backup_archive_enabled,
            backup_archive_name=str(archive.get("name")).strip() if archive.get("name") else out.backup_archive_name,
            backup_archive_paths=[str(x) for x in archive.get("paths", []) if str(x).strip()]
            if isinstance(archive.get("paths"), list)
            else out.backup_archive_paths,
        )
    return out


def _validate_cfg(cfg: AgentConfig) -> None:
    if not cfg.backup_enabled:
        raise RuntimeError("backup is disabled in config ([backup].enabled=false)")
    if _backup_method(cfg) not in {"mydumper", "xtrabackup"}:
        raise RuntimeError("backup method must be 'mydumper' or 'xtrabackup'")
    if not cfg.backup_ssh_host or not cfg.backup_ssh_user:
        raise RuntimeError("backup storage is not configured ([backup.storage].host/user)")
    if not cfg.backup_ssh_key_file and not cfg.backup_ssh_password:
        raise RuntimeError("backup storage auth is not configured (key_file or password required)")


def _backup_method(cfg: AgentConfig) -> str:
    method = str(getattr(cfg, "backup_method", "mydumper") or "mydumper").strip().lower()
    if method in {"logical", "dump", "mydump"}:
        return "mydumper"
    if method in {"physical", "xtra", "xtrabackup"}:
        return "xtrabackup"
    return method


def _apt_install_packages(packages: list[str]) -> None:
    wanted = [p for p in [str(x or "").strip() for x in packages] if p]
    if not wanted:
        return
    if not shutil.which("apt-get"):
        raise RuntimeError(f"missing required backup packages and apt-get is unavailable: {', '.join(wanted)}")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    _run(["apt-get", "update"], timeout_sec=900, env=env, check=True)
    _run(["apt-get", "install", "-y", "--no-install-recommends"] + wanted, timeout_sec=1800, env=env, check=True)


def _ensure_backup_tools(cfg: AgentConfig) -> dict[str, Any]:
    method = _backup_method(cfg)
    required: list[tuple[str, str]] = []
    if cfg.backup_ssh_host and cfg.backup_ssh_user:
        required.append(("sshfs", cfg.backup_sshfs_package))
    if method == "mydumper":
        required.append((cfg.backup_mydumper_bin, cfg.backup_mydumper_package))
    elif method == "xtrabackup":
        required.append((cfg.backup_xtrabackup_bin, cfg.backup_xtrabackup_package))
    missing = [(bin_path, pkg) for bin_path, pkg in required if not shutil.which(bin_path) and not Path(bin_path).exists()]
    installed: list[str] = []
    if missing:
        packages = []
        seen: set[str] = set()
        for _bin, pkg in missing:
            pkg = str(pkg or "").strip()
            if pkg and pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)
        if not cfg.backup_auto_install_packages:
            raise RuntimeError("missing required backup tools: " + ", ".join(bin_path for bin_path, _ in missing))
        _apt_install_packages(packages)
        installed = packages
    still_missing = [bin_path for bin_path, _ in required if not shutil.which(bin_path) and not Path(bin_path).exists()]
    if still_missing:
        raise RuntimeError("required backup tools are still missing after package preflight: " + ", ".join(still_missing))
    return {"method": method, "installed_packages": installed}


def _mount(cfg: AgentConfig, mount_path: Path) -> None:
    mount_path.mkdir(parents=True, exist_ok=True)
    if _mounted(mount_path):
        _unmount(mount_path, cfg.backup_unmount_timeout_sec)

    opts = [
        "reconnect",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "StrictHostKeyChecking=accept-new",
        f"port={cfg.backup_ssh_port}",
    ]
    if cfg.backup_ssh_key_file:
        opts.append(f"IdentityFile={cfg.backup_ssh_key_file}")
    if cfg.backup_ssh_password:
        opts.append("password_stdin")

    remote = f"{cfg.backup_ssh_user}@{cfg.backup_ssh_host}:{cfg.backup_ssh_remote_path}"
    cmd = ["sshfs", remote, str(mount_path), "-o", ",".join(opts)]
    if cfg.backup_ssh_password:
        proc = _run(
            cmd,
            timeout_sec=cfg.backup_mount_timeout_sec,
            input_text=cfg.backup_ssh_password + "\n",
            check=True,
        )
    else:
        proc = _run(cmd, timeout_sec=cfg.backup_mount_timeout_sec, check=True)
    if proc.returncode != 0 or not _mounted(mount_path):
        raise RuntimeError("sshfs mount did not appear in /proc/mounts")


def _mysql_defaults_file(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    socket_path: str | None = None,
) -> tempfile.NamedTemporaryFile:
    def opt_value(value: str) -> str:
        # MySQL option files treat leading "#" as comments unless the value is quoted.
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    tf = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
    for group in ("client", "xtrabackup"):
        tf.write(f"[{group}]\n")
        if socket_path:
            tf.write(f"socket={opt_value(socket_path)}\n")
            tf.write("protocol=socket\n")
        else:
            tf.write(f"host={opt_value(host)}\n")
            tf.write(f"port={port}\n")
        tf.write(f"user={opt_value(user)}\n")
        tf.write(f"password={opt_value(password)}\n")
    tf.flush()
    tf.close()
    try:
        os.chmod(tf.name, 0o600)
    except Exception:
        pass
    return tf


def _effective_db_for_instance(cfg: AgentConfig, inst: MauticInstall) -> DBConfig:
    if not inst.db:
        raise RuntimeError("Instance DB credentials are not available in inventory")
    if cfg.backup_mysql_user and cfg.backup_mysql_password:
        return DBConfig(
            host=cfg.backup_mysql_host or inst.db.host,
            port=int(cfg.backup_mysql_port or inst.db.port),
            name=cfg.backup_mysql_database or inst.db.name,
            user=cfg.backup_mysql_user,
            password=cfg.backup_mysql_password,
            table_prefix=inst.db.table_prefix,
        )
    return inst.db


def _mydumper_supports_flag(bin_path: str, flag: str) -> bool:
    key = (bin_path, flag)
    if key in _MYDUMPER_FLAG_CACHE:
        return _MYDUMPER_FLAG_CACHE[key]
    try:
        proc = subprocess.run(
            [bin_path, "--help"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        supported = flag in text
    except Exception:
        supported = False
    _MYDUMPER_FLAG_CACHE[key] = supported
    return supported


def _xtrabackup_supports_flag(bin_path: str, flag: str) -> bool:
    key = (bin_path, flag)
    if key in _XTRABACKUP_FLAG_CACHE:
        return _XTRABACKUP_FLAG_CACHE[key]
    try:
        proc = subprocess.run(
            [bin_path, "--help"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        supported = flag in text
    except Exception:
        supported = False
    _XTRABACKUP_FLAG_CACHE[key] = supported
    return supported


def _priority_prefix(cfg: AgentConfig) -> list[str]:
    prefix: list[str] = []
    if cfg.backup_mydumper_use_ionice and shutil.which("ionice"):
        ionice_class = max(1, min(3, int(cfg.backup_mydumper_ionice_class)))
        ionice_level = max(0, min(7, int(cfg.backup_mydumper_ionice_level)))
        prefix += ["ionice", "-c", str(ionice_class)]
        if ionice_class in {1, 2}:
            prefix += ["-n", str(ionice_level)]
    if cfg.backup_mydumper_use_nice and shutil.which("nice"):
        nice_level = max(-20, min(19, int(cfg.backup_mydumper_nice_level)))
        prefix += ["nice", "-n", str(nice_level)]
    return prefix


def _backup_db_from_config_or_instances(cfg: AgentConfig, db_instances: list[MauticInstall]) -> DBConfig:
    if cfg.backup_mysql_user and cfg.backup_mysql_password:
        first_db = db_instances[0].db if db_instances and db_instances[0].db else None
        return DBConfig(
            host=cfg.backup_mysql_host or (first_db.host if first_db else "localhost"),
            port=int(cfg.backup_mysql_port or (first_db.port if first_db else 3306)),
            name=cfg.backup_mysql_database or (first_db.name if first_db else ""),
            user=cfg.backup_mysql_user,
            password=cfg.backup_mysql_password,
            table_prefix=first_db.table_prefix if first_db else "",
        )
    if db_instances and db_instances[0].db:
        return _effective_db_for_instance(cfg, db_instances[0])
    raise RuntimeError("No DB credentials found for backup")


def _mysqldump_bin() -> str:
    for name in ("mariadb-dump", "mysqldump"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("mysqldump/mariadb-dump client is missing")


def _run_mysqldump_gz(cfg: AgentConfig, db: DBConfig, output_path: Path) -> int:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    dump_cmd = [
        _mysqldump_bin(),
        f"--defaults-extra-file={defaults.name}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--events",
        db.name,
    ]
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        with gzip.open(output_path, "wb") as gz:
            shutil.copyfileobj(proc.stdout, gz)
        _out, err = proc.communicate(timeout=cfg.backup_dump_timeout_sec)
        if proc.returncode != 0:
            msg = (err or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"mysqldump failed for {db.name}: {msg or proc.returncode}")
        return int(output_path.stat().st_size)
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _mysql_capture(cfg: AgentConfig, db: DBConfig, sql: str) -> str:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        proc = _run(
            ["mysql", f"--defaults-extra-file={defaults.name}", "-N", "-s", "-e", sql],
            timeout_sec=min(max(cfg.backup_mount_timeout_sec, 15), 120),
            check=True,
        )
        return str(proc.stdout or "").strip()
    except Exception:
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        proc = _run(
            ["mysql", "-h", db.host, "-P", str(db.port), "-u", db.user, "-N", "-s", "-e", sql],
            timeout_sec=min(max(cfg.backup_mount_timeout_sec, 15), 120),
            env=env,
            check=True,
        )
        return str(proc.stdout or "").strip()
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _mysql_server_snapshot(cfg: AgentConfig, db: DBConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        raw = _mysql_capture(
            cfg,
            db,
            "SELECT @@hostname, @@server_id, @@global.read_only, @@global.super_read_only, "
            "@@global.gtid_mode, @@global.log_bin, @@version",
        )
        parts = raw.split("\t")
        if len(parts) >= 7:
            out.update(
                {
                    "hostname": parts[0],
                    "server_id": parts[1],
                    "read_only": parts[2],
                    "super_read_only": parts[3],
                    "gtid_mode": parts[4],
                    "log_bin": parts[5],
                    "version": parts[6],
                }
            )
    except Exception as e:
        out["server_snapshot_error"] = str(e)
    try:
        raw = _mysql_capture(
            cfg,
            db,
            "SHOW REPLICA STATUS",
        )
        if raw:
            out["replica_status_available"] = True
    except Exception:
        try:
            raw = _mysql_capture(cfg, db, "SHOW SLAVE STATUS")
            if raw:
                out["replica_status_available"] = True
        except Exception:
            pass
    return out


def _effective_long_query_guard(value: int) -> int:
    # Operator value <= 0 is treated as "disable guard".
    # mydumper interprets 0 as "abort if any query runs >0s", so use a very
    # large guard to emulate disabled behavior safely.
    return 2_147_483_647 if int(value) <= 0 else int(value)


def _build_mydumper_cmd(
    cfg: AgentConfig,
    db: DBConfig,
    output_dir: Path,
    defaults_file: str,
) -> list[str]:
    extra_args = _effective_mydumper_extra_args(cfg, cfg.backup_mydumper_extra_args)
    cmd = _priority_prefix(cfg) + [
        cfg.backup_mydumper_bin,
        f"--defaults-file={defaults_file}",
        "-B",
        db.name,
        "-o",
        str(output_dir),
        "--threads",
        str(cfg.backup_mydumper_threads),
        "--verbose",
        str(cfg.backup_mydumper_verbose),
    ]
    cmd += ["--long-query-guard", str(_effective_long_query_guard(cfg.backup_mydumper_long_query_guard))]
    if cfg.backup_mydumper_kill_long_queries:
        cmd.append("--kill-long-queries")
    if cfg.backup_mydumper_compress:
        cmd.append("--compress")
    cmd += extra_args
    return cmd


def _effective_mydumper_extra_args(cfg: AgentConfig, extra_args: list[str]) -> list[str]:
    # Keep lock behavior soft by default:
    # - prefer trx snapshot mode for InnoDB;
    # - avoid forcing hard lock modes (FTWRL/LOCK_ALL).
    # Operators can still override explicitly in extra_args.
    out = [str(x).strip() for x in extra_args if str(x).strip()]
    has_trx_tables = any(a == "--trx-tables" or a.startswith("--trx-tables=") for a in out)
    has_trx_consistency_only = any(
        a == "--trx-consistency-only" or a.startswith("--trx-consistency-only=")
        for a in out
    )
    has_lock_mode = any(a.startswith("--sync-thread-lock-mode") for a in out)
    if not has_lock_mode and _mydumper_supports_flag(cfg.backup_mydumper_bin, "--sync-thread-lock-mode"):
        out.append("--sync-thread-lock-mode=AUTO")
    if not has_trx_tables and not has_trx_consistency_only:
        if _mydumper_supports_flag(cfg.backup_mydumper_bin, "--trx-tables"):
            out.append("--trx-tables")
        else:
            out.append("--trx-consistency-only")
    return out


def _has_mydumper_arg(args: list[str], names: set[str]) -> bool:
    for arg in args:
        value = str(arg or "").strip()
        if value in names:
            return True
        for name in names:
            if value.startswith(f"{name}="):
                return True
    return False


def _cluster_offsite_mydumper_cfg(cfg: AgentConfig) -> AgentConfig:
    extra_args = [str(x).strip() for x in list(getattr(cfg, "backup_mydumper_extra_args", []) or []) if str(x).strip()]
    if not _has_mydumper_arg(extra_args, {"--rows", "-r", "--chunk-filesize", "-F"}):
        # Large Mautic tracking tables otherwise become one huge single-threaded
        # file. Chunking lets mydumper use the configured parallelism.
        extra_args.append("--rows=500000")
    if (
        not _has_mydumper_arg(extra_args, {"--compress-protocol"})
        and _mydumper_supports_flag(cfg.backup_mydumper_bin, "--compress-protocol")
    ):
        extra_args.append("--compress-protocol")
    return replace(
        cfg,
        backup_method="mydumper",
        backup_mydumper_threads=max(16, int(getattr(cfg, "backup_mydumper_threads", 6) or 6)),
        backup_mydumper_extra_args=extra_args,
    )


def _is_long_query_guard_abort(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "long-query-guard" not in msg:
        return False
    return (
        "queries in processlist running longer than" in msg
        or "aborting dump" in msg
    )


def _run_mydumper(
    cfg: AgentConfig,
    db: DBConfig,
    output_dir: Path,
    *,
    socket_path: str | None = None,
) -> None:
    defaults = _mysql_defaults_file(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        socket_path=socket_path,
    )
    primary_exc: Exception | None = None
    try:
        cmd = _build_mydumper_cmd(cfg, db, output_dir, defaults.name)
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
        return
    except Exception as e:
        primary_exc = e
        # Some mydumper versions return non-zero even when dump files are complete
        # (e.g. global lock warnings with --trx-consistency-only).
        ok, _, _ = _verify_dump_dir(output_dir)
        if ok:
            return
        if _is_long_query_guard_abort(e):
            # Fallback safety net: when long-query-guard trips, rerun with guard
            # effectively disabled so dump can proceed on busy production DBs.
            no_guard_cfg = replace(cfg, backup_mydumper_long_query_guard=0)
            cmd_no_guard = _build_mydumper_cmd(no_guard_cfg, db, output_dir, defaults.name)
            try:
                _run(cmd_no_guard, timeout_sec=no_guard_cfg.backup_dump_timeout_sec, check=True)
                return
            except Exception:
                ok, _, _ = _verify_dump_dir(output_dir)
                if ok:
                    return
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        extra_args = _effective_mydumper_extra_args(cfg, cfg.backup_mydumper_extra_args)
        guard_value = cfg.backup_mydumper_long_query_guard
        if _is_long_query_guard_abort(e):
            guard_value = 0
        fallback = _priority_prefix(cfg) + [
            cfg.backup_mydumper_bin,
            "-u",
            db.user,
            "-B",
            db.name,
            "-o",
            str(output_dir),
            "--threads",
            str(cfg.backup_mydumper_threads),
            "--verbose",
            str(cfg.backup_mydumper_verbose),
        ]
        if socket_path:
            fallback += ["--socket", socket_path]
        else:
            fallback += ["-h", db.host, "-P", str(db.port)]
        fallback += ["--long-query-guard", str(_effective_long_query_guard(guard_value))]
        if cfg.backup_mydumper_kill_long_queries:
            fallback.append("--kill-long-queries")
        if cfg.backup_mydumper_compress:
            fallback.append("--compress")
        fallback += extra_args
        try:
            _run(fallback, timeout_sec=cfg.backup_dump_timeout_sec, env=env, check=True)
            return
        except Exception:
            ok, _, _ = _verify_dump_dir(output_dir)
            if ok:
                return
            if primary_exc is not None:
                raise primary_exc
            raise
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _effective_xtrabackup_extra_args(cfg: AgentConfig) -> list[str]:
    out = [str(x).strip() for x in cfg.backup_xtrabackup_extra_args if str(x).strip()]
    if not any(a == "--slave-info" or a.startswith("--slave-info=") for a in out):
        if _xtrabackup_supports_flag(cfg.backup_xtrabackup_bin, "--slave-info"):
            out.append("--slave-info")
    return out


def _run_xtrabackup(
    cfg: AgentConfig,
    db: DBConfig,
    output_dir: Path,
    *,
    incremental_base_dir: Path | None = None,
) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        parallel = max(1, int(cfg.backup_xtrabackup_parallel or 1))
        cmd = _priority_prefix(cfg) + [
            cfg.backup_xtrabackup_bin,
            f"--defaults-file={defaults.name}",
            "--backup",
            f"--target-dir={output_dir}",
            "--parallel",
            str(parallel),
        ]
        if incremental_base_dir is not None:
            cmd.append(f"--incremental-basedir={incremental_base_dir}")
        cmd += _effective_xtrabackup_extra_args(cfg)
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _verify_xtrabackup_dir(path: Path) -> tuple[bool, str, int]:
    if not path.exists() or not path.is_dir():
        return False, "xtrabackup directory missing", 0
    checkpoints = path / "xtrabackup_checkpoints"
    if not checkpoints.exists():
        return False, "xtrabackup_checkpoints missing", 0
    try:
        text = checkpoints.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""
    if "backup_type" not in text:
        return False, "xtrabackup_checkpoints is invalid", 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += int(f.stat().st_size)
        except Exception:
            pass
    if total <= 0:
        return False, "xtrabackup output is empty", 0
    return True, "ok", total


def _cluster_offsite_mysql_root(cfg: AgentConfig) -> Path:
    return _cluster_db_root(cfg) / "offsite-mysql"


def _clone_xtrabackup_for_offsite_prepare(cfg: AgentConfig, full_dir: Path) -> Path:
    src = full_dir / "physical-xtrabackup"
    ok, msg, _ = _verify_xtrabackup_dir(src)
    if not ok:
        raise RuntimeError(f"local full xtrabackup is not usable for offsite source: {msg}")
    root = _cluster_offsite_mysql_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_prepared_mysql_processes(cfg)
    active_prepared = {
        item["datadir"].resolve(strict=False): int(item["pid"])
        for item in _cluster_prepared_mysql_processes(cfg)
        if isinstance(item.get("datadir"), Path)
    }
    for old in root.iterdir():
        if old.is_dir() and old.name.startswith("prepared-"):
            pid = active_prepared.get(old.resolve(strict=False))
            if pid:
                raise RuntimeError(f"refusing to remove prepared offsite mysql datadir still used by pid {pid}: {old}")
            shutil.rmtree(old, ignore_errors=True)
    clone_dir = root / f"prepared-{_fmt_local_ts()}"
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=False)
    try:
        # This must be a reflink clone, not a full local copy: the cluster
        # replica intentionally has no spare 1.4T cache area for another DB copy.
        _run(
            ["cp", "-a", "--reflink=always", f"{src}/.", str(clone_dir)],
            timeout_sec=cfg.backup_dump_timeout_sec,
            check=True,
        )
    except Exception as e:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise RuntimeError(f"failed to create reflink clone of local xtrabackup source: {e}") from e
    return clone_dir


def _prepare_xtrabackup_clone_for_mysql(cfg: AgentConfig, clone_dir: Path) -> None:
    checkpoint = _xtrabackup_checkpoint_text(clone_dir).lower()
    if "full-prepared" not in checkpoint:
        _run(
            [cfg.backup_xtrabackup_bin, "--prepare", f"--target-dir={clone_dir}"],
            timeout_sec=cfg.backup_dump_timeout_sec,
            check=True,
        )
    if shutil.which("chown"):
        # MySQL refuses to run as root. Chowning the reflink clone changes
        # metadata only and keeps the source xtrabackup snapshot untouched.
        _run(["chown", "-R", "mysql:mysql", str(clone_dir)], timeout_sec=cfg.backup_dump_timeout_sec, check=True)


def _mysqladmin_ping(socket_path: Path) -> bool:
    defaults = _mysql_defaults_file(host="localhost", port=0, user="root", password="", socket_path=str(socket_path))
    try:
        proc = _run(
            ["mysqladmin", f"--defaults-extra-file={defaults.name}", "ping"],
            timeout_sec=10,
            check=False,
        )
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
        return proc.returncode == 0 and "mysqld is alive" in text
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _start_prepared_xtrabackup_mysql(cfg: AgentConfig, datadir: Path) -> _PreparedMysqlRuntime:
    mysqld = shutil.which("mysqld") or shutil.which("mariadbd")
    if not mysqld:
        raise RuntimeError("mysqld/mariadbd binary not found for xtrabackup offsite source")
    run_dir = Path(tempfile.mkdtemp(prefix="mcd-offsite-mysql-"))
    socket_path = run_dir / "mysql.sock"
    tmp_dir = run_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    error_log = run_dir / "error.log"
    pid_file = run_dir / "mysql.pid"
    if shutil.which("chown"):
        _run(["chown", "-R", "mysql:mysql", str(run_dir)], timeout_sec=cfg.backup_mount_timeout_sec, check=True)
    cmd = [
        mysqld,
        "--no-defaults",
        f"--datadir={datadir}",
        f"--socket={socket_path}",
        f"--pid-file={pid_file}",
        f"--log-error={error_log}",
        f"--tmpdir={tmp_dir}",
        "--skip-networking",
        "--skip-log-bin",
        "--skip-grant-tables",
        "--read-only=ON",
        "--super-read-only=ON",
        "--user=mysql",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + min(max(cfg.backup_mount_timeout_sec, 60), 300)
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            err = error_log.read_text(encoding="utf-8", errors="ignore")[-4000:] if error_log.exists() else ""
            shutil.rmtree(run_dir, ignore_errors=True)
            raise RuntimeError(f"temporary offsite mysqld exited early rc={rc}: {err.strip()}")
        if socket_path.exists() and _mysqladmin_ping(socket_path):
            return _PreparedMysqlRuntime(socket_path=socket_path, datadir=datadir, run_dir=run_dir, process=proc)
        time.sleep(1)
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except Exception:
        proc.kill()
    err = error_log.read_text(encoding="utf-8", errors="ignore")[-4000:] if error_log.exists() else ""
    shutil.rmtree(run_dir, ignore_errors=True)
    raise RuntimeError(f"temporary offsite mysqld did not become ready: {err.strip()}")


def _stop_prepared_xtrabackup_mysql(runtime: _PreparedMysqlRuntime) -> None:
    proc = runtime.process
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=120)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=30)
            except Exception:
                pass
    shutil.rmtree(runtime.run_dir, ignore_errors=True)


def _run_mydumper_from_xtrabackup_full(
    cfg: AgentConfig,
    db: DBConfig,
    full_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    clone_dir: Path | None = None
    runtime: _PreparedMysqlRuntime | None = None
    try:
        clone_dir = _clone_xtrabackup_for_offsite_prepare(cfg, full_dir)
        _prepare_xtrabackup_clone_for_mysql(cfg, clone_dir)
        runtime = _start_prepared_xtrabackup_mysql(cfg, clone_dir)
        temp_db = DBConfig(
            host="localhost",
            port=0,
            name=db.name,
            user="root",
            password="",
            table_prefix=db.table_prefix,
        )
        _run_mydumper(replace(cfg, backup_method="mydumper"), temp_db, output_dir, socket_path=str(runtime.socket_path))
        return {
            "offsite_db_source": "xtrabackup",
            "offsite_xtrabackup_full_path": str(full_dir),
            "offsite_temp_mysql": "prepared_reflink_clone",
        }
    finally:
        if runtime is not None:
            _stop_prepared_xtrabackup_mysql(runtime)
        if clone_dir is not None:
            shutil.rmtree(clone_dir, ignore_errors=True)


def _run_mysql_sql(cfg: AgentConfig, db: DBConfig, sql: str) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        _run(["mysql", f"--defaults-extra-file={defaults.name}", "-e", sql], timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    except Exception:
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        _run(
            ["mysql", "-h", db.host, "-P", str(db.port), "-u", db.user, "-e", sql],
            timeout_sec=cfg.backup_dump_timeout_sec,
            env=env,
            check=True,
        )
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _run_myloader(cfg: AgentConfig, db: DBConfig, dump_dir: Path) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        cmd = [
            cfg.backup_myloader_bin,
            f"--defaults-file={defaults.name}",
            "-B",
            db.name,
            "-d",
            str(dump_dir),
            "--threads",
            str(cfg.backup_myloader_threads),
            "--overwrite-tables",
        ]
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    except Exception:
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        fallback = [
            cfg.backup_myloader_bin,
            "-h",
            db.host,
            "-P",
            str(db.port),
            "-u",
            db.user,
            "-B",
            db.name,
            "-d",
            str(dump_dir),
            "--threads",
            str(cfg.backup_myloader_threads),
            "--overwrite-tables",
        ]
        _run(fallback, timeout_sec=cfg.backup_dump_timeout_sec, env=env, check=True)
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _candidate_db(dump_dir: Path, instances: list[MauticInstall], cfg: AgentConfig) -> DBConfig | None:
    name = dump_dir.name
    db_name = ""
    if "__" in name:
        db_name = name.split("__", 1)[1].strip()
    if cfg.backup_mysql_user and cfg.backup_mysql_password:
        host = cfg.backup_mysql_host or "localhost"
        port = int(cfg.backup_mysql_port or 3306)
        chosen = cfg.backup_mysql_database or db_name
        if not chosen:
            return None
        return DBConfig(
            host=host,
            port=port,
            name=chosen,
            user=cfg.backup_mysql_user,
            password=cfg.backup_mysql_password,
            table_prefix="",
        )

    for inst in instances:
        if not inst.db:
            continue
        if db_name and inst.db.name == db_name:
            return inst.db
    return None


def _resolve_restore_source(
    *,
    remote_parent: Path,
    date: str | None,
    path: str | None,
) -> Path:
    if path:
        p = Path(path)
        if p.exists() and p.is_dir():
            return p
        rel = remote_parent / path.strip().strip("/")
        if rel.exists() and rel.is_dir():
            return rel
        raise RuntimeError(f"restore source not found: {path}")

    if date:
        d = date.strip()
        if not _DATE_RE.match(d):
            raise RuntimeError("date must be YYYY-MM-DD")
        p = remote_parent / d
        if not p.exists() or not p.is_dir():
            raise RuntimeError(f"backup date not found: {d}")
        return p

    candidates = [x for x in remote_parent.iterdir() if x.is_dir() and _DATE_RE.match(x.name)]
    if not candidates:
        raise RuntimeError(f"no backups found in {remote_parent}")
    candidates.sort(key=lambda x: x.name, reverse=True)
    return candidates[0]


def _read_backup_marker(backup_dir: Path) -> dict[str, Any]:
    p = backup_dir / ".mcd-backup.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-") or "cluster"


def _cluster_name(cfg: AgentConfig) -> str:
    raw = (
        str(getattr(cfg, "cluster_name", "") or "").strip()
        or str(getattr(cfg, "cluster_id", "") or "").strip()
        or str(getattr(cfg, "backup_instance_name", "") or "").strip()
        or str(getattr(cfg, "backup_host_name", "") or "").strip()
        or _host_name(cfg)
    )
    return raw.strip("/") or "cluster"


def _cluster_local_identity_values(cfg: AgentConfig) -> set[str]:
    values = {
        str(getattr(cfg, "mcc_host_name", "") or "").strip(),
        str(getattr(cfg, "backup_instance_name", "") or "").strip(),
        str(getattr(cfg, "backup_host_name", "") or "").strip(),
        _host_name(cfg),
    }
    try:
        values.add(socket.gethostname().strip())
        values.add(socket.getfqdn().strip())
    except Exception:
        pass
    return {v.lower() for v in values if v}


def cluster_backup_authority_status(config: AgentConfig) -> dict[str, Any]:
    # Do not call _effective_cfg() here: authority must be cheap and safe even
    # before backup profile storage exists, and it is used by daemon scheduler
    # on non-authority nodes.
    cfg = config
    cluster_id = str(getattr(cfg, "cluster_id", "") or "").strip()
    role = str(getattr(cfg, "cluster_node_role", "") or "").strip().lower()
    required_role = str(getattr(cfg, "backup_cluster_authority_role", "replica") or "replica").strip().lower()
    if required_role in {"*", "all"}:
        required_role = "any"
    authority_host = (
        str(getattr(cfg, "backup_cluster_authority_host", "") or "").strip()
        or str(getattr(cfg, "cluster_route_backup_host", "") or "").strip()
    )
    identities = _cluster_local_identity_values(cfg)
    allowed = False
    reason = ""
    if not bool(getattr(cfg, "backup_cluster_enabled", False)):
        reason = "cluster backup disabled"
    elif authority_host:
        allowed = authority_host.lower() in identities
        reason = "authority host match" if allowed else f"authority host mismatch: required {authority_host}"
    elif not cluster_id:
        reason = "missing cluster_id"
    elif required_role == "any":
        allowed = True
        reason = "authority role any"
    elif role == required_role:
        allowed = True
        reason = f"authority role match: {role}"
    else:
        reason = f"authority role mismatch: node role {role or '-'}, required {required_role}"
    return {
        "allowed": allowed,
        "reason": reason,
        "cluster_id": cluster_id,
        "cluster_name": str(getattr(cfg, "cluster_name", "") or "").strip(),
        "node_role": role,
        "node_index": getattr(cfg, "cluster_node_index", None),
        "authority_role": required_role,
        "authority_host": authority_host,
        "local_identities": sorted(identities),
    }


def _ensure_cluster_backup_authority(cfg: AgentConfig) -> None:
    status = cluster_backup_authority_status(cfg)
    if bool(status.get("allowed")):
        return
    raise RuntimeError(f"cluster backup is not allowed on this node: {status.get('reason')}")


def _cluster_state_path(cfg: AgentConfig) -> Path:
    # Keep host-level backup state as the push source for MCC; cluster mode is
    # represented by the selected backup/replica node.
    return _state_path(cfg)


def _cluster_local_root(cfg: AgentConfig) -> Path:
    root = Path(str(getattr(cfg, "backup_cluster_local_root_dir", "") or "").strip())
    if not root.is_absolute() or str(root) in {"", "/"}:
        raise RuntimeError("backup.cluster.local_root_dir must be an absolute non-root path")
    return root


def _cluster_lock_path(cfg: AgentConfig, name: str) -> Path:
    return Path(cfg.backup_lock_dir) / f"backup-cluster-{_safe_slug(_cluster_name(cfg))}-{name}.lock"


def _lock_active(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = path.open("w", encoding="utf-8")
    locked = False
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        return False
    except BlockingIOError:
        return True
    finally:
        if locked:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lock_fh.close()
        except Exception:
            pass


def _proc_cmdline(pid_dir: Path) -> str:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _cluster_offsite_processes(cfg: AgentConfig) -> list[int]:
    """Detect offsite jobs that survived an MCD restart.

    The daemon uses flock while it is alive, but long-running offsite child
    processes intentionally keep running across MCD restarts. A fresh daemon
    must therefore treat matching children as active cluster backups even when
    the old lock FD is gone.
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    mount_path = str(Path(cfg.backup_mount_base_dir) / _host_slug(cfg)).rstrip("/")
    cluster_name = _cluster_name(cfg)
    if not mount_path or not cluster_name:
        return []
    daily_marker = f"{mount_path}/backup/{cluster_name}/daily/.incomplete-"
    matches: list[int] = []
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cmdline = _proc_cmdline(pid_dir)
        if not cmdline:
            continue
        if daily_marker not in cmdline:
            continue
        if "/databases/cluster__" in cmdline or "/files-snapshot-" in cmdline:
            matches.append(int(pid_dir.name))
    matches.extend(item["pid"] for item in _cluster_prepared_mysql_processes(cfg))
    return sorted(set(matches))


_MYSQL_DATADIR_ARG_RE = re.compile(r"(?:^|\s)--datadir=([^\s]+)")


def _cluster_prepared_mysql_datadir_from_cmdline(cfg: AgentConfig, cmdline: str) -> Path | None:
    if "mcd-offsite-mysql-" not in cmdline:
        return None
    if "--skip-networking" not in cmdline or "--skip-grant-tables" not in cmdline:
        return None
    match = _MYSQL_DATADIR_ARG_RE.search(cmdline)
    if not match:
        return None
    datadir = Path(match.group(1))
    if not datadir.is_absolute():
        return None
    root = _cluster_offsite_mysql_root(cfg)
    try:
        rel = datadir.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return None
    if not rel.parts or not rel.parts[0].startswith("prepared-"):
        return None
    return datadir


def _cluster_prepared_mysql_processes(cfg: AgentConfig) -> list[dict[str, Any]]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    matches: list[dict[str, Any]] = []
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cmdline = _proc_cmdline(pid_dir)
        if not cmdline:
            continue
        datadir = _cluster_prepared_mysql_datadir_from_cmdline(cfg, cmdline)
        if datadir is None:
            continue
        matches.append({"pid": int(pid_dir.name), "datadir": datadir, "cmdline": cmdline})
    matches.sort(key=lambda x: int(x["pid"]))
    return matches


def _pid_exists(pid: int) -> bool:
    return Path(f"/proc/{int(pid)}").exists()


def _terminate_pid(pid: int, *, timeout_sec: int = 20) -> bool:
    pid = int(pid)
    if not _pid_exists(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    deadline = time.monotonic() + max(1, int(timeout_sec))
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.25)
    return not _pid_exists(pid)


def _cleanup_stale_prepared_mysql_processes(cfg: AgentConfig) -> list[int]:
    stopped: list[int] = []
    for item in _cluster_prepared_mysql_processes(cfg):
        pid = int(item["pid"])
        datadir = item["datadir"]
        if isinstance(datadir, Path) and datadir.exists():
            continue
        if _terminate_pid(pid):
            stopped.append(pid)
    return stopped


def _cluster_archive_files_snapshot_to_remote(
    cfg: AgentConfig,
    files_snapshot: Path,
    dst_dir: Path,
) -> tuple[Path, int]:
    if not files_snapshot.exists() or not files_snapshot.is_dir():
        raise RuntimeError(f"files snapshot missing: {files_snapshot}")
    dst_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"files-snapshot-{files_snapshot.name}.tar.gz"
    archive_path = dst_dir / archive_name
    if archive_path.exists():
        archive_path.unlink()
    _run(
        ["tar", "-C", str(files_snapshot), "-czf", str(archive_path), "."],
        timeout_sec=cfg.backup_dump_timeout_sec,
        check=True,
    )
    try:
        size = int(archive_path.stat().st_size)
    except Exception:
        size = 0
    if size <= 0:
        raise RuntimeError(f"files snapshot archive is empty: {archive_path}")
    return archive_path, size


def _replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target.name, tmp)
    os.replace(tmp, link)


def _cluster_db_instances(cfg: AgentConfig) -> list[MauticInstall]:
    try:
        return [x for x in _list_instances(cfg) if x.db]
    except Exception:
        if cfg.backup_mysql_user and cfg.backup_mysql_password:
            return []
        raise


def _validate_cluster_cfg(cfg: AgentConfig, *, remote: bool = False) -> None:
    if not bool(getattr(cfg, "backup_cluster_enabled", False)):
        raise RuntimeError("cluster backup is disabled in config ([backup.cluster].enabled=false)")
    _cluster_local_root(cfg)
    if remote:
        if not bool(getattr(cfg, "backup_cluster_remote_enabled", True)):
            raise RuntimeError("cluster remote backup is disabled ([backup.cluster].remote_enabled=false)")
        if not cfg.backup_ssh_host or not cfg.backup_ssh_user:
            raise RuntimeError("backup storage is not configured ([backup.storage].host/user)")
        if not cfg.backup_ssh_key_file and not cfg.backup_ssh_password:
            raise RuntimeError("backup storage auth is not configured (key_file or password required)")


def _ensure_cluster_tools(cfg: AgentConfig, methods: set[str]) -> dict[str, Any]:
    required: list[tuple[str, str]] = []
    if "sshfs" in methods and cfg.backup_ssh_host and cfg.backup_ssh_user:
        required.append(("sshfs", cfg.backup_sshfs_package))
    if "mydumper" in methods:
        required.append((cfg.backup_mydumper_bin, cfg.backup_mydumper_package))
    if "xtrabackup" in methods:
        required.append((cfg.backup_xtrabackup_bin, cfg.backup_xtrabackup_package))
    missing = [(bin_path, pkg) for bin_path, pkg in required if not shutil.which(bin_path) and not Path(bin_path).exists()]
    installed: list[str] = []
    if missing:
        packages: list[str] = []
        seen: set[str] = set()
        for _bin, pkg in missing:
            pkg = str(pkg or "").strip()
            if pkg and pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)
        if not cfg.backup_auto_install_packages:
            raise RuntimeError("missing required backup tools: " + ", ".join(bin_path for bin_path, _ in missing))
        _apt_install_packages(packages)
        installed = packages
    still_missing = [bin_path for bin_path, _ in required if not shutil.which(bin_path) and not Path(bin_path).exists()]
    if still_missing:
        raise RuntimeError("required backup tools are still missing after package preflight: " + ", ".join(still_missing))
    return {"methods": sorted(methods), "installed_packages": installed}


def _cluster_db_root(cfg: AgentConfig) -> Path:
    return _cluster_local_root(cfg) / "db"


def _same_directory(a: Path, b: Path) -> bool:
    try:
        ast = a.stat()
        bst = b.stat()
    except OSError:
        return False
    return ast.st_dev == bst.st_dev and ast.st_ino == bst.st_ino


def _path_is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _ensure_cluster_local_root_outside_mysql_datadir(cfg: AgentConfig, db: DBConfig) -> None:
    raw = _mysql_capture(cfg, db, "SELECT @@datadir")
    datadir = Path(str(raw).strip().splitlines()[0].strip())
    local_root = _cluster_local_root(cfg)
    if not datadir.is_absolute():
        return
    if _path_is_under(local_root, datadir):
        raise RuntimeError(
            f"backup.cluster.local_root_dir must not be inside MySQL datadir: {local_root} under {datadir}"
        )
    # Bind mounts can make two different path prefixes point to the same
    # directory. Walk local_root parents and reject aliases of datadir too.
    for parent in [local_root, *local_root.parents]:
        if str(parent) == "/":
            break
        if _same_directory(parent, datadir):
            raise RuntimeError(
                "backup.cluster.local_root_dir is under a mount/bind alias of MySQL datadir: "
                f"{local_root} via {parent} == {datadir}"
            )


def _cluster_files_root(cfg: AgentConfig) -> Path:
    return _cluster_local_root(cfg) / "files" / "snapshots"


def _cluster_files_sync_root(cfg: AgentConfig) -> Path:
    root = Path(str(getattr(cfg, "backup_cluster_files_sync_dir", "") or "").strip())
    if not root.is_absolute() or str(root) in {"", "/"}:
        raise RuntimeError("backup.cluster.files_sync_dir must be an absolute non-root path")
    return root


def _cluster_file_layers_root(cfg: AgentConfig) -> Path:
    return _cluster_files_sync_root(cfg) / "layers" / _safe_slug(_cluster_name(cfg))


def _cluster_node_slug(cfg: AgentConfig) -> str:
    expected = set(_cluster_expected_file_nodes(cfg))
    if expected:
        local = {_safe_slug(item) for item in cluster_local_identity_values(cfg) if item}
        matches = sorted(expected & local)
        if matches:
            return matches[0]
    node_id = str(getattr(cfg, "mcc_host_name", "") or getattr(cfg, "backup_host_name", "") or "").strip()
    if not node_id:
        node_id = _host_name(cfg)
    return _safe_slug(node_id)


def _cluster_expected_file_nodes(cfg: AgentConfig) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in list(getattr(cfg, "backup_cluster_files_expected_nodes", []) or []):
        slug = _safe_slug(str(item or "").strip())
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _cluster_file_source_paths(cfg: AgentConfig, attr_name: str, fallback: list[str] | None = None) -> list[str]:
    raw = list(getattr(cfg, attr_name, []) or [])
    if not raw and fallback:
        raw = list(fallback)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        p = str(item or "").strip()
        if not p:
            continue
        paths: list[Path]
        if any(ch in p for ch in "*?["):
            base = Path(p)
            parent = base.parent if str(base.parent) else Path(".")
            try:
                paths = sorted(parent.glob(base.name))
            except Exception:
                paths = []
        else:
            paths = [Path(p)]
        for path in paths:
            if not path.exists():
                continue
            key = str(path)
            if _cluster_file_source_path_forbidden(key):
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _cluster_file_source_path_forbidden(path: str) -> bool:
    raw = str(path or "").strip()
    if not raw:
        return True
    normalized = os.path.normpath(raw)
    if not normalized.startswith("/"):
        normalized = "/" + normalized.lstrip("/")
    if normalized in _CLUSTER_FILE_FORBIDDEN_EXACT:
        return True
    for prefix in _CLUSTER_FILE_FORBIDDEN_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _cluster_should_produce_shared(cfg: AgentConfig) -> bool:
    shared_paths = list(getattr(cfg, "backup_cluster_files_shared_paths", []) or [])
    if not shared_paths:
        return False
    wanted = str(getattr(cfg, "backup_cluster_files_shared_producer_host", "") or "").strip()
    if wanted:
        wanted_slug = _safe_slug(wanted)
        return wanted_slug in {_safe_slug(x) for x in cluster_local_identity_values(cfg) if x}
    idx = getattr(cfg, "cluster_node_index", None)
    try:
        return int(idx or 0) == 1
    except Exception:
        return False


def _replace_dir_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    old = dst.with_name(f".{dst.name}.old-{_fmt_local_ts()}")
    if dst.exists() or dst.is_symlink():
        os.replace(dst, old)
    try:
        os.replace(src, dst)
    except Exception:
        if old.exists() and not dst.exists():
            os.replace(old, dst)
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def _rsync_tree(
    src_paths: list[str],
    dst: Path,
    cfg: AgentConfig,
    *,
    link_dest: Path | None = None,
    relative: bool = True,
) -> int:
    if not src_paths:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete", "--numeric-ids", "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r"]
    if relative:
        cmd.append("--relative")
    if link_dest is not None and link_dest.exists():
        cmd.append(f"--link-dest={link_dest}")
    for pattern in _CLUSTER_FILE_RSYNC_EXCLUDES:
        cmd.append(f"--exclude={pattern}")
    for pattern in list(getattr(cfg, "backup_cluster_files_snapshot_exclude", []) or []):
        pattern = str(pattern or "").strip()
        if pattern:
            cmd.append(f"--exclude={pattern}")
    cmd += src_paths + [str(dst)]
    _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    return _path_total_bytes(dst)


def _layer_manifest_path(layer_dir: Path) -> Path:
    return layer_dir / ".mcd-cluster-file-layer.json"


def _snapshot_manifest_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / ".mcd-backup.json"


def _cluster_current_full_dir(cfg: AgentConfig) -> Path | None:
    link = _cluster_db_root(cfg) / "current-full"
    candidates: list[Path] = []
    try:
        if link.exists():
            resolved = link.resolve()
            if resolved.exists() and resolved.is_dir():
                candidates.append(resolved)
    except Exception:
        pass
    root = _cluster_db_root(cfg)
    if root.exists():
        candidates += [x for x in root.iterdir() if x.is_dir() and x.name.startswith("full-")]
    good: list[Path] = []
    for path in candidates:
        marker = _read_backup_marker(path)
        if str(marker.get("status") or "").lower() == "ok" and (path / "physical-xtrabackup").exists():
            good.append(path)
    good.sort(key=lambda p: p.name)
    return good[-1] if good else None


def _cluster_latest_incremental_dir(cfg: AgentConfig, chain_id: str) -> Path | None:
    root = _cluster_db_root(cfg) / "incrementals"
    if not root.exists():
        return None
    candidates: list[Path] = []
    for path in root.iterdir():
        if not path.is_dir() or not path.name.startswith("incr-"):
            continue
        marker = _read_backup_marker(path)
        if str(marker.get("status") or "").lower() != "ok":
            continue
        if str(marker.get("chain_id") or "") != chain_id:
            continue
        if (path / "physical-xtrabackup").exists():
            candidates.append(path)
    candidates.sort(key=lambda p: p.name)
    return candidates[-1] if candidates else None


def _cluster_update_state(cfg: AgentConfig, updates: dict[str, Any], *, history_item: dict[str, Any] | None = None) -> Path:
    state_path = _cluster_state_path(cfg)
    state = _json_read(state_path)
    history = _compact_backup_history(state.get("history", []))
    if history_item is not None:
        history_item = dict(history_item)
        if "error" in history_item:
            history_item["error"] = _short_error(history_item.get("error"))
        history = [history_item] + history[:19]
        updates = dict(updates)
        updates["history"] = history
    state.update(updates)
    if "last_error" in state:
        state["last_error"] = _short_error(state.get("last_error"))
    if "history" in state:
        state["history"] = _compact_backup_history(state.get("history", []))
    _json_write(state_path, state)
    return state_path


def _cluster_prune_local_after_full(cfg: AgentConfig, keep_full: Path) -> list[str]:
    removed: list[str] = []
    db_root = _cluster_db_root(cfg)
    incr_root = db_root / "incrementals"
    keep_marker = _read_backup_marker(keep_full)
    keep_chain = str(keep_marker.get("chain_id") or keep_full.name)
    for path in db_root.iterdir() if db_root.exists() else []:
        if path == keep_full or not path.is_dir() or not path.name.startswith("full-"):
            continue
        try:
            shutil.rmtree(path)
            removed.append(str(path))
        except Exception:
            pass
    if incr_root.exists():
        for path in incr_root.iterdir():
            if not path.is_dir() or not path.name.startswith("incr-"):
                continue
            marker = _read_backup_marker(path)
            if str(marker.get("chain_id") or "") == keep_chain:
                continue
            try:
                shutil.rmtree(path)
                removed.append(str(path))
            except Exception:
                pass
    return removed


def _cluster_prune_local_before_full(cfg: AgentConfig) -> list[str]:
    """Free operational backup space before writing the next cluster full.

    Cluster local backups are a fast operational layer on the replica. The
    authoritative source still exists in the replica plus production nodes and
    the offsite tier, so this intentionally trades old-local-full retention for
    enough free space to create the next local full.
    """
    removed: list[str] = []
    db_root = _cluster_db_root(cfg)
    if not db_root.exists():
        return removed
    allowed_prefixes = (".incomplete-", "full-", "incremental-")
    allowed_names = {"current-full", "current-incremental", "incrementals"}
    for path in list(db_root.iterdir()):
        if path.name not in allowed_names and not path.name.startswith(allowed_prefixes):
            continue
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                continue
            removed.append(str(path))
        except FileNotFoundError:
            continue
        except Exception:
            pass
    return removed


def cluster_backup_local_full(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=False)
        _ensure_cluster_backup_authority(cfg)
        tool_state = _ensure_cluster_tools(cfg, {"xtrabackup"})
        db_instances = _cluster_db_instances(cfg)
        db = _backup_db_from_config_or_instances(cfg, db_instances)
        _ensure_cluster_local_root_outside_mysql_datadir(cfg, db)
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "job": "backup.cluster.local_full",
                "method": "xtrabackup",
            },
            history_item={"ts": started_ts, "status": "failed", "job": "backup.cluster.local_full", "error": str(e)},
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    lock_path = _cluster_lock_path(cfg, "local-full")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="cluster local full backup is already running", state_path=str(state_path))

    ts = _fmt_local_ts()
    db_root = _cluster_db_root(cfg)
    tmp_dir = db_root / f".incomplete-full-{ts}"
    final_dir = db_root / f"full-{ts}"
    db_dir = tmp_dir / "physical-xtrabackup"
    try:
        db_root.mkdir(parents=True, exist_ok=True)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "last_backup_path": str(final_dir),
                "job": "backup.cluster.local_full",
                "method": "xtrabackup",
                "tool_state": tool_state,
            },
        )
        prepruned = _cluster_prune_local_before_full(cfg)
        if prepruned:
            _cluster_update_state(cfg, {"last_local_prepruned": prepruned})
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        _run_xtrabackup(replace(cfg, backup_method="xtrabackup"), db, db_dir)
        ok, msg, bytes_written = _verify_xtrabackup_dir(db_dir)
        if not ok:
            raise RuntimeError(f"xtrabackup verification failed: {msg}")
        chain_id = final_dir.name
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "host_name": _host_name(cfg),
            "method": "xtrabackup",
            "backup_kind": "full",
            "chain_id": chain_id,
            "path": str(final_dir),
            "db_dir": str(final_dir / "physical-xtrabackup"),
            "bytes_written": bytes_written,
            "server_snapshot": _mysql_server_snapshot(cfg, db),
        }
        _write_marker(tmp_dir, marker)
        os.replace(tmp_dir, final_dir)
        _replace_symlink(db_root / "current-full", final_dir)
        removed = _cluster_prune_local_after_full(cfg, final_dir)
        if removed:
            marker["local_pruned"] = removed
        if prepruned:
            marker["local_prepruned"] = prepruned
        if removed or prepruned:
            _write_marker(final_dir, marker)
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "last_backup_kind": "cluster_local_full",
                "last_chain_id": chain_id,
                "last_local_full_path": str(final_dir),
                "last_local_full_at": marker["ts_utc"],
                "last_local_pruned": removed,
                "last_local_prepruned": prepruned,
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.local_full",
                "duration_sec": duration,
                "backup_path": str(final_dir),
                "bytes_written": bytes_written,
                "chain_id": chain_id,
                "local_pruned": removed,
                "local_prepruned": prepruned,
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster local full completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.local_full",
                "duration_sec": duration,
                "error": str(e),
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def cluster_backup_local_incremental(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=False)
        _ensure_cluster_backup_authority(cfg)
        _ensure_cluster_tools(cfg, {"xtrabackup"})
        db_instances = _cluster_db_instances(cfg)
        db = _backup_db_from_config_or_instances(cfg, db_instances)
        full_dir = _cluster_current_full_dir(cfg)
        if full_dir is None:
            raise RuntimeError("no completed local full backup found for incremental base")
        full_marker = _read_backup_marker(full_dir)
        chain_id = str(full_marker.get("chain_id") or full_dir.name)
        latest_incr = _cluster_latest_incremental_dir(cfg, chain_id)
        base_dir = (latest_incr or full_dir) / "physical-xtrabackup"
        if not base_dir.exists():
            raise RuntimeError(f"incremental base xtrabackup dir missing: {base_dir}")
        min_free = int(getattr(cfg, "backup_cluster_incremental_min_free_bytes", 0) or 0)
        if min_free > 0:
            usage = _storage_usage(_cluster_local_root(cfg))
            free = int(usage.get("free_bytes") or 0) if isinstance(usage, dict) else 0
            if free < min_free:
                raise RuntimeError(
                    "cluster incremental skipped: insufficient free space "
                    f"({free} bytes free, need at least {min_free})"
                )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "job": "backup.cluster.incremental",
                "method": "xtrabackup",
            },
            history_item={"ts": started_ts, "status": "failed", "job": "backup.cluster.incremental", "error": str(e)},
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    lock_path = _cluster_lock_path(cfg, "local-incremental")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="cluster local incremental backup is already running", state_path=str(state_path))

    ts = _fmt_local_ts()
    incr_root = _cluster_db_root(cfg) / "incrementals"
    tmp_dir = incr_root / f".incomplete-incr-{ts}"
    final_dir = incr_root / f"incr-{ts}"
    db_dir = tmp_dir / "physical-xtrabackup"
    try:
        incr_root.mkdir(parents=True, exist_ok=True)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "last_backup_path": str(final_dir),
                "job": "backup.cluster.incremental",
                "method": "xtrabackup",
                "last_chain_id": chain_id,
            },
        )
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        _run_xtrabackup(replace(cfg, backup_method="xtrabackup"), db, db_dir, incremental_base_dir=base_dir)
        ok, msg, bytes_written = _verify_xtrabackup_dir(db_dir)
        if not ok:
            raise RuntimeError(f"xtrabackup incremental verification failed: {msg}")
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "host_name": _host_name(cfg),
            "method": "xtrabackup",
            "backup_kind": "incremental",
            "chain_id": chain_id,
            "full_backup_path": str(full_dir),
            "base_backup_path": str(latest_incr or full_dir),
            "path": str(final_dir),
            "db_dir": str(final_dir / "physical-xtrabackup"),
            "bytes_written": bytes_written,
        }
        _write_marker(tmp_dir, marker)
        os.replace(tmp_dir, final_dir)
        _replace_symlink(incr_root / "current-incremental", final_dir)
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "last_backup_kind": "cluster_local_incremental",
                "last_chain_id": chain_id,
                "last_local_incremental_path": str(final_dir),
                "last_local_incremental_at": marker["ts_utc"],
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.incremental",
                "duration_sec": duration,
                "backup_path": str(final_dir),
                "bytes_written": bytes_written,
                "chain_id": chain_id,
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster local incremental completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {"last_status": "failed", "last_error": str(e), "last_duration_sec": duration},
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.incremental",
                "duration_sec": duration,
                "error": str(e),
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def _cluster_snapshot_paths(cfg: AgentConfig) -> list[str]:
    raw = list(getattr(cfg, "backup_cluster_files_snapshot_paths", []) or [])
    if not raw:
        raw = list(getattr(cfg, "backup_archive_paths", []) or [])
    out: list[str] = []
    for item in raw:
        p = str(item or "").strip()
        if p and Path(p).exists():
            out.append(p)
    return out


def cluster_backup_files_produce(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=False)
        transport = str(getattr(cfg, "backup_cluster_files_transport", "syncthing") or "syncthing").strip().lower()
        if transport != "syncthing":
            raise RuntimeError(f"unsupported cluster files transport: {transport}")
        node_slug = _cluster_node_slug(cfg)
        node_paths = _cluster_file_source_paths(
            cfg,
            "backup_cluster_files_node_paths",
            fallback=_cluster_snapshot_paths(cfg),
        )
        shared_paths = (
            _cluster_file_source_paths(cfg, "backup_cluster_files_shared_paths")
            if _cluster_should_produce_shared(cfg)
            else []
        )
        if not node_paths and not shared_paths:
            raise RuntimeError("no existing node/shared paths configured for cluster file layer producer")
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "job": "backup.cluster.files_produce",
                "method": "syncthing-layer",
            },
            history_item={"ts": started_ts, "status": "failed", "job": "backup.cluster.files_produce", "error": str(e)},
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    lock_path = _cluster_lock_path(cfg, f"files-produce-{node_slug}")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message=f"cluster file layer producer already running for {node_slug}", state_path=str(state_path))

    layer_root = _cluster_file_layers_root(cfg) / node_slug
    tmp_dir = layer_root / f".incomplete-{_fmt_local_ts()}"
    current_dir = layer_root / "current"
    stale_pruned: list[str] = []
    stale_prune_failed: list[str] = []
    try:
        layer_root.mkdir(parents=True, exist_ok=True)
        # Syncthing preserves failed producer temp directories on the replica.
        # Keep fresh dirs to avoid racing an active run; prune older remnants
        # before creating the next layer and again after a successful publish.
        stale_pruned, stale_prune_failed = _cleanup_incomplete_dirs(
            layer_root,
            min_age_sec=max(3600, int(getattr(cfg, "backup_cluster_files_layer_max_age_sec", 86400) or 86400) // 4),
            keep={tmp_dir},
        )
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=False)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "last_backup_path": str(current_dir),
                "last_backup_kind": "cluster_files_layer",
                "job": "backup.cluster.files_produce",
                "method": "syncthing-layer",
            },
        )
        node_bytes = (
            _rsync_tree(
                node_paths,
                tmp_dir / "node",
                cfg,
                link_dest=(current_dir / "node") if (current_dir / "node").exists() else None,
            )
            if node_paths
            else 0
        )
        shared_bytes = (
            _rsync_tree(
                shared_paths,
                tmp_dir / "shared",
                cfg,
                link_dest=(current_dir / "shared") if (current_dir / "shared").exists() else None,
            )
            if shared_paths
            else 0
        )
        manifest = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "cluster_id": str(getattr(cfg, "cluster_id", "") or ""),
            "node_slug": node_slug,
            "node_role": str(getattr(cfg, "cluster_node_role", "") or ""),
            "node_index": getattr(cfg, "cluster_node_index", None),
            "host_name": _host_name(cfg),
            "transport": "syncthing",
            "node_paths": node_paths,
            "shared_paths": shared_paths,
            "bytes_written": int(node_bytes + shared_bytes),
            "node_bytes": int(node_bytes),
            "shared_bytes": int(shared_bytes),
            "stale_incomplete_pruned": stale_pruned,
            "stale_incomplete_prune_failed": stale_prune_failed,
        }
        _json_write(_layer_manifest_path(tmp_dir), manifest)
        _replace_dir_atomic(tmp_dir, current_dir)
        post_pruned, post_prune_failed = _cleanup_incomplete_dirs(
            layer_root,
            min_age_sec=max(3600, int(getattr(cfg, "backup_cluster_files_layer_max_age_sec", 86400) or 86400) // 4),
        )
        stale_pruned += post_pruned
        stale_prune_failed += post_prune_failed
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(current_dir),
                "last_backup_kind": "cluster_files_layer",
                "last_files_layer_path": str(current_dir),
                "last_files_layer_at": manifest["ts_utc"],
                "last_bytes_written": int(node_bytes + shared_bytes),
                "last_files_layer_stale_pruned": stale_pruned,
                "last_files_layer_stale_prune_failed": stale_prune_failed,
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.files_produce",
                "duration_sec": duration,
                "backup_path": str(current_dir),
                "bytes_written": int(node_bytes + shared_bytes),
                "node_slug": node_slug,
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster file layer produced: {current_dir}",
            state_path=str(state_path),
            backup_path=str(current_dir),
            duration_sec=duration,
            bytes_written=int(node_bytes + shared_bytes),
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {"last_status": "failed", "last_error": str(e), "last_duration_sec": duration},
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.files_produce",
                "duration_sec": duration,
                "error": str(e),
                "node_slug": node_slug,
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def _read_cluster_file_layers(cfg: AgentConfig) -> tuple[list[dict[str, Any]], list[str]]:
    layers_root = _cluster_file_layers_root(cfg)
    expected = _cluster_expected_file_nodes(cfg)
    problems: list[str] = []
    layers: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    if not layers_root.exists():
        return [], [f"layers root missing: {layers_root}"]
    candidates = []
    if expected:
        candidates = [layers_root / node / "current" for node in expected]
    else:
        candidates = [p / "current" for p in layers_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    seen: set[str] = set()
    for layer_dir in candidates:
        manifest_path = _layer_manifest_path(layer_dir)
        if not manifest_path.exists():
            problems.append(f"missing layer manifest: {manifest_path}")
            continue
        manifest = _json_read(manifest_path)
        node_slug = _safe_slug(str(manifest.get("node_slug") or layer_dir.parent.name))
        if expected and node_slug not in expected:
            problems.append(f"unexpected node layer {node_slug}: {manifest_path}")
            continue
        if node_slug in seen:
            problems.append(f"duplicate node layer: {node_slug}")
            continue
        if str(manifest.get("status") or "").lower() != "ok":
            problems.append(f"node layer not ok: {node_slug}")
            continue
        ts_raw = str(manifest.get("ts_utc") or "").strip()
        try:
            layer_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            age_sec = int((now - layer_dt).total_seconds())
        except Exception:
            age_sec = 10**9
        if age_sec > int(getattr(cfg, "backup_cluster_files_layer_max_age_sec", 86400) or 86400):
            problems.append(f"node layer stale: {node_slug} age_sec={age_sec}")
            continue
        node_dir = layer_dir / "node"
        shared_dir = layer_dir / "shared"
        if not node_dir.exists() and not shared_dir.exists():
            problems.append(f"node layer has no node/shared directories: {node_slug}")
            continue
        seen.add(node_slug)
        layers.append({"node_slug": node_slug, "path": layer_dir, "manifest": manifest})
    if expected:
        missing = [node for node in expected if node not in seen]
        for node in missing:
            problems.append(f"missing expected node layer: {node}")
    return layers, problems


def cluster_backup_files_assemble(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=False)
        _ensure_cluster_backup_authority(cfg)
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "error",
                "last_error": str(e),
                "last_duration_sec": duration,
                "last_backup_kind": "cluster_files_snapshot",
                "last_started_at": started_ts,
            },
            history_item={"ts": _utc_now_iso(), "status": "error", "job": "backup.cluster.files_assemble", "error": str(e)},
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    lock_path = _cluster_lock_path(cfg, "files")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="cluster files snapshot is already running", state_path=str(state_path))

    ts = _fmt_local_ts()
    snap_root = _cluster_files_root(cfg)
    tmp_dir = snap_root / f".incomplete-{ts}"
    final_dir = snap_root / ts
    try:
        if not bool(getattr(cfg, "backup_cluster_files_snapshot_enabled", True)):
            raise RuntimeError("cluster files snapshot is disabled")
        layers, problems = _read_cluster_file_layers(cfg)
        if problems:
            raise RuntimeError("cluster file layers are not ready: " + "; ".join(problems[:10]))
        if not layers:
            raise RuntimeError("no cluster file layers found")
        snap_root.mkdir(parents=True, exist_ok=True)
        latest = snap_root / "latest"
        latest_resolved: Path | None = None
        try:
            if latest.exists():
                resolved = latest.resolve()
                if resolved.exists() and resolved.is_dir():
                    latest_resolved = resolved
        except Exception:
            latest_resolved = None
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=False)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "last_backup_path": str(final_dir),
                "job": "backup.cluster.files_assemble",
                "method": "syncthing-layer-assemble",
            },
        )
        total_bytes = 0
        layer_rows: list[dict[str, Any]] = []
        shared_done = False
        for layer in sorted(layers, key=lambda x: str(x.get("node_slug") or "")):
            node_slug = str(layer["node_slug"])
            layer_path = Path(layer["path"])
            node_src = layer_path / "node"
            shared_src = layer_path / "shared"
            node_dst = tmp_dir / "nodes" / node_slug
            shared_dst = tmp_dir / "shared"
            node_bytes = 0
            shared_bytes = 0
            if node_src.exists():
                node_bytes = _rsync_tree(
                    [str(node_src) + "/"],
                    node_dst,
                    cfg,
                    link_dest=(latest_resolved / "nodes" / node_slug) if latest_resolved is not None else None,
                    relative=False,
                )
            if shared_src.exists() and not shared_done:
                shared_bytes = _rsync_tree(
                    [str(shared_src) + "/"],
                    shared_dst,
                    cfg,
                    link_dest=(latest_resolved / "shared") if latest_resolved is not None else None,
                    relative=False,
                )
                shared_done = True
            total_bytes += int(node_bytes + shared_bytes)
            layer_rows.append(
                {
                    "node_slug": node_slug,
                    "layer_path": str(layer_path),
                    "layer_ts_utc": str((layer.get("manifest") or {}).get("ts_utc") or ""),
                    "node_bytes": int(node_bytes),
                    "shared_bytes": int(shared_bytes),
                }
            )
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "cluster_id": str(getattr(cfg, "cluster_id", "") or ""),
            "host_name": _host_name(cfg),
            "method": "syncthing-layer-assemble",
            "path": str(final_dir),
            "bytes_written": int(total_bytes),
            "layers": layer_rows,
            "expected_nodes": _cluster_expected_file_nodes(cfg),
        }
        _json_write(_snapshot_manifest_path(tmp_dir), marker)
        os.replace(tmp_dir, final_dir)
        _replace_symlink(snap_root / "latest", final_dir)
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": int(total_bytes),
                "last_backup_kind": "cluster_files_snapshot",
                "last_files_snapshot_path": str(final_dir),
                "last_files_snapshot_at": marker["ts_utc"],
                "last_files_snapshot_layers": layer_rows,
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.files_assemble",
                "duration_sec": duration,
                "backup_path": str(final_dir),
                "bytes_written": int(total_bytes),
                "layers": layer_rows,
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster files snapshot assembled: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=int(total_bytes),
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {"last_status": "failed", "last_error": str(e), "last_duration_sec": duration},
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.files_assemble",
                "duration_sec": duration,
                "error": str(e),
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def cluster_backup_files_snapshot(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    transport = str(getattr(cfg, "backup_cluster_files_transport", "syncthing") or "syncthing").strip().lower()
    if transport == "syncthing":
        return cluster_backup_files_assemble(cfg)
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=False)
        _ensure_cluster_backup_authority(cfg)
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "error",
                "last_error": str(e),
                "last_duration_sec": duration,
                "last_backup_kind": "cluster_files_snapshot",
                "last_started_at": started_ts,
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "error",
                "job": "backup.cluster.files",
                "error": str(e),
                "duration_sec": duration,
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    lock_path = _cluster_lock_path(cfg, "files")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="cluster files snapshot is already running", state_path=str(state_path))
    ts = _fmt_local_ts()
    snap_root = _cluster_files_root(cfg)
    tmp_dir = snap_root / f".incomplete-{ts}"
    final_dir = snap_root / ts
    try:
        _validate_cluster_cfg(cfg, remote=False)
        if not bool(getattr(cfg, "backup_cluster_files_snapshot_enabled", True)):
            raise RuntimeError("cluster files snapshot is disabled")
        paths = _cluster_snapshot_paths(cfg)
        if not paths:
            raise RuntimeError("no existing paths configured for cluster files snapshot")
        snap_root.mkdir(parents=True, exist_ok=True)
        latest = snap_root / "latest"
        link_dest = ""
        try:
            if latest.exists():
                resolved = latest.resolve()
                if resolved.exists() and resolved.is_dir():
                    link_dest = str(resolved)
        except Exception:
            link_dest = ""
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=False)
        cmd = ["rsync", "-a", "--delete", "--numeric-ids", "--relative"]
        if link_dest:
            cmd.append(f"--link-dest={link_dest}")
        for pattern in list(getattr(cfg, "backup_cluster_files_snapshot_exclude", []) or []):
            pattern = str(pattern or "").strip()
            if pattern:
                cmd.append(f"--exclude={pattern}")
        cmd += paths + [str(tmp_dir)]
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "last_backup_path": str(final_dir),
                "job": "backup.cluster.files_snapshot",
                "method": "rsync-hardlink",
            },
        )
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
        bytes_written = _path_total_bytes(tmp_dir)
        manifest = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "host_name": _host_name(cfg),
            "method": "rsync-hardlink",
            "paths": paths,
            "excludes": list(getattr(cfg, "backup_cluster_files_snapshot_exclude", []) or []),
            "link_dest": link_dest,
            "bytes_written": bytes_written,
        }
        _write_marker(tmp_dir, manifest)
        os.replace(tmp_dir, final_dir)
        _replace_symlink(snap_root / "latest", final_dir)
        duration = int(time.monotonic() - start_monotonic)
        _cluster_update_state(
            cfg,
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "last_backup_kind": "cluster_files_snapshot",
                "last_files_snapshot_path": str(final_dir),
                "last_files_snapshot_at": manifest["ts_utc"],
            },
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.files_snapshot",
                "duration_sec": duration,
                "backup_path": str(final_dir),
                "bytes_written": bytes_written,
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster files snapshot completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {"last_status": "failed", "last_error": str(e), "last_duration_sec": duration},
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.files_snapshot",
                "duration_sec": duration,
                "error": str(e),
            },
        )
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def _cluster_latest_files_snapshot(cfg: AgentConfig) -> Path | None:
    latest = _cluster_files_root(cfg) / "latest"
    try:
        if latest.exists():
            resolved = latest.resolve()
            if resolved.exists() and resolved.is_dir():
                return resolved
    except Exception:
        pass
    root = _cluster_files_root(cfg)
    if not root.exists():
        return None
    candidates = [x for x in root.iterdir() if x.is_dir() and not x.name.startswith(".")]
    candidates.sort(key=lambda p: p.name)
    return candidates[-1] if candidates else None


def _cluster_remote_parent(cfg: AgentConfig, mount_path: Path) -> Path:
    return mount_path / _format_remote_dir(cfg.backup_remote_root_dir, _cluster_name(cfg))


def _cluster_offsite_state_from_marker(marker: dict[str, Any], backup_path: Path | str) -> dict[str, Any]:
    """Build stable offsite-specific state fields.

    Generic last_backup_* fields are updated by every cluster backup job
    (local full, incremental, files assemble, offsite). Offsite file archive
    health must survive later local jobs, so it is stored under last_offsite_*.
    """
    archive_path = str(marker.get("files_archive_path") or "").strip()
    out: dict[str, Any] = {
        "last_offsite_backup_path": str(backup_path),
        "last_offsite_backup_at": str(marker.get("ts_utc") or ""),
        "last_offsite_files_archive_path": archive_path,
        "last_offsite_files_bytes": int(marker.get("files_bytes") or 0),
        "last_offsite_bytes_written": int(marker.get("bytes_written") or 0),
        "last_offsite_db_bytes": int(marker.get("db_bytes") or 0),
        "last_offsite_method": str(marker.get("method") or ""),
    }
    if archive_path:
        out["last_offsite_files_archive_ok"] = Path(archive_path).exists()
    else:
        out["last_offsite_files_archive_ok"] = False
    return out


def _cluster_backup_integrity_problem(state: dict[str, Any]) -> str:
    archive_path = str(state.get("last_offsite_files_archive_path") or "").strip()
    archive_ok = state.get("last_offsite_files_archive_ok")
    if archive_path and archive_ok is False:
        return f"last offsite files archive missing: {archive_path}"
    return ""


def _apply_cluster_backup_integrity_status(state: dict[str, Any]) -> None:
    problem = _cluster_backup_integrity_problem(state)
    if not problem:
        state.setdefault("cluster_integrity_status", "ok")
        state.setdefault("cluster_integrity_error", "")
        return
    state["cluster_integrity_status"] = "failed"
    state["cluster_integrity_error"] = problem
    last_status = str(state.get("last_status") or "").strip().lower()
    if last_status in {"", "ok", "ok_skip_existing"}:
        state["last_status"] = "failed"
        state["last_error"] = problem


def _cluster_is_protected_archive(path: Path) -> tuple[bool, str]:
    lname = path.name.lower()
    manual_tokens = ("manual", "dont-touch", "do-not-touch", "do_not_touch", "do-not-prune", "do_not_prune")
    if any(tok in lname for tok in manual_tokens):
        return True, "protected_by_name"
    marker = _read_backup_marker(path)
    for key in ("manual", "manual_archive", "do_not_prune", "dont_touch", "protected"):
        if bool(marker.get(key)):
            return True, f"protected_by_marker:{key}"
    kind = str(marker.get("kind") or marker.get("backup_kind") or marker.get("type") or "").strip().lower()
    if kind in {"manual", "manual_archive"}:
        return True, "protected_by_marker_kind"
    return False, ""


def _cluster_remote_retention_plan_for_parent(
    parent: Path,
    *,
    keep_daily: int,
    keep_weekly: int,
    apply: bool = False,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "root": str(parent),
        "apply": bool(apply),
        "keep_daily": int(keep_daily),
        "keep_weekly": int(keep_weekly),
        "kept": [],
        "delete_candidates": [],
        "protected": [],
        "problems": [],
        "removed": [],
    }
    if not parent.exists():
        return plan

    candidates: list[dict[str, Any]] = []

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(parent))
        except Exception:
            return str(p)

    def scan(container: Path, scope: str) -> None:
        if not container.exists():
            return
        for child in sorted(container.iterdir(), key=lambda p: p.name):
            if child.is_symlink() or child.is_file():
                continue
            if not child.is_dir():
                continue
            if child.name.startswith(".incomplete-"):
                continue
            protected, reason = _cluster_is_protected_archive(child)
            if protected:
                plan["protected"].append({"path": rel(child), "reason": reason})
                continue
            dt = _dir_date(child)
            if dt is None:
                plan["problems"].append(
                    {
                        "path": rel(child),
                        "reason": "directory is not date-named and is not marked protected/manual",
                    }
                )
                continue
            candidates.append({"path": child, "rel": rel(child), "date": dt, "scope": scope})

    known = {"daily", "weekly"}
    for child in sorted(parent.iterdir(), key=lambda p: p.name):
        if child.is_dir() and child.name in known:
            scan(child, child.name)
        elif child.is_dir() and child.name.startswith(".incomplete-"):
            continue
        elif child.is_dir():
            protected, reason = _cluster_is_protected_archive(child)
            if protected:
                plan["protected"].append({"path": rel(child), "reason": reason})
                continue
            dt = _dir_date(child)
            if dt is not None:
                candidates.append({"path": child, "rel": rel(child), "date": dt, "scope": "legacy"})
            else:
                plan["problems"].append(
                    {
                        "path": rel(child),
                        "reason": "top-level directory is not daily/weekly/date and is not marked protected/manual",
                    }
                )

    candidates.sort(key=lambda x: x["date"], reverse=True)
    keep_paths: set[str] = set()
    for item in [x for x in candidates if x["scope"] in {"daily", "legacy"}][: max(0, int(keep_daily))]:
        keep_paths.add(str(item["path"]))
    weekly_candidates = [x for x in candidates if x["scope"] == "weekly" or x["date"].weekday() == 6]
    for item in weekly_candidates[: max(0, int(keep_weekly))]:
        keep_paths.add(str(item["path"]))

    for item in candidates:
        row = {"path": item["rel"], "date": item["date"].strftime("%Y-%m-%d"), "scope": item["scope"]}
        if str(item["path"]) in keep_paths:
            plan["kept"].append(row)
        else:
            plan["delete_candidates"].append(row)

    if apply and not plan["problems"]:
        for item in plan["delete_candidates"]:
            path = parent / str(item["path"])
            try:
                shutil.rmtree(path)
                plan["removed"].append(dict(item))
            except Exception as e:
                plan["problems"].append({"path": str(item["path"]), "reason": f"delete failed: {e}"})
        if plan["removed"]:
            plan["delete_candidates"] = [
                x
                for x in plan["delete_candidates"]
                if str(x.get("path") or "") not in {str(y.get("path") or "") for y in plan["removed"]}
            ]
    return plan


def cluster_backup_retention_plan(config: AgentConfig, *, apply: bool = False) -> dict[str, Any]:
    cfg = _effective_cfg(config)
    _validate_cluster_cfg(cfg, remote=True)
    _ensure_cluster_backup_authority(cfg)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    _mount(cfg, mount_path)
    try:
        parent = _cluster_remote_parent(cfg, mount_path)
        parent.mkdir(parents=True, exist_ok=True)
        return _cluster_remote_retention_plan_for_parent(
            parent,
            keep_daily=max(1, int(getattr(cfg, "backup_cluster_remote_retention_daily", 7) or 7)),
            keep_weekly=max(0, int(getattr(cfg, "backup_cluster_remote_retention_weekly", 4) or 4)),
            apply=apply,
        )
    finally:
        _unmount(mount_path, cfg.backup_unmount_timeout_sec)


def cluster_backup_offsite(config: AgentConfig) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    started_ts = _utc_now_iso()
    start_monotonic = time.monotonic()
    try:
        _validate_cluster_cfg(cfg, remote=True)
        _ensure_cluster_backup_authority(cfg)
        stopped_prepared_mysql = _cleanup_stale_prepared_mysql_processes(cfg)
        active_pids = _cluster_offsite_processes(cfg)
        if active_pids:
            _cluster_update_state(
                cfg,
                {
                    "host_name": _host_name(cfg),
                    "cluster_name": _cluster_name(cfg),
                    "last_run_at": started_ts,
                    "last_status": "running",
                    "last_error": "",
                    "job": "backup.cluster.offsite",
                    "method": "mydumper",
                    "active_offsite_pids": active_pids,
                    "stale_prepared_mysql_stopped": stopped_prepared_mysql,
                },
            )
            return BackupResult(
                ok=False,
                message=f"cluster offsite backup is already running (pids: {', '.join(map(str, active_pids))})",
                state_path=str(state_path),
                duration_sec=0,
            )
        tool_state = _ensure_cluster_tools(cfg, {"sshfs", "mydumper"})
        db_instances = _cluster_db_instances(cfg)
        db = _backup_db_from_config_or_instances(cfg, db_instances)
        full_dir = _cluster_current_full_dir(cfg)
        if full_dir is None:
            raise RuntimeError("offsite backup requires a completed local full backup")
        full_marker = _read_backup_marker(full_dir)
        files_snapshot = (
            _cluster_latest_files_snapshot(cfg)
            if bool(getattr(cfg, "backup_cluster_files_snapshot_enabled", True))
            else None
        )
        if bool(getattr(cfg, "backup_cluster_files_snapshot_enabled", True)) and files_snapshot is None:
            snapshot_res = cluster_backup_files_snapshot(cfg)
            if not snapshot_res.ok:
                raise RuntimeError(
                    "offsite backup requires a completed local files snapshot; "
                    f"snapshot attempt failed: {snapshot_res.message}"
                )
            files_snapshot = _cluster_latest_files_snapshot(cfg)
            if files_snapshot is None:
                raise RuntimeError("offsite backup requires a completed local files snapshot")
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        err = _short_error(e)
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": err,
                "last_duration_sec": duration,
                "job": "backup.cluster.offsite",
                "method": "mydumper",
            },
            history_item={"ts": started_ts, "status": "failed", "job": "backup.cluster.offsite", "error": err},
        )
        return BackupResult(ok=False, message=err, state_path=str(state_path), duration_sec=duration)

    lock_path = _cluster_lock_path(cfg, "offsite")
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="cluster offsite backup is already running", state_path=str(state_path))

    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    date_dir = _fmt_local_date()
    tmp_dir: Path | None = None
    final_dir: Path | None = None
    try:
        _cluster_update_state(
            cfg,
            {
                "host_name": _host_name(cfg),
                "cluster_name": _cluster_name(cfg),
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "job": "backup.cluster.offsite",
                "method": "mydumper",
                "tool_state": tool_state,
                "stale_prepared_mysql_stopped": stopped_prepared_mysql,
            },
        )
        _mount(cfg, mount_path)
        remote_parent = _cluster_remote_parent(cfg, mount_path)
        daily_parent = remote_parent / "daily"
        daily_parent.mkdir(parents=True, exist_ok=True)
        removed_incomplete, failed_incomplete = _cleanup_incomplete_dirs(daily_parent)
        if failed_incomplete:
            raise RuntimeError("failed to cleanup stale incomplete offsite dirs: " + ", ".join(sorted(failed_incomplete)))
        final_dir = daily_parent / date_dir
        if final_dir.exists():
            marker = _read_backup_marker(final_dir)
            if str(marker.get("status") or "").lower() == "ok":
                duration = int(time.monotonic() - start_monotonic)
                existing_files_archive = str(marker.get("files_archive_path") or "").strip()
                existing_offsite_state = _cluster_offsite_state_from_marker(marker, final_dir)
                _cluster_update_state(
                    cfg,
                    {
                        "last_status": "ok",
                        "last_error": "",
                        "last_success_at": _utc_now_iso(),
                        "last_duration_sec": duration,
                        "last_backup_path": str(final_dir),
                        "last_backup_kind": "cluster_offsite",
                        "last_files_archive_path": existing_files_archive,
                        "last_files_bytes": int(existing_offsite_state.get("last_offsite_files_bytes") or 0),
                        **existing_offsite_state,
                    },
                    history_item={
                        "ts": _utc_now_iso(),
                        "status": "ok_skip_existing",
                        "job": "backup.cluster.offsite",
                        "duration_sec": duration,
                        "backup_path": str(final_dir),
                        "files_archive_path": existing_files_archive,
                    },
                )
                return BackupResult(
                    ok=True,
                    message=f"cluster offsite backup already exists: {final_dir}",
                    state_path=str(state_path),
                    backup_path=str(final_dir),
                    duration_sec=duration,
                )
            raise RuntimeError(f"offsite backup target already exists: {final_dir}")
        tmp_dir = daily_parent / f".incomplete-{date_dir}-{_fmt_local_ts()}"
        tmp_dir.mkdir(parents=True, exist_ok=False)
        files_bytes = 0
        files_archive_path = ""
        if files_snapshot is not None:
            archive_path, files_bytes = _cluster_archive_files_snapshot_to_remote(cfg, files_snapshot, tmp_dir)
            files_archive_path = str(archive_path)
        db_dir = tmp_dir / "databases" / f"cluster__{db.name or 'all'}"
        db_dir.mkdir(parents=True, exist_ok=False)
        dump_cfg = _cluster_offsite_mydumper_cfg(cfg)
        offsite_source = str(getattr(cfg, "backup_cluster_offsite_source", "xtrabackup") or "xtrabackup").strip().lower()
        if offsite_source in {"live", "live_replica", "replica"}:
            db_source_meta = {"offsite_db_source": "live_replica"}
            _run_mydumper(dump_cfg, db, db_dir)
        else:
            db_source_meta = _run_mydumper_from_xtrabackup_full(dump_cfg, db, full_dir, db_dir)
        ok, verify_msg, db_bytes = _verify_dump_dir(db_dir)
        if not ok:
            raise RuntimeError(f"cluster offsite mydumper verification failed: {verify_msg}")
        bytes_written = db_bytes + files_bytes
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "cluster_backup": True,
            "cluster_name": _cluster_name(cfg),
            "host_name": _host_name(cfg),
            "method": "mydumper",
            "path": str(final_dir),
            "database": db.name or "*",
            **db_source_meta,
            "mydumper_threads": dump_cfg.backup_mydumper_threads,
            "mydumper_extra_args": dump_cfg.backup_mydumper_extra_args,
            "bytes_written": bytes_written,
            "db_bytes": db_bytes,
            "files_bytes": files_bytes,
            "local_full_path": str(full_dir),
            "local_full_chain_id": str(full_marker.get("chain_id") or full_dir.name),
            "files_snapshot_path": str(files_snapshot) if files_snapshot is not None else "",
            "files_archive_path": files_archive_path,
            "server_snapshot": _mysql_server_snapshot(cfg, db),
            "cleanup_incomplete_removed": removed_incomplete,
        }
        _write_marker(tmp_dir, marker)
        os.replace(tmp_dir, final_dir)
        if files_archive_path:
            marker["files_archive_path"] = str(final_dir / Path(files_archive_path).name)
        retention_plan = _cluster_remote_retention_plan_for_parent(
            remote_parent,
            keep_daily=max(1, int(getattr(cfg, "backup_cluster_remote_retention_daily", 7) or 7)),
            keep_weekly=max(0, int(getattr(cfg, "backup_cluster_remote_retention_weekly", 4) or 4)),
            apply=True,
        )
        marker["retention_plan"] = retention_plan
        if retention_plan.get("problems"):
            marker["retention_skipped_reason"] = "problems_detected_no_delete"
        _write_marker(final_dir, marker)
        _write_storage_backup_manifest_and_index(
            mount_path=mount_path,
            backup_dir=final_dir,
            marker=marker,
            kind="mcc.cluster_offsite.mydumper",
        )
        usage = _storage_usage(mount_path)
        duration = int(time.monotonic() - start_monotonic)
        state_updates = {
            "last_status": "ok",
            "last_error": "",
            "last_success_at": _utc_now_iso(),
            "last_duration_sec": duration,
            "last_backup_path": str(final_dir),
            "last_bytes_written": bytes_written,
            "last_backup_kind": "cluster_offsite",
            "last_files_archive_path": str(marker.get("files_archive_path") or ""),
            "last_files_bytes": files_bytes,
            "last_remote_retention_plan": retention_plan,
            **_cluster_offsite_state_from_marker(marker, final_dir),
        }
        if isinstance(usage, dict):
            state_updates.update(
                {
                    "last_storage_total_bytes": int(usage.get("total_bytes") or 0),
                    "last_storage_used_bytes": int(usage.get("used_bytes") or 0),
                    "last_storage_free_bytes": int(usage.get("free_bytes") or 0),
                    "last_storage_used_pct": float(usage.get("used_pct") or 0.0),
                    "last_storage_checked_at": str(usage.get("checked_at") or ""),
                }
            )
        _cluster_update_state(
            cfg,
            state_updates,
            history_item={
                "ts": _utc_now_iso(),
                "status": "ok",
                "job": "backup.cluster.offsite",
                "duration_sec": duration,
                "backup_path": str(final_dir),
                "bytes_written": bytes_written,
                "files_archive_path": str(marker.get("files_archive_path") or ""),
                "retention_removed": retention_plan.get("removed", []),
                "retention_problems": retention_plan.get("problems", []),
            },
        )
        return BackupResult(
            ok=True,
            message=f"cluster offsite backup completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        err = _short_error(e)
        if tmp_dir is not None and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _cluster_update_state(
            cfg,
            {"last_status": "failed", "last_error": err, "last_duration_sec": duration},
            history_item={
                "ts": _utc_now_iso(),
                "status": "failed",
                "job": "backup.cluster.offsite",
                "duration_sec": duration,
                "error": err,
            },
        )
        return BackupResult(ok=False, message=err, state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def cluster_backup_offsite_dry_run(config: AgentConfig) -> dict[str, Any]:
    cfg = _effective_cfg(config)
    started_ts = _utc_now_iso()
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    result: dict[str, Any] = {
        "ok": False,
        "job": "backup.cluster.offsite.dry_run",
        "ts": started_ts,
        "cluster_name": _cluster_name(cfg),
        "host_name": _host_name(cfg),
        "method": "mydumper",
        "offsite_db_source": str(getattr(cfg, "backup_cluster_offsite_source", "xtrabackup") or "xtrabackup").strip().lower(),
        "would_run_mydumper": False,
        "would_prepare_xtrabackup_mysql": False,
        "would_include_files": False,
        "would_write_remote": False,
    }
    try:
        _validate_cluster_cfg(cfg, remote=True)
        _ensure_cluster_backup_authority(cfg)
        prepared_mysql = _cluster_prepared_mysql_processes(cfg)
        result["prepared_mysql_pids"] = [int(item["pid"]) for item in prepared_mysql]
        result["stale_prepared_mysql_pids"] = [
            int(item["pid"])
            for item in prepared_mysql
            if isinstance(item.get("datadir"), Path) and not item["datadir"].exists()
        ]
        active_pids = _cluster_offsite_processes(cfg)
        result["active_offsite_pids"] = active_pids
        if active_pids:
            raise RuntimeError(f"cluster offsite backup is already running (pids: {', '.join(map(str, active_pids))})")
        result["tools"] = _ensure_cluster_tools(cfg, {"sshfs", "mydumper"})
        db_instances = _cluster_db_instances(cfg)
        db = _backup_db_from_config_or_instances(cfg, db_instances)
        dump_cfg = _cluster_offsite_mydumper_cfg(cfg)
        result["database"] = db.name or "*"
        result["mydumper_threads"] = dump_cfg.backup_mydumper_threads
        result["mydumper_extra_args"] = dump_cfg.backup_mydumper_extra_args
        full_dir = _cluster_current_full_dir(cfg)
        if full_dir is None:
            raise RuntimeError("offsite dry-run requires a completed local full backup")
        result["local_full_path"] = str(full_dir)
        if str(result["offsite_db_source"]) not in {"live", "live_replica", "replica"}:
            result["would_prepare_xtrabackup_mysql"] = True
        files_snapshot = None
        if bool(getattr(cfg, "backup_cluster_files_snapshot_enabled", True)):
            files_snapshot = _cluster_latest_files_snapshot(cfg)
            if files_snapshot is None:
                raise RuntimeError("offsite dry-run requires a completed local files snapshot")
            result["files_snapshot_path"] = str(files_snapshot)
            result["files_snapshot_bytes"] = _path_total_bytes(files_snapshot)
            result["would_include_files"] = True
        _mount(cfg, mount_path)
        try:
            remote_parent = _cluster_remote_parent(cfg, mount_path)
            daily_parent = remote_parent / "daily"
            daily_parent.mkdir(parents=True, exist_ok=True)
            probe = daily_parent / f".mcd-offsite-dry-run-{_fmt_local_ts()}.json"
            probe.write_text(json.dumps({"ts": started_ts, "cluster": _cluster_name(cfg)}) + "\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            result["remote_parent"] = str(remote_parent)
            result["remote_daily_parent"] = str(daily_parent)
            result["would_write_remote"] = True
            result["would_run_mydumper"] = True
            result["retention_plan"] = _cluster_remote_retention_plan_for_parent(
                remote_parent,
                keep_daily=max(1, int(getattr(cfg, "backup_cluster_remote_retention_daily", 7) or 7)),
                keep_weekly=max(0, int(getattr(cfg, "backup_cluster_remote_retention_weekly", 4) or 4)),
                apply=False,
            )
        finally:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        result["ok"] = True
        result["message"] = "cluster offsite dry-run ok"
    except Exception as e:
        result["error"] = str(e)
        result["message"] = str(e)
    return result


def cluster_backup_status(config: AgentConfig) -> dict[str, Any]:
    cfg = _effective_cfg(config)
    state_path = _cluster_state_path(cfg)
    state = _json_read(state_path)
    root = _cluster_local_root(cfg)
    current_full = _cluster_current_full_dir(cfg)
    latest_incr = None
    chain_id = ""
    if current_full is not None:
        marker = _read_backup_marker(current_full)
        chain_id = str(marker.get("chain_id") or current_full.name)
        latest_incr = _cluster_latest_incremental_dir(cfg, chain_id)
    latest_files = _cluster_latest_files_snapshot(cfg)
    state.update(
        {
            "cluster_enabled": bool(getattr(cfg, "backup_cluster_enabled", False)),
            "cluster_name": _cluster_name(cfg),
            "cluster_id": str(getattr(cfg, "cluster_id", "") or "").strip(),
            "cluster_node_role": str(getattr(cfg, "cluster_node_role", "") or "").strip(),
            "cluster_node_index": getattr(cfg, "cluster_node_index", None),
            "cluster_backup_authority": cluster_backup_authority_status(cfg),
            "state_path": str(state_path),
            "local_root_dir": str(root),
            "current_full": str(current_full) if current_full is not None else "",
            "current_chain_id": chain_id,
            "latest_incremental": str(latest_incr) if latest_incr is not None else "",
            "latest_files_snapshot": str(latest_files) if latest_files is not None else "",
            "local_full_running": _lock_active(_cluster_lock_path(cfg, "local-full")),
            "local_incremental_running": _lock_active(_cluster_lock_path(cfg, "local-incremental")),
            "files_snapshot_running": _lock_active(_cluster_lock_path(cfg, "files")),
            "offsite_running": _lock_active(_cluster_lock_path(cfg, "offsite")) or bool(_cluster_offsite_processes(cfg)),
        }
    )
    offsite_path_raw = str(state.get("last_offsite_backup_path") or "").strip()
    if not offsite_path_raw and str(state.get("last_backup_kind") or "") == "cluster_offsite":
        offsite_path_raw = str(state.get("last_backup_path") or "").strip()
    if offsite_path_raw:
        offsite_path = Path(offsite_path_raw)
        marker = _read_backup_marker(offsite_path)
        if marker:
            offsite_state = _cluster_offsite_state_from_marker(marker, offsite_path)
            archive_path = str(offsite_state.get("last_offsite_files_archive_path") or "").strip()
            state.update(offsite_state)
            state["last_files_archive_path"] = archive_path
            state["last_files_bytes"] = int(offsite_state.get("last_offsite_files_bytes") or 0)
        else:
            state["last_offsite_files_archive_ok"] = False
    _apply_cluster_backup_integrity_status(state)
    return state


def _instance_slug(inst: MauticInstall) -> str:
    raw = inst.primary_domain or inst.name or inst.instance_uid or Path(inst.root).name
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or "").strip()).strip(".-")
    return slug or "instance"


def _select_instance_for_backup(instances: list[MauticInstall], selector: str | None) -> MauticInstall:
    raw = str(selector or "").strip()
    if not raw:
        raise RuntimeError("instance root selector is required for instance backup")
    for inst in instances:
        if str(inst.root or "").strip() == raw:
            return inst
    low = raw.lower()
    for inst in instances:
        candidates = [
            inst.instance_uid,
            inst.name,
            inst.primary_domain or "",
            *(inst.domains or []),
        ]
        if any(str(x or "").strip().lower() == low for x in candidates):
            return inst
    raise RuntimeError(f"instance not found for backup selector: {raw}")


def _instance_remote_parent(cfg: AgentConfig, mount_path: Path, host_name: str, inst: MauticInstall) -> Path:
    base = str(cfg.backup_remote_root_dir or "backup").strip().strip("/")
    parts = [p for p in [base, host_name, "instances", _instance_slug(inst)] if p]
    current = mount_path
    for part in parts:
        current = current / part
    return current


def backup_instance_run(config: AgentConfig, root: str | None = None) -> BackupResult:
    cfg = _effective_cfg(replace(config, backup_method="mydumper"))
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    start_monotonic = time.monotonic()
    started_ts = _utc_now_iso()
    host_name = _host_name(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(ok=False, message="backup is already running for this host", state_path=str(state_path))

    tmp_dir: Path | None = None
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    run_state = dict(state)
    try:
        if not cfg.backup_ssh_host or not cfg.backup_ssh_user:
            raise RuntimeError("backup storage is not configured ([backup.storage].host/user)")
        if not cfg.backup_ssh_key_file and not cfg.backup_ssh_password:
            raise RuntimeError("backup storage auth is not configured (key_file or password required)")
        tool_state = _ensure_cluster_tools(cfg, {"sshfs", "mydumper"})
        if not shutil.which("tar") or not shutil.which("gzip"):
            raise RuntimeError("tar/gzip are required for instance backup")
        instances = _list_instances(cfg)
        inst = _select_instance_for_backup(instances, root)
        if not inst.db:
            raise RuntimeError(f"instance has no DB credentials: {inst.name}")
        effective_db = _effective_db_for_instance(cfg, inst)
        remote_parent = _instance_remote_parent(cfg, mount_path, host_name, inst)
        backup_name = _fmt_local_ts()
        tmp_dir = remote_parent / f".incomplete-{backup_name}"
        final_dir = remote_parent / backup_name
        run_state.update(
            {
                "host_name": host_name,
                "selected_root": inst.root,
                "selected_instance": inst.name,
                "last_run_at": started_ts,
                "last_status": "running",
                "last_error": "",
                "job": "backup.instance_run",
                "method": "mydumper",
                "tools": tool_state,
            }
        )
        _json_write(state_path, run_state)

        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        _removed_incomplete, failed_incomplete = _cleanup_incomplete_dirs(remote_parent)
        if failed_incomplete:
            raise RuntimeError(
                "failed to cleanup stale incomplete instance backup dirs: "
                + ", ".join(sorted(failed_incomplete))
            )
        if final_dir.exists():
            raise RuntimeError(f"backup target already exists: {final_dir}")
        tmp_dir.mkdir(parents=True, exist_ok=False)
        files_archive = _archive_instance_files(cfg, inst, tmp_dir)
        db_dir = tmp_dir / "databases" / effective_db.name
        db_dir.mkdir(parents=True, exist_ok=False)
        _run_mydumper(cfg, effective_db, db_dir)
        ok, verify_msg, db_bytes = _verify_dump_dir(db_dir)
        if not ok:
            raise RuntimeError(f"instance backup verification failed for {effective_db.name}: {verify_msg}")
        file_bytes = _asset_bytes(files_archive)
        bytes_written = int(db_bytes) + int(file_bytes)
        os.replace(tmp_dir, final_dir)
        tmp_dir = None
        storage_usage = _storage_usage(mount_path)
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "host_name": host_name,
            "path": str(final_dir),
            "bytes_written": bytes_written,
            "instances_total": len(instances),
            "instances_with_db": 1,
            "dumped_instances": [
                {
                    "instance_uid": inst.instance_uid,
                    "instance_name": inst.name,
                    "root": inst.root,
                    "primary_domain": inst.primary_domain or "",
                    "database": effective_db.name,
                    "method": "mydumper",
                    "bytes": db_bytes,
                    "path": _path_rel_to(final_dir, final_dir / "databases" / effective_db.name),
                }
            ],
            "method": "mydumper",
            "mydumper_threads": cfg.backup_mydumper_threads,
            "mydumper_compress": cfg.backup_mydumper_compress,
            "files_archive_path": str(final_dir / files_archive.name),
            "restorable_as_image": True,
        }
        if inst.mautic_major:
            marker["mautic_major"] = int(inst.mautic_major)
        if isinstance(storage_usage, dict):
            marker["storage"] = {
                "total_bytes": int(storage_usage.get("total_bytes") or 0),
                "used_bytes": int(storage_usage.get("used_bytes") or 0),
                "free_bytes": int(storage_usage.get("free_bytes") or 0),
                "used_pct": float(storage_usage.get("used_pct") or 0.0),
                "checked_at": str(storage_usage.get("checked_at") or ""),
            }
        _write_marker(final_dir, marker)
        _write_storage_backup_manifest_and_index(
            mount_path=mount_path,
            backup_dir=final_dir,
            marker=marker,
            kind="mcc.instance_backup.mydumper",
        )
        duration = int(time.monotonic() - start_monotonic)
        history = state.get("history", [])
        if not isinstance(history, list):
            history = []
        success_state = dict(run_state)
        success_state.update(
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "history": [
                    {
                        "ts": _utc_now_iso(),
                        "status": "ok",
                        "duration_sec": duration,
                        "backup_path": str(final_dir),
                        "bytes_written": bytes_written,
                        "instances_with_db": 1,
                        "instance": inst.name,
                    }
                ]
                + history[:19],
            }
        )
        if isinstance(storage_usage, dict):
            success_state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
            success_state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
            success_state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
            success_state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
            success_state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or "")
        _json_write(state_path, success_state)
        return BackupResult(
            ok=True,
            message=f"instance backup completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        history = state.get("history", [])
        if not isinstance(history, list):
            history = []
        fail_state = dict(run_state)
        fail_state.update(
            {
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "history": [
                    {
                        "ts": started_ts,
                        "status": "failed",
                        "duration_sec": duration,
                        "error": str(e),
                        "instance_selector": root or "",
                    }
                ]
                + history[:19],
            }
        )
        _json_write(state_path, fail_state)
        if tmp_dir is not None and tmp_dir.exists():
            subprocess.run(["rm", "-rf", str(tmp_dir)], check=False)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def backup_run(config: AgentConfig, root: str | None = None) -> BackupResult:
    cfg = _effective_cfg(config)
    method = _backup_method(cfg)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    start_monotonic = time.monotonic()
    started_ts = _utc_now_iso()
    try:
        _validate_cfg(cfg)
        tool_state = _ensure_backup_tools(cfg)
        try:
            instances = _list_instances(cfg)
        except Exception:
            if method == "xtrabackup" and cfg.backup_mysql_user and cfg.backup_mysql_password:
                instances = []
            else:
                raise
        db_instances = [x for x in instances if x.db]
        if method == "mydumper" and not db_instances:
            raise RuntimeError("No instances with DB credentials found in inventory")
        if method == "xtrabackup" and not db_instances and not (cfg.backup_mysql_user and cfg.backup_mysql_password):
            raise RuntimeError("No DB credentials found for xtrabackup ([backup.mysql] required when inventory has no DB)")
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail_state = dict(state)
        history = fail_state.get("history", [])
        if not isinstance(history, list):
            history = []
        history = [
            {
                "ts": started_ts,
                "status": "failed",
                "duration_sec": duration,
                "error": str(e),
            }
        ] + history[:19]
        fail_state.update(
            {
                "host_name": _host_name(cfg),
                "selected_root": root or "",
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "job": "backup.run",
                "method": method,
                "history": history,
            }
        )
        _json_write(state_path, fail_state)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    host_name = _host_name(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, host_name)
    date_dir = _fmt_local_date()
    tmp_dir = remote_parent / f".incomplete-{date_dir}-{_fmt_local_ts()}"
    final_dir = remote_parent / date_dir

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(
            ok=False,
            message="backup is already running for this host",
            state_path=str(state_path),
        )

    run_state: dict[str, Any] = dict(state)
    history = state.get("history", [])
    if not isinstance(history, list):
        history = []
    run_state.update(
        {
            "host_name": host_name,
            "instances_total": len(instances),
            "instances_with_db": len(db_instances),
            "selected_root": root or "",
            "last_run_at": started_ts,
            "last_status": "running",
            "last_error": "",
            "last_success_at": state.get("last_success_at", ""),
            "last_backup_path": state.get("last_backup_path", ""),
            "job": "backup.run",
            "method": method,
            "tool_state": tool_state,
        }
    )
    _json_write(state_path, run_state)

    bytes_written = 0
    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        _removed_incomplete, failed_incomplete = _cleanup_incomplete_dirs(remote_parent)
        if failed_incomplete:
            raise RuntimeError(
                "failed to cleanup stale incomplete backup dirs: "
                + ", ".join(sorted(failed_incomplete))
            )
        if method != "xtrabackup":
            _prune_by_copies(remote_parent, cfg.backup_retention_copies)
        if final_dir.exists():
            marker = _read_backup_marker(final_dir)
            if str(marker.get("status") or "").strip().lower() == "ok":
                duration = int(time.monotonic() - start_monotonic)
                storage_usage = _storage_usage(mount_path)
                existing_bytes: int | None
                try:
                    existing_bytes = int(marker.get("bytes_written")) if marker.get("bytes_written") is not None else None
                except Exception:
                    existing_bytes = None
                success_state = dict(run_state)
                hist_item = {
                    "ts": _utc_now_iso(),
                    "status": "ok_skip_existing",
                    "duration_sec": duration,
                    "backup_path": str(final_dir),
                    "bytes_written": existing_bytes,
                    "instances_with_db": len(db_instances),
                }
                history = [hist_item] + history[:19]
                success_state.update(
                    {
                        "last_status": "ok",
                        "last_error": "",
                        "last_success_at": _utc_now_iso(),
                        "last_duration_sec": duration,
                        "last_backup_path": str(final_dir),
                        "history": history,
                    }
                )
                if existing_bytes is not None:
                    success_state["last_bytes_written"] = existing_bytes
                if isinstance(storage_usage, dict):
                    success_state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
                    success_state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
                    success_state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
                    success_state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
                    success_state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or "")
                _json_write(state_path, success_state)
                return BackupResult(
                    ok=True,
                    message=f"backup already exists for today: {final_dir}",
                    state_path=str(state_path),
                    backup_path=str(final_dir),
                    duration_sec=duration,
                    bytes_written=existing_bytes,
                )
            raise RuntimeError(f"backup target already exists: {final_dir}")
        tmp_dir.mkdir(parents=True, exist_ok=False)

        _archive_files(cfg, tmp_dir)

        db_root = tmp_dir / "databases"
        db_root.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        dumped: list[dict[str, Any]] = []
        server_snapshot: dict[str, Any] = {}
        xtrabackup_plan: dict[str, Any] = {}
        xtrabackup_space_removed: list[str] = []
        if method == "xtrabackup":
            effective_db = _backup_db_from_config_or_instances(cfg, db_instances)
            server_snapshot = _mysql_server_snapshot(cfg, effective_db)
            db_dir = db_root / "physical-xtrabackup"
            xtrabackup_plan = _select_xtrabackup_plan(cfg, remote_parent)
            xtrabackup_plan, xtrabackup_space_removed = _ensure_xtrabackup_space(
                remote_parent,
                mount_path,
                xtrabackup_plan,
            )
            backup_kind = str(xtrabackup_plan.get("kind") or "full").strip().lower()
            incremental_base_dir = xtrabackup_plan.get("base_dir") if backup_kind == "incremental" else None
            if incremental_base_dir is not None and not isinstance(incremental_base_dir, Path):
                incremental_base_dir = None
            _run_xtrabackup(
                cfg,
                effective_db,
                db_dir,
                incremental_base_dir=incremental_base_dir,
            )
            ok, verify_msg, one_bytes = _verify_xtrabackup_dir(db_dir)
            if not ok:
                raise RuntimeError(f"xtrabackup verification failed: {verify_msg}")
            verified_kind = _xtrabackup_kind_from_checkpoints(db_dir) or backup_kind
            if verified_kind not in {"full", "incremental"}:
                verified_kind = backup_kind if backup_kind in {"full", "incremental"} else "full"
            if verified_kind == "full":
                xtrabackup_plan = {
                    "kind": "full",
                    "base_dir": None,
                    "base_path": "",
                    "chain_id": _xtrabackup_chain_id_for_full(final_dir),
                    "full_path": str(final_dir),
                    "chain_index": 0,
                }
            elif not str(xtrabackup_plan.get("chain_id") or "").strip():
                raise RuntimeError("xtrabackup incremental completed without chain metadata")
            total_bytes += one_bytes
            dumped.append(
                {
                    "instance_uid": "physical-xtrabackup",
                    "instance_name": cfg.backup_instance_name or cfg.backup_host_name or host_name,
                    "root": "",
                    "database": effective_db.name or "*",
                    "method": "xtrabackup",
                    "backup_kind": verified_kind,
                    "bytes": one_bytes,
                }
            )
        else:
            seen_db_keys: set[tuple[str, int, str, str]] = set()
            for inst in db_instances:
                assert inst.db is not None
                effective_db = _effective_db_for_instance(cfg, inst)
                key = (effective_db.host, effective_db.port, effective_db.user, effective_db.name)
                if key in seen_db_keys:
                    continue
                seen_db_keys.add(key)
                db_dir = db_root / f"{inst.instance_uid}__{effective_db.name}"
                db_dir.mkdir(parents=True, exist_ok=False)
                _run_mydumper(cfg, effective_db, db_dir)
                ok, verify_msg, one_bytes = _verify_dump_dir(db_dir)
                if not ok:
                    raise RuntimeError(f"backup verification failed for {inst.instance_uid}: {verify_msg}")
                total_bytes += one_bytes
                dumped.append(
                    {
                        "instance_uid": inst.instance_uid,
                        "instance_name": inst.name,
                        "root": inst.root,
                        "database": effective_db.name,
                        "method": "mydumper",
                        "bytes": one_bytes,
                    }
                )
        bytes_written = total_bytes

        os.replace(tmp_dir, final_dir)
        storage_usage = _storage_usage(mount_path)
        xtrabackup_kind = str(xtrabackup_plan.get("kind") or "").strip().lower() if method == "xtrabackup" else ""
        xtrabackup_base_path = str(xtrabackup_plan.get("base_path") or "").strip() if method == "xtrabackup" else ""
        xtrabackup_chain_id = str(xtrabackup_plan.get("chain_id") or "").strip() if method == "xtrabackup" else ""
        xtrabackup_full_path = str(xtrabackup_plan.get("full_path") or "").strip() if method == "xtrabackup" else ""
        if method == "xtrabackup" and xtrabackup_kind == "full":
            xtrabackup_chain_id = xtrabackup_chain_id or _xtrabackup_chain_id_for_full(final_dir)
            xtrabackup_full_path = xtrabackup_full_path or str(final_dir)
        retention_removed: list[str] = []
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "host_name": host_name,
            "path": str(final_dir),
            "bytes_written": bytes_written,
            "instances_total": len(instances),
            "instances_with_db": len(db_instances),
            "dumped_instances": dumped,
            "method": method,
            "mydumper_threads": cfg.backup_mydumper_threads if method == "mydumper" else None,
            "xtrabackup_parallel": cfg.backup_xtrabackup_parallel if method == "xtrabackup" else None,
            "server_snapshot": server_snapshot,
            "tool_state": tool_state,
        }
        if method == "xtrabackup":
            marker.update(
                {
                    "backup_kind": xtrabackup_kind or "full",
                    "chain_id": xtrabackup_chain_id,
                    "full_backup_path": xtrabackup_full_path,
                    "base_backup_path": xtrabackup_base_path,
                    "chain_index": int(xtrabackup_plan.get("chain_index") or 0),
                    "xtrabackup_incremental_enabled": bool(
                        getattr(cfg, "backup_xtrabackup_incremental_enabled", True)
                    ),
                    "xtrabackup_full_interval_days": int(
                        getattr(cfg, "backup_xtrabackup_full_interval_days", 7) or 7
                    ),
                    "xtrabackup_retention_full_copies": int(
                        getattr(cfg, "backup_xtrabackup_retention_full_copies", 3) or 3
                    ),
                    "xtrabackup_retention_incremental_days": int(
                        getattr(cfg, "backup_xtrabackup_retention_incremental_days", 7) or 7
                    ),
                    "xtrabackup_space_pruned": xtrabackup_space_removed,
                }
            )
        if isinstance(storage_usage, dict):
            marker["storage"] = {
                "total_bytes": int(storage_usage.get("total_bytes") or 0),
                "used_bytes": int(storage_usage.get("used_bytes") or 0),
                "free_bytes": int(storage_usage.get("free_bytes") or 0),
                "used_pct": float(storage_usage.get("used_pct") or 0.0),
                "checked_at": str(storage_usage.get("checked_at") or ""),
            }
        _write_marker(final_dir, marker)
        if method == "xtrabackup":
            retention_removed = _prune_xtrabackup_retention(remote_parent, cfg)
            if retention_removed:
                marker["retention_removed"] = retention_removed
                _write_marker(final_dir, marker)
        _write_storage_backup_manifest_and_index(
            mount_path=mount_path,
            backup_dir=final_dir,
            marker=marker,
            kind=f"mcc.host_backup.{method}",
        )

        duration = int(time.monotonic() - start_monotonic)
        success_state = dict(run_state)
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "ok",
            "duration_sec": duration,
            "backup_path": str(final_dir),
            "bytes_written": bytes_written,
            "instances_with_db": len(db_instances),
        }
        if method == "xtrabackup":
            hist_item["backup_kind"] = xtrabackup_kind or "full"
            hist_item["chain_id"] = xtrabackup_chain_id
            hist_item["base_backup_path"] = xtrabackup_base_path
            hist_item["space_pruned"] = xtrabackup_space_removed
            hist_item["retention_removed"] = retention_removed
        history = [hist_item] + history[:19]
        success_state.update(
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "history": history,
            }
        )
        if method == "xtrabackup":
            success_state["last_backup_kind"] = xtrabackup_kind or "full"
            success_state["last_chain_id"] = xtrabackup_chain_id
            success_state["last_base_backup_path"] = xtrabackup_base_path
            success_state["last_space_pruned"] = xtrabackup_space_removed
            success_state["last_retention_removed"] = retention_removed
        if isinstance(storage_usage, dict):
            success_state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
            success_state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
            success_state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
            success_state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
            success_state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or "")
        _json_write(state_path, success_state)
        return BackupResult(
            ok=True,
            message=f"host backup completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail_state = dict(run_state)
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "failed",
            "duration_sec": duration,
            "error": str(e),
        }
        history = [hist_item] + history[:19]
        fail_state.update(
            {
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "history": history,
            }
        )
        _json_write(state_path, fail_state)
        if tmp_dir.exists():
            subprocess.run(["rm", "-rf", str(tmp_dir)], check=False)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path))
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def backup_preflight(config: AgentConfig, root: str | None = None) -> BackupResult:
    cfg = _effective_cfg(config)
    method = _backup_method(cfg)
    state_path = _state_path(cfg)
    start_monotonic = time.monotonic()
    started_ts = _utc_now_iso()
    details: dict[str, Any] = {"method": method, "job": "backup.preflight", "ts": started_ts}
    try:
        _validate_cfg(cfg)
        details["tools"] = _ensure_backup_tools(cfg)
        try:
            instances = _list_instances(cfg)
        except Exception:
            if method == "xtrabackup" and cfg.backup_mysql_user and cfg.backup_mysql_password:
                instances = []
            else:
                raise
        db_instances = [x for x in instances if x.db]
        details["instances_total"] = len(instances)
        details["instances_with_db"] = len(db_instances)
        if method == "mydumper" and not db_instances:
            raise RuntimeError("No instances with DB credentials found in inventory")
        db = _backup_db_from_config_or_instances(cfg, db_instances)
        details["mysql"] = _mysql_server_snapshot(cfg, db)
        if method == "mydumper":
            proc = _run([cfg.backup_mydumper_bin, "--version"], timeout_sec=15, check=False)
            details["mydumper_version"] = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[:3]
        else:
            proc = _run([cfg.backup_xtrabackup_bin, "--version"], timeout_sec=15, check=False)
            details["xtrabackup_version"] = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[:3]

        host_name = _host_name(cfg)
        mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
        remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, host_name)
        probe_dir = remote_parent / f".preflight-{_fmt_local_ts()}"
        _mount(cfg, mount_path)
        try:
            remote_parent.mkdir(parents=True, exist_ok=True)
            probe_dir.mkdir(parents=False, exist_ok=False)
            marker = probe_dir / "probe.json"
            marker.write_text(json.dumps({"status": "ok", "ts_utc": _utc_now_iso()}, ensure_ascii=True) + "\n", encoding="utf-8")
            marker.unlink(missing_ok=True)
            probe_dir.rmdir()
            usage = _storage_usage(mount_path)
            if isinstance(usage, dict):
                details["storage"] = usage
        finally:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)

        duration = int(time.monotonic() - start_monotonic)
        details["duration_sec"] = duration
        state = _json_read(state_path)
        state.update(
            {
                "host_name": host_name,
                "selected_root": root or "",
                "last_preflight_at": started_ts,
                "last_preflight_status": "ok",
                "last_preflight_error": "",
                "last_preflight": details,
            }
        )
        _json_write(state_path, state)
        return BackupResult(ok=True, message="backup preflight ok", state_path=str(state_path), duration_sec=duration)
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        details["duration_sec"] = duration
        details["error"] = str(e)
        state = _json_read(state_path)
        state.update(
            {
                "host_name": _host_name(cfg),
                "selected_root": root or "",
                "last_preflight_at": started_ts,
                "last_preflight_status": "failed",
                "last_preflight_error": str(e),
                "last_preflight": details,
            }
        )
        _json_write(state_path, state)
        return BackupResult(ok=False, message=f"backup preflight failed: {e}", state_path=str(state_path), duration_sec=duration)


def backup_restore(
    config: AgentConfig,
    *,
    root: str | None = None,
    date: str | None = None,
    path: str | None = None,
) -> BackupResult:
    _ = root
    cfg = _effective_cfg(config)
    _validate_cfg(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    host_name = _host_name(cfg)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, host_name)
    start_monotonic = time.monotonic()

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(
            ok=False,
            message="backup/restore lock is busy on this host",
            state_path=str(state_path),
        )

    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        source_dir = _resolve_restore_source(remote_parent=remote_parent, date=date, path=path)
        marker = _read_backup_marker(source_dir)

        if cfg.backup_restore_apply_files:
            files_archive = source_dir / cfg.backup_archive_name
            if files_archive.exists():
                _run(["tar", "-xzf", str(files_archive), "-C", "/"], timeout_sec=cfg.backup_dump_timeout_sec, check=True)

        restored_dbs = 0
        if cfg.backup_restore_apply_databases:
            db_root = source_dir / "databases"
            if db_root.exists() and db_root.is_dir():
                instances = _list_instances(cfg, force_rescan=True)
                for dump_dir in sorted([x for x in db_root.iterdir() if x.is_dir()], key=lambda p: p.name):
                    db = _candidate_db(dump_dir, instances, cfg)
                    if db is None:
                        continue
                    _run_mysql_sql(cfg, db, f"CREATE DATABASE IF NOT EXISTS `{db.name}`")
                    _run_myloader(cfg, db, dump_dir)
                    restored_dbs += 1

        duration = int(time.monotonic() - start_monotonic)
        last = dict(state)
        history = last.get("history", [])
        if not isinstance(history, list):
            history = []
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "restore_ok",
            "duration_sec": duration,
            "backup_path": str(source_dir),
            "restored_databases": restored_dbs,
        }
        history = [hist_item] + history[:19]
        last.update(
            {
                "last_restore_at": _utc_now_iso(),
                "last_restore_status": "ok",
                "last_restore_error": "",
                "last_restore_path": str(source_dir),
                "last_restore_duration_sec": duration,
                "last_restore_databases": restored_dbs,
                "history": history,
            }
        )
        if marker:
            last["last_restore_marker"] = marker
        _json_write(state_path, last)
        return BackupResult(
            ok=True,
            message=f"restore completed: {source_dir}",
            state_path=str(state_path),
            backup_path=str(source_dir),
            duration_sec=duration,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail = dict(state)
        history = fail.get("history", [])
        if not isinstance(history, list):
            history = []
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "restore_failed",
            "duration_sec": duration,
            "error": str(e),
        }
        history = [hist_item] + history[:19]
        fail.update(
            {
                "last_restore_at": _utc_now_iso(),
                "last_restore_status": "failed",
                "last_restore_error": str(e),
                "last_restore_duration_sec": duration,
                "history": history,
            }
        )
        _json_write(state_path, fail)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def backup_storage_probe(config: AgentConfig, root: str | None = None) -> dict[str, Any]:
    _ = root
    cfg = _effective_cfg(config)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    checked_at = _utc_now_iso()

    try:
        _validate_cfg(cfg)
    except Exception as e:
        state["last_storage_probe_at"] = checked_at
        state["last_storage_probe_status"] = "failed"
        state["last_storage_probe_error"] = str(e)
        _json_write(state_path, state)
        return {
            "ok": False,
            "status": "failed",
            "error": str(e),
            "state_path": str(state_path),
        }

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "ok": True,
                "status": "skipped",
                "reason": "backup_lock_busy",
                "state_path": str(state_path),
            }

        try:
            _mount(cfg, mount_path)
            storage_usage = _storage_usage(mount_path)
            if not isinstance(storage_usage, dict):
                raise RuntimeError("storage usage probe returned no data")
            state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
            state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
            state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
            state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
            state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or checked_at)
            state["last_storage_probe_at"] = checked_at
            state["last_storage_probe_status"] = "ok"
            state["last_storage_probe_error"] = ""
            _json_write(state_path, state)
            return {
                "ok": True,
                "status": "ok",
                "state_path": str(state_path),
                "checked_at": state["last_storage_checked_at"],
            }
        except Exception as e:
            state["last_storage_probe_at"] = checked_at
            state["last_storage_probe_status"] = "failed"
            state["last_storage_probe_error"] = str(e)
            _json_write(state_path, state)
            return {
                "ok": False,
                "status": "failed",
                "error": str(e),
                "state_path": str(state_path),
            }
        finally:
            try:
                _unmount(mount_path, cfg.backup_unmount_timeout_sec)
            except Exception:
                pass
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fh.close()
        except Exception:
            pass


def backup_status(config: AgentConfig, root: str | None = None) -> dict[str, Any]:
    _ = root
    cfg = _effective_cfg(config)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    state.setdefault("host_name", _host_name(cfg))
    state["mounted"] = _mounted(mount_path)
    state["state_path"] = str(state_path)
    return state


def backup_state_for_push(config: AgentConfig) -> dict[str, Any]:
    st = backup_status(config)
    out = {
        "last_run_at": st.get("last_run_at"),
        "last_success_at": st.get("last_success_at"),
        "last_status": st.get("last_status"),
        "last_error": st.get("last_error"),
        "last_backup_path": st.get("last_backup_path"),
        "last_duration_sec": st.get("last_duration_sec"),
        "last_bytes_written": st.get("last_bytes_written"),
        "last_restore_at": st.get("last_restore_at"),
        "last_restore_status": st.get("last_restore_status"),
        "last_restore_error": st.get("last_restore_error"),
        "last_restore_path": st.get("last_restore_path"),
        "last_storage_checked_at": st.get("last_storage_checked_at"),
        "last_storage_total_bytes": st.get("last_storage_total_bytes"),
        "last_storage_used_bytes": st.get("last_storage_used_bytes"),
        "last_storage_free_bytes": st.get("last_storage_free_bytes"),
        "last_storage_used_pct": st.get("last_storage_used_pct"),
        "last_storage_probe_at": st.get("last_storage_probe_at"),
        "last_storage_probe_status": st.get("last_storage_probe_status"),
        "last_storage_probe_error": st.get("last_storage_probe_error"),
    }
    if bool(getattr(config, "backup_cluster_enabled", False)):
        try:
            cst = cluster_backup_status(config)
            out.update(
                {
                    "cluster_enabled": cst.get("cluster_enabled"),
                    "cluster_name": cst.get("cluster_name"),
                    "last_backup_kind": cst.get("last_backup_kind"),
                    "last_chain_id": cst.get("last_chain_id"),
                    "last_local_full_path": cst.get("last_local_full_path") or cst.get("current_full"),
                    "last_local_full_at": cst.get("last_local_full_at"),
                    "last_local_incremental_path": cst.get("last_local_incremental_path") or cst.get("latest_incremental"),
                    "last_local_incremental_at": cst.get("last_local_incremental_at"),
                    "last_files_snapshot_path": cst.get("last_files_snapshot_path") or cst.get("latest_files_snapshot"),
                    "last_files_snapshot_at": cst.get("last_files_snapshot_at"),
                    "last_offsite_backup_path": cst.get("last_offsite_backup_path"),
                    "last_offsite_backup_at": cst.get("last_offsite_backup_at"),
                    "last_offsite_files_archive_path": cst.get("last_offsite_files_archive_path"),
                    "last_offsite_files_archive_ok": cst.get("last_offsite_files_archive_ok"),
                    "last_offsite_files_bytes": cst.get("last_offsite_files_bytes"),
                    "cluster_integrity_status": cst.get("cluster_integrity_status"),
                    "cluster_integrity_error": cst.get("cluster_integrity_error"),
                    "last_remote_retention_plan": cst.get("last_remote_retention_plan"),
                }
            )
            if str(cst.get("cluster_integrity_status") or "").strip().lower() == "failed":
                out["last_status"] = "failed"
                out["last_error"] = str(cst.get("cluster_integrity_error") or "").strip() or out.get("last_error")
        except Exception:
            pass
    return out


def backup_profile_for_push(config: AgentConfig) -> dict[str, Any]:
    cfg = _effective_cfg(config)
    if not cfg.backup_enabled:
        return {"enabled": False}
    profile = backup_profile_get(config)
    payload: dict[str, Any] = {
        "enabled": bool(cfg.backup_enabled),
        "storage": {
            "host": cfg.backup_ssh_host,
            "port": int(cfg.backup_ssh_port),
            "user": cfg.backup_ssh_user,
            "remote_path": cfg.backup_ssh_remote_path,
            "key_file": cfg.backup_ssh_key_file or "",
            "password": cfg.backup_ssh_password or "",
        },
        "mysql": {
            "host": cfg.backup_mysql_host or "",
            "port": int(cfg.backup_mysql_port or 0),
            "user": cfg.backup_mysql_user or "",
            "password": cfg.backup_mysql_password or "",
            "database": cfg.backup_mysql_database or "",
        },
        "archive": {
            "enabled": bool(cfg.backup_archive_enabled),
            "name": cfg.backup_archive_name,
            "paths": list(cfg.backup_archive_paths),
        },
        "schedule": {
            "enabled": bool(cfg.backup_schedule_enabled),
            "interval_sec": int(cfg.backup_schedule_interval_sec),
            "quiet_hour": int(cfg.backup_schedule_quiet_hour),
            "quiet_window_min": int(cfg.backup_schedule_quiet_window_min),
            "pre_pause_sec": int(cfg.backup_schedule_pre_pause_sec),
        },
        "source": "profile_db" if profile else "config",
    }
    return payload


def backup_prune(config: AgentConfig, root: str | None = None) -> BackupResult:
    _ = root
    cfg = _effective_cfg(config)
    _validate_cfg(cfg)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, _host_name(cfg))
    removed: list[str] = []
    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        if _backup_method(cfg) == "xtrabackup":
            removed = _prune_xtrabackup_retention(remote_parent, cfg)
        else:
            removed = _prune_by_copies(remote_parent, cfg.backup_retention_copies)
        return BackupResult(
            ok=True,
            message=f"prune done, removed={len(removed)}",
            state_path=str(_state_path(cfg)),
        )
    except Exception as e:
        return BackupResult(
            ok=False,
            message=str(e),
            state_path=str(_state_path(cfg)),
        )
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
