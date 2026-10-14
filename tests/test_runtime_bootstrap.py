from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_bridge():
    path = Path(__file__).parents[1] / "tools" / "mcd_runtime_bootstrap.py"
    spec = importlib.util.spec_from_file_location("mcd_runtime_bootstrap_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scheduler_status_uses_package_entrypoint_and_parses_json(tmp_path: Path) -> None:
    bridge = _load_bridge()
    config = tmp_path / "mcd.toml"
    config.write_text("[runtime]\n", encoding="utf-8")
    expected = {"paused": False, "running_tasks": 1, "tracked_running_tasks": 1}
    completed = SimpleNamespace(returncode=0, stdout='{"paused": false, "running_tasks": 1, "tracked_running_tasks": 1}\n', stderr="")

    with patch.object(bridge, "_venv_python", return_value=tmp_path / "venv" / "bin" / "python"), patch.object(
        bridge, "_run", return_value=completed
    ) as run:
        observed = bridge._scheduler_status(tmp_path, config)

    assert observed == expected
    run.assert_called_once_with(
        [
            str(tmp_path / "venv" / "bin" / "python"),
            "-m",
            "mcd_agent",
            "scheduler",
            "--config",
            str(config),
            "status",
            "--verbose",
            "--json",
        ],
        cwd=tmp_path,
        check=False,
    )


def test_scheduler_status_does_not_call_missing_cli_module_dispatch(tmp_path: Path) -> None:
    bridge = _load_bridge()
    config = tmp_path / "mcd.toml"
    config.write_text("[runtime]\n", encoding="utf-8")
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(bridge, "_venv_python", return_value=tmp_path / "python"), patch.object(
        bridge, "_run", return_value=completed
    ) as run:
        observed = bridge._scheduler_status(tmp_path, config)

    assert observed == {"status": "invalid_json"}
    assert run.call_args.args[0][1:3] == ["-m", "mcd_agent"]


def test_bridge_removes_only_stale_mcd_distribution_metadata(tmp_path: Path) -> None:
    bridge = _load_bridge()
    stale = [
        tmp_path / "mcd_agent-1.5.172.dist-info",
        tmp_path / "mcd_agent-1.5.177.dist-info",
        tmp_path / "mcd_agent-1.5.179.dist-info",
        tmp_path / "mcd_agent-1.5.181.dist-info",
    ]
    unrelated = tmp_path / "unrelated-7.2.dist-info"
    for path in [*stale, unrelated]:
        path.mkdir()

    removed = bridge._remove_agent_distribution_metadata(tmp_path)

    assert removed == [path.name for path in stale]
    assert all(not path.exists() for path in stale)
    assert unrelated.is_dir()


def test_bridge_successful_replay_is_read_only_and_hash_bound(tmp_path: Path) -> None:
    bridge = _load_bridge()
    install = tmp_path / "install"
    source = install / "src"
    source.mkdir(parents=True)
    (source / "mcd-runtime-manifest.json").write_text('{"version":"1.5.182"}\n', encoding="utf-8")
    receipt_path = install / "var" / "runtime-bootstrap-receipt.json"
    receipt_path.parent.mkdir(parents=True)
    receipt = {
        "schema": bridge.RECEIPT_SCHEMA,
        "status": "success",
        "target": "1.5.182",
        "archive_sha256": "a" * 64,
    }
    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    with patch.object(bridge, "_install_runtime_probe", return_value={"metadata_version": "1.5.182"}) as probe:
        observed = bridge._successful_replay(install, "a" * 64, "1.5.182")

    assert observed is not None
    assert observed["idempotent_replay"] is True
    assert observed["readback"] == {"metadata_version": "1.5.182"}
    probe.assert_called_once_with(install, "1.5.182")
    assert receipt_path.read_text(encoding="utf-8") == json.dumps(receipt) + "\n"
