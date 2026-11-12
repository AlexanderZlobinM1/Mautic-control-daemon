from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import cli


def test_cli_refreshes_materialized_catalog_before_operation_lookup(capsys) -> None:
    stale = SimpleNamespace(
        config_file_path="/etc/mcd/mcd.toml",
        plugin_operations={},
        plugin_operation_instance_settings={},
    )
    fresh = SimpleNamespace(
        config_file_path="/etc/mcd/mcd.toml",
        plugin_operations={"app.example": [{"operation_key": "warmupmail:update"}]},
        plugin_operation_instance_settings={},
    )
    inst = SimpleNamespace(instance_uid="app.example")
    args = SimpleNamespace(
        root="app.example",
        plugin_ops_cmd="list",
        prefix="",
        json=True,
        operation_key="",
        set=[],
    )
    operation = {
        "operation_key": "warmupmail:update",
        "bundle": "MauticWarmupMailBundle",
        "operation": {"id": "warmupmail:update", "mode": "scheduled"},
    }
    response = {
        "status": "ok",
        "runtime_overrides": {"plugin_operations": fresh.plugin_operations},
    }
    applied = {
        "config": fresh,
        "applied_keys": ["plugin_operations"],
        "unsupported_keys": [],
        "blocked_keys": [],
    }

    with patch.object(cli, "_select_instance_for_ops", return_value=inst), patch.object(
        cli, "fetch_runtime_overrides", return_value=response
    ) as fetch, patch.object(
        cli, "apply_remote_overrides", return_value=applied
    ), patch.object(
        cli,
        "operations_for_instance",
        side_effect=lambda cfg, selected: [operation] if cfg is fresh else [],
    ), patch.object(
        cli, "effective_values", return_value={"enabled": True, "interval_sec": 60}
    ):
        rc = cli._plugin_operation_cli(stale, args)

    assert rc == 0
    assert fetch.call_args.kwargs["instance_uids"] == ["app.example"]
    assert json.loads(capsys.readouterr().out) == [
        {
            "operation_key": "warmupmail:update",
            "bundle": "MauticWarmupMailBundle",
            "operation": {"id": "warmupmail:update", "mode": "scheduled"},
            "values": {"enabled": True, "interval_sec": 60},
        }
    ]
