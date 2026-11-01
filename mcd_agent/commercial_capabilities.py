from __future__ import annotations

import hashlib
from typing import Any, Mapping

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_onboarding import FORBIDDEN_PRIMITIVES, ONBOARDING_RECEIPTS, PRIMITIVE_VERSIONS, ONBOARDING_STAGE_KINDS
from mcd_agent.commercial_generic import validate_managed_installation_tenant


SCHEMA = "mcd-capability-manifest-v1"


def build_capability_manifest(*, agent_version: str, source_commit: str = "", runtime_archive_sha256: str = "") -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "contract_version": "1.0",
        "agent_version": str(agent_version),
        "capabilities": ["portal_scenario_execution_v1", "mcd_passive_activation_v1"],
        "primitives": [{"id": kind, "version": PRIMITIVE_VERSIONS[kind]} for kind in ONBOARDING_STAGE_KINDS],
        "receipts": {"stage": sorted(ONBOARDING_RECEIPTS), "terminal": "mcd-onboarding-terminal-receipt-v1", "preflight": "portal-self-hosted-preflight-receipt-v1"},
        "requirements": {"idempotency": "required", "replay": "return_existing_receipt", "rollback": "checkpoint_receipt_required", "transport_authentication": "commercial_api_key_https", "artifact_integrity": "sha256", "cron_ownership": "none"},
        "platform": {"os_ids": ["external_portal_catalog"], "architectures": ["amd64", "arm64"], "target_kinds": ["new_server", "existing_server"]},
        "forbidden_operations": sorted(FORBIDDEN_PRIMITIVES),
        "provenance": {"source_commit": str(source_commit), "runtime_archive_sha256": str(runtime_archive_sha256)},
    }
    manifest["manifest_sha256"] = "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def mautic_version_capability() -> dict[str, Any]:
    """Advertise the existing registered-UID Mautic core observation."""
    return {"capability_id": "mautic", "version": "1.0", "commands": [{"command_id": "mautic_version", "version": "1.0", "execution": "generic_primitive", "mutates_state": False, "input_schema_ref": "portal://mcd/input/mautic-version-v1"}]}


def execute_mautic_version(job: dict[str, Any], *, config: Any) -> dict[str, Any]:
    if job.get("input") != {}:
        return {"status": "failed", "reason": "commercial_mautic_version_input_must_be_empty"}
    if (job.get("capability") or {}).get("capability_id") != "mautic" or (job.get("command") or {}).get("command_id") != "mautic_version":
        return {"status": "failed", "reason": "commercial_mautic_version_command_invalid"}
    from mcd_agent.cli import _resolve_registered_command_runtime, _run_manual_command_with_scheduler
    uid = ""
    try:
        uid = resolve_portal_command_uid(job, config=config)
        root, php, user = _resolve_registered_command_runtime(config, uid)
    except ValueError:
        return {"status": "failed", "reason": "commercial_mautic_version_runtime_unavailable", "instance_uid": uid}
    rc, output = _run_manual_command_with_scheduler(cfg=config, php_bin=php, root=root, command="version", instance_id=None, timeout_sec=config.command_timeout_sec, run_as_user=user, console_args=[])
    return {"status": "succeeded" if rc == 0 else "failed", "instance_uid": uid, "returncode": rc, "output": str(output)[-2048:]}


def resolve_portal_command_uid(job: dict[str, Any], *, config: Any) -> str:
    """Map an authenticated installation binding to registered identity, not its display DNS."""
    from mcd_agent.commercial_portal import CommercialState, portal_state_identity
    from mcd_agent.inventory import InstanceInventory
    state = CommercialState(config.state_db_path, read_only=True)
    target = str((job.get("installation") or {}).get("installation_id") or "")
    dns = str((job.get("installation") or {}).get("dns_name") or "")
    installations = state.get("installation_ids")
    matches = [uid for uid, value in installations.items() if str(value) == target] if isinstance(installations, dict) else []
    if len(matches) != 1 or portal_state_identity(state, installation_id=target).get("local_identity") != dns:
        raise ValueError("commercial_command_installation_binding_invalid")
    if state.get("ownership") == "sales_snap_managed":
        validate_managed_installation_tenant(
            job["installation"],
            installation_ids=installations,
            managed_instances=state.get("managed_instances") if isinstance(state.get("managed_instances"), Mapping) else {},
        )
        return str(matches[0])
    inventory = InstanceInventory(config.state_db_path)
    try:
        rows = [row for row in inventory.list_instances() if dns in {str(value).strip().lower() for value in [row.primary_domain, *(row.domains or [])] if value}]
    finally:
        inventory.conn.close()
    if len(rows) != 1:
        raise ValueError("commercial_command_instance_identity_ambiguous")
    return rows[0].instance_uid
