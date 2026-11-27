"""Run bounded empty-contact cleanup in its own maintenance lane."""
from contextlib import contextmanager


class CleanupBusy(RuntimeError):
    """The short database maintenance lock is currently held elsewhere."""


@contextmanager
def cleanup_fence(root: str, lock_directory: str = "/run/lock/mcd-tasks"):
    """Compatibility boundary for callers of the former root-fence API.

    Empty-contact cleanup must not acquire the instance scheduler fences: a
    long segment/campaign session may legitimately hold those for its whole
    batch. The actual mutual exclusion is the short database advisory lock
    taken by :func:`delete_empty_leads_safely`.
    """
    del root, lock_directory
    yield


def delete_empty_leads_safely(db, root: str, *, mode: str, batch_size: int, cutoff_utc: str,
                             lock_directory: str = "/run/lock/mcd-tasks"):
    """Delete at most one batch under a per-database advisory lock.

    ``root`` and ``lock_directory`` remain accepted for compatibility with
    existing operation callers, but are intentionally not used for fencing.
    The DB lock is held only for this bounded SQL batch, so cleanup can make
    progress while a long scheduler session continues and concurrent cleanup
    attempts still serialize safely.
    """
    del root, lock_directory
    acquire = getattr(db, "try_acquire_empty_leads_cleanup_lock", None)
    release = getattr(db, "release_empty_leads_cleanup_lock", None)
    if not callable(acquire) or not callable(release):
        raise CleanupBusy("cleanup_db_lock_unavailable")
    connection = acquire()
    if connection is None:
        raise CleanupBusy("cleanup_db_lock_busy")
    try:
        result = db.delete_empty_leads(
            mode=mode,
            batch_size=batch_size,
            max_batches=1,
            cutoff_utc=cutoff_utc,
        )
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("lock_scope", "database_advisory_batch")
        return result
    finally:
        release(connection)
