import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import cli


def test_contract_profile_set_pushes_reloaded_effective_config(monkeypatch, tmp_path, capsys):
    payload_path = tmp_path / "profile.json"
    payload_path.write_text('{"schema":"mcd-backup-profile-v1"}', encoding="utf-8")
    initial = SimpleNamespace()
    reloaded = SimpleNamespace()
    monkeypatch.setattr(sys, "argv", [
        "mcd-cli", "backup", "--config", str(tmp_path / "mcd.toml"),
        "profile-set", "--profile-json-file", str(payload_path),
        "--contract", "mcd-backup-profile-v1",
    ])
    with patch.object(cli, "load_config", side_effect=[initial, reloaded]) as load, patch.object(
        cli, "maybe_notify_update", return_value=None
    ), patch.object(cli, "backup_profile_apply_sync", return_value=(reloaded, {"status": "ok"})) as apply, patch.object(
        cli, "_push_state_after_change"
    ) as push:
        assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    apply.assert_called_once()
    assert load.call_count == 2
    push.assert_called_once_with(reloaded, "backup-profile-set-contract")
