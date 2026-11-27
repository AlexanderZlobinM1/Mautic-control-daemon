from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_primitives import build_capability_inventory
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


def test_inventory_advertises_typed_upgrade_scenario_with_existing_binding(monkeypatch):
    from mcd_agent import commercial_primitives as primitives

    binding = {"binding_schema": "mcd-upgrade-os-binding-v1", "binding_sha256": "sha256:" + "a" * 64}
    monkeypatch.setattr(primitives, "upgrade_os_binding_identity", lambda _cfg: binding)
    inventory = build_capability_inventory(
        installation_id="installation-1",
        dns_name="mautic.example.com",
        config=object(),
    )
    capability = next(item for item in inventory["capabilities"] if item["capability_id"] == "mautic_upgrade_scenario_v1")
    assert capability["commands"] == [{
        "command_id": "apply",
        "version": "1.0",
        "execution": "generic_primitive",
        "mutates_state": True,
        "input_schema_ref": "portal://mcd/input/mautic-upgrade-scenario-v1",
        "target_scope": "instance",
        **binding,
    }]


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


def test_execute_returns_mcd_receipt_bindings(monkeypatch, tmp_path: Path):
    from mcd_agent import mautic_upgrade
    from mcd_agent import mautic_upgrade_scenario as scenario

    monkeypatch.setattr(mautic_upgrade, "_pick_install_record", lambda _config, _root: SimpleNamespace(instance_uid="instance-1", root="/var/www/example/public_html"))
    monkeypatch.setattr(mautic_upgrade, "run_upgrade_apply", lambda **_kwargs: 0)
    monkeypatch.setattr(scenario, "validate_scenario_document", lambda document, **_kwargs: document)
    monkeypatch.setattr(scenario, "_safe_projection", lambda value, **kwargs: {"root": kwargs["root"], "instance_uid": kwargs["instance_uid"], "target_version": value["target_version"]})
    monkeypatch.setattr("mcd_agent.mautic_version_cache.read_mautic_version_evidence_read_only", lambda _root: {"version": "7.2.3", "source": "static_metadata"})
    raw = json.dumps(_scenario(_pin(b"placeholder")), sort_keys=True, separators=(",", ":")).encode()
    pin = _pin(raw)
    document = _scenario(pin)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    pin = _pin(raw)
    config = SimpleNamespace(state_db_path=str(tmp_path / "state.sqlite3"))
    result = execute(
        {"job_id": "job-1", "command_id": "apply", "input": {
            "scenario_id": "mautic-upgrade-7-2-3",
            "scenario_revision": "upgrade-r1",
            "scenario_artifact": pin,
            "instance_uid": "instance-1",
            "application_root": "/var/www/example/public_html",
        }},
        config=config,
        scenario_downloader=lambda _pin: raw,
        inventory_rescan=lambda: {"status": "succeeded", "discovered_count": 1},
    )
    assert result["status"] == "succeeded"
    assert result["scenario"]["id"] == "mautic-upgrade-7-2-3"
    assert result["final_version"]["version"] == "7.2.3"
    assert result["inventory_rescan"]["status"] == "succeeded"
