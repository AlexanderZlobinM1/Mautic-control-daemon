"""Small, typed generic primitives exposed by the Portal worker.

This module deliberately contains no shell or caller supplied command line.
Only native observations and an empty-input inventory rescan are exposed here.
A mutating archive install is not advertised until it has its own typed,
artifact-bound contract.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mcd_agent.commercial_detector import (
    PORTAL_LOCAL_FACTS_CAPABILITIES,
    read_normalized_local_facts,
)
from mcd_agent.commercial_generic import capability_inventory_digest
from mcd_agent.install_readiness import collect_mautic_install_readiness
from mcd_agent.config import AgentConfig
from mcd_agent.hardware_profile import read_memory_kib
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.instance_uid import instance_uid_matches, normalize_instance_uid
from mcd_agent.maintenance_mode import collect_maintenance_state
from mcd_agent.upgrade_os_external import upgrade_os_binding_identity


CAPABILITY_VERSION = "1.0"
COMMAND_VERSION = "1.0"
_SNAPSHOT_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")

_COMMANDS = (
    ("host_facts", "read_host_facts", False, "portal://mcd/input/host-facts-v1", "host"),
    ("prerequisites", "check_prerequisites", False, "portal://mcd/input/prerequisites-v1", "host"),
    ("web_php", "check_web_php", False, "portal://mcd/input/web-php-v1", "host"),
    ("mysql", "check_mysql", False, "portal://mcd/input/mysql-v1", "host"),
    ("redis", "check_redis", False, "portal://mcd/input/redis-v1", "host"),
    ("inventory", "rescan_inventory", True, "portal://mcd/input/inventory-rescan-v1", "host"),
)


def _upgrade_os_binding(config: AgentConfig | None) -> dict[str, str] | None:
    if config is None:
        return None
    try:
        binding = upgrade_os_binding_identity(config)
    except RuntimeError:
        return None
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"binding_schema", "binding_sha256"}
        or binding.get("binding_schema") != "mcd-upgrade-os-binding-v1"
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding.get("binding_sha256") or "")) is None
    ):
        return None
    return {"binding_schema": str(binding["binding_schema"]), "binding_sha256": str(binding["binding_sha256"])}


def build_capability_inventory(
    *, installation_id: str, dns_name: str, config: AgentConfig | None = None,
) -> dict[str, Any]:
    """Build the exact secret-free inventory sent to Portal."""
    unsigned: dict[str, Any] = {
        "schema": "portal-mcd-capability-inventory-v1",
        "contract_version": "1.0",
        "request_id": f"capability-{installation_id}-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "installation": {"installation_id": str(installation_id), "dns_name": str(dns_name).strip().lower()},
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capabilities": [
            {
                "capability_id": capability_id,
                "version": CAPABILITY_VERSION,
                "commands": [
                    {
                        "command_id": command_id,
                        "version": COMMAND_VERSION,
                        "execution": "generic_primitive",
                        "mutates_state": mutates_state,
                        "input_schema_ref": schema_ref,
                        "target_scope": target_scope,
                    }
                ],
            }
            for capability_id, command_id, mutates_state, schema_ref, target_scope in sorted(_COMMANDS)
        ],
    }
    if config is not None:
        binding = _upgrade_os_binding(config)
        if binding is not None:
            unsigned["capabilities"].append(
                {
                    "capability_id": "maintenance",
                    "version": CAPABILITY_VERSION,
                    "commands": [
                        {
                            "command_id": "read_service_status",
                            "version": COMMAND_VERSION,
                            "execution": "generic_primitive",
                            "mutates_state": False,
                            "input_schema_ref": "portal://mcd/input/maintenance-service-status-v1",
                            "target_scope": "host",
                            **binding,
                        }
                    ],
                }
            )
            unsigned["capabilities"].append(
                {
                    "capability_id": "mautic_upgrade",
                    "version": CAPABILITY_VERSION,
                    "commands": [
                        {
                            "command_id": "apply",
                            "version": COMMAND_VERSION,
                            "execution": "generic_primitive",
                            "mutates_state": True,
                            "input_schema_ref": "portal://mcd/input/mautic-upgrade-apply-v1",
                            "target_scope": "instance",
                            **binding,
                        }
                    ],
                }
            )
            unsigned["capabilities"].sort(key=lambda item: str(item["capability_id"]))
    unsigned["inventory_sha256"] = capability_inventory_digest(unsigned)
    return unsigned


def _execute_mautic_upgrade(job: Mapping[str, Any], config: AgentConfig | None) -> Mapping[str, Any]:
    value = job.get("input")
    required = {"instance_uid", "target_version", "mode", "upgrade_plan_ref", "upgrade_plan_sha256"}
    if config is None or not isinstance(value, Mapping) or set(value) != required:
        return {"status": "failed", "reason": "commercial_mautic_upgrade_input_invalid"}
    requested_instance_uid = str(value.get("instance_uid") or "").strip()
    try:
        identity = resolve_agent_identity(config)
        host_name = str(identity.get("effective_mcc_host_name") or "").strip()
    except Exception:
        host_name = str(getattr(config, "mcc_host_name", "") or "").strip()
    try:
        instance_uid = normalize_instance_uid(requested_instance_uid, host_name)
    except ValueError:
        return {"status": "failed", "reason": "commercial_mautic_upgrade_instance_host_mismatch"}
    target_version = str(value.get("target_version") or "").strip()
    mode = str(value.get("mode") or "").strip()
    plan_ref = str(value.get("upgrade_plan_ref") or "").strip()
    plan_sha256 = str(value.get("upgrade_plan_sha256") or "").strip()
    if (
        not requested_instance_uid
        or len(requested_instance_uid) > 512
        or not instance_uid
        or len(instance_uid) > 256
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", instance_uid) is None
        or re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", target_version) is None
        or mode not in {"auto", "zip", "composer"}
        or not plan_ref
        or len(plan_ref) > 512
        or re.fullmatch(r"sha256:[0-9a-f]{64}", plan_sha256) is None
    ):
        return {"status": "failed", "reason": "commercial_mautic_upgrade_input_invalid"}

    from mcd_agent.inventory import InstanceInventory, ensure_seeded
    inventory = InstanceInventory(config.state_db_path)
    try:
        ensure_seeded(inventory, config)
        matches = [
            inst for inst in inventory.list_instances()
            if instance_uid_matches(instance_uid, str(inst.instance_uid or ""), host_name)
        ]
    finally:
        inventory.conn.close()
    if len(matches) != 1:
        return {"status": "failed", "reason": "commercial_mautic_upgrade_instance_not_current"}

    from mcd_agent.mautic_upgrade import run_upgrade_apply
    try:
        rc = run_upgrade_apply(
            config=config,
            root=matches[0].root,
            mode=mode,
            yes=True,
            do_backup=True,
            with_system_upgrade=True,
            target_override=target_version,
            allow_minor=True,
            allow_major=True,
            operation_id=str(job.get("job_id") or job.get("command_id") or "portal-upgrade"),
            upgrade_plan_ref=plan_ref,
            upgrade_plan_sha256=plan_sha256,
        )
    except Exception as exc:
        reason = str(exc or "commercial_mautic_upgrade_failed").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,192}", reason):
            reason = "commercial_mautic_upgrade_failed"
        return {
            "status": "failed",
            "reason": reason,
            "instance_uid": instance_uid,
            "target_version": target_version,
            "mode": mode,
            "upgrade_plan": {"ref": plan_ref, "sha256": plan_sha256},
        }
    if rc != 0:
        return {
            "status": "failed",
            "reason": "commercial_mautic_upgrade_failed",
            "instance_uid": instance_uid,
            "target_version": target_version,
            "mode": mode,
            "upgrade_plan": {"ref": plan_ref, "sha256": plan_sha256},
        }
    return {
        "status": "succeeded",
        "instance_uid": instance_uid,
        "target_version": target_version,
        "mode": mode,
        "upgrade_plan": {"ref": plan_ref, "sha256": plan_sha256},
    }


def _execute_maintenance_service_status(
    job: Mapping[str, Any], config: AgentConfig | None,
) -> Mapping[str, Any]:
    value = job.get("input")
    command = job.get("command")
    if (
        config is None
        or not isinstance(value, Mapping)
        or set(value) != {"action"}
        or value.get("action") != "status"
        or not isinstance(command, Mapping)
    ):
        return {"status": "failed", "reason": "commercial_maintenance_status_input_invalid"}
    binding = _upgrade_os_binding(config)
    if binding is None:
        return {"status": "failed", "reason": "commercial_maintenance_status_binding_unavailable"}
    advertised_binding = {
        "binding_schema": command.get("binding_schema"),
        "binding_sha256": command.get("binding_sha256"),
    }
    if advertised_binding != binding:
        return {"status": "failed", "reason": "commercial_maintenance_status_binding_mismatch"}
    try:
        maintenance = collect_maintenance_state(config)
    except Exception:
        return {"status": "failed", "reason": "commercial_maintenance_status_observation_failed"}
    external = maintenance.get("external_receipt") if isinstance(maintenance, Mapping) else None
    if (
        not isinstance(external, Mapping)
        or external.get("component") != "service"
        or external.get("action") != "status"
        or external.get("mutation_started") is not False
    ):
        return {"status": "failed", "reason": "commercial_maintenance_status_receipt_invalid"}
    public_maintenance = {
        key: maintenance.get(key)
        for key in (
            "mode", "paused", "active", "cron_stopped", "cron_service_name",
            "cron_service_active", "cron_marker_present", "checked_at_utc",
        )
    }
    return {
        "status": "succeeded",
        "action": "status",
        "binding": {"schema": binding["binding_schema"], "sha256": binding["binding_sha256"]},
        "maintenance": public_maintenance,
        "external_receipt": dict(external),
    }


def _service_state(names: tuple[str, ...]) -> dict[str, Any]:
    for name in names:
        path = shutil.which("systemctl")
        if not path:
            return {"installed": False, "active": False, "service": "", "reason": "systemctl_unavailable"}
        try:
            proc = subprocess.run([path, "is-active", name], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and (proc.stdout or "").strip() == "active":
            return {"installed": True, "active": True, "service": name}
        try:
            show = subprocess.run([path, "is-enabled", name], capture_output=True, text=True, timeout=5, check=False)
            if show.returncode == 0:
                return {"installed": True, "active": False, "service": name}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"installed": False, "active": False, "service": ""}


def execute_generic_primitive(
    job: Mapping[str, Any], *, inventory_rescan: Callable[[], int] | None = None,
    config: AgentConfig | None = None,
) -> Mapping[str, Any]:
    """Execute one declared primitive without caller-controlled paths or argv.

    The input is intentionally empty for every primitive. Rejecting any
    non-empty input prevents Portal from smuggling a path, command or secret
    into this generic boundary.
    """
    command = str((job.get("command") or {}).get("command_id") or "")
    capability = str((job.get("capability") or {}).get("capability_id") or "")
    if command == "read_service_status" and capability == "maintenance":
        return _execute_maintenance_service_status(job, config)
    if command == "apply" and str((job.get("capability") or {}).get("capability_id") or "") == "mautic_upgrade":
        return _execute_mautic_upgrade(job, config)
    value = job.get("input")
    if not isinstance(value, Mapping) or value:
        return {"status": "failed", "reason": "commercial_generic_input_must_be_empty"}
    if command == "rescan_inventory":
        if inventory_rescan is None:
            return {"status": "failed", "reason": "commercial_inventory_rescan_unavailable"}
        try:
            outcome = inventory_rescan()
        except Exception:
            # Discovery may inspect protected local configuration. Never put
            # an exception containing that material into a command receipt.
            return {"status": "failed", "reason": "commercial_inventory_rescan_failed"}
        reference = None
        if isinstance(outcome, Mapping):
            count = outcome.get("discovered_count")
            reference = outcome.get("snapshot_reference")
        else:
            count = outcome
        if type(count) is not int or count < 0:
            return {"status": "failed", "reason": "commercial_inventory_rescan_result_invalid"}
        result: dict[str, Any] = {"status": "succeeded", "discovered_count": count}
        if reference is not None:
            if not isinstance(reference, Mapping):
                return {"status": "failed", "reason": "commercial_inventory_snapshot_reference_invalid"}
            raw_revision = reference.get("snapshot_revision")
            durable_hash = reference.get("durable_hash")
            if type(raw_revision) is not int or raw_revision < 0 or not isinstance(durable_hash, str) or _SNAPSHOT_HASH_RE.fullmatch(durable_hash) is None:
                return {"status": "failed", "reason": "commercial_inventory_snapshot_reference_invalid"}
            result["snapshot_reference"] = {"snapshot_revision": raw_revision, "durable_hash": durable_hash}
        return result
    if command == "read_host_facts":
        try:
            cpu_count = os.cpu_count()
        except OSError:
            cpu_count = None
        memory_kib = read_memory_kib()
        return {
            "status": "succeeded",
            "facts": read_normalized_local_facts(capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES)),
            "hardware_observation": {
                "cpu_count": cpu_count if type(cpu_count) is int and cpu_count > 0 else None,
                "memory_kib": memory_kib if type(memory_kib) is int and memory_kib > 0 else None,
            },
        }
    readiness = collect_mautic_install_readiness(config)
    if command == "check_prerequisites":
        return {"status": "succeeded", "readiness": readiness}
    if command == "check_web_php":
        return {"status": "succeeded", "web": readiness.get("nginx", {}), "php": readiness.get("php", {})}
    if command == "check_mysql":
        return {"status": "succeeded", "database": readiness.get("database", {})}
    if command == "check_redis":
        return {"status": "succeeded", "redis": _service_state(("redis-server", "redis"))}
    return {"status": "failed", "reason": "commercial_generic_command_not_implemented"}

