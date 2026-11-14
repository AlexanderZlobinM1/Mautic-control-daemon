from __future__ import annotations

import base64
import hashlib
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

    assert result["schema"] == "mcd-profile-authority-probe-v1"
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
    source_bridge_path = Path(__file__).resolve().parents[1] / "tools" / "mcd_runtime_bootstrap.py"
    bridge_path = tmp_path / "mcd_runtime_bootstrap.py"
    bridge_path.write_bytes(source_bridge_path.read_bytes())
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
    assert payload["schema"] == "mcd-profile-authority-probe-v1"
    assert payload["capture_phase"] == "pre_runtime_install"
    assert payload["selection_exists"] is False


def test_source_free_runtime_repairs_only_managed_legacy_default_includes(tmp_path):
    bridge = _bridge_module()
    install = tmp_path / "mcd"
    entrypoint = install / "etc" / "mcd.toml"
    local = install / "etc" / "mcd.local.toml"
    runtime = install / "runtime"
    entrypoint.parent.mkdir(parents=True)
    (runtime / "etc").mkdir(parents=True)
    entrypoint.write_text(
        '# MCD_CONFIG_ENTRYPOINT v1\n'
        '[include]\n'
        f'files = ["{install / "src/etc/mcd-agent.system.example.toml"}", '
        f'"{install / "src/etc/mcd-agent.operator.example.toml"}", '
        f'"{local}"]\n',
        encoding="utf-8",
    )
    local_bytes = b'[mcc]\nurl = "https://controller.example.invalid"\ntoken = "preserve"\n'
    local.write_bytes(local_bytes)
    for name in ("mcd-agent.system.example.toml", "mcd-agent.operator.example.toml"):
        (runtime / "etc" / name).write_text("# verified runtime default\n", encoding="utf-8")

    captured = bridge._capture_profile_authority(install, entrypoint)
    result = bridge._migrate_runtime_config_includes(entrypoint, install, runtime)

    assert any(row["exists"] is False and "/src/etc/" in row["path"] for row in captured["config_files"])
    assert result["changed"] is True
    assert len(result["replacements"]) == 2
    assert "/src/" not in entrypoint.read_text(encoding="utf-8")
    assert str(runtime / "etc") in entrypoint.read_text(encoding="utf-8")
    assert local.read_bytes() == local_bytes
    assert not (install / "src").exists()
    assert bridge._profile_config_files(entrypoint) == [
        {"path": str(entrypoint.resolve()), "exists": True},
        {"path": str((runtime / "etc/mcd-agent.system.example.toml").resolve()), "exists": True},
        {"path": str((runtime / "etc/mcd-agent.operator.example.toml").resolve()), "exists": True},
        {"path": str(local.resolve()), "exists": True},
    ]
    assert result["after_sha256"] == hashlib.sha256(entrypoint.read_bytes()).hexdigest()


def test_named_profile_rejects_runtime_package_without_universal_resource(tmp_path):
    bridge = _bridge_module()
    config = tmp_path / "mcd.toml"
    config.write_text('[profile]\nname = "passive"\n', encoding="utf-8")

    try:
        bridge._require_runtime_profile_closure(config, {"version": "1.7.0.dev0"})
    except bridge.BridgeError as exc:
        assert "universal_workload_resource_ref" in str(exc)
    else:
        raise AssertionError("named profile admitted an incomplete runtime package")

    assert bridge._require_runtime_profile_closure(
        config,
        {"universal_workload_resource_ref": {"schema": "mcc-universal-workload-resource-ref-v1"}},
    ) == "passive"

