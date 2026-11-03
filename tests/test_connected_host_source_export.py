from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
from unittest.mock import patch

from mcd_agent import connected_host_source_export as source_export
from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
from mcd_agent.external_operation_cache import stage_transient_delivery, sync_binding


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
    (root / "manifest.json").write_text('{"manifest_version":1,"items":[]}\n')
    profile = {
        "schema": "mcc-mautic-connected-host-source-export-profile-v1",
        "version": "1.0.0",
        "catalog_item": {"id": source_export.CATALOG_ITEM_ID, "revision": "1.0.0"},
    }
    profile_bytes = canonical(profile) + b"\n"
    profile_sha = hashlib.sha256(profile_bytes).hexdigest()
    profile_file = f"profiles/source-export-{profile_sha}.json"
    bundle = b"fixture-source-export-bundle"
    artifact_sha = hashlib.sha256(bundle).hexdigest()
    key = "mautic-connected-host-source-export-v1.0.0"
    entrypoint = "scenarios/source_export.py"
    mapping = {
        "operator_profile_json": {"encoding": "json", "envelope_key": "profile", "source": "existing_private_operator_profile"},
        "cfg_facts_json": {"encoding": "json", "schema": source_export.CONTEXT_SCHEMA, "source": "mcd_resolved_source_installation_context"},
        "operation_input_json": {"encoding": "json", "envelope_key": "input", "source": "caller_operation_input"},
    }
    operation_binding = {
        "component": source_export.COMPONENT,
        "operation": source_export.COMMAND_ID,
        "artifact": {"entrypoint": entrypoint, "key": key, "sha256": artifact_sha},
        "profile_source": {"type": "cached-json", "file": profile_file, "sha256": profile_sha},
        "args": ["--bound-operation", source_export.COMMAND_ID, "--runtime-input-stdin"],
        "capture_result": True,
        "detach": False,
        "live_output": False,
        "offline_only": True,
        "runtime_input_schema": {"type": "string-map", "fields": list(mapping)},
        "runtime_input_mapping": mapping,
    }
    identity = {
        "schema": source_export.BINDING_SCHEMA,
        "catalog_item_id": source_export.CATALOG_ITEM_ID,
        "catalog_item_revision": "1.0.0",
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
            "capability_id": source_export.CAPABILITY_ID,
            "version": "1.0", "execution": "generic_primitive", "target_scope": "host",
            "commands": [{
                "command_id": source_export.COMMAND_ID, "version": "1.0",
                "execution": "generic_primitive", "mutates_state": False, "target_scope": "host",
                "input_schema_ref": source_export.INPUT_SCHEMA_REF,
            }],
        },
        "operation_bindings": [operation_binding],
        "input_contract": {
            "type": "exact_string_map",
            "allowed_keys": ["source_installation_id", "operation_id"],
            "additional_properties": False,
            "worker_context_schema": source_export.CONTEXT_SCHEMA,
        },
        "receipt_schemas": [source_export.RESULT_SCHEMA, "mcd-self-hosted-executable-output-v1"],
    }
    identity_bytes = canonical(identity) + b"\n"
    binding_sha = "sha256:" + hashlib.sha256(canonical(identity)).hexdigest()
    identity_cache = "bindings/mautic-connected-host-source-export-binding-v1.json"
    artifact_cache = f"downloads/{key}/{key}-{artifact_sha}.tar.gz"
    manifest = {
        "manifest_version": 1,
        "items": [{
            "key": key, "name": "source export", "file": artifact_cache,
            "entrypoint": entrypoint, "sha256": artifact_sha, "run_mode": "direct",
            "interactive": False, "type": "script",
        }],
        "operation_bindings": [operation_binding],
        "resources": [{
            "role": "binding_identity", "file": identity_cache,
            "sha256": hashlib.sha256(identity_bytes).hexdigest(),
            "size_bytes": len(identity_bytes),
            "canonical_json_sha256": binding_sha[7:],
        }, {
            "role": "operator_profile", "file": profile_file,
            "sha256": profile_sha, "size_bytes": len(profile_bytes),
        }],
    }
    manifest_bytes = canonical(manifest) + b"\n"
    payloads = {
        "binding-identity-v1.json": identity_bytes,
        "manifest.json": manifest_bytes,
        Path(profile_file).name: profile_bytes,
        "bundle.tar.gz": bundle,
    }
    descriptor_resources = [
        {"path": name, "role": role, "install_path": install,
         "sha256": hashlib.sha256(payloads[name]).hexdigest(), "size_bytes": len(payloads[name])}
        for name, role, install in (
            ("binding-identity-v1.json", "binding_identity", identity_cache),
            ("manifest.json", "operation_binding_manifest", "manifest.json"),
            (Path(profile_file).name, "operator_profile", profile_file),
            ("bundle.tar.gz", "executable_bundle", artifact_cache),
        )
    ]
    descriptor = {
        "schema": "mcc-external-operation-cache-closure-v1",
        "catalog_item_id": source_export.CATALOG_ITEM_ID,
        "catalog_item_revision": "1.0.0",
        "binding_schema": source_export.BINDING_SCHEMA,
        "binding_sha256": binding_sha,
        "install_mode": "atomic_append_only_preserve_existing",
        "offline_only_after_sync": True,
        "resources": descriptor_resources,
    }
    closure = deterministic_tar({**payloads, "descriptor.json": canonical(descriptor) + b"\n"})
    digest = "sha256:" + hashlib.sha256(closure).hexdigest()
    command = {
        "capability": {"capability_id": "external_operation_cache", "version": "1.0"},
        "command": {"command_id": "sync_binding", "version": "1.0"},
        "input": {
            "catalog_item_id": source_export.CATALOG_ITEM_ID,
            "catalog_item_revision": "1.0.0",
            "closure_digest": digest, "closure_size_bytes": len(closure),
            "closure_media_type": "application/gzip",
            "binding_schema": source_export.BINDING_SCHEMA,
            "binding_sha256": binding_sha,
        },
        "artifact_delivery": {
            "schema": "portal-mcd-transient-artifact-delivery-v1", "contract_version": "1.0",
            "artifacts": [{
                "role": "binding_closure", "artifact_path": "/artifacts/source-export",
                "artifact_grant": "one-time", "artifact_digest": digest,
                "artifact_size_bytes": len(closure), "artifact_media_type": "application/gzip",
                "artifact_revision": "1.0.0", "trusted_catalog": {
                    "source": "mcc", "transport": "portal_authenticated_catalog", "status": "active",
                    "revision": "1.0.0", "artifact_digest": digest,
                },
            }],
        },
    }
    return cfg, command, closure, binding_sha


def test_cache_sync_admits_exact_source_export_binding(tmp_path):
    cfg, command, closure, binding_sha = build_closure(tmp_path)
    sanitized = stage_transient_delivery(command, config=cfg, downloader=lambda **_kwargs: closure)
    assert sync_binding(sanitized, config=cfg)["status"] == "succeeded"
    admitted = source_export.installed_binding(cfg)
    assert admitted["binding_sha256"] == binding_sha
    assert admitted["capability"]["commands"] == [{
        "command_id": "source_export", "version": "1.0", "execution": "generic_primitive",
        "mutates_state": False, "input_schema_ref": source_export.INPUT_SCHEMA_REF,
        "target_scope": "host", "binding_schema": source_export.BINDING_SCHEMA,
        "binding_sha256": binding_sha,
    }]


def test_execute_returns_verified_regular_file_pins_and_removes_defaults(tmp_path):
    output = tmp_path / "private-output"
    final = output / "source-export-one"
    final.mkdir(parents=True)
    files = final / "files.tar.gz"
    database = final / "database.sql.gz"
    files.write_bytes(b"files")
    database.write_bytes(b"database")
    defaults = tmp_path / "database.cnf"
    defaults.write_text("secret")
    context = {
        "schema": source_export.CONTEXT_SCHEMA,
        "source_installation_id": "installation-zlobin2",
        "instance_uid": "zlobin2.sales-snap.com",
        "primary_domain": "zlobin2.sales-snap.com",
        "source_root": "/var/www/zlobin2/public_html",
        "database_name": "baza_zlobin2",
        "output_root": str(output),
        "mysql_dump_argv": ["/usr/bin/mariadb-dump", "--defaults-extra-file=" + str(defaults)],
    }
    nested = {
        "schema": source_export.RESULT_SCHEMA, "contract_version": "1.0", "status": "succeeded",
        "operation": "source.export", "operation_id": "export-op-1",
        "source": {key: context[key] for key in (
            "source_installation_id", "instance_uid", "primary_domain", "source_root", "database_name"
        )},
        "files_archive": {"path": str(files), "sha256": hashlib.sha256(b"files").hexdigest(), "size_bytes": 5, "format": "tar.gz"},
        "database_archive": {"path": str(database), "sha256": hashlib.sha256(b"database").hexdigest(), "size_bytes": 8, "format": "sql.gz"},
        "source_snapshot_sha256": "sha256:" + "a" * 64,
        "excluded_runtime_paths": ["var/cache"], "source_mutation": False, "output_rollback": "not_needed",
    }
    job = {
        "installation": {"installation_id": "installation-zlobin2", "dns_name": "zlobin2.sales-snap.com"},
        "capability": {"capability_id": source_export.CAPABILITY_ID, "version": "1.0"},
        "command": {"command_id": source_export.COMMAND_ID, "version": "1.0",
                    "binding_schema": source_export.BINDING_SCHEMA, "binding_sha256": "sha256:" + "b" * 64},
        "input": {"source_installation_id": "installation-zlobin2", "operation_id": "export-op-1"},
    }
    binding = {"binding_schema": source_export.BINDING_SCHEMA, "binding_sha256": "sha256:" + "b" * 64}
    with patch.object(source_export, "installed_binding", return_value=binding), \
         patch.object(source_export, "_build_context", return_value=(context, str(defaults))), \
         patch.object(source_export, "load_configured_profile_cache", return_value={"profile": {"schema": "fixture"}}), \
         patch.object(source_export, "run_configured_external_operation", return_value=(0, json.dumps(nested))) as run:
        result = source_export.execute(job, config=SimpleNamespace())
    assert result == {"status": "succeeded", "source_export": nested}
    assert not defaults.exists()
    assert run.call_args.kwargs["resolved_context"] == context


def test_execute_rejects_installation_mismatch_before_runner():
    job = {
        "installation": {"installation_id": "installation-zlobin2", "dns_name": "zlobin2.sales-snap.com"},
        "command": {"command_id": source_export.COMMAND_ID, "binding_schema": source_export.BINDING_SCHEMA,
                    "binding_sha256": "sha256:" + "b" * 64},
        "input": {"source_installation_id": "installation-other", "operation_id": "export-op-1"},
    }
    binding = {"binding_schema": source_export.BINDING_SCHEMA, "binding_sha256": "sha256:" + "b" * 64}
    with patch.object(source_export, "installed_binding", return_value=binding), \
         patch.object(source_export, "run_configured_external_operation") as run:
        result = source_export.execute(job, config=SimpleNamespace())
    assert result == {"status": "failed", "reason": "mautic_source_export_installation_binding_invalid"}
    run.assert_not_called()


def test_existing_runner_accepts_read_only_not_needed_rollback():
    frame = {
        "schema": "mcd-self-hosted-executable-output-v1",
        "contract_version": "1.0",
        "frame_type": "result",
        "status": "succeeded",
        "exit_code": 0,
        "completed": 1,
        "total": 1,
        "rollback": "not_needed",
        "result": {"status": "succeeded", "source_mutation": False},
    }
    payload = canonical_json(frame)
    wire = len(payload).to_bytes(4, "big") + payload
    assert ExternalProcessPolicy._decode_output_frames(wire) == [frame]
