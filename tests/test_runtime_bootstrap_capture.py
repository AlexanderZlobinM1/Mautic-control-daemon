from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import subprocess
import sys


def _bridge_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "mcd_runtime_bootstrap.py"
    spec = importlib.util.spec_from_file_location("mcd_runtime_bootstrap_capture_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_capture_reports_exact_split_selection_bytes(tmp_path):
    bridge = _bridge_module()
    install = tmp_path / "mcd"
    entrypoint = install / "etc" / "mcd.toml"
    local = install / "etc" / "mcd.local.toml"
    selection = install / "var" / "hardware-profile-selection.json"
    local.parent.mkdir(parents=True)
    selection.parent.mkdir(parents=True)
    entrypoint.write_text(
        '# MCD_CONFIG_ENTRYPOINT v1\n[include]\nfiles = ["mcd.local.toml"]\n',
        encoding="utf-8",
    )
    local.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
    selection_bytes = b'{"mode":"manual","profile":"farm-hiload"}\n'
    selection.write_bytes(selection_bytes)

    result = bridge._capture_profile_authority(install, entrypoint)

    assert result["schema"] == "mcd-profile-authority-v1"
    assert result["capture_phase"] == "pre_runtime_install"
    assert result["config_mutable_path"] == str(local)
    assert result["config_files"] == [
        {"path": str(entrypoint.resolve()), "exists": True},
        {"path": str(local.resolve()), "exists": True},
    ]
    assert result["selection_path"] == str(selection)
    assert result["selection_exists"] is True
    assert base64.b64decode(result["selection_bytes_base64"]) == selection_bytes
    assert result["selection_sha256"] == bridge._sha256(selection)


def test_bootstrap_capture_preserves_absent_selection_state(tmp_path):
    bridge = _bridge_module()
    install = tmp_path / "mcd"
    entrypoint = install / "etc" / "mcd.toml"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("[config]\nschema_version = 1\n", encoding="utf-8")

    result = bridge._capture_profile_authority(install, entrypoint)

    assert result["selection_exists"] is False
    assert result["selection_sha256"] == ""
    assert result["selection_bytes_base64"] == ""


def test_bootstrap_capture_cli_is_read_only_and_legacy_compatible(tmp_path):
    bridge_path = Path(__file__).resolve().parents[1] / "tools" / "mcd_runtime_bootstrap.py"
    install = tmp_path / "mcd"
    entrypoint = install / "etc" / "mcd.toml"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("[config]\nschema_version = 1\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(bridge_path),
            "--capture-profile-authority",
            "--install-dir",
            str(install),
            "--config",
            str(entrypoint),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    assert payload["schema"] == "mcd-profile-authority-v1"
    assert payload["capture_phase"] == "pre_runtime_install"
    assert payload["selection_exists"] is False

