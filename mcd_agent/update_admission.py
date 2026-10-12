"""Cross-process serialization for update barriers and new task admissions."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX development environments
    fcntl = None  # type: ignore[assignment]


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
