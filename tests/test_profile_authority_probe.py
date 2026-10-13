from __future__ import annotations

import base64
import importlib.util
from pathlib import Path


def _probe_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "mcd_profile_authority_probe.py"
    spec = importlib.util.spec_from_file_location("mcd_profile_authority_probe_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_is_independent_and_captures_exact_selection_bytes(tmp_path):
    probe = _probe_module()
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

    result = probe.capture(install_dir=install, config=entrypoint)

    assert result["schema"] == "mcd-profile-authority-probe-v1"
    assert result["read_only"] is True
    assert result["config_mutable_path"] == str(local)
    assert result["selection_path"] == str(selection)
    assert result["selection_exists"] is True
    assert base64.b64decode(result["selection_bytes_base64"]) == selection_bytes

