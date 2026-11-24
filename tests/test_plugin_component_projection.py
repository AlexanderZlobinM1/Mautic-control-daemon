from __future__ import annotations

from types import SimpleNamespace

from mcd_agent import cli, state_push


def _status(ready: bool) -> dict[str, object]:
    capability = {
        "schema": "mcd-external-component-capability-v1",
        "identity": "example-component",
        "component": "plugin",
        "operations": [{"operation": "manifest-owned-operation"}],
    }
    return {
        "status": "ready" if ready else "unavailable",
        "reason": "" if ready else "external_component_local_invalid",
        "operations": capability["operations"] if ready else [],
        "capabilities": [capability] if ready else [],
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
    payload = {}
    unavailable = {
        "schema": "mcd-external-component-capability-catalog-v1",
        "components": [_status(False)],
        "capabilities": [],
    }
    state_push._advertise_external_component_capabilities(payload, unavailable)
    assert payload["external_component_capabilities"] == unavailable
    assert "plugin_capabilities" not in payload

    payload = {}
    restored = {
        "schema": "mcd-external-component-capability-catalog-v1",
        "components": [_status(True)],
        "capabilities": _status(True)["capabilities"],
    }
    state_push._advertise_external_component_capabilities(payload, restored)
    assert payload["plugin_capabilities"]["operations"] == [
        {"operation": "manifest-owned-operation"}
    ]

