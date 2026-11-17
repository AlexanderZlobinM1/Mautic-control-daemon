from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import plugins


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        plugins_repo_base_url="https://catalog.example.test/mauticctl",
        mcc_url="",
        mcc_token=None,
        plugins_manifest_path_template="/packages/mautic{major}/manifest.json",
        plugins_repo_fallback_ip="",
        plugins_state_filename=".mcd-plugin.json",
    )


def _manifest() -> dict[str, object]:
    return {
        "plugins": [
            {
                "bundle": "CurrentProjectionBundle",
                "plugin_uid": "vendor.logical:7",
                "package_uid": "vendor.logical.current:7",
                "mautic_versions": ["7.2"],
                "version": "2.0.0",
                "package": "current.tar.gz",
                "sha256": "a" * 64,
            },
            {
                "bundle": "OlderProjectionBundle",
                "plugin_uid": "vendor.logical:7",
                "package_uid": "vendor.logical.older:7",
                "mautic_versions": ["7.0", "7.1"],
                "version": "1.0.0",
                "package": "older.tar.gz",
                "sha256": "b" * 64,
            },
            {
                "bundle": "MajorProjectionBundle",
                "plugin_uid": "vendor.major:7",
                "package_uid": "vendor.major.standard:7",
                "mautic_majors": [7],
                "version": "3.0.0",
                "package": "major.tar.gz",
                "sha256": "c" * 64,
            },
            {
                "bundle": "WrongMajorBundle",
                "plugin_uid": "vendor.wrong:6",
                "package_uid": "vendor.wrong.standard:6",
                "mautic_versions": ["6.0"],
                "version": "1.0.0",
                "package": "wrong.tar.gz",
                "sha256": "d" * 64,
            },
        ]
    }


def _build_rows(tmp_path: Path, *, registered: set[str] | None = None):
    root = tmp_path / "mautic"
    (root / "app").mkdir(parents=True)
    (root / "app" / "release_metadata.json").write_text(
        json.dumps({"version": "7.2.2"}),
        encoding="utf-8",
    )
    install = SimpleNamespace(
        root=str(root),
        instance_uid="instance-one",
        console_path=str(root / "bin" / "console"),
        mautic_major=7,
        db=None,
    )
    def external_status(_config, *, bundle: str, **_kwargs):
        if not bundle:
            return {"status": "ok", "bundles": {}}
        return {
            "status": "ok",
            "plugin_status": "missing",
            "reason": "not installed",
            "installed_version": "-",
        }

    with patch("mcd_agent.plugins._fetch_json", return_value=_manifest()), patch(
        "mcd_agent.plugins._registered_plugin_bundles", return_value=registered
    ), patch(
        "mcd_agent.plugins._plugin_filesystem_status_external",
        side_effect=external_status,
    ):
        _url, _fallback, _root, rows = plugins._build_plugin_rows(
            config=_config(),
            install=install,
        )
    return install, rows


def test_minor_constraints_match_exact_mautic_release_and_fail_closed_without_it():
    assert plugins._plugin_mautic_compatibility(
        {"mautic_versions": ["7.2"]}, mautic_major=7, exact_version="7.2.2"
    )[0] is True
    assert plugins._plugin_mautic_compatibility(
        {"mautic_versions": ["7.0", "7.1"]}, mautic_major=7, exact_version="7.2.2"
    )[0] is False
    assert plugins._plugin_mautic_compatibility(
        {"mautic_versions": ["7.2"]}, mautic_major=7, exact_version=None
    ) == (False, "exact Mautic version unavailable for mautic_versions admission")
    assert plugins._plugin_mautic_compatibility(
        {"mautic_majors": [7]}, mautic_major=7, exact_version=None
    )[0] is True
    assert plugins._plugin_mautic_compatibility(
        {"mautic_versions": ["latest"]}, mautic_major=7, exact_version="7.2.2"
    )[0] is False


def test_catalog_marks_only_compatible_projection_selectable(tmp_path):
    _install, rows = _build_rows(tmp_path)
    by_package = {row["package_uid"]: row for row in rows}

    assert by_package["vendor.logical.current:7"]["selectable"] is True
    assert by_package["vendor.logical.current:7"]["mautic_version"] == "7.2.2"
    assert by_package["vendor.logical.older:7"]["selectable"] is False
    assert "not allowed" in by_package["vendor.logical.older:7"]["mautic_compatibility_reason"]
    assert by_package["vendor.major.standard:7"]["selectable"] is True
    assert by_package["vendor.wrong.standard:6"]["selectable"] is False

    selected = plugins._select_plugin_rows_by_identity(
        rows,
        plugin_uids=["vendor.logical:7"],
        package_uids=[],
        action="install",
    )
    assert [row["package_uid"] for row in selected] == ["vendor.logical.current:7"]


def test_cached_version_is_not_authoritative_for_minor_admission(tmp_path):
    root = tmp_path / "mautic"
    root.mkdir(parents=True)
    install = SimpleNamespace(
        root=str(root),
        instance_uid="instance-one",
        console_path=str(root / "bin" / "console"),
        mautic_major=7,
        db=None,
    )
    with patch("mcd_agent.plugins._fetch_json", return_value=_manifest()), patch(
        "mcd_agent.plugins.read_mautic_version_evidence_read_only",
        return_value={"version": "7.2.2", "source": "cache_fallback"},
    ), patch("mcd_agent.plugins._registered_plugin_bundles", return_value=None), patch(
        "mcd_agent.plugins._plugin_filesystem_status_external",
        side_effect=lambda _config, *, bundle, **_kwargs: (
            {"status": "ok", "bundles": {}}
            if not bundle
            else {
                "status": "ok",
                "plugin_status": "missing",
                "reason": "not installed",
                "installed_version": "-",
            }
        ),
    ):
        _url, _fallback, _root, rows = plugins._build_plugin_rows(
            config=_config(),
            install=install,
        )

    by_package = {row["package_uid"]: row for row in rows}
    assert by_package["vendor.logical.current:7"]["selectable"] is False
    assert by_package["vendor.logical.older:7"]["selectable"] is False
    assert by_package["vendor.major.standard:7"]["selectable"] is True


def test_incompatible_projection_is_rejected_before_any_plugin_mutation(tmp_path):
    install, rows = _build_rows(tmp_path)
    config = _config()
    with patch("mcd_agent.plugins._select_install_with_db", return_value=install), patch(
        "mcd_agent.plugins._build_plugin_rows",
        return_value=("https://catalog.example.test/manifest.json", None, install.root, rows),
    ), patch(
        "mcd_agent.plugins._require_plugin_filesystem_bindings",
        return_value=None,
    ), patch(
        "mcd_agent.plugins._plugin_interaction_context",
        return_value={"sender_type": "", "installed_plugins": {}},
    ), patch("mcd_agent.plugins._apply_plugin_file_changes") as apply_files:
        with pytest.raises(RuntimeError, match="^plugin_package_incompatible_with_mautic_version$"):
            plugins.run_plugins_interactive(
                config=config,
                root="instance-uid",
                selection=None,
                plugin_uids=["vendor.logical:7"],
                package_uids=["vendor.logical.older:7"],
                action="install",
                no_color=True,
                yes=True,
                operation_id="compatibility-rejection",
            )
    apply_files.assert_not_called()

    incompatible = next(
        row for row in rows if row["package_uid"] == "vendor.logical.older:7"
    )
    with pytest.raises(RuntimeError, match="^plugin_package_incompatible_with_mautic_version$"):
        plugins._apply_plugin_file_changes_impl(
            config=config,
            install=install,
            install_root=install.root,
            manifest_dir="https://catalog.example.test/",
            fallback_ip=None,
            action="install",
            selected=[incompatible],
            auto_remove_bundles=[],
            rows_by_bundle={incompatible["bundle"]: incompatible},
            run_post_steps=False,
            operation_id="direct-guard",
        )


def test_machine_contract_reports_typed_incompatibility_without_mutation(tmp_path, capsys):
    install, rows = _build_rows(tmp_path)
    with patch("mcd_agent.plugins._select_install_with_db", return_value=install), patch(
        "mcd_agent.plugins._build_plugin_rows",
        return_value=("https://catalog.example.test/manifest.json", None, install.root, rows),
    ), patch(
        "mcd_agent.plugins._require_plugin_filesystem_bindings",
        return_value=None,
    ), patch(
        "mcd_agent.plugins._plugin_interaction_context",
        return_value={"sender_type": "", "installed_plugins": {}},
    ), patch("mcd_agent.plugins._apply_plugin_file_changes") as apply_files:
        rc = plugins.run_plugins_machine(
            config=_config(),
            instance_uid="instance-one",
            plugin_uids=["vendor.logical:7"],
            package_uids=["vendor.logical.older:7"],
            action="install",
            operation_id="machine-incompatible",
        )

    assert rc == 1
    apply_files.assert_not_called()
    line = capsys.readouterr().out.strip()
    payload = json.loads(line.removeprefix("MCD_PLUGIN_OPERATION_RESULT="))
    assert payload["status"] == "failed"
    assert payload["error_code"] == "plugin_package_incompatible_with_mautic_version"


def test_incompatible_installed_projection_remains_removable(tmp_path):
    _install, rows = _build_rows(tmp_path)
    selected = plugins._select_plugin_rows_by_identity(
        rows,
        plugin_uids=["vendor.logical:7"],
        package_uids=["vendor.logical.older:7"],
        action="remove",
    )
    assert [row["package_uid"] for row in selected] == ["vendor.logical.older:7"]
    plugins._validate_selected_mautic_compatibility(selected, action="remove")


def test_standalone_and_registered_catalogs_share_same_compatibility_decision(tmp_path):
    _install, standalone = _build_rows(tmp_path / "standalone", registered=None)
    _install, registered = _build_rows(
        tmp_path / "registered",
        registered={"CurrentProjectionBundle", "OlderProjectionBundle"},
    )
    standalone_flags = {
        row["package_uid"]: (row["selectable"], row["mautic_compatible"])
        for row in standalone
    }
    registered_flags = {
        row["package_uid"]: (row["selectable"], row["mautic_compatible"])
        for row in registered
    }
    assert standalone_flags == registered_flags
