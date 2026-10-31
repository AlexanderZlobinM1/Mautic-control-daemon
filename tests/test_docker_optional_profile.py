from __future__ import annotations

from mcd_agent import service_profiles


def test_retained_docker_reference_is_only_planned_on_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(
        service_profiles,
        "_docker_runtime_state",
        lambda: {
            "installed": False,
            "path": "",
            "version": "",
            "active": False,
            "daemon_reachable": False,
        },
    )

    result = service_profiles.apply_docker_profile(dry_run=True)

    assert result["status"] == "planned"
    assert ["apt-get", "install", "-y", "docker.io"] in result["commands"]


def test_retained_docker_reference_noops_when_daemon_is_ready(monkeypatch) -> None:
    state = {
        "installed": True,
        "path": "/usr/bin/docker",
        "version": "Docker version 27.0.0",
        "active": True,
        "daemon_reachable": True,
    }
    monkeypatch.setattr(service_profiles, "_docker_runtime_state", lambda: state)

    result = service_profiles.apply_docker_profile(dry_run=False)

    assert result["status"] == "noop"

