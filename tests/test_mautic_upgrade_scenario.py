from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_primitives import build_capability_inventory, execute_generic_primitive
from mcd_agent.mautic_upgrade_scenario import (
    SCENARIO_SCHEMA,
    execute,
    load_scenario_artifact,
    validate_scenario_document,
)


def _pin(raw: bytes, revision: str = "upgrade-r1") -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return {
        "artifact_path": "/api/v1/commercial/artifacts/upgrade-r1",
        "artifact_grant": "grant-1",
        "artifact_digest": digest,
        "artifact_size_bytes": len(raw),
        "artifact_media_type": "application/json",
        "artifact_revision": revision,
        "artifact_trust_mode": "mcc_catalog_sha256",
        "artifact_transport": "mcc_authenticated_api",
        "trusted_catalog": {
            "source": "mcc",
            "transport": "mcc_authenticated_api",
            "status": "active",
            "revision": revision,
            "artifact_digest": digest,
        },
    }


def _scenario(pin: dict[str, object]) -> dict[str, object]:
    plan = {
        "schema": "mcd-mautic-patch-plan-v3",
        "registry_commit": "a" * 40,
        "registry_sha256": "b" * 64,
        "source_version": "7.2.2",
        "target_version": "7.2.3",
        "install_type": "composer",
        "trigger": "upgrade_lifecycle",
        "phase": "dependency_update_preflight",
        "operation": "apply",
        "run_id": "run-1",
        "patches": [],
        "payloads": [],
    }
    document = {
        "schema": SCENARIO_SCHEMA,
        "contract_version": "1.0",
        "scenario_id": "mautic-upgrade-7-2-3",
        "scenario_revision": "upgrade-r1",
        "scenario_digest": "",
        "instance_uid": "instance-1",
        "application_root": "/var/www/example/public_html",
        "source_version": "7.2.2",
        "target_version": "7.2.3",
        "cli": {
            "mode": "composer",
            "yes": True,
            "backup": True,
            "with_system_upgrade": False,
            "allow_minor": True,
            "allow_major": False,
            "allow_release_transition": False,
            "mcc_preflighted_single_instance": True,
        },
        "patch": {
            "catalog_revision": "catalog-r1",
            "catalog_sha256": "sha256:" + "c" * 64,
            "plan_sha256": "sha256:" + hashlib.sha256(canonical_json(plan)).hexdigest(),
            "run_id": "run-1",
            "plan": plan,
        },
        "post": {"cache_clear": True, "inventory_rescan": True},
    }
    document["scenario_digest"] = "sha256:" + hashlib.sha256(
        canonical_json({key: value for key, value in document.items() if key != "scenario_digest"})
    ).hexdigest()
    return document


def test_inventory_advertises_only_standard_upgrade_command(monkeypatch):
    from mcd_agent import commercial_primitives as primitives

    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    inventory = build_capability_inventory(
        installation_id="installation-1",
        dns_name="mautic.example.com",
        config=object(),
    )
    capability = next(item for item in inventory["capabilities"] if item["capability_id"] == "mautic_upgrade")
    assert capability["commands"] == [{
        "command_id": "apply",
        "version": "1.0",
        "execution": "generic_primitive",
        "mutates_state": True,
        "input_schema_ref": "portal://mcd/input/mautic-upgrade-apply-v1",
        "target_scope": "instance",
        **binding,
    }]
    assert not any(item["capability_id"] == "mautic_upgrade_scenario_v1" for item in inventory["capabilities"])


def test_standard_upgrade_command_routes_minimal_input_to_mcd_scenario_executor(monkeypatch):
    from mcd_agent import mautic_upgrade_scenario as scenario

    calls = []
    monkeypatch.setattr(
        scenario,
        "execute",
        lambda job, **kwargs: calls.append((job, kwargs)) or {"status": "succeeded", "owner": "mcd"},
    )
    config = object()
    job = {
        "job_id": "upgrade-1",
        "capability": {"capability_id": "mautic_upgrade"},
        "command": {"command_id": "apply"},
        "input": {"instance_uid": "instance-1"},
    }
    assert execute_generic_primitive(job, config=config) == {"status": "succeeded", "owner": "mcd"}
    assert calls and calls[0][0] is job
    assert calls[0][1]["config"] is config


def test_standard_upgrade_command_rejects_scenario_parameters_from_caller(monkeypatch):
    from mcd_agent import mautic_upgrade_scenario as scenario

    called = []
    monkeypatch.setattr(scenario, "execute", lambda *_args, **_kwargs: called.append(True))
    result = execute_generic_primitive(
        {
            "capability": {"capability_id": "mautic_upgrade"},
            "command": {"command_id": "apply"},
            "input": {"instance_uid": "instance-1", "scenario_id": "caller-controlled"},
        },
        config=object(),
    )
    assert result == {"status": "failed", "reason": "commercial_mautic_upgrade_input_invalid"}
    assert called == []


def test_parameter_projection_contains_scenario_and_artifact_bindings():
    from mcd_agent.mautic_upgrade_scenario import _safe_projection

    pin = _pin(b"scenario")
    document = _scenario(pin)
    projection = _safe_projection(
        document,
        root=document["application_root"],
        instance_uid=document["instance_uid"],
        artifact_digest=pin["artifact_digest"],
    )
    assert projection["scenario_id"] == document["scenario_id"]
    assert projection["scenario_revision"] == document["scenario_revision"]
    assert projection["scenario_digest"] == document["scenario_digest"]
    assert projection["artifact_digest"] == pin["artifact_digest"]
    assert projection["patch_plan_sha256"] == document["patch"]["plan_sha256"]


def test_scenario_artifact_uses_local_exact_cache_without_download(tmp_path: Path):
    raw = json.dumps({"schema": SCENARIO_SCHEMA}, sort_keys=True).encode()
    pin = _pin(raw)
    config = SimpleNamespace(state_db_path=str(tmp_path / "state.sqlite3"))
    first, _ = load_scenario_artifact(
        config,
        pin,
        scenario_id="mautic-upgrade-7-2-3",
        scenario_revision="upgrade-r1",
        idempotency_key="job-1",
        downloader=lambda _pin: raw,
    )
    assert first["schema"] == SCENARIO_SCHEMA
    second, delivery = load_scenario_artifact(
        config,
        pin,
        scenario_id="mautic-upgrade-7-2-3",
        scenario_revision="upgrade-r1",
        idempotency_key="job-2",
        downloader=lambda _pin: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert second == first
    assert delivery["status"] == "local_cache_hit"


def test_local_catalog_runs_when_mcc_resolution_is_unavailable(monkeypatch, tmp_path: Path):
    from mcd_agent import mautic_upgrade_scenario as scenario

    config = SimpleNamespace(state_db_path=str(tmp_path / "state.sqlite3"))
    document = _scenario(_pin(b"placeholder"))
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    pin = _pin(raw)
    store = scenario._scenario_store(config, "instance-1")
    store.deliver(
        pin=pin,
        artifact=raw,
        verify=lambda candidate, _payload: {
            "artifact_revision": candidate["artifact_revision"],
            "artifact_digest": candidate["artifact_digest"],
        },
        binding={"scenario_id": document["scenario_id"], "scenario_revision": document["scenario_revision"], "idempotency_key": "seed"},
    )
    monkeypatch.setattr(scenario, "read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.2", "source": "static_metadata"})
    resolved = scenario._resolve_scenario(
        config,
        instance_uid="instance-1",
        application_root=document["application_root"],
        resolver=lambda _request: (_ for _ in ()).throw(RuntimeError("offline")),
        allow_local=True,
    )
    assert resolved[0:2] == ("mautic-upgrade-7-2-3", "upgrade-r1")
    assert resolved[2]["artifact_trust_mode"] == "local_file"


def test_validate_scenario_binds_plan_and_post_steps(monkeypatch):
    from mcd_agent import mautic_patch_plan

    monkeypatch.setattr(mautic_patch_plan, "validate_upgrade_plan", lambda *_args: "ok")
    raw = b"scenario"
    pin = _pin(raw)
    document = _scenario(pin)
    validated = validate_scenario_document(
        document,
        pin=pin,
        scenario_id="mautic-upgrade-7-2-3",
        scenario_revision="upgrade-r1",
        instance_uid="instance-1",
        application_root="/var/www/example/public_html",
    )
    assert validated["patch"]["plan_sha256"].startswith("sha256:")
    assert validated["post"] == {"cache_clear": True, "inventory_rescan": True}


def test_registered_manual_instance_is_current_for_uid_only_upgrade(tmp_path: Path):
    from mcd_agent.inventory import InstanceInventory
    from mcd_agent import mautic_upgrade_scenario as scenario

    root = tmp_path / "public_html"
    console = root / "bin" / "console"
    console.parent.mkdir(parents=True)
    console.write_text("#!/usr/bin/env php\n", encoding="utf-8")
    state_path = tmp_path / "state" / "mcd-state.db"
    inventory = InstanceInventory(str(state_path))
    try:
        inventory.add_or_update_manual(
            name="MCD 1.7.34 sandbox",
            root=str(root),
            console_path=str(console),
            local_php_path=None,
            mautic_major=7,
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_password=None,
            db_table_prefix=None,
            instance_uid="mcd1734-720-20261128",
            runtime_php_bin="/usr/bin/php8.4",
            runtime_user="www-data",
        )
    finally:
        inventory.conn.close()

    selected = scenario._install_for_uid(
        SimpleNamespace(state_db_path=str(state_path)),
        "mcd1734-720-20261128",
    )
    assert selected.instance_uid == "mcd1734-720-20261128"
    assert selected.root == str(root)
    assert selected.source == "manual"
    assert selected.runtime_php_bin == "/usr/bin/php8.4"


def test_direct_upgrade_resolves_registered_manual_root_outside_discovery(tmp_path: Path, monkeypatch):
    from mcd_agent.inventory import InstanceInventory
    from mcd_agent import mautic_upgrade

    root = tmp_path / "manual" / "public_html"
    console = root / "bin" / "console"
    console.parent.mkdir(parents=True)
    console.write_text("#!/usr/bin/env php\n", encoding="utf-8")
    state_path = tmp_path / "state" / "mcd-state.db"
    inventory = InstanceInventory(str(state_path))
    try:
        inventory.add_or_update_manual(
            name="manual-instance",
            root=str(root),
            console_path=str(console),
            local_php_path=None,
            mautic_major=7,
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_password=None,
            db_table_prefix=None,
            instance_uid="manual-instance",
            runtime_php_bin="/usr/bin/php8.4",
            runtime_user="www-data",
        )
    finally:
        inventory.conn.close()

    monkeypatch.setattr(mautic_upgrade, "discover_mautic", lambda *_args: [])
    selected = mautic_upgrade._pick_install_record(
        SimpleNamespace(
            state_db_path=str(state_path),
            discovery_roots=[],
            exclude_path_contains=[],
            supported_mautic_majors=[7],
            custom_instances=[],
        ),
        str(root),
    )

    assert selected.instance_uid == "manual-instance"
    assert selected.root == str(root)
    assert selected.source == "manual"


def test_execute_returns_mcd_receipt_bindings(monkeypatch, tmp_path: Path):
    from mcd_agent import mautic_upgrade
    from mcd_agent import mautic_upgrade_scenario as scenario

    monkeypatch.setattr(scenario, "_install_for_uid", lambda _config, _uid: SimpleNamespace(instance_uid="instance-1", root="/var/www/example/public_html"))
    monkeypatch.setattr(mautic_upgrade, "run_upgrade_apply", lambda **_kwargs: 0)
    monkeypatch.setattr(scenario, "validate_scenario_document", lambda document, **_kwargs: document)
    monkeypatch.setattr(scenario, "_safe_projection", lambda value, **kwargs: {"root": kwargs["root"], "instance_uid": kwargs["instance_uid"], "target_version": value["target_version"]})
    monkeypatch.setattr(scenario, "read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.2", "source": "static_metadata"})
    monkeypatch.setattr("mcd_agent.mautic_version_cache.read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.3", "source": "static_metadata"})
    raw = json.dumps(_scenario(_pin(b"placeholder")), sort_keys=True, separators=(",", ":")).encode()
    pin = _pin(raw)
    document = _scenario(pin)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    pin = _pin(raw)
    config = SimpleNamespace(state_db_path=str(tmp_path / "state.sqlite3"))
    resolved_requests = []

    def resolve(request):
        resolved_requests.append(dict(request))
        return {
            "status": "matched",
            "scenario_id": "mautic-upgrade-7-2-3",
            "scenario_revision": "upgrade-r1",
            "scenario_artifact": pin,
        }

    result = execute(
        {"job_id": "job-1", "command_id": "apply", "input": {
            "instance_uid": "instance-1",
        }},
        config=config,
        scenario_downloader=lambda _pin: raw,
        scenario_resolver=resolve,
        inventory_rescan=lambda: {"status": "succeeded", "discovered_count": 1},
    )
    assert result["status"] == "succeeded"
    assert result["scenario"]["id"] == "mautic-upgrade-7-2-3"
    assert result["final_version"]["version"] == "7.2.3"
    assert result["inventory_rescan"]["status"] == "succeeded"
    assert resolved_requests == [{
        "operation": "mautic-upgrade",
        "instance_uid": "instance-1",
        "source_version": "7.2.2",
    }]


def test_execute_uses_state_synced_external_scenario_runner(monkeypatch, tmp_path: Path):
    from mcd_agent import commercial_self_hosted
    from mcd_agent import mautic_upgrade_scenario as scenario
    from mcd_agent.inventory import InstanceInventory

    artifact = tmp_path / "mautic-upgrade.sh"
    artifact.write_bytes(b"#!/bin/sh\nexit 0\n")
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    pin = {
        "artifact_path": str(artifact),
        "artifact_grant": "mcc-state-sync",
        "artifact_digest": digest,
        "artifact_size_bytes": artifact.stat().st_size,
        "artifact_media_type": "application/x-sh",
        "artifact_revision": "7.2.3",
        "artifact_trust_mode": "mcc_catalog_sha256",
        "artifact_transport": "mcc_authenticated_api",
        "trusted_catalog": {"source": "mcc", "transport": "mcc_authenticated_api", "status": "active", "revision": "7.2.3", "artifact_digest": digest},
    }
    root = tmp_path / "public_html"
    console = root / "bin" / "console"
    console.parent.mkdir(parents=True)
    console.write_text("#!/usr/bin/env php\n", encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    inventory = InstanceInventory(str(state_path))
    try:
        inventory.add_or_update_manual(
            name="instance-1",
            root=str(root),
            console_path=str(console),
            local_php_path=None,
            mautic_major=7,
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_password=None,
            db_table_prefix=None,
            instance_uid="instance-1",
            runtime_php_bin="/usr/bin/php8.4",
            runtime_user="www-data",
        )
    finally:
        inventory.conn.close()
    config = SimpleNamespace(state_db_path=str(state_path), command_timeout_sec=60)

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **_kwargs):
            return {"status": "succeeded", "runner": {"status": "succeeded"}}

    monkeypatch.setattr(scenario, "local_scenario", lambda *_args: {"id": "mautic-upgrade", "version": "7.2.3", "pin": pin})
    monkeypatch.setattr(scenario, "read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.2", "source": "static_metadata"})
    monkeypatch.setattr(scenario, "_latest_upgrade_target", lambda *_args: (_ for _ in ()).throw(AssertionError("offline path must not resolve target")))
    monkeypatch.setattr(scenario, "_resolve_patch_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "composer")
    context_args = {}
    monkeypatch.setattr(
        scenario,
        "_external_scenario_context",
        lambda **kwargs: (context_args.update(kwargs) or {"job": {"job_id": "job-1"}, "scenario": {}}, {}),
    )
    monkeypatch.setattr(commercial_self_hosted, "ExternalProcessPolicy", FakeRunner)
    monkeypatch.setattr(commercial_self_hosted, "default_scenario_runner_argv", lambda: ["runner"])
    monkeypatch.setattr(scenario, "read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.3", "source": "static_metadata"})

    result = scenario.execute(
        {"job_id": "job-1", "command_id": "apply", "input": {"instance_uid": "instance-1"}},
        config=config,
        scenario_downloader=None,
        scenario_resolver=None,
        inventory_rescan=lambda: {"status": "succeeded"},
    )
    assert result["status"] == "succeeded"
    assert result["scenario"]["artifact_digest"] == digest
    assert result["cache_clear"]["status"] == "scenario_owned"
    assert context_args["target_version"] == ""


def test_external_scenario_failure_preserves_bounded_runner_receipt(monkeypatch, tmp_path: Path):
    from mcd_agent import commercial_self_hosted
    from mcd_agent import mautic_upgrade_scenario as scenario
    from mcd_agent.inventory import InstanceInventory

    artifact = tmp_path / "mautic-upgrade.sh"
    artifact.write_bytes(b"#!/bin/sh\nexit 70\n")
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    pin = {
        "artifact_path": str(artifact),
        "artifact_grant": "mcc-state-sync",
        "artifact_digest": digest,
        "artifact_size_bytes": artifact.stat().st_size,
        "artifact_media_type": "application/x-sh",
        "artifact_revision": "1.0.0",
        "artifact_trust_mode": "mcc_catalog_sha256",
        "artifact_transport": "mcc_authenticated_api",
        "trusted_catalog": {
            "source": "mcc",
            "transport": "mcc_authenticated_api",
            "status": "active",
            "revision": "1.0.0",
            "artifact_digest": digest,
        },
    }
    root = tmp_path / "public_html"
    console = root / "bin" / "console"
    console.parent.mkdir(parents=True)
    console.write_text("#!/usr/bin/env php\n", encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    inventory = InstanceInventory(str(state_path))
    try:
        inventory.add_or_update_manual(
            name="instance-1",
            root=str(root),
            console_path=str(console),
            local_php_path=None,
            mautic_major=7,
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_password=None,
            db_table_prefix=None,
            instance_uid="instance-1",
            runtime_php_bin="/usr/bin/php8.4",
            runtime_user="www-data",
        )
    finally:
        inventory.conn.close()

    class FailingRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **_kwargs):
            raise commercial_self_hosted.RunnerExecutionError(
                "upgrade_failed",
                evidence={
                    "schema": "mcd-self-hosted-runner-evidence-v1",
                    "admission_stage": "result_decode",
                    "exit_code": 70,
                },
                result={
                    "status": "failed",
                    "error_code": "upgrade_failed",
                    "result": {
                        "failed_stage": "assetmapper_verification",
                        "nested_exit_code": 1,
                        "assetmapper": {
                            "status": "failed",
                            "reason": "manifest_http_unavailable",
                            "attempts": 3,
                        },
                    },
                },
            )

    config = SimpleNamespace(state_db_path=str(state_path), command_timeout_sec=60)
    monkeypatch.setattr(scenario, "local_scenario", lambda *_args: {"id": "mautic-upgrade", "version": "1.0.0", "pin": pin})
    monkeypatch.setattr(scenario, "read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.0", "source": "static_metadata"})
    monkeypatch.setattr(scenario, "_latest_upgrade_target", lambda *_args: (_ for _ in ()).throw(AssertionError("offline path must not resolve target")))
    monkeypatch.setattr(scenario, "_resolve_patch_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "composer")
    monkeypatch.setattr(
        scenario,
        "_external_scenario_context",
        lambda **_kwargs: ({"job": {"job_id": "job-1"}, "scenario": {}}, {}),
    )
    monkeypatch.setattr(commercial_self_hosted, "ExternalProcessPolicy", FailingRunner)
    monkeypatch.setattr(commercial_self_hosted, "default_scenario_runner_argv", lambda: ["runner"])

    result = scenario.execute(
        {"job_id": "job-1", "command_id": "apply", "input": {"instance_uid": "instance-1"}},
        config=config,
        scenario_downloader=None,
        scenario_resolver=None,
        inventory_rescan=lambda: {"status": "succeeded"},
    )

    assert result["status"] == "failed"
    assert result["reason"] == "upgrade_failed"
    assert result["scenario"]["artifact_digest"] == digest
    assert result["runner"]["error_code"] == "upgrade_failed"
    assert result["runner"]["evidence"]["exit_code"] == 70
    assert result["runner"]["result"]["result"]["failed_stage"] == "assetmapper_verification"
    assert result["runner"]["result"]["result"]["assetmapper"]["attempts"] == 3


def test_external_scenario_context_covers_mautic_72_patch_matrix(monkeypatch):
    from mcd_agent import commercial_detector
    from mcd_agent import mautic_upgrade_scenario as scenario

    monkeypatch.setattr(
        commercial_detector,
        "read_normalized_applicability_facts",
        lambda **_kwargs: {"schema": "mcd-portal-applicability-facts-v1", "facts": []},
    )
    monkeypatch.setattr(
        commercial_detector,
        "applicability_facts_digest",
        lambda _facts: "sha256:" + "1" * 64,
    )
    install = SimpleNamespace(
        root="/var/www/example/public_html",
        console_path="/var/www/example/public_html/bin/console",
        runtime_php_bin="/usr/bin/php8.4",
        runtime_user="www-data",
    )
    config = SimpleNamespace(
        php_bin="/usr/bin/php8.4",
        composer_bin="/usr/local/bin/composer",
        mautic_run_as_user="www-data",
    )
    for source_version in ("7.2.0", "7.2.1", "7.2.2"):
        context, facts = scenario._external_scenario_context(
            config=config,
            install=install,
            instance_uid="instance-1",
            scenario_id="mautic-upgrade",
            scenario_revision="7.2.3",
            artifact_digest="sha256:" + "a" * 64,
            source_version=source_version,
            target_version="7.2.3",
            install_type="composer",
            run_id="matrix-" + source_version.replace(".", "-"),
        )
        assert context["parameters"]["source_version"] == source_version
        assert context["parameters"]["target_version"] == "7.2.3"
        assert context["scenario"]["artifact_digest"] == "sha256:" + "a" * 64
        assert facts["schema"] == "mcd-portal-applicability-facts-v1"
