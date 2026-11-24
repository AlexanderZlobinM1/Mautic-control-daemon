from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def test_mcd_trusts_the_major_channel_manifest_without_recomputing_compatibility(tmp_path):
    _install, rows = _build_rows(tmp_path)
    by_package = {row["package_uid"]: row for row in rows}

    assert set(by_package) == {
        "vendor.logical.current:7",
        "vendor.logical.older:7",
        "vendor.major.standard:7",
        "vendor.wrong.standard:6",
    }
    assert all(row["selectable"] is True for row in rows)
    assert all(row["mautic_compatible"] is True for row in rows)
    assert all(
        row["mautic_compatibility_reason"] == "mcc_manifest_filtered"
        for row in rows
    )


def test_mcd_requests_the_instance_major_channel(tmp_path):
    install, _rows = _build_rows(tmp_path)
    assert install.mautic_major == 7
    assert _config().plugins_manifest_path_template.format(major=install.mautic_major) == (
        "/packages/mautic7/manifest.json"
    )


def test_only_mcc_published_rows_are_exposed(tmp_path):
    filtered = {"plugins": [_manifest()["plugins"][0]]}
    root = tmp_path / "mautic"
    root.mkdir(parents=True)
    install = SimpleNamespace(
        root=str(root), instance_uid="instance-one",
        console_path=str(root / "bin" / "console"), mautic_major=7, db=None,
    )
    with patch("mcd_agent.plugins._fetch_json", return_value=filtered), patch(
        "mcd_agent.plugins._registered_plugin_bundles", return_value=None,
    ), patch(
        "mcd_agent.plugins._plugin_filesystem_status_external",
        side_effect=lambda _config, *, bundle, **_kwargs: (
            {"status": "ok", "bundles": {}}
            if not bundle else {
                "status": "ok", "plugin_status": "missing",
                "reason": "not installed", "installed_version": "-",
            }
        ),
    ):
        _url, _fallback, _root, rows = plugins._build_plugin_rows(
            config=_config(), install=install,
        )

    assert [row["package_uid"] for row in rows] == ["vendor.logical.current:7"]


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
