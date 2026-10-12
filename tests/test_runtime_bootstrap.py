from __future__ import annotations

import importlib.util
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
