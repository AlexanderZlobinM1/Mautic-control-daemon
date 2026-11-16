"""Exclude destructive contact cleanup from supervised instance work."""
from contextlib import contextmanager
import fcntl
from pathlib import Path

from mcd_agent.native_comparator import shared_root_fence_path, campaign_root_fence_path


class CleanupBusy(RuntimeError):
    pass


@contextmanager
def cleanup_fence(root: str, lock_directory: str = "/run/lock/mcd-tasks"):
    """Reuse both task fences, without waiting or stopping active workers."""
    handles = []
    try:
        for path in (
            shared_root_fence_path(root, lock_directory),
            campaign_root_fence_path(root, lock_directory),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            handles.append(handle)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CleanupBusy("instance_work_active") from exc
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


def delete_empty_leads_safely(db, root: str, *, mode: str, batch_size: int,
                             lock_directory: str = "/run/lock/mcd-tasks"):
    with cleanup_fence(root, lock_directory):
        return db.delete_empty_leads(mode=mode, batch_size=batch_size, max_batches=1)
