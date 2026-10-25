from __future__ import annotations

import hashlib
import json
import platform
import shutil
from pathlib import Path
from typing import Any, Mapping

from mcd_agent.commercial_portal import canonical_json


SCHEMA = "mcd-scenario-detector-v1"
def read_normalized_local_facts(*, os_release_path: str = "/etc/os-release", machine: str | None = None) -> dict[str, Any]:
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
    return {"os_id": os_id, "os_version": version, "arch": arch, "package_manager": managers[0], "package_manager_capabilities": ["update", "install", "query"], "detector_schema": SCHEMA}


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
    matching = [item for item in supported if isinstance(item, dict) and item.get("os_id") == facts.get("os_id") and facts.get("os_version") in list(item.get("versions") or []) and facts.get("arch") in list(item.get("architectures") or [])]
    if not matching:
        raise ValueError("commercial_detector_unsupported_platform")
    required_manager = matching[0].get("package_manager")
    if required_manager and required_manager != facts.get("package_manager"):
        raise ValueError("commercial_detector_package_manager_mismatch")
    required_capabilities = set(detector.get("required_capabilities") or [])
    available_capabilities = set(detector.get("available_capabilities") or [])
    if not required_capabilities.issubset(available_capabilities):
        raise ValueError("commercial_detector_capability_missing")
    return {"status": "matched", "detector_id": detector_id, "revision": detector_revision, "facts_digest": detector_digest(facts), "os_id": facts["os_id"], "os_version": facts["os_version"], "arch": facts["arch"]}
