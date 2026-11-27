import pytest
from collections import deque

from mcd_agent.daemon import (
    _empty_leads_cleanup_pending_due,
    _empty_leads_cleanup_state_load,
    _empty_leads_cleanup_state_save,
    _service_cleanup_ring_select,
)
from mcd_agent.empty_leads_cleanup import CleanupBusy, cleanup_fence, delete_empty_leads_safely


class FakeDB:
    def __init__(self, connection=object()):
        self.connection = connection
        self.deleted = []
        self.released = []

    def try_acquire_empty_leads_cleanup_lock(self):
        return self.connection

    def release_empty_leads_cleanup_lock(self, connection):
        self.released.append(connection)

    def delete_empty_leads(self, **kwargs):
        self.deleted.append(kwargs)
        return {"total_deleted": 1, "batches_run": 1}


def test_cleanup_uses_short_db_lock_even_when_instance_root_work_is_active():
    db = FakeDB()
    with cleanup_fence("/var/www/one", "/unused"):
        result = delete_empty_leads_safely(db, "/var/www/one", mode="both_null", batch_size=5000)
    assert result["lock_scope"] == "database_advisory_batch"
    assert db.deleted == [{"mode": "both_null", "batch_size": 5000, "max_batches": 1}]
    assert db.released == [db.connection]


def test_busy_db_lock_defers_without_delete():
    db = FakeDB(connection=None)
    with pytest.raises(CleanupBusy, match="cleanup_db_lock_busy"):
        delete_empty_leads_safely(db, "/var/www/one", mode="both_null", batch_size=5000)
    assert db.deleted == []
    assert db.released == []


def test_db_lock_is_released_after_delete_failure():
    db = FakeDB()
    db.delete_empty_leads = lambda **_: (_ for _ in ()).throw(RuntimeError("sql failure"))
    with pytest.raises(RuntimeError, match="sql failure"):
        delete_empty_leads_safely(db, "/var/www/one", mode="both_null", batch_size=5000)
    assert db.released == [db.connection]


def test_missing_lock_api_fails_closed():
    with pytest.raises(CleanupBusy, match="cleanup_db_lock_unavailable"):
        delete_empty_leads_safely(object(), "/var/www/one", mode="both_null", batch_size=5000)


class FakeStore:
    def __init__(self):
        self.values = {}

    def get_runtime_sync(self, key):
        return self.values.get(key)

    def put_runtime_sync(self, key, payload):
        self.values[key] = dict(payload)


def test_deferred_cleanup_is_durable_and_due_on_next_retry():
    store = FakeStore()
    cache = {}
    root = "/var/www/ogistra"
    state = _empty_leads_cleanup_state_save(
        store,
        cache,
        root,
        status="pending",
        now_ts=100.0,
        next_retry_at=101.0,
        pending_since=100.0,
        attempts=1,
        receipt={"event": "deferred", "reason": "cleanup_db_lock_busy"},
    )
    assert state["receipt"]["event"] == "deferred"
    restored = _empty_leads_cleanup_state_load(store, root)
    assert restored["status"] == "pending"
    assert not _empty_leads_cleanup_pending_due(restored, 100.5)
    assert _empty_leads_cleanup_pending_due(restored, 101.0)


def test_service_ring_is_one_worker_and_instance_order_is_contact_then_orphan():
    ring = deque([
        ("instance-a", "empty_leads_cleanup"),
        ("instance-a", "page_hits_orphan_cleanup"),
        ("instance-b", "empty_leads_cleanup"),
        ("instance-b", "page_hits_orphan_cleanup"),
    ])
    assert _service_cleanup_ring_select(ring, "instance-a", ["empty_leads_cleanup", "page_hits_orphan_cleanup"]) == "empty_leads_cleanup"
    ring.append(("instance-a", "page_hits_orphan_cleanup"))
    assert _service_cleanup_ring_select(ring, "instance-a", ["empty_leads_cleanup", "page_hits_orphan_cleanup"]) == "page_hits_orphan_cleanup"
    ring.append(("instance-b", "empty_leads_cleanup"))
    assert _service_cleanup_ring_select(ring, "instance-b", ["empty_leads_cleanup", "page_hits_orphan_cleanup"]) == "empty_leads_cleanup"


def test_unconfigured_service_is_skipped_without_ring_admission():
    ring = deque()
    assert _service_cleanup_ring_select(ring, "instance-a", []) == ""
    assert list(ring) == []
