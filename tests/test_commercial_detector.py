from pathlib import Path

import pytest

from mcd_agent.commercial_detector import detector_digest, read_normalized_local_facts, validate_scenario_detector
from mcd_agent.commercial_onboarding import exposed_capabilities


def test_normalized_detector_facts_are_data_driven(tmp_path, monkeypatch):
    path = tmp_path / "os-release"
    path.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
    monkeypatch.setattr("mcd_agent.commercial_detector.shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    facts = read_normalized_local_facts(os_release_path=str(path), machine="x86_64")
    assert facts["os_id"] == "ubuntu" and facts["os_version"] == "22.04" and facts["arch"] == "amd64"
    scenario = {"detector": {"id": "detector-1", "revision": "r1", "digest": detector_digest(facts), "supported": [{"os_id": "ubuntu", "versions": ["22.04", "24.04"], "architectures": ["amd64", "arm64"], "package_manager": "apt-get"}], "required_capabilities": ["portal_scenario_execution_v1"], "available_capabilities": ["portal_scenario_execution_v1"]}}
    assert validate_scenario_detector(scenario, facts, detector_id="detector-1", detector_revision="r1")["status"] == "matched"
    assert "primitive_versions" in exposed_capabilities()


def test_detector_mismatch_unsupported_and_ambiguous_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "os-release"
    path.write_text("ID=debian\nVERSION_ID=13\n", encoding="utf-8")
    monkeypatch.setattr("mcd_agent.commercial_detector.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="ambiguous"):
        read_normalized_local_facts(os_release_path=str(path), machine="arm64")
    facts = {"os_id": "ubuntu", "os_version": "24.04", "arch": "arm64", "package_manager": "apt-get"}
    scenario = {"detector": {"id": "detector-1", "revision": "r1", "digest": detector_digest(facts), "supported": [{"os_id": "ubuntu", "versions": ["22.04"], "architectures": ["amd64"]}]}}
    with pytest.raises(ValueError, match="unsupported"):
        validate_scenario_detector(scenario, facts, detector_id="detector-1", detector_revision="r1")
