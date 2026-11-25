from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from mcd_agent import custom_scripts as custom


OPERATIONS = (
    "plugin_filesystem_status",
    "plugin_filesystem_apply",
    "plugin_filesystem_cleanup",
    "plugin_alias_cleanup",
    "plugin_cache_reset",
)


def _archive(version: str) -> bytes:
    output = BytesIO()
    body = f"#!/usr/bin/env python3\nprint({version!r})\n".encode()
    resource = f"component={version}\n".encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("plugin_filesystem_component/runner.py")
        info.size = len(body)
        archive.addfile(info, BytesIO(body))
        info = tarfile.TarInfo("plugin_filesystem_component/component.txt")
        info.size = len(resource)
        archive.addfile(info, BytesIO(resource))
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


def _binding(operation: str, row: dict[str, object], *, include_size: bool) -> dict[str, object]:
    artifact = {
        "key": row["key"],
        "sha256": row["sha256"],
        "entrypoint": row["entrypoint"],
    }
    if include_size:
        artifact["size_bytes"] = row["size_bytes"]
    return {
        "component": "plugin",
        "component_identity": "plugin-filesystem-component",
        "operation": operation,
        "artifact": artifact,
        "args": ["--operation", operation, "--input-stdin"],
        "capture_result": True,
        "detach": False,
        "live_output": False,
        "offline_only": True,
        "runtime_input_schema": {"type": "string-map", "fields": ["plugin_runtime_json"]},
        "runtime_input_mapping": {
            "plugin_runtime_json": {"encoding": "json", "source": "caller_operation_input"}
        },
    }


def _write_manifest(config, row, *, include_size: bool) -> None:
    item = {"type": "script", **row}
    if not include_size:
        item.pop("size_bytes", None)
    payload = {
        "manifest_version": 1,
        "items": [item],
        "operation_bindings": [
            _binding(operation, row, include_size=include_size)
            for operation in OPERATIONS
        ],
    }
    root = Path(config.custom_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _catalog(row: dict[str, object]) -> dict[str, object]:
    version = str(row["key"]).removeprefix("plugin-filesystem-component-")
    return {
        "schema": "mcc-custom-component-catalog-v1",
        "contract_version": "1.0",
        "identity": "plugin-filesystem-component",
        "descriptor_schema": "mcc-selected-component-descriptor-v1",
        "descriptor_version": "1.0.0",
        "compatibility": {
            "owner": "mcc",
            "selection": "filtered_before_publication",
            "mcd_must_not_compute": True,
        },
        "capability_projection": {
            "opaque_to_mcd": True,
            "operations_source": "operations",
        },
        "operations": [
            {
                "id": operation,
                "name": operation.replace("_", " ").title(),
                "input_schema": {
                    "schema": "mcc-component-operation-input-v1",
                    "type": "json-object",
                    "opaque": True,
                },
                "result_schema": {
                    "schema": "mcc-component-operation-result-v1",
                    "type": "json-object",
                    "opaque": True,
                },
                "capability": {
                    "schema": "mcc-component-capability-v1",
                    "id": operation,
                    "opaque": True,
                },
                "sequencing": {
                    "owner": "component_adapter",
                    "scope": "instance",
                    "mode": "serial",
                },
            }
            for operation in OPERATIONS
        ],
        "selected": {
            "identity": "plugin-filesystem-component",
            "version": version,
            "key": row["key"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "source_revision": "f" * 40,
            "artifact_file": row["file"],
            "entrypoint": row["entrypoint"],
            "target_root": "/opt/mcd/var/custom/components/plugin-filesystem-component",
        },
    }


def _cache_manifest(config):
    payload = json.loads((Path(config.custom_cache_dir) / "manifest.json").read_text())
    return custom._parse_manifest(payload), "cache"


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


def test_valid_installed_component_repairs_stale_binding_sizes_offline(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    installed = _install(monkeypatch, config, row, raw)
    _write_manifest(config, row, include_size=False)
    monkeypatch.setattr(custom, "fetch_custom_manifest", lambda *_a, **_k: _cache_manifest(config))
    monkeypatch.setattr(
        custom,
        "fetch_external_component_catalog",
        lambda *_a, **_k: (_ for _ in ()).throw(
            custom.ExternalComponentCatalogUnavailable("offline")
        ),
    )
    monkeypatch.setattr(
        custom.urllib.request,
        "urlopen",
        lambda *_a, **_k: pytest.fail("valid local component must remain offline"),
    )

    status = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )

    assert status["status"] == "ready"
    assert status["source"] == "local-offline"
    assert installed.is_file()
    cached = json.loads((Path(config.custom_cache_dir) / "manifest.json").read_text())
    assert {binding["artifact"]["size_bytes"] for binding in cached["operation_bindings"]} == {len(raw)}
    parsed = next(item for item in custom._parse_manifest(cached) if item["key"] == row["key"])
    assert parsed["size_bytes"] == len(raw)


def test_selected_newer_component_replaces_lower_local_version(tmp_path, monkeypatch):
    config = _config(tmp_path)
    old_raw = _archive("1.0.2")
    old_row = _row("1.0.2", old_raw)
    _install(monkeypatch, config, old_row, old_raw)
    _write_manifest(config, old_row, include_size=False)
    new_raw = _archive("1.0.3")
    new_row = _row("1.0.3", new_raw)
    monkeypatch.setattr(custom, "fetch_custom_manifest", lambda *_a, **_k: _cache_manifest(config))
    monkeypatch.setattr(custom, "fetch_external_component_catalog", lambda *_a, **_k: _catalog(new_row))
    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: BytesIO(new_raw))

    status = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )

    assert status["status"] == "ready"
    assert status["source"] == "mcc-catalog"
    metadata = json.loads(
        (Path(config.custom_cache_dir) / "components/plugin-filesystem-component/metadata.json").read_text()
    )
    assert metadata["version"] == "1.0.3"
    cached = json.loads((Path(config.custom_cache_dir) / "manifest.json").read_text())
    assert {binding["artifact"]["key"] for binding in cached["operation_bindings"]} == {new_row["key"]}

    repeated = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )
    assert repeated["status"] == "ready"
    cached = json.loads((Path(config.custom_cache_dir) / "manifest.json").read_text())
    assert sum(
        item.get("key") == new_row["key"]
        for item in cached["items"]
        if isinstance(item, dict)
    ) == 1


def test_missing_and_corrupt_components_are_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    _write_manifest(config, row, include_size=False)
    monkeypatch.setattr(custom, "fetch_custom_manifest", lambda *_a, **_k: _cache_manifest(config))
    monkeypatch.setattr(
        custom,
        "fetch_external_component_catalog",
        lambda *_a, **_k: (_ for _ in ()).throw(
            custom.ExternalComponentCatalogUnavailable("offline")
        ),
    )

    missing = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )
    assert missing["status"] == "unavailable"
    assert missing["capabilities"] == []

    path = _install(monkeypatch, config, row, raw)
    path.write_text("corrupt", encoding="utf-8")
    corrupt = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=False,
    )
    assert corrupt["status"] == "unavailable"
    assert corrupt["reason"] == "external_component_local_invalid"


def test_non_entrypoint_component_corruption_is_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    _install(monkeypatch, config, row, raw)
    _write_manifest(config, row, include_size=True)
    metadata = json.loads(
        (Path(config.custom_cache_dir) / "components/plugin-filesystem-component/metadata.json").read_text()
    )
    resource = (
        Path(config.custom_cache_dir)
        / "components/plugin-filesystem-component/revisions"
        / metadata["sha256"]
        / "plugin_filesystem_component/component.txt"
    )
    resource.write_text("corrupt\n", encoding="utf-8")

    status = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=False,
    )

    assert status["status"] == "unavailable"
    assert status["reason"] == "external_component_local_invalid"


def test_failed_newer_download_hides_capability_until_valid_restoration(tmp_path, monkeypatch):
    config = _config(tmp_path)
    old_raw = _archive("1.0.2")
    old_row = _row("1.0.2", old_raw)
    _install(monkeypatch, config, old_row, old_raw)
    _write_manifest(config, old_row, include_size=True)
    new_raw = _archive("1.0.3")
    new_row = _row("1.0.3", new_raw)
    monkeypatch.setattr(custom, "fetch_custom_manifest", lambda *_a, **_k: _cache_manifest(config))
    monkeypatch.setattr(custom, "fetch_external_component_catalog", lambda *_a, **_k: _catalog(new_row))
    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: BytesIO(b"broken"))

    failed = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )
    assert failed["status"] == "unavailable"
    assert failed["reason"] == "external_component_activation_failed"
    assert custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=False,
    )["status"] == "unavailable"

    monkeypatch.setattr(custom.urllib.request, "urlopen", lambda *_a, **_k: BytesIO(new_raw))
    restored = custom.external_component_preflight(
        config,
        identity="plugin-filesystem-component",
        refresh=True,
    )
    assert restored["status"] == "ready"
    assert restored["selected"]["version"] == "1.0.3"


def test_authenticated_component_catalog_preserves_exact_selected_coordinates(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.mcc_url = "https://mcc.example"
    config.mcc_token = "existing-key"
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    payload = _catalog(row)
    seen = []

    def response(request, **_kwargs):
        seen.append(request)
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(custom.urllib.request, "urlopen", response)
    catalog = custom.fetch_external_component_catalog(config, "plugin-filesystem-component")

    assert catalog["selected"]["sha256"] == row["sha256"]
    assert catalog["selected"]["size_bytes"] == len(raw)
    assert seen[0].full_url.endswith(
        "/api/v1/agent/custom-components/plugin-filesystem-component/catalog"
    )
    assert seen[0].headers["Authorization"] == "Bearer existing-key"


def test_mcc_major_filtered_operation_descriptors_are_passed_through_unchanged(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.mcc_url = "https://mcc.example"
    config.mcc_token = "existing-key"
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    payload = _catalog(row)
    payload["operations"] = [{
        "operation": "vendor.catalog.refresh",
        "applicable_mautic_majors": [7],
        "ui": {"label": "Refresh catalog"},
    }]
    monkeypatch.setattr(
        custom.urllib.request,
        "urlopen",
        lambda *_a, **_k: BytesIO(json.dumps(payload).encode()),
    )

    catalog = custom.fetch_external_component_catalog(config, "plugin-filesystem-component")

    assert catalog["operations"] == payload["operations"]
    assert catalog["descriptor"] == {
        "descriptor_schema": payload["descriptor_schema"],
        "descriptor_version": payload["descriptor_version"],
        "compatibility": payload["compatibility"],
        "capability_projection": payload["capability_projection"],
    }


def test_generic_dispatch_uses_manifest_component_and_operation(tmp_path, monkeypatch):
    config = _config(tmp_path)
    raw = _archive("1.0.2")
    row = _row("1.0.2", raw)
    _install(monkeypatch, config, row, raw)
    operation = "vendor.catalog.refresh"
    payload = {
        "manifest_version": 1,
        "items": [{"type": "script", **row}],
        "operation_bindings": [_binding(operation, row, include_size=True)],
    }
    (Path(config.custom_cache_dir) / "manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(custom, "fetch_custom_manifest", lambda *_a, **_k: _cache_manifest(config))
    monkeypatch.setattr(
        custom,
        "fetch_external_component_catalog",
        lambda *_a, **_k: (_ for _ in ()).throw(
            custom.ExternalComponentCatalogUnavailable("offline")
        ),
    )
    calls = []
    monkeypatch.setattr(
        custom,
        "run_configured_external_operation",
        lambda _config, **kwargs: calls.append(kwargs) or (0, '{"status":"ok"}'),
    )

    rc, output = custom.run_external_component_operation(
        config,
        identity="plugin-filesystem-component",
        operation=operation,
        operation_input={"opaque": {"value": 1}},
    )

    assert (rc, json.loads(output)) == (0, {"status": "ok"})
    assert calls == [{
        "component": "plugin",
        "operation": operation,
        "profile": {},
        "operation_input": {"opaque": {"value": 1}},
    }]
