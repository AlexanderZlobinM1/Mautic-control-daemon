from types import SimpleNamespace

from mcd_agent.daemon import _commercial_worker_allowed


def test_commercial_worker_guard_only_allows_commercial_profile():
    factory = lambda _stop: None
    assert _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="standalone"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="corporate"), single_cycle=False, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=True, factory=factory)
    assert not _commercial_worker_allowed(SimpleNamespace(profile_name="commercial"), single_cycle=False, factory=None)
