from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.models import MauticInstall


DU_TIMEOUT_SEC = 45
INSTANCE_SIZE_CACHE_KEY = "mcd_instance_size_cache"
INSTANCE_SIZE_CACHE_SCHEMA = "mcd-instance-size-cache-v1"
INSTANCE_SIZE_REFRESH_SEC = 24 * 60 * 60
DB_SIZE_QUERY = """
SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) AS size_bytes
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
"""

_INSTANCE_SIZE_SCAN_LOCK = threading.Lock()


def _utc_now() -> str:
    return _utc_at(time.time())


def _utc_at(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default


def _db_signature(inst: MauticInstall) -> str:
    db = inst.db
    if db is None:
        return "none"
    # The signature identifies the measured database without storing a
    # password or any other credential in the size cache.
    return json.dumps(
        {
            "host": str(db.host or ""),
            "port": int(db.port or 0),
            "name": str(db.name or ""),
            "user": str(db.user or ""),
            "table_prefix": str(db.table_prefix or ""),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


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


class InstanceSizeCache:
    """Persisted per-instance size observations with a 24-hour refresh TTL.

    The cache deliberately serializes the complete refresh pass.  A refresh
    measures one instance, stores its result, and only then moves to the next
    instance.  This keeps the number of concurrent ``du`` processes at one,
    including when a second MCD command tries to publish state in parallel.
    """

    def __init__(self, runtime_store: Any | None = None, *, refresh_after_sec: int = INSTANCE_SIZE_REFRESH_SEC) -> None:
        self.runtime_store = runtime_store
        self.refresh_after_sec = max(1, int(refresh_after_sec or INSTANCE_SIZE_REFRESH_SEC))
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _cache_lock_path(self) -> Path | None:
        raw_path = str(getattr(self.runtime_store, "path", "") or "").strip()
        if not raw_path or raw_path == ":memory:":
            return None
        state_path = Path(raw_path)
        return state_path.with_name(f"{state_path.name}.instance-size.lock")

    @contextmanager
    def _exclusive_lock(self):
        lock_path = self._cache_lock_path()
        if lock_path is None:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.runtime_store is None:
            return
        try:
            payload = self.runtime_store.get_runtime_sync(INSTANCE_SIZE_CACHE_KEY)
        except Exception as exc:
            logging.debug("instance size cache read failed: %s", exc)
            return
        if not isinstance(payload, dict) or payload.get("schema") != INSTANCE_SIZE_CACHE_SCHEMA:
            return
        raw_rows = payload.get("instances")
        if not isinstance(raw_rows, dict):
            return
        self._rows = {
            str(uid): dict(row)
            for uid, row in raw_rows.items()
            if str(uid).strip() and isinstance(row, dict)
        }

    def _persist(self, now_ts: float) -> None:
        if self.runtime_store is None:
            return
        payload = {
            "schema": INSTANCE_SIZE_CACHE_SCHEMA,
            "refresh_after_sec": self.refresh_after_sec,
            "updated_at_utc": _utc_at(now_ts),
            "instances": self._rows,
        }
        try:
            self.runtime_store.put_runtime_sync(INSTANCE_SIZE_CACHE_KEY, payload)
        except Exception as exc:
            # The state payload can still use the in-memory observation.  A
            # later cycle will retry persistence without turning a status
            # heartbeat into a scheduler failure.
            logging.warning("instance size cache write failed: %s", exc)

    @staticmethod
    def _key(inst: MauticInstall) -> str:
        uid = str(inst.instance_uid or "").strip()
        return uid or f"root:{str(inst.root or '').strip()}"

    def _is_fresh(self, row: dict[str, Any], inst: MauticInstall, now_ts: float) -> bool:
        if str(row.get("root") or "") != str(inst.root or ""):
            return False
        if str(row.get("_db_signature") or "") != _db_signature(inst):
            return False
        try:
            checked_at = float(row.get("_checked_at_ts") or row.get("_measured_at_ts") or 0)
        except (TypeError, ValueError):
            return False
        return checked_at > 0 and now_ts - checked_at < self.refresh_after_sec

    @staticmethod
    def _public_row(row: dict[str, Any], inst: MauticInstall) -> dict[str, Any]:
        public = {key: value for key, value in row.items() if not str(key).startswith("_")}
        public["instance_uid"] = inst.instance_uid
        public["name"] = inst.name
        public["root"] = inst.root
        return public

    def _measure(self, inst: MauticInstall, previous: dict[str, Any] | None, now_ts: float) -> dict[str, Any]:
        root = Path(str(inst.root or "").strip() or "/")
        errors: list[str] = []
        root_bytes, root_error = _du_bytes(root)
        if root_error:
            errors.append(f"root:{root_error}")
        db_bytes, db_error = _db_size_bytes(inst)
        if db_error and db_error != "db_config_missing":
            errors.append(f"db:{db_error}")

        # Keep the last successful value during a transient read failure, but
        # record the failed attempt and cooldown it for the same 24-hour
        # window.  This prevents a broken path or DB from creating a retry
        # loop in the independent heartbeat worker.
        if previous and str(previous.get("root") or "") == str(inst.root or ""):
            if root_bytes is None:
                root_bytes = previous.get("root_bytes")
            if db_bytes is None and str(previous.get("_db_signature") or "") == _db_signature(inst):
                db_bytes = previous.get("db_bytes")
        total = int(root_bytes or 0) + int(db_bytes or 0)
        measured_at_ts = (
            now_ts
            if not errors
            else float(previous.get("_measured_at_ts") or 0) if previous else 0
        )
        measured_at_utc = (
            _utc_at(measured_at_ts)
            if measured_at_ts > 0
            else str(previous.get("measured_at_utc") or "") if previous else ""
        )
        row: dict[str, Any] = {
            "instance_uid": inst.instance_uid,
            "name": inst.name,
            "root": inst.root,
            "root_bytes": root_bytes,
            "db_bytes": db_bytes,
            "total_bytes": total if total > 0 else None,
            "breakdown": (
                _subdir_breakdown(root)
                if root_bytes is not None and root_error is None
                else dict(previous.get("breakdown") or {}) if previous else {}
            ),
            "measured_at_utc": measured_at_utc,
            "errors": errors,
            "_db_signature": _db_signature(inst),
            "_checked_at_ts": now_ts,
            "_measured_at_ts": measured_at_ts,
        }
        return row

    def collect(self, installs: list[MauticInstall], *, now_ts: float | None = None) -> list[dict[str, Any]]:
        now = float(now_ts if now_ts is not None else time.time())
        rows: list[dict[str, Any]] = []
        # The process-local lock handles MCD's many state/scheduler threads;
        # the file lock extends the same guarantee to a concurrent CLI process.
        with _INSTANCE_SIZE_SCAN_LOCK:
            with self._exclusive_lock():
                self._load()
                for inst in sorted(installs, key=lambda item: self._key(item)):
                    key = self._key(inst)
                    cached = self._rows.get(key)
                    if cached is None or not self._is_fresh(cached, inst, now):
                        refreshed = self._measure(inst, cached, now)
                        self._rows[key] = refreshed
                        # Persist each completed instance before advancing to
                        # the next one, so a restart never loses finished work.
                        self._persist(now)
                        cached = refreshed
                    rows.append(self._public_row(cached, inst))
        rows.sort(key=lambda x: str(x.get("instance_uid") or ""))
        return rows


def collect_instance_sizes(
    installs: list[MauticInstall],
    *,
    runtime_store: Any | None = None,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    return InstanceSizeCache(runtime_store).collect(installs, now_ts=now_ts)
