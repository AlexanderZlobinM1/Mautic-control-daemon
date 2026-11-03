from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
from unittest.mock import patch

from mcd_agent import connected_host_prerequisites as prerequisites
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


def build_closure(tmp_path: Path, revision: str = "1.0.7"):
    cfg = SimpleNamespace(custom_cache_dir=str(tmp_path / "custom"))
    root = Path(cfg.custom_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text('{"manifest_version":1,"items":[]}\n')
    profile = {
        "schema": "mcc-mautic-connected-host-prerequisites-profile-v1",
        "version": revision,
        "catalog_item": {"id": prerequisites.CATALOG_ITEM_ID, "revision": revision},
    }
    profile_bytes = canonical(profile) + b"\n"
    profile_sha = hashlib.sha256(profile_bytes).hexdigest()
    profile_file = f"profiles/host-prerequisites-{profile_sha}.json"
    bundle = f"fixture-host-prerequisites-bundle-{revision}".encode()
    artifact_sha = hashlib.sha256(bundle).hexdigest()
    key = f"mautic-connected-host-prerequisites-v{revision}"
    entrypoint = f"scenarios/{revision}/host_prerequisites.py"
    mapping = {
        "operator_profile_json": {"encoding": "json", "envelope_key": "profile", "source": "existing_private_operator_profile"},
        "cfg_facts_json": {"encoding": "json", "schema": prerequisites.CONTEXT_SCHEMA, "source": "mcd_resolved_operation_context"},
        "operation_input_json": {"encoding": "json", "envelope_key": "input", "source": "caller_operation_input"},
    }
    bindings = []
    for command_id in prerequisites.COMMANDS:
        bindings.append({
            "component": prerequisites.COMPONENT,
            "operation": command_id,
            "artifact": {"entrypoint": entrypoint, "key": key, "sha256": artifact_sha},
            "profile_source": {"type": "cached-json", "file": profile_file, "sha256": profile_sha},
            "args": ["--bound-operation", command_id, "--runtime-input-stdin"],
            "capture_result": True,
            "detach": False,
            "live_output": False,
            "offline_only": True,
            "runtime_input_schema": {"type": "string-map", "fields": list(mapping)},
            "runtime_input_mapping": mapping,
        })
    identity = {
        "schema": prerequisites.BINDING_SCHEMA,
        "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
        "catalog_item_revision": revision,
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
            "capability_id": prerequisites.CAPABILITY_ID,
            "version": "1.0", "execution": "generic_primitive", "target_scope": "host",
            "commands": [{
                "command_id": command_id, "version": "1.0",
                "execution": "generic_primitive", "mutates_state": True,
                "target_scope": "host", "input_schema_ref": schema_ref,
            } for command_id, schema_ref in prerequisites.COMMANDS.items()],
        },
        "operation_bindings": bindings,
        "input_contract": {
            "type": "exact_string_map",
            "allowed_keys": sorted(prerequisites.INPUT_FIELDS),
            "additional_properties": False,
            "worker_context_schema": prerequisites.CONTEXT_SCHEMA,
            "rules": {
                "dns_check_mode": "require_exact",
                "dns_expected_addresses_json": "exactly one IPv4",
                "preserve_certificate": "true",
                "target_instance_uid": "equals canonical target_dns FQDN",
            },
        },
        "receipt_schemas": [prerequisites.RESULT_SCHEMA, "mcd-self-hosted-executable-output-v1"],
    }
    identity_bytes = canonical(identity) + b"\n"
    binding_sha = "sha256:" + hashlib.sha256(canonical(identity)).hexdigest()
    identity_cache = "bindings/mautic-connected-host-prerequisites-binding-v1.json"
    artifact_cache = f"downloads/{key}/{key}-{artifact_sha}.tar.gz"
    manifest = {
        "manifest_version": 1,
        "items": [{
            "key": key, "name": "host prerequisites", "file": artifact_cache,
            "entrypoint": entrypoint, "sha256": artifact_sha, "run_mode": "direct",
            "interactive": False, "type": "script",
        }],
        "operation_bindings": bindings,
        "resources": [{
            "role": "binding_identity", "file": identity_cache,
            "sha256": hashlib.sha256(identity_bytes).hexdigest(),
            "size_bytes": len(identity_bytes), "canonical_json_sha256": binding_sha[7:],
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
        "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
        "catalog_item_revision": revision,
        "binding_schema": prerequisites.BINDING_SCHEMA,
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
            "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
            "catalog_item_revision": revision,
            "closure_digest": digest, "closure_size_bytes": len(closure),
            "closure_media_type": "application/gzip",
            "binding_schema": prerequisites.BINDING_SCHEMA,
            "binding_sha256": binding_sha,
        },
        "artifact_delivery": {
            "schema": "portal-mcd-transient-artifact-delivery-v1", "contract_version": "1.0",
            "artifacts": [{
                "role": "binding_closure", "artifact_path": "/artifacts/host-prerequisites",
                "artifact_grant": "one-time", "artifact_digest": digest,
                "artifact_size_bytes": len(closure), "artifact_media_type": "application/gzip",
                "artifact_revision": revision, "trusted_catalog": {
                    "source": "mcc", "transport": "portal_authenticated_catalog", "status": "active",
                    "revision": revision, "artifact_digest": digest,
                },
            }],
        },
    }
    return cfg, command, closure, binding_sha


def test_cache_sync_admits_exact_prerequisites_binding(tmp_path):
    cfg, command, closure, binding_sha = build_closure(tmp_path)
    sanitized = stage_transient_delivery(command, config=cfg, downloader=lambda **_kwargs: closure)
    assert sync_binding(sanitized, config=cfg)["status"] == "succeeded"
    admitted = prerequisites.installed_binding(cfg)
    assert admitted["binding_sha256"] == binding_sha
    assert [item["command_id"] for item in admitted["capability"]["commands"]] == [
        "prepare_create", "preclean_legacy",
    ]


def test_cache_sync_atomically_activates_new_revision_and_preserves_history(tmp_path):
    cfg, old_command, old_closure, _old_binding_sha = build_closure(tmp_path, "1.0.5")
    old_job = stage_transient_delivery(
        old_command, config=cfg, downloader=lambda **_kwargs: old_closure,
    )
    with patch.object(prerequisites, "CATALOG_ITEM_REVISION", "1.0.5"):
        assert sync_binding(old_job, config=cfg)["status"] == "succeeded"
    before = json.loads((Path(cfg.custom_cache_dir) / "manifest.json").read_text())
    old_selector = before["active_bindings"][prerequisites.BINDING_SCHEMA]
    old_identity = Path(cfg.custom_cache_dir) / old_selector["cache_file"]
    old_identity_bytes = old_identity.read_bytes()

    cfg, new_command, new_closure, new_binding_sha = build_closure(tmp_path, "1.0.7")
    new_job = stage_transient_delivery(
        new_command, config=cfg, downloader=lambda **_kwargs: new_closure,
    )
    assert sync_binding(new_job, config=cfg) == {
        "status": "succeeded",
        "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
        "catalog_item_revision": "1.0.7",
        "binding_schema": prerequisites.BINDING_SCHEMA,
        "binding_sha256": new_binding_sha,
        "closure_digest": new_command["input"]["closure_digest"],
    }
    admitted = prerequisites.installed_binding(cfg)
    assert admitted["catalog_item_revision"] == "1.0.7"
    assert admitted["binding_sha256"] == new_binding_sha

    manifest_path = Path(cfg.custom_cache_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert [
        row["key"] for row in manifest["items"]
        if row.get("key", "").startswith("mautic-connected-host-prerequisites-v")
    ] == [
        "mautic-connected-host-prerequisites-v1.0.5",
        "mautic-connected-host-prerequisites-v1.0.7",
    ]
    assert len([
        row for row in manifest["operation_bindings"]
        if row.get("component") == prerequisites.COMPONENT
    ]) == 4
    identity_rows = [
        row for row in manifest["resources"]
        if row.get("role") == "binding_identity"
        and row.get("file") == "bindings/mautic-connected-host-prerequisites-binding-v1.json"
    ]
    assert len(identity_rows) == 2
    assert len({row["cache_file"] for row in identity_rows}) == 2
    assert old_identity.read_bytes() == old_identity_bytes

    before_repeat = manifest_path.read_bytes()
    assert sync_binding(new_job, config=cfg)["status"] == "succeeded"
    assert manifest_path.read_bytes() == before_repeat


def test_cache_sync_revision_drift_fails_before_active_selector_changes(tmp_path):
    cfg, old_command, old_closure, _old_binding_sha = build_closure(tmp_path, "1.0.5")
    old_job = stage_transient_delivery(
        old_command, config=cfg, downloader=lambda **_kwargs: old_closure,
    )
    with patch.object(prerequisites, "CATALOG_ITEM_REVISION", "1.0.5"):
        assert sync_binding(old_job, config=cfg)["status"] == "succeeded"

    cfg, new_command, new_closure, _new_binding_sha = build_closure(tmp_path, "1.0.7")
    with tarfile.open(fileobj=io.BytesIO(new_closure), mode="r:gz") as archive:
        incoming_manifest = json.load(archive.extractfile("manifest.json"))
    identity_sha = next(
        row["sha256"] for row in incoming_manifest["resources"]
        if row.get("role") == "binding_identity"
    )
    conflict = Path(cfg.custom_cache_dir) / "bindings" / "by-sha256" / f"{identity_sha}.json"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"content-addressed drift")
    manifest_path = Path(cfg.custom_cache_dir) / "manifest.json"
    before = manifest_path.read_bytes()

    new_job = stage_transient_delivery(
        new_command, config=cfg, downloader=lambda **_kwargs: new_closure,
    )
    assert sync_binding(new_job, config=cfg) == {
        "status": "failed", "reason": "external_operation_cache_install_conflict",
    }
    assert manifest_path.read_bytes() == before


def input_map(**changes):
    value = {
        "operation_id": "host-prep-1",
        "target_instance_uid": "future.example.com",
        "target_dns": "future.example.com",
        "dns_check_mode": "require_exact",
        "dns_expected_addresses_json": '["192.0.2.1"]',
        "preserve_certificate": "true",
    }
    value.update(changes)
    return value


def test_input_is_exact_and_forbids_host_qualified_uid():
    assert prerequisites._validate_input(input_map())["target_instance_uid"] == "future.example.com"
    for invalid in (
        input_map(target_instance_uid="future@host"),
        {**input_map(), "managed_root": "/var/www/future"},
        input_map(preserve_certificate="false"),
        input_map(dns_expected_addresses_json='[ "192.0.2.1" ]'),
    ):
        try:
            prerequisites._validate_input(invalid)
        except prerequisites.ConnectedHostPrerequisitesError:
            pass
        else:
            raise AssertionError("invalid input accepted")


def test_source_export_roots_include_only_complete_regular_pairs(tmp_path):
    base = tmp_path / "custom" / "operation-output" / "mautic-source-export"
    valid = base / "valid"
    valid.mkdir(parents=True)
    (valid / "files.tar.gz").write_bytes(b"files")
    (valid / "database.sql.gz").write_bytes(b"database")
    incomplete = base / "incomplete"
    incomplete.mkdir()
    (incomplete / "files.tar.gz").write_bytes(b"files")
    (base / "alias").symlink_to(valid, target_is_directory=True)

    assert prerequisites._source_export_roots(SimpleNamespace(custom_cache_dir=str(tmp_path / "custom"))) == [
        str(valid.resolve()),
    ]


def test_preclean_replay_uses_operation_bound_secret_free_tombstone(tmp_path):
    custom = tmp_path / "custom"
    export = custom / "operation-output" / "mautic-source-export" / "source-export-1"
    export.mkdir(parents=True)
    (export / "files.tar.gz").write_bytes(b"files")
    (export / "database.sql.gz").write_bytes(b"database")
    config_file = tmp_path / "mcd.toml"
    config_file.write_text("[agent]\n", encoding="utf-8")
    cli_path = tmp_path / "mcd-cli"
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o755)
    target_root = tmp_path / "var" / "www" / "legacy" / "public_html"
    target_root.mkdir(parents=True)
    row = SimpleNamespace(
        instance_uid="legacy.example.com",
        primary_domain="legacy.example.com",
        domains=["legacy.example.com"],
        root=str(target_root),
        db=SimpleNamespace(name="baza_legacy", user="korisnik_legacy"),
    )
    rows = [row]

    class Inventory:
        def __init__(self, _path):
            self.conn = SimpleNamespace(close=lambda: None)

        def list_instances(self):
            return list(rows)

    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        custom_cache_dir=str(custom),
        config_file_path=str(config_file),
    )
    operation_input = input_map(
        operation_id="preclean-replay-1",
        target_instance_uid="legacy.example.com",
        target_dns="legacy.example.com",
    )
    job = {"installation": {"installation_id": "control-1", "dns_name": "control.example.com"}}
    binding = {
        "catalog_item_revision": "1.0.7",
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "binding_sha256": "sha256:" + "b" * 64,
    }
    with patch.object(prerequisites, "InstanceInventory", Inventory), \
         patch.object(prerequisites.shutil, "which", return_value=str(cli_path)):
        initial = prerequisites._inventory_context("preclean_legacy", operation_input, job, cfg, binding)
        rows.clear()
        replay = prerequisites._inventory_context("preclean_legacy", operation_input, job, cfg, binding)

    assert initial["target_inventory"] == {
        "status": "present",
        "instance_uid": "legacy.example.com",
        "primary_domain": "legacy.example.com",
        "domains": ["legacy.example.com"],
        "root": str(target_root.resolve()),
        "database_name": "baza_legacy",
        "database_user": "korisnik_legacy",
    }
    assert replay["target_inventory"] == {**initial["target_inventory"], "status": "absent"}
    assert replay["source_export_roots"] == [str(export.resolve())]
    tombstones = list((custom / "operation-state" / "mautic-host-prerequisites").glob("*.json"))
    assert len(tombstones) == 1
    assert tombstones[0].stat().st_mode & 0o077 == 0
    payload = json.loads(tombstones[0].read_text(encoding="utf-8"))
    assert "password" not in json.dumps(payload).lower()
    assert payload["operation_id"] == "preclean-replay-1"
    tombstones[0].chmod(0o644)
    with patch.object(prerequisites, "InstanceInventory", Inventory), \
         patch.object(prerequisites.shutil, "which", return_value=str(cli_path)):
        try:
            prerequisites._inventory_context("preclean_legacy", operation_input, job, cfg, binding)
        except prerequisites.ConnectedHostPrerequisitesError as exc:
            assert str(exc) == "mautic_host_prerequisites_tombstone_invalid"
        else:
            raise AssertionError("world-readable tombstone accepted")


def test_preclean_absent_target_without_matching_tombstone_fails_closed(tmp_path):
    custom = tmp_path / "custom"
    export = custom / "operation-output" / "mautic-source-export" / "source-export-1"
    export.mkdir(parents=True)
    (export / "files.tar.gz").write_bytes(b"files")
    (export / "database.sql.gz").write_bytes(b"database")
    config_file = tmp_path / "mcd.toml"
    config_file.write_text("[agent]\n", encoding="utf-8")
    cli_path = tmp_path / "mcd-cli"
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o755)

    class Inventory:
        def __init__(self, _path):
            self.conn = SimpleNamespace(close=lambda: None)

        def list_instances(self):
            return []

    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        custom_cache_dir=str(custom),
        config_file_path=str(config_file),
    )
    binding = {
        "catalog_item_revision": "1.0.7",
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "binding_sha256": "sha256:" + "b" * 64,
    }
    job = {"installation": {"installation_id": "control-1", "dns_name": "control.example.com"}}
    with patch.object(prerequisites, "InstanceInventory", Inventory), \
         patch.object(prerequisites.shutil, "which", return_value=str(cli_path)):
        try:
            prerequisites._inventory_context(
                "preclean_legacy",
                input_map(
                    operation_id="unknown-operation",
                    target_instance_uid="legacy.example.com",
                    target_dns="legacy.example.com",
                ),
                job,
                cfg,
                binding,
            )
        except prerequisites.ConnectedHostPrerequisitesError as exc:
            assert str(exc) == "mautic_host_prerequisites_tombstone_missing"
        else:
            raise AssertionError("absent target without tombstone accepted")


def test_preclean_result_requires_equal_certificate_and_boundary_hashes():
    binding = {
        "catalog_item_revision": "1.0.7",
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "binding_sha256": "sha256:" + "b" * 64,
    }
    operation_input = input_map(operation_id="preclean-1", target_instance_uid="legacy.example.com", target_dns="legacy.example.com")
    nested = {
        "schema": prerequisites.RESULT_SCHEMA,
        "contract_version": "1.0", "status": "applied", "operation": "preclean_legacy",
        "operation_id": "preclean-1",
        "target": {"instance_uid": "legacy.example.com", "dns": "legacy.example.com"},
        "mutation_started": True, "rollback": "not_needed",
        "artifact_binding": {
            "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
            "catalog_item_revision": "1.0.7", "artifact_sha256": "c" * 64,
            "profile_sha256": "d" * 64, "binding_sha256": "sha256:" + "b" * 64,
        },
        "preserved_boundaries": {
            "certificate": {
                "requested": True, "directory": "/etc/letsencrypt/live/legacy.example.com",
                "before_sha256": "sha256:" + "1" * 64,
                "after_sha256": "sha256:" + "1" * 64,
                "disposition": "preserved", "unchanged": True,
            },
            "unrelated": {
                "checked": True, "before_sha256": "sha256:" + "2" * 64,
                "after_sha256": "sha256:" + "2" * 64, "unchanged": True,
            },
        },
        "post_state": {
            "root_absent": True, "vhost_absent": True, "database_absent": True,
            "database_user_absent": True, "inventory_absent": True,
            "certificate_present": True, "nginx_valid": True,
            "mcd_cli_status": "warning", "mcd_cli_instances": 2,
        },
    }
    assert prerequisites._validate_result("preclean_legacy", operation_input, binding, nested) == nested
    nested["preserved_boundaries"]["certificate"]["after_sha256"] = "sha256:" + "3" * 64
    try:
        prerequisites._validate_result("preclean_legacy", operation_input, binding, nested)
    except prerequisites.ConnectedHostPrerequisitesError:
        pass
    else:
        raise AssertionError("changed certificate accepted")


def test_execute_passes_only_mcd_resolved_context_to_existing_runner():
    binding = {
        "binding_schema": prerequisites.BINDING_SCHEMA,
        "binding_sha256": "sha256:" + "b" * 64,
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "catalog_item_revision": "1.0.7",
    }
    operation_input = input_map()
    context = {
        "schema": prerequisites.CONTEXT_SCHEMA,
        "command_id": "prepare_create",
        "control_installation_id": "control-1",
        "control_dns": "control.example.com",
        "mcd_cli_path": "/opt/mcd/venv/bin/mcd-cli",
        "mcd_config_path": "/etc/mcd/mcd.toml",
        "target_inventory": {
            "status": "absent", "instance_uid": "future.example.com",
            "primary_domain": "future.example.com", "domains": ["future.example.com"],
            "root": "", "database_name": "", "database_user": "",
        },
        "source_export_roots": [],
        "artifact_binding": {
            "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
            "catalog_item_revision": "1.0.7",
            "artifact_sha256": "c" * 64,
            "profile_sha256": "d" * 64,
            "binding_sha256": "sha256:" + "b" * 64,
        },
    }
    nested = {
        "schema": prerequisites.RESULT_SCHEMA,
        "contract_version": "1.0", "status": "applied", "operation": "prepare_create",
        "operation_id": "host-prep-1",
        "target": {"instance_uid": "future.example.com", "dns": "future.example.com"},
        "mutation_started": True, "rollback": "not_needed",
        "artifact_binding": {
            "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
            "catalog_item_revision": "1.0.7", "artifact_sha256": "c" * 64,
            "profile_sha256": "d" * 64, "binding_sha256": "sha256:" + "b" * 64,
        },
        "preserved_boundaries": {
            "certificate": {
                "requested": True,
                "directory": "/etc/letsencrypt/live/future.example.com",
                "before_sha256": "",
                "after_sha256": "sha256:" + "e" * 64,
                "disposition": "created",
                "unchanged": False,
            },
            "unrelated": {
                "checked": True,
                "before_sha256": "sha256:" + "f" * 64,
                "after_sha256": "sha256:" + "f" * 64,
                "unchanged": True,
            },
        },
        "post_state": {
            "managed_root_parent": {
                "path": "/var/www/future", "exists": True, "owned_by_operation": True,
            },
            "certificate": {
                "directory": "/etc/letsencrypt/live/future.example.com",
                "fullchain_sha256": "sha256:" + "1" * 64,
                "privkey_sha256": "sha256:" + "2" * 64,
                "covers_target_dns": True,
                "current": True,
            },
            "renewal": {
                "config_path": "/etc/letsencrypt/renewal/future.example.com.conf",
                "webroot_path": "/var/www/future/.well-known/acme-challenge",
                "webroot_exists": True,
                "confined_to_managed_parent": True,
                "stale_temporary_reference_absent": True,
            },
            "temporary_vhost_absent": True,
            "nginx_valid": True,
            "dns": {"mode": "require_exact", "observed_addresses": ["192.0.2.1"]},
        },
    }
    job = {
        "installation": {"installation_id": "control-1", "dns_name": "control.example.com"},
        "command": {"command_id": "prepare_create", "binding_schema": prerequisites.BINDING_SCHEMA,
                    "binding_sha256": binding["binding_sha256"]},
        "input": operation_input,
    }
    with patch.object(prerequisites, "installed_binding", return_value=binding), \
         patch.object(prerequisites, "_inventory_context", return_value=context), \
         patch.object(prerequisites, "load_configured_profile_cache", return_value={"profile": {"schema": "fixture"}}), \
         patch.object(prerequisites, "run_configured_external_operation", return_value=(0, json.dumps(nested))) as run:
        assert prerequisites.execute(job, config=SimpleNamespace()) == {
            "status": "succeeded", "host_prerequisites": nested,
        }
    assert run.call_args.kwargs["resolved_context"] == context
    assert run.call_args.kwargs["operation_input"] == operation_input

    invalid = json.loads(json.dumps(nested))
    invalid["post_state"]["renewal"]["webroot_exists"] = False
    try:
        prerequisites._validate_result("prepare_create", operation_input, binding, invalid)
    except prerequisites.ConnectedHostPrerequisitesError as exc:
        assert str(exc) == "mautic_host_prerequisites_result_invalid"
    else:
        raise AssertionError("missing renewal webroot accepted")

    escaped = json.loads(json.dumps(nested))
    escaped["post_state"]["renewal"]["webroot_path"] = "/var/lib/acme/future.example.com"
    try:
        prerequisites._validate_result("prepare_create", operation_input, binding, escaped)
    except prerequisites.ConnectedHostPrerequisitesError as exc:
        assert str(exc) == "mautic_host_prerequisites_result_invalid"
    else:
        raise AssertionError("renewal webroot outside managed parent accepted")

    repaired = json.loads(json.dumps(nested))
    repaired_certificate = repaired["preserved_boundaries"]["certificate"]
    repaired_certificate.update({
        "before_sha256": "sha256:" + "3" * 64,
        "disposition": "repaired",
        "unchanged": False,
    })
    assert prerequisites._validate_result(
        "prepare_create", operation_input, binding, repaired,
    ) == repaired

    invalid_repair = json.loads(json.dumps(repaired))
    invalid_repair["mutation_started"] = False
    try:
        prerequisites._validate_result(
            "prepare_create", operation_input, binding, invalid_repair,
        )
    except prerequisites.ConnectedHostPrerequisitesError as exc:
        assert str(exc) == "mautic_host_prerequisites_result_invalid"
    else:
        raise AssertionError("non-mutating renewal repair accepted")


def test_execute_preserves_valid_external_failure_evidence():
    binding = {
        "binding_schema": prerequisites.BINDING_SCHEMA,
        "binding_sha256": "sha256:" + "b" * 64,
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "catalog_item_revision": "1.0.7",
    }
    operation_input = input_map()
    nested = {
        "schema": prerequisites.RESULT_SCHEMA,
        "contract_version": "1.0",
        "status": "failed",
        "operation": "prepare_create",
        "operation_id": operation_input["operation_id"],
        "error_code": "certificate_issue_failed",
        "mutation_started": True,
        "rollback": "succeeded",
    }
    frame = {
        "schema": prerequisites.FRAME_SCHEMA,
        "contract_version": "1.0",
        "frame_type": "result",
        "status": "failed",
        "exit_code": 70,
        "completed": 0,
        "total": 1,
        "rollback": "succeeded",
        "result": nested,
        "error_code": "certificate_issue_failed",
    }
    job = {
        "installation": {"installation_id": "control-1", "dns_name": "control.example.com"},
        "command": {
            "command_id": "prepare_create",
            "binding_schema": prerequisites.BINDING_SCHEMA,
            "binding_sha256": binding["binding_sha256"],
        },
        "input": operation_input,
    }
    with patch.object(prerequisites, "installed_binding", return_value=binding), \
         patch.object(prerequisites, "_inventory_context", return_value={}), \
         patch.object(prerequisites, "load_configured_profile_cache", return_value={"profile": {}}), \
         patch.object(prerequisites, "run_configured_external_operation", return_value=(70, json.dumps(frame))):
        assert prerequisites.execute(job, config=SimpleNamespace()) == {
            "status": "failed",
            "reason": "certificate_issue_failed",
            "host_prerequisites": nested,
        }


def test_execute_reduces_invalid_external_failure_output():
    binding = {
        "binding_schema": prerequisites.BINDING_SCHEMA,
        "binding_sha256": "sha256:" + "b" * 64,
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "catalog_item_revision": "1.0.7",
    }
    operation_input = input_map()
    job = {
        "installation": {"installation_id": "control-1", "dns_name": "control.example.com"},
        "command": {
            "command_id": "prepare_create",
            "binding_schema": prerequisites.BINDING_SCHEMA,
            "binding_sha256": binding["binding_sha256"],
        },
        "input": operation_input,
    }
    with patch.object(prerequisites, "installed_binding", return_value=binding), \
         patch.object(prerequisites, "_inventory_context", return_value={}), \
         patch.object(prerequisites, "load_configured_profile_cache", return_value={"profile": {}}), \
         patch.object(prerequisites, "run_configured_external_operation", return_value=(70, "not-json")):
        assert prerequisites.execute(job, config=SimpleNamespace()) == {
            "status": "failed",
            "reason": "mautic_host_prerequisites_external_operation_failed",
        }


def test_execute_preserves_bounded_pre_execution_operation_code():
    binding = {
        "binding_schema": prerequisites.BINDING_SCHEMA,
        "binding_sha256": "sha256:" + "b" * 64,
        "artifact_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "catalog_item_revision": "1.0.7",
    }
    job = {
        "installation": {"installation_id": "control-1", "dns_name": "control.example.com"},
        "command": {
            "command_id": "prepare_create",
            "binding_schema": prerequisites.BINDING_SCHEMA,
            "binding_sha256": binding["binding_sha256"],
        },
        "input": input_map(),
    }
    with patch.object(prerequisites, "installed_binding", return_value=binding), \
         patch.object(prerequisites, "_inventory_context", return_value={}), \
         patch.object(prerequisites, "load_configured_profile_cache", return_value={"profile": {}}), \
         patch.object(prerequisites, "run_configured_external_operation", return_value=(2, "custom_operation_mapping_invalid")):
        assert prerequisites.execute(job, config=SimpleNamespace()) == {
            "status": "failed",
            "reason": "custom_operation_mapping_invalid",
        }


def test_generic_runner_serializes_exact_resolved_operation_context(tmp_path):
    from mcd_agent import custom_scripts

    root = tmp_path / "custom"
    root.mkdir()
    digest = "a" * 64
    mapping = {
        "operator_profile_json": {
            "encoding": "json", "envelope_key": "profile",
            "source": "existing_private_operator_profile",
        },
        "cfg_facts_json": {
            "encoding": "json", "schema": prerequisites.CONTEXT_SCHEMA,
            "source": "mcd_resolved_operation_context",
        },
        "operation_input_json": {
            "encoding": "json", "envelope_key": "input",
            "source": "caller_operation_input",
        },
    }
    binding = {
        "component": prerequisites.COMPONENT,
        "operation": "prepare_create",
        "artifact": {"key": "fixture", "sha256": digest, "entrypoint": "entry.py"},
        "args": ["--bound-operation", "prepare_create", "--runtime-input-stdin"],
        "capture_result": True,
        "detach": False,
        "live_output": False,
        "offline_only": True,
        "runtime_input_schema": {"type": "string-map", "fields": list(mapping)},
        "runtime_input_mapping": mapping,
    }
    (root / "manifest.json").write_text(
        json.dumps({"operation_bindings": [binding]}),
        encoding="utf-8",
    )
    context = {
        "schema": prerequisites.CONTEXT_SCHEMA,
        "command_id": "prepare_create",
        "control_installation_id": "control-1",
        "control_dns": "control.example.com",
        "mcd_cli_path": "/opt/mcd/venv/bin/mcd-cli",
        "mcd_config_path": "/etc/mcd/mcd.toml",
        "target_inventory": {
            "status": "absent", "instance_uid": "future.example.com",
            "primary_domain": "future.example.com", "domains": ["future.example.com"],
            "root": "", "database_name": "", "database_user": "",
        },
        "source_export_roots": [],
        "artifact_binding": {
            "catalog_item_id": prerequisites.CATALOG_ITEM_ID,
            "catalog_item_revision": "1.0.7",
            "artifact_sha256": digest,
            "profile_sha256": "b" * 64,
            "binding_sha256": "sha256:" + "c" * 64,
        },
    }
    calls = []
    with patch.object(
        custom_scripts,
        "run_custom_script_by_key",
        side_effect=lambda config, **kwargs: calls.append(kwargs) or (0, '{}'),
    ):
        rc, output = custom_scripts.run_configured_external_operation(
            SimpleNamespace(custom_cache_dir=str(root)),
            component=prerequisites.COMPONENT,
            operation="prepare_create",
            profile={"schema": "fixture"},
            operation_input=input_map(),
            resolved_context=context,
        )
    assert (rc, output) == (0, '{}')
    assert json.loads(calls[0]["runtime_input"]["cfg_facts_json"]) == context
