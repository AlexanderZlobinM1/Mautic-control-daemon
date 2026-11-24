from __future__ import annotations

import hashlib
from io import BytesIO
import json
import tarfile
from types import SimpleNamespace

import pytest

from mcd_agent import custom_scripts as custom


def _archive(version: str) -> bytes:
    output = BytesIO()
    body = f"#!/usr/bin/env python3\nprint({version!r})\n".encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("plugin_filesystem_component/runner.py")
        info.size = len(body)
        archive.addfile(info, BytesIO(body))
    return output.getvalue()


def _row(version: str, raw: bytes) -> dict[str, object]:
    return {
        "key": f"plugin-filesystem-component-{version}",
        "file": f"plugin-filesystem-component-{version}.tar.gz",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "entrypoint": "plugin_filesystem_component/runner.py",
        "run_mode": "direct",
        "interactive": False,
    }


def _config(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    return SimpleNamespace(
        custom_cache_dir=str(root), custom_repo_base_url="https://mcc.example/custom",
        plugins_repo_base_url="", mcc_url="", mcc_token=None,
        custom_manifest_path="manifest.json", custom_run_mode_default="direct",
    )


def _install(monkeypatch, config, row, raw):
    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: BytesIO(raw))
    return custom._fetch_script_to_cache(config, row)


def test_component_first_install_uses_stable_identity_path(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    path = _install(monkeypatch, config, _row("1.0.2", raw), raw)
    root = tmp_path / "cache/components/plugin-filesystem-component"
    metadata = json.loads((root / "metadata.json").read_text())
    assert path == (root / "current/plugin_filesystem_component/runner.py").resolve()
    assert metadata["version"] == "1.0.2" and metadata["rollback_sha256"] is None
    assert not list(root.glob(".download-*")) and len(list((root / "revisions").iterdir())) == 1


def test_component_update_keeps_only_current_and_one_rollback(tmp_path, monkeypatch):
    config = _config(tmp_path)
    rows = []
    for version in ("1.0.2", "1.0.3", "1.0.4"):
        raw = _archive(version)
        row = _row(version, raw)
        _install(monkeypatch, config, row, raw)
        rows.append(row)
    root = tmp_path / "cache/components/plugin-filesystem-component"
    metadata = json.loads((root / "metadata.json").read_text())
    revisions = {path.name for path in (root / "revisions").iterdir()}
    assert metadata["version"] == "1.0.4"
    assert revisions == {rows[-1]["sha256"], rows[-2]["sha256"]}
    assert (root / "rollback").resolve().name == rows[-2]["sha256"]


def test_corrupt_or_interrupted_download_preserves_active_component(tmp_path, monkeypatch):
    config = _config(tmp_path)
    initial = _archive("1.0.2")
    initial_row = _row("1.0.2", initial)
    original = _install(monkeypatch, config, initial_row, initial)
    before = original.read_bytes()
    update = _archive("1.0.3")
    update_row = _row("1.0.3", update)

    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: BytesIO(b"corrupt"))
    with pytest.raises(RuntimeError, match="(?:size|checksum) mismatch"):
        custom._fetch_script_to_cache(config, update_row)
    assert original.read_bytes() == before

    def interrupted(*_a, **_k):
        raise OSError("interrupted")
    monkeypatch.setattr(custom.urllib.request, "urlopen", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        custom._fetch_script_to_cache(config, update_row)
    root = tmp_path / "cache/components/plugin-filesystem-component"
    assert original.read_bytes() == before and not list(root.glob(".download-*"))


def test_repeat_is_noop_and_offline_missing_is_exact(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    first = _install(monkeypatch, config, row, raw)
    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("must stay offline"))
    assert custom._fetch_script_to_cache(config, row, offline_only=True) == first

    missing = _row("1.0.3", _archive("1.0.3"))
    with pytest.raises(custom.CustomSelectedTaskError, match="plugin-filesystem-component:plugin-filesystem-component-1.0.3"):
        custom._fetch_script_to_cache(config, missing, offline_only=True)
