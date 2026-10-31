from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import mcd_agent.apt_profile as apt


PROFILE = {"db_repo_profile_target": "mariadb_11_4", "packages_present": ["mariadb-server", "mariadb-client"]}


@pytest.mark.parametrize("version,valid", [("1:11.4.8+maria~ubu2404", True), ("1:10.11.14-0ubuntu0.24.04.1", False), ("(none)", False), ("", False)])
def test_candidate_version_must_match_declared_family(version, valid):
    with patch.object(apt, "_run", return_value=SimpleNamespace(returncode=0, stdout=f"  Candidate: {version}\n")):
        assert (not apt._declared_db_version_errors(PROFILE, installed=False, timeout_sec=30)) == valid


def test_installed_version_checked_independently_of_candidate():
    with patch.object(apt, "_dpkg_installed_versions", return_value={"mariadb-server": "1:10.11.14", "mariadb-client": "1:11.4.8"}):
        errors = apt._declared_db_version_errors(PROFILE, installed=True, timeout_sec=30)
    assert len(errors) == 1
    assert "installed_version_mismatch" in errors[0]


def test_conflicting_declared_versions_rejected():
    with patch.object(apt, "_run", side_effect=AssertionError("must not probe")):
        assert apt._declared_db_version_errors({**PROFILE, "mariadb_repo_setup_version": "mariadb-10.11"}, installed=False, timeout_sec=30) == ["db_repo_profile:conflicting_mariadb_version"]


@pytest.mark.parametrize("cached_status,repo_present", [("skipped", False), ("applied", False), ("applied", True)])
def test_repo_cache_requires_success_and_actual_repository(cached_status, repo_present):
    profile = {**PROFILE, "ondrej_php_profile_enabled": False}
    marker = {"profiles": {"db_repo": {"applied": True, "last_status": cached_status,
        "profile_hash": hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
        "detected": {"profile": "mariadb_11_4"}}}}
    with patch.object(apt, "_read_json", return_value=marker), patch.object(apt, "_write_json"), patch.object(apt, "_repo_profile_present", return_value=repo_present), patch.object(apt, "_run_mariadb_repo_setup", return_value=(True, "setup")) as setup:
        actions, errors = apt._apply_repo_profiles(profile)
    assert not errors
    assert setup.call_count == (0 if repo_present else 1)
    assert ("db_repo_profile:skip_once" in actions) == (cached_status == "applied" and repo_present)


def test_empty_host_detection_is_not_applied_and_blocks_db_install():
    marker = {}
    with patch.object(apt, "_read_json", return_value=marker), patch.object(apt, "_write_json"), patch.object(apt, "_detect_db_repo_target", return_value={"profile": "none", "action": "none"}):
        _, errors = apt._apply_repo_profiles({"packages_present": ["mariadb-server"], "ondrej_php_profile_enabled": False})
    assert "db_repo_profile:explicit_target_required" in errors
    assert marker["profiles"]["db_repo"]["applied"] is False


def test_repo_failure_stops_before_any_package_operations():
    with patch.object(apt, "_apply_repo_profiles", return_value=([], ["repo failed"])), patch.object(apt, "_run", side_effect=AssertionError("package operation forbidden")):
        result = apt.apply_apt_profile({**PROFILE, "dedupe_list_sources": False, "remove_third_party_sources": False})
    assert result["status"] == "error"
    assert result["reason"] == "apt_profile_repo_failed"


def test_stale_apt_candidate_blocks_install_even_after_zero_exit_refresh():
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["apt-get", "update"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["apt-cache", "policy"]:
            return SimpleNamespace(returncode=0, stdout="  Candidate: 1:10.11.14\n", stderr="")
        raise AssertionError(f"unexpected package operation: {command}")
    with patch.object(apt, "_apply_repo_profiles", return_value=([], [])), patch.object(apt, "_apply_unattended_upgrades", return_value=([], [], [])), patch.object(apt, "_run", side_effect=run):
        result = apt.apply_apt_profile({**PROFILE, "dedupe_list_sources": False, "remove_third_party_sources": False})
    assert result["reason"] == "apt_profile_db_candidate_mismatch"
    assert all(command[:2] != ["apt-get", "install"] for command in calls)


def test_unrepaired_refresh_blocks_install():
    with patch.object(apt, "_apply_repo_profiles", return_value=([], [])), patch.object(apt, "_apply_unattended_upgrades", return_value=([], [], [])), patch.object(apt, "_run", return_value=SimpleNamespace(returncode=100, stdout="", stderr="network unavailable")) as run:
        result = apt.apply_apt_profile({**PROFILE, "repair_on_error": False, "dedupe_list_sources": False, "remove_third_party_sources": False})
    assert result["reason"] == "apt_profile_refresh_failed"
    assert len(run.call_args_list) == 1
