from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import custom_scripts
from mcd_agent.config import load_config
from mcd_agent.plugins import (
    _configured_plugin_filesystem_operation,
    _install_or_replace_plugin,
    _plugin_cache_reset_external,
    _plugin_filesystem_runtime,
    _plugin_filesystem_status_external,
    _plugin_status,
    _remove_plugin_alias_path,
    _remove_plugin_bundle_path,
)


def _cfg(cache_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(custom_cache_dir=str(cache_dir), custom_manifest_path="manifest.json")


def test_plugin_filesystem_profile_attributes_load_without_defaults(tmp_path):
    path = tmp_path / "mcd.toml"
    path.write_text(
        '[profile]\nname = "custom"\n'
        '[plugins]\nmanaged_root = "/srv/app"\n'
        'filesystem_owner = "www-data"\n'
        'filesystem_group = "www-data"\n'
        'filesystem_mode = "0775"\n',
        encoding="utf-8",
    )
    config = load_config(str(path), allow_recover_from_mcc=False)
    assert config.plugins_managed_root == "/srv/app"
    assert config.plugins_filesystem_owner == "www-data"
    assert config.plugins_filesystem_group == "www-data"
    assert config.plugins_filesystem_mode == "0775"


@pytest.mark.parametrize("value", [["var/cache/prod"], ["a/b"]])
def test_plugin_cache_relative_paths_accept_strict_relative_values(tmp_path, value):
    path = tmp_path / "mcd.toml"
    path.write_text("[plugins]\ncache_relative_paths = " + json.dumps(value) + "\n", encoding="utf-8")
    assert load_config(str(path), allow_recover_from_mcc=False).plugins_cache_relative_paths == value


def test_plugin_cache_relative_paths_missing_is_none(tmp_path):
    path = tmp_path / "mcd.toml"
    path.write_text("[plugins]\n", encoding="utf-8")
    assert load_config(str(path), allow_recover_from_mcc=False).plugins_cache_relative_paths is None


@pytest.mark.parametrize("value", ["var/cache/prod", [], ["/tmp/x"], ["."], [".."], ["a/../b"], ["a\\b"], ["a\u0000b"], ["C:/x"], ["C:x"], ["//server/share"], ["a/"], ["a//b"], ["a/ b"]])
def test_plugin_cache_relative_paths_reject_invalid_values(tmp_path, value):
    path = tmp_path / "mcd.toml"
    path.write_text("[plugins]\ncache_relative_paths = " + json.dumps(value) + "\n", encoding="utf-8")
    assert load_config(str(path), allow_recover_from_mcc=False).plugins_cache_relative_paths is None


def test_plugin_binding_accepts_only_declared_resolved_runtime(tmp_path, monkeypatch):
    binding = {
        "component": "plugin",
        "operation": "plugin_filesystem_apply",
        "artifact": {"key": "plugin", "sha256": "a" * 64, "entrypoint": "block.py"},
        "args": ["--input-stdin"],
        "capture_result": True,
        "detach": False,
        "live_output": False,
        "offline_only": True,
        "runtime_input_schema": {"type": "string-map", "fields": ["plugin_runtime_json"]},
        "runtime_input_mapping": {
            "plugin_runtime_json": {
                "encoding": "json",
                    "source": "caller_operation_input",
                "fields": ["plugins_root", "bundle"],
            }
        },
    }
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text(json.dumps({"operation_bindings": [binding]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        custom_scripts,
        "run_custom_script_by_key",
        lambda _config, **kwargs: calls.append(kwargs) or (0, '{"status":"noop"}'),
    )

    code, output = custom_scripts.run_configured_external_operation(
        _cfg(cache),
        component="plugin",
        operation="plugin_filesystem_apply",
        profile={},
        operation_input={"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle"},
    )

    assert code == 0
    assert json.loads(output) == {"status": "noop"}
    assert json.loads(calls[0]["runtime_input"]["plugin_runtime_json"]) == {
        "bundle": "VendorBundle",
        "plugins_root": "/srv/app/plugins",
    }


def test_bound_plugin_failure_does_not_fall_back_to_mcd_filesystem(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    (root / "plugins").mkdir(parents=True)
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"opaque archive")
    cfg = SimpleNamespace(custom_cache_dir=str(tmp_path / "cache"), custom_manifest_path="manifest.json")
    (tmp_path / "cache").mkdir()

    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {"status": "error", "reason": "unsafe_archive"},
    )
    monkeypatch.setattr("mcd_agent.plugins._fetch_file", lambda _url, _token, target, **_kw: target.write_bytes(archive.read_bytes()))

    try:
        _install_or_replace_plugin(
            config=cfg,
            root=str(root),
            bundle="VendorBundle",
            package_url="https://mcc.example/VendorBundle.tar.gz",
            token=None,
            fallback_ip=None,
            state_filename=".mcd-plugin.json",
            state_payload={"bundle": "VendorBundle"},
        )
    except RuntimeError as exc:
        assert str(exc) == "unsafe_archive"
    else:
        raise AssertionError("bound external failure must be terminal")


@pytest.mark.parametrize("external_status", ["applied", "noop"])
def test_bound_apply_passes_config_managed_root_and_accepts_repeat_status(monkeypatch, external_status):
    calls = []

    def dispatch(_config, *, component, operation, profile, operation_input):
        calls.append((component, operation, profile, operation_input))
        return 0, json.dumps({"status": external_status})

    monkeypatch.setattr("mcd_agent.plugins._plugin_filesystem_binding_present", lambda *_args: True)
    monkeypatch.setattr("mcd_agent.plugins.run_configured_external_operation", dispatch)
    config = SimpleNamespace(
        plugins_managed_root="/srv/app",
        plugins_filesystem_owner="www-data",
        plugins_filesystem_group="www-data",
        plugins_filesystem_mode="0775",
    )
    result = _configured_plugin_filesystem_operation(
        config,
        operation="plugin_filesystem_apply",
        runtime={
            "archive_path": "/tmp/plugin.tar.gz",
            "plugins_root": "/srv/app/plugins",
            "bundle": "VendorBundle",
            "state_filename": ".mcd-plugin.json",
            "state_payload": {"bundle": "VendorBundle"},
        },
    )

    assert result == {"status": external_status}
    assert calls == [
        (
            "plugin",
            "plugin_filesystem_apply",
            {},
            {
                "archive_path": "/tmp/plugin.tar.gz",
                "plugins_root": "/srv/app/plugins",
                "bundle": "VendorBundle",
                "state_filename": ".mcd-plugin.json",
                "state_payload": {"bundle": "VendorBundle"},
                "managed_root": "/srv/app",
                "owner": "www-data",
                "group": "www-data",
                "mode": "0775",
            },
        )
    ]


def test_cleanup_and_alias_use_declared_external_operations(monkeypatch, tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    calls = []

    def dispatch(_config, *, operation, runtime):
        calls.append((operation, runtime))
        return {"status": "cleaned", "changed_paths": ["x"]}

    monkeypatch.setattr("mcd_agent.plugins._configured_plugin_filesystem_operation", dispatch)
    cfg = SimpleNamespace(plugins_managed_root="/srv/app")
    assert _remove_plugin_bundle_path(config=cfg, plugins_dir=plugins, bundle="VendorBundle")
    assert _remove_plugin_alias_path(
        config=cfg,
        install_root=str(tmp_path),
        plugins_dir=plugins,
        bundle="VendorBundleDev",
    )
    assert calls == [
        ("plugin_filesystem_cleanup", {"plugins_root": str(plugins), "bundle": "VendorBundle"}),
        ("plugin_alias_cleanup", {"paths": ["plugins/VendorBundleDev"]}),
    ]


def test_cleanup_alias_and_cache_bindings_keep_relative_path_contract(monkeypatch, tmp_path):
    calls = []

    def dispatch(_config, *, operation, runtime):
        calls.append((operation, runtime))
        return {"status": "noop"}

    monkeypatch.setattr("mcd_agent.plugins._configured_plugin_filesystem_operation", dispatch)
    config = SimpleNamespace(plugins_managed_root="/srv/app")
    plugins = tmp_path / "plugins"
    plugins.mkdir()

    assert not _remove_plugin_bundle_path(config=config, plugins_dir=plugins, bundle="VendorBundle")
    assert not _remove_plugin_alias_path(
        config=config,
        install_root=str(tmp_path),
        plugins_dir=plugins,
        bundle="VendorBundleDev",
    )
    assert _plugin_cache_reset_external(config, relative_paths=["var/cache/prod"]) == {"status": "noop"}
    assert calls == [
        ("plugin_filesystem_cleanup", {"plugins_root": str(plugins), "bundle": "VendorBundle"}),
        ("plugin_alias_cleanup", {"paths": ["plugins/VendorBundleDev"]}),
        ("plugin_cache_reset", {"paths": ["var/cache/prod"]}),
    ]


@pytest.mark.parametrize(
    "operation,runtime",
    [
        ("plugin_filesystem_cleanup", {"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle"}),
        ("plugin_alias_cleanup", {"paths": ["plugins/VendorBundleDev"]}),
        ("plugin_cache_reset", {"paths": ["var/cache/prod"]}),
    ],
)
@pytest.mark.parametrize("config_value", [None, "relative/root", "/srv/app/../other"])
def test_bound_transaction_managed_root_missing_or_invalid_fails_before_runner(
    tmp_path,
    monkeypatch,
    operation,
    runtime,
    config_value,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text(
        json.dumps({"operation_bindings": [{"component": "plugin", "operation": operation}]}),
        encoding="utf-8",
    )
    config = SimpleNamespace(custom_cache_dir=str(cache))
    if config_value is not None:
        config.plugins_managed_root = config_value
    monkeypatch.setattr(
        "mcd_agent.plugins.run_configured_external_operation",
        lambda *_args, **_kwargs: pytest.fail("runner must not be invoked"),
    )
    result = _configured_plugin_filesystem_operation(config, operation=operation, runtime=runtime)
    assert result == {"status": "error", "reason": "plugin_filesystem_managed_root_invalid"}


@pytest.mark.parametrize(
    "operation,runtime",
    [
        ("plugin_filesystem_cleanup", {"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle"}),
        ("plugin_alias_cleanup", {"paths": ["plugins/VendorBundleDev"]}),
        ("plugin_cache_reset", {"paths": ["var/cache/prod"]}),
    ],
)
def test_bound_transaction_passes_config_managed_root(monkeypatch, operation, runtime):
    calls = []

    monkeypatch.setattr("mcd_agent.plugins._plugin_filesystem_binding_present", lambda *_args: True)

    def dispatch(_config, *, component, operation, profile, operation_input):
        calls.append((component, operation, profile, operation_input))
        return 0, json.dumps({"status": "noop"})

    monkeypatch.setattr("mcd_agent.plugins.run_configured_external_operation", dispatch)
    result = _configured_plugin_filesystem_operation(
        SimpleNamespace(
            plugins_managed_root="/srv/app",
            plugins_filesystem_owner="www-data",
            plugins_filesystem_group="www-data",
            plugins_filesystem_mode="0775",
        ),
        operation=operation,
        runtime=runtime,
    )
    assert result == {"status": "noop"}
    expected_runtime = {**runtime, "managed_root": "/srv/app"}
    if operation == "plugin_cache_reset":
        expected_runtime.update({"owner": "www-data", "group": "www-data", "mode": "0775"})
    assert calls == [("plugin", operation, {}, expected_runtime)]


@pytest.mark.parametrize(
    ("operation", "runtime"),
    [
        ("plugin_filesystem_status", {"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle", "state_filename": ".mcd-plugin.json"}),
        ("plugin_filesystem_cleanup", {"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle"}),
        ("plugin_alias_cleanup", {"paths": ["plugins/VendorBundleDev"]}),
    ],
)
def test_bound_mapping_adds_authoritative_managed_root_without_overriding_runtime(monkeypatch, operation, runtime):
    calls = []
    monkeypatch.setattr("mcd_agent.plugins._plugin_filesystem_binding_present", lambda *_args: True)

    def dispatch(_config, *, component, operation, profile, operation_input):
        calls.append((component, operation, profile, operation_input))
        return 0, json.dumps({"status": "noop"})

    monkeypatch.setattr("mcd_agent.plugins.run_configured_external_operation", dispatch)
    config = SimpleNamespace(plugins_managed_root="/srv/app")

    assert _configured_plugin_filesystem_operation(config, operation=operation, runtime=runtime) == {"status": "noop"}
    assert calls == [("plugin", operation, {}, {**runtime, "managed_root": "/srv/app"})]


@pytest.mark.parametrize("operation", ["plugin_filesystem_apply", "plugin_cache_reset"])
@pytest.mark.parametrize("field", ["plugins_filesystem_owner", "plugins_filesystem_group", "plugins_filesystem_mode"])
def test_bound_filesystem_attributes_missing_or_invalid_fails_before_runner(
    tmp_path,
    monkeypatch,
    operation,
    field,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text(
        json.dumps({"operation_bindings": [{"component": "plugin", "operation": operation}]}),
        encoding="utf-8",
    )
    values = {
        "plugins_managed_root": "/srv/app",
        "plugins_filesystem_owner": "www-data",
        "plugins_filesystem_group": "www-data",
        "plugins_filesystem_mode": "0775",
    }
    values[field] = None if field != "plugins_filesystem_mode" else "775x"
    monkeypatch.setattr(
        "mcd_agent.plugins.run_configured_external_operation",
        lambda *_args, **_kwargs: pytest.fail("runner must not be invoked"),
    )
    runtime = (
        {"archive_path": "/tmp/plugin.tar.gz", "plugins_root": "/srv/app/plugins", "bundle": "VendorBundle", "state_filename": ".mcd-plugin.json", "state_payload": {}}
        if operation == "plugin_filesystem_apply"
        else {"paths": ["var/cache/prod"]}
    )
    result = _configured_plugin_filesystem_operation(
        SimpleNamespace(custom_cache_dir=str(cache), **values),
        operation=operation,
        runtime=runtime,
    )
    assert result == {"status": "error", "reason": "plugin_filesystem_attributes_invalid"}


def test_plugin_runtime_does_not_assign_external_ownership_defaults(tmp_path):
    runtime = _plugin_filesystem_runtime(
        plugins_dir=tmp_path / "plugins",
        bundle="VendorBundle",
        archive_path=tmp_path / "bundle.tar.gz",
        state_filename=".mcd-plugin.json",
        state_payload={"bundle": "VendorBundle"},
    )
    assert set(runtime) == {"archive_path", "plugins_root", "bundle", "state_filename", "state_payload"}


def test_filesystem_status_binding_returns_typed_observation(monkeypatch, tmp_path):
    calls = []

    def dispatch(_config, *, operation, runtime):
        calls.append((operation, runtime))
        return {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "ok",
            "plugin_status": "ok",
            "installed_version": "2.4.1",
            "digest": "a" * 64,
            "state": {"bundle": "VendorBundle", "sha256": "b" * 64},
            "has_doctrine_entity_metadata": False,
            "has_native_migrations": True,
        }

    monkeypatch.setattr("mcd_agent.plugins._configured_plugin_filesystem_operation", dispatch)
    result = _plugin_filesystem_status_external(
        SimpleNamespace(),
        plugins_dir=tmp_path / "plugins",
        bundle="VendorBundle",
        state_filename=".mcd-plugin.json",
    )

    assert result["plugin_status"] == "ok"
    assert result["installed_version"] == "2.4.1"
    assert calls == [
        (
            "plugin_filesystem_status",
            {
                "plugins_root": str(tmp_path / "plugins"),
                "bundle": "VendorBundle",
                "state_filename": ".mcd-plugin.json",
            },
        )
    ]


def test_filesystem_status_no_binding_keeps_compatibility_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {"status": "error", "reason": "plugin_filesystem_binding_not_configured"},
    )
    with pytest.raises(RuntimeError, match="binding_not_configured"):
        _plugin_filesystem_status_external(SimpleNamespace(), plugins_dir=tmp_path / "plugins", bundle="VendorBundle", state_filename=".mcd-plugin.json")


def test_filesystem_status_noop_is_a_valid_typed_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "noop",
            "plugin_status": "noop",
            "installed_version": "2.4.1",
            "digest": "a" * 64,
            "state": None,
            "has_doctrine_entity_metadata": False,
            "has_native_migrations": True,
        },
    )
    result = _plugin_filesystem_status_external(
        SimpleNamespace(),
        plugins_dir=tmp_path / "plugins",
        bundle="VendorBundle",
        state_filename=".mcd-plugin.json",
    )
    assert result["plugin_status"] == "noop"


def test_cache_reset_binding_is_terminal_and_typed(monkeypatch):
    calls = []

    def dispatch(_config, *, operation, runtime):
        calls.append((operation, runtime))
        return {"status": "applied", "reset_paths": ["var/cache/prod"]}

    monkeypatch.setattr("mcd_agent.plugins._configured_plugin_filesystem_operation", dispatch)
    result = _plugin_cache_reset_external(SimpleNamespace(), relative_paths=["var/cache/prod"])
    assert result == {"status": "applied", "reset_paths": ["var/cache/prod"]}
    assert calls == [("plugin_cache_reset", {"paths": ["var/cache/prod"]})]


def test_filesystem_status_typed_failure_never_falls_back_to_local_observer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {"status": "error", "reason": "unsafe_archive"},
    )
    with pytest.raises(RuntimeError, match="unsafe_archive"):
        _plugin_status(
            tmp_path / "plugins",
            {"bundle": "VendorBundle", "version": "2.4.1"},
            ".mcd-plugin.json",
            install_bundle="VendorBundle",
            config=SimpleNamespace(),
        )


def test_filesystem_status_invalid_result_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "unexpected",
        },
    )
    with pytest.raises(RuntimeError, match="plugin_filesystem_status_result_invalid"):
        _plugin_filesystem_status_external(
            SimpleNamespace(),
            plugins_dir=tmp_path / "plugins",
            bundle="VendorBundle",
            state_filename=".mcd-plugin.json",
        )


def test_filesystem_status_requires_complete_inventory_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "ok",
            "plugin_status": "observed",
            "digest": "a" * 64,
            "installed_version": "-",
            "state": None,
            "bundles": [{"bundle": "VendorBundle"}],
        },
    )
    with pytest.raises(RuntimeError, match="plugin_filesystem_status_bundle_rows_invalid"):
        _plugin_filesystem_status_external(
            SimpleNamespace(),
            plugins_dir=tmp_path / "plugins",
            bundle="",
            state_filename=".mcd-plugin.json",
        )


def test_filesystem_status_accepts_canonical_empty_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "ok",
            "plugin_status": "observed",
            "digest": "0" * 64,
            "installed_version": "-",
            "state": None,
            "bundles": [],
        },
    )
    result = _plugin_filesystem_status_external(
        SimpleNamespace(),
        plugins_dir=tmp_path / "plugins",
        bundle="",
        state_filename=".mcd-plugin.json",
    )
    assert result["digest"] == "0" * 64
    assert result["installed_version"] == "-"
    assert result["state"] is None
    assert result["bundles"] == []


@pytest.mark.parametrize(
    "bundles, error",
    [
        (["not-a-row"], "plugin_filesystem_status_bundle_rows_invalid"),
        ([{"bundle": ""}], "plugin_filesystem_status_bundle_name_invalid"),
        ([{"bundle": "VendorBundle"}, {"bundle": "VendorBundle"}], "plugin_filesystem_status_bundle_duplicate"),
        ({"": {}}, "plugin_filesystem_status_bundle_rows_invalid"),
        ({"VendorBundle": {"bundle": "OtherBundle"}}, "plugin_filesystem_status_bundle_name_invalid"),
    ],
)
def test_filesystem_status_rejects_malformed_inventory_rows(monkeypatch, tmp_path, bundles, error):
    monkeypatch.setattr(
        "mcd_agent.plugins._configured_plugin_filesystem_operation",
        lambda *_args, **_kwargs: {
            "schema": "mcd-plugin-filesystem-status-v1",
            "status": "ok",
            "plugin_status": "observed",
            "digest": "0" * 64,
            "installed_version": "-",
            "state": None,
            "bundles": bundles,
        },
    )
    with pytest.raises(RuntimeError, match=error):
        _plugin_filesystem_status_external(
            SimpleNamespace(),
            plugins_dir=tmp_path / "plugins",
            bundle="",
            state_filename=".mcd-plugin.json",
        )


def test_real_configured_status_binding_uses_framed_runner(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    runner = """#!/usr/bin/env python3
import json
import struct
import sys

size = struct.unpack('!I', sys.stdin.buffer.read(4))[0]
frame = json.loads(sys.stdin.buffer.read(size))
runtime = json.loads(frame['plugin_runtime_json'])
assert runtime['bundle'] == 'VendorBundle'
assert runtime['plugins_root'] == '/srv/app/plugins'
assert runtime['managed_root'] == '/srv/app'
result = {
    'schema': 'mcd-plugin-filesystem-status-v1',
    'status': 'ok',
    'plugin_status': 'ok',
    'digest': 'a' * 64,
    'installed_version': '2.4.1',
    'state': {'bundle': 'VendorBundle', 'sha256': 'b' * 64},
    'has_doctrine_entity_metadata': False,
    'has_native_migrations': True,
}
frame = {
    'schema': 'mcd-self-hosted-executable-output-v1',
    'contract_version': '1.0',
    'frame_type': 'result',
    'status': 'succeeded',
    'exit_code': 0,
    'completed': 1,
    'total': 1,
    'rollback': 'not_attempted',
    'result': result,
}
raw = json.dumps(frame, sort_keys=True, separators=(',', ':')).encode()
print(raw.decode())
"""
    bundle = tmp_path / "status.tar.gz"
    payload = tmp_path / "runner.py"
    payload.write_text(runner, encoding="utf-8")
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(payload, arcname="runner.py")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    key = "plugin-status"
    cached = cache / "downloads" / key
    cached.mkdir(parents=True)
    (cached / f"{key}-{digest}.tar.gz").write_bytes(bundle.read_bytes())
    manifest = {
        "manifest_version": 1,
        "items": [{
            "key": key,
            "file": "status.tar.gz",
            "sha256": digest,
            "entrypoint": "runner.py",
            "run_mode": "direct",
            "interactive": False,
        }],
        "operation_bindings": [{
            "component": "plugin",
            "operation": "plugin_filesystem_status",
            "artifact": {"key": key, "sha256": digest, "entrypoint": "runner.py"},
            "args": [],
            "capture_result": True,
            "detach": False,
            "live_output": False,
            "offline_only": True,
            "runtime_input_schema": {"type": "string-map", "fields": ["plugin_runtime_json"]},
            "runtime_input_mapping": {"plugin_runtime_json": {
                "encoding": "json",
                    "source": "caller_operation_input",
                "fields": ["plugins_root", "bundle", "state_filename", "managed_root"],
            }},
        }],
    }
    (cache / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    config = SimpleNamespace(
        custom_cache_dir=str(cache),
        custom_manifest_path="manifest.json",
        plugins_managed_root="/srv/app",
        custom_run_mode_default="direct",
        custom_prefer_tmux=False,
        custom_prefer_screen=False,
    )
    result = _configured_plugin_filesystem_operation(
        config,
        operation="plugin_filesystem_status",
        runtime={"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle", "state_filename": ".mcd-plugin.json"},
    )
    assert result.get("schema") == "mcd-plugin-filesystem-status-v1", result
    assert result.get("installed_version") == "2.4.1", result


@pytest.mark.parametrize("config_value", [None, "relative/root", "/srv/app/../other"])
def test_bound_managed_root_missing_or_invalid_fails_before_runner(tmp_path, monkeypatch, config_value):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text(
        json.dumps({"operation_bindings": [{"component": "plugin", "operation": "plugin_filesystem_status"}]}),
        encoding="utf-8",
    )
    config = SimpleNamespace(custom_cache_dir=str(cache))
    if config_value is not None:
        config.plugins_managed_root = config_value
    monkeypatch.setattr(
        "mcd_agent.plugins.run_configured_external_operation",
        lambda *_args, **_kwargs: pytest.fail("runner must not be invoked"),
    )
    result = _configured_plugin_filesystem_operation(
        config,
        operation="plugin_filesystem_status",
        runtime={"plugins_root": "/srv/app/plugins", "bundle": "VendorBundle", "state_filename": ".mcd-plugin.json"},
    )
    assert result == {"status": "error", "reason": "plugin_filesystem_managed_root_invalid"}


def test_managed_root_is_config_sourced_and_outside_plugins_root_reaches_external(monkeypatch):
    calls = []
    monkeypatch.setattr("mcd_agent.plugins._plugin_filesystem_binding_present", lambda *_args: True)

    def dispatch(_config, *, component, operation, profile, operation_input):
        calls.append((component, operation, operation_input))
        return 0, json.dumps({"status": "error", "reason": "plugins_root_outside_managed_root"})

    monkeypatch.setattr("mcd_agent.plugins.run_configured_external_operation", dispatch)
    config = SimpleNamespace(plugins_managed_root="/srv/app")
    result = _configured_plugin_filesystem_operation(
        config,
        operation="plugin_filesystem_status",
        runtime={"plugins_root": "/srv/other/plugins", "bundle": "VendorBundle", "state_filename": ".mcd-plugin.json"},
    )
    assert result == {"status": "error", "reason": "plugins_root_outside_managed_root"}
    assert calls == [
        (
            "plugin",
            "plugin_filesystem_status",
            {
                "plugins_root": "/srv/other/plugins",
                "bundle": "VendorBundle",
                "state_filename": ".mcd-plugin.json",
                "managed_root": "/srv/app",
            },
        )
    ]
