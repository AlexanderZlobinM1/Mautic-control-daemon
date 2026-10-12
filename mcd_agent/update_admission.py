"""Cross-process serialization for update barriers and new task admissions."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX development environments
    fcntl = None  # type: ignore[assignment]


def scheduler_pause_path(config_or_path: object) -> Path:
    raw = getattr(config_or_path, "scheduler_pause_flag_path", config_or_path)
    return Path(str(raw or "/opt/mcd/var/scheduler.pause"))


@contextmanager
def scheduler_admission_lock(config_or_path: object) -> Iterator[None]:
    """Serialize the barrier transition with the final new-task admission."""
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

