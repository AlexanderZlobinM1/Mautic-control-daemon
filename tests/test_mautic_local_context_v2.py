import json
import subprocess

import pytest

from test_mautic_patch_context import fixture
from mcd_agent.mautic_patch_context import load_readonly_discovery_config
from mcd_agent.mautic_local_context import read_local_context


def test_split_discovery_v2_both_entry_points_readonly(tmp_path, monkeypatch):
    config_path, roots = fixture(tmp_path, monkeypatch)
    root = roots[0]
    app = root / "docroot"
    app.mkdir()
    (root / "app").rename(app / "app")
    (root / "plugins").rename(app / "plugins")
    (root / "config").mkdir()
    (app / "app/config/local.php").rename(root / "config/local.php")
    (app / "app/release_metadata.json").write_text('{"version":"7.2.0"}')
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("external command"))
    monkeypatch.setattr("pymysql.connect", lambda **kw: pytest.fail("database query"))
    monkeypatch.setattr("mcd_agent.config.load_config", lambda *a, **kw: pytest.fail("mutating loader"))
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in tmp_path.rglob("*") if p.is_file()}
    config = load_readonly_discovery_config(str(config_path))
    project_result = read_local_context(config, root=str(root))
    application_result = read_local_context(config, root=str(app))
    assert project_result == application_result
    assert project_result["schema"] == "mcd-mautic-local-context-v2"
    assert project_result["source_version"] == "7.2.0"
    assert project_result["version_evidence_source"] == "static_metadata"
    assert set(project_result["context"]) == {"local_instance_uid", "application_root", "table_prefix"}
    assert project_result["context"]["application_root"] == str(app)
    assert project_result["layout"] == dict(project_root=str(root),
        application_root_relative="docroot", console_relative_path="bin/console")
    after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_legacy_composer_dev_placeholder_keeps_migration_context_readonly(tmp_path, monkeypatch):
    config_path, roots = fixture(tmp_path, monkeypatch)
    root = roots[0]
    (root / "app/release_metadata.json").write_text('{"version":"6.0.9"}')
    package = {"name": "mautic/core-lib", "version": "6.0.0-dev"}
    (root / "composer.lock").write_text(json.dumps({"packages": [package]}))
    (root / "vendor/composer").mkdir(parents=True)
    (root / "vendor/composer/installed.json").write_text(json.dumps({"packages": [package]}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("external command"))
    monkeypatch.setattr("pymysql.connect", lambda **kw: pytest.fail("database query"))
    monkeypatch.setattr("mcd_agent.config.load_config", lambda *a, **kw: pytest.fail("mutating loader"))

    result = read_local_context(load_readonly_discovery_config(str(config_path)), root=str(root))

    assert result["source_version"] == "6.0.9"
    assert result["version_evidence_source"] == "static_metadata"
