from copy import deepcopy

from mcd_agent import daemon


class Store:
    def __init__(self):
        self.values = {"local_runtime": {"campaign_limit": 1, "jobs_max_workers": 2}}

    def get_runtime_sync(self, key):
        return deepcopy(self.values.get(key))

    def put_runtime_sync(self, key, value):
        self.values[key] = deepcopy(value)


def test_unchanged_pending_keeps_original_event_clock_across_poll_and_restart(monkeypatch):
    store = Store()
    clock = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    current = {"campaign_limit": 3, "jobs_max_workers": 2}
    pending = daemon._queue_local_runtime_delta(store, current)
    original = deepcopy(store.values[daemon._LOCAL_RUNTIME_METADATA_KEY])
    clock[0] = 5000.0
    restarted = Store()
    restarted.values = deepcopy(store.values)
    assert daemon._queue_local_runtime_delta(restarted, current) == pending
    assert restarted.values[daemon._LOCAL_RUNTIME_METADATA_KEY] == original
    local_stamp = daemon._runtime_metadata_get(original, "host", "campaign_limit", "")
    assert daemon._runtime_stamp_wins(
        {"updated_at": 150.0, "revision": 217, "source": "mcc", "event_id": "mcc:217"},
        local_stamp)


def test_second_local_edit_does_not_retimestamp_first_pending_field(monkeypatch):
    store = Store()
    clock = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    daemon._queue_local_runtime_delta(store, {"campaign_limit": 3, "jobs_max_workers": 2})
    first = deepcopy(store.values[daemon._LOCAL_RUNTIME_METADATA_KEY])
    first_stamp = daemon._runtime_metadata_get(first, "host", "campaign_limit", "")
    clock[0] = 200.0
    pending = daemon._queue_local_runtime_delta(store, {"campaign_limit": 3, "jobs_max_workers": 4})
    latest = store.values[daemon._LOCAL_RUNTIME_METADATA_KEY]
    assert daemon._runtime_metadata_get(latest, "host", "campaign_limit", "") == first_stamp
    second_stamp = daemon._runtime_metadata_get(latest, "host", "jobs_max_workers", "")
    assert second_stamp["updated_at"] > first_stamp["updated_at"]
    assert pending["previous"] == {"campaign_limit": 1, "jobs_max_workers": 2}
    assert pending["current"] == {"campaign_limit": 3, "jobs_max_workers": 4}
