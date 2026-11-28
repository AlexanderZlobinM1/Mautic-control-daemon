from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon


class _Store:
    def __init__(self):
        self.values = {}

    def get_runtime_sync(self, key):
        return self.values.get(key)

    def put_runtime_sync(self, key, value):
        self.values[key] = value


def _config(**overrides):
    values = {
        "dispatch_interval_sec": 1.0,
        "empty_leads_cleanup_enabled": True,
        "empty_leads_cleanup_interval_sec": 900,
        "empty_leads_cleanup_batch_size": 5000,
        "empty_leads_cleanup_min_contact_age_days": 7,
        "empty_leads_cleanup_max_runs_per_window": 0,
        "empty_leads_cleanup_quiet_window_min": 660,
        "empty_leads_cleanup_instance_settings": {},
        "enable_page_hits_orphan_cleanup": False,
        "page_hits_orphan_cleanup_instance_settings": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _inst(root):
    return SimpleNamespace(root=root, instance_uid=root.rsplit("/", 1)[-1], db=object())


def _maps():
    return {
        "empty_leads_cleanup_state_cache": {},
        "last_empty_leads_cleanup_ts": {},
        "empty_leads_cleanup_window_counts": {},
        "empty_leads_cleanup_window_keys": {},
        "empty_leads_cleanup_done_window_keys": {},
        "last_page_hits_orphan_cleanup_ts": {},
        "page_hits_orphan_cleanup_active": {},
        "page_hits_orphan_cleanup_window_counts": {},
        "page_hits_orphan_cleanup_window_keys": {},
        "page_hits_orphan_cleanup_done_window_keys": {},
    }


def test_admission_reads_all_live_instances_and_round_robins_without_dispatch_roots():
    store = _Store()
    ring = deque()
    installs = [_inst(f"/var/www/instance-{index}") for index in range(60)]
    live = daemon._maintenance_cleanup_ring_admit(
        _config(),
        installs,
        store,
        ring,
        now=1000.0,
    )

    assert list(live) == [inst.root for inst in installs]
    assert len(ring) == 60
    assert ring[0] == (installs[0].root, "empty_leads_cleanup")
    daemon._maintenance_cleanup_ring_reconcile(
        _config(),
        ring,
        installs,
        completed_item=(installs[0].root, "empty_leads_cleanup"),
    )
    assert ring[0] == (installs[1].root, "empty_leads_cleanup")


def test_ring_survives_restart_and_runtime_sync_does_not_clear_it():
    store = _Store()
    original = deque([("/var/www/a", "empty_leads_cleanup"), ("/var/www/b", "page_hits_orphan_cleanup")])
    daemon._maintenance_cleanup_ring_save(store, original, now_ts=1000.0)

    restored = daemon._maintenance_cleanup_ring_load(store)

    assert list(restored) == list(original)
    assert store.values[daemon._MAINTENANCE_CLEANUP_RING_KEY]["schema"] == daemon._MAINTENANCE_CLEANUP_RING_SCHEMA
    assert store.values[daemon._MAINTENANCE_CLEANUP_RING_KEY]["cursor"] == list(original[0])


def test_admission_publishes_all_enabled_cleanup_tiles_without_due_gate():
    store = _Store()
    ring = deque()
    config = _config(enable_page_hits_orphan_cleanup=True)

    daemon._maintenance_cleanup_ring_admit(
        config,
        [_inst("/var/www/a"), _inst("/var/www/b")],
        store,
        ring,
        now=1.0,
    )

    assert list(ring) == [
        ("/var/www/a", "empty_leads_cleanup"),
        ("/var/www/a", "page_hits_orphan_cleanup"),
        ("/var/www/b", "empty_leads_cleanup"),
        ("/var/www/b", "page_hits_orphan_cleanup"),
    ]


def test_ring_reconcile_requeues_completed_zero_delete_item_and_applies_tile_changes():
    ring = deque([
        ("/var/www/a", "empty_leads_cleanup"),
        ("/var/www/b", "empty_leads_cleanup"),
    ])
    config = _config(
        empty_leads_cleanup_instance_settings={
            "b": {"enabled": False},
            "c": {"enabled": True},
        },
    )

    daemon._maintenance_cleanup_ring_reconcile(
        config,
        ring,
        [_inst("/var/www/a"), _inst("/var/www/b"), _inst("/var/www/c")],
        completed_item=("/var/www/a", "empty_leads_cleanup"),
    )

    assert list(ring) == [
        ("/var/www/c", "empty_leads_cleanup"),
        ("/var/www/a", "empty_leads_cleanup"),
    ]


def test_ring_pause_gate_blocks_only_next_item():
    assert daemon._maintenance_cleanup_ring_paused(False, False) is False
    assert daemon._maintenance_cleanup_ring_paused(True, False) is True
    assert daemon._maintenance_cleanup_ring_paused(False, True) is True


def test_empty_cleanup_ring_receipt_contains_effective_settings_and_requeues_batch():
    store = _Store()
    ring = deque([("/var/www/a", "empty_leads_cleanup")])
    maps = _maps()
    maps["empty_leads_cleanup_state_cache"]["/var/www/a"] = {"finished_at": 900.0, "attempts": 2}
    inst = _inst("/var/www/a")
    config = _config()

    with patch.object(
        daemon,
        "delete_empty_leads_safely",
        return_value={"total_deleted": 3, "cutoff_utc": "2026-11-20 00:00:00"},
    ):
        daemon._run_empty_leads_cleanup_ring_item(
            config,
            store,
            inst,
            ring,
            now=1000.0,
            now_utc=daemon.datetime(2026, 11, 27, tzinfo=daemon.timezone.utc),
            ring_position=0,
            empty_leads_cleanup_state_cache=maps["empty_leads_cleanup_state_cache"],
            last_empty_leads_cleanup_ts=maps["last_empty_leads_cleanup_ts"],
            last_empty_leads_cleanup_idle_ts={},
            empty_leads_cleanup_window_counts=maps["empty_leads_cleanup_window_counts"],
            empty_leads_cleanup_window_keys=maps["empty_leads_cleanup_window_keys"],
            empty_leads_cleanup_done_window_keys=maps["empty_leads_cleanup_done_window_keys"],
        )

    key = daemon._maintenance_cleanup_receipt_key("/var/www/a", "empty_leads_cleanup")
    receipt = store.values[key]
    assert receipt["root"] == "/var/www/a"
    assert receipt["service"] == "empty_leads_cleanup"
    assert receipt["previous_finished_at"] == 900.0
    assert receipt["min_contact_age_days"] == 7
    assert receipt["cutoff_utc"] == "2026-11-20 00:00:00"
    assert receipt["effective_settings"]["batch_size"] == 5000
    assert list(ring) == [("/var/www/a", "empty_leads_cleanup")]


def test_empty_cleanup_zero_delete_is_idle_but_next_due_is_immediate():
    store = _Store()
    ring = deque([("/var/www/a", "empty_leads_cleanup")])
    maps = _maps()
    inst = _inst("/var/www/a")

    with patch.object(
        daemon,
        "delete_empty_leads_safely",
        return_value={"total_deleted": 0, "cutoff_utc": "2026-11-20 00:00:00"},
    ):
        daemon._run_empty_leads_cleanup_ring_item(
            _config(),
            store,
            inst,
            ring,
            now=1000.0,
            now_utc=daemon.datetime(2026, 11, 27, tzinfo=daemon.timezone.utc),
            ring_position=0,
            empty_leads_cleanup_state_cache=maps["empty_leads_cleanup_state_cache"],
            last_empty_leads_cleanup_ts=maps["last_empty_leads_cleanup_ts"],
            last_empty_leads_cleanup_idle_ts={},
            empty_leads_cleanup_window_counts=maps["empty_leads_cleanup_window_counts"],
            empty_leads_cleanup_window_keys=maps["empty_leads_cleanup_window_keys"],
            empty_leads_cleanup_done_window_keys=maps["empty_leads_cleanup_done_window_keys"],
        )

    receipt = store.values[daemon._maintenance_cleanup_receipt_key("/var/www/a", "empty_leads_cleanup")]
    assert receipt["event"] == "idle"
    assert receipt["deleted"] == 0
    assert receipt["next_due"] == 1000.0
    assert list(ring) == [("/var/www/a", "empty_leads_cleanup")]


def test_zero_age_cleanup_passes_no_cutoff_but_receipts_current_cutoff():
    store = _Store()
    ring = deque([("/var/www/a", "empty_leads_cleanup")])
    maps = _maps()
    inst = _inst("/var/www/a")
    config = _config(
        empty_leads_cleanup_min_contact_age_days=7,
        empty_leads_cleanup_instance_settings={"a": {"min_contact_age_days": 0}},
    )

    with patch.object(
        daemon,
        "delete_empty_leads_safely",
        return_value={"total_deleted": 1, "cutoff_utc": None, "age_filter_enabled": False},
    ) as delete:
        daemon._run_empty_leads_cleanup_ring_item(
            config,
            store,
            inst,
            ring,
            now=1000.0,
            now_utc=daemon.datetime(2026, 11, 27, tzinfo=daemon.timezone.utc),
            ring_position=0,
            empty_leads_cleanup_state_cache=maps["empty_leads_cleanup_state_cache"],
            last_empty_leads_cleanup_ts=maps["last_empty_leads_cleanup_ts"],
            last_empty_leads_cleanup_idle_ts={},
            empty_leads_cleanup_window_counts=maps["empty_leads_cleanup_window_counts"],
            empty_leads_cleanup_window_keys=maps["empty_leads_cleanup_window_keys"],
            empty_leads_cleanup_done_window_keys=maps["empty_leads_cleanup_done_window_keys"],
        )

    assert delete.call_args.kwargs["cutoff_utc"] is None
    receipt = store.values[daemon._maintenance_cleanup_receipt_key("/var/www/a", "empty_leads_cleanup")]
    assert receipt["min_contact_age_days"] == 0
    assert receipt["cutoff_utc"] == "2026-11-27 00:00:00"
