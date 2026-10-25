from types import SimpleNamespace

from mcd_agent.commercial_portal import CommercialState
from mcd_agent.daemon import _default_commercial_worker_factory, _commercial_worker_allowed


def test_commercial_worker_guard_only_allows_commercial_profile():
    factory = lambda _stop: None
    assert _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="standalone"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="corporate"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=True, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=False, factory=None)


def test_default_factory_validates_material_and_constructs_one_worker(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.sqlite")
    monkeypatch.setattr("mcd_agent.commercial_portal._host_fingerprint", lambda: "sha256:" + "a" * 64)
    state = CommercialState(state_path)
    state.put("portal_agent_credential", "opaque")
    state.put("host_fingerprint", "sha256:" + "a" * 64)
    state.put("portal_api_base", "https://portal.example/api/v1/commercial")
    state.put("agent_id", "agent-1")
    state.put("installation_ids", {"local-1": "inst-1", "local-2": "inst-2"})
    state.put("portal_job_signing_keys", {"portal-1": "key"})
    state.put("portal_mcc_publisher_keys", {"mcc-1": "key"})
    factory = _default_commercial_worker_factory(SimpleNamespace(state_db_path=state_path))
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
