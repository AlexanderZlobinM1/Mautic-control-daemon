import base64
import hashlib
import json
import os
import subprocess

import pytest

from mcd_agent import mautic_patch_plan as v1
from mcd_agent import mautic_patch_plan_v2 as v2


def _payload(old="legacy", new="fixed"):
    return (
        "diff --git a/app/example.php b/app/example.php\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/example.php\n"
        "+++ b/app/example.php\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n+{new}\n"
    ).encode()


def test_target_version_accepts_official_zip_release_metadata_over_dev_lock(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "release_metadata.json").write_text('{"version":"6.0.9"}', encoding="utf-8")
    (tmp_path / "composer.lock").write_text(
        '{"packages":[{"name":"mautic/core-lib","version":"6.0.0-dev"}]}',
        encoding="utf-8",
    )

    assert v1._target_version(tmp_path) == "6.0.9"


def test_target_version_rejects_conflicting_release_metadata_and_stable_lock(tmp_path):
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "docroot" / "app" / "bundles" / "CoreBundle").mkdir(parents=True)
    (tmp_path / "app" / "release_metadata.json").write_text('{"version":"6.0.9"}', encoding="utf-8")
    (tmp_path / "docroot" / "app" / "bundles" / "CoreBundle" / "release_metadata.json").write_text('{"version":"6.0.10"}', encoding="utf-8")
    (tmp_path / "composer.lock").write_text(
        '{"packages":[{"name":"mautic/core-lib","version":"6.0.8"}]}',
        encoding="utf-8",
    )

    assert v1._target_version(tmp_path) is None


def test_target_version_rejects_malformed_or_missing_metadata(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "release_metadata.json").write_text('{"version":', encoding="utf-8")
    with pytest.raises(v1.PatchPlanError, match="invalid_version_metadata"):
        v1._target_version(tmp_path)

    missing = tmp_path / "missing"
    missing.mkdir()
    assert v1._target_version(missing) is None


def _record(payload, *, absent_gate=False, phase="post_source_install", fixed_ranges=None):
    vulnerable_needle = "legacy" if not absent_gate else "migration_marker"
    vulnerable_count = 1 if not absent_gate else 0
    gates = [{"group": "vulnerable", "kind": "exact_count", "path": "app/example.php",
              "needle": vulnerable_needle, "expected_count": vulnerable_count}]
    if absent_gate:
        gates[0]["allow_missing_path"] = True
    gates.append({"group": "fixed", "kind": "exact_count", "path": "app/example.php",
                  "needle": "fixed", "expected_count": 1})
    return {
        "id": "M7-TEST-PATCH", "status": "active", "owner": "Mautic-Operations", "enabled": True,
        "execution_scope": "generic_upgrade", "affected_ranges": [">=7.2.0 while signature matches"],
        "fixed_ranges": fixed_ranges or [], "install_types": ["zip", "composer"],
        "phases": [phase], "depends_on": [], "conflicts_with": [], "phase_order": 10,
        "gate_logic": "vulnerable_all; fixed_all; mixed_or_unknown=error",
        "source_paths": ["app/example.php"], "patch_path": "patches/test.patch",
        "patch_sha256": hashlib.sha256(payload).hexdigest(), "gate": gates,
    }


def _plan(payload, record=None, **overrides):
    record = record or _record(payload)
    plan = {
        "schema": v2.SCHEMA, "registry_commit": "1" * 40, "registry_sha256": "2" * 64,
        "source_version": "7.1.3", "target_version": "7.2.1", "install_type": "zip",
        "phase": "post_source_install", "run_id": "test-run", "operation": "apply",
        "patches": [record],
        "payloads": [{"path": record["patch_path"], "sha256": record["patch_sha256"],
                       "content_base64": base64.b64encode(payload).decode()}],
    }
    plan.update(overrides)
    return json.dumps(plan, separators=(",", ":"))


def _binary_create_patch(tmp_path, relative_path, content):
    repo = tmp_path / "binary-patch-source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "MCD tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "mcd-tests@example.invalid"], cwd=repo, check=True)
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    subprocess.run(["git", "add", "--", relative_path], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--", relative_path],
        cwd=repo, check=True, capture_output=True,
    )
    return result.stdout


def _binary_replace_patch(tmp_path, relative_path, old_content, new_content):
    repo = tmp_path / "binary-patch-source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "MCD tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "mcd-tests@example.invalid"], cwd=repo, check=True)
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(old_content)
    subprocess.run(["git", "add", "--", relative_path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    target.write_bytes(new_content)
    result = subprocess.run(
        ["git", "diff", "--binary", "--", relative_path],
        cwd=repo, check=True, capture_output=True,
    )
    return result.stdout


def _binary_record(payload, relative_path, *, old_sha256=None, new_sha256):
    record = _record(payload)
    record.update({
        "id": "GENERIC-BINARY-ASSET",
        "patch_path": "patches/generated-assets.patch",
        "patch_sha256": hashlib.sha256(payload).hexdigest(),
        "source_paths": [relative_path],
        "gate": [],
    })
    if old_sha256 is None:
        record["gate"].append({
            "group": "vulnerable", "kind": "path_state", "path": relative_path,
            "expected_state": "absent",
        })
    else:
        record["gate"].extend([
            {"group": "vulnerable", "kind": "path_state", "path": relative_path,
             "expected_state": "present"},
            {"group": "vulnerable", "kind": "sha256", "path": relative_path,
             "expected_sha256": old_sha256},
        ])
    record["gate"].append({
        "group": "fixed", "kind": "sha256", "path": relative_path,
        "expected_sha256": new_sha256,
    })
    return record


def test_v2_contract_is_additive_and_advertises_literal_gate_capability():
    contract = v1.contract()
    assert contract["plan_schema"] == v1.PLAN_SCHEMA
    assert "mautic-patch-plan-v2" in contract["capabilities"]
    assert v2.EVIDENCE_SCHEMA in contract["capabilities"]
    capability = contract["patch_plan_v2"]
    assert capability["minimum_agent_version"] == "1.2.62"
    assert capability["gate_kinds"] == ["exact_count", "path_state", "sha256"]
    assert capability["gate_groups"] == ["vulnerable", "fixed"]
    assert capability["gate_semantics"]["exact_count"] == "literal_substring_count"
    assert capability["gate_semantics"]["path_state"] == "root_relative_regular_file_absent_or_present"


@pytest.mark.parametrize("state", ["absent", "already"])
def test_v2_binary_create_verify_apply_hash_and_rollback(tmp_path, monkeypatch, state):
    root = tmp_path / "mautic"
    assets = root / "plugins" / "Bundle" / "Assets"
    assets.mkdir(parents=True)
    relative = "plugins/Bundle/Assets/engine.bin"
    content = b"\x00binary-generated-asset\xff"
    payload = _binary_create_patch(tmp_path, relative, content)
    record = _binary_record(payload, relative, new_sha256=hashlib.sha256(content).hexdigest())
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    target = root / relative
    if state == "already":
        target.write_bytes(content)
        result = v2.execute(str(root), raw)
        assert result["status"] == "success"
        assert result["patches"][0]["decision"] == "already"
        assert target.read_bytes() == content
        return
    verify_plan = json.loads(raw)
    verify_plan["operation"] = "verify"
    verified = v2.execute(str(root), json.dumps(verify_plan))
    assert verified["status"] == "success"
    assert verified["patches"][0]["decision"] == "candidate"
    assert not target.exists()
    applied = v2.execute(str(root), raw)
    assert applied["status"] == "success"
    assert applied["patches"][0]["decision"] == "applied"
    assert applied["patches"][0]["before_sha256"][relative] is None
    assert applied["patches"][0]["after_sha256"][relative] == hashlib.sha256(content).hexdigest()
    assert target.read_bytes() == content
    rolled_back = v2.rollback(str(root), raw)
    assert rolled_back["status"] == "success"
    assert rolled_back["restored"] == [record["id"]]
    assert not target.exists()


def test_v2_binary_replacement_restores_existing_bytes_and_mode(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    relative = "plugins/Bundle/Assets/engine.bin"
    target = root / relative
    target.parent.mkdir(parents=True)
    old_content = b"\x00old-binary\xfe"
    new_content = b"\x00new-binary\xff"
    target.write_bytes(old_content)
    target.chmod(0o640)
    old_mode = target.stat().st_mode & 0o777
    payload = _binary_replace_patch(tmp_path, relative, old_content, new_content)
    record = _binary_record(
        payload, relative, old_sha256=hashlib.sha256(old_content).hexdigest(),
        new_sha256=hashlib.sha256(new_content).hexdigest(),
    )
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    applied = v2.execute(str(root), raw)
    assert applied["status"] == "success"
    assert target.read_bytes() == new_content
    rolled_back = v2.rollback(str(root), raw)
    assert rolled_back["status"] == "success"
    assert target.read_bytes() == old_content
    assert target.stat().st_mode & 0o777 == old_mode


def test_v2_atomic_preflight_rolls_back_created_file_after_later_phase_failure(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    assets = root / "plugins" / "Bundle" / "Assets"
    assets.mkdir(parents=True)
    first_path = "plugins/Bundle/Assets/first.bin"
    second_path = "plugins/Bundle/Assets/second.bin"
    first_content = b"\x00first"
    second_content = b"\x00second"
    first_payload = _binary_create_patch(tmp_path, first_path, first_content)
    second_tmp = tmp_path / "other-patch"
    second_tmp.mkdir()
    second_payload = _binary_create_patch(second_tmp, second_path, second_content)
    first_record = _binary_record(
        first_payload, first_path, new_sha256=hashlib.sha256(first_content).hexdigest()
    )
    first_record["id"] = "GENERIC-BINARY-FIRST"
    first_record["phase_order"] = 10
    first_record["phases"] = ["post_source_install", "before_doctrine_migrations"]
    second_record = _binary_record(
        second_payload, second_path, old_sha256=hashlib.sha256(b"old").hexdigest(),
        new_sha256=hashlib.sha256(second_content).hexdigest(),
    )
    second_record["id"] = "GENERIC-BINARY-SECOND"
    second_record["patch_path"] = "patches/second-assets.patch"
    second_record["phase_order"] = 20
    second_record["phases"] = ["before_doctrine_migrations"]
    plan = json.loads(_plan(first_payload, first_record))
    plan["patches"].append(second_record)
    plan["payloads"].append({
        "path": second_record["patch_path"],
        "sha256": second_record["patch_sha256"],
        "content_base64": base64.b64encode(second_payload).decode(),
    })
    raw = json.dumps(plan, separators=(",", ":"))
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")

    result = v2.atomic_preflight(str(root), raw)
    assert result["status"] == "error"
    assert result["rollback_attempted"] is True
    assert result["rollback_succeeded"] is True
    assert not (root / first_path).exists()
    assert not (root / second_path).exists()


def test_v2_rollback_does_not_overwrite_post_apply_changes(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    assets = root / "plugins" / "Bundle" / "Assets"
    assets.mkdir(parents=True)
    relative = "plugins/Bundle/Assets/engine.bin"
    content = b"\x00expected"
    payload = _binary_create_patch(tmp_path, relative, content)
    record = _binary_record(payload, relative, new_sha256=hashlib.sha256(content).hexdigest())
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    assert v2.execute(str(root), raw)["status"] == "success"
    target = root / relative
    target.write_bytes(b"operator-change")
    result = v2.rollback(str(root), raw)
    assert result["status"] == "error"
    assert result["rollback_succeeded"] is False
    assert target.read_bytes() == b"operator-change"


def test_v2_binary_partial_or_wrong_hash_fails_closed_without_mutation(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    relative = "plugins/Bundle/Assets/engine.bin"
    target = root / relative
    target.parent.mkdir(parents=True)
    partial = b"\x00partial-unknown"
    target.write_bytes(partial)
    expected = b"\x00expected"
    payload = _binary_create_patch(tmp_path, relative, expected)
    record = _binary_record(payload, relative, new_sha256=hashlib.sha256(expected).hexdigest())
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v2.execute(str(root), raw)
    assert result["status"] == "error"
    assert result["patches"][0]["reason"] == "ambiguous_or_unknown_gate"
    assert target.read_bytes() == partial


def test_v2_rejects_symlinked_gate_path(tmp_path):
    root = tmp_path / "mautic"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "engine.bin").write_bytes(b"outside")
    (root / "assets").symlink_to(outside, target_is_directory=True)
    record = {
        "gate": [
            {"group": "vulnerable", "kind": "path_state", "path": "assets/engine.bin", "expected_state": "absent"},
            {"group": "fixed", "kind": "sha256", "path": "assets/engine.bin", "expected_sha256": "a" * 64},
        ]
    }
    with pytest.raises(v2.PatchPlanV2Error, match="source_path_symlink"):
        v2._gate_results(root, record)


def test_v2_accepts_large_binary_git_patch_payloads(tmp_path):
    relative = "plugins/Bundle/Assets/generated.bin"
    content = os.urandom(3_500_000)
    payload = _binary_create_patch(tmp_path, relative, content)
    assert len(payload) > 4_126_671
    record = _binary_record(payload, relative, new_sha256=hashlib.sha256(content).hexdigest())
    parsed = v2.parse_plan(_plan(payload, record))
    assert len(parsed["_payload_map"][record["patch_path"]]) == len(payload)


def test_plan_accepts_future_same_major_target_without_registry_target_pin():
    raw = _plan(_payload(), target_version="7.2.3")
    parsed = v2.parse_plan(raw)
    assert parsed["target_version"] == "7.2.3"
    assert parsed["registry_commit"] == "1" * 40


@pytest.mark.parametrize("group,kind", [("fixed_cast", "exact_count"), ("vulnerable", "required_file"), ("asset", "exact_count")])
def test_v2_rejects_unsupported_gate_kinds_and_groups(group, kind):
    payload = _payload()
    record = _record(payload)
    record["gate"][0]["group"] = group
    record["gate"][0]["kind"] = kind
    with pytest.raises(v2.PatchPlanV2Error, match="unsupported_gate_kind_or_group"):
        v2.parse_plan(_plan(payload, record))


def test_v2_rejects_unflagged_missing_path_for_absence_gate(tmp_path):
    root = tmp_path / "mautic"
    root.mkdir()
    payload = _payload()
    record = _record(payload, absent_gate=True)
    record["gate"][0].pop("allow_missing_path")
    with pytest.raises(v2.PatchPlanV2Error, match="source_gate_path_missing"):
        v2._gate_results(root, record)


def test_v2_explicit_missing_zero_count_gate_is_a_candidate(tmp_path):
    root = tmp_path / "mautic"
    (root / "app").mkdir(parents=True)
    (root / "app/example.php").write_text("plain source\n")
    record = _record(_payload(), absent_gate=True)
    record["gate"][0]["path"] = "app/migrations/not-created-yet.php"
    assert v2._gate_results(root, record)[0] == "vulnerable"


def test_v2_atomic_preflight_returns_first_phase_error_without_rollback_ledger(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    target = root / "app/example.php"
    target.parent.mkdir(parents=True)
    target.write_text("legacy\nfixed\n")
    payload = _payload()
    raw = _plan(payload)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v2.atomic_preflight(str(root), raw)
    assert result["status"] == "error"
    assert result["rollback_attempted"] is False
    assert result["rollback_succeeded"] is True
    assert target.read_text() == "legacy\nfixed\n"


def test_v2_rolls_back_every_file_in_a_multi_file_patch(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    first = root / "app/example.php"
    second = root / "app/second.php"
    first.parent.mkdir(parents=True)
    first.write_text("legacy\n")
    second.write_text("before\n")
    payload = (
        "diff --git a/app/example.php b/app/example.php\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/example.php\n+++ b/app/example.php\n@@ -1 +1 @@\n-legacy\n+fixed\n"
        "diff --git a/app/second.php b/app/second.php\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/second.php\n+++ b/app/second.php\n@@ -1 +1 @@\n-before\n+after\n"
    ).encode()
    record = _record(payload)
    record["source_paths"] = ["app/example.php", "app/second.php"]
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    original_gate = v2._gate_results
    calls = 0

    def fail_post_apply(source, patch_record):
        nonlocal calls
        calls += 1
        return original_gate(source, patch_record) if calls == 1 else ("ambiguous_or_unknown", [])

    monkeypatch.setattr(v2, "_gate_results", fail_post_apply)
    result = v2.execute(str(root), raw)
    assert result["status"] == "error"
    assert result["rollback_attempted"] is True
    assert result["rollback_succeeded"] is True
    assert first.read_text() == "legacy\n"
    assert second.read_text() == "before\n"


def test_v2_fixed_signatures_win_when_vulnerable_absence_gate_still_matches(tmp_path):
    root = tmp_path / "mautic"
    source = root / "app"
    source.mkdir(parents=True)
    (source / "example.php").write_text("fixed\n")
    record = _record(_payload(), absent_gate=True)
    assert v2._gate_results(root, record)[0] == "fixed"


def test_v2_mixed_positive_vulnerable_and_fixed_signatures_fail_closed(tmp_path):
    root = tmp_path / "mautic"
    source = root / "app"
    source.mkdir(parents=True)
    (source / "example.php").write_text("legacy\nfixed\n")
    record = _record(_payload())
    assert v2._gate_results(root, record)[0] == "ambiguous_or_unknown"


def test_v2_applies_only_the_current_phase_and_rolls_back_from_signed_hashes(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    target = root / "app/example.php"
    target.parent.mkdir(parents=True)
    target.write_text("legacy\n")
    payload = _payload()
    record = _record(payload)
    record["phases"] = ["post_source_install", "before_doctrine_migrations"]
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v2.execute(str(root), raw)
    assert result["schema"] == v2.EVIDENCE_SCHEMA
    assert result["status"] == "success"
    assert result["patches"][0]["decision"] == "applied"
    assert target.read_text() == "fixed\n"
    rolled_back = v2.rollback(str(root), raw)
    assert rolled_back["status"] == "success"
    assert target.read_text() == "legacy\n"


def test_v2_atomic_upgrade_preflight_advances_only_real_upgrade_phases(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    target = root / "app/example.php"
    target.parent.mkdir(parents=True)
    target.write_text("legacy\n")
    payload = _payload()
    record = _record(payload)
    record["phases"] = ["post_source_install", "before_doctrine_migrations", "before_background_imports"]
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v1.atomic_preflight(str(root), raw, "test-run")
    assert result["status"] == "success"
    assert [item["phase"] for item in result["phases"]] == [
        "post_source_install", "before_doctrine_migrations",
    ]
    assert result["patches"][0]["decision"] == "applied"
    assert target.read_text() == "fixed\n"
    later = json.loads(raw)
    later["phase"] = "before_background_imports"
    result = v1.execute(str(root), json.dumps(later), "before_background_imports", "test-run")
    assert result["status"] == "success"
    assert result["patches"][0]["decision"] == "already"


def test_v2_phase_with_no_matching_records_is_successful_noop(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    (root / "app").mkdir(parents=True)
    (root / "app/example.php").write_text("legacy\n")
    payload = _payload()
    record = _record(payload, phase="before_background_imports")
    raw = _plan(payload, record, phase="post_source_install")
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    result = v2.execute(str(root), raw)
    assert result["status"] == "success"
    assert result["selected"] == []
    assert result["patches"] == []
