from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent.commercial_detector import (
    PORTAL_LOCAL_FACTS_CAPABILITIES,
    build_applicability_facts_snapshot,
    classify_host,
    detector_digest,
    read_normalized_local_facts,
    validate_portal_scenario_resolution,
    validate_scenario_detector,
)
from mcd_agent.commercial_portal import CommercialState
from mcd_agent.commercial_self_hosted import SelfHostedScenarioExecutor
from mcd_agent.commercial_onboarding import exposed_capabilities


@pytest.mark.parametrize("layout", ["bin/console", "site/bin/console", "site/project/bin/console", "site/public_html/bin/console", "site/project/app/console"])
def test_mautic_probe_recognizes_bounded_layouts(tmp_path, monkeypatch, layout):
    from mcd_agent import commercial_detector as detector
    monkeypatch.setattr(detector, "_MAUTIC_ROOTS", (str(tmp_path),))
    console = tmp_path / layout
    console.parent.mkdir(parents=True)
    console.touch()
    assert detector._probe_mautic_installation() == {"installed": True, "active": False, "markers": [f"path:{console}"]}


@pytest.mark.parametrize("link_at", ["site", "site/project", "site/project/bin", "site/project/bin/console", "root"])
def test_mautic_probe_does_not_follow_foreign_symlinks(tmp_path, monkeypatch, link_at):
    from mcd_agent import commercial_detector as detector
    root = tmp_path / "approved"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    console = foreign / "bin/console"
    console.parent.mkdir()
    console.touch()
    if link_at == "root":
        root.rmdir()
        root.symlink_to(foreign, target_is_directory=True)
    else:
        link = root / link_at
        link.parent.mkdir(parents=True, exist_ok=True)
        target = console if link.name == "console" else console.parent if link.name == "bin" else foreign
        link.symlink_to(target, target_is_directory=link.name != "console")
    monkeypatch.setattr(detector, "_MAUTIC_ROOTS", (str(root),))
    assert detector._probe_mautic_installation()["installed"] is False


def test_mautic_probe_ignores_deeper_unknown_layout(tmp_path, monkeypatch):
    from mcd_agent import commercial_detector as detector
    console = tmp_path / "site/unknown/project/bin/console"
    console.parent.mkdir(parents=True)
    console.touch()
    monkeypatch.setattr(detector, "_MAUTIC_ROOTS", (str(tmp_path),))
    assert detector._probe_mautic_installation()["markers"] == []


@pytest.mark.parametrize("active", [{"php8.5-fpm"}, {"php8.4-fpm", "php8.5-fpm"}, set()])
def test_php_probe_discovers_actual_units_and_checks_activity(monkeypatch, active):
    from mcd_agent import commercial_detector as detector
    calls = []
    monkeypatch.setattr(detector.shutil, "which", lambda _: "/usr/bin/systemctl")
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "list-unit-files":
            return SimpleNamespace(returncode=0, stdout="php8.5-fpm.service enabled\nphp8.4-fpm.service disabled\nphp8.6-fpm@evil.service enabled\n../../evil enabled\nphp8.5-fpm.service enabled\n")
        return SimpleNamespace(returncode=0 if argv[-1] in active else 3)
    monkeypatch.setattr(detector.subprocess, "run", run)
    assert detector._active_services(("php-fpm",)) == sorted(active)
    assert {argv[-1] for argv in calls if argv[1] == "is-active"} == {"php-fpm", "php8.4-fpm", "php8.5-fpm"}


def test_php_probe_without_systemctl_is_empty(monkeypatch):
    from mcd_agent import commercial_detector as detector
    monkeypatch.setattr(detector.shutil, "which", lambda _: None)
    monkeypatch.setattr(detector.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected command"))
    assert detector._active_services(("php-fpm",)) == []


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


def test_default_facts_restore_truthful_preflight_capabilities(tmp_path, monkeypatch):
    path = tmp_path / "os-release"
    path.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
    monkeypatch.setattr(
        "mcd_agent.commercial_detector.shutil.which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else "/usr/bin/tool" if name in {"dpkg", "curl", "systemctl"} else None,
    )
    monkeypatch.setattr("mcd_agent.commercial_detector.os.geteuid", lambda: 0)
    facts = read_normalized_local_facts(os_release_path=str(path), machine="x86_64")
    assert facts["capabilities"] == [
        "dpkg",
        "os_like:debian",
        "package:curl",
        "package_manager:apt-get",
        "root_sudo",
        "service_manager:systemd",
    ]


def test_host_classification_is_conservative_and_excludes_mcd_service():
    clean = {
        name: {"installed": False, "active": False, "markers": []}
        for name in ("mautic_installation", "web_server", "database", "cache", "php_runtime")
    }
    classified = classify_host(probes=clean)
    assert classified["target_kind"] == "new_server"
    assert classified["reason"] == "clean_host_no_mautic_or_server_stack"
    assert classified["evidence"]["excluded_from_classification"] == ["mcd.service"]

    occupied = {**clean, "php_runtime": {"installed": True, "active": False, "markers": ["path:/etc/php"]}}
    classified = classify_host(probes=occupied)
    assert classified["target_kind"] == "existing_server"
    assert classified["reason"] == "existing_server_markers:php_runtime"
    assert "mcd.service" not in classified["evidence"]["probes"]["php_runtime"]["markers"]


def test_emitted_clean_facts_are_consumed_by_self_hosted_stage_guard(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=ubuntu\nVERSION_ID=24.04\n", encoding="utf-8")
    monkeypatch.setattr("mcd_agent.commercial_detector.shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    clean = {
        name: {"installed": False, "active": False, "markers": []}
        for name in ("mautic_installation", "web_server", "database", "cache", "php_runtime")
    }

    def reader(**kwargs):
        return read_normalized_local_facts(
            os_release_path=str(os_release),
            machine="x86_64",
            capabilities=kwargs.get("capabilities"),
            classification_probes=clean,
        )

    snapshot = build_applicability_facts_snapshot(capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES), facts_reader=reader)
    expected = dict(snapshot["facts"])
    expected.pop("detector_schema", None)
    executor = SelfHostedScenarioExecutor(CommercialState(str(tmp_path / "commercial.sqlite")), facts_reader=reader)
    observed = executor._local_facts(expected, {"local_facts_capabilities": list(PORTAL_LOCAL_FACTS_CAPABILITIES)})
    assert observed == expected
    assert observed["target_kind"] == "new_server"
    assert observed["classification_evidence"]["schema"] == "mcd-host-classification-evidence-v1"


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
    response = {"schema": "portal-scenario-resolve-response-v1", "contract_version": "1.0", "status": "matched", "detected_facts": facts, "matches": [{"scenario_id": "s1", "revision": "r1", "lifecycle": "published", "artifact": {"digest": "sha256:" + "a" * 64, "size_bytes": 1, "media_type": "application/octet-stream"}}]}
    assert validate_portal_scenario_resolution(response)["status"] == "matched"
    with pytest.raises(ValueError, match="not_unique"):
        validate_portal_scenario_resolution({**response, "matches": []})
