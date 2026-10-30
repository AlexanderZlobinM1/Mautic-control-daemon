"""Small, typed generic primitives exposed by the Portal worker.

This module deliberately contains no shell or caller supplied command line.
Only native observations and an empty-input inventory rescan are exposed here.
A mutating archive install is not advertised until it has its own typed,
artifact-bound contract.
"""

from __future__ import annotations

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


CAPABILITY_VERSION = "1.0"
COMMAND_VERSION = "1.0"

_COMMANDS = (
    ("host_facts", "read_host_facts", False, "portal://mcd/input/host-facts-v1"),
    ("prerequisites", "check_prerequisites", False, "portal://mcd/input/prerequisites-v1"),
    ("web_php", "check_web_php", False, "portal://mcd/input/web-php-v1"),
    ("mysql", "check_mysql", False, "portal://mcd/input/mysql-v1"),
    ("redis", "check_redis", False, "portal://mcd/input/redis-v1"),
    ("inventory", "rescan_inventory", True, "portal://mcd/input/inventory-rescan-v1"),
)


def build_capability_inventory(*, installation_id: str, dns_name: str) -> dict[str, Any]:
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
                    }
                ],
            }
            for capability_id, command_id, mutates_state, schema_ref in sorted(_COMMANDS)
        ],
    }
    unsigned["inventory_sha256"] = capability_inventory_digest(unsigned)
    return unsigned


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


def execute_generic_primitive(job: Mapping[str, Any], *, inventory_rescan: Callable[[], int] | None = None) -> Mapping[str, Any]:
    """Execute one declared primitive without caller-controlled paths or argv.

    The input is intentionally empty for every primitive. Rejecting any
    non-empty input prevents Portal from smuggling a path, command or secret
    into this generic boundary.
    """
    command = str((job.get("command") or {}).get("command_id") or "")
    value = job.get("input")
    if not isinstance(value, Mapping) or value:
        return {"status": "failed", "reason": "commercial_generic_input_must_be_empty"}
    if command == "rescan_inventory":
        if inventory_rescan is None:
            return {"status": "failed", "reason": "commercial_inventory_rescan_unavailable"}
        try:
            count = inventory_rescan()
        except Exception:
            # Discovery may inspect protected local configuration. Never put
            # an exception containing that material into a command receipt.
            return {"status": "failed", "reason": "commercial_inventory_rescan_failed"}
        if type(count) is not int or count < 0:
            return {"status": "failed", "reason": "commercial_inventory_rescan_result_invalid"}
        return {"status": "succeeded", "discovered_count": count}
    if command == "read_host_facts":
        return {"status": "succeeded", "facts": read_normalized_local_facts(capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES))}
    readiness = collect_mautic_install_readiness()
    if command == "check_prerequisites":
        return {"status": "succeeded", "readiness": readiness}
    if command == "check_web_php":
        return {"status": "succeeded", "web": readiness.get("nginx", {}), "php": readiness.get("php", {})}
    if command == "check_mysql":
        return {"status": "succeeded", "database": readiness.get("database", {})}
    if command == "check_redis":
        return {"status": "succeeded", "redis": _service_state(("redis-server", "redis"))}
    return {"status": "failed", "reason": "commercial_generic_command_not_implemented"}

