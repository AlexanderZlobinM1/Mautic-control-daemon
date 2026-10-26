from pathlib import Path

import pytest

from mcd_agent.commercial_detector import detector_digest, read_normalized_local_facts, validate_portal_scenario_resolution, validate_scenario_detector
from mcd_agent.commercial_onboarding import exposed_capabilities


def test_normalized_detector_facts_are_data_driven(tmp_path, monkeypatch):
    path = tmp_path / "os-release"
    path.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
    monkeypatch.setattr("mcd_agent.commercial_detector.shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    facts = read_normalized_local_facts(os_release_path=str(path), machine="x86_64")
    assert facts["os_id"] == "ubuntu" and facts["os_version"] == "22.04" and facts["architecture"] == "amd64"
    facts["capabilities"].append("portal_scenario_execution_v1")
    scenario = {"detector": {"id": "detector-1", "revision": "r1", "digest": detector_digest(facts), "supported": [{"os_ids": ["ubuntu"], "os_versions": ["22.04", "24.04"], "architectures": ["amd64", "arm64"], "package_managers": ["apt-get"]}], "required_capabilities": ["portal_scenario_execution_v1"]}}
    assert validate_scenario_detector(scenario, facts, detector_id="detector-1", detector_revision="r1")["status"] == "matched"
    assert "primitive_versions" in exposed_capabilities()


def test_detector_mismatch_unsupported_and_ambiguous_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "os-release"
    path.write_text("ID=debian\nVERSION_ID=13\n", encoding="utf-8")
    monkeypatch.setattr("mcd_agent.commercial_detector.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="ambiguous"):
        read_normalized_local_facts(os_release_path=str(path), machine="arm64")
    facts = {"os_id": "ubuntu", "os_version": "24.04", "architecture": "arm64", "package_manager": "apt-get", "capabilities": [], "target_kind": "existing_server"}
    scenario = {"detector": {"id": "detector-1", "revision": "r1", "digest": detector_digest(facts), "supported": [{"os_ids": ["ubuntu"], "os_versions": ["22.04"], "architectures": ["amd64"]}]}}
    with pytest.raises(ValueError, match="unsupported"):
        validate_scenario_detector(scenario, facts, detector_id="detector-1", detector_revision="r1")


def test_portal_resolution_requires_one_published_external_artifact():
    facts = {"os_id": "ubuntu", "os_version": "24.04", "architecture": "amd64", "package_manager": "apt-get", "capabilities": [], "target_kind": "new_server"}
    response = {"schema": "portal-scenario-resolve-response-v1", "contract_version": "1.0", "status": "matched", "detected_facts": facts, "matches": [{"scenario_id": "s1", "revision": "r1", "lifecycle": "published", "artifact": {"digest": "sha256:" + "a" * 64, "size_bytes": 1, "media_type": "application/octet-stream"}, "signature": {"algorithm": "ed25519", "key_id": "k", "value": "sig"}}]}
    assert validate_portal_scenario_resolution(response)["status"] == "matched"
    with pytest.raises(ValueError, match="not_unique"):
        validate_portal_scenario_resolution({**response, "matches": []})
