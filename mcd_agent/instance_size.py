from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.models import MauticInstall


DU_TIMEOUT_SEC = 45
DB_SIZE_QUERY = """
SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) AS size_bytes
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default


def _du_bytes(path: Path, *, timeout_sec: int = DU_TIMEOUT_SEC) -> tuple[int | None, str | None]:
    try:
        if not path.exists():
            return None, "path_missing"
        if not path.is_dir():
            return None, "path_not_directory"
        proc = subprocess.run(
            ["du", "-sk", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return None, msg[:240] or f"du_failed_rc_{proc.returncode}"
        first = (proc.stdout or "").strip().split()
        if not first:
            return None, "du_empty_output"
        return _safe_int(first[0], 0) * 1024, None
    except subprocess.TimeoutExpired:
        return None, "du_timeout"
    except Exception as exc:
        return None, str(exc)[:240]


def _db_size_bytes(inst: MauticInstall) -> tuple[int | None, str | None]:
    if not inst.db:
        return None, "db_config_missing"
    try:
        rows = MauticDB(inst.db).fetch_rows(DB_SIZE_QUERY, limit=1)
        return _safe_int((rows[0] if rows else {}).get("size_bytes"), 0), None
    except Exception as exc:
        return None, str(exc)[:240]


def _subdir_breakdown(root: Path) -> dict[str, int]:
    candidates = {
        "media": [root / "media", root / "app" / "media"],
        "var": [root / "var"],
        "cache": [root / "var" / "cache", root / "app" / "cache"],
        "logs": [root / "var" / "logs", root / "var" / "log", root / "app" / "logs"],
    }
    out: dict[str, int] = {}
    seen: set[Path] = set()
    for key, paths in candidates.items():
        total = 0
        for path in paths:
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            value, _err = _du_bytes(path, timeout_sec=15)
            if value is not None:
                total += value
        if total > 0:
            out[key] = total
    return out


def collect_instance_sizes(installs: list[MauticInstall]) -> list[dict[str, Any]]:
    measured_at = _utc_now()
    rows: list[dict[str, Any]] = []
    for inst in installs:
        root = Path(str(inst.root or "").strip() or "/")
        errors: list[str] = []
        root_bytes, root_error = _du_bytes(root)
        if root_error:
            errors.append(f"root:{root_error}")
        db_bytes, db_error = _db_size_bytes(inst)
        if db_error and db_error != "db_config_missing":
            errors.append(f"db:{db_error}")
        total = int(root_bytes or 0) + int(db_bytes or 0)
        rows.append(
            {
                "instance_uid": inst.instance_uid,
                "name": inst.name,
                "root": inst.root,
                "root_bytes": root_bytes,
                "db_bytes": db_bytes,
                "total_bytes": total if total > 0 else None,
                "breakdown": _subdir_breakdown(root) if root_bytes is not None else {},
                "measured_at_utc": measured_at,
                "errors": errors,
            }
        )
    rows.sort(key=lambda x: str(x.get("instance_uid") or ""))
    return rows
