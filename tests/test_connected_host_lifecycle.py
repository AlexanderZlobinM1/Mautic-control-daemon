from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

from mcd_agent import connected_host_lifecycle as lifecycle
from mcd_agent.external_operation_cache import stage_transient_delivery, sync_binding

REVISION = "3.2.1"


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def deterministic_tar(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as stream:
        stream.write(raw.getvalue())
    return out.getvalue()


def build_closure(tmp_path: Path):
    cfg = SimpleNamespace(custom_cache_dir=str(tmp_path / "custom"))
    root = Path(cfg.custom_cache_dir)
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({
        "manifest_version": 1,
        "items": [{"key": "existing", "file": "existing.sh", "sha256": "e" * 64}],
    }))
    profile = {
        "schema": "mcc-mautic-connected-host-lifecycle-profile-v1",
        "version": "1.0.0",
        "catalog_item": {"id": lifecycle.CATALOG_ITEM_ID, "revision": REVISION},
    }
    profile_bytes = canonical(profile) + b"\n"
    profile_sha = hashlib.sha256(profile_bytes).hexdigest()
    profile_file = f"profiles/profile-{profile_sha}.json"
    bundle = b"fixture-executable-bundle"
    artifact_sha = hashlib.sha256(bundle).hexdigest()
    key = "mautic-connected-host-lifecycle-v3.2.1"
    entrypoint = "scenarios/lifecycle.py"
    mappings = {
        "operator_profile_json": {"encoding": "json", "envelope_key": "profile", "source": "existing_private_operator_profile"},
        "cfg_facts_json": {"encoding": "json", "fields": [], "source": "effective_caller_cfg"},
        "operation_input_json": {"encoding": "json", "envelope_key": "input", "source": "caller_operation_input"},
    }
    operation_bindings = []
    for command_id in lifecycle._COMMANDS:
        operation_bindings.append({
            "component": lifecycle.COMPONENT,
            "operation": command_id,
            "artifact": {"entrypoint": entrypoint, "key": key, "sha256": artifact_sha},
            "profile_source": {"type": "cached-json", "file": profile_file, "sha256": profile_sha},
            "args": ["--bound-operation", command_id, "--runtime-input-stdin"],
            "capture_result": True,
            "detach": False,
            "live_output": False,
            "offline_only": True,
            "runtime_input_schema": {"type": "string-map", "fields": list(mappings)},
            "runtime_input_mapping": mappings,
        })
    identity = {
        "schema": lifecycle.BINDING_SCHEMA,
        "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
        "catalog_item_revision": REVISION,
        "artifact": {
            "key": key, "sha256": artifact_sha, "size_bytes": len(bundle),
            "media_type": "application/gzip", "entrypoint": entrypoint,
            "entrypoint_sha256": "a" * 64, "block_sha256": "b" * 64,
        },
        "profile": {
            "schema": profile["schema"], "version": profile["version"],
            "file": profile_file, "sha256": profile_sha,
        },
        "capability": {
            "capability_id": lifecycle.CAPABILITY_ID,
            "version": lifecycle.CAPABILITY_VERSION,
            "execution": "generic_primitive",
            "target_scope": "host",
            "commands": [
                {
                    "command_id": command_id, "version": "1.0",
                    "mutates_state": values[0], "input_schema_ref": values[1],
                    "target_scope": "host",
                }
                for command_id, values in lifecycle._COMMANDS.items()
            ],
        },
        "operation_bindings": operation_bindings,
        "receipt_schemas": ["fixture"],
    }
    identity_bytes = canonical(identity) + b"\n"
    binding_sha = "sha256:" + hashlib.sha256(canonical(identity)).hexdigest()
    identity_cache = "bindings/mautic-connected-host-lifecycle-binding-v1.json"
    artifact_cache = f"downloads/{key}/{key}-{artifact_sha}.tar.gz"
    resources = [
        {"role": "binding_identity", "file": "binding-identity-v1.json", "cache_file": identity_cache,
         "sha256": hashlib.sha256(identity_bytes).hexdigest(), "size_bytes": len(identity_bytes),
         "canonical_json_sha256": binding_sha[7:]},
        {"role": "operator_profile", "file": Path(profile_file).name, "cache_file": profile_file,
         "sha256": profile_sha, "size_bytes": len(profile_bytes)},
    ]
    manifest = {
        "manifest_version": 1,
        "items": [{
            "key": key, "name": "lifecycle", "file": "bundle.tar.gz", "entrypoint": entrypoint,
            "sha256": artifact_sha, "run_mode": "direct", "interactive": False, "type": "script",
        }],
        "operation_bindings": operation_bindings,
        "resources": resources,
    }
    manifest_bytes = canonical(manifest) + b"\n"
    payloads = {
        "binding-identity-v1.json": identity_bytes,
        "manifest.json": manifest_bytes,
        Path(profile_file).name: profile_bytes,
        "bundle.tar.gz": bundle,
    }
    descriptor_resources = [
        {"path": name, "role": role, "install_path": install, "sha256": hashlib.sha256(payloads[name]).hexdigest(), "size_bytes": len(payloads[name])}
        for name, role, install in (
            ("binding-identity-v1.json", "binding_identity", identity_cache),
            ("manifest.json", "operation_binding_manifest", "manifest.json"),
            (Path(profile_file).name, "operator_profile", profile_file),
            ("bundle.tar.gz", "executable_bundle", artifact_cache),
        )
    ]
    descriptor = {
        "schema": "mcc-external-operation-cache-closure-v1",
        "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
        "catalog_item_revision": REVISION,
        "binding_schema": lifecycle.BINDING_SCHEMA,
        "binding_sha256": binding_sha,
        "install_mode": "atomic_append_only_preserve_existing",
        "offline_only_after_sync": True,
        "resources": descriptor_resources,
    }
    closure = deterministic_tar({**payloads, "descriptor.json": canonical(descriptor) + b"\n"})
    closure_digest = "sha256:" + hashlib.sha256(closure).hexdigest()
    command_input = {
        "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
        "catalog_item_revision": REVISION,
        "closure_digest": closure_digest,
        "closure_size_bytes": len(closure),
        "closure_media_type": "application/gzip",
        "binding_schema": lifecycle.BINDING_SCHEMA,
        "binding_sha256": binding_sha,
    }
    command = {
        "capability": {"capability_id": "external_operation_cache", "version": "1.0"},
        "command": {"command_id": "sync_binding", "version": "1.0"},
        "input": command_input,
        "artifact_delivery": {
            "schema": "portal-mcd-transient-artifact-delivery-v1", "contract_version": "1.0",
            "artifacts": [{
                "role": "binding_closure", "artifact_path": "/artifacts/closure", "artifact_grant": "one-time",
                "artifact_digest": closure_digest, "artifact_size_bytes": len(closure),
                "artifact_media_type": "application/gzip", "artifact_revision": REVISION,
                "trusted_catalog": {
                    "source": "mcc", "transport": "portal_authenticated_catalog", "status": "active",
                    "revision": REVISION, "artifact_digest": closure_digest,
                },
            }],
        },
    }
    return cfg, command, closure, binding_sha, artifact_sha, profile_sha


def install_fixture(tmp_path):
    cfg, command, closure, binding_sha, artifact_sha, profile_sha = build_closure(tmp_path)
    sanitized = stage_transient_delivery(
        command, config=cfg,
        downloader=lambda **kwargs: closure,
    )
    assert "artifact_delivery" not in sanitized
    result = sync_binding(sanitized, config=cfg)
    assert result["status"] == "succeeded"
    return cfg, binding_sha, artifact_sha, profile_sha


def test_portal_grant_is_stripped_before_atomic_cache_activation(tmp_path):
    cfg, binding_sha, artifact_sha, profile_sha = install_fixture(tmp_path)
    installed = lifecycle.installed_binding(cfg)
    assert installed["binding_sha256"] == binding_sha
    assert installed["artifact_sha256"] == artifact_sha
    assert installed["profile_sha256"] == profile_sha
    assert [row["command_id"] for row in installed["capability"]["commands"]] == list(lifecycle._COMMANDS)
    manifest = json.loads((Path(cfg.custom_cache_dir) / "manifest.json").read_text())
    assert any(row.get("key") == "existing" for row in manifest["items"])
    assert sync_binding({"input": {
        "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
        "catalog_item_revision": REVISION,
        "closure_digest": "sha256:" + next((Path(cfg.custom_cache_dir) / "portal-ingress").glob("*.tar.gz")).name.removesuffix(".tar.gz"),
        "closure_size_bytes": next((Path(cfg.custom_cache_dir) / "portal-ingress").glob("*.tar.gz")).stat().st_size,
        "closure_media_type": "application/gzip",
        "binding_schema": lifecycle.BINDING_SCHEMA,
        "binding_sha256": binding_sha,
    }}, config=cfg)["status"] == "succeeded"


def test_cache_sync_conflict_keeps_live_manifest_unchanged(tmp_path):
    cfg, command, closure, _binding_sha, _artifact_sha, _profile_sha = build_closure(tmp_path)
    manifest_path = Path(cfg.custom_cache_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["items"].append({
        "key": "mautic-connected-host-lifecycle-v3.2.1",
        "file": "conflicting.tar.gz",
        "sha256": "f" * 64,
    })
    manifest_path.write_bytes(canonical(manifest) + b"\n")
    before = manifest_path.read_bytes()
    sanitized = stage_transient_delivery(
        command, config=cfg, downloader=lambda **_kwargs: closure,
    )
    assert sync_binding(sanitized, config=cfg) == {
        "status": "failed", "reason": "external_operation_cache_manifest_conflict",
    }
    assert manifest_path.read_bytes() == before


def valid_operation_input(binding):
    values = {field: "value" for field in lifecycle._INPUT_FIELDS}
    preflight_receipt = canonical({"schema": "mcc-mautic-connected-host-preflight-v1"}).decode()
    values.update({
        "action": "instance.create", "plan_operation": "instance.create",
        "target_instance_uid": "future-instance-1", "target_dns": "mautic.example.com",
        "dns_check_mode": "require_absent", "php_version": "8.4", "health_wait_seconds": "30",
        "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
        "catalog_item_revision": REVISION,
        "artifact_sha256": binding["artifact_sha256"], "profile_sha256": binding["profile_sha256"],
        "binding_sha256": binding["binding_sha256"],
        "expected_plan_hash": "sha256:" + "1" * 64,
        "expected_before_state_sha256": "sha256:" + "2" * 64,
        "preflight_receipt_sha256": hashlib.sha256(preflight_receipt.encode()).hexdigest(),
        "preflight_receipt_json": preflight_receipt,
        "ownership_receipt_sha256": "", "ownership_receipt_json": "",
    })
    for field in lifecycle._JSON_FIELDS - {"ownership_receipt_json", "preflight_receipt_json"}:
        values[field] = "[]" if field.endswith("argv_json") or field == "dns_expected_addresses_json" else "{}"
    return values


def test_lifecycle_execution_rejects_host_qualified_uid_and_preserves_binding(tmp_path, monkeypatch):
    cfg, _, _, _ = install_fixture(tmp_path)
    binding = lifecycle.installed_binding(cfg)
    command = {
        "binding_schema": binding["binding_schema"], "binding_sha256": binding["binding_sha256"],
        "command_id": "create",
    }
    operation_input = valid_operation_input(binding)
    calls = []
    ownership_receipt = {
        "schema": "mcc-mautic-connected-host-ownership-v1",
        "scenario_revision": REVISION,
        "create_operation_id": operation_input["operation_id"],
        "plan_hash": operation_input["expected_plan_hash"],
        "domain": operation_input["target_dns"],
        "instance_uid": operation_input["target_instance_uid"],
        "managed_root": operation_input["managed_root"],
        "database_name": operation_input["database_name"],
        "database_user": operation_input["database_user"],
        "vhost_available_path": operation_input["vhost_available_path"],
        "vhost_enabled_path": operation_input["vhost_enabled_path"],
    }
    monkeypatch.setattr(lifecycle, "run_configured_external_operation", lambda *a, **kw: calls.append(kw) or (0, json.dumps({
        "schema": "mcc-mautic-connected-host-lifecycle-result-v1",
        "contract_version": "1.0",
        "operation": "instance.create",
        "status": "applied",
        "plan_hash": operation_input["expected_plan_hash"],
        "ownership_receipt": ownership_receipt,
        "ownership_receipt_sha256": hashlib.sha256(canonical(ownership_receipt)).hexdigest(),
        "artifact_binding": {
            "catalog_item_id": lifecycle.CATALOG_ITEM_ID,
            "catalog_item_revision": REVISION,
            "artifact_sha256": binding["artifact_sha256"],
            "profile_sha256": binding["profile_sha256"],
            "binding_sha256": binding["binding_sha256"],
        },
    })))
    job = {"command": command, "input": operation_input}
    assert lifecycle.execute(job, config=cfg)["status"] == "succeeded"
    assert calls[0]["operation_input"]["target_instance_uid"] == "future-instance-1"
    job["input"] = {**operation_input, "target_instance_uid": "future-instance-1@dev135"}
    assert lifecycle.execute(job, config=cfg) == {
        "status": "failed", "reason": "mautic_lifecycle_instance_uid_invalid",
    }
    assert len(calls) == 1
    job["input"] = {
        **operation_input,
        "preflight_receipt_sha256": "4" * 64,
    }
    assert lifecycle.execute(job, config=cfg) == {
        "status": "failed", "reason": "mautic_lifecycle_preflight_receipt_invalid",
    }
    assert len(calls) == 1
