from types import SimpleNamespace

from mcd_agent.commercial_portal import CommercialState
from mcd_agent.daemon import _commercial_state_eligibility, _default_commercial_worker_factory, _commercial_worker_allowed


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
    state.put("installation_ids", {"local-1": "inst-1", "local-2": "inst-2"})
    state.put("portal_job_signing_keys", {"portal-1": "key"})
    state.put("portal_mcc_publisher_keys", {"mcc-1": "key"})
    factory = _default_commercial_worker_factory(config)
    worker = factory(None)
    assert worker is not None
    assert worker.installation_ids == ["inst-1", "inst-2"]
    assert state.get("commercial_worker_status")["status"] == "enabled"


def test_default_factory_disables_without_portal_material(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.sqlite")
    state = CommercialState(state_path)
    factory = _default_commercial_worker_factory(SimpleNamespace(state_db_path=state_path))
    assert factory(None) is None
    assert state.get("commercial_worker_status")["status"] == "disabled"
