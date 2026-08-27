from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PID_RE = re.compile(r"^\s*(\d{1,10})\s*$")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _PID_RE.match(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def cleanup_stale_mautic_file_locks(
    root: str | Path,
    *,
    min_age_sec: int = 21600,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Remove stale Symfony/Mautic lock files without touching live commands."""
    root_path = Path(root)
    run_dir = root_path / "var" / "cache" / "run"
    now = float(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
    min_age = max(0, int(min_age_sec or 0))
    rows: list[dict[str, Any]] = []
    cleared = 0
    skipped_live = 0
    skipped_young_unknown = 0

    if not run_dir.exists():
        return {
            "status": "skipped",
            "reason": "missing_run_dir",
            "run_dir": str(run_dir),
            "file_locks": rows,
            "cleared_file_locks": 0,
            "skipped_live_file_locks": 0,
            "skipped_young_unknown_file_locks": 0,
        }
    if not run_dir.is_dir():
        return {
            "status": "skipped",
            "reason": "run_dir_not_directory",
            "run_dir": str(run_dir),
            "file_locks": rows,
            "cleared_file_locks": 0,
            "skipped_live_file_locks": 0,
            "skipped_young_unknown_file_locks": 0,
        }

    for path in sorted(run_dir.glob("sf.mautic-*.lock")):
        row: dict[str, Any] = {"path": str(path)}
        try:
            stat = path.lstat()
            if path.is_symlink() or not path.is_file():
                row["status"] = "skipped"
                row["reason"] = "not_regular_file"
                rows.append(row)
                continue
            age_sec = max(0, int(now - float(stat.st_mtime)))
            row["age_sec"] = age_sec
            row["mtime_utc"] = datetime.fromtimestamp(float(stat.st_mtime), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            pid = _read_lock_pid(path)
            row["pid"] = pid
            if pid is not None and _pid_alive(pid):
                row["status"] = "skipped"
                row["reason"] = "pid_alive"
                skipped_live += 1
                rows.append(row)
                continue
            if pid is None and age_sec < min_age:
                row["status"] = "skipped"
                row["reason"] = "unknown_pid_young"
                skipped_young_unknown += 1
                rows.append(row)
                continue
            path.unlink()
            row["status"] = "cleared"
            row["reason"] = "dead_pid" if pid is not None else "unknown_pid_old"
            cleared += 1
        except OSError as exc:
            row["status"] = "error"
            row["reason"] = str(exc)
        rows.append(row)

    return {
        "status": "ok",
        "run_dir": str(run_dir),
        "file_locks": rows,
        "cleared_file_locks": int(cleared),
        "skipped_live_file_locks": int(skipped_live),
        "skipped_young_unknown_file_locks": int(skipped_young_unknown),
    }
