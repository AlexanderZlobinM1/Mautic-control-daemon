"""Cross-process serialization for update barriers and new task admissions."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX development environments
    fcntl = None  # type: ignore[assignment]


UPDATE_ADMISSION_BARRIER = "mcd-update-admission\n"
UPDATE_ADMISSION_OWNER_SCHEMA = "mcd-update-admission-owner-v1"


def scheduler_pause_path(config_or_path: object) -> Path:
    raw = getattr(config_or_path, "scheduler_pause_flag_path", None)
    if raw is None:
        if isinstance(config_or_path, (str, Path)):
            raw = config_or_path
        else:
            # Production AgentConfig always supplies the persisted path. Keep
            # lightweight test/config doubles out of /opt when they omit it.
            raw = os.environ.get("MCD_SCHEDULER_PAUSE_FLAG_PATH") or (
                Path(tempfile.gettempdir()) / f"mcd-scheduler-{os.getpid()}.pause"
            )
    return Path(str(raw or "/opt/mcd/var/scheduler.pause"))


def scheduler_pause_owner_path(config_or_path: object) -> Path:
    """Return the metadata path for an updater-owned pause barrier."""
    pause = scheduler_pause_path(config_or_path)
    return pause.with_name(f"{pause.name}.owner.json")


def source_operation_lock_path(config_or_path: object) -> Path:
    """Return the persisted MCD source/update lease path.

    The updater keeps this lease for the complete source/package mutation. A
    pause barrier without a live owner and without this lease is recoverable,
    including barriers left by a process that crashed during an update.
    """
    raw = getattr(config_or_path, "state_db_path", None)
    if raw:
        return Path(str(raw)).parent / "mcd-self-update.lock"
    return scheduler_pause_path(config_or_path).parent / "mcd-self-update.lock"


def _pid_start_time(pid: int) -> int | None:
    """Read Linux process start ticks for PID-reuse-safe owner checks."""
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2 :].split()
        return int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def create_update_admission_barrier(config_or_path: object, *, target: str = "") -> dict[str, Any]:
    """Publish an updater-owned pause and its process identity.

    Callers must hold :func:`scheduler_admission_lock` while invoking this
    function. Metadata is written before the flag so a crash cannot leave a
    new flag with no owner record.
    """
    pause = scheduler_pause_path(config_or_path)
    owner = scheduler_pause_owner_path(config_or_path)
    payload: dict[str, Any] = {
        "schema": UPDATE_ADMISSION_OWNER_SCHEMA,
        "operation": "mcd-self-update",
        "pid": os.getpid(),
        "pid_start_time": _pid_start_time(os.getpid()),
        "created_at": time.time(),
    }
    if str(target).strip():
        payload["target"] = str(target).strip()
    _atomic_json_write(owner, payload)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(UPDATE_ADMISSION_BARRIER, encoding="utf-8")
    return payload


def remove_update_admission_barrier(config_or_path: object) -> bool:
    """Remove only the updater-owned barrier and its metadata.

    Manual/maintenance values such as ``paused`` are intentionally left
    untouched. The caller may invoke this after a successful or failed update
    once its source-operation lease has been released.
    """
    pause = scheduler_pause_path(config_or_path)
    try:
        if pause.read_text(encoding="utf-8") != UPDATE_ADMISSION_BARRIER:
            return False
    except OSError:
        return False
    pause.unlink(missing_ok=True)
    scheduler_pause_owner_path(config_or_path).unlink(missing_ok=True)
    return True


def _source_operation_lock_available(config_or_path: object) -> bool:
    """Check the source/update lease without changing its contents."""
    lock_path = source_operation_lock_path(config_or_path)
    if not lock_path.exists():
        return True
    if fcntl is None:  # pragma: no cover
        return False
    try:
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def recover_stale_update_admission_barrier(config_or_path: object) -> dict[str, Any]:
    """Recover an orphaned updater barrier without bypassing a live lease.

    A live owner PID or source-operation lease keeps the barrier. A legacy
    ``mcd-update-admission`` flag without either is safe to remove because no
    MCD critical update operation remains. Other pause values are never
    touched.
    """
    pause = scheduler_pause_path(config_or_path)
    try:
        if pause.read_text(encoding="utf-8") != UPDATE_ADMISSION_BARRIER:
            return {"active": False, "recovered": False, "reason": "not_mcd_update_barrier"}
    except OSError:
        return {"active": False, "recovered": False, "reason": "barrier_missing"}

    with scheduler_admission_lock(config_or_path):
        try:
            if pause.read_text(encoding="utf-8") != UPDATE_ADMISSION_BARRIER:
                return {"active": False, "recovered": False, "reason": "barrier_changed"}
        except OSError:
            return {"active": False, "recovered": False, "reason": "barrier_missing"}

        owner_path = scheduler_pause_owner_path(config_or_path)
        owner: dict[str, Any] = {}
        try:
            raw = json.loads(owner_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                owner = raw
        except (OSError, ValueError, TypeError):
            owner = {}

        pid = int(owner.get("pid", 0) or 0)
        expected_start = owner.get("pid_start_time")
        live_owner = _pid_is_alive(pid)
        if live_owner and expected_start is not None:
            try:
                live_owner = _pid_start_time(pid) == int(expected_start)
            except (TypeError, ValueError):
                live_owner = False
        if live_owner:
            return {"active": True, "recovered": False, "reason": "owner_alive", "pid": pid}
        if not _source_operation_lock_available(config_or_path):
            return {"active": True, "recovered": False, "reason": "source_operation_lock"}

        pause.unlink(missing_ok=True)
        owner_path.unlink(missing_ok=True)
        return {
            "active": False,
            "recovered": True,
            "reason": "orphaned_mcd_update_barrier",
            "owner_pid": pid or None,
        }


@contextmanager
def scheduler_admission_lock(config_or_path: object) -> Iterator[None]:
    """Serialize the barrier transition with the final new-task admission."""
    if not isinstance(config_or_path, (str, Path)) and getattr(
        config_or_path, "scheduler_pause_flag_path", None
    ) is None:
        # Minimal test/config doubles have no filesystem authority. Do not
        # serialize them through a synthetic production-like lock path.
        yield
        return
    pause_path = scheduler_pause_path(config_or_path)
    lock_path = pause_path.with_name(pause_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover
        yield
        return
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
