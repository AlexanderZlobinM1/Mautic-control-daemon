import pytest
from collections import deque
from threading import Event, Thread
from types import SimpleNamespace

from mcd_agent.daemon import (
    _empty_leads_cleanup_pending_due,
    _maintenance_cleanup_ring_reconcile,
    _empty_leads_cleanup_state_load,
    _empty_leads_cleanup_state_save,
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
        result = delete_empty_leads_safely(
            db,
            "/var/www/one",
            mode="both_null",
            batch_size=5000,
            cutoff_utc="2026-11-20 00:00:00",
        )
    assert result["lock_scope"] == "database_advisory_batch"
    assert db.deleted == [{
        "mode": "both_null",
        "batch_size": 5000,
        "max_batches": 1,
        "cutoff_utc": "2026-11-20 00:00:00",
    }]
    assert db.released == [db.connection]


def test_cleanup_deletes_before_long_segment_session_finishes(monkeypatch):
    db = FakeDB()
    segment_started = Event()
    segment_finished = Event()
    cleanup_finished = Event()
    segment_release = Event()
    result = {}

    def forbidden_root_fence(*_args, **_kwargs):
        raise AssertionError("cleanup must not acquire the segment root fence")

    monkeypatch.setattr("mcd_agent.empty_leads_cleanup.cleanup_fence", forbidden_root_fence)

    def long_segment_update():
        segment_started.set()
        segment_release.wait(timeout=2)
        segment_finished.set()

    def cleanup_batch():
        result["value"] = delete_empty_leads_safely(
            db,
            "/var/www/ogistra",
            mode="both_null",
            batch_size=5000,
            cutoff_utc="2026-11-20 00:00:00",
        )
        cleanup_finished.set()

    segment_thread = Thread(target=long_segment_update)
    cleanup_thread = Thread(target=cleanup_batch)
    segment_thread.start()
    assert segment_started.wait(timeout=1)
    cleanup_thread.start()
    assert cleanup_finished.wait(timeout=1)
    assert not segment_finished.is_set()
    assert result["value"]["total_deleted"] == 1
    segment_release.set()
    segment_thread.join(timeout=2)
    cleanup_thread.join(timeout=2)
    assert segment_finished.is_set()


def test_busy_db_lock_defers_without_delete():
    db = FakeDB(connection=None)
    with pytest.raises(CleanupBusy, match="cleanup_db_lock_busy"):
        delete_empty_leads_safely(
            db,
            "/var/www/one",
            mode="both_null",
            batch_size=5000,
            cutoff_utc="2026-11-20 00:00:00",
        )
    assert db.deleted == []
    assert db.released == []


def test_db_lock_is_released_after_delete_failure():
    db = FakeDB()
    db.delete_empty_leads = lambda **_: (_ for _ in ()).throw(RuntimeError("sql failure"))
    with pytest.raises(RuntimeError, match="sql failure"):
        delete_empty_leads_safely(
            db,
            "/var/www/one",
            mode="both_null",
            batch_size=5000,
            cutoff_utc="2026-11-20 00:00:00",
        )
    assert db.released == [db.connection]


def test_missing_lock_api_fails_closed():
    with pytest.raises(CleanupBusy, match="cleanup_db_lock_unavailable"):
        delete_empty_leads_safely(
            object(),
            "/var/www/one",
            mode="both_null",
            batch_size=5000,
            cutoff_utc="2026-11-20 00:00:00",
        )


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


def test_service_ring_rebuilds_after_completion_from_current_tiles():
    config = SimpleNamespace(
        empty_leads_cleanup_enabled=True,
        empty_leads_cleanup_interval_sec=900,
        empty_leads_cleanup_batch_size=5000,
        empty_leads_cleanup_min_contact_age_days=7,
        empty_leads_cleanup_max_runs_per_window=0,
        empty_leads_cleanup_quiet_window_min=660,
        empty_leads_cleanup_instance_settings={},
        enable_page_hits_orphan_cleanup=True,
        page_hits_orphan_cleanup_interval_sec=3600,
        page_hits_orphan_cleanup_quiet_hour=2,
        page_hits_orphan_cleanup_quiet_window_min=180,
        page_hits_orphan_cleanup_batch_size=5000,
        page_hits_orphan_cleanup_batches_per_run=12,
        page_hits_orphan_cleanup_sleep_sec=0.2,
        page_hits_orphan_cleanup_grace_min=1440,
        page_hits_orphan_cleanup_max_run_sec=3600,
        page_hits_orphan_cleanup_instance_settings={},
    )
    instance_a = SimpleNamespace(root="instance-a", db=object())
    instance_b = SimpleNamespace(root="instance-b", db=object())
    ring = deque([("instance-a", "empty_leads_cleanup")])

    _maintenance_cleanup_ring_reconcile(
        config,
        ring,
        [instance_a, instance_b],
        completed_item=("instance-a", "empty_leads_cleanup"),
    )

    assert list(ring) == [
        ("instance-a", "page_hits_orphan_cleanup"),
        ("instance-b", "empty_leads_cleanup"),
        ("instance-b", "page_hits_orphan_cleanup"),
        ("instance-a", "empty_leads_cleanup"),
    ]


def test_unconfigured_service_is_skipped_without_ring_admission():
    config = SimpleNamespace(
        empty_leads_cleanup_enabled=False,
        empty_leads_cleanup_instance_settings={},
        enable_page_hits_orphan_cleanup=False,
        page_hits_orphan_cleanup_instance_settings={},
    )
    ring = deque()
    _maintenance_cleanup_ring_reconcile(config, ring, [SimpleNamespace(root="instance-a", db=object())])
    assert list(ring) == []
