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
    maps = _maps()

    live = daemon._maintenance_cleanup_ring_admit(
        _config(),
        installs,
        store,
        ring,
        now=1000.0,
        cluster_cron_allowed=True,
        **maps,
    )

    assert list(live) == [inst.root for inst in installs]
    assert len(ring) == 60
    assert ring[0] == (installs[0].root, "empty_leads_cleanup")
    assert daemon._service_cleanup_ring_select(
        ring,
        installs[0].root,
        ["empty_leads_cleanup"],
    ) == "empty_leads_cleanup"
    assert ring[0] == (installs[1].root, "empty_leads_cleanup")


def test_ring_survives_restart_and_runtime_sync_does_not_clear_it():
    store = _Store()
    original = deque([("/var/www/a", "empty_leads_cleanup"), ("/var/www/b", "page_hits_orphan_cleanup")])
    daemon._maintenance_cleanup_ring_save(store, original, now_ts=1000.0)

    restored = daemon._maintenance_cleanup_ring_load(store)

    assert list(restored) == list(original)
    assert store.values[daemon._MAINTENANCE_CLEANUP_RING_KEY]["schema"] == daemon._MAINTENANCE_CLEANUP_RING_SCHEMA


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
