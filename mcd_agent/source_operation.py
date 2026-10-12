"""Keep the installed source stable throughout an executing operation."""
from contextlib import contextmanager
from functools import wraps
import fcntl
from typing import Any, Iterator


def _config_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    config = kwargs.get("config")
    if config is None and args:
        config = args[0]
    if config is None:
        raise TypeError("source operation requires a config argument")
    return config


@contextmanager
def source_operation_lease(config: Any) -> Iterator[None]:
    """Hold the shared MCD source-operation barrier for a critical operation.

    Self-update takes the same file lock exclusively.  This keeps source,
    deployment, and migration entrypoints explicit and fail-closed without
    using live Mautic process names as an update gate.
    """
    from mcd_agent.self_update import _lock_path

    path = _lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)


def stable_source_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        config = _config_from_call(args, kwargs)
        with source_operation_lease(config):
            return function(*args, **kwargs)
    return guarded
