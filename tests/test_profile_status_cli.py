from __future__ import annotations

import json
from types import SimpleNamespace
import sys

from mcd_agent import cli
from mcd_agent.mode import ModeResult


def test_profile_status_json_publishes_selection_path(monkeypatch, tmp_path, capsys):
    selection_path = tmp_path / "var" / "hardware-profile-selection.json"
    selection_path.parent.mkdir()
    selection_path.write_text('{"mode":"manual","profile":"farm-hiload"}\n', encoding="utf-8")
    cfg = SimpleNamespace(profile_name="farm-hiload", scheduler_pause_flag_path=str(tmp_path / "pause"))

    monkeypatch.setattr(sys, "argv", [
        "mcd-cli", "profile", "status", "--config", str(tmp_path / "mcd.toml"), "--json"
    ])
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "maybe_notify_update", lambda _cfg: None)
    monkeypatch.setattr(cli, "profile_status", lambda **_kwargs: ModeResult(ok=True, lines=[]))
    monkeypatch.setattr(cli, "profile_selection_path", lambda _install: selection_path)
    monkeypatch.setattr(cli, "read_profile_selection", lambda _install: {
        "mode": "manual", "profile": "farm-hiload"
    })
    monkeypatch.setattr(cli, "read_memory_kib", lambda: 64 * 1024 * 1024)
    monkeypatch.setattr(cli.os, "cpu_count", lambda: 12)

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mcd-profile-status-v1"
    assert payload["selection_path"] == str(selection_path)
    assert payload["selection_exists"] is True
    assert payload["selection_mode"] == "manual"

