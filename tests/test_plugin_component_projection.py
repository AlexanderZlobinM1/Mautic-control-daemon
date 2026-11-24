from __future__ import annotations

from types import SimpleNamespace

from mcd_agent import cli, plugins, state_push


def _status(ready: bool) -> dict[str, object]:
    return {
        "status": "ready" if ready else "unavailable",
        "reason": "" if ready else "external_component_local_invalid",
        "capabilities": list(plugins._PLUGIN_FILESYSTEM_REQUIRED_OPERATIONS) if ready else [],
    }


def test_interactive_menu_hides_plugins_when_component_is_unavailable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_pick_active_instance_interactive", lambda _cfg, _root: "/srv/instance")
    monkeypatch.setattr(cli, "plugin_filesystem_component_preflight", lambda *_a, **_k: _status(False))
    answers = iter(["0"])
    monkeypatch.setattr(cli, "_ask", lambda _prompt: next(answers))

    assert cli._run_interactive_hub(SimpleNamespace(), None, True) == 0
    output = capsys.readouterr().out
    assert "1. Plugins" not in output
    assert "Plugins unavailable: external_component_local_invalid" in output


def test_machine_capability_catalog_tracks_component_validity(monkeypatch) -> None:
    monkeypatch.setattr(plugins, "plugin_filesystem_component_preflight", lambda *_a, **_k: _status(False))
    unavailable = state_push.plugin_component_capability_projection(SimpleNamespace())
    assert unavailable["capability"] is None
    assert unavailable["status"]["capabilities"] == []

    monkeypatch.setattr(plugins, "plugin_filesystem_component_preflight", lambda *_a, **_k: _status(True))
    restored = state_push.plugin_component_capability_projection(SimpleNamespace())
    assert restored["capability"]["features"] == [
        "mcd-plugin-job-v1",
        "mcd-plugin-auto-update-policy-v1",
        "mcd-plugin-catalog-digest-v1",
    ]
    assert restored["status"]["status"] == "ready"

