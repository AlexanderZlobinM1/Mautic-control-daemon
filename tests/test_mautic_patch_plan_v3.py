from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mcd_agent import mautic_patch_plan_v3 as executor


FIXTURE = Path(__file__).parents[1] / "mcd_agent" / "contracts" / "fixtures" / "mautic-patch-resolution-v1.json"


def _plan():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["resolve_response"]["plan"]


def _payload(plan, content: bytes) -> None:
    item = plan["payloads"][0]
    item["content_base64"] = base64.b64encode(content).decode("ascii")
    item["sha256"] = hashlib.sha256(content).hexdigest()


class MauticPatchPlanV3Tests(unittest.TestCase):
    def test_mixed_gate_groups_are_ambiguous_even_when_one_group_is_complete(self):
        path = self.root / "docroot/app/mixed.txt"
        path.write_bytes(b"vulnerable-a\nvulnerable-b\nfixed-a\n")
        record = {
            "gate": [
                {"group": "vulnerable", "kind": "exact_count", "path": "docroot/app/mixed.txt", "needle": "vulnerable-a", "expected_count": 1},
                {"group": "vulnerable", "kind": "exact_count", "path": "docroot/app/mixed.txt", "needle": "vulnerable-b", "expected_count": 1},
                {"group": "fixed", "kind": "exact_count", "path": "docroot/app/mixed.txt", "needle": "fixed-a", "expected_count": 1},
                {"group": "fixed", "kind": "exact_count", "path": "docroot/app/mixed.txt", "needle": "fixed-b", "expected_count": 1},
            ],
        }
        state, _rows = executor._record_state(self.root, record)
        self.assertEqual(state, "ambiguous")

        path.write_bytes(b"fixed-a\nfixed-b\nvulnerable-a\n")
        state, _rows = executor._record_state(self.root, record)
        self.assertEqual(state, "ambiguous")

    def test_unexecuted_phase_source_gates_do_not_prevent_applied_phase_restore(self):
        plan = _plan()
        plan.update(trigger="upgrade_lifecycle", phase="dependency_update_preflight", source_version="7.1.3", target_version="7.2.1")
        first = plan["patches"][0]
        first.update(triggers=["upgrade_lifecycle"], phases=["before_cache_warmup"])
        import copy
        second = copy.deepcopy(first)
        second.update(id="UNEXECUTED-PHASE", phase_order=first["phase_order"] + 1,
                      phases=["before_doctrine_migrations"], source_paths=["docroot/app/second.txt"], payload_path="fixtures/second.patch")
        path = self.root / "docroot/app/second.txt"
        path.write_bytes(b"before\n")
        for gate in second["gate"]:
            gate["path"] = "docroot/app/second.txt"
        data = base64.b64decode(plan["payloads"][0]["content_base64"]).replace(b"docroot/app/fixture.txt", b"docroot/app/second.txt")
        plan["patches"].append(second)
        plan["payloads"].append({"path": second["payload_path"], "sha256": hashlib.sha256(data).hexdigest(), "content_base64": base64.b64encode(data).decode()})
        executor.execute(str(self.root), plan, phase="before_cache_warmup")
        executor.execute(str(self.root), dict(plan, operation="rollback"))
        self.assertEqual(self.source.read_bytes(), b"before\n")
        self.assertEqual(path.read_bytes(), b"before\n")

    def test_gate_only_common_identity_files_are_never_rewritten_and_guard_rollback(self):
        plan = _plan()
        identity = self.root / "docroot/app/identity.json"
        identity.write_bytes(b"identity\n")
        identity.chmod(0o440)
        stat_before = identity.stat()
        record = plan["patches"][0]
        record["source_paths"].append("docroot/app/identity.json")
        record["gate_logic"] = "vulnerable_all; fixed_all; mixed_or_unknown=error"
        for group in ("vulnerable", "fixed"):
            record["gate"].append({"group": group, "kind": "sha256", "path": "docroot/app/identity.json", "expected_sha256": hashlib.sha256(b"identity\n").hexdigest()})
        executor.execute(str(self.root), plan)
        self.assertEqual(identity.stat().st_ino, stat_before.st_ino)
        self.assertEqual(identity.stat().st_mode, stat_before.st_mode)
        identity.chmod(0o600)
        identity.write_bytes(b"changed identity\n")
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "identity_gate_mismatch"):
            executor.execute(str(self.root), dict(plan, operation="rollback"))
        self.assertEqual(self.source.read_bytes(), b"after\n")

    def test_unsupported_parameters_cannot_silently_drop_required_predicates(self):
        plan = _plan()
        plan["patches"][0]["parameters"] = {"migration_pending": "undeclared"}
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "parameters_not_supported"):
            executor.execute(str(self.root), plan)
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "docroot/app/fixture.txt"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"before\n")
        self.source.chmod(0o640)

    def tearDown(self):
        self.temp.cleanup()

    def test_verify_is_read_only_and_apply_is_idempotent_then_rollback_restores_metadata(self):
        plan = _plan()
        verify = dict(plan, operation="verify")
        result = executor.execute(str(self.root), verify)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.source.read_bytes(), b"before\n")

        applied = executor.execute(str(self.root), plan)
        self.assertEqual(applied["status"], "success")
        self.assertEqual(self.source.read_bytes(), b"after\n")
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o640)
        repeated = executor.execute(str(self.root), plan)
        self.assertEqual(repeated["status"], "success")
        self.assertEqual(self.source.read_bytes(), b"after\n")

        rollback_plan = dict(plan, operation="rollback")
        restored = executor.execute(str(self.root), rollback_plan)
        self.assertEqual(restored["rollback_succeeded"], True)
        self.assertEqual(self.source.read_bytes(), b"before\n")
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o640)

    def test_wrong_or_external_after_state_fails_closed_without_overwrite(self):
        plan = _plan()
        executor.execute(str(self.root), plan)
        self.source.write_bytes(b"operator-change\n")
        rollback_plan = dict(plan, operation="rollback")
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "after_hash_mismatch"):
            executor.execute(str(self.root), rollback_plan)
        self.assertEqual(self.source.read_bytes(), b"operator-change\n")

    def test_phase_plan_and_payload_path_are_validated_before_mutation(self):
        plan = _plan()
        plan["patches"][0]["payload_path"] = "../outside.patch"
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "patch_payload_reference_invalid"):
            executor.execute(str(self.root), plan)
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def test_new_file_is_removed_on_rollback(self):
        plan = _plan()
        target_rel = "docroot/app/new-fixture.txt"
        target = self.root / target_rel
        plan["patches"][0]["source_paths"] = [target_rel]
        plan["patches"][0]["gate"] = [
            {"group": "vulnerable", "kind": "path_state", "path": target_rel, "expected_state": "absent"},
            {"group": "fixed", "kind": "exact_count", "path": target_rel, "needle": "created", "expected_count": 1},
        ]
        plan["patches"][0]["gate_logic"] = "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error"
        patch_bytes = (
            b"diff --git a/docroot/app/new-fixture.txt b/docroot/app/new-fixture.txt\n"
            b"new file mode 100644\n"
            b"--- /dev/null\n"
            b"+++ b/docroot/app/new-fixture.txt\n"
            b"@@ -0,0 +1 @@\n"
            b"+created\n"
        )
        plan["payloads"][0]["path"] = "fixtures/create.patch"
        plan["patches"][0]["payload_path"] = "fixtures/create.patch"
        _payload(plan, patch_bytes)
        executor.execute(str(self.root), plan)
        self.assertEqual(target.read_bytes(), b"created\n")
        restored = executor.execute(str(self.root), dict(plan, operation="rollback"))
        self.assertTrue(restored["rollback_succeeded"])
        self.assertFalse(target.exists())

    def test_path_state_present_and_sha256_gates_use_exact_file_bytes(self):
        plan = _plan()
        plan["patches"][0]["gate_logic"] = "vulnerable_all; fixed_all; mixed_or_unknown=error"
        before_sha = hashlib.sha256(b"before\n").hexdigest()
        after_sha = hashlib.sha256(b"after\n").hexdigest()
        path = "docroot/app/fixture.txt"
        plan["patches"][0]["gate"] = [
            {"group": "vulnerable", "kind": "path_state", "path": path, "expected_state": "present"},
            {"group": "vulnerable", "kind": "sha256", "path": path, "expected_sha256": before_sha},
            {"group": "fixed", "kind": "path_state", "path": path, "expected_state": "present"},
            {"group": "fixed", "kind": "sha256", "path": path, "expected_sha256": after_sha},
        ]
        result = executor.execute(str(self.root), plan)
        self.assertEqual(result["status"], "success")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), after_sha)

    def test_sha256_mismatch_fails_closed_without_mutation(self):
        plan = _plan()
        path = "docroot/app/fixture.txt"
        plan["patches"][0]["gate"] = [
            {"group": "vulnerable", "kind": "sha256", "path": path, "expected_sha256": "0" * 64},
            {"group": "fixed", "kind": "sha256", "path": path, "expected_sha256": hashlib.sha256(b"after\n").hexdigest()},
        ]
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "ambiguous"):
            executor.execute(str(self.root), plan)
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def test_declared_legacy_backup_is_read_only_until_safe_generic_rollback(self):
        plan = _plan()
        self.source.write_bytes(b"after\n")
        backup = self.source.with_name(self.source.name + ".mcd-import-tag-713.bak")
        backup.write_bytes(b"before\n")
        metadata_path = self.source.with_name(self.source.name + ".mcd-import-tag-713.json")
        metadata = {
            "patch": "mautic-7.1.3-import-tag-detach-v1",
            "path": str(self.source.resolve()),
            "original_sha256": hashlib.sha256(b"before\n").hexdigest(),
            "patched_sha256": hashlib.sha256(b"after\n").hexdigest(),
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        plan["patches"][0]["legacy_state"] = {
            "kind": "backup_file_with_metadata",
            "backup_suffix": ".mcd-import-tag-713.bak",
            "metadata_suffix": ".mcd-import-tag-713.json",
            "metadata_fields": {
                "marker": "patch",
                "path": "path",
                "original_sha256": "original_sha256",
                "applied_sha256": "patched_sha256",
            },
            "expected_marker": "mautic-7.1.3-import-tag-detach-v1",
        }
        status = executor.execute(str(self.root), dict(plan, operation="status"))
        self.assertTrue(status["patches"][0]["legacy_rollback_source_validated"])
        self.assertFalse((self.root / ".mcd").exists())
        rollback = executor.execute(str(self.root), dict(plan, operation="rollback", run_id="legacy-import-rollback"))
        self.assertTrue(rollback["rollback_succeeded"])
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def test_legacy_metadata_mismatch_fails_closed(self):
        plan = _plan()
        self.source.write_bytes(b"after\n")
        backup = self.source.with_name(self.source.name + ".mcd-import-tag-713.bak")
        backup.write_bytes(b"before\n")
        metadata_path = self.source.with_name(self.source.name + ".mcd-import-tag-713.json")
        metadata_path.write_text(json.dumps({"patch": "wrong"}), encoding="utf-8")
        plan["patches"][0]["legacy_state"] = {
            "kind": "backup_file_with_metadata",
            "backup_suffix": ".mcd-import-tag-713.bak",
            "metadata_suffix": ".mcd-import-tag-713.json",
            "metadata_fields": {
                "marker": "patch",
                "path": "path",
                "original_sha256": "original_sha256",
                "applied_sha256": "patched_sha256",
            },
            "expected_marker": "mautic-7.1.3-import-tag-detach-v1",
        }
        with self.assertRaisesRegex(executor.PatchPlanV3Error, "legacy_state_metadata_mismatch"):
            executor.execute(str(self.root), dict(plan, operation="status"))
        self.assertEqual(self.source.read_bytes(), b"after\n")


if __name__ == "__main__":
    unittest.main()
