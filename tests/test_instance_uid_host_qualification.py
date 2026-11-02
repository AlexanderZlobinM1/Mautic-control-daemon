import json
from pathlib import Path

import pytest

from mcd_agent.instance_uid import instance_uid_matches, qualify_instance_uid
from mcd_agent.mautic_patch_context import preflight
from mcd_agent.instance_uid import build_domain_uid


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("mcd_agent.discovery._web_vhosts_from_server_configs", lambda: {})
    monkeypatch.setattr("mcd_agent.discovery.discover_runtime_instances", lambda **kwargs: [])
    root = tmp_path / "selected"
    for directory in ("app/config", "plugins", "bin"):
        (root / directory).mkdir(parents=True)
    (root / "bin/console").write_text("must not execute")
    (root / "app/config/local.php").write_text(
        "<?php $parameters = ['db_host' => '127.0.0.1', 'db_name' => 'fixture', "
        "'db_user' => 'user', 'db_password' => 'secret', 'db_table_prefix' => 'ss_'];"
    )
    config = tmp_path / "agent.toml"
    config.write_text(
        "[discovery]\nroots = " + json.dumps([str(root)])
        + '\n[mcc]\nhost_name = "MauticFarm-02"\n'
    )
    return config, root


def test_demov6_wire_uid_is_host_qualified_and_cross_host_is_rejected():
    bare = "demov6.sales-snap.com"
    canonical = qualify_instance_uid(bare, "MauticFarm-02")
    assert canonical == "demov6.sales-snap.com@MauticFarm-02"
    assert instance_uid_matches(canonical, bare, "MauticFarm-02")
    assert instance_uid_matches(bare, bare, "MauticFarm-02")
    assert not instance_uid_matches("demov6.sales-snap.com@MauticFarm-01", bare, "MauticFarm-02")


def test_patch_context_preserves_canonical_uid_and_rejects_other_host(tmp_path, monkeypatch):
    config, root = _fixture(tmp_path, monkeypatch)
    bare = build_domain_uid(domain=None, root=str(root), name=root.name)
    canonical = f"{bare}@MauticFarm-02"
    result = preflight(str(config), str(root), canonical)
    assert result["execution_context"]["instance_uid"] == canonical
    with pytest.raises(ValueError, match="unknown_or_mismatch"):
        preflight(str(config), str(root), f"{bare}@MauticFarm-01")
