from types import SimpleNamespace
import sys

from mcd_agent.commercial_portal import CommercialState
from mcd_agent.daemon import (
    _commercial_registration_descriptor,
    _commercial_state_eligibility,
    _default_commercial_worker_factory,
    _commercial_worker_allowed,
    _reconcile_commercial_worker,
)


def _eligible_state(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.sqlite")
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: "sha256:" + "a" * 64)
    state = CommercialState(state_path)
    state.put("portal_agent_credential", "opaque")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("agent_id", "agent-1")
    state.put("commercial_key_id", "commercial-key-1")
    state.put("installation_ids", {"local-1": "inst-1"})
    return state, SimpleNamespace(state_db_path=state_path)


def test_commercial_worker_guard_uses_persistent_state_not_base_profile(tmp_path, monkeypatch):
    _state, config = _eligible_state(tmp_path, monkeypatch)
    factory = lambda _stop: None
    config.mcc_url = ""
    config.mcc_token = ""
    for profile in ("passive", "standalone", "corporate", "commercial"):
        config.profile_name = profile
        assert _commercial_worker_allowed(config, single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(config, single_cycle=True, factory=factory)
    assert not _commercial_worker_allowed(config, single_cycle=False, factory=None)


def test_commercial_worker_fails_closed_without_persistent_state(tmp_path):
    config = SimpleNamespace(profile_name="commercial", state_db_path=str(tmp_path / "missing.sqlite"))
    assert _commercial_state_eligibility(config) == (False, "commercial_state_not_enabled")
    assert not _commercial_worker_allowed(config, single_cycle=False, factory=lambda _stop: None)


def test_commercial_worker_fails_closed_for_incomplete_binding(tmp_path, monkeypatch):
    state, config = _eligible_state(tmp_path, monkeypatch)
    for key, value in (
        ("portal_api_base", ""),
        ("commercial_key_id", ""),
        ("agent_id", ""),
        ("host_fingerprint", "sha256:" + "b" * 64),
        ("installation_ids", {}),
    ):
        state.put("portal_api_base", "https://portal.example/api/v1/commercial")
        state.put("commercial_key_id", "commercial-key-1")
        state.put("agent_id", "agent-1")
        state.put("host_fingerprint", "sha256:" + "a" * 64)
        state.put("installation_ids", {"local-1": "inst-1"})
        state.put(key, value)
        assert not _commercial_worker_allowed(config, single_cycle=False, factory=lambda _stop: None)


def test_commercial_worker_accepts_key_id_from_durable_key_metadata(tmp_path, monkeypatch):
    state, config = _eligible_state(tmp_path, monkeypatch)
    state.put("commercial_key_id", None)
    state.put("commercial_key_metadata", {"commercial_key_id": "commercial-key-from-metadata"})
    assert _commercial_state_eligibility(config) == (True, "")


def test_revoke_or_detach_fails_closed_without_touching_base_profile(tmp_path, monkeypatch):
    state, config = _eligible_state(tmp_path, monkeypatch)
    config.profile_name = "passive"
    config.cron_enabled = True
    for key, value in (("commercial_revoked", True), ("commercial_detached", True), ("commercial_portal_connection_state", "detached")):
        state.put("commercial_revoked", False)
        state.put("commercial_detached", False)
        state.put("commercial_portal_connection_state", "active")
        state.put(key, value)
        assert _commercial_state_eligibility(config) == (False, "commercial_state_revoked")
        assert not _commercial_worker_allowed(config, single_cycle=False, factory=lambda _stop: None)
        assert config.profile_name == "passive"
        assert config.cron_enabled is True


def test_registered_installation_survives_restart_without_activation(tmp_path, monkeypatch):
    state, config = _eligible_state(tmp_path, monkeypatch)
    state.put("portal_registration_id", "registration-1")
    state.put("commercial_portal_connection_state", "active")
    config.profile_name = "standalone"
    first = _commercial_state_eligibility(config)
    restarted_config = SimpleNamespace(state_db_path=config.state_db_path, profile_name="standalone")
    second = _commercial_state_eligibility(restarted_config)
    assert first == second == (True, "")
    assert state.get("portal_registration_id") == "registration-1"
    assert state.get("portal_activation_code") is None


def test_default_factory_validates_material_and_constructs_one_worker(tmp_path, monkeypatch):
    state, config = _eligible_state(tmp_path, monkeypatch)
    monkeypatch.setattr("mcd_agent.daemon.default_scenario_runner_argv", lambda: [sys.executable])
    state.put("installation_ids", {"local-1": "inst-1", "local-2": "inst-2"})
    state.put("portal_job_signing_keys", {"portal-1": "key"})
    factory = _default_commercial_worker_factory(config)
    worker = factory(None)
    assert worker is not None
    assert worker.installation_ids == ["inst-1", "inst-2"]
    assert worker.publisher_keys == {}
    assert state.get("commercial_worker_status")["status"] == "enabled"


def test_default_factory_disables_without_portal_material(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.sqlite")
    state = CommercialState(state_path)
    factory = _default_commercial_worker_factory(SimpleNamespace(state_db_path=state_path))
    assert factory(None) is None
    assert state.get("commercial_worker_status")["status"] == "disabled"


def test_native_rescan_uses_current_local_config_and_preserves_manual_instances(tmp_path, monkeypatch):
    from mcd_agent.inventory import InstanceInventory
    from mcd_agent.models import MauticInstall

    _state, config = _eligible_state(tmp_path, monkeypatch)
    config.config_file_path = str(tmp_path / "local.toml")
    current = SimpleNamespace(
        state_db_path=config.state_db_path, discovery_roots=[str(tmp_path / "current-root")],
        exclude_path_contains=[], supported_mautic_majors=[7], custom_instances=[],
    )
    loads = []

    def load(path, *, allow_recover_from_mcc):
        loads.append((path, allow_recover_from_mcc))
        return current

    monkeypatch.setattr("mcd_agent.daemon.load_config", load)
    monkeypatch.setattr("mcd_agent.daemon.default_scenario_runner_argv", lambda: [sys.executable])
    native = InstanceInventory(config.state_db_path)
    manual = MauticInstall("manual", "manual", str(tmp_path / "manual"), "/manual/bin/console", source="manual")
    discovered = MauticInstall("new", "new", str(tmp_path / "current-root"), "/new/bin/console", mautic_major=7)
    native._upsert_install(manual)
    native.conn.commit()
    discoveries = []

    def discover(roots, *args):
        discoveries.append(roots)
        return [discovered]

    monkeypatch.setattr("mcd_agent.inventory.discover_mautic", discover)
    worker = _default_commercial_worker_factory(config)(None)
    assert worker is not None
    result = worker.generic_command_handler({"command": {"command_id": "rescan_inventory"}, "input": {}})
    assert result == {"status": "succeeded", "discovered_count": 1}
    assert loads == [(config.config_file_path, False)]
    assert discoveries == [current.discovery_roots]
    assert {(row.instance_uid, row.source) for row in native.list_instances()} == {
        ("manual", "manual"), ("new", "autodiscovery"),
    }
    native.conn.close()


def test_commercial_worker_retries_after_activation_material_is_temporarily_incomplete(monkeypatch):
    class Worker:
        def run_forever(self, stop_event, **_kwargs):
            stop_event.wait(0.05)

    monkeypatch.setattr("mcd_agent.daemon.SelfHostedPortalWorker", Worker)
    calls = []

    def factory(_stop_event):
        calls.append(True)
        return None if len(calls) == 1 else Worker()

    import threading

    stop_event = threading.Event()
    thread, stop_event, retry_after = _reconcile_commercial_worker(
        SimpleNamespace(poll_interval_sec=1),
        single_cycle=False,
        eligible=True,
        reason="",
        worker_factory=factory,
        worker_thread=None,
        stop_event=stop_event,
        retry_after=0.0,
        now=100.0,
    )
    assert thread is None
    assert retry_after > 100.0

    thread, stop_event, retry_after = _reconcile_commercial_worker(
        SimpleNamespace(poll_interval_sec=1),
        single_cycle=False,
        eligible=True,
        reason="",
        worker_factory=factory,
        worker_thread=thread,
        stop_event=stop_event,
        retry_after=retry_after,
        now=retry_after,
    )
    assert thread is not None
    assert retry_after == 0.0
    assert calls == [True, True]
    stop_event.set()
    thread.join(timeout=1.0)


def test_portal_registration_identity_wins_over_host_derived_inventory_identity(tmp_path, monkeypatch):
    state, _config = _eligible_state(tmp_path, monkeypatch)
    state.put(
        "commercial_installation_metadata",
        {
            "mautic.zlobin.com": {
                "local_identity": "mautic.zlobin.com",
                "display_name": "Mautic Zlobin",
                "mautic_version": "7.2",
            }
        },
    )
    descriptor = _commercial_registration_descriptor(state, {"mautic.zlobin.com": "inst-1"}, "inst-1")
    assert descriptor == {
        "local_identity": "mautic.zlobin.com",
        "display_name": "Mautic Zlobin",
        "mautic_version": "7.2",
    }


def test_pending_portal_identity_precedes_stale_metadata(tmp_path, monkeypatch):
    state, _config = _eligible_state(tmp_path, monkeypatch)
    state.put(
        "commercial_installation_metadata",
        {"cluster1.mail.m1.rs": {"local_identity": "cluster1.mail.m1.rs", "display_name": "Cluster 1"}},
    )
    state.put(
        "commercial_pending_activation",
        {
            "registration_id": "inst-1",
            "local_identity": "mautic.zlobin.com",
            "display_name": "Mautic Zlobin",
            "mautic_version": "7.2",
        },
    )
    descriptor = _commercial_registration_descriptor(state, {"cluster1.mail.m1.rs": "inst-1"}, "inst-1")
    assert descriptor["local_identity"] == "mautic.zlobin.com"
    assert descriptor["display_name"] == "Mautic Zlobin"
