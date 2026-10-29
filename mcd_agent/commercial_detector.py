from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from mcd_agent.commercial_portal import canonical_json


SCHEMA = "mcd-scenario-detector-v1"
APPLICABILITY_FACTS_SCHEMA = "mcd-portal-applicability-facts-v1"
APPLICABILITY_FACTS_CONTRACT_VERSION = "1.0"
PORTAL_LOCAL_FACTS_CAPABILITIES = (
    "package_manager.query",
    "portal_scenario_execution_v1",
    "mcd_passive_activation_v1",
)
_APPLICABILITY_FACT_KEYS = (
    "os_id",
    "os_version",
    "architecture",
    "package_manager",
    "capabilities",
    "target_kind",
)


def _detected_host_capabilities(*, os_id: str, package_manager: str) -> list[str]:
    """Return truthful host/preflight capabilities, not MCD feature labels."""
    capabilities = {f"package_manager:{package_manager}"}
    if os_id in {"debian", "ubuntu", "linuxmint", "pop"}:
        capabilities.add("os_like:debian")
    if shutil.which("dpkg"):
        capabilities.add("dpkg")
    if shutil.which("curl"):
        capabilities.add("package:curl")
    if shutil.which("systemctl"):
        capabilities.add("service_manager:systemd")
    root_sudo = False
    try:
        root_sudo = os.geteuid() == 0
    except AttributeError:
        pass
    if not root_sudo and shutil.which("sudo"):
        try:
            root_sudo = subprocess.run(
                ["sudo", "-n", "true"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            root_sudo = False
    if root_sudo:
        capabilities.add("root_sudo")
    return sorted(capabilities)


def validate_portal_scenario_resolution(value: Mapping[str, Any], *, expected_digest: str | None = None) -> dict[str, Any]:
    """Validate Portal's external catalog resolution without embedding scenarios."""
    if value.get("schema") != "portal-scenario-resolve-response-v1" or value.get("contract_version") != "1.0":
        raise ValueError("commercial_portal_resolution_schema_invalid")
    status = value.get("status")
    if status not in {"matched", "unsupported", "ambiguous"}:
        raise ValueError("commercial_portal_resolution_status_invalid")
    facts = value.get("detected_facts")
    required = {"os_id", "os_version", "architecture", "package_manager", "capabilities", "target_kind"}
    if not isinstance(facts, dict) or not required.issubset(facts) or facts["target_kind"] not in {"new_server", "existing_server"}:
        raise ValueError("commercial_portal_resolution_facts_invalid")
    matches = value.get("matches") or []
    if not isinstance(matches, list):
        raise ValueError("commercial_portal_resolution_matches_invalid")
    if status == "matched":
        if len(matches) != 1:
            raise ValueError("commercial_portal_resolution_not_unique")
        match = matches[0]
        artifact = match.get("artifact") if isinstance(match, dict) else None
        if match.get("lifecycle") != "published" or not isinstance(artifact, dict) or not str(artifact.get("digest", "")).startswith("sha256:"):
            raise ValueError("commercial_portal_resolution_artifact_invalid")
        if expected_digest and artifact["digest"] != expected_digest:
            raise ValueError("commercial_portal_resolution_digest_mismatch")
    elif matches:
        raise ValueError("commercial_portal_resolution_unexpected_matches")
    return {"schema": value["schema"], "contract_version": "1.0", "status": status, "detected_facts": dict(facts), "matches": matches}


def read_normalized_local_facts(*, os_release_path: str = "/etc/os-release", machine: str | None = None, target_kind: str = "existing_server", capabilities: list[str] | None = None) -> dict[str, Any]:
    values: dict[str, str] = {}
    try:
        for line in Path(os_release_path).read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key in {"ID", "VERSION_ID"}:
                values[key] = value.strip().strip('"')
    except OSError as exc:
        raise ValueError("commercial_detector_os_release_unavailable") from exc
    os_id = values.get("ID", "").strip().lower()
    version = values.get("VERSION_ID", "").strip()
    raw_machine = (machine or platform.machine()).strip().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(raw_machine, raw_machine)
    managers = [name for name in ("apt-get", "dnf", "yum", "apk") if shutil.which(name)]
    if not os_id or not version or not arch or len(managers) != 1:
        raise ValueError("commercial_detector_facts_ambiguous")
    if target_kind not in {"new_server", "existing_server"}:
        raise ValueError("commercial_detector_target_kind_invalid")
    observed_capabilities = list(capabilities) if capabilities is not None else _detected_host_capabilities(os_id=os_id, package_manager=managers[0])
    return {"os_id": os_id, "os_version": version, "architecture": arch, "package_manager": managers[0], "capabilities": sorted(set(observed_capabilities)), "target_kind": target_kind, "detector_schema": SCHEMA}


def normalize_applicability_facts(value: Mapping[str, Any], *, require_detector_schema: bool = False) -> dict[str, Any]:
    """Normalize the exact facts shared by the Portal inventory and job guard."""
    if not isinstance(value, Mapping):
        raise ValueError("commercial_applicability_facts_invalid")
    allowed = set(_APPLICABILITY_FACT_KEYS) | {"detector_schema"}
    if set(value) - allowed or any(key not in value for key in _APPLICABILITY_FACT_KEYS):
        raise ValueError("commercial_applicability_facts_invalid")
    normalized: dict[str, Any] = {}
    for key in _APPLICABILITY_FACT_KEYS:
        if key == "capabilities":
            capabilities = value.get(key)
            if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item.strip() for item in capabilities):
                raise ValueError("commercial_applicability_facts_invalid")
            normalized[key] = sorted(set(str(item).strip() for item in capabilities))
            continue
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("commercial_applicability_facts_invalid")
        normalized[key] = str(item).strip()
    detector_schema = value.get("detector_schema")
    if detector_schema is None:
        if require_detector_schema:
            raise ValueError("commercial_applicability_detector_schema_missing")
    elif not isinstance(detector_schema, str) or not detector_schema.strip():
        raise ValueError("commercial_applicability_detector_schema_invalid")
    else:
        normalized["detector_schema"] = detector_schema.strip()
    return normalized


def applicability_facts_digest(value: Mapping[str, Any]) -> str:
    """Hash the guard mapping; detector schema identifies the producer, not facts."""
    normalized = normalize_applicability_facts(value)
    normalized.pop("detector_schema", None)
    return "sha256:" + hashlib.sha256(canonical_json(normalized)).hexdigest()


def read_normalized_applicability_facts(*, target_kind: str, capabilities: list[str] | None, facts_reader: Any = read_normalized_local_facts, require_detector_schema: bool = False) -> dict[str, Any]:
    """Read and normalize facts using the same reader/context as the job guard."""
    observed = facts_reader(target_kind=target_kind, capabilities=capabilities)
    return normalize_applicability_facts(observed, require_detector_schema=require_detector_schema)


def build_applicability_facts_snapshot(*, target_kind: str, capabilities: list[str] | None, facts_reader: Any = read_normalized_local_facts) -> dict[str, Any]:
    """Build the signed, additive inventory coordinate consumed by Portal."""
    facts = read_normalized_applicability_facts(
        target_kind=target_kind,
        capabilities=capabilities,
        facts_reader=facts_reader,
        require_detector_schema=True,
    )
    return {
        "schema": APPLICABILITY_FACTS_SCHEMA,
        "contract_version": APPLICABILITY_FACTS_CONTRACT_VERSION,
        "facts": facts,
        "facts_digest": applicability_facts_digest(facts),
    }


def detector_digest(facts: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(facts))).hexdigest()


def validate_scenario_detector(scenario: Mapping[str, Any], facts: Mapping[str, Any], *, detector_id: str, detector_revision: str) -> dict[str, Any]:
    detector = scenario.get("detector")
    if not isinstance(detector, dict):
        return {"status": "not_required"}
    if detector.get("id") != detector_id or detector.get("revision") != detector_revision or detector.get("digest") != detector_digest(facts):
        raise ValueError("commercial_detector_mismatch")
    supported = detector.get("supported")
    if not isinstance(supported, list) or not supported:
        raise ValueError("commercial_detector_requirements_missing")
    matching = [item for item in supported if isinstance(item, dict) and (not item.get("os_ids") or facts.get("os_id") in list(item.get("os_ids") or [])) and (not item.get("os_versions") and not item.get("versions") or facts.get("os_version") in list(item.get("os_versions") or item.get("versions") or [])) and (not item.get("architectures") or facts.get("architecture") in list(item.get("architectures") or []))]
    if not matching:
        raise ValueError("commercial_detector_unsupported_platform")
    required_manager = matching[0].get("package_manager")
    required_managers = list(matching[0].get("package_managers") or ([required_manager] if required_manager else []))
    if required_managers and facts.get("package_manager") not in required_managers:
        raise ValueError("commercial_detector_package_manager_mismatch")
    required_capabilities = set(detector.get("required_capabilities") or [])
    available_capabilities = set(facts.get("capabilities") or detector.get("available_capabilities") or [])
    if not required_capabilities.issubset(available_capabilities):
        raise ValueError("commercial_detector_capability_missing")
    return {"status": "matched", "detector_id": detector_id, "revision": detector_revision, "facts_digest": detector_digest(facts), "os_id": facts["os_id"], "os_version": facts["os_version"], "architecture": facts["architecture"], "target_kind": facts["target_kind"]}
