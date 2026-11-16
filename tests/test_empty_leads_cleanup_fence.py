import fcntl
import sqlite3
from unittest.mock import Mock

import pytest

from mcd_agent.empty_leads_cleanup import CleanupBusy, cleanup_fence, delete_empty_leads_safely
from mcd_agent.native_comparator import shared_root_fence_path, campaign_root_fence_path


@pytest.mark.parametrize("path_for", [shared_root_fence_path, campaign_root_fence_path])
def test_active_work_defers_delete_and_other_instance_stays_independent(tmp_path, path_for):
    root = "/var/www/one"
    locks = str(tmp_path)
    active = path_for(root, locks).open("a+b")
    fcntl.flock(active, fcntl.LOCK_SH | fcntl.LOCK_NB)
    db = Mock()
    try:
        with pytest.raises(CleanupBusy):
            delete_empty_leads_safely(db, root, mode="both_null", batch_size=5000,
                                     lock_directory=locks)
        db.delete_empty_leads.assert_not_called()
        with cleanup_fence("/var/www/two", locks):
            pass
    finally:
        active.close()
    delete_empty_leads_safely(db, root, mode="both_null", batch_size=5000,
                             lock_directory=locks)
    db.delete_empty_leads.assert_called_once_with(mode="both_null", batch_size=5000, max_batches=1)


def test_cleanup_excludes_both_task_families_and_releases_after_failure(tmp_path):
    locks = str(tmp_path)
    root = "/var/www/one"
    with pytest.raises(RuntimeError, match="sql failure"):
        with cleanup_fence(root, locks):
            for path_for in (shared_root_fence_path, campaign_root_fence_path):
                with path_for(root, locks).open("a+b") as contender:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
            raise RuntimeError("sql failure")
    with cleanup_fence(root, locks):
        pass


@pytest.mark.parametrize("path_for", [shared_root_fence_path, campaign_root_fence_path])
def test_contact_read_insert_sequence_keeps_fk_parent_until_work_finishes(tmp_path, path_for):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("CREATE TABLE leads(id INTEGER PRIMARY KEY);"
                       "CREATE TABLE members(lead_id INTEGER REFERENCES leads(id));"
                       "INSERT INTO leads VALUES(1);")
    root = "/var/www/one"
    locks = str(tmp_path)
    db = Mock()
    db.delete_empty_leads.side_effect = lambda **_: conn.execute("DELETE FROM leads WHERE id=1")
    try:
        with path_for(root, locks).open("a+b") as active:
            fcntl.flock(active, fcntl.LOCK_SH | fcntl.LOCK_NB)
            lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
            with pytest.raises(CleanupBusy):
                delete_empty_leads_safely(db, root, mode="both_null", batch_size=5000,
                                         lock_directory=locks)
            conn.execute("INSERT INTO members VALUES(?)", (lead_id,))
            assert conn.execute("SELECT count(*) FROM members").fetchone()[0] == 1
            db.delete_empty_leads.assert_not_called()
    finally:
        conn.close()
