import json
from pathlib import Path
import subprocess
import sys

import pytest

from mcd_agent import __version__
from mcd_agent.instance_uid import build_domain_uid
from mcd_agent.mautic_patch_context import preflight


def fixture(tmp_path, monkeypatch, prefix="ss_"):
    monkeypatch.setattr("mcd_agent.discovery._web_vhosts_from_server_configs", lambda: {})
    monkeypatch.setattr("mcd_agent.discovery.discover_runtime_instances", lambda **kwargs: [])
    roots = []
    for name in ("selected", "other"):
        root = tmp_path / name
        for directory in ("app/config", "plugins", "bin"):
            (root / directory).mkdir(parents=True)
        (root / "bin/console").write_text("must not execute")
        (root / "app/config/local.php").write_text(
            "<?php $parameters = ['db_host' => '127.0.0.1', 'db_name' => 'shared_fixture', 'db_user' => 'private_user', "
            "'db_password' => 'private_secret', 'db_table_prefix' => '" + prefix + "'];")
        roots.append(root)
    config = tmp_path / "agent.toml"
    config.write_text("[discovery]\nroots = " + json.dumps([str(root) for root in roots]) + "\n")
    return config, roots


@pytest.mark.parametrize("prefix", ["", "ss_"])
def test_context_is_selected_root_not_shared_database(tmp_path, monkeypatch, prefix):
    config, roots = fixture(tmp_path, monkeypatch, prefix)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("external command"))
    monkeypatch.setattr("mcd_agent.config.load_config", lambda *args, **kwargs: pytest.fail("mutating config loader"))
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: pytest.fail("database query"))
    outputs = []
    for root in roots:
        uid = build_domain_uid(domain=None, root=str(root), name=root.name)
        output = preflight(str(config), str(root), uid)
        assert set(output) == {"schema", "agent_version", "features", "execution_context"}
        assert output["agent_version"] == __version__
        assert output["execution_context"] == {"instance_uid": uid, "application_root": str(root), "table_prefix": prefix}
        assert "private_" not in json.dumps(output)
        assert "shared_fixture" not in json.dumps(output)
        outputs.append(output)
    assert outputs[0]["execution_context"] != outputs[1]["execution_context"]
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after


def test_wrong_uid_missing_prefix_and_root_alias_fail_closed(tmp_path, monkeypatch):
    config, roots = fixture(tmp_path, monkeypatch)
    root = roots[0]
    uid = build_domain_uid(domain=None, root=str(root), name=root.name)
    with pytest.raises(ValueError, match="unknown_or_mismatch"):
        preflight(str(config), str(root), "wrong")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="patch_context_(unknown_or_mismatch|discovery_unavailable)"):
        preflight(str(config), str(alias), uid)
    (root / "app/config/local.php").write_text("<?php $parameters = ['db_name' => 'shared_fixture'];")
    with pytest.raises(ValueError, match="unknown_or_mismatch"):
        preflight(str(config), str(root), uid)


@pytest.mark.parametrize("root", ["relative", "/a/../b", "/a//b", "/a/"])
def test_noncanonical_selection_rejected(root):
    with pytest.raises(ValueError, match="selection_invalid"):
        preflight("unused", root, "uid")


def test_cli_json_only_error_no_context(tmp_path, monkeypatch, capsys):
    from mcd_agent import cli
    monkeypatch.setattr(sys, "argv", ["mcd-cli", "mautic-patch-context", "--root", str(tmp_path),
                                      "--instance-uid", "uid", "--config", str(tmp_path / "missing"), "--json"])
    assert cli.main() == 2
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "patch_context_discovery_unavailable"
    assert "execution_context" not in output


def test_split_composer_context_roundtrip_readonly(tmp_path, monkeypatch):
    config, roots = fixture(tmp_path, monkeypatch)
    root = roots[0]
    docroot = root / "docroot"
    docroot.mkdir()
    (root / "app").rename(docroot / "app")
    (root / "plugins").rename(docroot / "plugins")
    (root / "config").mkdir()
    (docroot / "app/config/local.php").rename(root / "config/local.php")
    from mcd_agent.discovery import discover_mautic
    from mcd_agent.mautic_patch_context import load_readonly_discovery_config
    discovery_config = load_readonly_discovery_config(str(config))
    installs = discover_mautic(discovery_config.discovery_roots,
        discovery_config.exclude_path_contains, discovery_config.supported_mautic_majors,
        discovery_config.custom_instances)
    selected = [item for item in installs if Path(item.root) in (root, docroot)]
    assert len(selected) == 1
    uid = selected[0].instance_uid
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("external command"))
    monkeypatch.setattr("mcd_agent.config.load_config", lambda *a, **kw: pytest.fail("mutating loader"))
    monkeypatch.setattr("pymysql.connect", lambda **kw: pytest.fail("database query"))
    for selected_root in (root, docroot):
        output = preflight(str(config), str(selected_root), uid)
        assert output["execution_context"] == dict(instance_uid=uid,
            application_root=str(docroot), table_prefix="ss_")
    after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_qualified_mcc_uid_is_preserved_and_other_host_is_rejected(tmp_path, monkeypatch):
    config, roots = fixture(tmp_path, monkeypatch)
    config.write_text(config.read_text() + '\n[mcc]\nhost_name = "MauticFarm-02"\n')
    root = roots[0]
    bare_uid = build_domain_uid(domain=None, root=str(root), name=root.name)
    qualified_uid = f"{bare_uid}@MauticFarm-02"

    output = preflight(str(config), str(root), qualified_uid)
    assert output["execution_context"]["instance_uid"] == qualified_uid

    with pytest.raises(ValueError, match="unknown_or_mismatch"):
        preflight(str(config), str(root), f"{bare_uid}@MauticFarm-01")
